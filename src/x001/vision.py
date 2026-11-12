"""识别线路列表中的卡片、折叠入口和跨屏重叠。"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from io import BytesIO

import numpy as np
from PIL import Image

# OCR 文本与边界框，坐标顺序为左、上、右、下。
type Line = tuple[str, int, int, int, int]
_PRICE = re.compile(r"(\d+(?:\.\d+)?)元[/／](吨|趟|车)")
_FOLD = "折叠信息高度相似"
_END = ("没有更多货源", "没有更多了", "已到底", "暂无货源")


def normalize(text: str) -> str:
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    value = re.sub(r"(?<=\d)[一–—~至](?=\d)", "-", value)
    return re.sub(r"(?<=\d)·(?=\d)", ".", value)


def decode(png: bytes) -> Image.Image:
    with Image.open(BytesIO(png)) as image:
        return image.convert("RGB")


def list_area(image: Image.Image) -> Image.Image:
    width, height = image.size
    return image.crop((0, int(height * 0.045), width, int(height * 0.945)))


def detail_area(image: Image.Image, *, first: bool) -> Image.Image:
    width, height = image.size
    return image.crop(
        (0, int(height * (0.10 if first else 0.104)), width, int(height * 0.857))
    )


def detail_loading(image: Image.Image) -> bool:
    """详情主体可能先出现，底栏的浅灰占位按钮仍表示页面在加载。"""
    width, height = image.size
    buttons = np.asarray(image)[
        int(height * 0.88) : int(height * 0.92),
        int(width * 0.03) : int(width * 0.97),
    ]
    gray = np.all((buttons >= 243) & (buttons <= 245), axis=2)
    return bool(np.mean(gray) > 0.75)


def list_body(image: Image.Image) -> Image.Image:
    """提取列表用于比较滚动进展的区域，避开页头和右侧操作区。"""
    width, height = image.size
    return image.crop((0, int(height * 0.23), int(width * 0.65), int(height * 0.945)))


def visual_progress(previous: Image.Image, current: Image.Image) -> bool:
    def sample(image: Image.Image) -> np.ndarray:
        return np.asarray(image.convert("L").resize((100, 160)), dtype=np.int16)

    return bool(np.mean(np.abs(sample(previous) - sample(current)) > 15) > 0.008)


def rows(lines: list[Line]) -> list[str]:
    groups: list[list[Line]] = []
    for line in sorted(lines, key=lambda item: (item[2] + item[4]) / 2):
        if groups:
            anchor = groups[-1][0]
            offset = abs(line[2] + line[4] - anchor[2] - anchor[4]) / 2
            if offset <= max(10, max(line[4] - line[2], anchor[4] - anchor[2]) * 0.38):
                groups[-1].append(line)
                continue
        groups.append([line])
    return [
        " ".join(line[0] for line in sorted(group, key=lambda line: line[1]))
        for group in groups
    ]


@dataclass(frozen=True)
class Card:
    """一张可点击的货源卡片及其匹配特征。"""

    key: str
    x: int
    y: int
    price: str | None
    price_ink: bytes = field(default=b"", repr=False)
    body_ink: bytes = field(default=b"", repr=False)
    recommended: bool = False
    viewed: bool = False
    clipped: bool = False
    tail_ink: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class Viewport:
    """一张线路页截图中可确认的内容和页面状态。"""

    cards: tuple[Card, ...]
    folds: tuple[tuple[int, int], ...]
    end: bool
    is_list: bool
    route: str = ""
    refreshing: bool = False
    detail_back: tuple[int, int] | None = None
    voice_cancel: tuple[int, int] | None = None


def _card_spans(image: Image.Image, top: int, bottom: int) -> list[tuple[int, int]]:
    pixels = np.asarray(image)
    width, height = image.size
    # 货源卡片之间有近似均匀的浅灰色分隔带。
    gutters = pixels[top:bottom, width // 10 : width * 9 // 10]
    neutral = (gutters.max(axis=2) - gutters.min(axis=2)) <= 5
    gray = (gutters[:, :, 0] >= 225) & (gutters[:, :, 0] <= 246)
    divider = np.mean(neutral & gray, axis=1) > 0.87
    boundaries = np.flatnonzero(np.diff(np.r_[True, divider, True]))
    return [
        (top + int(start), top + int(end))
        for start, end in zip(boundaries[::2], boundaries[1::2], strict=True)
        if end - start > height * 0.025
    ]


def _price_ink(image: Image.Image, top: int) -> bytes:
    width, height = image.size
    patch = image.crop(
        (
            int(width * 0.70),
            top + int(height * 0.038),
            int(width * 0.98),
            top + int(height * 0.063),
        )
    )
    pixels = np.asarray(patch)
    dark = (pixels.max(axis=2) < 115) & (np.ptp(pixels, axis=2) < 35)
    mask = Image.fromarray(dark.astype(np.uint8) * 255)
    bounds = mask.getbbox()
    if not bounds or bounds[2] - bounds[0] < width * 0.065:
        return b""
    return (
        mask.crop(bounds)
        .resize((96, 32))
        .point(lambda value: 255 if value > 128 else 0, "1")
        .tobytes()
    )


def _body_ink(
    image: Image.Image,
    top: int,
    bottom: int,
    *,
    viewed: bool,
    height_ratio: float = 0.10,
) -> bytes:
    width, height = image.size
    end = bottom - int(height * 0.035)
    # 固定栏可能遮住首卡上缘。优先从仍可见的卡片下缘对齐正文。
    start = max(top, end - int(height * height_ratio))
    # 跳过靠屏幕边缘的推荐角标，用卡片正文对齐。
    pixels = np.asarray(image)[start:end, int(width * 0.02) : int(width * 0.69)]
    dark = pixels.min(axis=2) < (241 if viewed else 218)
    # 从首列文字对齐，避免返回列表后的横向偏移改变整张指纹。
    columns = np.flatnonzero(dark.sum(axis=0) >= height * 0.0085)
    if columns.size == 0:
        return b""
    left = columns[0]
    dark = dark[:, left : left + int(width * 0.635)]
    return (
        Image.fromarray(dark.astype(np.uint8) * 255)
        .resize((128, 48))
        .point(lambda value: 255 if value > 110 else 0, "1")
        .tobytes()
    )


def _viewed_title(pixels: np.ndarray) -> bool:
    # 返回动画期间已读字色也会在 163 附近浮动。
    return bool(
        np.count_nonzero(np.all((pixels >= 158) & (pixels <= 170), axis=2))
        > pixels.size / 600
        and np.count_nonzero(np.all((pixels >= 20) & (pixels <= 35), axis=2))
        < pixels.size / 300
    )


def _same_ink(left: bytes, right: bytes, tolerance: float) -> bool:
    return (
        bool(left and right)
        and sum((a ^ b).bit_count() for a, b in zip(left, right, strict=True))
        <= tolerance * len(left) * 8
    )


def read_viewport(image: Image.Image, lines: list[Line]) -> Viewport:
    width, height = image.size
    offset = int(height * 0.045)
    lines = [(s, x, y + offset, x2, y2 + offset) for s, x, y, x2, y2 in lines]
    header = [line for line in lines if line[2] < height * 0.215]
    settings = any(
        "设置" in normalize(s) and x > width * 0.85 and y < height * 0.09
        for s, x, y, _, _ in header
    )
    filters = [
        y2
        for s, x, y, _, y2 in header
        if "筛选" in normalize(s) and x > width * 0.75 and y > height * 0.09
    ]
    # 返回动画会先露出左边的线路和听单控件，右侧仍被详情页遮挡。
    is_list = settings and bool(filters)
    route = "".join(
        normalize(s).translate(str.maketrans("", "", "→乛>-"))
        for s, x, y, _, _ in header
        if width * 0.2 < x < width * 0.7 and y < height * 0.09
    )
    top = max(
        filters + [line[4] for line in header if "听单" in line[0]],
        default=int(height * 0.18),
    )
    bottom = int(height * 0.945)
    recommendation_headers = [
        (y + bottom_y) // 2
        for text, x, y, _, bottom_y in lines
        if "推荐货源" in normalize(text)
        and width * 0.2 < x < width * 0.8
        and top <= y < bottom
    ]
    cards = []
    folds = []
    for start, end in _card_spans(image, top, bottom):
        panel = [line for line in lines if start <= (line[2] + line[4]) / 2 < end]
        panel_text = normalize("".join(rows(panel)))
        if _FOLD in panel_text:
            fold = next((line for line in panel if "折叠" in line[0]), panel[0])
            folds.append(((fold[1] + fold[3]) // 2, (fold[2] + fold[4]) // 2))
            continue
        # 卡片露出足够内容时再定位，残缺卡片留到下一屏处理。
        if end - start < height * 0.085 or "抢单" not in panel_text:
            continue
        left = [
            line
            for line in panel
            if line[1] < width * 0.66
            and not any(
                word in line[0]
                for word in ("结算", "爽约", "评价", "运费", "推荐", "相似", "保运")
            )
        ]
        right = normalize(
            "".join(rows([line for line in panel if line[1] > width * 0.66]))
        )
        price_match = _PRICE.search(right)
        titles = [
            line
            for line in left
            if line[4] - line[2] > height * 0.017 and line[2] < start + height * 0.055
        ]
        if not titles:
            continue
        summary = normalize("".join(rows(left))).translate(
            str.maketrans("", "", "|'、…")
        )
        if not any(
            word in summary for word in ("装货", "可装", "联系", "吨", "米", "→")
        ):
            continue
        price = price_match[0] if price_match else None
        raw_price = re.search(r"\d+(?:\.\d+)?元", right)
        title = min(titles, key=lambda line: line[2])
        _, x, y, right_x, bottom_y = title
        title_pixels = np.asarray(image)[y:bottom_y, x:right_x]
        viewed = _viewed_title(title_pixels)
        title_center = (y + bottom_y) // 2
        recommended = any(
            normalize(text) in {"推荐", "推荐货源"}
            and x < width * 0.18
            and y <= title[2] + height * 0.012
            for text, x, y, _, _ in panel
        ) or any(section_y < title_center for section_y in recommendation_headers)
        cards.append(
            Card(
                f"{summary}|{price or (raw_price[0] if raw_price else '电议')}",
                width // 4,
                title_center,
                price,
                _price_ink(image, start),
                _body_ink(image, start, end, viewed=viewed),
                recommended,
                viewed,
                start < height * 0.25 and title_center - start < height * 0.018,
                _body_ink(image, start, end, viewed=viewed, height_ratio=0.073),
            )
        )
    screen_text = normalize("".join(rows(lines)))
    toolbar = [
        (y + bottom_y) // 2
        for s, x, y, _, bottom_y in header
        if normalize(s) in {"客服", "举报", "分享"}
        and x > width * 0.60
        and y < height * 0.12
    ]
    detail_back = None
    if not is_list and len(toolbar) == 3 and max(toolbar) - min(toolbar) < 20:
        detail_back = (int(width * 0.075), min(toolbar) - int(height * 0.012))
    voice_cancel = None
    if all(word in screen_text for word in ("声明与条款", "语音引擎", "同意并继续")):
        voice_cancel = next(
            (
                ((x + right_x) // 2, (y + bottom_y) // 2)
                for s, x, y, right_x, bottom_y in lines
                if normalize(s) == "取消" and x < width / 2 and y > height * 0.75
            ),
            None,
        )
    return Viewport(
        tuple(cards),
        tuple(folds),
        any(s in screen_text for s in _END),
        is_list,
        route,
        any(s in screen_text for s in ("正在刷新", "刷新中")),
        detail_back,
        voice_cancel,
    )


def _similar(first: Card, second: Card) -> bool:
    if first.key == second.key:
        if first.price is not None and first.price == second.price:
            return True
        price_matches = (
            not first.price_ink
            or not second.price_ink
            or _same_ink(first.price_ink, second.price_ink, 0.08)
        )
        body_matches = (
            not first.body_ink
            or not second.body_ink
            or _same_ink(first.body_ink, second.body_ink, 0.12)
        )
        return price_matches and body_matches
    left, right = first.key.split("|", 1), second.key.split("|", 1)
    if len(left) != 2 or len(right) != 2:
        return False
    price_identical = _same_ink(first.price_ink, second.price_ink, 0.02)
    if left[1].split("/")[0] != right[1].split("/")[0]:
        if "电议" not in (left[1], right[1]):
            return False
        if not price_identical:
            return False
    if "/" in left[1] and "/" in right[1] and left[1] != right[1]:
        return False
    both_negotiable = left[1] == right[1] == "电议" and not (
        first.price_ink or second.price_ink
    )
    if (price_identical or both_negotiable) and _same_ink(
        first.body_ink, second.body_ink, 0.005
    ):
        return True
    text_similarity = SequenceMatcher(None, left[0], right[0]).ratio()
    if (
        (first.clipped or second.clipped)
        and (price_identical or both_negotiable)
        and text_similarity >= 0.68
        and _same_ink(first.tail_ink, second.tail_ink, 0.005)
    ):
        return True
    anchors = r"\d+(?:\.\d+)?|今天|明天|后天|今明"
    return left[0] == right[0] or (
        text_similarity >= 0.89
        and _same_ink(first.body_ink, second.body_ink, 0.05)
        and re.findall(anchors, left[0]) == re.findall(anchors, right[0])
    )


def match_cards(
    old: tuple[Card, ...],
    visits: list[int | None],
    new: tuple[Card, ...],
    *,
    scroll: bool = False,
    historical: bool = False,
) -> list[int | None]:
    old_keys = [card.key for card in old]
    new_keys = [card.key for card in new]
    if (
        old_keys == new_keys
        and (not historical or len(set(new_keys)) > 1)
        and all(
            abs(a.y - b.y) <= 20 and _similar(a, b)
            for a, b in zip(old, new, strict=True)
        )
    ):
        return visits.copy()
    old_counts, new_counts = Counter(old_keys), Counter(new_keys)
    result: list[int | None] = [None] * len(new)
    for index, key in enumerate(new_keys):
        options = [
            i for i, previous in enumerate(old) if _similar(previous, new[index])
        ]
        if len(options) != 1 or new_counts[key] != 1:
            continue
        previous = options[0]
        if (
            old_counts[old_keys[previous]] != 1
            or sum(_similar(old[previous], current) for current in new) != 1
        ):
            continue
        for direction in (-1, 1):
            a, b = previous + direction, index + direction
            if (
                0 <= a < len(old)
                and 0 <= b < len(new)
                and old_counts[old_keys[a]] == new_counts[new_keys[b]] == 1
                and _similar(old[a], new[b])
            ):
                result[index] = visits[previous]
                break
        if (
            not historical
            and result[index] is None
            and abs(old[previous].y - new[index].y) <= 20
        ):
            # 列表原地插入推荐时，未受影响的卡片仍在原位置。
            result[index] = visits[previous]
        if (
            new[index].viewed
            and visits[previous] is not None
            and _same_ink(old[previous].body_ink, new[index].body_ink, 0.005)
        ):
            # 灰色路线且正文高度一致时，沿用本轮记录，即使附近卡片已换位。
            result[index] = visits[previous]
        if scroll and previous >= len(old) - 2 and index < 2:
            if old[previous].y > new[index].y + 100:
                result[index] = visits[previous]
    return result
