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
FALLBACK_OVERPASS_URLS: tuple[str, ...] = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
)
# The harvest is a committed input (the tier-3 gazetteer), not a scratch cache:
# a fresh clone must resolve stations/hospitals without hitting Overpass.
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "constants" / "osm_pois.json"

# Overpass blocks the default python-requests UA; a descriptive one is
# expected by their usage policy.
_OVERPASS_HEADERS = {
    "User-Agent": "knowledge-run-generator/0.1 (https://github.com/MaybeItsAdam/knowledge-run-generator)",
    "Accept": "application/json",
}

# Tag groups. Each (osm_key, regex-of-values, kind) triple becomes one
# Overpass filter. Kind is stored on the entry so callers can reason about
# snap hints (e.g. a station should prefer its on-street approach).
_TAG_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("amenity", "pub|restaurant|cafe|theatre|cinema|hospital|police|"
                "library|townhall|place_of_worship|bank|university|college|"
                "school|prison|courthouse|fire_station|marketplace|"
                "arts_centre|conference_centre|embassy",
     "amenity"),
    ("tourism", "hotel|museum|attraction|gallery|hostel|zoo", "tourism"),
    # Stations are the single biggest category of Blue Book run endpoints that
    # isn't in the Points List. `halt` catches the smaller National Rail stops,
    # and bus_station catches coach stations (e.g. Victoria Coach Station).
    ("railway", "station|halt", "station"),
    ("public_transport", "station", "station"),
    ("amenity", "bus_station", "station"),
    ("aeroway", "terminal", "station"),
    ("leisure", "stadium|sports_centre|park", "leisure"),
    ("historic", "memorial|monument|castle", "historic"),
    ("shop", "department_store|mall", "shop"),
    ("office", "diplomatic", "office"),
)


def _build_query(
    bbox: tuple[float, float, float, float],
    groups: Iterable[tuple[str, str, str]] | None = None,
    timeout_s: int = 180,
) -> str:
    """Build an Overpass QL query.

    Restricting to a subset of ``groups`` lets the fetcher chunk a heavy
    London-wide harvest into per-category requests, which reliably
    avoids the 406 "query too complex" response that combining them all
    into one request can trigger.
    """
    south, west, north, east = bbox
    active = tuple(groups) if groups is not None else _TAG_GROUPS
    clauses: list[str] = []
    for key, values, _kind in active:
        clauses.append(
            f'  node["{key}"~"{values}"]["name"]({south},{west},{north},{east});'
        )
        clauses.append(
            f'  way["{key}"~"{values}"]["name"]({south},{west},{north},{east});'
        )
    body = "\n".join(clauses)
    # ``out center tags`` is the smallest output that still gives us
    # name + coordinates for ways; ``body`` would pull every node of
    # every way, which is what pushes the response past the server cap.
    return f"[out:json][timeout:{timeout_s}];\n(\n{body}\n);\nout center tags;"


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


def fetch_overpass(query: str, url: str = DEFAULT_OVERPASS_URL, retries: int = 3) -> dict:
    """Public wrapper over :func:`_fetch_overpass` for reuse by other modules
    (e.g. the turn-restriction fetcher) that want the same UA + mirror
    failover + backoff behaviour."""
    return _fetch_overpass(query, url, retries=retries)


def _fetch_overpass(query: str, url: str, retries: int = 3) -> dict:
    """Send *query* to Overpass, failing over to mirrors on 406/429/5xx.

    The main overpass-api.de mirror returns 406 Not Acceptable when a
    query is too heavy, the UA is flagged, or the server is under
    pressure. We retry with a public fallback mirror before giving up.
    """
    import requests

    endpoints = [url] + [u for u in FALLBACK_OVERPASS_URLS if u != url]
    last_err: Exception | None = None
    last_body: str | None = None

    for endpoint in endpoints:
        for attempt in range(retries):
            try:
                resp = requests.post(
                    endpoint,
                    data={"data": query},
                    headers=_OVERPASS_HEADERS,
                    timeout=240,
                )
                if resp.status_code == 200:
                    return resp.json()
                last_err = RuntimeError(
                    f"{endpoint} -> HTTP {resp.status_code}"
                )
                last_body = resp.text[:400] if resp.text else None
                # 406/429/5xx are all worth a mirror failover; 400 is a
                # client bug we shouldn't retry.
                if resp.status_code == 400:
                    break
            except Exception as exc:
                last_err = exc
            time.sleep(2 ** attempt)

    raise RuntimeError(
        f"Overpass fetch failed across {len(endpoints)} endpoint(s): {last_err}"
        + (f"\nLast body: {last_body}" if last_body else "")
    )


def _element_latlon(el: dict) -> tuple[float, float] | None:
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center")
    if center:
        return center.get("lat"), center.get("lon")
    return None


# How many same-named alternates to keep per name. London has a handful of
# genuinely repeated names; beyond a few the extras are noise.
MAX_ALTERNATES = 4


def parse_overpass(payload: dict) -> dict[str, dict]:
    """Turn raw Overpass JSON into ``{NAME_UPPER: {lat, lon, kind, osm_id, ...}}``.

    Same-named places are common and meaningfully different: *Finsbury Park*
    is a station, a park and an area. Dropping all but the first silently
    decided which one every future lookup would get. The first occurrence is
    still the primary record — the query is bbox-ordered, so that's stable
    across runs — but the alternates ride along under ``_others`` so a caller
    can pick by kind. The shape stays ``name -> record``, so existing caches
    and consumers keep working.
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
        entry = {
            "lat": float(latlon[0]),
            "lon": float(latlon[1]),
            "kind": _kind_for(tags),
            "osm_id": f"{el.get('type')}/{el.get('id')}",
        }
        addr_street = tags.get("addr:street")
        if addr_street:
            entry["on_street"] = addr_street

        primary = out.get(name)
        if primary is None:
            out[name] = entry
            continue
        # Only keep an alternate that says something new about the place; a
        # second pub of the same name tells a lookup nothing the first didn't.
        others = primary.get("_others") or []
        if entry["kind"] == primary["kind"]:
            continue
        if len(others) < MAX_ALTERNATES and not any(
            o["kind"] == entry["kind"] for o in others
        ):
            primary.setdefault("_others", others).append(entry)
    return out


def fetch_pois(
    bbox: tuple[float, float, float, float] = LONDON_BBOX,
    cache_path: Path | str | None = DEFAULT_CACHE_PATH,
    overpass_url: str = DEFAULT_OVERPASS_URL,
    force_refresh: bool = False,
    progress: bool = True,
) -> dict[str, dict]:
    """
    Fetch (or load from cache) a normalised POI dict.

    Runs one Overpass request per tag group. Splitting is deliberate:
    the combined query for London-wide multi-category POIs is large
    enough that the main mirror has historically returned 406 ("query
    too heavy") on a single combined request. One small request per
    group keeps every individual fetch well under the server cap, and
    partial success still yields a useful gazetteer.

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

    all_pois: dict[str, dict] = {}
    failures: list[str] = []
    for group in _TAG_GROUPS:
        key, _values, kind = group
        if progress:
            print(f"  [osm] fetching {kind} ({key}) ...", flush=True)
        try:
            query = _build_query(bbox, groups=(group,))
            payload = _fetch_overpass(query, overpass_url)
            chunk = parse_overpass(payload)
            # curated-override semantics apply across chunks: first-writer wins.
            for name, record in chunk.items():
                all_pois.setdefault(name, record)
            if progress:
                print(f"    {len(chunk)} named", flush=True)
        except Exception as exc:
            failures.append(f"{kind}: {exc}")
            if progress:
                print(f"    FAILED: {exc}", flush=True)

    if not all_pois and failures:
        # Don't cache an empty result from a total outage.
        raise RuntimeError(
            "Overpass fetch failed for every tag group:\n  "
            + "\n  ".join(failures)
        )

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "fetched_at": int(time.time()),
            "bbox": list(bbox),
            "pois": all_pois,
            "partial_failures": failures,
        }, indent=2, sort_keys=True))

    return all_pois


def load_cached_pois(*candidates: Path | str | None) -> dict[str, dict]:
    """Return the POI dict from the first readable harvest cache.

    The default cache lives in ``/tmp``, which is wiped on reboot — and when it
    vanishes the gazetteer's OSM tier silently disappears with it, taking the
    station endpoints down to the geocoder. Callers pass the places a harvest
    might reasonably live (env var, output dir, ``constants/``, the /tmp cache)
    and take whichever exists.
    """
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            blob = json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001 - report and try the next one
            print(f"  Warning: could not load {path}: {exc}")
            continue
        pois = blob.get("pois") if isinstance(blob, dict) else None
        if pois:
            print(f"  {len(pois)} OSM POIs from {path}.")
            return pois
    return {}


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
