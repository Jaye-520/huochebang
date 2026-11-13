"""Local web console for the live collector database and process."""

import argparse
import base64
import hmac
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4

from .adb import DEFAULT_ADB_PATH
from .export import export_workbook
from .filters import FreightFilters
from .storage import DEFAULT_DATABASE, FIELDS, REPORT_FIELDS, Store, exclusive_run

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).parent / "web_static"
ADB = DEFAULT_ADB_PATH


def local_timestamp(value: str, *, hour: bool = False) -> str:
    """打印 SQLite 时间时保持本地表述，避免把无时区字符串再次按 UTC 解释。"""
    stamp = value + (":00:00" if hour else "")
    dt = datetime.fromisoformat(stamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    return local.strftime("%Y-%m-%d %H" if hour else "%Y-%m-%d %H:%M:%S")


class Console:
    def __init__(self, database: Path):
        self.database = database.resolve()
        self.lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.mode = ""
        self.started_at = ""
        self.exit_code: int | None = None
        self.logs: deque[str] = deque(maxlen=160)

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        with process.stdout:
            for line in process.stdout:
                with self.lock:
                    self.logs.append(line.rstrip())
        code = process.wait()
        with self.lock:
            self.exit_code = code
            self.logs.append(f"采集进程已结束，退出码 {code}")

    def start(
        self,
        mode: str,
        maximum: int | None,
        capacity: int,
        ocr: str,
        expected_route: str = "",
    ) -> None:
        if mode not in {"collect", "process", "reprocess"}:
            raise ValueError("无效的运行模式")
        if maximum is not None and not 1 <= maximum <= 100000:
            raise ValueError("采集上限需为正整数")
        if not 1 <= capacity <= 100:
            raise ValueError("队列容量需为 1–100")
        if ocr not in {"auto", "windows", "hybrid", "rapid", "paddle"}:
            raise ValueError("无效的 OCR 模式")
        allowed_routes = {
            "宁夏天津",
            "宁夏河北",
            "天津宁夏",
            "天津河北",
            "河北宁夏",
            "河北天津",
        }
        if mode == "collect" and expected_route not in allowed_routes:
            raise ValueError("请先选择手机上当前打开的线路")
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise ValueError("已有任务正在运行")
            command = [
                sys.executable,
                "-u",
                "-m",
                "x001.collect",
                "--database",
                str(self.database),
                "--ocr",
                ocr,
            ]
            if mode == "collect":
                command += [
                    "--capacity",
                    str(capacity),
                    "--expected-route",
                    expected_route,
                ]
                if maximum is not None:
                    command += ["--max-orders", str(maximum)]
            else:
                command.append(
                    "--process-only" if mode == "process" else "--reprocess-all"
                )
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            self.process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            self.mode = mode
            self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.exit_code = None
            self.logs.clear()
            self.logs.append("任务已启动，正在等待采集器初始化…")
            threading.Thread(
                target=self._read_output, args=(self.process,), daemon=True
            ).start()

    def image(self, capture_id: int, index: int) -> bytes:
        if capture_id < 1 or index < 0:
            raise ValueError("无效的图片编号")
        db = sqlite3.connect(f"file:{self.database.as_posix()}?mode=ro", uri=True)
        try:
            row = db.execute(
                "SELECT screenshots FROM captures WHERE id=?", (capture_id,)
            ).fetchone()
        finally:
            db.close()
        if row is None or not row[0]:
            raise FileNotFoundError("该货源没有可查看的截图")
        names = json.loads(row[0])
        if not isinstance(names, list) or index >= len(names):
            raise FileNotFoundError("截图不存在")
        path = (self.database.parent / names[index]).resolve()
        folder_name = (
            "captures"
            if self.database.name == "x001.sqlite3"
            else f"{self.database.name}-captures"
        )
        captures = (self.database.parent / folder_name).resolve()
        if not path.is_relative_to(captures) or path.suffix.lower() != ".png":
            raise ValueError("无效的截图路径")
        return path.read_bytes()

    def clear_images(self) -> dict:
        """Clear screenshots but preserve extracted rows and reset file numbers."""
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise ValueError("请先停止采集任务，再清除截图")
            with exclusive_run(self.database):
                store = Store(self.database)
                try:
                    store.initialize()
                    if store.active_count():
                        raise ValueError("数据库仍有待处理截图，请先完成识别")
                    captures = store.captures.resolve()
                    root = self.database.parent.resolve()
                    if not captures.is_relative_to(root) or captures == root:
                        raise ValueError("截图目录超出数据目录")
                    count = (
                        sum(1 for _ in captures.rglob("*.png"))
                        if captures.exists()
                        else 0
                    )
                    backup = root / f"captures-clearing-{uuid4().hex}"
                    moved = False
                    try:
                        if captures.exists():
                            captures.rename(backup)
                            moved = True
                        with store.db:
                            store.db.execute("UPDATE captures SET screenshots=NULL")
                            store.db.execute(
                                "UPDATE image_sequence SET next_number=1 WHERE id=1"
                            )
                    except Exception:
                        if moved and backup.exists():
                            backup.rename(captures)
                        raise
                    if moved:
                        shutil.rmtree(backup)
                    return {"removed": count, "next_image_number": 1}
                finally:
                    store.db.close()

    def stop(self) -> None:
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                raise ValueError("当前没有正在运行的任务")
            self.logs.append("正在停止任务…")
            if os.name == "nt":
                try:
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)
                except OSError:
                    self.process.terminate()
            else:
                self.process.send_signal(signal.SIGINT)

    def process_state(self) -> dict:
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return {
                "running": running,
                "mode": self.mode,
                "started_at": self.started_at,
                "exit_code": self.exit_code,
                "logs": list(self.logs)[-60:],
            }

    def snapshot(
        self,
        route: str = "",
        search: str = "",
        vehicle: str = "",
        trucks: str = "",
        price: str = "",
        length: str = "",
        period: str = "",
        day: str = "",
    ) -> dict:
        state = self.process_state()
        if not self.database.exists():
            return {
                "ready": False,
                "process": state,
                "stats": {},
                "routes": [],
                "records": [],
                "matching_total": 0,
                "activity": [],
            }
        db = sqlite3.connect(
            f"file:{self.database.as_posix()}?mode=ro", uri=True, timeout=3
        )
        db.row_factory = sqlite3.Row
        try:
            statuses = dict(
                db.execute("SELECT status, count(*) FROM captures GROUP BY status")
            )
            routes = [
                dict(row)
                for row in db.execute(
                    "SELECT route, count(*) AS total FROM captures "
                    "WHERE route IS NOT NULL AND trim(route) != '' "
                    "GROUP BY route ORDER BY total DESC"
                )
            ]
            where, params = FreightFilters(
                route, search, vehicle, trucks, price, length, period, day
            ).sql()
            matching_total = db.execute(
                "SELECT count(*) FROM captures c LEFT JOIN freight f "
                f"ON f.capture_id=c.id WHERE {where}",
                params,
            ).fetchone()[0]
            fields = ", ".join("f." + field for field in FIELDS)
            records = []
            for row in db.execute(
                "SELECT c.id, c.captured_at, c.route, c.card_key, "
                "c.status, c.error, c.list_price, c.screenshots, f.review_fields, "
                f"{fields} FROM captures c LEFT JOIN freight f ON f.capture_id=c.id "
                f"WHERE {where} ORDER BY c.id DESC LIMIT 200",
                params,
            ):
                item = dict(row)
                paths = json.loads(item.pop("screenshots") or "[]")
                item["images"] = [
                    {
                        "name": Path(name).name,
                        "url": f"/api/image?id={item['id']}&index={index}",
                    }
                    for index, name in enumerate(paths)
                ]
                item["review_fields"] = json.loads(item["review_fields"] or "[]")
                item["captured_at"] = local_timestamp(item["captured_at"])
                records.append(item)
            activity = [
                dict(row)
                for row in db.execute(
                    "SELECT substr(captured_at,1,13) AS hour, count(*) AS total "
                    "FROM captures GROUP BY hour ORDER BY hour DESC LIMIT 12"
                )
            ][::-1]
            for item in activity:
                item["hour"] = local_timestamp(item["hour"], hour=True)
            analysis_filter = " AND c.route=?" if route else ""
            analysis_params = (route,) if route else ()
            quality = db.execute(
                f"""
                SELECT count(*) AS total,
                       sum(CASE WHEN json_array_length(f.review_fields) > 0
                           THEN 1 ELSE 0 END) AS review,
                       sum(CASE WHEN trim(coalesce(f.price, '')) != ''
                           THEN 1 ELSE 0 END) AS price,
                       sum(CASE WHEN trim(coalesce(f.weight, '')) != ''
                           THEN 1 ELSE 0 END) AS weight
                FROM captures c JOIN freight f ON f.capture_id=c.id
                WHERE c.status='done'{analysis_filter}
            """,
                analysis_params,
            ).fetchone()
            cargo_top = [
                dict(row)
                for row in db.execute(
                    f"""
                SELECT f.cargo AS name, count(*) AS total
                FROM captures c JOIN freight f ON f.capture_id=c.id
                WHERE c.status='done' AND trim(coalesce(f.cargo, '')) != ''
                {analysis_filter}
                GROUP BY f.cargo ORDER BY total DESC LIMIT 6
            """,
                    analysis_params,
                )
            ]
            return {
                "ready": True,
                "process": state,
                "stats": {
                    "total": sum(statuses.values()),
                    "done": statuses.get("done", 0),
                    "pending": sum(
                        statuses.get(s, 0)
                        for s in ("capturing", "queued", "processing")
                    ),
                    "failed": sum(
                        statuses.get(s, 0) for s in ("capture_failed", "ocr_failed")
                    ),
                    "routes": len(routes),
                },
                "routes": routes,
                "analysis": {"quality": dict(quality), "cargo_top": cargo_top},
                "records": records,
                "matching_total": matching_total,
                "activity": activity,
                "fields": REPORT_FIELDS,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        finally:
            db.close()


class Handler(BaseHTTPRequestHandler):
    server: "WebServer"

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _authorized(self) -> bool:
        if self.server.token is None:
            return True
        expected = (
            "Basic " + base64.b64encode(f"x001:{self.server.token}".encode()).decode()
        )
        if hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="X001 操作台"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(
        self, status: int, body: bytes, content_type: str, download_name: str = ""
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if download_name:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="x001-freight.xlsx"; '
                f"filename*=UTF-8''{quote(download_name, safe='')}",
            )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict) -> None:
        self._send(
            status,
            json.dumps(value, ensure_ascii=False).encode(),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/snapshot":
            query = parse_qs(parsed.query)
            try:
                self._json(
                    200,
                    self.server.console.snapshot(
                        query.get("route", [""])[0][:100],
                        query.get("search", [""])[0][:100],
                        query.get("vehicle", [""])[0][:30],
                        query.get("trucks", [""])[0][:30],
                        query.get("price", [""])[0][:30],
                        query.get("length", [""])[0][:30],
                        query.get("period", [""])[0][:30],
                        query.get("day", [""])[0][:30],
                    ),
                )
            except ValueError as error:
                self._json(400, {"error": str(error)})
            except sqlite3.Error as error:
                self._json(503, {"error": f"数据库暂时不可读：{error}"})
            return
        if parsed.path == "/api/device":
            try:
                result = subprocess.run(
                    [str(ADB), "devices"], capture_output=True, text=True, timeout=4
                )
                devices = [
                    line.split()[0]
                    for line in result.stdout.splitlines()[1:]
                    if line.strip().endswith("device")
                ]
                self._json(200, {"connected": bool(devices), "devices": devices})
            except (OSError, subprocess.TimeoutExpired) as error:
                self._json(200, {"connected": False, "error": str(error)})
            return
        if parsed.path == "/api/image":
            query = parse_qs(parsed.query)
            try:
                image = self.server.console.image(
                    int(query.get("id", [""])[0]),
                    int(query.get("index", [""])[0]),
                )
                self._send(200, image, "image/png")
            except ValueError, FileNotFoundError, sqlite3.Error, json.JSONDecodeError:
                self.send_error(HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/export":
            query = parse_qs(parsed.query)
            route = query.get("route", [""])[0][:100]
            try:
                with TemporaryDirectory() as directory:
                    path = Path(directory) / "x001-freight.xlsx"
                    export_workbook(
                        self.server.console.database,
                        path,
                        route,
                        search=query.get("search", [""])[0][:100],
                        vehicle=query.get("vehicle", [""])[0][:30],
                        trucks=query.get("trucks", [""])[0][:30],
                        price=query.get("price", [""])[0][:30],
                        length=query.get("length", [""])[0][:30],
                        period=query.get("period", [""])[0][:30],
                        day=query.get("day", [""])[0][:30],
                    )
                    data = path.read_bytes()
                stamp = datetime.now().strftime("%Y%m%d-%H%M")
                self._send(
                    200,
                    data,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    f"X001-货源明细-{route or '全部线路'}-{stamp}.xlsx",
                )
            except (ValueError, FileNotFoundError, sqlite3.Error) as error:
                self._json(400, {"error": str(error)})
            return
        pages = {
            "/": "index.html",
            "/records": "records.html",
            "/console": "console.html",
        }
        name = pages.get(parsed.path, parsed.path.removeprefix("/"))
        if name not in {
            "index.html",
            "records.html",
            "console.html",
            "app.css",
            "light.css",
            "app.js",
        }:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        types = {
            "index.html": "text/html; charset=utf-8",
            "records.html": "text/html; charset=utf-8",
            "console.html": "text/html; charset=utf-8",
            "app.css": "text/css; charset=utf-8",
            "light.css": "text/css; charset=utf-8",
            "app.js": "text/javascript; charset=utf-8",
        }
        self._send(200, (STATIC / name).read_bytes(), types[name])

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if self.path not in {
            "/api/start",
            "/api/stop",
            "/api/clear-images",
            "/api/shutdown",
        }:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._json(415, {"error": "请使用 JSON 请求"})
            return
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            self._json(403, {"error": "请求来源无效"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 4096:
                raise ValueError("请求长度无效或过大")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("请求内容必须是 JSON 对象")
            if self.path == "/api/start":
                maximum = payload.get("maximum")
                self.server.console.start(
                    payload.get("mode", "collect"),
                    int(maximum) if maximum not in (None, "") else None,
                    int(payload.get("capacity", 3)),
                    payload.get("ocr", "auto"),
                    payload.get("route", ""),
                )
                self._json(200, {"ok": True})
            elif self.path == "/api/stop":
                self.server.console.stop()
                self._json(200, {"ok": True})
            elif self.path == "/api/shutdown":
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json(200, self.server.console.clear_images())
        except (ValueError, TypeError, json.JSONDecodeError, OSError) as error:
            self._json(400, {"error": str(error)})

    def log_message(self, format: str, *args: object) -> None:
        pass


class WebServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], console: Console, token: str | None):
        super().__init__(address, Handler)
        self.console = console
        self.token = token


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 X001 本地网页操作台")
    parser.add_argument(
        "--host", default="127.0.0.1", help="监听地址；局域网访问可填 0.0.0.0"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args()
    token = (
        secrets.token_urlsafe(18)
        if args.host not in {"127.0.0.1", "localhost", "::1"}
        else None
    )
    server = WebServer((args.host, args.port), Console(args.database), token)
    print(f"X001 操作台: http://{args.host}:{args.port}", flush=True)
    if token:
        print(f"局域网访问：用户名 x001，密码 {token}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
