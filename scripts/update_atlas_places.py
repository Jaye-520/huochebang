"""Refresh offline place centers from DataV; run explicitly, never at startup."""

import json
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1] / "src/x001/web_static"
PROVINCES = {
    130000: "河北",
    120000: "天津",
    640000: "宁夏",
    140000: "山西",
    610000: "陕西",
    150000: "内蒙古",
    110000: "北京",
    620000: "甘肃",
    370000: "山东",
    410000: "河南",
}


def fetch(code):
    with urlopen(
        f"https://geo.datav.aliyun.com/areas_v3/bound/{code}_full.json", timeout=30
    ) as response:
        return json.load(response)["features"]


def place(feature, province, parent=None):
    p = feature["properties"]
    full = p["name"]
    lon, lat = p.get("center") or p["centroid"]
    name = full[:-1] if full.endswith("市") else full
    return dict(
        id=str(p["adcode"]),
        name=name,
        full_name=full,
        lon=lon,
        lat=lat,
        province=province,
        aliases=list(dict.fromkeys([full, name])),
        **({"parent": parent} if parent else {}),
    )


def main():
    path = ROOT / "atlas-data.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    cities = []
    districts = []
    for code, province in PROVINCES.items():
        features = fetch(code)
        if code in (120000, 110000):
            cities.append(
                dict(
                    id=str(code),
                    name=province,
                    full_name=province + "市",
                    lon=117.20 if code == 120000 else 116.40,
                    lat=39.12 if code == 120000 else 39.90,
                    province=province,
                    aliases=[province + "市", province],
                )
            )
            if code == 120000:
                districts.extend(place(f, province, province) for f in features)
            continue
        for f in features:
            c = place(f, province)
            cities.append(c)
            if code in (130000, 640000):
                districts.extend(place(d, province, c["name"]) for d in fetch(c["id"]))
    # Keep the existing corridor control points independently of city-level centers.
    cities.extend(c for c in data["cities"] if c.get("scope"))
    data.update(
        cities=cities,
        districts=districts,
        place_source="https://geo.datav.aliyun.com/areas_v3/bound/",
        place_note="DataV 离线地名快照，2026-11-13 获取；不保证反映最新行政区划调整。",
    )
    path.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(f"{len(cities)} city/control points; {len(districts)} districts")


if __name__ == "__main__":
    main()
