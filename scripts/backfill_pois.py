"""Backfill Points List entries the paid geocoder failed on, offline.

``krg generate pois`` leaves ~640 names in ``knowledge_pois_failed.json`` —
Mapbox/Nominatim misses. Most are resolvable without any network call, using
the same tiers the run pipeline's gazetteer already trusts:

  * the OSM POI harvest (``constants/osm_pois.json``) — stations, churches,
    venues, with station-stem name matching;
  * the street tier — entries that name a street resolve off the graph, with
    the postal district picking between same-named streets.

Every backfilled coordinate is checked against the district plausibility
model; a candidate that lands implausibly far from the entry's stated postal
district is left failed rather than written wrong.

    .venv/bin/python scripts/backfill_pois.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from knowledge_run_generator.aliases import load_or_build_alias_index
from knowledge_run_generator.cache import cache_dir
from knowledge_run_generator.gazetteer import Gazetteer, load_knowledge_pois
from knowledge_run_generator.osm_pois import load_cached_pois
from knowledge_run_generator.router import load_graph

POIS_PATH = ROOT / "constants" / "knowledge_pois.json"
FAILED_PATH = ROOT / "constants" / "knowledge_pois_failed.json"
EXTRACTED_PATH = ROOT / "constants" / "extracted_pois.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be backfilled without writing.")
    args = parser.parse_args()

    geocoded = json.loads(POIS_PATH.read_text())
    failed_names = json.loads(FAILED_PATH.read_text())
    extracted = json.loads(EXTRACTED_PATH.read_text())
    by_name = {}
    for rec in extracted:
        name = str(rec.get("name") or "").strip()
        if name:
            by_name.setdefault(name.upper(), rec)

    print("Loading graph and indexes...")
    G = load_graph()
    alias_index = load_or_build_alias_index(G, cache_dir() / "alias_index.pkl")
    osm_pois = load_cached_pois(None, ROOT / "constants" / "osm_pois.json")
    knowledge_pois = load_knowledge_pois(POIS_PATH)
    gazetteer = Gazetteer(
        alias_index=alias_index,
        osm_pois=osm_pois,
        knowledge_pois=knowledge_pois,
    )
    model = gazetteer.district_model

    backfilled, residual = [], []
    for name in failed_names:
        clean = str(name).strip()
        source_rec = by_name.get(clean.upper(), {})
        district = str(source_rec.get("postal_district") or "").upper()
        query = f"{clean} {district}".strip()

        entry = None
        try:
            entry = gazetteer.resolve(query, G)
        except Exception:
            entry = None
        if entry is None and district:
            try:
                entry = gazetteer.resolve(clean, G)
            except Exception:
                entry = None
        if entry is None:
            residual.append(clean)
            continue

        # Never write a coordinate the district model calls implausible.
        if district:
            verdict = model.check(district, entry.lat, entry.lon)
            if verdict is not None and verdict["status"] == "fail":
                residual.append(clean)
                continue

        record = dict(source_rec) if source_rec else {"name": clean}
        record["name"] = record.get("name") or clean
        record["coordinates"] = [entry.lon, entry.lat]
        record["geocode_source"] = f"backfill_{entry.source}"
        backfilled.append(record)

    print(f"\n{len(failed_names)} failed names: "
          f"{len(backfilled)} backfilled, {len(residual)} residual")
    from collections import Counter
    print("by tier:", dict(Counter(r["geocode_source"] for r in backfilled)))
    print("residual sample:", residual[:15])

    if args.dry_run:
        return

    existing = {str(r.get("name", "")).upper() for r in geocoded}
    added = [r for r in backfilled if str(r["name"]).upper() not in existing]
    geocoded.extend(added)
    POIS_PATH.write_text(json.dumps(geocoded, indent=1))
    FAILED_PATH.write_text(json.dumps(sorted(residual), indent=2))
    print(f"Wrote {len(added)} new records to {POIS_PATH.name} "
          f"({len(geocoded)} total); {len(residual)} left in {FAILED_PATH.name}")


if __name__ == "__main__":
    main()
