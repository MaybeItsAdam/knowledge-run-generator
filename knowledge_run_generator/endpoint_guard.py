"""
Build guards on run endpoints: names against TfL Annex B, and no two
different places on one coordinate.

Two differently named endpoints on the *identical* coordinate is the
signature of a geocoder fallback, not of two real places: the shipped data had
Chancery Lane Station on Farringdon Station's point and Holland Park Station on
Latimer Road Station's, and a fresh build put Golden, Bryanston, Cavendish and
Fitzroy Squares W1 on one point near the Oval. Snapping to graph nodes makes
the check sharp: two genuinely different places only share a coordinate if
they snapped to the same junction, which for Blue Book endpoints (hundreds of
metres apart) is itself a bug worth stopping the build for.

Names are compared by their canonical form (``aliases.normalise`` of the name
without its district), so the same place used by several runs is one name.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from .aliases import normalise
from .annex_b import load_annex_b
from .gazetteer import _split_postcode

# Coordinates are graph-node positions written straight from the graph; 1e-7
# degrees (~1 cm) is far below any real separation and above float noise.
_COORD_DECIMALS = 7


def canonical_point_name(name: str) -> str:
    stem, _district = _split_postcode(name)
    return normalise(stem)


def _coord_key(coords) -> tuple[float, float]:
    return (round(float(coords[0]), _COORD_DECIMALS),
            round(float(coords[1]), _COORD_DECIMALS))


def check_endpoint_collisions(points: Mapping[str, Iterable[float]]) -> list[str]:
    """Problems for coordinates shared by two or more *different* names.

    *points* maps an endpoint name to its ``[lon, lat]``. Returns one message
    per shared coordinate; empty means the guard passes.
    """
    by_coord: dict[tuple[float, float], dict[str, str]] = {}
    for name, coords in points.items():
        by_coord.setdefault(_coord_key(coords), {}).setdefault(
            canonical_point_name(name), name)
    problems = []
    for coord, names in sorted(by_coord.items()):
        if len(names) > 1:
            listed = ", ".join(sorted(names.values()))
            problems.append(
                f"{len(names)} different endpoints share [{coord[0]}, {coord[1]}]: {listed}"
            )
    return problems


def run_endpoints(runs: Iterable[dict]) -> dict[str, list[float]]:
    """``{name: coordinates}`` over every start and end in a runPoints list.

    A name that appears at two different coordinates keeps its first; the
    collision guard is about different names, and the pipeline resolves a
    name once per build.
    """
    out: dict[str, list[float]] = {}
    for run in runs:
        for side in ("start", "end"):
            point = run.get(side) or {}
            name, coords = point.get("name"), point.get("coordinates")
            if name and coords:
                out.setdefault(name, coords)
    return out


def check_run_collisions(runs: Iterable[dict]) -> list[str]:
    return check_endpoint_collisions(run_endpoints(runs))


def check_runs_match_annex_b(runs: Iterable[dict]) -> list[str]:
    """Every run's id, title and endpoint names must be TfL Annex B's."""
    canonical = {r.id: r for r in load_annex_b()}
    problems = []
    seen = set()
    for run in runs:
        rid = run.get("id")
        seen.add(rid)
        ref = canonical.get(rid)
        if ref is None:
            problems.append(f"run {rid} is not in TfL Annex B")
            continue
        start = (run.get("start") or {}).get("name")
        end = (run.get("end") or {}).get("name")
        if start != ref.start.display or end != ref.end.display:
            problems.append(
                f"run {rid} is '{start} to {end}', Annex B says "
                f"'{ref.start.display} to {ref.end.display}'"
            )
        elif run.get("title") != ref.title:
            problems.append(f"run {rid} title {run.get('title')!r} is not {ref.title!r}")
    missing = sorted(set(canonical) - seen)
    if missing:
        problems.append(f"{len(missing)} Annex B run(s) missing: {missing[:10]}")
    return problems
