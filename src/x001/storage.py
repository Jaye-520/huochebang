"""保存截图任务、OCR 结果及中断恢复状态。"""

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from .vision import Line

DEFAULT_DATABASE = Path("data/x001.sqlite3")

FIELDS = (
    "price",
    "weight",
    "estimated_distance",
    "vehicle",
    "truck_count",
    "other_info",
    "unloading_location",
    "loading_location",
    "shipper",
    "cargo",
)
REPORT_FIELDS = {
    "shipper": "货主",
    "loading_location": "装货地",
    "unloading_location": "卸货地",
    "cargo": "货物",
    "weight": "货物重量",
    "estimated_distance": "预估里程",
    "vehicle": "车型要求",
    "truck_count": "需要车数",
    "price": "运价",
    "other_info": "其他信息",
}


@contextmanager
def exclusive_run(path: Path) -> Iterator[None]:
    """使用操作系统文件锁，进程退出时自动释放。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as lock:
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(f"Another collector is using {path}") from error
        yield


class Store:
    """为采集进程和识别进程提供独立的 SQLite 连接。"""

    def __init__(self, path: Path) -> None:
        self.root = path.parent
        self.captures = self.root / (
            "captures" if path.name == "x001.sqlite3" else f"{path.name}-captures"
        )
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def initialize(self) -> None:
        """创建表和索引，并补齐已有数据库缺少的列。"""
        self.db.execute("PRAGMA journal_mode = WAL")
        columns = ", ".join(f"{field} TEXT" for field in FIELDS)
        with self.db:
            self.db.executescript(f"""
                CREATE TABLE IF NOT EXISTS captures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    route TEXT,
                    card_key TEXT NOT NULL,
                    list_price TEXT,
                    screenshots TEXT,
                    image_number INTEGER,
                    status TEXT NOT NULL,
                    error TEXT,
                    ocr_seconds REAL
                );
                CREATE INDEX IF NOT EXISTS capture_status ON captures(status, id);
                CREATE TABLE IF NOT EXISTS image_sequence (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    next_number INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS freight (
                    capture_id INTEGER PRIMARY KEY REFERENCES captures(id),
                    ocr_raw TEXT NOT NULL,
                    review_fields TEXT NOT NULL DEFAULT '[]',
                    second_opinion TEXT,
                    {columns}
                );
            """)
            capture_columns = {
                row["name"] for row in self.db.execute("PRAGMA table_info(captures)")
            }
            if "route" not in capture_columns:
                self.db.execute("ALTER TABLE captures ADD COLUMN route TEXT")
            if "image_number" not in capture_columns:
                self.db.execute("ALTER TABLE captures ADD COLUMN image_number INTEGER")
            self.db.execute("""
                INSERT OR IGNORE INTO image_sequence(id, next_number)
                SELECT 1, coalesce(max(id), 0) + 1 FROM captures
            """)
            columns = {
                row["name"] for row in self.db.execute("PRAGMA table_info(freight)")
            }
            if "review_fields" not in columns:
                self.db.execute(
                    "ALTER TABLE freight ADD COLUMN "
                    "review_fields TEXT NOT NULL DEFAULT '[]'"
                )
            if "second_opinion" not in columns:
                self.db.execute("ALTER TABLE freight ADD COLUMN second_opinion TEXT")
            report_columns = []
            for key, label in REPORT_FIELDS.items():
                value = (
                    "coalesce(truck_count, '未显示')" if key == "truck_count" else key
                )
                report_columns.append(
                    f"""CASE WHEN instr(review_fields, '"{key}"') > 0
                         THEN coalesce({key} || '（待核对）', '待核对')
                         ELSE {value}
                    END AS {label}"""
                )
            self.db.execute(f"""
                CREATE VIEW IF NOT EXISTS freight_report AS
                SELECT row_number() OVER (ORDER BY capture_id) AS 序号,
                       {", ".join(report_columns)}
                FROM freight
                """)

    def recover(self) -> None:
        """恢复已保存的识别任务，标记未完成的采集。"""
        with self.db:
            interrupted = self.db.execute(
                "SELECT id FROM captures WHERE status='capturing'"
            ).fetchall()
            for row in interrupted:
                capture_id = row[0]
                paths = sorted(self.folder(capture_id).glob("*.png"))
                self.publish(capture_id, paths, "Capture interrupted")
            self.db.execute("""
                UPDATE captures SET status='queued', error=NULL
                WHERE status IN ('processing', 'ocr_failed')
                  AND screenshots IS NOT NULL
            """)

    def active_count(self) -> int:
        return self.db.execute("""
            SELECT count(*) FROM captures
            WHERE status IN ('capturing','queued','processing')
        """).fetchone()[0]

    def requeue_completed(self) -> None:
        """重新识别已保存的截图，成功时才覆盖上一版结果。"""
        with self.db:
            self.db.execute("""
                UPDATE captures SET status='queued', error=NULL
                WHERE status='done' AND screenshots IS NOT NULL
                """)

    def begin(self, key: str, price: str | None, route: str | None = None) -> int:
        with self.db:
            image_number = self.db.execute("""
                UPDATE image_sequence SET next_number = next_number + 1
                WHERE id = 1 RETURNING next_number - 1
            """).fetchone()[0]
            return self.db.execute(
                """
                INSERT INTO captures(card_key, list_price, route, image_number, status)
                VALUES (?, ?, ?, ?, 'capturing')
            """,
                (key, price, route, image_number),
            ).lastrowid

    def folder(self, capture_id: int) -> Path:
        row = self.db.execute(
            "SELECT image_number FROM captures WHERE id=?", (capture_id,)
        ).fetchone()
        number = row[0] if row and row[0] else capture_id
        return self.captures / str(number)

    def publish(
        self, capture_id: int, paths: list[Path], error: str | None = None
    ) -> None:
        relative = [str(path.relative_to(self.root)) for path in paths]
        status = "capture_failed" if error is not None else "queued"
        with self.db:
            self.db.execute(
                """
                UPDATE captures SET screenshots=?, status=?, error=? WHERE id=?
            """,
                (json.dumps(relative), status, error, capture_id),
            )

    def discard_capture(self, capture_id: int, paths: list[Path]) -> None:
        """丢弃尚未提交识别队列的临时采集。"""
        row = self.db.execute(
            "SELECT status FROM captures WHERE id=?", (capture_id,)
        ).fetchone()
        if row is None or row["status"] != "capturing":
            raise RuntimeError(f"Capture {capture_id} is not an unpublished capture")

        directory = self.folder(capture_id)
        if any(path.parent != directory for path in paths):
            raise ValueError("Capture screenshots must be inside its capture folder")
        for path in paths:
            path.unlink(missing_ok=True)
        if directory.exists():
            directory.rmdir()

        with self.db:
            self.db.execute("DELETE FROM freight WHERE capture_id=?", (capture_id,))
            deleted = self.db.execute(
                "DELETE FROM captures WHERE id=? AND status='capturing'",
                (capture_id,),
            ).rowcount
            if deleted != 1:
                raise RuntimeError(f"Capture {capture_id} could not be discarded")

    def take(self) -> sqlite3.Row | None:
        with self.db:
            return self.db.execute("""
                UPDATE captures SET status='processing'
                WHERE id=(SELECT id FROM captures
                          WHERE status='queued' ORDER BY id LIMIT 1)
                RETURNING *
            """).fetchone()

    def complete(
        self,
        capture_id: int,
        raw: list[list[Line]],
        fields: Mapping[str, str],
        seconds: float,
        review_fields: Iterable[str] = (),
        second_opinion: Mapping[str, object] | None = None,
    ) -> None:
        """在同一事务中写入货源结果并完成对应的识别任务。"""
        columns = (
            "capture_id",
            "ocr_raw",
            "review_fields",
            "second_opinion",
            *FIELDS,
        )
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{key}=excluded.{key}" for key in columns[1:])
        with self.db:
            self.db.execute(
                f"""
                INSERT INTO freight ({", ".join(columns)}) VALUES ({placeholders})
                ON CONFLICT(capture_id) DO UPDATE SET {updates}
            """,
                (
                    capture_id,
                    json.dumps(raw, ensure_ascii=False),
                    json.dumps(sorted(review_fields)),
                    (
                        json.dumps(second_opinion, ensure_ascii=False)
                        if second_opinion is not None
                        else None
                    ),
                    *(fields.get(key) for key in FIELDS),
                ),
            )
            self.db.execute(
                """
                UPDATE captures SET status='done', error=NULL, ocr_seconds=? WHERE id=?
            """,
                (seconds, capture_id),
            )

    def ocr_failed(self, capture_id: int, error: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE captures SET status='ocr_failed', error=? WHERE id=?",
                (error, capture_id),
            )
