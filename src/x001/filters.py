"""Shared database filters for the freight list and Excel export."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

VEHICLE_TYPES = ("高栏", "平板", "厢式", "高低板", "自卸", "冷藏")
VEHICLE_LENGTHS = (
    "4.2",
    "6.8",
    "9.6",
    "11.7",
    "12.5",
    "13",
    "13.7",
    "15",
    "16",
    "17.5",
)
TRUCK_FILTERS = ("", "1-2", "3-5", "6+", "unknown")
PRICE_FILTERS = ("", "priced", "unpriced")
TIME_FILTERS = ("", "today", "yesterday", "7d", "30d", "day")


@dataclass(frozen=True)
class FreightFilters:
    route: str = ""
    search: str = ""
    vehicle: str = ""
    trucks: str = ""
    price: str = ""
    length: str = ""
    period: str = ""
    day: str = ""

    def sql(self, *, now: datetime | None = None) -> tuple[str, list[str]]:
        """Return parameterized conditions for captures c and freight f."""
        if self.vehicle and self.vehicle not in VEHICLE_TYPES:
            raise ValueError("无效的车型筛选")
        if self.length and self.length not in (*VEHICLE_LENGTHS, "unknown"):
            raise ValueError("无效的车长筛选")
        if self.trucks not in TRUCK_FILTERS:
            raise ValueError("无效的车数筛选")
        if self.price not in PRICE_FILTERS:
            raise ValueError("无效的运价筛选")
        if self.period not in TIME_FILTERS:
            raise ValueError("无效的时间筛选")
        if self.period == "day":
            try:
                chosen_day = date.fromisoformat(self.day)
            except ValueError as error:
                raise ValueError("请选择有效的采集日期") from error
            if chosen_day.isoformat() != self.day:
                raise ValueError("请选择有效的采集日期")
        where = ["1=1"]
        params = []
        if self.route:
            where.append("c.route = ?")
            params.append(self.route)
        if self.period:
            local_now = now or datetime.now().astimezone()
            local_zone = local_now.tzinfo
            if local_zone is None:
                raise ValueError("时间筛选需要本地时区")
            if self.period in ("today", "yesterday", "day"):
                selected_day = (
                    chosen_day
                    if self.period == "day"
                    else local_now.date() - timedelta(days=self.period == "yesterday")
                )
                start = datetime.combine(selected_day, time.min, local_zone)
                end = start + timedelta(days=1)
            else:
                start = local_now - timedelta(days=7 if self.period == "7d" else 30)
                end = local_now
            where.append("c.captured_at >= ? AND c.captured_at < ?")
            params.extend(
                stamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                for stamp in (start, end)
            )
        if self.search:
            where.append(
                "(c.card_key LIKE ? OR c.route LIKE ? "
                "OR f.loading_location LIKE ? OR f.unloading_location LIKE ? "
                "OR f.cargo LIKE ? OR f.shipper LIKE ?)"
            )
            params.extend([f"%{self.search}%"] * 6)
        if self.vehicle:
            where.append("instr(coalesce(f.vehicle, ''), ?) > 0")
            params.append(self.vehicle)
        lengths = (
            "replace(replace(substr(coalesce(f.vehicle, ''), 1, "
            "instr(coalesce(f.vehicle, ''), '米') - 1), '零担', ''), '长', '')"
        )
        if self.length == "unknown":
            where.append(f"trim({lengths}) = ''")
        elif self.length:
            where.append(f"instr('/' || {lengths} || '/', '/' || ? || '/') > 0")
            params.append(self.length)
        count = (
            "CAST(replace(replace(coalesce(f.truck_count, ''), '还需', ''), "
            "'辆', '') AS INTEGER)"
        )
        if self.trucks == "unknown":
            where.append(f"{count} = 0")
        elif self.trucks == "1-2":
            where.append(f"{count} BETWEEN 1 AND 2")
        elif self.trucks == "3-5":
            where.append(f"{count} BETWEEN 3 AND 5")
        elif self.trucks == "6+":
            where.append(f"{count} >= 6")
        display_price = "coalesce(nullif(f.price, ''), nullif(c.list_price, ''), '')"
        if self.price == "priced":
            where.append(f"instr({display_price}, '元') > 0")
        elif self.price == "unpriced":
            where.append(f"instr({display_price}, '元') = 0")
        return " AND ".join(where), params
