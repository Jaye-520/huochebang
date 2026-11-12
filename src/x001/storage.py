"""保存截图任务、OCR 结果及中断恢复状态。"""

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from .vision import Line

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
                    card_key TEXT NOT NULL,
                    list_price TEXT,
                    screenshots TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    ocr_seconds REAL
                );
                CREATE INDEX IF NOT EXISTS capture_status ON captures(status, id);
                CREATE TABLE IF NOT EXISTS freight (
                    capture_id INTEGER PRIMARY KEY REFERENCES captures(id),
                    ocr_raw TEXT NOT NULL,
                    review_fields TEXT NOT NULL DEFAULT '[]',
                    {columns}
                );
            """)
            columns = {
                row["name"] for row in self.db.execute("PRAGMA table_info(freight)")
            }
            if "review_fields" not in columns:
                self.db.execute(
                    "ALTER TABLE freight ADD COLUMN "
                    "review_fields TEXT NOT NULL DEFAULT '[]'"
                )

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
            """)

    def active_count(self) -> int:
        return self.db.execute("""
            SELECT count(*) FROM captures
            WHERE status IN ('capturing','queued','processing')
        """).fetchone()[0]

    def begin(self, key: str, price: str | None) -> int:
        with self.db:
            return self.db.execute(
                """
                INSERT INTO captures(card_key, list_price, status)
                VALUES (?, ?, 'capturing')
            """,
                (key, price),
            ).lastrowid

    def folder(self, capture_id: int) -> Path:
        return self.captures / str(capture_id)

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
    ) -> None:
        """在同一事务中写入货源结果并完成对应的识别任务。"""
        columns = ("capture_id", "ocr_raw", "review_fields", *FIELDS)
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
