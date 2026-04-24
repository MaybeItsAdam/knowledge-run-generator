"""
POI gazetteer and semantic snap.

Previously the pipeline treated ``poi_overrides.json`` as a flat ``name → [lat,
lon]`` map and snapped every POI to the geometrically nearest graph node. That
produced two recurring map-point bugs:

  1. POIs adjacent to a dual carriageway (motorway, A-road trunk) snapped to
     the wrong side of the central reservation — taxis physically cannot make
     the U-turn, so the router either failed or produced absurd detours.
  2. Stations snapped to the platform-centre node, stranded on a pedestrian
     segment of the graph that has no drive connection.

The gazetteer fixes both by:

  * Supporting an enriched POI schema with optional ``on_street`` /
    ``approach_from`` hints.
  * A ``semantic_snap`` that prefers a node on the hinted street, then any
     node on a drive-permitted edge, and only falls back to the geometric
    nearest node when nothing better is available.

The old ``[lat, lon]`` schema is still accepted transparently, so existing
``poi_overrides.json`` files work unchanged.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .aliases import AliasIndex

_POSTCODE_SUFFIX_RE = re.compile(r"\s+[A-Z]{1,2}\d{1,2}[A-Z]?\s*$")

# Highway classes we avoid snapping to when a better option exists. Dual
# carriageways and slip roads are the sources of the "wrong side" bug.
_AVOID_HIGHWAY_CLASSES = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
}


@dataclass(frozen=True)
class GazetteerEntry:
    canonical_name: str
    lat: float
    lon: float
    snapped_node: int
    snap_distance_m: float
    on_street: str | None = None
    approach_node: int | None = None
    source: str = "override"


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _edge_highway_class(edge_data: dict) -> str:
    hwy = edge_data.get("highway")
    if isinstance(hwy, (list, tuple)) and hwy:
        hwy = hwy[0]
    return str(hwy or "")


def _node_has_drive_edge(G, node: int) -> bool:
    """True if *node* is touched by at least one non-avoided drivable edge."""
    try:
        for _, _, data in G.edges(node, data=True):
            klass = _edge_highway_class(data)
            if klass and klass not in _AVOID_HIGHWAY_CLASSES:
                return True
        for _, _, data in G.in_edges(node, data=True):
            klass = _edge_highway_class(data)
            if klass and klass not in _AVOID_HIGHWAY_CLASSES:
                return True
    except Exception:
        pass
    return False


def _parse_override(value: Any) -> dict | None:
    """Normalise a raw ``poi_overrides.json`` value into a dict.

    Accepted forms:
      * ``[lat, lon]`` or ``(lat, lon)``
      * ``{"lat": ..., "lon": ..., "on_street"?: ..., "approach_from"?: ...}``
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return {"lat": float(value[0]), "lon": float(value[1])}
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        if "lat" not in value or "lon" not in value:
            return None
        try:
            out = {"lat": float(value["lat"]), "lon": float(value["lon"])}
        except (TypeError, ValueError):
            return None
        for k in ("on_street", "approach_from"):
            if value.get(k):
                out[k] = str(value[k])
        return out
    return None


class Gazetteer:
    """
    Resolves human-readable place names into a ``GazetteerEntry`` containing a
    graph node chosen by :func:`semantic_snap`.

    Two lookup paths:
      1. ``overrides`` — the manually curated dict (existing
         ``poi_overrides.json``). Matched exactly, then with the postcode
         suffix stripped. This is the authoritative source.
      2. ``osm_pois`` — harvested from OpenStreetMap via
         :mod:`knowledge_run_generator.osm_pois`. Fills in the long tail
         (pubs, stations, theatres, ...) that would otherwise need a
         manual override each time a new one shows up.

    Overrides always win over OSM POIs so human corrections can't be
    silently overwritten by a fresh OSM harvest.

    Resolution is cached per graph so repeated lookups in a pipeline run are
    near-free.
    """

    def __init__(
        self,
        overrides: dict | None = None,
        alias_index: AliasIndex | None = None,
        osm_pois: dict | None = None,
    ):
        self._overrides_raw: dict[str, Any] = overrides or {}
        self._overrides: dict[str, dict] = {}
        for key, value in self._overrides_raw.items():
            parsed = _parse_override(value)
            if parsed is not None:
                self._overrides[key.upper()] = parsed

        self._osm_pois: dict[str, dict] = {}
        for key, value in (osm_pois or {}).items():
            parsed = _parse_override(value)
            if parsed is not None:
                self._osm_pois[key.upper()] = parsed

        self.alias_index = alias_index
        self._resolve_cache: dict[tuple[int, str], GazetteerEntry | None] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup_coords(self, address: str) -> dict | None:
        """Return the raw coord dict for *address* if one exists.

        Overrides win over OSM POIs. The postcode suffix is stripped
        on the fallback in both tables.
        """
        if not address:
            return None
        upper = address.upper()

        if upper in self._overrides:
            return self._tag_source(dict(self._overrides[upper]), "override")
        no_pc = _POSTCODE_SUFFIX_RE.sub("", upper).strip()
        if no_pc != upper and no_pc in self._overrides:
            return self._tag_source(dict(self._overrides[no_pc]), "override")

        if upper in self._osm_pois:
            return self._tag_source(dict(self._osm_pois[upper]), "osm")
        if no_pc != upper and no_pc in self._osm_pois:
            return self._tag_source(dict(self._osm_pois[no_pc]), "osm")

        return None

    @staticmethod
    def _tag_source(record: dict, source: str) -> dict:
        record["_source"] = source
        return record

    def resolve(self, address: str, G) -> GazetteerEntry | None:
        """Resolve *address* → ``GazetteerEntry`` using semantic snap."""
        if not address:
            return None
        cache_key = (id(G), address.upper())
        if cache_key in self._resolve_cache:
            return self._resolve_cache[cache_key]

        record = self.lookup_coords(address)
        if record is None:
            self._resolve_cache[cache_key] = None
            return None

        lat = record["lat"]
        lon = record["lon"]
        on_street = record.get("on_street")
        approach_from = record.get("approach_from")

        snapped_node, snap_m = semantic_snap(
            G,
            lat,
            lon,
            on_street=on_street,
            alias_index=self.alias_index,
        )

        approach_node: int | None = None
        if approach_from and self.alias_index is not None:
            approach_candidates = self.alias_index.nodes_for(approach_from)
            approach_node = _closest_node(G, lat, lon, approach_candidates)

        entry = GazetteerEntry(
            canonical_name=address.upper(),
            lat=lat,
            lon=lon,
            snapped_node=snapped_node,
            snap_distance_m=snap_m,
            on_street=on_street,
            approach_node=approach_node,
            source=record.get("_source", "override"),
        )
        self._resolve_cache[cache_key] = entry
        return entry


# ---------------------------------------------------------------------------
# Semantic snap
# ---------------------------------------------------------------------------

def _closest_node(G, lat: float, lon: float, candidates) -> int | None:
    best: int | None = None
    best_d = float("inf")
    for nid in candidates:
        if nid not in G.nodes:
            continue
        n = G.nodes[nid]
        d = _haversine(lat, lon, n["y"], n["x"])
        if d < best_d:
            best_d = d
            best = nid
    return best


def semantic_snap(
    G,
    lat: float,
    lon: float,
    on_street: str | None = None,
    alias_index: AliasIndex | None = None,
    max_street_radius_m: float = 250.0,
    max_drivable_radius_m: float = 400.0,
) -> tuple[int, float]:
    """
    Snap (lat, lon) to a graph node with context awareness.

    Resolution order:

      1. If ``on_street`` is given and we have an alias index, find the
         closest node lying on that street. Bail if nothing is within
         ``max_street_radius_m`` — we'd rather fall through than snap to the
         wrong instance of a repeated street name.
      2. Snap to the nearest node that sits on a drive-permitted edge whose
         highway class is not in :data:`_AVOID_HIGHWAY_CLASSES` (i.e., not a
         motorway/trunk/slip). This is what eliminates the "wrong side of
         the dual carriageway" class of failures.
      3. Geometric nearest node (legacy behaviour).

    Returns ``(node_id, snap_distance_metres)``.
    """
    # 1. on_street hint
    if on_street and alias_index is not None:
        nodes = alias_index.nodes_for(on_street)
        if nodes:
            best = _closest_node(G, lat, lon, nodes)
            if best is not None:
                n = G.nodes[best]
                d = _haversine(lat, lon, n["y"], n["x"])
                if d <= max_street_radius_m:
                    return best, d

    # Fall-throughs iterate the whole graph once; cache a BallTree for speed.
    tree, ids = _get_ball_tree(G)
    import numpy as np

    # 2. Prefer drivable non-avoided nodes within a reasonable radius
    target = np.deg2rad([[lat, lon]])
    # query the k nearest then filter; k=25 is enough for dense London
    dists, idxs = tree.query(target, k=min(25, len(ids)))
    best_id: int | None = None
    best_d = float("inf")
    for dist_rad, idx in zip(dists[0], idxs[0]):
        nid = int(ids[idx])
        d = dist_rad * 6_371_000
        if d > max_drivable_radius_m:
            break
        if _node_has_drive_edge(G, nid):
            best_id = nid
            best_d = d
            break
    if best_id is not None:
        return best_id, best_d

    # 3. Absolute nearest node (may be on a motorway/trunk; caller will see the
    #    distance and can warn).
    nid = int(ids[idxs[0][0]])
    n = G.nodes[nid]
    d = _haversine(lat, lon, n["y"], n["x"])
    return nid, d


# Cached BallTree per graph instance — mirrors the existing geocoder cache but
# lives here so the snap logic is self-contained.
_graph_trees: dict[int, tuple[Any, Any]] = {}


def _get_ball_tree(G):
    import numpy as np
    import osmnx as ox
    from sklearn.neighbors import BallTree

    gid = id(G)
    cached = _graph_trees.get(gid)
    if cached is not None:
        return cached
    nodes = ox.graph_to_gdfs(G, edges=False, node_geometry=False)[["x", "y"]]
    nodes_rad = np.deg2rad(nodes[["y", "x"]].values)
    tree = BallTree(nodes_rad, metric="haversine")
    _graph_trees[gid] = (tree, nodes.index.values)
    return _graph_trees[gid]


def load_overrides(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

@dataclass
class PreflightReport:
    ok: bool
    reasons: list = None
    warnings: list = None
    start_snap_m: float | None = None
    end_snap_m: float | None = None
    unresolved_streets: list = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []
        if self.warnings is None:
            self.warnings = []
        if self.unresolved_streets is None:
            self.unresolved_streets = []


def preflight_run(
    start_entry: GazetteerEntry | None,
    end_entry: GazetteerEntry | None,
    intermediate_streets: list,
    alias_index: AliasIndex | None,
    max_snap_m: float = 50.0,
    warn_snap_m: float = 20.0,
) -> PreflightReport:
    """
    Sanity-check a run *before* we spend time routing it.

    Fails the run (``ok = False``) when:
      * the start or end didn't resolve through the gazetteer, or
      * the snap distance is ``> max_snap_m`` (the POI probably points at the
        wrong side of a motorway or onto a pedestrian-only platform).

    Warns (``ok = True`` with entries in ``warnings``) when:
      * the snap distance is ``> warn_snap_m``, or
      * an intermediate street can't be resolved by the alias index.

    The distinction matters for triage: failures should be fixed in
    ``poi_overrides.json``; warnings are usually OK but flag runs to audit.
    """
    report = PreflightReport(ok=True)

    if start_entry is None:
        report.ok = False
        report.reasons.append("start not resolved by gazetteer")
    else:
        report.start_snap_m = start_entry.snap_distance_m
        if start_entry.snap_distance_m > max_snap_m:
            report.ok = False
            report.reasons.append(
                f"start snapped {start_entry.snap_distance_m:.0f}m away (>{max_snap_m:.0f})"
            )
        elif start_entry.snap_distance_m > warn_snap_m:
            report.warnings.append(
                f"start snap distance {start_entry.snap_distance_m:.0f}m"
            )

    if end_entry is None:
        report.ok = False
        report.reasons.append("end not resolved by gazetteer")
    else:
        report.end_snap_m = end_entry.snap_distance_m
        if end_entry.snap_distance_m > max_snap_m:
            report.ok = False
            report.reasons.append(
                f"end snapped {end_entry.snap_distance_m:.0f}m away (>{max_snap_m:.0f})"
            )
        elif end_entry.snap_distance_m > warn_snap_m:
            report.warnings.append(
                f"end snap distance {end_entry.snap_distance_m:.0f}m"
            )

    if alias_index is not None and intermediate_streets:
        unresolved = [s for s in intermediate_streets if alias_index.resolve(s) is None]
        report.unresolved_streets = unresolved
        if unresolved:
            report.warnings.append(
                f"{len(unresolved)} unresolved streets: {unresolved[:5]}"
                + ("..." if len(unresolved) > 5 else "")
            )

    return report
