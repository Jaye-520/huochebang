import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from x001.filters import FreightFilters


class TimeFilterTests(unittest.TestCase):
    def test_local_periods_match_utc_database_timestamps(self):
        local = timezone(timedelta(hours=8))
        now = datetime(2026, 11, 12, 20, 0, tzinfo=local)
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE captures (id INTEGER, captured_at TEXT)")
        db.executemany(
            "INSERT INTO captures VALUES (?, ?)",
            [
                (1, "2026-11-12 05:00:00"),
                (2, "2026-11-11 15:59:59"),
                (3, "2026-11-05 16:00:00"),
                (4, "2026-11-05 11:59:59"),
            ],
        )
        try:
            for filters, expected in (
                (FreightFilters(period="today"), [1]),
                (FreightFilters(period="yesterday"), [2]),
                (FreightFilters(period="7d"), [1, 2, 3]),
                (FreightFilters(period="day", day="2026-11-11"), [2]),
            ):
                where, params = filters.sql(now=now)
                rows = db.execute(
                    f"SELECT c.id FROM captures c WHERE {where} ORDER BY c.id", params
                ).fetchall()
                self.assertEqual([row[0] for row in rows], expected)
            self.assertEqual(
                FreightFilters(period="yesterday").sql(now=now)[1],
                ["2026-11-10 16:00:00", "2026-11-11 16:00:00"],
            )
            self.assertEqual(
                FreightFilters(period="today").sql(now=now)[1],
                ["2026-11-11 16:00:00", "2026-11-12 16:00:00"],
            )
            self.assertEqual(
                FreightFilters(period="7d").sql(now=now)[1],
                ["2026-11-05 12:00:00", "2026-11-12 12:00:00"],
            )
            with self.assertRaisesRegex(ValueError, "有效的采集日期"):
                FreightFilters(period="day", day="2026-11-32").sql(now=now)
        finally:
            db.close()
