import json
import queue
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from x001.launcher import CONSOLE_URL, Launcher, healthy_console, read_health


class Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class LauncherTests(unittest.TestCase):
    def test_prepare_runtime_falls_back_to_official_source_when_mirror_fails(self):
        launcher = Launcher.__new__(Launcher)
        launcher.project = Path.cwd()
        launcher.events = queue.Queue()
        launcher.cancel_requested = threading.Event()
        uv = Path("uv.exe")

        with (
            patch.object(
                launcher,
                "_install_runtime_from_mirror",
                side_effect=RuntimeError("mirror unavailable"),
            ),
            patch.object(launcher, "_run_command") as run_command,
        ):
            launcher._prepare_runtime(uv)

        run_command.assert_called_once_with(
            [
                str(uv),
                "sync",
                "--frozen",
                "--no-dev",
                "--python",
                "3.14",
                "--no-progress",
            ],
            "官方源运行环境准备",
        )

    def test_prepare_runtime_does_not_fall_back_after_cancellation(self):
        launcher = Launcher.__new__(Launcher)
        launcher.project = Path.cwd()
        launcher.events = queue.Queue()
        launcher.cancel_requested = threading.Event()
        launcher.cancel_requested.set()

        with (
            patch.object(
                launcher,
                "_install_runtime_from_mirror",
                side_effect=RuntimeError("startup cancelled"),
            ),
            patch.object(launcher, "_run_command") as run_command,
        ):
            with self.assertRaisesRegex(RuntimeError, "startup cancelled"):
                launcher._prepare_runtime(Path("uv.exe"))

        run_command.assert_not_called()

    def test_run_command_decodes_utf8_child_output(self):
        launcher = Launcher.__new__(Launcher)
        launcher.events = queue.Queue()
        launcher.project = Path.cwd()
        launcher.cancel_requested = threading.Event()
        launcher.command_process = None

        launcher._run_command(
            [sys.executable, "-c", "print('本地数据目录已初始化')"],
            "测试 UTF-8 输出",
        )

        logs = []
        while not launcher.events.empty():
            event, value = launcher.events.get_nowait()
            if event == "log":
                logs.append(value)
        self.assertIn("组件输出：本地数据目录已初始化", logs)

    @patch("x001.launcher.urllib.request.urlopen")
    def test_health_requires_service_identity_and_database(self, urlopen):
        self.assertEqual(CONSOLE_URL, "http://127.0.0.1:8765")
        urlopen.return_value = Response(
            {"ok": True, "service": "x001", "database_ready": True}
        )
        self.assertTrue(healthy_console())

        urlopen.return_value = Response(
            {"ok": True, "service": "other", "database_ready": True}
        )
        self.assertFalse(healthy_console())

        urlopen.return_value = Response(
            {"ok": True, "service": "x001", "database_ready": False}
        )
        self.assertFalse(healthy_console())

    @patch("x001.launcher.urllib.request.urlopen")
    def test_read_health_treats_network_failure_as_unavailable(self, urlopen):
        urlopen.side_effect = OSError("connection refused")
        self.assertIsNone(read_health())

    @patch("x001.launcher.urllib.request.urlopen")
    def test_server_cleanup_terminates_a_stuck_process(self, urlopen):
        urlopen.return_value = Response({"ok": True})
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("x001.web", 0),
            0,
        ]
        launcher = Launcher.__new__(Launcher)
        launcher.server = process
        launcher.server_owned = True
        launcher.server_url = CONSOLE_URL

        launcher._shutdown_server()

        process.terminate.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
