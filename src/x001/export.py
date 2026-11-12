"""导出指定线路的货源核对表。"""

import argparse
import csv
import json
import sqlite3
from pathlib import Path
from tempfile import NamedTemporaryFile

from .storage import DEFAULT_DATABASE, FIELDS, REPORT_FIELDS, Store

_REQUIRED_FIELDS = (
    "price",
    "weight",
    "estimated_distance",
    "vehicle",
    "unloading_location",
    "loading_location",
    "shipper",
    "cargo",
)


def _review_items(row: sqlite3.Row) -> str:
    stored = json.loads(row["review_fields"])
    if not isinstance(stored, list) or not all(
        isinstance(field, str) for field in stored
    ):
        raise ValueError(f"Invalid review_fields for capture #{row['capture_id']}")

    second_opinion = {}
    second_targets = set()
    second_opinion_error = False
    if row["second_opinion"] is not None:
        stored_opinion = json.loads(row["second_opinion"])
        if not isinstance(stored_opinion, dict):
            raise ValueError(f"Invalid second_opinion for capture #{row['capture_id']}")
        stored_error = stored_opinion.get("error")
        if stored_error is not None and not isinstance(stored_error, str):
            raise ValueError(
                f"Invalid second_opinion error for capture #{row['capture_id']}"
            )
        second_opinion_error = bool(stored_error)
        stored_targets = stored_opinion.get("target_fields", [])
        if not isinstance(stored_targets, list) or not all(
            isinstance(field, str) for field in stored_targets
        ):
            raise ValueError(
                f"Invalid second_opinion targets for capture #{row['capture_id']}"
            )
        second_targets = set(stored_targets)
        stored_fields = stored_opinion.get("fields", {})
        if not isinstance(stored_fields, dict) or not all(
            isinstance(field, str) and isinstance(value, str)
            for field, value in stored_fields.items()
        ):
            raise ValueError(
                f"Invalid second_opinion fields for capture #{row['capture_id']}"
            )
        second_opinion = stored_fields

    fields = set(stored)
    fields.update(field for field in _REQUIRED_FIELDS if not (row[field] or "").strip())
    labels = []
    for field, label in REPORT_FIELDS.items():
        if field not in fields:
            continue
        suggestion = second_opinion.get(field)
        if suggestion:
            label += f"（PP-OCRv5 二次意见：{suggestion}；未自动采用）"
        elif field in second_targets:
            if second_opinion_error:
                label += "（PP-OCRv5 二次识别失败；未自动采用）"
            else:
                label += "（PP-OCRv5 未识别；仍需人工核对）"
        labels.append(label)
    labels.extend(sorted(fields - REPORT_FIELDS.keys()))
    return "、".join(labels)


def _csv_cell(value: str | int | None) -> str | int:
    if isinstance(value, str) and value.lstrip(" \t\r\n")[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value or ""


def export_report(database: Path, output: Path, route: str) -> int:
    """导出指定线路已完成的记录，并返回导出条数。"""
    if not database.is_file():
        raise FileNotFoundError(f"Database file does not exist: {database}")
    if not route.strip():
        raise ValueError("Route must not be empty")
    if output.resolve() == database.resolve():
        raise ValueError("CSV output path must not overwrite the database")

    columns = ", ".join(f"f.{field}" for field in FIELDS)
    store = Store(database)
    try:
        store.initialize()
        db = store.db
        rows = db.execute(
            f"""
            SELECT c.id AS capture_id, c.captured_at, c.route, f.review_fields,
                   f.second_opinion,
                   {columns}
            FROM captures AS c
            JOIN freight AS f ON f.capture_id = c.id
            WHERE c.status = 'done' AND c.route = ?
            ORDER BY c.id
            """,
            (route,),
        ).fetchall()
    finally:
        store.db.close()

    if not rows:
        raise ValueError(f"No completed records found for route {route!r}")

    headers = (
        "序号",
        "线路",
        "采集编号",
        "采集时间",
        *REPORT_FIELDS.values(),
        "待核对项",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            writer = csv.writer(temporary)
            writer.writerow(headers)
            for number, row in enumerate(rows, start=1):
                values = {field: row[field] for field in REPORT_FIELDS}
                if values["truck_count"] is None:
                    values["truck_count"] = "未显示"
                writer.writerow(
                    (
                        *(
                            _csv_cell(value)
                            for value in (
                                number,
                                row["route"],
                                row["capture_id"],
                                row["captured_at"],
                                *(values[field] for field in REPORT_FIELDS),
                                _review_items(row),
                            )
                        ),
                    )
                )
        temporary_path.replace(output)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="导出指定线路的货源核对表")
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="SQLite 文件路径（默认 %(default)s）",
    )
    parser.add_argument("--route", required=True, help="手机线路页识别出的线路文字")
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV 输出路径（默认与数据库同目录）",
    )
    args = parser.parse_args()
    output = args.output or args.database.with_name(f"{args.database.stem}-freight.csv")

    try:
        count = export_report(args.database.resolve(), output.resolve(), args.route)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f"导出失败：{error}\n")
    print(f"已导出 {count} 条货源到 {output}")


if __name__ == "__main__":
    main()
