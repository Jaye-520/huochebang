"""执行单轮货源采集，并协调独立的 OCR 工作进程。"""

import argparse
import json
import multiprocessing as mp
import sqlite3
import sys
from collections import deque
from collections.abc import Iterator
from contextlib import nullcontext
from multiprocessing.connection import Connection
from pathlib import Path
from subprocess import SubprocessError
from tempfile import gettempdir
from time import monotonic, sleep
from typing import Literal

from uiautomator2.exceptions import BaseException as UiAutomatorError

from .adb import ADBController
from .engines import PaddleEngine, WindowsOCR, open_engine
from .freight import (
    detail_confirmed,
    detail_control,
    extract_fields,
    feedback_blocked,
    recognize_detail,
    resolve_feedback,
)
from .storage import Store, exclusive_run
from .vision import (
    Card,
    Line,
    Viewport,
    decode,
    detail_area,
    list_area,
    list_body,
    match_cards,
    read_viewport,
    visual_progress,
)

_RECOMMENDATION_LIMIT = 3
_OCR_RESPONSE_TIMEOUT = 600

type _CachedPage = tuple[list[Line], int, float]


def _recognize_group(
    engine: WindowsOCR | PaddleEngine,
    store: Store,
    task: sqlite3.Row,
    cached: dict[int, _CachedPage],
) -> Iterator[bool]:
    """逐张识别已提交的截图，在相邻截图之间让出 OCR 进程。"""
    capture_id = task["id"]
    pages, widths, seconds = [], [], 0.0
    try:
        paths = json.loads(task["screenshots"])[1:]
        for index, screenshot in enumerate(paths):
            if index in cached:
                lines, width, elapsed = cached[index]
            else:
                start = monotonic()
                image = decode((store.root / screenshot).read_bytes())
                first = index == 0
                lines = recognize_detail(
                    engine, detail_area(image, first=first), first=first
                )
                width, elapsed = image.width, monotonic() - start
            pages.append(lines)
            widths.append(width)
            seconds += elapsed
            yield True
        if not pages or not detail_confirmed(pages[0]):
            raise ValueError("Could not verify the first detail screenshot")
        fields, review = resolve_feedback(
            pages, widths, task["list_price"], task["card_key"]
        )
        store.complete(capture_id, pages, fields, seconds, review)
        print(f"#{capture_id} OCR {seconds:.2f}s, {len(fields)}/10 fields", flush=True)
    except Exception as error:
        store.ocr_failed(capture_id, str(error))
        print(f"#{capture_id} OCR failed: {error}", file=sys.stderr, flush=True)


def _worker(path: Path, backend: str, connection: Connection) -> None:
    try:
        engine = open_engine(backend)
    except Exception as error:
        connection.send(("error", str(error)))
        connection.close()
        return
    detail_pages: dict[int, dict[int, _CachedPage]] = {}
    job = None
    try:
        store = Store(path)
        try:
            connection.send(("ready", None))
            while True:
                if connection.poll():
                    kind, args = connection.recv()
                    if kind == "stop":
                        return
                    try:
                        if kind == "list":
                            result = engine.locate(list_area(decode(args)))
                        elif kind == "detail":
                            capture_id, png, index = args
                            start = monotonic()
                            image = decode(png)
                            first = index == 0
                            result = recognize_detail(
                                engine, detail_area(image, first=first), first=first
                            )
                            detail_pages.setdefault(capture_id, {})[index] = (
                                result,
                                image.width,
                                monotonic() - start,
                            )
                        else:
                            raise ValueError(f"Unknown OCR request: {kind}")
                        connection.send(("ok", result))
                    except Exception as error:
                        connection.send(("error", str(error)))
                    continue

                if job is None:
                    task = store.take()
                    if task is None:
                        connection.poll(0.12)
                        continue
                    job = _recognize_group(
                        engine, store, task, detail_pages.pop(task["id"], {})
                    )
                # 两张排队截图之间可以插入当前页面的识别请求。
                if not next(job, False):
                    job = None
        finally:
            store.db.close()
    except EOFError:
        pass
    finally:
        engine.close()
        connection.close()


class Worker:
    """负责 OCR 子进程的启动、请求应答和退出。"""

    def __init__(self, path: Path, backend: str) -> None:
        context = mp.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_worker, args=(path, backend, child))
        try:
            self.process.start()
        except BaseException:
            self.connection.close()
            raise
        finally:
            child.close()

    def check(self) -> None:
        if not self.process.is_alive():
            raise RuntimeError(f"OCR worker exited with code {self.process.exitcode}")

    def receive(self) -> list[Line] | None:
        deadline = monotonic() + _OCR_RESPONSE_TIMEOUT
        try:
            while not self.connection.poll(0.25):
                self.check()
                if monotonic() > deadline:
                    raise RuntimeError("OCR worker did not respond within 10 minutes")
            status, result = self.connection.recv()
        except (OSError, EOFError) as error:
            raise RuntimeError("OCR worker connection closed") from error
        if status == "error":
            raise RuntimeError(f"OCR worker: {result}")
        return result

    def request(
        self, kind: Literal["list", "detail"], data: bytes | tuple[int, bytes, int]
    ) -> list[Line]:
        self.check()
        try:
            self.connection.send((kind, data))
        except OSError as error:
            raise RuntimeError("OCR worker connection closed") from error
        result = self.receive()
        if result is None:
            raise RuntimeError(
                f"OCR worker did not return recognition results for {kind}"
            )
        return result

    def close(self, *, graceful: bool) -> None:
        try:
            if self.process.is_alive() and graceful:
                self.connection.send(("stop", None))
                self.process.join(timeout=5)
        except OSError:
            pass
        finally:
            self.connection.close()
            if self.process.is_alive():
                self.process.terminate()
            self.process.join()


def _save(path: Path, data: bytes) -> Path:
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


class Collector:
    """独占手机，遍历当前线路并提交完整的截图组。"""

    def __init__(
        self, device: ADBController, store: Store, worker: Worker, capacity: int
    ) -> None:
        self.device = device
        self.store = store
        self.worker = worker
        self.capacity = capacity

    def _wait_for_capacity(self) -> None:
        while self.store.active_count() >= self.capacity:
            self.worker.check()
            sleep(0.15)
        self.worker.check()

    def _read_list(self) -> tuple[bytes, Viewport]:
        png = self.device.screenshot()
        return png, read_viewport(decode(png), self.worker.request("list", png))

    def _return_and_refresh(self, route: str, png: bytes, view: Viewport) -> str:
        # 回到顶部后，同一个下滑手势会触发下拉刷新。
        for _ in range(40):
            self.worker.check()
            before = decode(png)
            width, height = before.size
            self.device.scroll(
                "down",
                region=(width // 4, int(height * 0.34), width // 4, int(height * 0.86)),
                duration_ms=530,
            )
            sleep(0.7)
            for _ in range(3):
                png, after = self._read_list()
                if after.is_list and after.route == route:
                    break
                sleep(0.45)
            else:
                return (
                    "stopped returning because the route screen could not be confirmed"
                )

            anchors = {card.key: card.y for card in view.cards}
            shared = [
                card.y - anchors[card.key]
                for card in after.cards
                if card.key in anchors
            ]
            stationary = (
                bool(shared)
                and all(abs(delta) <= 18 for delta in shared)
                or not visual_progress(list_body(before), list_body(decode(png)))
            )
            if after.refreshing or stationary:
                return "reached the top and issued the pull-to-refresh gesture"
            view = after
        return "could not confirm the top within 40 return gestures"

    def _capture_detail(self, card: Card, png: bytes) -> int:
        capture_id = self.store.begin(card.key, card.price)
        paths = []
        try:
            directory = self.store.folder(capture_id)
            directory.mkdir(parents=True, exist_ok=True)
            paths.append(_save(directory / "card.png", png))
            self.device.tap(card.x, card.y)
            sleep(1.6)
            previous = None
            handled = set()
            for _ in range(6):
                first_png = self.device.screenshot()
                paths_for_first = _save(directory / "detail-01.png", first_png)
                if len(paths) == 1:
                    paths.append(paths_for_first)
                lines = self.worker.request("detail", (capture_id, first_png, 0))
                control = detail_control(lines)
                if control is not None and control[0] not in handled:
                    name, (_, x, y, right, bottom) = control
                    handled.add(name)
                    height = decode(first_png).height
                    self.device.tap(
                        (x + right) // 2, (y + bottom) // 2 + int(height * 0.10)
                    )
                    sleep(0.75)
                    continue
                if detail_confirmed(lines):
                    previous = detail_area(decode(first_png), first=False)
                    break
                sleep(0.65)
            if previous is None:
                raise RuntimeError(
                    "Detail page could not be confirmed; check the phone"
                )

            width, height = decode(first_png).size
            covered = set()
            supplements = 0

            def record(frame: bytes, *, supplemental: bool = False) -> list[Line]:
                index = len(paths) - 1
                suffix = "-supplement" if supplemental else ""
                paths.append(
                    _save(directory / f"detail-{index + 1:02}{suffix}.png", frame)
                )
                return self.worker.request("detail", (capture_id, frame, index))

            for _ in range(8):
                regular = previous
                hidden = feedback_blocked(lines, width)
                covered.update(extract_fields([lines], None, card.key).keys() - hidden)
                pending = hidden - covered
                for direction in ("up", "down"):
                    if not pending or supplements >= 2:
                        break
                    supplements += 1
                    self.device.scroll(
                        direction,
                        distance=int(height * 0.115),
                        region=(
                            width // 4,
                            int(height * 0.42),
                            width // 4,
                            int(height * 0.68),
                        ),
                        duration_ms=520,
                    )
                    sleep(0.65)
                    frame = self.device.screenshot()
                    current = detail_area(decode(frame), first=False)
                    if visual_progress(previous, current):
                        lines = record(frame, supplemental=True)
                        previous = current
                        hidden = feedback_blocked(lines, width)
                        covered.update(
                            extract_fields([lines], None, card.key).keys() - hidden
                        )
                        pending -= covered
                        print(
                            f"#{capture_id} captured a feedback supplement", flush=True
                        )
                self.device.scroll(
                    "up",
                    region=(
                        width // 4,
                        int(height * 0.44),
                        width // 4,
                        int(height * 0.74),
                    ),
                    duration_ms=480,
                )
                sleep(0.65)
                detail_png = self.device.screenshot()
                current = detail_area(decode(detail_png), first=False)
                if not visual_progress(previous, current) or not visual_progress(
                    regular, current
                ):
                    # 再取一帧，给延迟加载的详情区块留出时间。
                    sleep(0.45)
                    detail_png = self.device.screenshot()
                    current = detail_area(decode(detail_png), first=False)
                    if not visual_progress(previous, current) or not visual_progress(
                        regular, current
                    ):
                        break
                lines = record(detail_png)
                previous = current
            else:
                raise RuntimeError("Detail scrolling did not settle after eight swipes")
            self.store.publish(capture_id, paths)
            self.device.back()
            sleep(0.65)
            print(
                f"#{capture_id} captured {len(paths) - 1} detail screenshots",
                flush=True,
            )
            return capture_id
        except Exception as error:
            # 截图组已经提交时，返回列表失败也保留其待处理状态。
            if (
                self.store.db.execute(
                    "SELECT status FROM captures WHERE id=?", (capture_id,)
                ).fetchone()[0]
                == "capturing"
            ):
                self.store.publish(capture_id, paths, str(error))
            raise

    def run(self, maximum: int | None) -> str:
        old_cards: tuple[Card, ...] = ()
        visits: list[int | None] = []
        route = ""
        old_view = None
        old_image = None
        history = deque(maxlen=12)
        scroll = False
        stale = 0
        captured = 0
        recommendations = 0
        last_fold = None
        while True:
            # 队列等待期间可能插入新货源，继续操作前重新识别当前列表。
            self._wait_for_capacity()
            for _ in range(3):
                png, view = self._read_list()
                if view.is_list and view.route and (not route or route == view.route):
                    break
                sleep(0.45)
            else:
                raise RuntimeError(
                    "Expected the original freight route list; check the phone"
                )
            route = view.route
            image = decode(png)
            width, height = image.size
            current_body = list_body(image)
            if scroll:
                changed = (
                    [card.key for card in old_cards]
                    != [card.key for card in view.cards]
                    or view.folds != old_view.folds
                    or visual_progress(old_image, current_body)
                )
                stale = 0 if changed else stale + 1
            visits = match_cards(old_cards, visits, view.cards, scroll=scroll)
            for earlier_cards, earlier_visits in reversed(history):
                if all(visit is not None for visit in visits):
                    break
                known = match_cards(
                    earlier_cards, earlier_visits, view.cards, historical=True
                )
                visits = [
                    current if current is not None else previous
                    for current, previous in zip(visits, known, strict=True)
                ]
            old_cards, old_view = view.cards, view
            old_image = current_body
            scroll = False
            if view.folds:
                fold = (tuple(card.key for card in view.cards), view.folds[0])
                if fold == last_fold:
                    raise RuntimeError(
                        "The visible folded group did not expand; check the phone"
                    )
                last_fold = fold
                recommendations = 0
                self.device.tap(*view.folds[0])
                print("Expanded a folded group", flush=True)
                sleep(0.65)
                continue
            last_fold = None
            pending = next((i for i, visit in enumerate(visits) if visit is None), None)
            if pending is not None:
                card = view.cards[pending]
                visits[pending] = self._capture_detail(card, png)
                history.append((view.cards, visits.copy()))
                captured += 1
                recommendations = recommendations + 1 if card.recommended else 0
                if maximum is not None and captured >= maximum:
                    return f"Stopped at the requested limit ({captured})"
                if recommendations >= _RECOMMENDATION_LIMIT and all(
                    candidate.recommended for candidate in view.cards[pending + 1 :]
                ):
                    current_png, current_view = self._read_list()
                    if current_view.is_list and current_view.route == route:
                        if current_view.folds or any(
                            not candidate.recommended and visit is None
                            for candidate, visit in zip(
                                current_view.cards,
                                match_cards(view.cards, visits, current_view.cards),
                                strict=True,
                            )
                        ):
                            continue
                        result = self._return_and_refresh(
                            route, current_png, current_view
                        )
                    else:
                        result = (
                            "could not confirm the route after returning from detail"
                        )
                    return f"Stopped after {recommendations} recommendations: {result}"
                continue
            if view.end:
                return f"Reached the list end ({captured} captures)"
            if stale >= 3:
                return (
                    "No progress after three scrolls; "
                    f"list end unconfirmed ({captured} captures)"
                )
            start = int(height * 0.86)
            # 翻页时尽量保留一张卡片，便于定位两屏重叠的货源。
            shift = min(
                int(height * 0.53),
                max(
                    int(height * 0.27),
                    (view.cards[-1].y - view.cards[0].y) if len(view.cards) > 1 else 0,
                ),
            )
            self.device.scroll(
                "up",
                region=(width // 4, start - shift, width // 4, start),
                duration_ms=530,
            )
            print("Scrolled to the next list viewport", flush=True)
            sleep(0.7)
            scroll = True


def main() -> None:
    parser = argparse.ArgumentParser(description="对手机上已打开的线路采集一轮")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/x001.sqlite3"),
        help="SQLite 文件路径（默认 %(default)s）",
    )
    parser.add_argument(
        "--ocr",
        choices=("auto", "windows", "paddle"),
        default="auto",
        help="OCR 后端（默认 %(default)s）",
    )
    parser.add_argument(
        "--capacity", type=int, default=3, help="未完成截图组上限（默认 %(default)s）"
    )
    parser.add_argument("--max-orders", type=int, help="本轮最多采集的货源数")
    parser.add_argument("--process-only", action="store_true", help="仅处理已有截图组")
    args = parser.parse_args()
    if args.capacity < 1 or (args.max_orders is not None and args.max_orders < 1):
        parser.error("--capacity 和 --max-orders 必须为正整数")

    path = args.database.resolve()
    try:
        device_lock = (
            nullcontext()
            if args.process_only
            else exclusive_run(Path(gettempdir()) / "x001-android-device")
        )
        with device_lock, exclusive_run(path):
            store = Store(path)
            try:
                store.initialize()
                store.recover()
                done_query = "SELECT count(*) FROM captures WHERE status='done'"
                before = store.db.execute(done_query).fetchone()[0]
                worker = Worker(path, args.ocr)
                graceful = False
                try:
                    worker.receive()
                    start = monotonic()
                    failure = None
                    if not args.process_only:
                        try:
                            print(
                                Collector(
                                    ADBController(), store, worker, args.capacity
                                ).run(args.max_orders),
                                flush=True,
                            )
                        except (
                            OSError,
                            RuntimeError,
                            SubprocessError,
                            UiAutomatorError,
                        ) as error:
                            failure = (
                                str(error.args[0])
                                if isinstance(error, UiAutomatorError) and error.args
                                else str(error)
                            )
                    while store.active_count():
                        worker.check()
                        sleep(0.15)
                    graceful = True
                    added = store.db.execute(done_query).fetchone()[0] - before
                    elapsed = max(0.001, monotonic() - start)
                    print(
                        f"{added} groups processed in {elapsed:.1f}s "
                        f"({60 * added / elapsed:.1f}/min)"
                    )
                    failures = store.db.execute(
                        "SELECT count(*) FROM captures WHERE status='ocr_failed'"
                    ).fetchone()[0]
                    if failure:
                        raise RuntimeError(failure)
                    if failures:
                        raise RuntimeError(
                            f"{failures} OCR groups can be retried with --process-only"
                        )
                finally:
                    worker.close(graceful=graceful)
            finally:
                store.db.close()
    except KeyboardInterrupt:
        parser.exit(
            130, "Interrupted. Saved screenshots can be processed on restart.\n"
        )
    except (OSError, RuntimeError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
