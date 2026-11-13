"""执行单轮货源采集，并协调独立的 OCR 工作进程。"""

import argparse
import json
import multiprocessing as mp
import re
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
from typing import Literal, TypedDict

from uiautomator2.exceptions import BaseException as UiAutomatorError

from .adb import ADBController
from .engines import HybridEngine, PaddleEngine, RapidEngine, WindowsOCR, open_engine
from .freight import (
    clean_freight,
    detail_confirmed,
    detail_control,
    detail_removed,
    extract_fields,
    feedback_blocked,
    recognize_detail,
    resolve_feedback,
)
from .storage import DEFAULT_DATABASE, Store, exclusive_run
from .vision import (
    Card,
    Line,
    Viewport,
    decode,
    detail_area,
    detail_loading,
    list_area,
    list_body,
    match_cards,
    read_viewport,
    visual_progress,
)

_OCR_RESPONSE_TIMEOUT = 600
_LIST_READ_ATTEMPTS = 6
_SECOND_OPINION_ENGINE = "RapidOCR PP-OCRv5 mobile/OpenVINO"
_AMBIGUOUS_WEIGHT_RANGE = re.compile(
    r"(?P<prefix>总重)?(?P<lower>[1-9]\d+)\.(?P<upper>[1-9]\d+)吨"
)

type _CachedPage = tuple[list[Line], int, float]
_SECOND_OPINION_FIELDS = {
    "weight",
    "other_info",
    "price",
    "loading_location",
    "unloading_location",
    "vehicle",
    "shipper",
    "cargo",
    "estimated_distance",
}


class _SecondOpinionBase(TypedDict):
    engine: str
    target_fields: list[str]
    fields: dict[str, str]


class _SecondOpinion(_SecondOpinionBase):
    review_fields: list[str]
    seconds: float
    raw_pages: list[list[Line]]
    applied_fields: list[str]


class _SecondOpinionFailure(_SecondOpinionBase):
    error: str


class _RouteListError(RuntimeError):
    """返回或滚动后未能确认原线路。"""


def _recognize_group(
    engine: HybridEngine | WindowsOCR | PaddleEngine | RapidEngine,
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
        clean_freight(pages, fields, review, task["card_key"])
        if "truck_count" not in fields and detail_loading(
            decode((store.root / paths[-1]).read_bytes())
        ):
            review.add("truck_count")
        second_opinion: _SecondOpinion | _SecondOpinionFailure | None = None
        if isinstance(engine, HybridEngine):
            targets = review & _SECOND_OPINION_FIELDS
            if not fields.get("weight"):
                targets.add("weight")
            if targets:
                start = monotonic()
                try:
                    second_opinion = _second_opinion(
                        engine,
                        store,
                        paths,
                        targets,
                        task["list_price"],
                        task["card_key"],
                    )
                except Exception as error:
                    seconds += monotonic() - start
                    second_opinion = {
                        "engine": _SECOND_OPINION_ENGINE,
                        "target_fields": sorted(targets),
                        "fields": {},
                        "error": str(error),
                    }
                    print(
                        f"#{capture_id} PP-OCRv5 second opinion failed "
                        f"(not applied): {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    seconds += second_opinion["seconds"]
                    weight_applied = _reconcile_consensus_weight(
                        fields, review, second_opinion
                    )
                    if weight_applied:
                        print(
                            f"#{capture_id} PP-OCRv5 consensus corrected weight "
                            f"to {fields['weight']}; other suggestions not applied",
                            flush=True,
                        )
                    else:
                        print(
                            f"#{capture_id} PP-OCRv5 second opinion "
                            f"(not applied): {second_opinion['fields']}",
                            flush=True,
                        )
        store.complete(
            capture_id, pages, fields, seconds, review, second_opinion=second_opinion
        )
        print(f"#{capture_id} OCR {seconds:.2f}s, {len(fields)}/10 fields", flush=True)
    except Exception as error:
        store.ocr_failed(capture_id, str(error))
        print(f"#{capture_id} OCR failed: {error}", file=sys.stderr, flush=True)


def _second_opinion(
    engine: HybridEngine,
    store: Store,
    paths: list[str],
    targets: set[str],
    list_price: str | None,
    card_key: str,
) -> _SecondOpinion:
    pages = []
    widths = []
    start = monotonic()
    for index, screenshot in enumerate(paths):
        image = decode((store.root / screenshot).read_bytes())
        first = index == 0
        pages.append(engine.locate_secondary(detail_area(image, first=first)))
        widths.append(image.width)
    fields, review = resolve_feedback(pages, widths, list_price, card_key)
    clean_freight(pages, fields, review, card_key)
    return {
        "engine": _SECOND_OPINION_ENGINE,
        "target_fields": sorted(targets),
        "fields": {key: fields[key] for key in sorted(targets) if key in fields},
        "review_fields": sorted(review),
        "seconds": monotonic() - start,
        "raw_pages": pages,
        "applied_fields": [],
    }


def _reconcile_consensus_weight(
    fields: dict[str, str], review: set[str], opinion: _SecondOpinion
) -> bool:
    """Apply only an unambiguous cross-engine decimal-to-range correction."""
    primary = fields.get("weight")
    secondary = opinion["fields"].get("weight")
    if (
        "weight" not in review
        or not primary
        or not secondary
        or "weight" in opinion["review_fields"]
    ):
        return False

    match = _AMBIGUOUS_WEIGHT_RANGE.search(primary)
    if not match:
        return False
    range_candidate = (
        primary[: match.start()]
        + match.group().replace(".", "-", 1)
        + primary[match.end() :]
    )
    if range_candidate != secondary:
        return False

    fields["weight"] = secondary
    review.remove("weight")
    opinion["applied_fields"].append("weight")
    return True


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

    def _wait_for_list(
        self, route: str = "", *, recover_detail: bool = False
    ) -> tuple[bytes, Viewport]:
        dismissed_voice = False
        back_taps = 0
        for attempt in range(_LIST_READ_ATTEMPTS):
            png, view = self._read_list()
            if recover_detail and view.voice_cancel is not None:
                if not dismissed_voice:
                    print("Dismissing system voice prompt", flush=True)
                    self.device.tap(*view.voice_cancel)
                    dismissed_voice = True
            elif view.is_list and view.route and (not route or view.route == route):
                return png, view
            elif (
                recover_detail
                and back_taps < 2
                and view.detail_back is not None
                and (dismissed_voice or attempt >= 2 + 2 * back_taps)
            ):
                print(
                    "Returning with the freight detail back button "
                    f"(attempt {back_taps + 1})",
                    flush=True,
                )
                self.device.tap(*view.detail_back)
                back_taps += 1
            if attempt + 1 < _LIST_READ_ATTEMPTS:
                sleep(0.8)
        raise _RouteListError(
            f"Could not confirm the freight route list after {_LIST_READ_ATTEMPTS} "
            f"checks (expected={route!r}, last OCR route={view.route!r}, "
            f"list controls detected={view.is_list})"
        )

    def _expand_fold(self, route: str, position: tuple[int, int]) -> None:
        x, y = position
        print(f"Tapping folded group at ({x}, {y})", flush=True)
        self.device.tap(x, y)
        for _ in range(_LIST_READ_ATTEMPTS):
            sleep(0.65)
            try:
                _, view = self._wait_for_list(route)
            except _RouteListError as error:
                raise _RouteListError(
                    f"After tapping the folded group at ({x}, {y}): {error}"
                ) from error
            # 等原入口离开该位置，期间不重复点击，避免点到展开后的货源。
            if all(abs(other_y - y) > 40 for _, other_y in view.folds):
                print("Expanded a folded group", flush=True)
                return
        raise RuntimeError(f"The folded group at ({x}, {y}) did not expand")

    def _wait_for_refresh(self, route: str) -> str | None:
        for _ in range(_LIST_READ_ATTEMPTS):
            sleep(0.8)
            try:
                _, view = self._wait_for_list(route)
            except _RouteListError as error:
                return str(error)
            if not view.refreshing:
                return None
        return "refresh indicator remained active"

    def _return_and_refresh(self, route: str, png: bytes, view: Viewport) -> str | None:
        for _ in range(40):
            self.worker.check()
            before = decode(png)
            width, height = before.size
            region = (
                width // 4,
                int(height * 0.34),
                width // 4,
                int(height * 0.86),
            )
            self.device.scroll(
                "down",
                region=region,
                duration_ms=530,
            )
            sleep(0.7)
            try:
                png, after = self._wait_for_list(route)
            except _RouteListError as error:
                return str(error)
            if after.refreshing:
                return self._wait_for_refresh(route)

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
            if stationary:
                print("Reached the top; pulling down to refresh", flush=True)
                self.device.scroll(
                    "down",
                    distance=int(height * 0.25),
                    region=region,
                    duration_ms=530,
                )
                sleep(0.7)
                try:
                    _, refreshed = self._wait_for_list(route)
                except _RouteListError as error:
                    return str(error)
                if refreshed.refreshing:
                    return self._wait_for_refresh(route)
                return None
            view = after
        return "could not confirm the top within 40 return gestures"

    def _capture_detail(
        self, card: Card, png: bytes, route: str | None = None
    ) -> int | None:
        capture_id = self.store.begin(card.key, card.price, route)
        paths = []
        try:
            directory = self.store.folder(capture_id)
            directory.mkdir(parents=True, exist_ok=True)
            paths.append(_save(directory / "card.png", png))
            print(
                f"#{capture_id} opening card at ({card.x}, {card.y}): {card.key}",
                flush=True,
            )
            self.device.tap(card.x, card.y)
            sleep(1.6)
            previous = None
            handled = set()
            loading = False
            for _ in range(10):
                first_png = self.device.screenshot()
                first_image = decode(first_png)
                paths_for_first = _save(directory / "detail-01.png", first_png)
                if len(paths) == 1:
                    paths.append(paths_for_first)
                lines = self.worker.request("detail", (capture_id, first_png, 0))
                if detail_removed(lines):
                    print(f"#{capture_id} skipped: freight was removed", flush=True)
                    self.device.back()
                    sleep(0.65)
                    self.store.discard_capture(capture_id, paths)
                    return None
                control = detail_control(lines)
                if control is not None and control[0] not in handled:
                    name, (_, x, y, right, bottom) = control
                    handled.add(name)
                    height = first_image.height
                    print(f"#{capture_id} tapping detail control: {name}", flush=True)
                    self.device.tap(
                        (x + right) // 2, (y + bottom) // 2 + int(height * 0.10)
                    )
                    sleep(0.75)
                    continue
                if detail_loading(first_image):
                    if not loading:
                        print(f"#{capture_id} waiting for detail to load", flush=True)
                    loading = True
                elif detail_confirmed(lines):
                    previous = detail_area(first_image, first=False)
                    break
                sleep(0.65)
            if previous is None:
                if loading and detail_confirmed(lines):
                    print(
                        f"#{capture_id} detail still loading; keeping visible fields",
                        flush=True,
                    )
                    self._finish_detail(capture_id, paths)
                    return capture_id
                raise RuntimeError(
                    "Detail page did not finish loading or could not be confirmed"
                )

            width, height = first_image.size
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
            self._finish_detail(capture_id, paths)
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

    def _finish_detail(self, capture_id: int, paths: list[Path]) -> None:
        self.store.publish(capture_id, paths)
        self.device.back()
        sleep(0.65)
        print(
            f"#{capture_id} captured {len(paths) - 1} detail screenshots",
            flush=True,
        )

    def run(self, maximum: int | None, expected_route: str = "") -> str:
        old_cards: tuple[Card, ...] = ()
        visits: list[int | None] = []
        route = expected_route
        old_view = None
        old_image = None
        history = deque(maxlen=12)
        scroll = False
        stale = 0
        captured = 0
        captured_keys: set[str] = set()
        route_announced = False
        returning_from_detail = False
        while True:
            # 队列等待期间可能插入新货源，继续操作前重新识别当前列表。
            self._wait_for_capacity()
            png, view = self._wait_for_list(route, recover_detail=returning_from_detail)
            returning_from_detail = False
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
            if not route_announced and route:
                print(f"Route: {route}", flush=True)
                route_announced = True
            if view.folds:
                self._expand_fold(route, view.folds[0])
                continue
            pending = next(
                (
                    i
                    for i, (card, visit) in enumerate(
                        zip(view.cards, visits, strict=True)
                    )
                    if not card.viewed
                    and not card.recommended
                    and visit is None
                    and card.key not in captured_keys
                ),
                None,
            )
            if pending is not None:
                card = view.cards[pending]
                capture_id = self._capture_detail(card, png, route)
                captured_keys.add(card.key)
                returning_from_detail = True
                if capture_id is None:
                    continue
                visits[pending] = capture_id
                history.append((view.cards, visits.copy()))
                captured += 1
                if maximum is not None and captured >= maximum:
                    return f"Stopped at the requested limit ({captured})"
                continue
            if any(card.recommended for card in view.cards):
                result = self._return_and_refresh(route, png, view)
                if result is not None:
                    return f"Stopped at the recommendation section: {result}"
                print(
                    "Refreshed after the recommendation section; restarting collection",
                    flush=True,
                )
                old_cards = ()
                visits = []
                old_view = None
                old_image = None
                history.clear()
                scroll = False
                stale = 0
                returning_from_detail = False
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
        default=DEFAULT_DATABASE,
        help="SQLite 文件路径（默认 %(default)s）",
    )
    parser.add_argument(
        "--ocr",
        choices=("auto", "windows", "hybrid", "rapid", "paddle"),
        default="auto",
        help="OCR 后端；auto 在 Windows 上启用字段级二次识别（默认 %(default)s）",
    )
    parser.add_argument(
        "--capacity", type=int, default=3, help="未完成截图组上限（默认 %(default)s）"
    )
    parser.add_argument("--max-orders", type=int, help="本轮最多采集的货源数")
    parser.add_argument(
        "--expected-route",
        default="",
        help="预期线路；手机顶部识别的线路不一致时停止采集",
    )
    processing = parser.add_mutually_exclusive_group()
    processing.add_argument(
        "--process-only", action="store_true", help="仅处理已有截图组"
    )
    processing.add_argument(
        "--reprocess-all", action="store_true", help="用已有截图重做识别和清洗"
    )
    args = parser.parse_args()
    if args.capacity < 1 or (args.max_orders is not None and args.max_orders < 1):
        parser.error("--capacity 和 --max-orders 必须为正整数")

    path = args.database.resolve()
    process_only = args.process_only or args.reprocess_all
    try:
        device_lock = (
            nullcontext()
            if process_only
            else exclusive_run(Path(gettempdir()) / "x001-android-device")
        )
        with device_lock, exclusive_run(path):
            store = Store(path)
            try:
                store.initialize()
                store.recover()
                if args.reprocess_all:
                    store.requeue_completed()
                done_query = "SELECT count(*) FROM captures WHERE status='done'"
                before = store.db.execute(done_query).fetchone()[0]
                worker = Worker(path, args.ocr)
                graceful = False
                try:
                    worker.receive()
                    start = monotonic()
                    failure = None
                    if not process_only:
                        try:
                            print(
                                Collector(
                                    ADBController(), store, worker, args.capacity
                                ).run(args.max_orders, args.expected_route),
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
