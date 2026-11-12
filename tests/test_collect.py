import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from x001.collect import Collector, _save, main
from x001.freight import (
    detail_control,
    extract_fields,
    feedback_blocked,
    resolve_feedback,
)
from x001.storage import Store
from x001.vision import Card, Viewport, match_cards, read_viewport


class MatchingTests(unittest.TestCase):
    def test_ocr_variation_at_scroll_overlap_uses_card_pixels(self):
        price_ink = b"\x17" * 384
        body_ink = b"\x19" * 768
        old = Card(
            "银川湿焦今明均可装货|270元/吨", 270, 1800, None, price_ink, body_ink
        )
        same = Card(
            "银川湿焦今日均可装货|270元/吨", 270, 740, None, price_ink, body_ink
        )
        other = Card(
            "银川湿焦明天均可装货|270元/吨", 270, 1150, None, price_ink, b"\x99" * 768
        )
        self.assertEqual(
            match_cards((old,), [7], (same, other), scroll=True), [7, None]
        )

    def test_overlap_skips_captured_card_but_preserves_changed_time_or_price(self):
        old = (
            Card("石嘴山|7200元/趟", 270, 800, None),
            Card("银川明天8-20点|280元/吨", 270, 1700, None),
        )
        new = (
            Card(old[1].key, 270, 800, None),
            Card("银川后天8-20点|280元/吨", 270, 1200, None),
            Card("银川明天8-20点|290元/吨", 270, 1600, None),
        )
        self.assertEqual(match_cards(old, [1, 2], new, scroll=True), [2, None, None])

    def test_identical_candidate_with_multiple_occurrences_is_ambiguous(self):
        card = Card("同一摘要|280元/吨", 270, 800, None)
        self.assertEqual(match_cards((card,), [8], (card, card)), [None, None])

    def test_reorder_with_stationary_card_and_new_recommendation(self):
        a = Card("一|200元/吨", 270, 700, None)
        b = Card("二|300元/吨", 270, 1000, None)
        c = Card("三|400元/吨", 270, 1300, None)
        self.assertEqual(match_cards((a, b), [1, 2], (a, c, b)), [1, None, 2])


class StorageTests(unittest.TestCase):
    def test_failed_screenshot_write_preserves_previous_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "detail-01.png"
            _save(path, b"existing image")

            def interrupted_write(temporary_path, _data):
                with temporary_path.open("wb") as file:
                    file.write(b"partial image")
                raise OSError("disk write failed")

            with patch.object(
                Path, "write_bytes", autospec=True, side_effect=interrupted_write
            ):
                with self.assertRaisesRegex(OSError, "disk write failed"):
                    _save(path, b"new image")
            self.assertEqual(path.read_bytes(), b"existing image")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_existing_database_adds_review_fields_and_preserves_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "test.sqlite3")
            try:
                store.initialize()
                capture_id = store.begin("a", None)
                store.complete(capture_id, [], {"vehicle": "整车13米"}, 0.2)
                store.db.execute("ALTER TABLE freight DROP COLUMN review_fields")
                store.initialize()
                self.assertEqual(
                    tuple(
                        store.db.execute(
                            "SELECT vehicle, review_fields FROM freight"
                        ).fetchone()
                    ),
                    ("整车13米", "[]"),
                )
                store.complete(capture_id, [], {}, 0.2, {"vehicle"})
                self.assertEqual(
                    tuple(
                        store.db.execute(
                            "SELECT vehicle, review_fields FROM freight"
                        ).fetchone()
                    ),
                    (None, '["vehicle"]'),
                )
            finally:
                store.db.close()

    def test_resume_processing_and_upsert_same_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.sqlite3"
            store = Store(path)
            store.initialize()
            first = store.begin("a", "280元/吨")
            store.publish(first, [store.folder(first) / "card.png"])
            self.assertEqual(store.take()["id"], first)
            interrupted = store.begin("b", None)
            directory = store.folder(interrupted)
            directory.mkdir(parents=True)
            (directory / "card.png").touch()
            self.assertEqual(store.active_count(), 2)
            store.db.close()

            store = Store(path)
            try:
                store.recover()
                self.assertEqual(store.active_count(), 1)
                self.assertEqual(store.take()["id"], first)
                store.complete(first, [], {"price": "280元/吨"}, 0.2)
                store.complete(first, [], {"price": "300元/吨"}, 0.3)
                self.assertEqual(
                    [
                        tuple(row)
                        for row in store.db.execute(
                            "SELECT capture_id, price FROM freight"
                        )
                    ],
                    [(first, "300元/吨")],
                )
                row = store.db.execute(
                    "SELECT status, screenshots FROM captures WHERE id=?",
                    (interrupted,),
                ).fetchone()
                self.assertEqual(row["status"], "capture_failed")
                self.assertEqual(len(json.loads(row["screenshots"])), 1)
            finally:
                store.db.close()


class CollectorTests(unittest.TestCase):
    @patch("x001.collect.sleep")
    @patch("x001.collect.exclusive_run")
    @patch("x001.collect.ADBController")
    @patch("x001.collect.Collector")
    @patch("x001.collect.Worker")
    @patch("x001.collect.Store")
    def test_cli_drains_saved_captures_after_adb_timeout(
        self, store_type, worker_type, collector_type, _device_type, _lock, _sleep
    ):
        store = store_type.return_value
        store.active_count.side_effect = [1, 0]
        store.db.execute.return_value.fetchone.side_effect = [(0,), (1,), (0,)]
        collector_type.return_value.run.side_effect = TimeoutExpired(["adb"], 10)
        with (
            patch("sys.argv", ["x001-collect"]),
            patch("sys.stdout", new=Mock()),
            patch("sys.stderr", new=Mock()),
        ):
            with self.assertRaises(SystemExit) as stopped:
                main()
        self.assertEqual(stopped.exception.code, 1)
        worker_type.return_value.check.assert_called_once()
        worker_type.return_value.close.assert_called_once_with(graceful=True)

    @patch("x001.collect.sleep")
    def test_recommendation_streak_returns_to_top_and_refreshes(self, _sleep):
        png = BytesIO()
        Image.new("RGB", (1080, 2340), "white").save(png, format="PNG")
        cards = tuple(
            Card(str(i), 270, 600 + 350 * i, None, recommended=True) for i in range(3)
        )
        frame = png.getvalue(), Viewport(cards, (), False, True, "宁夏天津")
        store = Mock()
        store.active_count.return_value = 0
        collector = Collector(Mock(), store, Mock(), 3)
        collector._read_list = Mock(return_value=frame)
        collector._capture_detail = Mock(side_effect=[1, 2, 3])
        result = collector.run(None)
        self.assertIn("Stopped after 3 recommendations", result)
        self.assertIn("pull-to-refresh", result)
        collector.device.scroll.assert_called_once()
        self.assertEqual(collector.device.scroll.call_args.args, ("down",))

    @patch("x001.collect.sleep")
    def test_streak_does_not_skip_regular_card_visible_below_recommendations(
        self, _sleep
    ):
        png = BytesIO()
        Image.new("RGB", (1080, 2340), "white").save(png, format="PNG")
        cards = tuple(
            Card(str(i), 270, 600 + 350 * i, None, recommended=i < 3) for i in range(4)
        )
        store = Mock()
        store.active_count.return_value = 0
        collector = Collector(Mock(), store, Mock(), 3)
        collector._read_list = Mock(
            return_value=(png.getvalue(), Viewport(cards, (), False, True, "宁夏天津"))
        )
        collector._capture_detail = Mock(side_effect=[1, 2, 3, 4])
        self.assertIn("requested limit (4)", collector.run(4))
        collector.device.scroll.assert_not_called()

    @patch("x001.collect.sleep")
    def test_occlusion_at_end_can_take_reverse_supplement(self, _sleep):
        def frame(color):
            png = BytesIO()
            Image.new("RGB", (1080, 2340), color).save(png, format="PNG")
            return png.getvalue()

        original, reverse = frame("white"), frame("gray")
        device, store, worker = Mock(), Mock(), Mock()
        device.screenshot.side_effect = [
            original,
            original,
            reverse,
            original,
            original,
        ]
        blocked = [
            ("预估里程", 590, 100, 850, 132),
            ("车辆", 85, 150, 163, 190),
            ("需要车数", 85, 562, 220, 601),
            ("货源反馈", 925, 560, 1065, 593),
        ]
        worker.request.side_effect = [
            blocked,
            [("需要车数 还需4辆", 85, 840, 980, 880)],
        ]
        store.begin.return_value = 1
        with tempfile.TemporaryDirectory() as temporary:
            store.folder.return_value = Path(temporary) / "1"
            capture = Collector(device, store, worker, 3)
            capture._capture_detail(Card("test", 270, 900, None), original)
            paths = store.publish.call_args.args[1]
            self.assertEqual(
                [p.name for p in paths],
                ["card.png", "detail-01.png", "detail-02-supplement.png"],
            )
        self.assertEqual(
            [c.args[0] for c in device.scroll.call_args_list], ["up", "down", "up"]
        )

    @patch("x001.collect.sleep")
    def test_expand_scroll_and_skip_already_captured_overlap(self, _sleep):
        png = BytesIO()
        Image.new("RGB", (1080, 2340), "white").save(png, format="PNG")
        a = Card("甲|280元/吨", 270, 1600, "280元/吨")
        overlap = Card(a.key, 270, 700, a.price)
        b = Card("乙|320元/吨", 270, 1150, "320元/吨")
        frames = [
            Viewport((a,), ((540, 2050),), False, True, "宁夏→天津"),
            Viewport((a,), (), False, True, "宁夏→天津"),
            Viewport((a,), (), False, True, "宁夏→天津"),
            Viewport((overlap, b), (), False, True, "宁夏→天津"),
        ]
        store, worker, device = Mock(), Mock(), Mock()
        store.active_count.return_value = 0
        collector = Collector(device, store, worker, 3)
        collector._read_list = Mock(
            side_effect=[(png.getvalue(), frame) for frame in frames]
        )
        collector._capture_detail = Mock(side_effect=[11, 12])
        collector.run(2)
        self.assertEqual(
            [call.args[0] for call in collector._capture_detail.call_args_list], [a, b]
        )
        device.tap.assert_called_once_with(540, 2050)
        device.scroll.assert_called_once_with(
            "up", region=(270, 1381, 270, 2012), duration_ms=530
        )

    @patch("x001.collect.sleep")
    def test_capacity_waits_and_checks_worker(self, sleep):
        store, worker = Mock(), Mock()
        store.active_count.side_effect = [3, 3, 2]
        Collector(Mock(), store, worker, 3)._wait_for_capacity()
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(worker.check.call_count, 3)


class FreightTests(unittest.TestCase):
    def test_recommended_badge_is_distinct_from_similar(self):
        image = Image.new("RGB", (1080, 2340), (240, 240, 240))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 550, 1079, 890), fill="white")
        draw.rectangle((0, 905, 1079, 1245), fill="white")
        lines = [
            ("宁夏→天津", 380, 35, 670, 85),
            ("未开启听单", 145, 247, 365, 289),
            ("筛选", 917, 250, 1030, 288),
        ]
        for index, badge in enumerate(("推荐", "相似")):
            top = 448 + 355 * index
            lines.extend(
                [
                    (badge, 18, top, 75, top + 25),
                    ("银川灵武→天津滨海新区", 38, top + 30, 650, top + 76),
                    ("13米平板33吨", 38, top + 105, 590, top + 145),
                    ("280元/吨", 840, top + 100, 1040, top + 150),
                    ("去抢单", 885, top + 260, 1000, top + 298),
                ]
            )
        self.assertEqual(
            [c.recommended for c in read_viewport(image, lines).cards], [True, False]
        )

    def test_feedback_uses_complete_vehicle_or_marks_the_field(self):
        blocked = [
            ("车辆", 85, 573, 163, 612),
            ("整车13米", 618, 576, 830, 615),
            ("货源反馈", 925, 560, 1065, 593),
        ]
        clear = [
            ("车辆", 85, 340, 163, 379),
            ("整车13米平板/高栏", 618, 343, 994, 382),
            ("货源反馈", 925, 560, 1065, 593),
        ]
        self.assertEqual(feedback_blocked(clear, 1080), set())
        self.assertEqual(
            resolve_feedback([blocked], [1080], None, ""), ({}, {"vehicle"})
        )
        self.assertEqual(
            resolve_feedback([blocked, clear], [1080, 1080], None, ""),
            ({"vehicle": "整车13米平板/高栏"}, set()),
        )

    def test_address_obscured_on_first_frame_can_be_read_from_later_frame(self):
        overlay = ("货源反馈", 925, 560, 1065, 593)
        first = [("装:银川市灵武市", 140, 553, 831, 600), overlay]
        later = [("装:银川市灵武市物流中心", 140, 330, 994, 381), overlay]
        fields, review = resolve_feedback([first, later], [1080, 1080], None, "")
        self.assertEqual(fields["loading_location"], "银川市灵武市物流中心")
        self.assertEqual(review, set())

    def test_truck_count_without_number_preserves_the_visible_label(self):
        lines = [
            ("需要车数", 85, 800, 245, 841),
            ("需多车", 875, 803, 994, 844),
        ]
        self.assertEqual(extract_fields([lines], None)["truck_count"], "需多车")

    def test_range_read_as_dot_can_be_checked_against_list_and_price_is_negotiable(
        self,
    ):
        lines = [
            ("货物锌锭33.35吨捆扎", 85, 810, 995, 850),
            ("净得运费:请先与货主协商", 85, 950, 995, 990),
        ]
        fields = extract_fields([lines], None, "锌锭33-35吨|电议")
        self.assertEqual(fields["weight"], "33-35吨")
        self.assertEqual(fields["price"], "电议")
        self.assertEqual(extract_fields([lines], None)["weight"], "33.35吨")

    def test_only_detail_mileage_and_explicit_price_win(self):
        lines = [
            ("装:中卫市沙坡头区", 165, 180, 610, 220),
            ("卸:天津市滨海新区", 165, 300, 610, 340),
            ("826km", 130, 420, 250, 450),
            ("1330km", 580, 420, 730, 450),
            ("13米/预估里程", 580, 480, 800, 510),
            ("车辆", 85, 710, 165, 750),
            ("整车13米高栏", 460, 710, 800, 750),
            ("货物", 85, 810, 165, 850),
            ("塑料颗粒总重量31-36吨袋装", 460, 810, 1000, 850),
            ("需要车数", 85, 910, 240, 950),
            ("还需2辆", 700, 910, 980, 950),
            ("总运费", 85, 1010, 240, 1050),
            ("9800元", 790, 1010, 980, 1050),
        ]
        fields = extract_fields([lines], "9000元/趟")
        self.assertEqual(fields["price"], "9800元")
        self.assertEqual(fields["estimated_distance"], "1330km")
        self.assertEqual(fields["weight"], "31-36吨")
        self.assertEqual(fields["truck_count"], "2")
        self.assertEqual(fields["cargo"], "塑料颗粒袋装")

    def test_only_known_notice_has_a_dismiss_action(self):
        button = ("我知道了", 400, 1000, 600, 1060)
        notice = ("当前货源以下信息有变更", 250, 850, 750, 890)
        self.assertEqual(detail_control([notice, button]), ("notice", button))
        self.assertIsNone(detail_control([button]))
