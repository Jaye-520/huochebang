"""One-time, source-checked cleanup of the initial X001 freight database."""

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from x001.freight import clean_freight
from x001.storage import exclusive_run

DATABASE = Path("data/x001.sqlite3").resolve()
BACKUP = DATABASE.with_name("x001.before-text-audit-20261112.sqlite3")
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
REGION_TERMS = {
    "宁夏": (
        "银川",
        "吴忠",
        "石嘴山",
        "中卫",
        "固原",
        "青铜峡",
        "宁夏",
        "花马池",
        "红果子",
    ),
    "河北": (
        "石家庄",
        "唐山",
        "邯郸",
        "保定",
        "张家口",
        "沧州",
        "衡水",
        "廊坊",
        "河北",
    ),
    "天津": ("天津", "静海", "塘沽", "北港"),
}


def main() -> None:
    if not DATABASE.is_file():
        raise FileNotFoundError(f"Database file does not exist: {DATABASE}")
    with exclusive_run(DATABASE):
        with sqlite3.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row
            old = db.execute(
                "SELECT f.*, c.card_key, c.route FROM freight f JOIN captures c "
                "ON c.id=f.capture_id WHERE f.capture_id<41 ORDER BY f.capture_id"
            ).fetchall()
            if len(old) != 38 or old[-1]["capture_id"] != 39:
                raise RuntimeError(
                    "Legacy record selection changed; inspect before migrating"
                )
            if not old[-1]["vehicle"].startswith("整车"):
                raise RuntimeError("Legacy cleanup already applied")
            if not BACKUP.exists():
                with sqlite3.connect(BACKUP) as destination:
                    db.backup(destination)
            edits = Counter()
            with db:
                for row in old:
                    fields = {key: row[key] for key in FIELDS if row[key] is not None}
                    before = fields.copy()
                    review = set(json.loads(row["review_fields"]))
                    clean_freight(
                        json.loads(row["ocr_raw"]), fields, review, row["card_key"]
                    )
                    changes = {
                        key: value
                        for key, value in fields.items()
                        if before.get(key) != value
                    }
                    edits.update(changes.keys())
                    review_json = json.dumps(sorted(review), ensure_ascii=False)
                    if review_json != row["review_fields"]:
                        changes["review_fields"] = review_json
                    if changes:
                        update(db, row["capture_id"], changes)
                    if row["route"] is None:
                        origin = region(fields.get("loading_location", ""))
                        destination = region(fields.get("unloading_location", ""))
                        if origin and destination and origin != destination:
                            db.execute(
                                "UPDATE captures SET route=? WHERE id=?",
                                (origin + destination, row["capture_id"]),
                            )

                targeted = {
                    82: ({"unloading_location": "天津市-东丽区"}, {"loading_location"}),
                    87: (
                        {"loading_location": "青铜峡", "unloading_location": "静海镇"},
                        {"loading_location", "unloading_location"},
                    ),
                    64: ({}, set()),
                    58: ({}, set()),
                    45: ({}, set()),
                    46: ({}, {"other_info", "unloading_location"}),
                    79: ({}, {"price"}),
                    85: ({}, {"price"}),
                    73: ({}, {"cargo", "loading_location"}),
                    65: ({}, {"cargo"}),
                    70: ({}, {"price"}),
                    71: ({}, {"loading_location"}),
                    66: ({}, {"loading_location"}),
                    50: ({}, {"unloading_location"}),
                    27: ({}, {"other_info"}),
                    26: ({}, {"other_info"}),
                }
                for capture_id, (changes, flagged) in targeted.items():
                    row = db.execute(
                        "SELECT * FROM freight WHERE capture_id=?", (capture_id,)
                    ).fetchone()
                    if row is None:
                        raise RuntimeError(f"Missing capture {capture_id}")
                    changes = changes.copy()
                    if capture_id == 58:
                        note = row["other_info"]
                        rating = re.search(r"；结算[^；]+$", note)
                        changes["other_info"] = note.split("以上信息由系统", 1)[0]
                        if rating:
                            changes["other_info"] += rating[0]
                    elif capture_id == 45:
                        changes["other_info"] = row["other_info"].replace(";；", "；")
                    elif capture_id == 64:
                        changes["other_info"] = row["other_info"].replace("。；", "；")
                    elif capture_id == 46:
                        changes["other_info"] = row["other_info"].replace(
                            "明天08:00", "明天8-17点装货", 1
                        )
                    review = set(json.loads(row["review_fields"])) | flagged
                    changes["review_fields"] = json.dumps(
                        sorted(review), ensure_ascii=False
                    )
                    changes = {
                        key: value
                        for key, value in changes.items()
                        if row[key] != value
                    }
                    if changes:
                        update(db, capture_id, changes)
    print(f"Backup: {BACKUP}")
    print(f"Legacy records cleaned: {len(old)}")
    print(f"Legacy changed fields: {dict(edits)}")
    print(f"Targeted records reviewed: {len(targeted)}")


def update(db: sqlite3.Connection, capture_id: int, changes: dict) -> None:
    assignments = ", ".join(f"{key}=?" for key in changes)
    db.execute(
        f"UPDATE freight SET {assignments} WHERE capture_id=?",
        (*changes.values(), capture_id),
    )


def region(address: str) -> str:
    matches = [
        name
        for name, terms in REGION_TERMS.items()
        if any(term in address for term in terms)
    ]
    return matches[0] if len(matches) == 1 else ""


if __name__ == "__main__":
    main()
