"""Enrich Knowledge POIs with their Greater London borough and yellow-badge sector.

Each POI carries ``coordinates`` as ``[lng, lat]``. We run a point-in-polygon test
against the 33 Greater London local-authority boundaries to assign a ``borough``,
then map that borough to a yellow-badge ``sector`` (1-9) -- or ``None`` for the 11
central green-badge boroughs and for points that fall outside all boroughs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from shapely.geometry import Point, shape
from shapely.prepared import prep

# Default paths to the vendored constants, resolved relative to this file so the
# module works regardless of the current working directory.
_CONSTANTS_DIR = Path(__file__).resolve().parent.parent / "constants"
DEFAULT_BOROUGHS_PATH = _CONSTANTS_DIR / "london_boroughs.geojson"
DEFAULT_SECTORS_PATH = _CONSTANTS_DIR / "yellow_badge_sectors.json"


def missing_inputs(
    boroughs_path: Path | str = DEFAULT_BOROUGHS_PATH,
    sectors_path: Path | str = DEFAULT_SECTORS_PATH,
) -> list[Path]:
    """Return the enrichment reference files that aren't on disk.

    Both files are *inputs*, not pipeline outputs: the borough boundaries come
    from the ONS/London Datastore local-authority GeoJSON, and the sector map
    is hand-maintained. They live under ``constants/``, which is gitignored, so
    a fresh checkout has neither. Callers use this to skip enrichment with a
    useful message instead of dying on a bare FileNotFoundError.
    """
    return [Path(p) for p in (boroughs_path, sectors_path) if not Path(p).exists()]


def load_sectors(path: Path | str = DEFAULT_SECTORS_PATH) -> dict:
    """Load the yellow-badge sector mapping JSON."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_boroughs(path: Path | str = DEFAULT_BOROUGHS_PATH, sectors: Optional[dict] = None):
    """Load borough boundaries as a list of ``(name, prepared_geometry)`` tuples.

    Names are normalized via the sector file's ``name_normalization`` map (if a
    sectors dict is provided) so spelling variants reconcile to the canonical
    ONS names used in ``yellow_badge_sectors.json``. The returned geometries are
    ``shapely.prepared`` objects for fast repeated point-in-polygon queries.
    """
    normalization = {}
    if sectors is not None:
        normalization = sectors.get("name_normalization", {})

    with open(path, "r", encoding="utf-8") as fh:
        geojson = json.load(fh)

    boroughs = []
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        # Accept a few common property keys for the authority name.
        name = (
            props.get("name")
            or props.get("NAME")
            or props.get("lad11nm")
            or props.get("LAD11NM")
            or props.get("borough")
        )
        if name is None:
            continue
        name = normalization.get(name, name)
        geom = shape(feature["geometry"])
        if not geom.is_valid:
            geom = geom.buffer(0)
        boroughs.append((name, prep(geom)))
    return boroughs


def enrich_pois(
    pois: list[dict],
    boroughs=None,
    sectors: Optional[dict] = None,
) -> list[dict]:
    """Add ``borough`` and ``yellow_badge_sector`` to each POI in place.

    ``pois`` records must have ``coordinates`` as ``[lng, lat]``. Points matching
    no borough get ``borough=None`` and ``yellow_badge_sector=None``. Existing keys
    and record order are preserved. Returns the same list for convenience.
    """
    if sectors is None:
        sectors = load_sectors()
    if boroughs is None:
        boroughs = load_boroughs(sectors=sectors)

    borough_to_sector = sectors.get("borough_to_sector", {})

    for poi in pois:
        borough_name = None
        coords = poi.get("coordinates")
        if coords and len(coords) >= 2:
            lng, lat = coords[0], coords[1]
            point = Point(lng, lat)
            for name, prepared_geom in boroughs:
                if prepared_geom.contains(point):
                    borough_name = name
                    break
        poi["borough"] = borough_name
        poi["yellow_badge_sector"] = (
            borough_to_sector.get(borough_name) if borough_name is not None else None
        )
    return pois
