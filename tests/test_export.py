import csv
import tempfile
import unittest
from pathlib import Path

from x001.export import export_report
from x001.storage import Store


class ExportTests(unittest.TestCase):
    def test_marks_failed_second_opinion_as_manual_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "test.sqlite3"
            output = Path(temporary) / "report.csv"
            store = Store(database)
            store.initialize()
            capture_id = store.begin("锌锭33-35吨|电议", None, "宁夏河北")
            store.complete(
                capture_id,
                [],
                {"weight": "33.35吨"},
                0.1,
                {"weight"},
                second_opinion={
                    "engine": "RapidOCR PP-OCRv5 mobile/OpenVINO",
                    "target_fields": ["weight"],
                    "fields": {},
                    "error": "model initialization failed",
                },
            )
            store.db.close()

            self.assertEqual(export_report(database, output, "宁夏河北"), 1)

            with output.open(encoding="utf-8-sig", newline="") as file:
                row = next(csv.DictReader(file))
            self.assertIn(
                "货物重量（PP-OCRv5 二次识别失败；未自动采用）",
                row["待核对项"],
            )

    def test_exports_only_completed_route_records_and_marks_anomalies(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "test.sqlite3"
            output = Path(temporary) / "nested" / "report.csv"
            store = Store(database)
            store.initialize()
            fields = {
                "weight": "33.35吨",
                "estimated_distance": "1200km",
                "vehicle": "13米高栏",
                "other_info": ' =HYPERLINK("https://example.test")',
                "unloading_location": "唐山市·丰润区",
                "loading_location": "银川市·灵武市",
                "shipper": "王老板",
                "cargo": "煤炭散装",
            }
            selected = store.begin("selected", None, "宁夏河北")
            store.complete(
                selected,
                [],
                fields,
                0.1,
                {"vehicle", "weight"},
                second_opinion={
                    "engine": "RapidOCR PP-OCRv5 mobile/OpenVINO",
                    "target_fields": ["weight"],
                    "fields": {"weight": "33-35吨"},
                },
            )
            other = store.begin("other", "200元/吨", "宁夏天津")
            store.complete(other, [], {"price": "200元/吨"}, 0.1)
            store.begin("pending", None, "宁夏河北")
            try:
                store.db.close()

                self.assertEqual(export_report(database, output, "宁夏河北"), 1)

                with output.open(encoding="utf-8-sig", newline="") as file:
                    rows = list(csv.DictReader(file))
                self.assertEqual(len(rows), 1)
                self.assertEqual(
                    list(rows[0]),
                    [
                        "序号",
                        "线路",
                        "采集编号",
                        "采集时间",
                        "货主",
                        "装货地",
                        "卸货地",
                        "货物",
                        "货物重量",
                        "预估里程",
                        "车型要求",
                        "需要车数",
                        "运价",
                        "其他信息",
                        "待核对项",
                    ],
                )
                self.assertEqual(rows[0]["线路"], "宁夏河北")
                self.assertEqual(rows[0]["采集编号"], str(selected))
                self.assertEqual(rows[0]["货物"], "煤炭散装")
                self.assertEqual(rows[0]["货物重量"], "33.35吨")
                self.assertEqual(rows[0]["运价"], "")
                self.assertEqual(rows[0]["需要车数"], "未显示")
                self.assertEqual(
                    rows[0]["待核对项"],
                    "货物重量（PP-OCRv5 二次意见：33-35吨；未自动采用）、"
                    "车型要求、运价",
                )
                self.assertEqual(rows[0]["其他信息"], "'" + fields["other_info"])
            finally:
                if store.db:
                    store.db.close()

    def test_export_requires_a_completed_match_and_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "test.sqlite3"
            output = Path(temporary) / "report.csv"
            store = Store(database)
            store.initialize()
            store.db.close()
            output.write_text("existing", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "No completed records"):
                export_report(database, output, "宁夏河北")

            self.assertEqual(output.read_text(encoding="utf-8"), "existing")


if __name__ == "__main__":
    unittest.main()
