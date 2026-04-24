"""
OSM POI harvester.

Quick Win 8: auto-populate the gazetteer from OpenStreetMap so that
``poi_overrides.json`` only has to hold the corrections and ambiguities
humans have already caught, not every named landmark in London.

Queries Overpass for the categories that actually come up in the Knowledge
(pubs, stations, hotels, theatres, hospitals, museums, places of worship,
parks) within a bounding box, normalises names the same way the alias
index does, and caches the result as JSON so we only hit Overpass on
demand.

The output is a plain dict keyed by uppercased name, shaped like the
``poi_overrides.json`` dict form — so :class:`Gazetteer` can consume it
as a second-chance lookup after the curated overrides.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

# Greater London bbox (south, west, north, east), matching the graph place.
LONDON_BBOX = (51.28, -0.51, 51.69, 0.33)

DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
DEFAULT_CACHE_PATH = Path("/tmp/app_cache/osm_pois.json")

# Tag groups. Each (osm_key, regex-of-values, kind) triple becomes one
# Overpass filter. Kind is stored on the entry so callers can reason about
# snap hints (e.g. a station should prefer its on-street approach).
_TAG_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("amenity", "pub|restaurant|cafe|theatre|cinema|hospital|police|"
                "library|townhall|place_of_worship|bank|university|college",
     "amenity"),
    ("tourism", "hotel|museum|attraction|gallery|hostel", "tourism"),
    ("railway", "station", "station"),
    ("public_transport", "station", "station"),
    ("leisure", "stadium|sports_centre|park", "leisure"),
    ("historic", "memorial|monument|castle", "historic"),
    ("shop", "department_store|mall", "shop"),
)


def _build_query(bbox: tuple[float, float, float, float], timeout_s: int = 180) -> str:
    south, west, north, east = bbox
    clauses: list[str] = []
    for key, values, _kind in _TAG_GROUPS:
        clauses.append(
            f'  node["{key}"~"{values}"]["name"]({south},{west},{north},{east});'
        )
        clauses.append(
            f'  way["{key}"~"{values}"]["name"]({south},{west},{north},{east});'
        )
    body = "\n".join(clauses)
    return f"[out:json][timeout:{timeout_s}];\n(\n{body}\n);\nout center body;"


def _kind_for(tags: dict[str, str]) -> str:
    for key, values, kind in _TAG_GROUPS:
        if key in tags and tags[key] in values.split("|"):
            return kind
    return "misc"


def _extract_name(tags: dict[str, str]) -> str | None:
    name = tags.get("name") or tags.get("official_name") or tags.get("alt_name")
    if not name:
        return None
    return str(name).strip().upper()


def _fetch_overpass(query: str, url: str, retries: int = 3) -> dict:
    import requests
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.post(url, data={"data": query}, timeout=240)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_err = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Overpass fetch failed after {retries} attempts: {last_err}")


def _element_latlon(el: dict) -> tuple[float, float] | None:
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center")
    if center:
        return center.get("lat"), center.get("lon")
    return None


def parse_overpass(payload: dict) -> dict[str, dict]:
    """Turn raw Overpass JSON into ``{NAME_UPPER: {lat, lon, kind, osm_id, on_street?}}``.

    Duplicates on name are dropped — the first occurrence wins. The query
    is bbox-ordered, so the effective tiebreaker is essentially OSM id,
    which is stable across Overpass runs.
    """
    out: dict[str, dict] = {}
    for el in payload.get("elements", []):
        tags = el.get("tags", {})
        name = _extract_name(tags)
        if not name:
            continue
        latlon = _element_latlon(el)
        if latlon is None or latlon[0] is None or latlon[1] is None:
            continue
        if name in out:
            continue
        entry = {
            "lat": float(latlon[0]),
            "lon": float(latlon[1]),
            "kind": _kind_for(tags),
            "osm_id": f"{el.get('type')}/{el.get('id')}",
        }
        addr_street = tags.get("addr:street")
        if addr_street:
            entry["on_street"] = addr_street
        out[name] = entry
    return out


def fetch_pois(
    bbox: tuple[float, float, float, float] = LONDON_BBOX,
    cache_path: Path | str | None = DEFAULT_CACHE_PATH,
    overpass_url: str = DEFAULT_OVERPASS_URL,
    force_refresh: bool = False,
) -> dict[str, dict]:
    """
    Fetch (or load from cache) a normalised POI dict.

    The cached file is shaped::

        {
          "fetched_at": <unix-ts>,
          "bbox": [south, west, north, east],
          "pois": { "NAME UPPER": {lat, lon, kind, osm_id, on_street?}, ... }
        }

    Cache-invalidation is deliberate: if you change the bbox or want a
    fresh harvest, pass ``force_refresh=True`` (or delete the file).
    """
    cache_path = Path(cache_path) if cache_path else None

    if cache_path and cache_path.exists() and not force_refresh:
        try:
            blob = json.loads(cache_path.read_text())
            cached_bbox = tuple(blob.get("bbox") or [])
            if cached_bbox == tuple(bbox):
                return blob.get("pois", {})
        except Exception:
            pass  # fall through to a fresh fetch

    query = _build_query(bbox)
    payload = _fetch_overpass(query, overpass_url)
    pois = parse_overpass(payload)

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "fetched_at": int(time.time()),
            "bbox": list(bbox),
            "pois": pois,
        }, indent=2, sort_keys=True))

    return pois


def merge_with_overrides(overrides: dict, osm_pois: dict) -> dict:
    """
    Return a new dict where curated *overrides* take precedence over OSM POIs.

    The Gazetteer treats overrides as authoritative because they represent
    human corrections to OSM noise — we must not let a freshly-harvested
    OSM entry silently shadow a fix.
    """
    merged = dict(osm_pois)
    for key, value in (overrides or {}).items():
        merged[key.upper()] = value
    return merged


def kind_counts(pois: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for v in pois.values():
        k = v.get("kind", "misc")
        counts[k] = counts.get(k, 0) + 1
    return counts
