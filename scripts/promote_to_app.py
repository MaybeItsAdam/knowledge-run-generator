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
    python scripts/promote_to_app.py --expected 320 --allow-partial
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT.parent / "the-blue-app"

# (source in generator) -> (destination in app)
RUNS_SRC = ROOT / "constants" / "runPoints.json"
RUNS_DST = APP / "constants" / "runPoints.json"
QA_SRC = ROOT / "constants" / "qa_report.json"
QA_DST = APP / "constants" / "qa_report.json"
POIS_SRC = ROOT / "constants" / "knowledge_pois.json"
POIS_DST = APP / "constants" / "knowledgePois.json"


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
    return ok


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
    parser.add_argument("--allow-partial", action="store_true",
                        help="Promote even if runs are incomplete or POIs missing.")
    args = parser.parse_args()

    print(f"Validating generator outputs in {ROOT / 'constants'} ...")
    runs_ok, _missing = validate_runs(args.expected)
    pois_ok = validate_pois()

    if not (runs_ok and pois_ok) and not args.allow_partial:
        print("\nRefusing to promote: validation failed. "
              "Re-run the pipeline(s), or pass --allow-partial to override.")
        return 1

    print(f"\nPromoting into {APP / 'constants'} ...")
    promote_one(RUNS_SRC, RUNS_DST)
    promote_one(QA_SRC, QA_DST)
    promote_one(POIS_SRC, POIS_DST)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
