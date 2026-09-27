"""
Build ``knowledge_run_generator/data/postal_district_centres.json``.

One centre per London postal district (W1, SE11, EC1, ...), from postcodes.io
(ONS Postcode Directory, Open Government Licence). postcodes.io only knows
*outcodes*, and the central districts are split into lettered outcodes (W1 is
W1B..W1W, EC1 is EC1A/EC1M/EC1N/EC1R/EC1V/EC1Y), so a district that is not an
outcode itself is centred on the mean of its lettered outcodes' centres.

These centres replaced a Mapbox geocode of the bare district ("W1, London,
UK"), which put W1's centre in Kennington, 3 km south of the Thames; every W1
point was then biased toward, and accepted near, that spot.

Usage:
    python scripts/build_district_centres.py              # districts in use
    python scripts/build_district_centres.py --out PATH
"""

from __future__ import annotations

import argparse
import json
import re
import string
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from knowledge_run_generator.annex_b import load_annex_b  # noqa: E402

OUT = ROOT / "knowledge_run_generator" / "data" / "postal_district_centres.json"
API = "https://api.postcodes.io/outcodes/"
_DISTRICT_RE = re.compile(r"^[A-Z]{1,2}\d{1,2}$")


def _outcode(code: str) -> tuple[float, float] | None:
    for attempt in range(4):
        try:
            resp = requests.get(API + code, timeout=20)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 404:
            return None
        if resp.status_code == 200:
            result = resp.json().get("result") or {}
            lat, lon = result.get("latitude"), result.get("longitude")
            if lat is None or lon is None:
                return None
            return round(float(lat), 6), round(float(lon), 6)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"postcodes.io kept failing for {code}")


def district_centre(district: str) -> dict | None:
    direct = _outcode(district)
    if direct is not None:
        return {"lat": direct[0], "lon": direct[1], "outcodes": [district]}
    parts = {}
    for letter in string.ascii_uppercase:
        centre = _outcode(district + letter)
        if centre is not None:
            parts[district + letter] = centre
        time.sleep(0.05)
    if not parts:
        return None
    lat = sum(c[0] for c in parts.values()) / len(parts)
    lon = sum(c[1] for c in parts.values()) / len(parts)
    return {"lat": round(lat, 6), "lon": round(lon, 6), "outcodes": sorted(parts)}


def districts_in_use() -> set[str]:
    out = set()
    for run in load_annex_b():
        out.update({run.start.district, run.end.district})
    extracted = ROOT / "constants" / "extracted_pois.json"
    if extracted.exists():
        for poi in json.loads(extracted.read_text()):
            d = str(poi.get("postal_district") or "").strip().upper()
            if _DISTRICT_RE.match(d):
                out.add(d)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    centres = {}
    missing = []
    for district in sorted(districts_in_use()):
        centre = district_centre(district)
        if centre is None:
            missing.append(district)
            continue
        centres[district] = centre
        print(f"{district:>5}  {centre['lat']:.5f} {centre['lon']:.5f}  {','.join(centre['outcodes'])}")
    payload = {
        "_source": "postcodes.io /outcodes (ONS Postcode Directory, OGL). A district that "
                   "is not itself an outcode is the mean of its lettered outcodes' centres. "
                   "Built by scripts/build_district_centres.py.",
        "centres": centres,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    print(f"{len(centres)} districts -> {args.out}")
    if missing:
        print(f"no postcodes.io outcode for: {missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
