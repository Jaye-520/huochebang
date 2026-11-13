import http.client
import json
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from x001.adb import DEFAULT_ADB_PATH
from x001.storage import Store
from x001.web import Console, Handler, WebServer, local_timestamp


class WebConsoleTests(unittest.TestCase):
    @patch("x001.web.inspect_device")
    def test_device_probe_uses_the_shared_platform_adb_path(self, inspect):
        inspect.return_value = {
            "adb_available": True,
            "connected": True,
            "devices": [{"serial": "7450df55", "state": "device"}],
            "target_serial": "7450df55",
            "target_state": "device",
            "screen_awake": True,
            "unlocked": True,
            "package_installed": True,
            "foreground": True,
        }
        server = WebServer(("127.0.0.1", 0), Console(Path("test.sqlite3")), None)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                connection.request("GET", "/api/device")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    json.loads(response.read()),
                    {
                        **inspect.return_value,
                        "devices": ["7450df55"],
                        "device_states": [{"serial": "7450df55", "state": "device"}],
                    },
                )
            finally:
                connection.close()
            inspect.assert_called_once_with(adb_path=DEFAULT_ADB_PATH)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_naive_sqlite_timestamps_are_displayed_as_utc(self):
        stored = "2026-11-12 05:00:00"
        expected = datetime.fromisoformat(stored).replace(tzinfo=timezone.utc)

        self.assertEqual(
            local_timestamp(stored),
            expected.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def test_post_rejects_non_object_json_and_negative_content_length(self):
        server = WebServer(("127.0.0.1", 0), Console(Path("test.sqlite3")), None)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            address = server.server_address
            for body, headers in (
                (b"[]", {"Content-Type": "application/json"}),
                (
                    b"{}",
                    {
                        "Content-Type": "application/json",
                        "Content-Length": "-1",
                    },
                ),
            ):
                connection = http.client.HTTPConnection(*address, timeout=2)
                try:
                    connection.request("POST", "/api/start", body=body, headers=headers)
                    self.assertEqual(connection.getresponse().status, 400)
                finally:
                    connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_shutdown_endpoint_stops_server_cleanly(self):
        server = WebServer(("127.0.0.1", 0), Console(Path("test.sqlite3")), None)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                connection.request(
                    "POST",
                    "/api/shutdown",
                    body=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
            finally:
                connection.close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        finally:
            if thread.is_alive():
                server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_health_reports_service_and_database_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "test.sqlite3"
            store = Store(database)
            store.initialize()
            store.db.close()
            server = WebServer(("127.0.0.1", 0), Console(database), None)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    *server.server_address, timeout=2
                )
                try:
                    connection.request("GET", "/api/health")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        json.loads(response.read()),
                        {
                            "ok": True,
                            "service": "x001",
                            "database_ready": True,
                            "collector_running": False,
                        },
                    )
                finally:
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_close_waits_for_collector_and_escalates_when_needed(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("collect", 0), 0]
        console = Console(Path("test.sqlite3"))
        console.process = process

        console.close(timeout=0.01)

        process.terminate.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_old_collector_output_does_not_update_the_new_task(self):
        class Output:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter(["旧任务输出\n"])

        old = Mock()
        old.stdout = Output()
        old.wait.return_value = 9
        console = Console(Path("test.sqlite3"))
        console.process = Mock()

        console._read_output(old)

        self.assertEqual(list(console.logs), [])
        self.assertIsNone(console.exit_code)

    def test_shutdown_endpoint_stops_a_running_collector(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        console = Console(Path("test.sqlite3"))
        console.process = process
        server = WebServer(("127.0.0.1", 0), console, None)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                connection.request(
                    "POST",
                    "/api/shutdown",
                    body=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
            finally:
                connection.close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            process.wait.assert_called_once()
        finally:
            if thread.is_alive():
                server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_vehicle_truck_count_and_price_filters_use_all_matching_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "test.sqlite3"
            store = Store(database)
            store.initialize()
            selected = store.begin("selected", None, "宁夏天津")
            store.complete(
                selected,
                [],
                {
                    "vehicle": "13/13.7米 高栏/平板",
                    "truck_count": "还需2辆",
                    "price": "260元/吨",
                },
                0.1,
            )
            other = store.begin("other", None, "宁夏天津")
            store.complete(
                other,
                [],
                {"vehicle": "17.5米 平板", "truck_count": "还需7辆", "price": "电议"},
                0.1,
            )
            missing = store.begin("missing", None, "宁夏河北")
            store.complete(missing, [], {"vehicle": "13米 高栏"}, 0.1)
            store.db.close()

            console = Console(database)
            result = console.snapshot(
                vehicle="高栏", trucks="1-2", price="priced", length="13.7"
            )
            self.assertEqual(result["matching_total"], 1)
            self.assertEqual(
                [row["id"] for row in console.snapshot(length="13.7")["records"]],
                [selected],
            )
            self.assertEqual(
                [row["id"] for row in console.snapshot(length="17.5")["records"]],
                [other],
            )
            self.assertEqual([row["id"] for row in result["records"]], [selected])
            self.assertEqual(
                [row["id"] for row in console.snapshot(trucks="6+")["records"]],
                [other],
            )
            self.assertEqual(
                [row["id"] for row in console.snapshot(trucks="unknown")["records"]],
                [missing],
            )

    def test_snapshot_reads_new_database_rows_and_filters_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "test.sqlite3"
            store = Store(database)
            store.initialize()
            console = Console(database)
            self.assertEqual(console.snapshot()["stats"]["total"], 0)

            selected = store.begin("煤炭卡片", "260元/吨", "宁夏天津")
            store.complete(
                selected,
                [],
                {"cargo": "煤炭", "price": "260元/吨"},
                0.1,
            )
            store.begin("钢材卡片", None, "宁夏河北")
            store.db.close()

            snapshot = console.snapshot("宁夏天津", "煤炭")
            self.assertEqual(snapshot["stats"]["total"], 2)
            self.assertEqual(snapshot["stats"]["done"], 1)
            self.assertEqual(snapshot["stats"]["pending"], 1)
            self.assertEqual([row["id"] for row in snapshot["records"]], [selected])
            self.assertEqual(snapshot["records"][0]["price"], "260元/吨")
            self.assertEqual(snapshot["analysis"]["quality"]["total"], 1)

    def test_rejects_invalid_start_without_running_collector(self):
        with tempfile.TemporaryDirectory() as temporary:
            console = Console(Path(temporary) / "test.sqlite3")
            with self.assertRaisesRegex(ValueError, "无效的运行模式"):
                console.start("unknown", None, 3, "auto")
            self.assertFalse(console.process_state()["running"])

    def test_requires_expected_route_for_web_collection(self):
        with tempfile.TemporaryDirectory() as temporary:
            console = Console(Path(temporary) / "test.sqlite3")
            with self.assertRaisesRegex(ValueError, "请先选择手机上当前打开的线路"):
                console.start("collect", 5, 3, "auto")
            self.assertFalse(console.process_state()["running"])

    def test_stop_falls_back_when_windows_console_signal_is_unavailable(self):
        if os.name != "nt":
            self.skipTest("Windows console signals are platform-specific")
        process = Mock()
        process.poll.return_value = None
        process.send_signal.side_effect = OSError("no console attached")
        console = Console(Path("test.sqlite3"))
        console.process = process

        console.stop()

        process.terminate.assert_called_once_with()

    @patch("x001.web.threading.Thread")
    @patch("x001.web.subprocess.Popen")
    def test_collector_process_uses_utf8_for_captured_output(self, popen, thread):
        process = Mock()
        process.poll.return_value = None
        popen.return_value = process
        console = Console(Path("test.sqlite3"))

        console.start("process", None, 3, "auto")

        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        thread.assert_called_once()

    @patch("x001.web.BaseHTTPRequestHandler.handle")
    def test_handler_suppresses_expected_client_disconnects(self, handle):
        handler = object.__new__(Handler)
        for error in (
            BrokenPipeError(),
            ConnectionAbortedError(),
            ConnectionResetError(),
        ):
            with self.subTest(error=type(error).__name__):
                handle.side_effect = error
                handler.handle()
        handle.side_effect = OSError("unexpected socket error")
        with self.assertRaisesRegex(OSError, "unexpected socket error"):
            handler.handle()

    def test_clear_images_preserves_records_and_restarts_file_numbering(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "test.sqlite3"
            store = Store(database)
            store.initialize()
            old_id = store.begin("old", None, "宁夏天津")
            old_folder = store.folder(old_id)
            old_folder.mkdir(parents=True)
            image = old_folder / "detail-01.png"
            image.write_bytes(b"test image")
            store.publish(old_id, [image])
            store.complete(old_id, [], {"cargo": "塑料颗粒"}, 0.1)
            failed_id = store.begin("failed", None, "宁夏河北")
            failed_folder = store.folder(failed_id)
            failed_folder.mkdir(parents=True)
            failed_image = failed_folder / "detail-01.png"
            failed_image.write_bytes(b"failed image")
            store.publish(failed_id, [failed_image])
            store.ocr_failed(failed_id, "OCR error")
            store.db.close()

            console = Console(database)
            self.assertEqual(console.image(old_id, 0), b"test image")
            self.assertEqual(console.clear_images()["removed"], 2)
            self.assertFalse(image.exists())
            self.assertEqual(console.snapshot()["stats"]["done"], 1)
            self.assertEqual(console.snapshot()["records"][0]["images"], [])

            store = Store(database)
            store.initialize()
            store.recover()
            self.assertEqual(
                store.db.execute(
                    "SELECT status FROM captures WHERE id=?", (failed_id,)
                ).fetchone()[0],
                "ocr_failed",
            )
            new_id = store.begin("new", None, "宁夏河北")
            self.assertGreater(new_id, old_id)
            self.assertEqual(store.folder(new_id).name, "1")
            store.db.close()


if __name__ == "__main__":
    unittest.main()
