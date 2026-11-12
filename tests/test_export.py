import csv
import tempfile
import unittest
import zipfile
from pathlib import Path

from x001.export import export_report, export_workbook
from x001.storage import Store


class ExportTests(unittest.TestCase):
    def test_workbook_exports_current_filters_across_routes(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "test.sqlite3"
            output = Path(temporary) / "filtered.xlsx"
            store = Store(database)
            store.initialize()
            selected = store.begin("selected", None, "河北宁夏")
            store.complete(
                selected,
                [],
                {
                    "cargo": "测试锰铁",
                    "vehicle": "13/13.7米 高栏",
                    "truck_count": "还需2辆",
                    "price": "260元/吨",
                },
                0.1,
            )
            other = store.begin("other", None, "宁夏天津")
            store.complete(
                other,
                [],
                {
                    "cargo": "测试塑料颗粒",
                    "vehicle": "17.5米 平板",
                    "truck_count": "还需7辆",
                    "price": "电议",
                },
                0.1,
            )
            store.db.close()

            self.assertEqual(
                export_workbook(
                    database,
                    output,
                    "",
                    vehicle="高栏",
                    trucks="1-2",
                    price="priced",
                    length="13.7",
                ),
                1,
            )
            with zipfile.ZipFile(output) as archive:
                strings = archive.read("xl/sharedStrings.xml").decode()
            self.assertIn("测试锰铁", strings)
            self.assertNotIn("测试塑料颗粒", strings)

    def test_workbook_is_styled_filterable_and_keeps_text_literal(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "test.sqlite3"
            output = Path(temporary) / "report.xlsx"
            store = Store(database)
            store.initialize()
            selected = store.begin("selected", None, "宁夏河北")
            store.complete(
                selected,
                [],
                {
                    "loading_location": "银川市-灵武市",
                    "unloading_location": "唐山市-丰润区",
                    "cargo": "锰铁",
                    "other_info": '=HYPERLINK("https://example.test")',
                },
                0.1,
                {"price"},
            )
            other = store.begin("other", None, "宁夏天津")
            store.complete(other, [], {"cargo": "塑料颗粒"}, 0.1)
            store.db.close()

            self.assertEqual(export_workbook(database, output, "宁夏河北"), 1)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                sheet = archive.read("xl/worksheets/sheet1.xml").decode()
                strings = archive.read("xl/sharedStrings.xml").decode()
                styles = archive.read("xl/styles.xml").decode()
            self.assertIn('state="frozen"', sheet)
            self.assertIn('ySplit="2"', sheet)
            self.assertNotIn("xSplit=", sheet)
            self.assertIn('autoFilter ref="A2:K3"', sheet)
            self.assertIn("宁夏 → 河北货源（1条）", strings)
            self.assertIn("货主", strings)
            self.assertIn("其他信息", strings)
            self.assertIn("银川市-灵武市", strings)
            self.assertNotIn("塑料颗粒", strings)
            self.assertIn("=HYPERLINK", strings)
            self.assertNotIn("<f>", sheet)
            self.assertIn("4472C4", styles)
            self.assertNotIn('r="A4"', sheet)

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
