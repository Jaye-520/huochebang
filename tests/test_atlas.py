import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from x001.atlas import DATA, atlas_snapshot, locate, locate_district
from x001.storage import Store
from x001.web import Console, WebServer


class AtlasTests(unittest.TestCase):
    def test_district_coverage_and_ambiguous_names(self):
        data = json.loads(DATA.read_text(encoding="utf-8"))
        districts = data["districts"]
        self.assertEqual(len({d["id"] for d in districts}), 205)
        self.assertEqual(sum(d["province"] == "天津" for d in districts), 16)
        self.assertEqual(sum(d["province"] == "宁夏" for d in districts), 22)
        self.assertEqual(sum(d["province"] == "河北" for d in districts), 167)
        self.assertIsNone(locate_district("桥西区", districts, None))
        self.assertEqual(
            locate_district("石家庄市-桥西区", districts, "石家庄")["parent"],
            "石家庄",
        )
        self.assertEqual(
            locate_district("武邑县-工业园", districts, None)["parent"], "衡水"
        )
        self.assertIsNone(locate_district("衡水市", districts, "衡水"))

    def test_address_matching_is_explicit(self):
        cities = json.loads(DATA.read_text(encoding="utf-8"))["cities"]
        self.assertEqual(locate("宁夏回族自治区银川市-兴庆区", cities), "银川")
        self.assertEqual(locate("石嘴山市-平罗县", cities), "石嘴山")
        self.assertEqual(locate("天津市-滨海新区", cities), "天津")
        self.assertIsNone(locate("某物流园银川仓库", cities))
        self.assertIsNone(locate("银川路-物流园", cities))
        self.assertIsNone(locate("宁夏", cities))
        self.assertEqual(locate("盐池县-工业园", cities), "吴忠")

    def test_http_assets_and_missing_database(self):
        with tempfile.TemporaryDirectory() as folder:
            server = WebServer(
                ("127.0.0.1", 0), Console(Path(folder) / "missing.sqlite3"), None
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                for path, mime in (
                    ("/", "text/html"),
                    ("/atlas.js", "text/javascript"),
                    ("/shell.js", "text/javascript"),
                    ("/atlas.css", "text/css"),
                    ("/atlas-data.json", "application/json"),
                    ("/china-map.json", "application/json"),
                    ("/api/atlas", "application/json"),
                ):
                    with self.subTest(path=path):
                        connection = http.client.HTTPConnection(
                            *server.server_address, timeout=3
                        )
                        try:
                            connection.request("GET", path)
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertIn(mime, response.getheader("Content-Type"))
                            body = response.read()
                            if path == "/api/atlas":
                                self.assertEqual(json.loads(body)["total"], 0)
                        finally:
                            connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_all_rows_filters_and_uncertain_addresses(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.sqlite3"
            store = Store(path)
            store.initialize()
            for index in range(205):
                key = store.begin(str(index), None, "宁夏河北")
                store.complete(
                    key,
                    [],
                    {
                        "loading_location": "银川市-兴庆区",
                        "unloading_location": "唐山市-丰润区",
                    },
                    0.1,
                )
            key = store.begin("uncertain", None, "宁夏天津")
            store.complete(
                key,
                [],
                {
                    "loading_location": "银川市",
                    "unloading_location": "天津市",
                },
                0.1,
            )
            with store.db:
                store.db.execute(
                    "UPDATE freight SET review_fields=? WHERE capture_id=?",
                    ('["loading_location"]', key),
                )
            store.db.close()
            all_rows = atlas_snapshot(path)
            self.assertEqual(all_rows["total"], 206)
            self.assertEqual(all_rows["unlocated"], 1)
            self.assertIsNone(all_rows["records"][0]["origin"])
            self.assertEqual(atlas_snapshot(path, "宁夏河北")["total"], 205)
            with self.assertRaises(ValueError):
                atlas_snapshot(path, "invalid")
            self.assertEqual(atlas_snapshot(Path(folder) / "missing")["total"], 0)
