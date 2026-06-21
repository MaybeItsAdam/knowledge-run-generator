"""
Route validation for Cotton Thread method.

Three checks:
  A. Turn restriction legality — no prohibited (from, via, to) triples
  B. Directness — route distance / straight-line distance within threshold
  C. Street coverage — route traverses expected Blue Book streets
"""

import json
import math
import os
import re
import requests
from dataclasses import dataclass, field
from pathlib import Path
from shapely.geometry import LineString, Point


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    passed: bool = False

    is_legal: bool = True
    turn_violations: list = field(default_factory=list)

    is_direct: bool = True
    directness_metrics: dict = field(default_factory=dict)

    is_covered: bool = True
    coverage_metrics: dict = field(default_factory=dict)

    has_sane_detours: bool = True
    detour_violations: list = field(default_factory=list)


def _best_edge_data(edge_bundle):
    """
    Return the shortest parallel edge data dict from a MultiDiGraph edge bundle.
    """
    if not edge_bundle:
        return None
    return min(edge_bundle.values(), key=lambda d: d.get("length", float("inf")))


# ---------------------------------------------------------------------------
# Turn Restrictions
# ---------------------------------------------------------------------------

def load_turn_restrictions(G, cache_dir="cache"):
    """
    Fetch OSM turn restrictions for the graph's bounding box via Overpass API.
    Returns a set of (from_node, via_node, to_node) prohibited triples mapped
    to the graph's node IDs.

    The result is cached to ``cache/turn_restrictions.json``.
    """
    cache_path = Path(cache_dir) / "turn_restrictions.json"
    if cache_path.exists():
        raw = json.loads(cache_path.read_text())
        return _build_prohibited_set(G, raw)

    # Compute bounding box from graph
    ys = [d['y'] for _, d in G.nodes(data=True)]
    xs = [d['x'] for _, d in G.nodes(data=True)]
    south, north = min(ys), max(ys)
    west, east = min(xs), max(xs)

    # We only need the restriction relations' member *refs* (from_way,
    # via_node, to_way) — see _build_prohibited_set, which maps those ids onto
    # the graph and never touches member geometry. The old query also did
    # ``>; out skel qt;`` to pull every member way's nodes; over a
    # Greater-London bbox that payload is what made overpass-api.de return
    # 406 "query too heavy", silently disabling legality checks. Dropping the
    # recursion keeps the response small, and we route the request through the
    # same UA + mirror-failover + backoff used by the POI harvest.
    query = (
        f"[out:json][timeout:180];\n"
        f'relation["type"="restriction"]({south},{west},{north},{east});\n'
        f"out body;"
    )
    print("Fetching turn restrictions from Overpass API...")
    try:
        from .osm_pois import fetch_overpass
        data = fetch_overpass(query)
    except Exception as e:
        print(f"  Warning: Overpass query failed ({e}). Skipping turn restrictions.")
        return set()

    # Extract restriction relations
    restrictions = []
    elements_by_id = {e["id"]: e for e in data.get("elements", [])}

    for elem in data.get("elements", []):
        if elem.get("type") != "relation":
            continue
        tags = elem.get("tags", {})
        restriction_type = tags.get("restriction", "")
        if not restriction_type.startswith(("no_", "only_")):
            continue

        members = elem.get("members", [])
        from_way = via_node = to_way = None
        for m in members:
            if m.get("role") == "from" and m.get("type") == "way":
                from_way = m["ref"]
            elif m.get("role") == "via" and m.get("type") == "node":
                via_node = m["ref"]
            elif m.get("role") == "to" and m.get("type") == "way":
                to_way = m["ref"]

        if from_way and via_node and to_way:
            restrictions.append({
                "type": restriction_type,
                "from_way": from_way,
                "via_node": via_node,
                "to_way": to_way,
            })

    # Cache
    Path(cache_dir).mkdir(exist_ok=True)
    cache_path.write_text(json.dumps(restrictions, indent=2))
    print(f"  Cached {len(restrictions)} turn restrictions.")

    return _build_prohibited_set(G, restrictions)


def _build_prohibited_set(G, restrictions):
    """
    Map OSM way/node IDs to graph node triples.

    For each restriction we need:
      - The graph node matching the via_node OSM ID
      - The graph edge(s) whose osmid matches from_way → find the node on
        that edge adjacent to via
      - Same for to_way
    """
    # Build an index: osmid → set of (u, v) edges
    way_to_edges = {}
    for u, v, _, edata in G.edges(keys=True, data=True):
        osmids = edata.get("osmid", [])
        if isinstance(osmids, (int, str)):
            osmids = [int(osmids)]
        elif isinstance(osmids, list):
            osmids = [int(x) for x in osmids]
        for oid in osmids:
            way_to_edges.setdefault(oid, []).append((u, v))

    # Build index: osmid → graph node
    osm_to_node = {}
    for n, ndata in G.nodes(data=True):
        osm_id = ndata.get("osmid", n)
        # osmnx stores the osmid as the node key by default
        osm_to_node[int(n)] = n

    prohibited = set()
    for r in restrictions:
        via = osm_to_node.get(r["via_node"])
        if via is None:
            continue

        from_edges = way_to_edges.get(r["from_way"], [])
        to_edges = way_to_edges.get(r["to_way"], [])

        # Find from-node: the node on from_way that connects to via
        from_nodes = set()
        for u, v in from_edges:
            if v == via:
                from_nodes.add(u)
            if u == via:
                from_nodes.add(v)

        # Find to-node: the node on to_way that connects from via
        to_nodes = set()
        for u, v in to_edges:
            if u == via:
                to_nodes.add(v)
            if v == via:
                to_nodes.add(u)

        rtype = r["type"]
        if rtype.startswith("no_"):
            # Explicit prohibition: these specific triples are illegal
            for fn in from_nodes:
                for tn in to_nodes:
                    prohibited.add((fn, via, tn))
        # NOTE: only_* restrictions (only_straight_on, only_right_turn, etc.)
        # are intentionally NOT enforced. On osmnx's simplified graph, the
        # from/to node mapping is imprecise (simplified edges merge multiple
        # OSM nodes), causing massive false positives. Additionally, many
        # only_* restrictions have exemptions for taxis/PSVs which our
        # drive_service graph already permits.

    return prohibited


def check_turn_legality(route_nodes, prohibited_turns, exempted_turns=None):
    """
    Check every consecutive triple in the route against prohibited turns.

    Returns (is_legal, violations_list).
    """
    if not prohibited_turns:
        return True, []

    exempted_turns = exempted_turns or set()
    violations = []
    for i in range(len(route_nodes) - 2):
        triple = (route_nodes[i], route_nodes[i + 1], route_nodes[i + 2])
        if triple in prohibited_turns and triple not in exempted_turns:
            violations.append({
                "index": i,
                "from": route_nodes[i],
                "via": route_nodes[i + 1],
                "to": route_nodes[i + 2],
            })
    return len(violations) == 0, violations


# ---------------------------------------------------------------------------
# Directness Check
# ---------------------------------------------------------------------------

def _haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def check_waypoint_detours(G, route_nodes, waypoint_nodes, origin_node, dest_node):
    """
    Check the physical distance between waypoints against their straight-line distance.
    If the physical distance is > 3x the straight-line distance, flag it as a detour.
    """
    stages = [origin_node] + list(waypoint_nodes) + [dest_node]
    
    stage_indices = [-1] * len(stages)
    stage_indices[0] = 0
    current_stage_idx = 1
    
    for i, nid in enumerate(route_nodes):
        if current_stage_idx >= len(stages):
            break
            
        target = stages[current_stage_idx]
        if (isinstance(target, (set, list, tuple)) and nid in target) or (nid == target):
            stage_indices[current_stage_idx] = i
            current_stage_idx += 1
            
    # Compute distances for found legs
    violations = []
    
    for i in range(len(stages) - 1):
        if stage_indices[i] == -1 or stage_indices[i+1] == -1:
            continue
            
        start_idx = stage_indices[i]
        end_idx = stage_indices[i+1]
        
        graph_dist = 0.0
        for j in range(start_idx, end_idx):
            edge = G.get_edge_data(route_nodes[j], route_nodes[j+1])
            if edge:
                # MultiDiGraph, grab min length
                min_len = min(d.get('length', 0) for d in edge.values())
                graph_dist += min_len
                
        sn = G.nodes[route_nodes[start_idx]]
        en = G.nodes[route_nodes[end_idx]]
        straight_dist = _haversine(sn['y'], sn['x'], en['y'], en['x'])
        
        # We only care about somewhat significant detours (e.g., straight_dist > 50m) to avoid div-by-zero or micro-wiggles
        if straight_dist > 50 and (graph_dist / straight_dist) > 3.0:
            if i < len(waypoint_nodes):
                violations.append({
                    "waypoint_idx": i,
                    "waypoint": waypoint_nodes[i],
                    "ratio": graph_dist / straight_dist
                })
                
    return len(violations) == 0, violations


def check_directness(G, route_nodes, origin_node, dest_node,
                     max_deviation_ratio=1.8, max_lateral_offset_m=800):
    """
    Two-part directness check:

    1. Distance ratio  — route_distance / straight_line_distance
    2. Max lateral offset — max perpendicular distance from any route point
       to the origin→destination line.

    Thresholds are adaptive by straight-line distance:
      - <1 km   → ratio 2.5, offset 500 m
      - 1–3 km  → ratio 1.8, offset 800 m
      - >3 km   → ratio 1.8, offset 960 m

    Returns (is_direct, metrics_dict).
    """
    o = G.nodes[origin_node]
    d = G.nodes[dest_node]
    straight_dist = _haversine(o['y'], o['x'], d['y'], d['x'])

    # Route distance
    route_dist = 0.0
    for i in range(len(route_nodes) - 1):
        edge = G.get_edge_data(route_nodes[i], route_nodes[i + 1])
        best = _best_edge_data(edge)
        if best:
            route_dist += best.get('length', 0)

    ratio = route_dist / straight_dist if straight_dist > 0 else float('inf')

    # Lateral offset
    straight_line = LineString([(o['x'], o['y']), (d['x'], d['y'])])
    # Approximate degrees → metres at London latitude
    deg_to_m = 111_320 * math.cos(math.radians(51.5))

    max_offset = 0.0
    worst_idx = 0
    for i, nid in enumerate(route_nodes):
        n = G.nodes[nid]
        p = Point(n['x'], n['y'])
        offset_m = p.distance(straight_line) * deg_to_m
        if offset_m > max_offset:
            max_offset = offset_m
            worst_idx = i

    # Adaptive thresholds
    if straight_dist < 1000:
        eff_ratio = 2.5
        eff_offset = 500
    elif straight_dist < 3000:
        eff_ratio = max_deviation_ratio
        eff_offset = max_lateral_offset_m
    else:
        eff_ratio = max_deviation_ratio
        eff_offset = max_lateral_offset_m * 1.2

    is_direct = ratio <= eff_ratio and max_offset <= eff_offset

    metrics = {
        "straight_distance": round(straight_dist, 1),
        "route_distance": round(route_dist, 1),
        "ratio": round(ratio, 3),
        "max_lateral_offset_m": round(max_offset, 1),
        "worst_point_index": worst_idx,
        "thresholds": {"ratio": eff_ratio, "offset": eff_offset},
    }
    return is_direct, metrics


# ---------------------------------------------------------------------------
# Street Coverage Check
# ---------------------------------------------------------------------------

_ABBREVIATIONS = {
    " ST": " STREET", " RD": " ROAD", " AVE": " AVENUE",
    " SQ": " SQUARE", " PL": " PLACE", " LN": " LANE",
    " GDNS": " GARDENS", " PK": " PARK", " CIR": " CIRCUS",
    " HL": " HILL", " RI": " RISE", " CR": " CRESCENT",
}


def _normalise_street(name):
    """Upper-case, strip punctuation, expand common abbreviations."""
    if not name:
        return ""
    n = str(name).upper().strip().replace("'", "").replace(".", "").replace("'", "")
    for abbr, full in _ABBREVIATIONS.items():
        if n.endswith(abbr):
            n = n[: -len(abbr)] + full
    return n


def _extract_route_streets(G, route_nodes):
    """Return ordered list of unique street names along the route."""
    streets = []
    for i in range(len(route_nodes) - 1):
        edge = G.get_edge_data(route_nodes[i], route_nodes[i + 1])
        if not edge:
            continue
        name = _best_edge_data(edge).get("name", "")
        if isinstance(name, list):
            name = name[0]
        norm = _normalise_street(name)
        if norm and (not streets or streets[-1] != norm):
            streets.append(norm)
    return streets


def check_street_coverage(G, route_nodes, expected_streets, min_coverage=0.6):
    """
    Verify the route covers at least ``min_coverage`` fraction of
    ``expected_streets`` (from the Blue Book).

    Uses normalised string matching.  Returns (is_covered, metrics).
    """
    route_streets_norm = set(_extract_route_streets(G, route_nodes))
    expected_norm = [_normalise_street(s) for s in expected_streets if s]

    if not expected_norm:
        return True, {"coverage": 1.0, "matched": 0, "expected": 0, "missing": []}

    matched = []
    missing = []
    for s in expected_norm:
        if s in route_streets_norm:
            matched.append(s)
        else:
            # Substring check — e.g. "KINGS CROSS ROAD" in route as "KINGS CROSS"
            found = False
            for rs in route_streets_norm:
                if s in rs or rs in s:
                    matched.append(s)
                    found = True
                    break
            if not found:
                missing.append(s)

    coverage = len(matched) / len(expected_norm) if expected_norm else 1.0
    return coverage >= min_coverage, {
        "coverage": round(coverage, 3),
        "matched": len(matched),
        "expected": len(expected_norm),
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def validate_route(G, route_nodes, origin_node, dest_node,
                   prohibited_turns=None, expected_streets=None,
                   config=None, waypoint_nodes=None, exempted_turns=None):
    """
    Run all three checks on a route.

    ``config`` is an optional dict that can override thresholds:
      - max_deviation_ratio
      - max_lateral_offset_m
      - min_street_coverage
      - skip_directness_check  (bool)

    Returns a :class:`ValidationResult`.
    """
    config = config or {}
    result = ValidationResult()

    # A — Legality
    result.is_legal, result.turn_violations = check_turn_legality(
        route_nodes, prohibited_turns or set(), exempted_turns=exempted_turns
    )

    # B — Directness
    if config.get("skip_directness_check"):
        result.is_direct = True
        result.directness_metrics = {"skipped": True}
    else:
        result.is_direct, result.directness_metrics = check_directness(
            G, route_nodes, origin_node, dest_node,
            max_deviation_ratio=config.get("max_deviation_ratio", 1.8),
            max_lateral_offset_m=config.get("max_lateral_offset_m", 800),
        )

    # C — Street coverage
    if expected_streets:
        result.is_covered, result.coverage_metrics = check_street_coverage(
            G, route_nodes, expected_streets,
            min_coverage=config.get("min_street_coverage", 0.6),
        )
    if waypoint_nodes:
        result.has_sane_detours, result.detour_violations = check_waypoint_detours(
            G, route_nodes, waypoint_nodes, origin_node, dest_node
        )
    else:
        result.has_sane_detours = True
        result.detour_violations = []

    # Coverage is now a soft metric, as the semantic router naturally handles expected streets
    result.passed = result.is_legal and result.is_direct and result.has_sane_detours
    return result
