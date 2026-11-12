import os
import subprocess
from pathlib import Path


_DEFAULT_ADB_PATH = (
    Path(__file__).parent / "platform-tools" / "adb.exe"
    if os.name == "nt"
    else "adb"
)


class ADBController:
    def __init__(
        self,
        serial: str | None = None,
        adb_path: str | Path = _DEFAULT_ADB_PATH,
        timeout: float = 10,
    ) -> None:
        if serial == "":
            raise ValueError("serial must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

        self.serial = serial
        self.adb_path = str(adb_path)
        self.timeout = timeout

    def tap(self, x: int, y: int) -> None:
        self._validate_coordinates(x, y)
        self._run("shell", "input", "tap", str(x), str(y))

    def back(self) -> None:
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
