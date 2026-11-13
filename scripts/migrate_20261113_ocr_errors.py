"""Source-checked repair of four OCR-corrupted freight records."""

import json
import sqlite3
from pathlib import Path

from x001.storage import exclusive_run

DATABASE = Path("data/x001.sqlite3").resolve()
BACKUP = DATABASE.with_name("x001.before-ocr-fix-20261113.sqlite3")
REPAIRS = {
    21: {
        "fields": {
            "cargo": ("化肥复合肥60-70方卩", "化肥复合肥"),
            "weight": ("总重60-70吨", "总重60-70吨 60-70方 吨包"),
        },
        "evidence": "化肥复合肥总重量60·70吨",
        "review_before": set(),
        "review_after": set(),
    },
    29: {
        "fields": {
            "cargo": ("化肥衣亻弋丬士", "化肥"),
            "weight": ("总重35吨", "总重35吨 袋装"),
        },
        "evidence": "化肥总重量35吨袋装",
        "review_before": {"cargo"},
        "review_after": set(),
    },
    39: {
        "fields": {
            "cargo": ("地铺石pc砖托", "地铺石pc砖"),
            "weight": ("33-35吨", "33-35吨 托盘"),
            "unloading_location": (
                "亠石家庄市-藁城区-藁城北收费站(澳高速入口)",
                "石家庄市-藁城区-藁城北收费站",
            ),
        },
        "evidence": "货源反馈澳高速入口",
        "review_before": set(),
        "review_after": {"unloading_location"},
        "resume_fields": {
            "cargo": "地铺石pc砖托",
            "weight": "33-35吨",
            "unloading_location": "石家庄市-藁城区-藁城北收费站",
        },
        "resume_review": {"unloading_location"},
    },
    73: {
        "fields": {
            "cargo": ("镀锌压块压±找货《", "镀锌压块压块"),
        },
        "evidence": "镀锌压块总重量66吨压块",
        "review_before": set(),
        "review_after": set(),
    },
}


def main() -> None:
    if not DATABASE.is_file():
        raise FileNotFoundError(f"Database file does not exist: {DATABASE}")
    with exclusive_run(DATABASE):
        db = sqlite3.connect(DATABASE, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            pending = []
            for capture_id, repair in REPAIRS.items():
                row = db.execute(
                    "SELECT f.*, c.status FROM freight f JOIN captures c "
                    "ON c.id=f.capture_id WHERE f.capture_id=?",
                    (capture_id,),
                ).fetchone()
                if row is None or row["status"] != "done":
                    raise RuntimeError(f"Completed capture #{capture_id} is missing")

                raw = " ".join(
                    line[0] for page in json.loads(row["ocr_raw"]) for line in page
                )
                if repair["evidence"] not in raw:
                    raise RuntimeError(
                        f"OCR evidence changed for capture #{capture_id}"
                    )

                current = {key: row[key] for key in repair["fields"]}
                before = {key: values[0] for key, values in repair["fields"].items()}
                after = {key: values[1] for key, values in repair["fields"].items()}
                review = set(json.loads(row["review_fields"]))
                if current == after and review == repair["review_after"]:
                    continue
                accepted_states = [
                    (before, repair["review_before"]),
                    (
                        repair.get("resume_fields", {}),
                        repair.get("resume_review", set()),
                    ),
                ]
                if not any(
                    current == values and review == expected_review
                    for values, expected_review in accepted_states
                ):
                    raise RuntimeError(
                        f"Capture #{capture_id} no longer matches the expected "
                        "pre-repair values"
                    )
                pending.append((capture_id, after, repair["review_after"]))

            if not pending:
                print("OCR repairs already applied.")
                return

            if not BACKUP.exists():
                with sqlite3.connect(BACKUP) as destination:
                    db.backup(destination)

            db.execute("BEGIN IMMEDIATE")
            try:
                for capture_id, fields, review in pending:
                    assignments = ", ".join(f"{key}=?" for key in fields)
                    db.execute(
                        f"UPDATE freight SET {assignments}, review_fields=? "
                        "WHERE capture_id=?",
                        (
                            *fields.values(),
                            json.dumps(sorted(review), ensure_ascii=False),
                            capture_id,
                        ),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        finally:
            db.close()

    print(f"Backup: {BACKUP}")
    print(f"Repaired captures: {', '.join(str(item[0]) for item in pending)}")


if __name__ == "__main__":
    main()
