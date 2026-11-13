import os
import random
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from x001 import adb
from x001.adb import ADBController, inspect_device, list_devices

_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _expected_default_adb_path() -> str:
    if os.name == "nt":
        return str(Path(adb.__file__).parent / "platform-tools" / "adb.exe")
    return "adb"


class ADBControllerTests(unittest.TestCase):
    @patch("x001.adb.subprocess.run")
    def test_screenshot_keeps_png_bytes_and_targets_selected_device(self, run: Mock):
        png = b"\x89PNG\r\n\x1a\n"
        run.return_value.stdout = png
        self.assertEqual(ADBController(serial="demo", adb_path="adb").screenshot(), png)
        run.assert_called_once_with(
            ["adb", "-s", "demo", "exec-out", "screencap", "-p"],
            check=True,
            capture_output=True,
            timeout=10,
            creationflags=_CREATION_FLAGS,
        )

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
            creationflags=_CREATION_FLAGS,
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
            creationflags=_CREATION_FLAGS,
        )

    @patch("x001.adb.subprocess.run")
    def test_swipe_uses_duration(self, run: Mock) -> None:
        ADBController(
            adb_path="C:\\Android\\adb.exe", serial=None, input_backend="adb"
        ).swipe(1, 2, 30, 40, duration_ms=500, humanize=False)

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
            creationflags=_CREATION_FLAGS,
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
            creationflags=_CREATION_FLAGS,
        )

    def test_rejects_negative_coordinates(self) -> None:
        with self.assertRaises(ValueError):
            ADBController().tap(-1, 0)

    def test_propagates_adb_failures(self) -> None:
        error = subprocess.CalledProcessError(1, ["adb"])
        with patch("x001.adb.subprocess.run", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError):
                ADBController(serial=None, input_backend="adb").back()


class DeviceInspectionTests(unittest.TestCase):
    @staticmethod
    def _result(stdout: str, returncode: int = 0, stderr: str = "") -> Mock:
        result = Mock()
        result.stdout = stdout
        result.stderr = stderr
        result.returncode = returncode
        return result

    def test_list_devices_keeps_authorized_and_pending_states(self) -> None:
        with patch(
            "x001.adb.subprocess.run",
            return_value=self._result(
                "List of devices attached\n7450df55\tdevice\nother\tunauthorized\n"
            ),
        ) as run:
            self.assertEqual(
                list_devices("adb"),
                [
                    {"serial": "7450df55", "state": "device"},
                    {"serial": "other", "state": "unauthorized"},
                ],
            )
        self.assertEqual(run.call_args.args[0], ["adb", "devices"])

    def test_inspect_device_reports_target_lock_and_app_state(self) -> None:
        outputs = [
            self._result("List of devices attached\n7450df55\tdevice\n"),
            self._result("mWakefulness=Awake\n"),
            self._result(
                "mKeyguardShowing=false\n"
                "mCurrentFocus=Window{u0 com.wlqq/com.wlqq.activity.HomeActivity}\n"
            ),
            self._result("package:/data/app/com.wlqq/base.apk\n"),
        ]
        with patch("x001.adb.subprocess.run", side_effect=outputs) as run:
            report = inspect_device("7450df55", adb_path="adb")
        self.assertTrue(report["adb_available"])
        self.assertTrue(report["connected"])
        self.assertEqual(report["target_state"], "device")
        self.assertTrue(report["screen_awake"])
        self.assertTrue(report["unlocked"])
        self.assertTrue(report["package_installed"])
        self.assertTrue(report["foreground"])
        self.assertEqual(run.call_count, 4)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["adb", "-s", "7450df55", "shell", "dumpsys", "power"],
        )

    def test_inspect_device_distinguishes_unauthorized_from_missing(self) -> None:
        with patch(
            "x001.adb.subprocess.run",
            return_value=self._result(
                "List of devices attached\n7450df55\tunauthorized\n"
            ),
        ) as run:
            report = inspect_device("7450df55", adb_path="adb")
        self.assertFalse(report["connected"])
        self.assertEqual(report["target_state"], "unauthorized")
        self.assertEqual(run.call_count, 1)

        with patch(
            "x001.adb.subprocess.run",
            return_value=self._result("List of devices attached\n"),
        ):
            report = inspect_device("7450df55", adb_path="adb")
        self.assertEqual(report["target_state"], "missing")

    def test_inspect_device_reports_adb_command_failure(self) -> None:
        with patch(
            "x001.adb.subprocess.run",
            return_value=self._result(
                "", returncode=1, stderr="adb server is out of date"
            ),
        ):
            report = inspect_device("7450df55", adb_path="adb")
        self.assertFalse(report["adb_available"])
        self.assertIn("adb server is out of date", report["error"])


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

        controller.swipe(1, 2, 30, 40, duration_ms=500, humanize=False)
        controller.back()

        connect.assert_called_once_with("7450df55")
        connect.return_value.swipe.assert_called_once_with(1, 2, 30, 40, duration=0.5)
        connect.return_value.press.assert_called_once_with("back")

    def test_rejects_unknown_input_backend(self) -> None:
        with self.assertRaises(ValueError):
            ADBController(input_backend="unknown")


class HumanSwipePlanTests(unittest.TestCase):
    START = (270, 2000)
    END = (270, 700)

    @staticmethod
    def plan(seed: int):
        return adb._build_swipe_plan(
            HumanSwipePlanTests.START,
            HumanSwipePlanTests.END,
            530,
            random.Random(seed),
            (1080, 2340),
        )

    def test_trajectory_stays_on_screen_and_near_requested_swipe(self) -> None:
        for seed in range(40):
            plan = self.plan(seed)
            self.assertIn(len(plan.gestures), (1, 2))
            self.assertEqual(len(plan.gestures), len(plan.pauses))
            self.assertGreater(plan.pre_delay, 0)
            for gesture in plan.gestures:
                points = gesture.points
                self.assertTrue(12 <= len(points) <= 36)
                times = [at for _, _, at in points]
                self.assertEqual(times[0], 0.0)
                self.assertTrue(all(b >= a for a, b in zip(times, times[1:])))
                self.assertGreater(gesture.duration, 0)
                for x, y, _ in points:
                    self.assertTrue(1 <= x <= 1078)
                    self.assertTrue(1 <= y <= 2338)
            first_x, first_y, _ = plan.gestures[0].points[0]
            last_x, last_y, _ = plan.gestures[-1].points[-1]
            self.assertLess(abs(first_y - 2000), 60)
            self.assertLess(abs(last_y - 700), 60)
            self.assertLess(abs(first_x - 270), 100)
            self.assertLess(abs(last_x - 270), 100)

    def test_every_run_produces_a_different_gesture(self) -> None:
        signatures = set()
        durations = set()
        for seed in range(30):
            plan = self.plan(seed)
            signatures.add(
                tuple(
                    (x, y, round(at, 5))
                    for gesture in plan.gestures
                    for x, y, at in gesture.points
                )
            )
            durations.add(round(sum(g.duration for g in plan.gestures), 4))
        self.assertEqual(len(signatures), 30)
        self.assertGreater(len(durations), 20)

    def test_plan_contains_holds_and_two_stroke_gestures(self) -> None:
        holds = doubles = 0
        for seed in range(60):
            plan = self.plan(seed)
            doubles += len(plan.gestures) == 2
            for gesture in plan.gestures:
                points = gesture.points
                holds += any(
                    abs(a[0] - b[0]) <= 3 and abs(a[1] - b[1]) <= 3
                    for a, b in zip(points, points[1:])
                )
        self.assertGreater(doubles, 0)
        self.assertGreater(holds, 0)

    def test_rejects_invalid_screen_size(self) -> None:
        with self.assertRaises(ValueError):
            ADBController(screen_size=(0, 10))


class HumanSwipeInjectionTests(unittest.TestCase):
    def test_batch_resamples_by_time_and_preserves_hold(self):
        gesture = adb._Gesture([(270, 1800, 0.0), (270, 1800, 0.1), (320, 800, 0.2)])
        points, duration = gesture.batched_points()
        self.assertEqual(points[:5], [(270, 1800)] * 5)
        self.assertEqual(points[-1], (320, 800))
        self.assertAlmostEqual((len(points) - 1) * duration, 0.2, delta=0.01)

    @patch("x001.adb.time.sleep")
    @patch("uiautomator2.connect")
    def test_uiautomator_batches_each_curved_stroke(
        self, connect: Mock, _sleep: Mock
    ) -> None:
        ADBController(seed=7).swipe(270, 2000, 270, 700, duration_ms=530)

        device = connect.return_value
        plan = adb._build_swipe_plan(
            (270, 2000), (270, 700), 530, random.Random(7), (1080, 2340)
        )
        self.assertEqual(device.swipe_points.call_count, len(plan.gestures))
        device.touch.move.assert_not_called()
        device.swipe.assert_not_called()
        for call, stroke in zip(device.swipe_points.call_args_list, plan.gestures):
            points = call.args[0]
            self.assertEqual(
                (points[0], points[-1]), (stroke.points[0][:2], stroke.points[-1][:2])
            )
            self.assertGreater(len({x for x, _ in points}), 2)
            total = (len(points) - 1) * call.kwargs["duration"]
            self.assertAlmostEqual(total, stroke.duration, delta=0.06)

    @patch("x001.adb.time.sleep")
    @patch("uiautomator2.connect")
    def test_touch_is_released_when_a_move_fails(
        self, connect: Mock, _sleep: Mock
    ) -> None:
        touch = connect.return_value.touch
        connect.return_value.swipe_points = None
        touch.move.side_effect = RuntimeError("uiautomator rpc lost")

        with self.assertRaises(RuntimeError):
            ADBController(seed=3).swipe(270, 2000, 270, 700, duration_ms=530)

        touch.down.assert_called_once()
        self.assertGreaterEqual(touch.up.call_count, 1)

    @patch("x001.adb.time.sleep")
    @patch("x001.adb.subprocess.run")
    def test_adb_humanized_swipe_randomizes_every_call(
        self, run: Mock, _sleep: Mock
    ) -> None:
        signatures = []
        for seed in (1, 2, 3, 4):
            run.reset_mock()
            controller = ADBController(serial=None, input_backend="adb", seed=seed)
            controller.swipe(270, 2000, 270, 700, duration_ms=530)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertGreaterEqual(len(commands), 1)
            for command in commands:
                self.assertEqual(command[1:4], ["shell", "input", "swipe"])
                self.assertTrue(1 <= int(command[4]) <= 1078)
                self.assertTrue(1 <= int(command[5]) <= 2338)
                self.assertTrue(1 <= int(command[6]) <= 1078)
                self.assertTrue(1 <= int(command[7]) <= 2338)
                self.assertGreater(int(command[8]), 0)
            signatures.append(tuple(tuple(command) for command in commands))
        self.assertEqual(len(set(signatures)), 4)

    @patch("x001.adb.subprocess.run")
    def test_humanize_can_be_turned_off_for_the_controller(self, run: Mock) -> None:
        controller = ADBController(serial=None, input_backend="adb", humanize=False)

        controller.swipe(270, 2000, 270, 700, duration_ms=530)

        self.assertEqual(int(run.call_args.args[0][8]), 530)

    @patch("x001.adb.time.sleep")
    @patch("x001.adb.subprocess.run")
    def test_scroll_up_and_down_keep_direction(self, run: Mock, _sleep: Mock) -> None:
        for direction, sign in (("up", -1), ("down", 1)):
            run.reset_mock()
            controller = ADBController(serial=None, input_backend="adb", seed=11)
            controller.scroll(direction, duration_ms=400)
            command = run.call_args_list[0].args[0]
            self.assertGreater((int(command[7]) - int(command[5])) * sign, 0)
        with self.assertRaises(ValueError):
            controller.scroll("left")

    @patch("uiautomator2.connect")
    def test_linear_scroll_option_inside_body_region(self, connect: Mock):
        controller = ADBController(humanize=False)
        region = (200, 780, 340, 1900)
        controller.scroll("up", region=region, distance=640)
        controller.scroll("down", region=region, distance=5000)
        self.assertEqual(
            [call.args for call in connect.return_value.swipe.call_args_list],
            [(270, 1900, 270, 1260), (270, 780, 270, 1900)],
        )
        connect.return_value.touch.down.assert_not_called()


if __name__ == "__main__":
    unittest.main()
