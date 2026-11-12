"""封装 Android 截图、点击和滚动手势。"""

import math
import os
import random
import subprocess
import time
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_INPUT_BACKENDS = frozenset({"adb", "uiautomator"})

# 默认连接用于验证的设备，启动货车帮司机首页。
_TARGET_SERIAL = "7450df55"
_TARGET_PACKAGE = "com.wlqq"
_TARGET_ACTIVITY = f"{_TARGET_PACKAGE}/.activity.HomeActivity"
_DEFAULT_INPUT_BACKEND = "uiautomator"
_DEFAULT_SCREEN_SIZE = (1080, 2340)

DEFAULT_ADB_PATH = (
    Path(__file__).parent / "platform-tools" / "adb.exe" if os.name == "nt" else "adb"
)


@dataclass(frozen=True)
class _Gesture:
    """一次手指不离开屏幕的轨迹；(x, y, t) 中 t 是落指后的秒数。"""

    points: list[tuple[int, int, float]]

    @property
    def duration(self) -> float:
        return self.points[-1][2]

    def batched_points(self) -> tuple[list[tuple[int, int]], float]:
        """按等时间间隔重采样，适配 UiAutomator 的分段手势接口。"""
        segment_count = max(2, round(self.duration / 0.025))
        # UiAutomator 按 5 毫秒为单位计算每段轨迹的步数。
        steps = max(2, round(self.duration / (segment_count * 0.005)))
        times = [point[2] for point in self.points]
        coordinates = [self.points[0][:2]]
        for index in range(1, segment_count):
            at = self.duration * index / segment_count
            right = bisect_left(times, at)
            x0, y0, t0 = self.points[right - 1]
            x1, y1, t1 = self.points[right]
            fraction = (at - t0) / (t1 - t0)
            coordinates.append(
                (round(x0 + (x1 - x0) * fraction), round(y0 + (y1 - y0) * fraction))
            )
        coordinates.append(self.points[-1][:2])
        return coordinates, (steps + 0.01) * 0.005


@dataclass(frozen=True)
class _SwipePlan:
    """一整套拟人滑动：落指前迟疑、若干段手势、每段抬指后的停顿。"""

    gestures: list[_Gesture]
    pauses: list[float]
    pre_delay: float


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _hermite(u: float, v0: float, v1: float) -> float:
    """起点速度 v0、终点速度 v1 的三次进度曲线，u ∈ [0, 1]。

    v1 大于 1 表示抬指前仍在加速（甩动，靠惯性继续滚动），小于 1
    表示减速跟读。真人两种都会出现，且不会是匀速直线。
    """
    u2, u3 = u * u, u * u * u
    return (u3 - 2 * u2 + u) * v0 + (-2 * u3 + 3 * u2) + (u3 - u2) * v1


def _build_swipe_plan(
    start: tuple[int, int],
    end: tuple[int, int],
    duration_ms: int,
    rng: random.Random,
    screen_size: tuple[int, int],
) -> _SwipePlan:
    """把一次直线滑动翻译成带随机性的真人手势轨迹。

    随机维度包括：手势模式、整体快慢、落指/抬指位置、起终速度、
    横向弧线与低频抖动、采样间隔、中途停顿、回拉修正、二次补滑。
    起终点只在小幅范围内扰动，净位移仍接近调用方请求的幅度，
    这样 collect.py 依赖的「留一张卡片做锚点」不会被破坏。
    """
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    length = max(1.0, math.hypot(dx, dy))
    ux, uy = dx / length, dy / length
    normal_x, normal_y = -uy, ux
    width, height = screen_size

    mode = rng.choices(
        ("fling", "read", "hesitate", "two_stroke"),
        weights=(40, 26, 18, 16),
    )[0]
    pace = {
        "fling": (0.66, 0.96),
        "read": (1.06, 1.5),
        "hesitate": (0.85, 1.3),
        "two_stroke": (0.9, 1.35),
    }[mode]
    seconds = max(0.14, duration_ms / 1000 * rng.uniform(*pace))

    # 落指与抬指都不是模板坐标，但也别偏到会改变滚动幅度。
    start_axis = rng.uniform(-0.012, 0.012) * length
    start_normal = rng.gauss(0, rng.uniform(7, 22))
    end_axis = rng.uniform(-0.024, 0.024) * length
    end_normal = rng.gauss(0, rng.uniform(5, 16))
    ax = sx + ux * start_axis + normal_x * start_normal
    ay = sy + uy * start_axis + normal_y * start_normal
    bx = sx + dx + ux * end_axis + normal_x * end_normal
    by = sy + dy + uy * end_axis + normal_y * end_normal

    # 手指不会走直线：一条随机弧线 + 低频摆动 + 每帧高斯抖动。
    bend = rng.uniform(-1, 1) * rng.uniform(6, 26)
    wobble_amp = rng.uniform(1.2, 4.2)
    wobble_freq = rng.uniform(1.5, 3.5) * math.pi
    wobble_phase = rng.uniform(0, 2 * math.pi)
    jitter_sigma = rng.uniform(0.5, 2.0)

    def position(progress: float, jitter: bool) -> tuple[int, int]:
        u = _clamp(progress, 0.0, 1.0)
        arc = math.sin(math.pi * u)
        lateral = (
            bend * arc + wobble_amp * math.sin(wobble_freq * u + wobble_phase) * arc
        )
        if jitter:
            lateral += rng.gauss(0, jitter_sigma)
        x = ax + (bx - ax) * progress + normal_x * lateral
        y = ay + (by - ay) * progress + normal_y * lateral
        return (
            int(round(_clamp(x, 1, width - 2))),
            int(round(_clamp(y, 1, height - 2))),
        )

    def stroke(
        p_from: float, p_to: float, seconds: float, hold_at_end: bool
    ) -> _Gesture:
        count = int(_clamp(round(seconds / rng.uniform(0.011, 0.022)), 12, 30))
        # (时间间隔, 是否为手指停住的采样)。停住时只推进挂钟时间，不推进手指进度。
        segments: list[tuple[float, bool]] = [
            (rng.uniform(0.6, 1.4), False) for _ in range(count - 1)
        ]
        if mode in ("read", "hesitate") and rng.random() < 0.75:
            index = rng.randint(int(count * 0.3), int(count * 0.7))
            hold_each = rng.uniform(0.02, 0.06)
            segments[index:index] = [
                (hold_each, True) for _ in range(rng.randint(1, 3))
            ]
        if hold_at_end and rng.random() < 0.6:
            segments.extend(
                (rng.uniform(0.015, 0.04), True) for _ in range(rng.randint(1, 2))
            )

        active_total = sum(dt for dt, held in segments if not held)
        scale = seconds / sum(dt for dt, _ in segments)
        v0 = rng.uniform(0.03, 0.35)
        if mode == "fling":
            v1 = rng.uniform(0.9, 1.6)  # 抬指时仍有速度，保留列表惯性
        elif mode == "read":
            v1 = rng.uniform(0.35, 0.8)
        else:
            v1 = rng.uniform(0.55, 1.15)
        back_start = rng.uniform(0.28, 0.5)
        back_width = rng.uniform(0.1, 0.22)
        back_amp = rng.uniform(0.015, 0.05) if mode == "hesitate" else 0.0

        points: list[tuple[int, int, float]] = [(*position(p_from, False), 0.0)]
        wall = active = 0.0
        for index, (dt, held) in enumerate(segments):
            wall += dt * scale
            if not held:
                active += dt
            base = _clamp(_hermite(active / active_total, v0, v1), 0.0, 1.0)
            progress = p_from + (p_to - p_from) * base
            if back_amp and back_start <= base <= back_start + back_width:
                back = math.sin(math.pi * (base - back_start) / back_width)
                progress -= (p_to - p_from) * back_amp * back
            is_last = index == len(segments) - 1
            points.append((*position(progress, not is_last), wall))
        x, y, _ = points[-1]
        points[-1] = (x, y, seconds)
        return _Gesture(points)

    pre_delay = rng.uniform(0.02, 0.16)
    if mode == "two_stroke":
        split = rng.uniform(0.72, 0.88)
        first = seconds * rng.uniform(0.72, 0.86)
        gestures = [
            stroke(0.0, split, first, False),
            stroke(split, 1.0, seconds - first, False),
        ]
        pauses = [rng.uniform(0.1, 0.28), rng.uniform(0.05, 0.2)]
    else:
        gestures = [stroke(0.0, 1.0, seconds, mode == "read")]
        pauses = [rng.uniform(0.04, 0.2)]
    return _SwipePlan(gestures, pauses, pre_delay)


def _inject_touch(touch: Any, points: list[tuple[int, int, float]]) -> None:
    """按绝对时间轴逐点注入，尽量还原真实触摸采样间隔。"""
    x, y, _ = points[0]
    touch.down(x, y)
    started = time.monotonic()
    try:
        for x, y, at in points[1:]:
            remaining = at - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
            touch.move(x, y)
    finally:
        # 中途 RPC 失败也必须抬指，否则手指会一直按在屏幕上。
        x, y, _ = points[-1]
        touch.up(x, y)


class ADBController:
    """通过 ADB 截图，并向选定设备发送 UI 操作。"""

    def __init__(
        self,
        serial: str | None = _TARGET_SERIAL,
        adb_path: str | Path = DEFAULT_ADB_PATH,
        timeout: float = 10,
        input_backend: str = _DEFAULT_INPUT_BACKEND,
        screen_size: tuple[int, int] = _DEFAULT_SCREEN_SIZE,
        humanize: bool = True,
        seed: int | None = None,
    ) -> None:
        if serial == "":
            raise ValueError("serial must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if input_backend not in _INPUT_BACKENDS:
            raise ValueError(
                f"input_backend must be one of: {', '.join(sorted(_INPUT_BACKENDS))}"
            )
        if len(screen_size) != 2 or min(screen_size) < 8:
            raise ValueError("screen_size must be a positive (width, height) pair")

        self.serial = serial
        self.adb_path = str(adb_path)
        self.timeout = timeout
        self.input_backend = input_backend
        self.screen_size = tuple(screen_size)
        self.humanize = humanize
        self._rng = random.Random(seed)
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
        *,
        humanize: bool | None = None,
    ) -> None:
        """执行滑动。曲线模式以 duration_ms 为节奏基准。"""
        self._validate_coordinates(start_x, start_y)
        self._validate_coordinates(end_x, end_y)
        if duration_ms <= 0:
            raise ValueError("duration_ms must be greater than zero")
        use_human = self.humanize if humanize is None else humanize
        if not use_human:
            self._linear_swipe(start_x, start_y, end_x, end_y, duration_ms)
            return

        plan = _build_swipe_plan(
            (start_x, start_y),
            (end_x, end_y),
            duration_ms,
            self._rng,
            self.screen_size,
        )
        if self.input_backend == "uiautomator":
            self._play_uiautomator(plan)
        else:
            self._play_adb(plan)

    def scroll(
        self,
        direction: Literal["up", "down"] = "up",
        *,
        distance: int | None = None,
        duration_ms: int = 480,
        region: tuple[int, int, int, int] | None = None,
        humanize: bool | None = None,
    ) -> None:
        """在像素区域内滚动。up 表示手指上滑，显示下方内容。"""
        if direction not in ("up", "down"):
            raise ValueError("direction must be 'up' or 'down'")
        width, height = self.screen_size
        left, top, right, bottom = region or (
            0,
            int(height * 0.31),
            width - 1,
            int(height * 0.83),
        )
        if not (0 <= left <= right < width and 0 <= top < bottom < height):
            raise ValueError("scroll region must be within the display")
        span = bottom - top if distance is None else min(int(distance), bottom - top)
        if span <= 0:
            raise ValueError("distance must be greater than zero")
        x = (left + right + 1) // 2
        if direction == "up":
            start_y, end_y = bottom, bottom - span
        else:
            start_y, end_y = top, top + span
        self.swipe(
            x,
            start_y,
            x,
            end_y,
            duration_ms=duration_ms,
            humanize=humanize,
        )

    def launch(self) -> None:
        self._run("shell", "am", "start", "-n", _TARGET_ACTIVITY)

    def screenshot(self) -> bytes:
        """返回当前屏幕的 PNG 字节。"""
        return subprocess.run(
            self._command("exec-out", "screencap", "-p"),
            check=True,
            capture_output=True,
            timeout=self.timeout,
        ).stdout

    def _linear_swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int,
    ) -> None:
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

    def _play_uiautomator(self, plan: _SwipePlan) -> None:
        device = self._uiautomator()
        swipe_points = getattr(device, "swipe_points", None)
        time.sleep(plan.pre_delay)
        for gesture, pause in zip(plan.gestures, plan.pauses, strict=True):
            if swipe_points is not None:
                coordinates, segment_duration = gesture.batched_points()
                swipe_points(coordinates, duration=segment_duration)
            else:
                _inject_touch(device.touch, gesture.points)
            time.sleep(pause)

    def _play_adb(self, plan: _SwipePlan) -> None:
        # `input swipe` 只能画直线，因此这里靠随机化的分段、停顿和
        # 起终点/时长抖动拟人；完整曲线需要 uiautomator 后端。
        time.sleep(plan.pre_delay)
        for gesture, pause in zip(plan.gestures, plan.pauses, strict=True):
            start_x, start_y, _ = gesture.points[0]
            end_x, end_y, _ = gesture.points[-1]
            self._run(
                "shell",
                "input",
                "swipe",
                str(start_x),
                str(start_y),
                str(end_x),
                str(end_y),
                str(max(16, round(gesture.duration * 1000))),
            )
            time.sleep(pause)

    def _uiautomator(self) -> Any:
        if self._uiautomator_device is None:
            os.environ.setdefault("ADBUTILS_ADB_PATH", self.adb_path)
            import uiautomator2

            self._uiautomator_device = uiautomator2.connect(self.serial)
        return self._uiautomator_device

    def _run(self, *args: str) -> None:
        subprocess.run(
            self._command(*args),
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )

    def _command(self, *args: str) -> list[str]:
        command = [self.adb_path]
        if self.serial is not None:
            command.extend(("-s", self.serial))
        command.extend(args)
        return command

    @staticmethod
    def _validate_coordinates(x: int, y: int) -> None:
        if x < 0 or y < 0:
            raise ValueError("screen coordinates must not be negative")
