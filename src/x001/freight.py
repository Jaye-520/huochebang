"""从有序详情截图提取字段，并处理浮层遮挡。"""

import re

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from .engines import PaddleEngine, WindowsOCR
from .vision import Line, normalize, rows

_PRICE = re.compile(r"\d+(?:\.\d+)?元(?:[/／](?:吨|趟|车))?")
_WEIGHT = re.compile(r"\d+(?:\.\d+)?(?:[-–~至、]\d+(?:\.\d+)?)?吨")
_COUNT = re.compile(r"(?:还需|需要车数[:：]?)?(\d+)辆")
_BLOCK = re.compile(r"^(?:车辆|货物|需要车数|运费|订金|净得运费|实付技术服务费)")


def detail_confirmed(lines: list[Line]) -> bool:
    """用详情特有的标签确认当前页面。"""
    text = normalize("".join(line[0] for line in lines))
    return "预估里程" in text and (
        "货主要求" in text or "车辆" in text or "装货" in text
    )


def detail_control(lines: list[Line]) -> tuple[str, Line] | None:
    """查找允许处理的变更提示或货主要求展开入口。"""
    text = normalize("".join(rows(lines)))
    if "货源以下信息有变更" in text:
        return next((("notice", line) for line in lines if "我知道了" in line[0]), None)
    notes = [line[2] for line in lines if "货主要求" in line[0]]
    vehicles = [line[2] for line in lines if line[0] == "车辆"]
    if notes:
        return next(
            (
                ("notes", line)
                for line in lines
                if line[0].startswith("展开")
                and min(notes) < line[2] < min(vehicles, default=min(notes) + 360)
                and line[1] < 340
            ),
            None,
        )
    return None


def recognize_detail(
    engine: WindowsOCR | PaddleEngine, image: Image.Image, *, first: bool
) -> list[Line]:
    if not isinstance(engine, WindowsOCR):
        return engine.locate(image)
    # 加深彩色文字和浅灰标签，保留白底。
    pixels = np.asarray(image)
    gray = np.clip(pixels.min(axis=2).astype(np.float32) * 1.55 - 125, 0, 255)
    prepared = Image.fromarray(gray.astype(np.uint8)).convert("RGB")
    lines = engine.locate(prepared)
    if first or any("货源反馈" in line[0] for line in lines):
        # 彩色装卸标记容易被误读为数字，结合颜色定位地址行。
        strip = pixels[
            : image.height // 2, image.width * 7 // 100 : image.width * 11 // 100
        ]
        red = (strip[:, :, 0] > 175) & (strip[:, :, 1] < 130)
        blue = (strip[:, :, 0] < 100) & (strip[:, :, 2] > 170)
        black = strip.max(axis=2) < 45
        badges = []
        for pickup, dropoff in ((blue, red), (red, black)):
            for first_top, first_bottom in _badge_bounds(pickup):
                for second_top, second_bottom in _badge_bounds(dropoff):
                    if 75 <= second_top - first_top <= 420:
                        badges = [
                            ("装", first_top, first_bottom),
                            ("卸", second_top, second_bottom),
                        ]
                        break
                if badges:
                    break
            if badges:
                break
        if len(badges) == 2:
            left = image.width * 13 // 100
            for label, top, bottom in badges:
                next_top = (
                    badges[1][1]
                    if label == "装"
                    else min(
                        (
                            line[2]
                            for line in lines
                            if line[2] > bottom
                            and ("km" in line[0] or "里程" in line[0])
                        ),
                        default=bottom + 95,
                    )
                )
                end = min(bottom + 90, next_top - 10)
                if end <= top + 20:
                    continue
                address = engine.locate(
                    prepared.crop((left, top - 6, image.width, end))
                )
                if address:
                    value = "".join(
                        row
                        for row in rows(address)
                        if not any(word in row for word in ("限行情况", "补充车辆"))
                    )
                    if not value:
                        continue
                    lines = [
                        line
                        for line in lines
                        if not (top - 8 <= line[2] < end) or "货源反馈" in line[0]
                    ]
                    lines.append(
                        (
                            label + ":" + value,
                            left,
                            top,
                            left + max(line[3] for line in address),
                            top - 6 + max(line[4] for line in address),
                        )
                    )
    return lines


def _badge_bounds(mask: NDArray[np.bool_]) -> list[tuple[int, int]]:
    ys = np.flatnonzero(mask.mean(axis=1) > 0.30)
    return [
        (int(group[0]), int(group[-1]))
        for group in np.split(ys, np.flatnonzero(np.diff(ys) > 13) + 1)
        if len(group) >= 18 and 28 <= group[-1] - group[0] <= 65
    ]


def extract_fields(
    pages: list[list[Line]], list_price: str | None, card_key: str = ""
) -> dict[str, str]:
    fields = {}
    displayed_total = None
    money_with_unit = set()
    negotiable = False
    for index, lines in enumerate(pages):
        page_rows = rows(lines)
        compact = [normalize(row) for row in page_rows]
        for number, text in enumerate(compact):
            text = text.replace("货源反馈", "")
            if "请先与货主协商" in text:
                negotiable = True
            if match := re.search(r"[¥￥](\d+(?:\.\d+)?)\(运费\)", text):
                displayed_total = match[1] + "元"
            money_with_unit.update(re.findall(r"\d+(?:\.\d+)?元/(?:吨|趟|车)", text))
            if re.match(r"^[装卸]:", text) or (
                index == 0 and re.match(r"^(装|卸)(?=\S)", text)
            ):
                label, value = text[0], text[1:]
                if value.startswith(":"):
                    key = "loading_location" if label == "装" else "unloading_location"
                    fields.setdefault(key, value[1:])
                elif not value.startswith(("货", "卸", "车")) and re.search(
                    r"[市县区旗州]", value
                ):
                    if number + 1 < len(compact):
                        follow = compact[number + 1]
                        if re.match(r"^[\u4e00-\u9fff]+$", follow) and len(follow) < 12:
                            if not follow.startswith(("装", "卸", "距", "去", "查看")):
                                value += follow
                    key = "loading_location" if label == "装" else "unloading_location"
                    fields.setdefault(key, value)
            if "预估里程" in text:
                # 左列是司机到装货地的距离，预估里程对应右列。
                labels = [line for line in lines if "预估里程" in line[0]]
                for _, x, y, _, _ in labels:
                    candidates = [
                        (y - top, match[0])
                        for value, left, top, _, _ in lines
                        if x - 45 <= left and 0 <= y - top <= 140
                        if (
                            match := re.search(
                                r"\d+(?:\.\d+)?km", normalize(value), re.I
                            )
                        )
                    ]
                    if candidates:
                        fields.setdefault("estimated_distance", min(candidates)[1])
            if all(label in text for label in ("结算", "爽约", "评价")):
                rating = next((line for line in lines if "结算" in line[0]), None)
                if rating:
                    _, x, y, _, _ = rating
                    names = [
                        (y - bottom, value)
                        for value, left, _, _, bottom in lines
                        if abs(left - x) < 35
                        and 4 < y - bottom < 80
                        and re.fullmatch(r"[\w·*＊]{2,30}", value)
                    ]
                    if names:
                        fields.setdefault("shipper", min(names)[1])
            if text.startswith("车辆") and len(text) > 2:
                value = text[2:]
                if value.endswith("/") and number + 1 < len(compact):
                    extra = compact[number + 1]
                    if extra in ("飞翼车", "平板", "高栏", "高低板", "厢式", "冷藏"):
                        value += extra
                if value.startswith(fields.get("vehicle", "")):
                    fields["vehicle"] = value
            has_cargo_label = text.startswith("货物")
            below_vehicle = any(
                item.startswith("车辆") for item in compact[max(0, number - 3) : number]
            )
            if has_cargo_label or (
                below_vehicle and _WEIGHT.search(text) and "元" not in text
            ):
                value = text.removeprefix("货物")
                if match := _WEIGHT.search(value):
                    fields.setdefault("weight", match[0].replace("、", "-"))
                    cargo = (
                        value[: match.start()].removesuffix("总重量")
                        + value[match.end() :]
                    )
                    if cargo:
                        fields.setdefault("cargo", re.sub(r",+", ",", cargo).strip(","))
                else:
                    fields.setdefault("cargo", value)
            if text.startswith("需要车数"):
                if match := _COUNT.search(text):
                    fields.setdefault("truck_count", match[1])
                elif "需多车" in text:
                    fields.setdefault("truck_count", "需多车")
            if text.startswith(("运费", "总运费", "运输价格")) and (
                match := _PRICE.search(text)
            ):
                fields.setdefault("price", match[0])
            if text.startswith("货主要求"):
                note = [
                    text.removeprefix("货主要求").lstrip(":").removesuffix("问货主")
                ]
                for extra in compact[number + 1 :]:
                    if _BLOCK.match(extra) or extra.startswith(("接单前", "查看详情")):
                        break
                    if not extra.startswith(("问货主", "货源反馈", "展开", "收起")):
                        note.append(
                            extra.removesuffix("问货主").replace("货源反馈", "")
                        )
                combined = "".join(note)
                if len(combined) > len(fields.get("other_info", "")):
                    fields["other_info"] = combined
    if displayed_total:
        units = [
            price for price in money_with_unit if price.startswith(displayed_total)
        ]
        fields.setdefault("price", units[0] if len(units) == 1 else displayed_total)
    if negotiable:
        fields.setdefault("price", "电议")
    elif list_price:
        fields.setdefault("price", list_price)
    # 列表确认同一组数字为区间时，才修正详情中的小数点误读。
    weight = fields.get("weight", "")
    if re.fullmatch(r"\d+\.\d+吨", weight):
        listed_ranges = re.findall(r"\d+(?:\.\d+)?-\d+(?:\.\d+)?吨", card_key)
        corrected = weight.replace(".", "-")
        if corrected in listed_ranges:
            fields["weight"] = corrected
    return fields


_FIELD_LABELS = {
    "装:": ("loading_location",),
    "卸:": ("unloading_location",),
    "车辆": ("vehicle",),
    "货物": ("cargo", "weight"),
    "需要车数": ("truck_count",),
    "货主要求": ("other_info",),
    "总运费": ("price",),
    "运费": ("price",),
}


def feedback_blocked(lines: list[Line], width: int) -> set[str]:
    """找出预期内容与「货源反馈」浮层重叠的字段。"""
    feedback = next(
        (line for line in lines if "货源反馈" in line[0] and line[3] >= width * 0.9),
        None,
    )
    if not feedback:
        return set()
    _, left, top, right, bottom = feedback
    edge = max(left, right - width * 0.14) - width * 0.08
    margin = width * 0.025
    fields = set()
    text_lines = [line for line in lines if "货源反馈" not in line[0]]
    for label, keys in _FIELD_LABELS.items():
        for text, x, y, x2, y2 in text_lines:
            if not normalize(text).startswith(label):
                continue
            if label == "货主要求":
                # 货主要求可能换行，范围截止于后续字段标签。
                stop = min(
                    (
                        line[2]
                        for line in text_lines
                        if line[2] > y and _BLOCK.match(normalize(line[0]))
                    ),
                    default=y2 + 200,
                )
                candidates = [
                    line
                    for line in text_lines
                    if y <= line[2] < stop and line[1] < width * 0.65
                ]
            else:
                candidates = [(text, x, y, x2, y2)]
            for _, _, row_top, value_right, row_bottom in candidates:
                if row_top >= bottom + margin or row_bottom <= top - margin:
                    continue
                if label in ("车辆", "货物", "需要车数", "总运费", "运费"):
                    # 这些字段值右对齐，即使被挡到只剩左侧标签也要标记。
                    fields.update(keys)
                elif value_right >= edge - width * 0.07:
                    fields.update(keys)
    return fields


def resolve_feedback(
    pages: list[list[Line]], widths: list[int], list_price: str | None, card_key: str
) -> tuple[dict[str, str], set[str]]:
    fields = extract_fields(pages, list_price, card_key)
    blocked = [
        feedback_blocked(lines, width)
        for lines, width in zip(pages, widths, strict=True)
    ]
    affected = set().union(*blocked)
    if not affected:
        return fields, set()
    observations = [extract_fields([lines], None, card_key) for lines in pages]
    review = set()
    for key in affected:
        candidates = [
            extracted[key]
            for extracted, hidden in zip(observations, blocked, strict=True)
            if key not in hidden and key in extracted
        ]
        if candidates:
            fields[key] = max(candidates, key=len)
        else:
            fields.pop(key, None)
            review.add(key)
    return fields, review
