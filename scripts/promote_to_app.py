"""
Promote generated data into the-blue-app, with validation gates.

The blue app is a pure consumer: it reads JSON from its own ``constants/``
folder. This script is the single, checked promotion path from the generator's
outputs to those files, so the app can never silently end up with a partial
dataset (the failure mode that left it at 30/320 runs).

It refuses to overwrite the app's files unless:
  * runPoints.json contains every expected run id (default 1..320), and
  * knowledgePois.json clears its floors and its shape checks: enough geocoded
    points, enough of them carrying a borough, every record's ``category``
    drawn from the closed taxonomy, no emergency or fuel "station" filed as
    transport, and a ``transport_modes`` list on every record whose entries
    are all in the closed vocabulary.

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
sys.path.insert(0, str(ROOT))

from knowledge_run_generator.poi_categories import (  # noqa: E402
    CATEGORIES,
    TRANSPORT_MODES,
    is_non_transport_station_name,
)

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


def _sample(names: list[str], limit: int = 5) -> str:
    shown = ", ".join(sorted(names)[:limit])
    return shown + (" ..." if len(names) > limit else "")


def validate_pois(min_pois: int, min_enriched: int) -> bool:
    """Gate the POI file on shape, not just on being non-empty.

    Floors rather than exact counts, in the style of ``--min-passed``: the row
    count moves whenever the Points List edition or the geocoder does, and a
    gate that has to be edited every build stops being read.

    Deliberately *not* gated: ``yellow_badge_sector``. It is null in 4,972 of
    5,746 rows by design (the central green-badge boroughs have no sector), so
    a floor on it would only measure how much of London is in the middle.
    """
    pois = _load_json(POIS_SRC)
    if not isinstance(pois, list):
        print(f"  ! {POIS_SRC} missing or not a list")
        return False
    records = [p for p in pois if isinstance(p, dict)]
    n = len(records)
    ok = n >= min_pois
    print(f"  pois: {n} geocoded points (floor {min_pois})"
          + (" ✓" if ok else ": TOO FEW"))
    if n != len(pois):
        print(f"  ! {len(pois) - n} entries are not objects")
        ok = False

    # The app relies on borough/sector enrichment; promoting an unenriched
    # build (e.g. one made with missing reference data) is a regression.
    enriched = sum(1 for p in records if p.get("borough"))
    enriched_ok = enriched >= min_enriched
    print(f"  pois enriched with a borough: {enriched}/{n} (floor {min_enriched})"
          + (" ✓" if enriched_ok else ": MISSING ENRICHMENT"))
    ok = ok and enriched_ok

    # Every record must carry a category the app knows how to render.
    unknown = [p.get("name", "?") for p in records if p.get("category") not in CATEGORIES]
    print(f"  pois with a known category: {n - len(unknown)}/{n}"
          + (" ✓" if not unknown else f": {len(unknown)} unknown, {_sample(unknown)}"))
    ok = ok and not unknown

    # The direct regression gate for the taxonomy bug: a fire, police,
    # ambulance, lifeboat, petrol or power "station" filed as transport. These
    # were 71 of the 330 `station` rows in the last promoted build, and they
    # fed the gazetteer's station snapping, so they could move an endpoint.
    leaked = [p.get("name", "?") for p in records
              if p.get("category") == "station"
              and is_non_transport_station_name(str(p.get("name", "")))]
    print(f"  pois wrongly filed as transport stations: {len(leaked)}"
          + (" ✓" if not leaked else f": {_sample(leaked)}"))
    ok = ok and not leaked

    # transport_modes is always present and always a list, even while nothing
    # populates it, so a consumer can iterate without a null check.
    missing_modes = [p.get("name", "?") for p in records
                     if not isinstance(p.get("transport_modes"), list)]
    print(f"  pois with a transport_modes list: {n - len(missing_modes)}/{n}"
          + (" ✓" if not missing_modes else
             f": {len(missing_modes)} missing, {_sample(missing_modes)}"))
    ok = ok and not missing_modes

    bad_modes = sorted({
        str(mode)
        for p in records
        for mode in (p.get("transport_modes") or [])
        if mode not in TRANSPORT_MODES
    })
    if bad_modes:
        print(f"  ! transport_modes outside the vocabulary: {_sample(bad_modes)}")
        ok = False

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

    # Blue Book fidelity. `passed` now gates on legality + ordered traversal +
    # no hard gaps, so these figures largely mirror it — kept as an independent
    # readout so a gate regression can't hide a fidelity slide.
    ordered = [v.get("ordered_coverage") for v in runs.values()
               if v.get("ordered_coverage") is not None]
    strict = [v.get("strict_ordered") for v in runs.values()
              if v.get("strict_ordered") is not None]
    if ordered:
        full = sum(1 for v in ordered if v >= 1.0)
        print(f"  fidelity: {full}/{len(ordered)} runs fully in Blue Book order, "
              f"mean ordered {sum(ordered) / len(ordered):.3f}"
              + (f", mean strict {sum(strict) / len(strict):.3f}" if strict else ""))
    else:
        print("  ! no run carries ordered_coverage — rebuild with a current "
              "pipeline before promoting")
        ok = False
    return ok


def validate_regression() -> bool:
    """Diff this build against the committed baseline.

    CI cannot run this — it needs a qa_report.json, which needs the OSM graph —
    so promotion is the only place the per-run regression check can actually
    gate. Without it, `krg regression diff` is advisory and nothing stops a
    build that quietly lost fidelity on 50 runs from shipping.
    """
    try:
        from knowledge_run_generator.regression import (
            DEFAULT_BASELINE_PATH, diff, format_diff, load_snapshot, summarise,
        )
    except ImportError as exc:
        print(f"  ! cannot import the regression harness: {exc}")
        return False

    baseline_path = Path(DEFAULT_BASELINE_PATH)
    if not baseline_path.exists():
        print(f"  ! no baseline at {baseline_path}; "
              "run `krg regression snapshot` to establish one")
        return False

    result = diff(load_snapshot(baseline_path), summarise(QA_SRC))
    if result.has_regressions:
        print("  ! regression vs baseline:")
        for line in format_diff(result).splitlines():
            print(f"      {line}")
        return False
    print("  regression: no regressions vs baseline ✓")
    return True


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
    # Floor sits just under the current honest pass count (311 under the
    # ordered gate: legality + full ordered traversal + no hard gaps) so
    # routine promotions can't regress below it. The residual failures are
    # explicit OSM-vs-Blue-Book drift (e.g. Hammersmith Bridge closure).
    parser.add_argument("--min-passed", type=int, default=305,
                        help="Minimum QA-passed run count required to promote.")
    # Floors, not exact counts: the row count moves with the Points List
    # edition and with the geocoder's success rate. Current build: 5,746 rows,
    # 5,530 of them carrying a borough.
    parser.add_argument("--min-pois", type=int, default=5000,
                        help="Minimum geocoded POI count required to promote.")
    parser.add_argument("--min-enriched-pois", type=int, default=5000,
                        help="Minimum POIs carrying a borough required to promote.")
    parser.add_argument("--allow-partial", action="store_true",
                        help="Promote even if runs are incomplete or POIs missing.")
    parser.add_argument("--skip-regression", action="store_true",
                        help="Skip the diff against tests/golden/qa_baseline.json. "
                             "Use when the baseline is deliberately being moved.")
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
    pois_ok = validate_pois(args.min_pois, args.min_enriched_pois)
    qa_ok = validate_qa(args.min_passed)
    regression_ok = True if args.skip_regression else validate_regression()

    if not (runs_ok and pois_ok and qa_ok and regression_ok) and not args.allow_partial:
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
