"""导出指定线路的货源核对表。"""

import argparse
import csv
import json
import sqlite3
from pathlib import Path
from tempfile import NamedTemporaryFile

import xlsxwriter

from .filters import FreightFilters
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


def _load_rows(
    database: Path,
    output: Path,
    route: str,
    filters: FreightFilters | None = None,
) -> list[sqlite3.Row]:
    if not database.is_file():
        raise FileNotFoundError(f"Database file does not exist: {database}")
    if filters is None and not route.strip():
        raise ValueError("Route must not be empty")
    if output.resolve() == database.resolve():
        raise ValueError("Export output path must not overwrite the database")

    columns = ", ".join(f"f.{field}" for field in FIELDS)
    where, params = (filters or FreightFilters(route=route)).sql()
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
            WHERE c.status = 'done' AND {where}
            ORDER BY c.id
            """,
            params,
        ).fetchall()
    finally:
        store.db.close()

    if not rows:
        if filters is not None:
            raise ValueError("当前筛选条件下没有已识别的货源可导出")
        raise ValueError(f"No completed records found for route {route!r}")
    return rows


def export_report(database: Path, output: Path, route: str) -> int:
    """导出指定线路已完成的 CSV 记录，并返回导出条数。"""
    rows = _load_rows(database, output, route)

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


_WORKBOOK_FIELDS = (
    ("货主", "shipper"),
    ("装货地", "loading_location"),
    ("卸货地", "unloading_location"),
    ("货物", "cargo"),
    ("货物重量", "weight"),
    ("预计里程", "estimated_distance"),
    ("车型要求", "vehicle"),
    ("需要车数", "truck_count"),
    ("运价", "price"),
    ("其他信息", "other_info"),
)


def export_workbook(
    database: Path,
    output: Path,
    route: str,
    *,
    search: str = "",
    vehicle: str = "",
    trucks: str = "",
    price: str = "",
    length: str = "",
    period: str = "",
    day: str = "",
) -> int:
    """按参考表的紧凑布局导出指定线路的 Excel 货源表。"""
    rows = _load_rows(
        database,
        output,
        route,
        FreightFilters(route, search, vehicle, trucks, price, length, period, day),
    )[::-1]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".xlsx",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        workbook = xlsxwriter.Workbook(
            str(temporary_path),
            {"strings_to_formulas": False, "strings_to_urls": False},
        )
        try:
            sheet = workbook.add_worksheet(f"{route[:2]}装货" if route else "全部线路")
            sheet.freeze_panes(2, 0)
            sheet.set_landscape()
            sheet.fit_to_pages(1, 0)
            sheet.repeat_rows(0, 1)
            sheet.set_default_row(28)
            widths = (6, 11, 39, 35, 18, 18, 12, 24, 13, 20, 43)
            for col, width in enumerate(widths):
                sheet.set_column(col, col, width)

            title = workbook.add_format(
                {
                    "bold": True,
                    "font_name": "Microsoft YaHei",
                    "font_size": 14,
                    "font_color": "#1F2933",
                    "valign": "vcenter",
                }
            )
            header = workbook.add_format(
                {
                    "bold": True,
                    "font_name": "Microsoft YaHei",
                    "font_size": 10,
                    "font_color": "#FFFFFF",
                    "bg_color": "#4472C4",
                    "valign": "vcenter",
                    "align": "center",
                    "text_wrap": True,
                }
            )
            center = workbook.add_format(
                {
                    "font_name": "Microsoft YaHei",
                    "font_size": 10,
                    "font_color": "#202B33",
                    "align": "center",
                    "valign": "vcenter",
                    "text_wrap": True,
                }
            )
            left = workbook.add_format(
                {
                    "font_name": "Microsoft YaHei",
                    "font_size": 10,
                    "font_color": "#202B33",
                    "align": "left",
                    "valign": "vcenter",
                    "text_wrap": True,
                }
            )
            flagged = workbook.add_format(
                {
                    "font_name": "Microsoft YaHei",
                    "font_size": 10,
                    "font_color": "#B36A1C",
                    "align": "center",
                    "valign": "vcenter",
                }
            )
            title_text = (
                f"{route[:2]} → {route[2:]}货源（{len(rows)}条）"
                if route
                else f"全部线路货源（{len(rows)}条）"
            )
            sheet.merge_range(0, 0, 0, 10, title_text, title)
            sheet.set_row(0, 29)
            sheet.set_row(1, 28)
            headers = ("序号", *(name for name, _ in _WORKBOOK_FIELDS))
            for col, name in enumerate(headers):
                sheet.write_string(1, col, name, header)

            for index, row in enumerate(rows):
                excel_row = index + 2
                review = _review_items(row)
                sheet.write_number(
                    excel_row, 0, index + 1, flagged if review else center
                )
                if review:
                    sheet.write_comment(
                        excel_row,
                        0,
                        f"采集编号 #{row['capture_id']}\n待核对：{review}",
                        {"author": "X001"},
                    )
                for col, (_, field) in enumerate(_WORKBOOK_FIELDS, start=1):
                    value = row[field]
                    if field == "truck_count" and not value:
                        value = "未显示"
                    sheet.write_string(
                        excel_row,
                        col,
                        value or "",
                        left
                        if field
                        in ("loading_location", "unloading_location", "other_info")
                        else center,
                    )
            sheet.autofilter(1, 0, len(rows) + 1, 10)
            sheet.print_area(0, 0, len(rows) + 1, 10)
        finally:
            workbook.close()
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
