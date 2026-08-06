"""
Promote generated data into the-blue-app, with validation gates.

The blue app is a pure consumer: it reads JSON from its own ``constants/``
folder. This script is the single, checked promotion path from the generator's
outputs to those files, so the app can never silently end up with a partial
dataset (the failure mode that left it at 30/320 runs).

It refuses to overwrite the app's files unless:
  * runPoints.json contains every expected run id (default 1..320), and
  * knowledgePois.json is a non-empty list of geocoded points.

Use ``--allow-partial`` to promote anyway (prints what's missing first).

Usage:
    python scripts/promote_to_app.py
    python scripts/promote_to_app.py --app-dir ../the-blue-app
    python scripts/promote_to_app.py --expected 320 --allow-partial
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP = ROOT.parent / "the-blue-app"

# (source in generator) -> (destination filename in the app's constants/)
RUNS_SRC = ROOT / "constants" / "runPoints.json"
QA_SRC = ROOT / "constants" / "qa_report.json"
POIS_SRC = ROOT / "constants" / "knowledge_pois.json"
BOROUGHS_SRC = ROOT / "constants" / "london_boroughs.geojson"

DESTINATION_NAMES = {
    RUNS_SRC: "runPoints.json",
    QA_SRC: "qa_report.json",
    POIS_SRC: "knowledgePois.json",
}


def _load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 - surface the reason, don't crash
        print(f"  ! could not parse {path}: {exc}")
        return None


def validate_runs(expected: int) -> tuple[bool, list[int]]:
    runs = _load_json(RUNS_SRC)
    if not isinstance(runs, list):
        print(f"  ! {RUNS_SRC} missing or not a list")
        return False, list(range(1, expected + 1))
    present = {r.get("id") for r in runs if isinstance(r, dict)}
    missing = [i for i in range(1, expected + 1) if i not in present]
    print(f"  runs: {len(present)}/{expected} present"
          + (f" — MISSING {missing}" if missing else " ✓"))
    return not missing, missing


def validate_pois() -> bool:
    pois = _load_json(POIS_SRC)
    ok = isinstance(pois, list) and len(pois) > 0
    n = len(pois) if isinstance(pois, list) else 0
    print(f"  pois: {n} geocoded points" + (" ✓" if ok else " — EMPTY/MISSING"))
    if ok:
        # The app relies on borough/sector enrichment; promoting an unenriched
        # build (e.g. one made with missing reference data) is a regression.
        enriched = sum(1 for p in pois if isinstance(p, dict) and "borough" in p)
        print(f"  pois enriched with borough/sector: {enriched}/{n}"
              + (" ✓" if enriched else " — MISSING ENRICHMENT"))
        ok = enriched > 0
    return ok


def validate_qa(min_passed: int) -> bool:
    qa = _load_json(QA_SRC)
    if not isinstance(qa, dict):
        print(f"  ! {QA_SRC} missing or not a dict")
        return False
    runs = {k: v for k, v in qa.items() if str(k).lstrip("-").isdigit()}
    stale = sum(1 for v in runs.values() if "status" not in v)
    passed = sum(1 for v in runs.values() if v.get("passed"))
    osm = (qa.get("_provenance") or {}).get("osm_pois", 0)
    print(f"  qa: {passed}/{len(runs)} passed, {stale} stale-shape entries, "
          f"osm_pois={osm}")
    ok = True
    if stale:
        print(f"  ! {stale} qa entries lack 'status' — stale merge; rebuild first")
        ok = False
    if not osm:
        print("  ! qa _provenance.osm_pois is 0 — the OSM gazetteer tier was "
              "empty for this build; run `krg osm-pois` and rebuild")
        ok = False
    if passed < min_passed:
        print(f"  ! only {passed} runs passed (< --min-passed {min_passed})")
        ok = False
    return ok


def build_zones(app: Path) -> None:
    """Emit the app's zones.json from the borough reference data so the zone
    layer and POI enrichment share one source."""
    boroughs = _load_json(BOROUGHS_SRC)
    if not isinstance(boroughs, dict) or not boroughs.get("features"):
        print(f"  - skip zones.json: {BOROUGHS_SRC} missing or empty")
        return
    zones = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": f.get("properties", {}).get("name")},
                "geometry": f.get("geometry"),
            }
            for f in boroughs["features"]
            if f.get("properties", {}).get("name")
        ],
    }
    dst = app / "constants" / "zones.json"
    dst.write_text(json.dumps(zones))
    print(f"  + built zones.json ({len(zones['features'])} boroughs) -> {dst}")


def promote_one(src: Path, dst: Path) -> None:
    if not src.exists():
        print(f"  - skip {dst.name}: source {src} not found")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"  + {src.relative_to(ROOT)} -> {dst}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=int, default=320, help="Expected run count.")
    # Floor sits just under the current honest pass count (227 after the 2026-08
    # resolution work) so routine promotions can't regress below it; raise toward
    # 300 as router legality/directness work lands.
    parser.add_argument("--min-passed", type=int, default=225,
                        help="Minimum QA-passed run count required to promote.")
    parser.add_argument("--allow-partial", action="store_true",
                        help="Promote even if runs are incomplete or POIs missing.")
    parser.add_argument("--app-dir", type=Path, default=DEFAULT_APP,
                        help="Consumer app checkout to promote into "
                             f"(default: {DEFAULT_APP}).")
    args = parser.parse_args()

    app = args.app_dir.expanduser().resolve()
    # Without this check a wrong/missing --app-dir silently *creates* the tree
    # and writes a dataset nothing reads.
    if not app.is_dir():
        print(f"App directory not found: {app}\n"
              "Pass --app-dir /path/to/the-blue-app.")
        return 1

    print(f"Validating generator outputs in {ROOT / 'constants'} ...")
    runs_ok, _missing = validate_runs(args.expected)
    pois_ok = validate_pois()
    qa_ok = validate_qa(args.min_passed)

    if not (runs_ok and pois_ok and qa_ok) and not args.allow_partial:
        print("\nRefusing to promote: validation failed. "
              "Re-run the pipeline(s), or pass --allow-partial to override.")
        return 1

    print(f"\nPromoting into {app / 'constants'} ...")
    for src, name in DESTINATION_NAMES.items():
        promote_one(src, app / "constants" / name)
    build_zones(app)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
