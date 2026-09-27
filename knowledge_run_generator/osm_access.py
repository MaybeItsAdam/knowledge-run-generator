"""
Road access points for area endpoints: station entrances, park gates,
building entrances.

A station, park or museum is a footprint, and every geocoder hands back a
point somewhere inside it: the platform centre, the middle of the lawn, the
roof. Snapping that point to the nearest drivable node measures the wrong
thing. Clissold Park's centre is 210 m from the nearest road, Farringdon
station's 160 m, so preflight (rightly) refused them, and where it did not the
snap landed on whichever road happened to be nearest the centre, not where a
cab sets down.

A cabbie sets down at a way in. This module harvests those from OSM once
(``krg osm-access``, committed to ``constants/osm_access.json`` like the POI
harvest) and :func:`best_access_point` picks, for a resolved endpoint, the way
in nearest the place that is also on a road.

Record shape, keyed by OSM node id::

    {"lat", "lon", "kind": "station_entrance" | "entrance" | "gate",
     "parents": ["CLISSOLD PARK", ...],   # names of the features it is part of
     "name": "..."}                       # the node's own name, if any
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from .aliases import normalise
from .osm_pois import LONDON_BBOX, _fetch_overpass, DEFAULT_OVERPASS_URL

DEFAULT_ACCESS_PATH = Path(__file__).resolve().parent.parent / "constants" / "osm_access.json"

# Features whose ways in are harvested. Parks for gates; stations and the
# building-type endpoints the Blue Book uses (museums, hospitals, theatres,
# churches, courts, prisons, colleges) for entrances.
_PARENT_WAYS = (
    '["leisure"~"^(park|garden|common|recreation_ground)$"]["name"]',
    '["landuse"="recreation_ground"]["name"]',
    '["railway"="station"]',
    '["public_transport"="station"]',
    '["building"="train_station"]',
    '["tourism"~"^(museum|attraction|gallery)$"]["name"]',
    '["amenity"~"^(hospital|place_of_worship|theatre|courthouse|prison|university|college|townhall|bus_station|exhibition_centre|arts_centre)$"]["name"]',
    '["historic"]["name"]',
    '["leisure"~"^(stadium|sports_centre)$"]["name"]',
)
_ACCESS_NODE_FILTERS = (
    ('["entrance"]', "entrance"),
    ('["barrier"~"^(gate|entrance|kissing_gate|lift_gate)$"]', "gate"),
)


def _query(bbox) -> str:
    s, w, n, e = bbox
    box = f"({s},{w},{n},{e})"
    ways = "\n".join(f"  way{f}{box};" for f in _PARENT_WAYS)
    rels = "\n".join(f"  rel{f}{box};" for f in _PARENT_WAYS[:2])
    access = "\n".join(f"  node(w.all){f};" for f, _kind in _ACCESS_NODE_FILTERS)
    return f"""[out:json][timeout:600];
(
{ways}
)->.w;
(
{rels}
)->.r;
way(r.r)->.rw;
(.w; .rw;)->.all;
(
{access}
  node["railway"~"^(subway_entrance|train_station_entrance)$"]{box};
);
out;
"""


def parse_access(payload: dict) -> dict[str, dict]:
    """Overpass JSON -> ``{node_id: record}``.

    ``out tags`` on the parent ways does not list their nodes, so parents are
    attached by :func:`fetch_access`'s second pass; this parser records the
    node and the standalone station entrances.
    """
    out: dict[str, dict] = {}
    for el in payload.get("elements", []):
        if el.get("type") != "node":
            continue
        tags = el.get("tags") or {}
        if tags.get("railway") in ("subway_entrance", "train_station_entrance"):
            kind = "station_entrance"
        elif "entrance" in tags:
            kind = "entrance"
        else:
            kind = "gate"
        if tags.get("access") in ("private", "no"):
            continue
        rec = {"lat": float(el["lat"]), "lon": float(el["lon"]), "kind": kind, "parents": []}
        if tags.get("name"):
            rec["name"] = str(tags["name"]).upper()
        out[str(el["id"])] = rec
    return out


def fetch_access(bbox=LONDON_BBOX, cache_path: Path | str | None = DEFAULT_ACCESS_PATH,
                 overpass_url: str = DEFAULT_OVERPASS_URL, force_refresh: bool = False,
                 progress: bool = True) -> dict[str, dict]:
    """Harvest (or load) the access points. Two Overpass requests: the access
    nodes, then their parent features with node lists, to name each node's
    parents."""
    cache_path = Path(cache_path) if cache_path else None
    if cache_path and cache_path.exists() and not force_refresh:
        blob = json.loads(cache_path.read_text())
        if tuple(blob.get("bbox") or []) == tuple(bbox):
            return blob.get("access", {})

    if progress:
        print("  [osm-access] fetching entrances and gates ...", flush=True)
    payload = _fetch_overpass(_query(bbox), overpass_url)
    access = parse_access(payload)

    # Parents: re-query the parent ways/relations of the harvested nodes with
    # their node lists (``out body`` on this small set only).
    ids = ",".join(k for k, v in access.items() if v["kind"] != "station_entrance")
    if ids:
        if progress:
            print(f"  [osm-access] naming parents of {len(access)} nodes ...", flush=True)
        q = f"""[out:json][timeout:600];
node(id:{ids})->.acc;
way(bn.acc)->.p;
.p out body;
rel(bw.p)["name"]["leisure"];
out body;
"""
        parents = _fetch_overpass(q, overpass_url)
        way_names: dict[int, str] = {}
        way_members: dict[int, list[int]] = {}
        for el in parents.get("elements", []):
            tags = el.get("tags") or {}
            if el.get("type") == "way":
                way_members[el["id"]] = el.get("nodes") or []
                if tags.get("name"):
                    way_names[el["id"]] = tags["name"]
        for el in parents.get("elements", []):
            if el.get("type") == "relation" and (el.get("tags") or {}).get("name"):
                for m in el.get("members") or []:
                    if m.get("type") == "way":
                        way_names.setdefault(m["ref"], el["tags"]["name"])
        for wid, members in way_members.items():
            name = way_names.get(wid)
            if not name:
                continue
            for nid in members:
                rec = access.get(str(nid))
                if rec is not None and name.upper() not in rec["parents"]:
                    rec["parents"].append(name.upper())

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "fetched_at": int(time.time()),
            "bbox": list(bbox),
            "source": "OpenStreetMap via Overpass (ODbL). Built by `krg osm-access`.",
            "access": access,
        }, indent=0, sort_keys=True))
    return access


def load_access(*candidates: Path | str | None) -> dict[str, dict]:
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists():
            try:
                return json.loads(path.read_text()).get("access", {})
            except Exception as exc:  # noqa: BLE001
                print(f"  Warning: could not load {path}: {exc}")
    return {}


def _haversine(lat1, lon1, lat2, lon2):
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class AccessIndex:
    """Lookup over the harvest: ways in by parent name, and station entrances
    by position."""

    def __init__(self, access: dict[str, dict] | None):
        self._by_parent: dict[str, list[dict]] = {}
        self._station_entrances: list[dict] = []
        for rec in (access or {}).values():
            if rec.get("kind") == "station_entrance":
                self._station_entrances.append(rec)
            for parent in rec.get("parents") or []:
                self._by_parent.setdefault(_identity(parent), []).append(rec)

    def __len__(self) -> int:
        return len(self._station_entrances) + sum(len(v) for v in self._by_parent.values())

    def candidates(self, name: str, lat: float, lon: float, station: bool,
                   parent_radius_m: float = 1500.0,
                   station_radius_m: float = 250.0) -> list[dict]:
        """Ways in for the place *name* resolved at (*lat*, *lon*).

        A way in belongs to the place when it is part of an OSM feature of the
        same name within ``parent_radius_m`` (``station_radius_m`` for a
        station). For a station, the standalone ``railway=subway_entrance``
        nodes within ``station_radius_m`` count too; OSM rarely ties those to
        the station by name.
        """
        out = []
        # A station's ways in are at the station: a park or estate sharing its
        # name (Wandsworth Common) has gates a kilometre away.
        radius = station_radius_m if station else parent_radius_m
        for rec in self._by_parent.get(_identity(name), []):
            if _haversine(lat, lon, rec["lat"], rec["lon"]) <= radius:
                out.append(rec)
        if station:
            for rec in self._station_entrances:
                if _haversine(lat, lon, rec["lat"], rec["lon"]) <= station_radius_m:
                    out.append(rec)
        return out


_STATION_WORDS = {"STATION", "STATIONS", "STN", "BR", "B_R", "RAIL", "RAILWAY",
                  "UNDERGROUND", "TUBE", "OVERGROUND", "DLR", "LONDON", "THE"}


def _identity(name: str) -> str:
    return " ".join(t for t in normalise(name).split() if t not in _STATION_WORDS)


def best_access_point(candidates, lat: float, lon: float, kerb_fn):
    """The way in a cab would use: of the ways in that front a road, the one
    nearest the place.

    ``kerb_fn(lat, lon)`` is the caller's kerb snap, returning a tuple whose
    last element is the distance a route's end node is from the set-down
    point, or ``None`` when the way in fronts no road. Returns
    ``(record, kerb_fn_result)`` or ``None``.
    """
    fronting = []
    for rec in candidates:
        kerb = kerb_fn(rec["lat"], rec["lon"])
        if kerb is not None:
            fronting.append((_haversine(lat, lon, rec["lat"], rec["lon"]), kerb[-1], rec, kerb))
    if not fronting:
        return None
    _d, _along, rec, kerb = min(fronting, key=lambda f: (f[0], f[1]))
    return rec, kerb
