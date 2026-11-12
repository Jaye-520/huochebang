import os
import subprocess
from pathlib import Path
from typing import Any

_INPUT_BACKENDS = frozenset({"adb", "uiautomator"})

# 货车帮司机（com.wlqq）专用配置：设备、输入通道、启动组件均可按需覆盖。
_TARGET_SERIAL = "7450df55"
_TARGET_PACKAGE = "com.wlqq"
_TARGET_ACTIVITY = f"{_TARGET_PACKAGE}/.activity.HomeActivity"
_DEFAULT_INPUT_BACKEND = "uiautomator"

_DEFAULT_ADB_PATH = (
    Path(__file__).parent / "platform-tools" / "adb.exe" if os.name == "nt" else "adb"
)


class ADBController:
    def __init__(
        self,
        serial: str | None = _TARGET_SERIAL,
        adb_path: str | Path = _DEFAULT_ADB_PATH,
        timeout: float = 10,
        input_backend: str = _DEFAULT_INPUT_BACKEND,
    ) -> None:
        if serial == "":
            raise ValueError("serial must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if input_backend not in _INPUT_BACKENDS:
            raise ValueError(
                f"input_backend must be one of: {', '.join(sorted(_INPUT_BACKENDS))}"
            )

        self.serial = serial
        self.adb_path = str(adb_path)
        self.timeout = timeout
        self.input_backend = input_backend
        self._uiautomator_device: Any | None = None

    def tap(self, x: int, y: int) -> None:
        self._validate_coordinates(x, y)
        if self.input_backend == "uiautomator":
            self._uiautomator().click(x, y)
            return
        self._run("shell", "input", "tap", str(x), str(y))

    def back(self) -> None:
        if self.input_backend == "uiautomator":
            self._uiautomator().press("back")
            return
        self._run("shell", "input", "keyevent", "KEYCODE_BACK")

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 300,
    ) -> None:
        self._validate_coordinates(start_x, start_y)
        self._validate_coordinates(end_x, end_y)
        if duration_ms <= 0:
            raise ValueError("duration_ms must be greater than zero")

        if self.input_backend == "uiautomator":
            self._uiautomator().swipe(
                start_x,
                start_y,
                end_x,
                end_y,
                duration=duration_ms / 1000,
            )
            return

        self._run(
            "shell",
            "input",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
        )

    def launch(self) -> None:
        self._run("shell", "am", "start", "-n", _TARGET_ACTIVITY)

    def _uiautomator(self) -> Any:
        if self._uiautomator_device is None:
            os.environ.setdefault("ADBUTILS_ADB_PATH", self.adb_path)
            import uiautomator2

            self._uiautomator_device = uiautomator2.connect(self.serial)
        return self._uiautomator_device

    def _run(self, *args: str) -> None:
        command = [self.adb_path]
        if self.serial is not None:
            command.extend(("-s", self.serial))
        command.extend(args)
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )

    @staticmethod
    def _validate_coordinates(x: int, y: int) -> None:
        if x < 0 or y < 0:
            raise ValueError("screen coordinates must not be negative")
