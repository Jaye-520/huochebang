import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from x001 import adb
from x001.adb import ADBController


def _expected_default_adb_path() -> str:
    if os.name == "nt":
        return str(Path(adb.__file__).parent / "platform-tools" / "adb.exe")
    return "adb"


class ADBControllerTests(unittest.TestCase):
    def test_defaults_are_pinned_to_huochebang_driver(self) -> None:
        controller = ADBController()

        self.assertEqual(controller.serial, "7450df55")
        self.assertEqual(controller.input_backend, "uiautomator")

    @patch("x001.adb.subprocess.run")
    def test_tap_includes_selected_device(self, run: Mock) -> None:
        controller = ADBController(serial="device-1", input_backend="adb")

        controller.tap(12, 34)

        run.assert_called_once_with(
            [
                _expected_default_adb_path(),
                "-s",
                "device-1",
                "shell",
                "input",
                "tap",
                "12",
                "34",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    @patch("x001.adb.subprocess.run")
    def test_back_sends_android_back_key(self, run: Mock) -> None:
        ADBController(serial=None, input_backend="adb").back()

        run.assert_called_once_with(
            [
                _expected_default_adb_path(),
                "shell",
                "input",
                "keyevent",
                "KEYCODE_BACK",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    @patch("x001.adb.subprocess.run")
    def test_swipe_uses_duration(self, run: Mock) -> None:
        ADBController(
            adb_path="C:\\Android\\adb.exe", serial=None, input_backend="adb"
        ).swipe(1, 2, 30, 40, duration_ms=500)

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

    @patch("x001.adb.subprocess.run")
    def test_launch_starts_huochebang_driver(self, run: Mock) -> None:
        ADBController(serial=None).launch()

        run.assert_called_once_with(
            [
                _expected_default_adb_path(),
                "shell",
                "am",
                "start",
                "-n",
                "com.wlqq/.activity.HomeActivity",
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
                ADBController(serial=None, input_backend="adb").back()


class UIAutomatorBackendTests(unittest.TestCase):
    @patch("uiautomator2.connect")
    @patch("x001.adb.subprocess.run")
    def test_tap_uses_uiautomator_instead_of_adb(
        self, run: Mock, connect: Mock
    ) -> None:
        controller = ADBController(
            serial="device-1",
            input_backend="uiautomator",
        )

        controller.tap(12, 34)

        connect.assert_called_once_with("device-1")
        connect.return_value.click.assert_called_once_with(12, 34)
        run.assert_not_called()

    @patch("uiautomator2.connect")
    def test_reuses_uiautomator_device_for_swipe_and_back(self, connect: Mock) -> None:
        controller = ADBController()

        controller.swipe(1, 2, 30, 40, duration_ms=500)
        controller.back()

        connect.assert_called_once_with("7450df55")
        connect.return_value.swipe.assert_called_once_with(1, 2, 30, 40, duration=0.5)
        connect.return_value.press.assert_called_once_with("back")

    def test_rejects_unknown_input_backend(self) -> None:
        with self.assertRaises(ValueError):
            ADBController(input_backend="unknown")


if __name__ == "__main__":
    unittest.main()
