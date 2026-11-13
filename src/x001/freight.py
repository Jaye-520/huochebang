"""从有序详情截图提取字段，并处理浮层遮挡。"""

import re
from decimal import Decimal

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageOps

from .engines import HybridEngine, PaddleEngine, RapidEngine, WindowsOCR
from .vision import Line, normalize, rows

_PRICE = re.compile(r"\d+(?:\.\d+)?元(?:[/／](?:吨|趟|车))?")
_WEIGHT = re.compile(r"\d+(?:\.\d+)?(?:[-–~至、]\d+(?:\.\d+)?)?吨")
_COUNT = re.compile(r"(?:还需|需要车数[:：]?)?(\d+)辆")
_BLOCK = re.compile(r"^(?:车辆|货物|需要车数|运费|订金|净得运费|实付技术服务费)")
_PACKAGING = re.compile(r"[,(]?(吨袋|吨包|袋装|散装|裸装|捆扎|纸箱|筐装|托盘|其他)\)?$")
_PACKAGING_OCR_FIXES = {
    "散丬": "散装",
    "散〗": "散装",
    "袋丬": "袋装",
    "袋〗": "袋装",
    "捆扌": "捆扎",
}
_RATING = re.compile(
    r"结算([0-5](?:\.\d+)?)爽约([0-5](?:\.\d+)?)评价([0-5](?:\.\d+)?)$"
)
_MILEAGE = re.compile(r"(?<![\w.])\d[\dOo]*(?:\.[\dOo]+)?km", re.I)
_NET_RECHECK = "净得运费局部复读:"


def detail_confirmed(lines: list[Line]) -> bool:
    """用详情特有的标签确认当前页面。"""
    text = normalize("".join(line[0] for line in lines))
    return "预估里程" in text and (
        "货主要求" in text or "车辆" in text or "装货" in text
    )


def detail_removed(lines: list[Line]) -> bool:
    """识别详情页打开时货源已下架的提示。"""
    text = normalize("".join(line[0] for line in lines))
    return any(
        notice in text
        for notice in ("该货源已下架", "货主已将货源删除", "该货源已被抢")
    )


def detail_removed_control(lines: list[Line]) -> Line | None:
    """查找失效货源弹窗自身的返回按钮。"""
    if not detail_removed(lines):
        return None
    return next((line for line in lines if normalize(line[0]) == "返回"), None)


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
    engine: HybridEngine | WindowsOCR | PaddleEngine | RapidEngine,
    image: Image.Image,
    *,
    first: bool,
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
                offset_x, offset_y = left, top - 6
                if not address:
                    address = [
                        line
                        for line in lines
                        if line[1] >= left
                        and top - 15 <= (line[2] + line[4]) / 2 <= bottom + 15
                        and not normalize(line[0]).startswith(("装:", "卸:"))
                        and any(
                            marker in normalize(line[0])
                            for marker in ("市", "县", "区", "州", "旗")
                        )
                    ]
                    offset_x = offset_y = 0
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
                            offset_x + max(line[3] for line in address),
                            offset_y + max(line[4] for line in address),
                        )
                    )
    # 触控轨迹可能穿过金额首位，保留原识别并用较深阈值复读金额。
    for label, x, y, right, bottom in lines:
        if normalize(label) != "净得运费" or x > image.width * 0.16:
            continue
        if not any(
            right - 10 <= left < image.width * 0.48
            and top <= bottom
            and lower >= y
            and re.fullmatch(r"\d+(?:\.\d+)?元", normalize(value))
            for value, left, top, _, lower in lines
        ):
            continue
        left = right - 5
        end = int(image.width * 0.51)
        if end <= left:
            continue
        crop = pixels[max(0, y - 36) : bottom + 10, left:end]
        ink = np.where(np.median(crop, axis=2) < 75, 0, 255).astype(np.uint8)
        prepared_crop = ImageOps.expand(
            Image.fromarray(ink).convert("RGB"), border=10, fill="white"
        )
        amounts = {
            match[1] + "元"
            for value, *_ in engine.locate(prepared_crop)
            if (
                match := re.fullmatch(
                    r"[《¥￥]?(\d{2,}(?:\.\d+)?)(?:元[oO]?)?", normalize(value)
                )
            )
        }
        if len(amounts) == 1:
            lines.append((_NET_RECHECK + next(iter(amounts)), left, y, end, bottom))
        break
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
    listed_ranges = re.findall(r"\d+(?:\.\d+)?-\d+(?:\.\d+)?吨", card_key)
    for index, lines in enumerate(pages):
        page_rows = rows(
            [line for line in lines if not line[0].startswith(_NET_RECHECK)]
        )
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
                        if (match := _MILEAGE.search(normalize(value)))
                    ]
                    if candidates:
                        distance = min(candidates)[1].lower().replace("o", "0")
                        fields.setdefault("estimated_distance", distance)
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
                if number + 1 < len(compact):
                    extra = compact[number + 1]
                    if (
                        value.endswith("/")
                        and extra
                        in ("飞翼车", "平板", "高栏", "高低板", "厢式", "冷藏")
                        or value.endswith("高低")
                        and extra == "板"
                    ):
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
                for listed_range in listed_ranges:
                    value = re.sub(
                        rf"(?<![\d.]){re.escape(listed_range.replace('-', '.'))}",
                        listed_range,
                        value,
                    )
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
                    if _BLOCK.match(extra) or extra.startswith(
                        ("接单前", "查看详情", "以上信息由系统", "常见问题")
                    ):
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
    return fields


def clean_freight(
    pages: list[list[Line]], fields: dict[str, str], review: set[str], card_key: str
) -> None:
    """整理人工表字段。保留 OCR 原文，疑义留给复核。"""
    text = [
        normalize(row)
        for page in pages
        for row in rows([line for line in page if not line[0].startswith(_NET_RECHECK)])
    ]
    for key in ("loading_location", "unloading_location"):
        if address := fields.get(key):
            address = re.sub(r"(?<=[\u4e00-\u9fff])[Oo○]$", "", address)
            address = re.sub(r"(?<!\d)[.·、。]+|[.·、。]+(?!\d)", "-", address)
            # OCR can mistake the colored pickup/dropoff marker for a vertical bar.
            address = re.sub(r"[|丨｜]+", "-", address)
            address = re.sub(r"一/$", "", address).strip("-")
            if match := re.fullmatch(r"([\u4e00-\u9fff]{2,})\1", address):
                address = match[1]
            fields[key] = address
            if re.search(r"[，,丿《“”亠]|^市-|[\u4e00-\u9fff][Oo0]$", address):
                review.add(key)

    details = []
    volume = None
    if cargo := fields.get("cargo"):
        cleaned_cargo = re.sub(r"(?:[±卩]+|找货[《〈<]*)+$", "", cargo)
        if cleaned_cargo != cargo:
            review.add("cargo")
        cargo = cleaned_cargo
        if delivery := re.search(r"(卸(?:货地:?)?个人货场)$", cargo):
            details.append(delivery[1])
            cargo = cargo[: delivery.start()]
        for observed, corrected in _PACKAGING_OCR_FIXES.items():
            if cargo.endswith(observed):
                cargo = cargo[: -len(observed)] + corrected
                break
        cleaned_cargo = re.sub(r"(?:[亻弋丬士]|衣(?=[亻弋丬士]))+$", "", cargo)
        if cleaned_cargo != cargo:
            review.add("cargo")
        cargo = cleaned_cargo
        if packaging := _PACKAGING.search(cargo):
            cargo = cargo[: packaging.start()].rstrip(",")
            if cargo.startswith(packaging[1]) and len(cargo) > len(packaging[1]):
                cargo = cargo[len(packaging[1]) :]
        if "weight" in fields and (size := re.search(r"(\d+(?:-\d+)?方)(吨?)$", cargo)):
            volume = size[1]
            if size[2]:
                review.add("cargo")
            cargo = cargo[: size.start()]
        if "总重量" in cargo:
            review.update(("cargo", "weight"))
            cargo = re.sub(r"总重量\d+(?:[.．]\d*)?$", "", cargo)
        if re.search(r"[〗丬扌亻弋卩±]", cargo):
            review.add("cargo")
        if cargo:
            fields["cargo"] = cargo
        else:
            fields.pop("cargo")
    else:
        packaging = None

    if weight := fields.get("weight"):
        if re.fullmatch(r"[1-9]\d+\.[1-9]\d+吨", weight):
            review.add("weight")
        variants = {weight, weight.replace("-", ".")}
        total = any("总重量" + value in row for value in variants for row in text)
        fields["weight"] = " ".join(
            part
            for part in (
                ("总重" if total else "") + weight,
                volume,
                packaging[1] if packaging else None,
            )
            if part
        )

    if vehicle := fields.get("vehicle"):
        fields["vehicle"] = vehicle.removeprefix("整车")
        fields["vehicle"] = re.sub(
            r"(?<=米)(?=[\u4e00-\u9fff])", " ", fields["vehicle"], count=1
        )
        if fields["vehicle"].endswith(("/", "高低")):
            review.add("vehicle")
    if fields.get("truck_count", "").isdigit():
        fields["truck_count"] = f"还需{fields['truck_count']}辆"
    elif "truck_count" not in fields and any("需要车数" in row for row in text):
        review.add("truck_count")

    rates = {
        match[0]
        for row in text
        if not row.startswith(("订金", "实付", "信息费"))
        for match in _PRICE.finditer(row)
        if "/" in match[0]
    }
    net = {
        match[1]
        for row in text
        if row.startswith(("净得运费", "预计净得运费"))
        and (
            match := re.match(
                r"(?:预计)?净得运费:?[¥￥]?(\d+(?:\.\d+)?元)(?![\d.*/])", row
            )
        )
        and not re.search(r"\d\*", row)
    }
    corroborated = {
        amount for amount in net if any(rate.split("/")[0] == amount for rate in rates)
    }
    detail_totals = {
        match[0]
        for row in text
        if (row.startswith("总运费") or re.match(r"^灬费\d", row))
        and (match := _PRICE.search(row))
    }
    listed_price = card_key.rsplit("|", 1)[-1]
    if re.fullmatch(r"\d+(?:\.\d+)?元", listed_price) and not corroborated:
        truncated = {
            amount
            for amount in net
            if amount != listed_price and listed_price.endswith(amount)
        }
        if truncated:
            net -= truncated
            fees = {
                Decimal(match[0].removesuffix("元"))
                for row in text
                if row.startswith("信息费")
                and (match := re.search(r"\d+(?:\.\d+)?元", row))
            }
            listed_amount = Decimal(listed_price.removesuffix("元"))
            totals = {Decimal(rate.split("元")[0]) for rate in detail_totals}
            rechecked = any(
                value == _NET_RECHECK + listed_price
                for page in pages
                for value, *_ in page
            )
            if len(totals) == 1 and (
                listed_amount in totals
                or rechecked
                and listed_amount <= next(iter(totals))
                or len(fees) == 1
                and listed_amount + next(iter(fees)) in totals
            ):
                net.add(listed_price)
            else:
                review.add("price")
    if corroborated:
        net = corroborated
    if fields.get("price") == "电议":
        fields["price"] = "请先与货主协商(净得运费)"
    elif any(row.startswith(("运费:在线沟通", "运费在线沟通")) for row in text):
        fields["price"] = "在线沟通"
    elif len(net) == 1:
        amount = next(iter(net))
        totals = detail_totals.copy()
        totals.update(rate for rate in rates if rate.split("/")[0] == amount)
        # 同额时优先保留有单位的读数。
        totals = {total for total in totals if total + "/趟" not in totals}
        rate = next(iter(totals)) if len(totals) == 1 else None
        if len(totals) > 1:
            review.add("price")
        fields["price"] = f"净得{amount}(总运费{rate})" if rate else f"净得{amount}"
    elif len(net) > 1:
        review.add("price")
    elif "price" not in fields and len(rates) == 1:
        fields["price"] = next(iter(rates))
    elif "price" not in fields and listed_price:
        # 无单位的列表金额仍有证据，保留金额并提示核对单位。
        if re.fullmatch(r"\d+(?:\.\d+)?元", listed_price):
            fields["price"] = listed_price
            review.add("price")

    loading_time = None
    loading_time_reviewed = False
    reviewed_before_time = "other_info" in review
    loading_start_hour = None
    if pages:
        for line, x, y, _, _ in pages[0]:
            if x > 160 or y > 180:
                continue
            line = normalize(line).replace("裝", "装")
            if re.match(r"^(今天|明天|后天|今明|装货时间)", line):
                line = re.split(
                    r"\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)*米", line, maxsplit=1
                )[0]
                line = re.sub(r"(?<=\d{2}:\d{2})[.·](?=\d{1,2}:\d{2})", "-", line)
                # OCR 偶尔把时间范围的短横线和下一小时的「1」合并识别为「刁」。
                line = re.sub(r"(\d{1,2}:\d{2})刁(?=\d:\d{2})", r"\1-1", line)
                if re.search(r"[亿刁]|\d{1,2}:\d{2}[.·]", line):
                    review.add("other_info")
                    loading_time_reviewed = True
                    if start := re.search(r"(\d{1,2}):\d{2}", line):
                        loading_start_hour = int(start[1])
                elif re.search(r"装货|可装", line):
                    loading_time = line.replace(",", "，").strip("，")
                    break
    if loading_time is None and (
        listed_time := re.search(
            r"(?:今天或明天|今明均|今天|明天|后天|明(?=\d))"
            r"(?:\d{1,2}(?::\d{2})?(?:-\d{1,2}(?::\d{2})?)?点?(?:前)?)?"
            r"(?:可装|装货)",
            card_key,
        )
    ):
        loading_time = listed_time[0]
        if loading_time.startswith("明") and not loading_time.startswith("明天"):
            loading_time = "明天" + loading_time[1:]
        listed_start = re.match(r"明(?:天)?(\d{1,2})", loading_time)
        if (
            loading_time_reviewed
            and not reviewed_before_time
            and listed_start
            and loading_start_hour == int(listed_start[1])
        ):
            review.discard("other_info")
    if loading_time:
        details.insert(0, loading_time)
    if note := fields.get("other_info"):
        note = note.split("以上信息由系统", 1)[0]
        note = re.sub(r"[,，]+", "，", note)
        note = re.sub(r"[;；]+", "；", note).strip("，；。")
        if note:
            details.append(note)
    ratings = {match.groups() for row in text if (match := _RATING.search(row))}
    if len(ratings) == 1:
        settlement, cancellation, rating = next(iter(ratings))
        details.append(f"结算{settlement}/爽约{cancellation}/评价{rating}")
    elif len(ratings) > 1:
        review.add("other_info")
    if details:
        fields["other_info"] = "；".join(dict.fromkeys(filter(None, details)))
    if price_match := re.fullmatch(r"(\d+(?:\.\d+)?)元", fields.get("price", "")):
        unit_rates = {rate for rate in rates if rate.startswith(price_match[1] + "元/")}
        if (
            "price" not in review
            and len(unit_rates) == 1
            and listed_price in unit_rates
        ):
            fields["price"] = listed_price
        else:
            review.add("price")
    review.update(
        key
        for key in (
            "shipper",
            "loading_location",
            "unloading_location",
            "cargo",
            "weight",
            "estimated_distance",
            "vehicle",
            "price",
        )
        if not fields.get(key)
    )


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
    text_lines = lines
    for label, keys in _FIELD_LABELS.items():
        for text, x, y, x2, y2 in text_lines:
            normalized = normalize(text)
            if not normalized.startswith(label):
                continue
            if "货源反馈" in normalized:
                marker_tail = normalized.split("货源反馈", 1)[1]
                if not marker_tail or re.fullmatch(
                    r"[一/|丨｜.·，,。:：()（）]*", marker_tail
                ):
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
    observations = [extract_fields([lines], None, card_key) for lines in pages]
    review = set()
    cargo_values = {
        normalize(observation["cargo"])
        for observation in observations
        if observation.get("cargo")
    }
    if len(cargo_values) > 1:
        packaged = [
            observation["cargo"]
            for observation in observations
            if observation.get("cargo") and _PACKAGING.search(observation["cargo"])
        ]
        if len(packaged) == 1:
            packaged_value = packaged[0]
            package_match = _PACKAGING.search(packaged_value)
            cargo_base = normalize(_PACKAGING.sub("", packaged_value).rstrip(","))
            package_suffix = package_match[1] if package_match else ""
            has_packaging_variant = any(
                value.startswith(cargo_base)
                and package_suffix.startswith(value[len(cargo_base) :])
                for value in cargo_values
            )
            suspect_tail = re.compile(r"(?:[衣亻弋丬士±卩]|找货[《〈<]*)+$")
            has_suspect_variant = any(
                (tail := value[len(cargo_base) :]) and re.fullmatch(suspect_tail, tail)
                for value in cargo_values
                if value.startswith(cargo_base)
            )
            if has_packaging_variant or has_suspect_variant:
                fields["cargo"] = packaged_value
            if has_suspect_variant:
                review.add("cargo")
        else:
            card_confirmed = {
                normalize(observation["cargo"]): observation["cargo"]
                for observation in observations
                if observation.get("cargo")
                and normalize(observation["cargo"]) in normalize(card_key)
            }
            if len(card_confirmed) == 1:
                fields["cargo"] = next(iter(card_confirmed.values()))
                review.add("cargo")
    blocked = [
        feedback_blocked(lines, width)
        for lines, width in zip(pages, widths, strict=True)
    ]
    affected = set().union(*blocked)
    if not affected:
        return fields, review
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
