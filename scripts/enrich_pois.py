"""Standalone CLI: enrich Knowledge POIs with borough + yellow-badge sector.

Usage:
    python scripts/enrich_pois.py [--in constants/knowledge_pois.json] [--out <path>]

Loads the POI list, assigns each record a ``borough`` and ``yellow_badge_sector``
via point-in-polygon against the vendored London borough boundaries, and writes
the records back (in place by default). Prints a per-sector summary and the count
of points that matched no borough.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from knowledge_run_generator.poi_enrichment import enrich_pois  # noqa: E402

DEFAULT_POIS = ROOT / "constants" / "knowledge_pois.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--in",
        dest="in_path",
        default=str(DEFAULT_POIS),
        help="Input POI JSON (default: constants/knowledge_pois.json)",
    )
    parser.add_argument(
        "--out",
        dest="out_path",
        default=None,
        help="Output path (default: same as --in, written in place)",
    )
    args = parser.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path) if args.out_path else in_path

    with open(in_path, "r", encoding="utf-8") as fh:
        pois = json.load(fh)

    enrich_pois(pois)

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(pois, fh, ensure_ascii=False, indent=2)

    # Summary.
    sector_counts = Counter()
    no_borough = 0
    central = 0
    for poi in pois:
        if poi.get("borough") is None:
            no_borough += 1
            continue
        sector = poi.get("yellow_badge_sector")
        if sector is None:
            central += 1
        else:
            sector_counts[sector] += 1

    print(f"Enriched {len(pois)} POIs -> {out_path}")
    print("Per yellow-badge sector:")
    for sector in range(1, 10):
        print(f"  sector {sector}: {sector_counts.get(sector, 0)}")
    print(f"Central green-badge boroughs (sector=null): {central}")
    print(f"No borough (outside all 33 authorities): {no_borough}")


if __name__ == "__main__":
    main()
