"""Read-only, offline geography for the freight workspace."""

import json
import re
import sqlite3
from pathlib import Path

from .filters import FreightFilters

DATA = Path(__file__).parent / "web_static" / "atlas-data.json"


def locate(address: str, cities: list[dict]) -> str | None:
    # Only match explicit place names, never guess from a provincial route label.
    for city in cities:
        for alias in city.get("aliases", [city["name"]]):
            if re.search(
                r"(?:^|[-—\s/省区])" + re.escape(alias) + r"(?=$|[-—\s/市县区])",
                address,
            ):
                return city.get("scope", city["name"])
    return None


def atlas_snapshot(database: Path, route: str = "", period: str = "") -> dict:
    geography = json.loads(DATA.read_text(encoding="utf-8"))
    if route and route not in geography["routes"]:
        raise ValueError("无效的线路")
    where, params = FreightFilters(route=route, period=period).sql()
    result = {"records": [], "total": 0, "unlocated": 0}
    if not database.exists():
        return result
    db = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT c.id, c.route, c.captured_at, c.list_price, c.screenshots, "
            "f.loading_location, f.unloading_location, f.cargo, f.price, "
            "f.weight, f.vehicle, f.estimated_distance, f.review_fields "
            "FROM captures c JOIN freight f ON f.capture_id=c.id "
            f"WHERE c.status='done' AND {where} ORDER BY c.id DESC",
            params,
        )
        for row in rows:
            item = dict(row)
            review = json.loads(item.pop("review_fields") or "[]")
            for field, target in (
                ("loading_location", "origin"),
                ("unloading_location", "destination"),
            ):
                item[target] = (
                    None
                    if field in review
                    else locate(item[field] or "", geography["cities"])
                )
                district = (
                    None
                    if field in review
                    else locate_district(
                        item[field] or "", geography.get("districts", []), item[target]
                    )
                )
                item[target + "_district"] = district["id"] if district else None
                if district and not item[target]:
                    item[target] = district["parent"]
            item["review"] = bool(review)
            screenshots = json.loads(item.pop("screenshots") or "[]")
            item["images"] = [
                {
                    "name": Path(name).name,
                    "url": f"/api/image?id={item['id']}&index={index}",
                }
                for index, name in enumerate(screenshots)
                if Path(name).name.startswith("detail-")
            ]
            if not item["origin"] or not item["destination"]:
                result["unlocated"] += 1
            result["records"].append(item)
        result["total"] = len(result["records"])
        return result
    finally:
        db.close()


def locate_district(address: str, districts: list[dict], parent: str | None):
    """Match explicit district names; ambiguous names never select a random city."""
    matches = [
        district
        for district in districts
        if (not parent or district["parent"] == parent)
        and re.search(
            r"(?:^|[-—\s/省市区])" + re.escape(district["full_name"]), address
        )
    ]
    return matches[0] if len(matches) == 1 else None
