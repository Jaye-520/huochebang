import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from x001.collect import (
    Collector,
    _recognize_group,
    _reconcile_consensus_weight,
    _save,
    _second_opinion,
    main,
)
from x001.engines import HybridEngine, WindowsOCR
from x001.freight import (
    clean_freight,
    detail_control,
    extract_fields,
    feedback_blocked,
    recognize_detail,
    resolve_feedback,
)
from x001.storage import Store
from x001.vision import (
    Card,
    Viewport,
    _body_ink,
    _viewed_title,
    match_cards,
    read_viewport,
)


class MatchingTests(unittest.TestCase):
    def test_viewed_title_accepts_gray_during_return_animation(self):
        import numpy as np

        image = Image.new("RGB", (200, 40), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 10, 120, 30), fill=(164, 164, 164))
        self.assertTrue(_viewed_title(np.asarray(image)))
        draw.rectangle((10, 10, 120, 30), fill=(27, 27, 27))
        self.assertFalse(_viewed_title(np.asarray(image)))

    def test_body_ink_survives_card_top_clipped_by_header(self):
        image = Image.new("RGB", (1080, 2340), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((38, 586, 240, 611), fill=163)
        draw.rectangle((38, 680, 370, 712), fill=194)
        full = _body_ink(image, 500, 896, viewed=True)
        clipped = _body_ink(image, 570, 896, viewed=True)
        self.assertTrue(full)
        self.assertEqual(full, clipped)

    def test_scroll_matches_clipped_card_using_its_visible_body(self):
        old = Card(
            "吴忠盐池邢台隆尧13米高栏34吨石膏散装今天装货|电议",
            270,
            1654,
            None,
            body_ink=b"\x19" * 768,
            tail_ink=b"\x17" * 768,
        )
        clipped = Card(
            "吴思盐旭邢冒陲尧13米高栏34吨石膏散装今天装货|电议",
            270,
            566,
            None,
            body_ink=b"\x99" * 768,
            viewed=True,
            clipped=True,
            tail_ink=old.tail_ink,
        )
        self.assertEqual(match_cards((old,), [7], (clipped,), scroll=True), [7])

    def test_list_requires_right_side_controls_after_return(self):
        image = Image.new("RGB", (1080, 2340), "white")
        left = [
            ("宁夏乛河北", 390, 35, 660, 68),
            ("已订阅", 37, 135, 180, 170),
            ("未开启听单", 145, 248, 330, 285),
        ]
        right = [
            ("设置", 965, 41, 1040, 72),
            ("筛选", 917, 250, 1030, 288),
        ]
        partial = read_viewport(image, left)
        self.assertEqual(partial.route, "宁夏河北")
        self.assertFalse(partial.is_list)
        self.assertTrue(read_viewport(image, left + right).is_list)

    def test_visited_card_matches_after_fade_shift_and_ocr_changes(self):
        def body(color, offset=0, *, changed=False):
            image = Image.new("RGB", (1080, 2340), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((1, 580, 14, 598), fill="red")
            draw.rectangle((38 + offset, 584, 240 + offset, 612), fill=color)
            draw.rectangle(
                (38 + offset, 685, (155 if changed else 370) + offset, 717),
                fill=color,
            )
            return _body_ink(image, 546, 896, viewed=color == 163)

        price_ink = b"\x17" * 384
        old = Card(
            "固原原州13米高栏35吨杏核|8800元", 270, 598, None, price_ink, body(26)
        )
        same = Card(
            "固原原州0到3吨杏核|8800元", 270, 598, None, price_ink, body(163, 6)
        )
        changed = Card(
            "固原原州后天装货|8800元",
            270,
            598,
            None,
            price_ink,
            body(163, 6, changed=True),
        )
        self.assertEqual(match_cards((old,), [7], (same,)), [7])
        self.assertEqual(match_cards((old,), [7], (changed,)), [None])

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

    def test_negotiable_price_matches_stable_pixels_despite_ocr_number_change(self):
        body_ink = b"\x19" * 768
        old = Card(
            "固原原州13/13.7米90吨今天15点前装货|电议",
            270,
            598,
            None,
            body_ink=body_ink,
        )
        same = Card(
            "固原原州0到13/13.7米90吨今天15点前装货|电议",
            270,
            598,
            None,
            body_ink=body_ink,
        )
        other = Card(
            "固原原州13/13.7米90吨明天15点前装货|电议",
            270,
            598,
            None,
            body_ink=b"\x99" * 768,
        )
        self.assertEqual(match_cards((old,), [7], (same,)), [7])
        self.assertEqual(match_cards((old,), [7], (other,)), [None])

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

    def test_gray_title_recovers_displaced_card_from_this_round(self):
        body_ink = b"\x19" * 768
        key = "固原原州今天装货|电议"
        captured = Card(key, 270, 600, None, body_ink=body_ink)
        gray = Card(key, 270, 1200, None, body_ink=body_ink, viewed=True)
        dark = Card(key, 270, 1200, None, body_ink=body_ink)
        self.assertEqual(match_cards((captured,), [7], (gray,), historical=True), [7])
        self.assertEqual(
            match_cards((captured,), [7], (dark,), historical=True), [None]
        )
        self.assertEqual(match_cards((captured,), [None], (gray,)), [None])
        self.assertEqual(match_cards((captured,), [7], (gray, gray)), [None, None])

    def test_reorder_with_stationary_card_and_new_recommendation(self):
        a = Card("一|200元/吨", 270, 700, None)
        b = Card("二|300元/吨", 270, 1000, None)
        c = Card("三|400元/吨", 270, 1300, None)
        self.assertEqual(match_cards((a, b), [1, 2], (a, c, b)), [1, None, 2])


class StorageTests(unittest.TestCase):
    def test_existing_database_adds_route_column_and_keeps_capture_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "test.sqlite3")
            try:
                store.initialize()
                capture_id = store.begin("a", None, "宁夏河北")
                store.db.execute("ALTER TABLE captures DROP COLUMN route")
                store.initialize()
                row = store.db.execute(
                    "SELECT card_key, route FROM captures WHERE id=?", (capture_id,)
                ).fetchone()
                self.assertEqual(tuple(row), ("a", None))
            finally:
                store.db.close()

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
                store.db.execute("DROP VIEW freight_report")
                store.db.execute("ALTER TABLE freight DROP COLUMN review_fields")
                store.db.execute("ALTER TABLE freight DROP COLUMN second_opinion")
                store.initialize()
                self.assertEqual(
                    tuple(
                        store.db.execute(
                            "SELECT vehicle, review_fields, second_opinion FROM freight"
                        ).fetchone()
                    ),
                    ("整车13米", "[]", None),
                )
                opinion = {
                    "engine": "RapidOCR PP-OCRv5 mobile/OpenVINO",
                    "target_fields": ["weight"],
                    "fields": {"weight": "33-35吨"},
                }
                store.complete(
                    capture_id, [], {}, 0.2, {"vehicle"}, second_opinion=opinion
                )
                self.assertEqual(
                    tuple(
                        store.db.execute(
                            "SELECT vehicle, review_fields, second_opinion FROM freight"
                        ).fetchone()
                    ),
                    (None, '["vehicle"]', json.dumps(opinion, ensure_ascii=False)),
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
    def test_expected_route_rejects_different_phone_route(self, _sleep):
        collector = Collector(Mock(), Mock(), Mock(), 3)
        collector._read_list = Mock(
            return_value=(b"list", Viewport((), (), False, True, "宁夏天津"))
        )
        with self.assertRaisesRegex(RuntimeError, "expected='宁夏河北'"):
            collector._wait_for_list("宁夏河北")

    @patch("x001.collect.sleep")
    def test_return_cancels_system_prompt_and_uses_detail_back_button(self, _sleep):
        collector = Collector(Mock(), Mock(), Mock(), 3)
        popup = Viewport((), (), False, False, voice_cancel=(299, 2061))
        detail = Viewport((), (), False, False, detail_back=(81, 168))
        original = (b"list", Viewport((), (), False, True, "河北宁夏"))
        collector._read_list = Mock(
            side_effect=[(b"popup", popup), (b"detail", detail), original]
        )
        self.assertEqual(
            collector._wait_for_list("河北宁夏", recover_detail=True), original
        )
        self.assertEqual(
            [call.args for call in collector.device.tap.call_args_list],
            [(299, 2061), (81, 168)],
        )

    @patch("x001.collect.sleep")
    def test_retries_detail_back_when_detail_remains_visible(self, _sleep):
        collector = Collector(Mock(), Mock(), Mock(), 3)
        detail = (b"detail", Viewport((), (), False, False, detail_back=(81, 168)))
        original = (b"list", Viewport((), (), False, True, "宁夏河北"))
        collector._read_list = Mock(side_effect=[detail] * 5 + [original])

        self.assertEqual(
            collector._wait_for_list("宁夏河北", recover_detail=True), original
        )
        self.assertEqual(
            [call.args for call in collector.device.tap.call_args_list],
            [(81, 168), (81, 168)],
        )

    @patch("x001.collect.sleep")
    def test_detail_waits_for_placeholder_buttons_before_scrolling(self, _sleep):
        image = Image.new("RGB", (1080, 2340), "white")
        ready_buffer = BytesIO()
        image.save(ready_buffer, format="PNG")
        ready = ready_buffer.getvalue()
        ImageDraw.Draw(image).rectangle((30, 2059, 1050, 2153), fill=(244, 244, 244))
        loading_buffer = BytesIO()
        image.save(loading_buffer, format="PNG")
        loading = loading_buffer.getvalue()
        device, store, worker = Mock(), Mock(), Mock()
        device.screenshot.side_effect = [loading, ready, ready, ready]
        worker.request.return_value = [
            ("预估里程", 590, 100, 850, 132),
            ("车辆", 85, 150, 163, 190),
        ]
        store.begin.return_value = 1
        with tempfile.TemporaryDirectory() as temporary:
            store.folder.return_value = Path(temporary) / "1"
            collector = Collector(device, store, worker, 3)
            collector._capture_detail(Card("test", 270, 900, None), ready)
            first_detail = store.folder.return_value / "detail-01.png"
            self.assertEqual(first_detail.read_bytes(), ready)
        self.assertEqual(
            [call.args[1][1] for call in worker.request.call_args_list],
            [loading, ready],
        )
        device.scroll.assert_called_once()

    @patch("x001.collect.sleep")
    def test_persistent_loading_saves_confirmed_detail_and_returns(self, _sleep):
        image = Image.new("RGB", (1080, 2340), "white")
        ImageDraw.Draw(image).rectangle((30, 2059, 1050, 2153), fill=(244, 244, 244))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        frame = buffer.getvalue()
        device, store, worker = Mock(), Mock(), Mock()
        device.screenshot.return_value = frame
        worker.request.return_value = [
            ("预估里程", 590, 100, 850, 132),
            ("车辆", 85, 150, 163, 190),
        ]
        store.begin.return_value = 1
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "1"
            store.folder.return_value = directory
            collector = Collector(device, store, worker, 3)
            self.assertEqual(
                collector._capture_detail(Card("test", 270, 900, None), frame), 1
            )
            store.publish.assert_called_once_with(
                1, [directory / "card.png", directory / "detail-01.png"]
            )
        device.back.assert_called_once()
        device.scroll.assert_not_called()

    @patch("x001.collect.sleep")
    def test_scrolls_past_gray_cards_and_opens_only_unread_card(self, _sleep):
        png = BytesIO()
        Image.new("RGB", (1080, 2340), "white").save(png, format="PNG")
        gray = Card("已读|电议", 270, 600, None, viewed=True)
        unread = Card("未读|电议", 270, 960, None)
        store = Mock()
        store.active_count.return_value = 0
        collector = Collector(Mock(), store, Mock(), 3)
        collector._read_list = Mock(
            side_effect=[
                (png.getvalue(), Viewport((gray,), (), False, True, "宁夏河北")),
                (png.getvalue(), Viewport((gray, unread), (), False, True, "宁夏河北")),
            ]
        )
        collector._capture_detail = Mock(return_value=1)
        self.assertIn("requested limit (1)", collector.run(1))
        collector.device.scroll.assert_called_once()
        collector._capture_detail.assert_called_once_with(
            unread, png.getvalue(), "宁夏河北"
        )

    @patch("x001.collect.sleep")
    def test_wait_for_list_retries_until_the_original_route_returns(self, _sleep):
        collector = Collector(Mock(), Mock(), Mock(), 3)
        detail = (b"detail", Viewport((), (), False, False))
        other = (b"other", Viewport((), (), False, True, "宁夏天津"))
        original = (b"list", Viewport((), (), False, True, "宁夏河北"))
        collector._read_list = Mock(
            side_effect=[detail, detail, detail, other, original]
        )
        self.assertEqual(collector._wait_for_list("宁夏河北"), original)

        collector._read_list = Mock(return_value=other)
        with self.assertRaisesRegex(RuntimeError, "last OCR route='宁夏天津'"):
            collector._wait_for_list("宁夏河北")

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
    def test_recommendation_section_refreshes_and_restarts_without_recapturing(
        self, _sleep
    ):
        png = BytesIO()
        Image.new("RGB", (1080, 2340), "white").save(png, format="PNG")
        initial_cards = (
            Card("regular-1", 270, 600, None),
            Card("regular-2", 270, 950, None),
            Card("recommended-1", 270, 1300, None, recommended=True),
            Card("recommended-2", 270, 1650, None, recommended=True),
        )
        refreshed_cards = (
            *initial_cards[:2],
            Card("regular-new", 270, 1120, None),
            *initial_cards[2:],
        )
        initial = (
            png.getvalue(),
            Viewport(initial_cards, (), False, True, "宁夏天津"),
        )
        refreshed = (
            png.getvalue(),
            Viewport(refreshed_cards, (), False, True, "宁夏天津"),
        )
        store = Mock()
        store.active_count.return_value = 0
        collector = Collector(Mock(), store, Mock(), 3)
        collector._read_list = Mock(side_effect=[initial, initial, initial, refreshed])
        collector._capture_detail = Mock(side_effect=[1, 2, 3])
        collector._return_and_refresh = Mock(return_value=None)

        result = collector.run(3)

        self.assertIn("requested limit (3)", result)
        self.assertEqual(
            [call.args[0].key for call in collector._capture_detail.call_args_list],
            ["regular-1", "regular-2", "regular-new"],
        )
        collector._return_and_refresh.assert_called_once_with(
            "宁夏天津", initial[0], initial[1]
        )

    @patch("x001.collect.sleep")
    def test_return_to_top_issues_a_separate_refresh_pull(self, _sleep):
        png = BytesIO()
        Image.new("RGB", (1080, 2340), "white").save(png, format="PNG")
        image = png.getvalue()
        view = Viewport(
            (Card("recommended", 270, 600, None, recommended=True),),
            (),
            False,
            True,
            "宁夏天津",
        )
        collector = Collector(Mock(), Mock(), Mock(), 3)
        collector._wait_for_list = Mock(return_value=(image, view))

        self.assertIsNone(collector._return_and_refresh("宁夏天津", image, view))

        self.assertEqual(collector.device.scroll.call_count, 2)
        self.assertEqual(collector.device.scroll.call_args_list[0].args, ("down",))
        self.assertEqual(collector.device.scroll.call_args_list[1].args, ("down",))
        self.assertEqual(
            collector.device.scroll.call_args_list[1].kwargs["distance"], 585
        )

    @patch("x001.collect.sleep")
    def test_refresh_waits_until_the_refresh_indicator_clears(self, _sleep):
        png = BytesIO()
        Image.new("RGB", (1080, 2340), "white").save(png, format="PNG")
        image = png.getvalue()
        card = Card("recommended", 270, 600, None, recommended=True)
        initial = Viewport((card,), (), False, True, "宁夏天津")
        refreshing = Viewport((card,), (), False, True, "宁夏天津", refreshing=True)
        settled = Viewport((card,), (), False, True, "宁夏天津")
        collector = Collector(Mock(), Mock(), Mock(), 3)
        collector._wait_for_list = Mock(
            side_effect=[
                (image, refreshing),
                (image, refreshing),
                (image, settled),
            ]
        )

        self.assertIsNone(collector._return_and_refresh("宁夏天津", image, initial))

        collector.device.scroll.assert_called_once()
        self.assertEqual(collector._wait_for_list.call_count, 3)

    @patch("x001.collect.sleep")
    def test_regular_card_below_recommendations_is_still_captured(self, _sleep):
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
        collector._capture_detail = Mock(return_value=4)
        self.assertIn("requested limit (1)", collector.run(1))
        collector._capture_detail.assert_called_once_with(
            cards[3], png.getvalue(), "宁夏天津"
        )
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
            Viewport((a,), ((542, 2052),), False, True, "宁夏→天津"),
            Viewport((a,), (), False, True, "宁夏→天津"),
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
    def test_consensus_range_corrects_only_the_suspected_separator(self):
        fields = {"weight": "总重200.210吨 吨包"}
        review = {"weight"}
        opinion = {
            "fields": {"weight": "总重200-210吨 吨包"},
            "review_fields": [],
            "applied_fields": [],
        }

        self.assertTrue(_reconcile_consensus_weight(fields, review, opinion))
        self.assertEqual(fields["weight"], "总重200-210吨 吨包")
        self.assertEqual(review, set())
        self.assertEqual(opinion["applied_fields"], ["weight"])

    def test_consensus_weight_is_not_applied_when_opinions_differ_or_are_reviewed(self):
        for secondary, secondary_review in (
            ("总重200-211吨 吨包", []),
            ("总重200-210吨 吨包", ["weight"]),
        ):
            with self.subTest(secondary_review=secondary_review):
                fields = {"weight": "总重200.210吨 吨包"}
                review = {"weight"}
                opinion = {
                    "fields": {"weight": secondary},
                    "review_fields": secondary_review,
                    "applied_fields": [],
                }

                self.assertFalse(_reconcile_consensus_weight(fields, review, opinion))
                self.assertEqual(fields["weight"], "总重200.210吨 吨包")
                self.assertEqual(review, {"weight"})
                self.assertEqual(opinion["applied_fields"], [])

    def test_failed_second_opinion_keeps_primary_result_for_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "test.sqlite3")
            try:
                store.initialize()
                capture_id = store.begin("锌锭33-35吨|电议", None, "宁夏河北")
                folder = store.folder(capture_id)
                folder.mkdir(parents=True)
                card = folder / "card.png"
                detail = folder / "detail.png"
                Image.new("RGB", (1080, 2340), "white").save(card)
                Image.new("RGB", (1080, 2340), "white").save(detail)
                store.publish(capture_id, [card, detail])
                task = store.take()
                engine = object.__new__(HybridEngine)
                fields = {"weight": "33.35吨", "truck_count": "1"}
                lines = [("货物锌锭", 100, 100, 300, 140)]

                with (
                    patch("x001.collect.recognize_detail", return_value=lines),
                    patch("x001.collect.detail_confirmed", return_value=True),
                    patch(
                        "x001.collect.resolve_feedback",
                        return_value=(fields, {"weight"}),
                    ),
                    patch("x001.collect.clean_freight"),
                    patch.object(
                        engine,
                        "locate_secondary",
                        side_effect=RuntimeError("model initialization failed"),
                    ),
                ):
                    list(_recognize_group(engine, store, task, {}))

                capture = store.db.execute(
                    "SELECT status, error FROM captures WHERE id=?", (capture_id,)
                ).fetchone()
                result = store.db.execute(
                    "SELECT weight, review_fields, second_opinion "
                    "FROM freight WHERE capture_id=?",
                    (capture_id,),
                ).fetchone()
            finally:
                store.db.close()

        self.assertEqual(tuple(capture), ("done", None))
        self.assertEqual(result["weight"], "33.35吨")
        self.assertEqual(json.loads(result["review_fields"]), ["weight"])
        opinion = json.loads(result["second_opinion"])
        self.assertEqual(opinion["target_fields"], ["weight"])
        self.assertEqual(opinion["fields"], {})
        self.assertEqual(opinion["error"], "model initialization failed")

    def test_recognize_group_applies_clean_cross_engine_weight_agreement(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "test.sqlite3")
            try:
                store.initialize()
                capture_id = store.begin("锌锭33-34吨|电议", None, "宁夏河北")
                folder = store.folder(capture_id)
                folder.mkdir(parents=True)
                card = folder / "card.png"
                detail = folder / "detail.png"
                Image.new("RGB", (1080, 2340), "white").save(card)
                Image.new("RGB", (1080, 2340), "white").save(detail)
                store.publish(capture_id, [card, detail])
                task = store.take()
                engine = object.__new__(HybridEngine)
                fields = {
                    "weight": "总重33.34吨 袋装",
                    "cargo": "合金块",
                    "truck_count": "1",
                }
                lines = [("货物锌锭", 100, 100, 300, 140)]
                opinion = {
                    "engine": "RapidOCR PP-OCRv5 mobile/OpenVINO",
                    "target_fields": ["cargo", "weight"],
                    "fields": {
                        "cargo": "合金块",
                        "weight": "总重33-34吨 袋装",
                    },
                    "review_fields": [],
                    "seconds": 0.1,
                    "raw_pages": [lines],
                    "applied_fields": [],
                }

                with (
                    patch("x001.collect.recognize_detail", return_value=lines),
                    patch("x001.collect.detail_confirmed", return_value=True),
                    patch(
                        "x001.collect.resolve_feedback",
                        return_value=(fields, {"cargo", "weight"}),
                    ),
                    patch("x001.collect.clean_freight"),
                    patch("x001.collect._second_opinion", return_value=opinion),
                ):
                    list(_recognize_group(engine, store, task, {}))

                result = store.db.execute(
                    "SELECT weight, review_fields, second_opinion "
                    "FROM freight WHERE capture_id=?",
                    (capture_id,),
                ).fetchone()
            finally:
                store.db.close()

        self.assertEqual(result["weight"], "总重33-34吨 袋装")
        self.assertEqual(json.loads(result["review_fields"]), ["cargo"])
        opinion = json.loads(result["second_opinion"])
        self.assertEqual(opinion["target_fields"], ["cargo", "weight"])
        self.assertEqual(opinion["applied_fields"], ["weight"])

    def test_second_opinion_is_stored_as_a_suggestion_for_requested_fields(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGB", (1080, 2340), "white").save(root / "detail-01.png")
            lines = [("货物锌锭总重量33-35吨吨包", 460, 800, 990, 850)]
            engine = Mock()
            engine.locate_secondary.return_value = lines

            opinion = _second_opinion(
                engine,
                SimpleNamespace(root=root),
                ["detail-01.png"],
                {"weight"},
                None,
                "锌锭33-35吨|电议",
            )

        self.assertEqual(opinion["fields"], {"weight": "总重33-35吨 吨包"})
        self.assertEqual(opinion["raw_pages"], [lines])
        self.assertEqual(opinion["engine"], "RapidOCR PP-OCRv5 mobile/OpenVINO")
        self.assertEqual(opinion["target_fields"], ["weight"])
        self.assertEqual(opinion["applied_fields"], [])
        self.assertGreaterEqual(opinion["seconds"], 0)

    def test_unlabeled_address_aligned_with_dropoff_badge_is_recovered(self):
        image = Image.new("RGB", (1080, 900), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((75, 200, 116, 240), fill=(220, 30, 30))
        draw.rectangle((75, 300, 116, 340), fill=(0, 0, 0))
        address = ("保定市·雄县", 140, 304, 410, 343)
        engine = object.__new__(WindowsOCR)

        with patch.object(WindowsOCR, "locate", side_effect=[[address], [], []]):
            lines = recognize_detail(engine, image, first=True)

        self.assertIn("卸:保定市·雄县", [line[0] for line in lines])
        self.assertNotIn("保定市·雄县", [line[0] for line in lines])

    def test_recovers_loading_time_when_ocr_merges_dash_and_leading_one(self):
        fields = {}
        review = set()

        clean_freight(
            [[("明天06：00刁2：00可装", 41, 49, 600, 120)]],
            fields,
            review,
            "",
        )

        self.assertEqual(fields["other_info"], "明天06:00-12:00可装")
        self.assertNotIn("other_info", review)

    def test_list_card_confirms_a_malformed_loading_time(self):
        fields = {}
        review = set()

        clean_freight(
            [[("明天06：00刁／：00可装", 41, 49, 600, 120)]],
            fields,
            review,
            "矿粉吨包明6-17点装货|220元/吨",
        )

        self.assertEqual(fields["other_info"], "明天6-17点装货")
        self.assertNotIn("other_info", review)

    def test_cleaning_repairs_common_ocr_packaging_suffixes(self):
        examples = (
            ("肥料袋〗", "肥料", "33吨 袋装"),
            ("木材木头散丬", "木材木头", "66-68吨 散装"),
            ("化工产品袋丬", "化工产品", "34吨 袋装"),
            ("大葱捆扌", "大葱", "30-35吨 捆扎"),
        )
        for cargo, expected_cargo, expected_weight in examples:
            with self.subTest(cargo=cargo):
                fields = {
                    "cargo": cargo,
                    "weight": expected_weight.split(" ", 1)[0],
                }
                review = set()

                clean_freight([], fields, review, "")

                self.assertEqual(fields["cargo"], expected_cargo)
                self.assertEqual(fields["weight"], expected_weight)
                self.assertNotIn("cargo", review)

    def test_cleaning_marks_unrecognized_cargo_glyphs_for_review(self):
        fields = {"cargo": "化肥衣亻弋丬", "weight": "35吨"}
        review = set()

        clean_freight([], fields, review, "")

        self.assertIn("cargo", review)

    def test_cleaning_formats_table_and_keeps_ambiguous_weight_for_review(self):
        lines = [
            ("明天06:00-12:00可装", 41, 49, 600, 120),
            ("结算4·5爽约4·4评价4·5", 230, 550, 666, 583),
            ("货物矿渣总重量33．36吨散装", 85, 810, 995, 850),
            ("净得运费6200元", 85, 950, 995, 990),
            ("灬费6200元/趟", 85, 1050, 995, 1090),
        ]
        fields = extract_fields([lines], None)
        fields.update(loading_location="唐山市·迁西县O", truck_count="2")
        review = set()
        clean_freight([lines], fields, review, "")
        self.assertEqual(fields["loading_location"], "唐山市-迁西县")
        self.assertEqual(fields["cargo"], "矿渣")
        self.assertEqual(fields["weight"], "总重33.36吨 散装")
        self.assertEqual(fields["truck_count"], "还需2辆")
        self.assertEqual(fields["price"], "净得6200元(总运费6200元/趟)")
        self.assertEqual(
            fields["other_info"], "明天06:00-12:00可装；结算4.5/爽约4.4/评价4.5"
        )
        self.assertIn("weight", review)

    def test_cleaning_removes_ocr_vertical_bars_from_locations(self):
        fields = {
            "loading_location": "丨银川市|灵武市",
            "unloading_location": "｜天津市-东丽区丨",
        }
        clean_freight([], fields, set(), "")
        self.assertEqual(fields["loading_location"], "银川市-灵武市")
        self.assertEqual(fields["unloading_location"], "天津市-东丽区")

    def test_cleaning_removes_repeated_place_and_trailing_marker(self):
        fields = {
            "loading_location": "青铜峡青铜峡",
            "unloading_location": "天津市-东丽区一/",
            "cargo": "吨包锰铁吨包",
            "weight": "105吨",
        }
        clean_freight([], fields, set(), "")
        self.assertEqual(fields["loading_location"], "青铜峡")
        self.assertEqual(fields["unloading_location"], "天津市-东丽区")
        self.assertEqual(fields["cargo"], "锰铁")
        self.assertEqual(fields["weight"], "105吨 吨包")

    def test_cleaning_uses_card_time_when_ocr_time_is_clipped(self):
        fields = {"other_info": "需雨布；以上信息由系统根据货主上传信息自动发送"}
        clean_freight(
            [[("明天08:00", 41, 49, 300, 120)]],
            fields,
            set(),
            "锰铁明天8-17点装货|电议",
        )
        self.assertEqual(fields["other_info"], "明天8-17点装货；需雨布")

    def test_mileage_reads_only_the_estimated_distance_column(self):
        lines = [
            ("1003·6km", 135, 502, 319, 535),
            ("11OOkm", 592, 502, 736, 535),
            ("9.6米/预估里程", 589, 559, 793, 589),
        ]
        self.assertEqual(extract_fields([lines], None)["estimated_distance"], "1100km")

    def test_truncated_net_price_requires_total_and_fee_to_recover(self):
        net_and_total = [
            ("净得运费300元", 85, 950, 995, 990),
            ("总运费6500元/趟", 85, 1050, 995, 1090),
        ]
        key = "筛子|6300元"
        fields = extract_fields([net_and_total], None, key)
        review = set()
        clean_freight([net_and_total], fields, review, key)
        self.assertEqual(fields["price"], "6500元/趟")
        self.assertIn("price", review)

        lines = net_and_total + [("信息费200元", 85, 1150, 995, 1190)]
        fields = extract_fields([lines], None, key)
        review = set()
        clean_freight([lines], fields, review, key)
        self.assertEqual(fields["price"], "净得6300元(总运费6500元/趟)")
        self.assertNotIn("price", review)

    def test_unitless_price_uses_matching_card_and_detail_unit(self):
        fields = {"price": "260元"}
        review = set()
        lines = [("净得运费260元/吨", 85, 950, 995, 990)]

        clean_freight([lines], fields, review, "锰铁|260元/吨")

        self.assertEqual(fields["price"], "260元/吨")
        self.assertNotIn("price", review)

    def test_unitless_price_stays_for_review_when_card_and_detail_disagree(self):
        fields = {"price": "260元"}
        review = set()
        lines = [("净得运费260元/吨", 85, 950, 995, 990)]

        clean_freight([lines], fields, review, "锰铁|260元/趟")

        self.assertEqual(fields["price"], "260元")
        self.assertIn("price", review)

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

    def test_recommendation_section_header_marks_only_cards_below_it(self):
        image = Image.new("RGB", (1080, 2340), (240, 240, 240))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 500, 1079, 840), fill="white")
        draw.rectangle((0, 900, 1079, 1320), fill="white")
        lines = [
            ("宁夏→天津", 380, 35, 670, 85),
            ("未开启听单", 145, 247, 365, 289),
            ("筛选", 917, 250, 1030, 288),
            ("银川灵武→天津滨海新区", 38, 400, 650, 448),
            ("13米平板33吨", 38, 505, 590, 545),
            ("280元/吨", 840, 550, 1040, 600),
            ("去抢单", 885, 660, 1000, 700),
            ("推荐货源", 400, 740, 680, 780),
            ("石嘴山大武口→廊坊广阳", 38, 845, 650, 893),
            ("13米平板33吨", 38, 950, 590, 990),
            ("7300元/趟", 840, 995, 1040, 1045),
            ("去抢单", 885, 1145, 1000, 1185),
        ]

        self.assertEqual(
            [card.recommended for card in read_viewport(image, lines).cards],
            [False, True],
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
