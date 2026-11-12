import subprocess
import unittest
from unittest.mock import Mock, patch

from x001.adb import ADBController


class ADBControllerTests(unittest.TestCase):
    @patch("x001.adb.subprocess.run")
    def test_tap_includes_selected_device(self, run: Mock) -> None:
        controller = ADBController(serial="device-1")

        controller.tap(12, 34)

        run.assert_called_once_with(
            ["adb", "-s", "device-1", "shell", "input", "tap", "12", "34"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    @patch("x001.adb.subprocess.run")
    def test_back_sends_android_back_key(self, run: Mock) -> None:
        ADBController().back()

        run.assert_called_once_with(
            ["adb", "shell", "input", "keyevent", "KEYCODE_BACK"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    @patch("x001.adb.subprocess.run")
    def test_swipe_uses_duration(self, run: Mock) -> None:
        ADBController(adb_path="C:\\Android\\adb.exe").swipe(
            1, 2, 30, 40, duration_ms=500
        )

        run.assert_called_once_with(
            [
                "C:\\Android\\adb.exe",
                "shell",
                "input",
                "swipe",
                "1",
                "2",
                "30",
                "40",
                "500",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_rejects_negative_coordinates(self) -> None:
        with self.assertRaises(ValueError):
            ADBController().tap(-1, 0)

    def test_propagates_adb_failures(self) -> None:
        error = subprocess.CalledProcessError(1, ["adb"])
        with patch("x001.adb.subprocess.run", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError):
                ADBController().back()


if __name__ == "__main__":
    unittest.main()
