import queue
import sys
import threading
import unittest
from pathlib import Path

from x001.launcher import Launcher


class LauncherTests(unittest.TestCase):
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
        self.assertIn("本地数据目录已初始化", logs)


if __name__ == "__main__":
    unittest.main()
