"""
Auto-correction for failed route validation.

When the validator flags a route, the corrector injects waypoints and re-routes
to fix turn-restriction violations, directness failures, and street-coverage gaps.
"""

import math
import networkx as nx
import osmnx as ox

from .aliases import normalise as _normalise_street


MAX_CORRECTION_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Correction strategies
# ---------------------------------------------------------------------------

def fix_turn_violation(G, route_nodes, violation):
    """
    Fix a single prohibited (from → via → to) turn by inserting a detour
    waypoint that avoids the via node.

    Strategy: find a neighbour of ``via`` (other than ``to``) that still
    reaches ``to`` via a short path, and return it as a waypoint.
    """
    via = violation["via"]
    to_node = violation["to"]
    from_node = violation["from"]

    # All successors of via except the prohibited to-node
    candidates = [n for n in G.successors(via) if n != to_node and n != from_node]
    if not candidates:
        return None

    # Pick the candidate that minimises total detour distance (via → candidate → to)
    best = None
    best_dist = float("inf")
    for c in candidates:
        try:
            d1 = nx.shortest_path_length(G, via, c, weight="length")
            d2 = nx.shortest_path_length(G, c, to_node, weight="length")
            total = d1 + d2
            if total < best_dist:
                best_dist = total
                best = c
        except nx.NetworkXNoPath:
            continue

    return best


def fix_directness(G, route_nodes, origin_node, dest_node, directness_metrics):
    """
    Pull the route back toward the straight line by injecting a waypoint at
    the point of maximum lateral deviation.

    Strategy: project the worst-deviation fraction onto the origin→dest line,
    snap that point to the road network, and return it as a waypoint.
    """
    o = G.nodes[origin_node]
    d = G.nodes[dest_node]

    worst_idx = directness_metrics.get("worst_point_index", len(route_nodes) // 2)
    fraction = worst_idx / max(len(route_nodes) - 1, 1)

    # Clamp fraction away from endpoints (injecting at 0% or 100% is useless)
    fraction = max(0.15, min(0.85, fraction))

    target_lon = o["x"] + fraction * (d["x"] - o["x"])
    target_lat = o["y"] + fraction * (d["y"] - o["y"])

    corrective_node = ox.distance.nearest_nodes(G, target_lon, target_lat)

    # Don't inject if it's the same as origin or dest
    if corrective_node in (origin_node, dest_node):
        return []

    return [corrective_node]


def fix_street_coverage(G, route_nodes, coverage_metrics, street_to_nodes,
                        origin_node, dest_node):
    """
    For each missing street, find the node on that street closest to the
    straight-line trajectory and inject it as a waypoint.

    Waypoints are returned in geographic order (sorted by distance from origin
    along the origin→dest line).
    """
    missing = coverage_metrics.get("missing", [])
    if not missing:
        return []

    o = G.nodes[origin_node]
    d = G.nodes[dest_node]

    # Direction vector origin → dest (for projection)
    dx = d["x"] - o["x"]
    dy = d["y"] - o["y"]
    line_len_sq = dx * dx + dy * dy
    if line_len_sq == 0:
        return []

    waypoints_with_proj = []
    for street in missing:
        # Must match how build_street_index keyed the index, or every
        # coverage fix silently finds no nodes.
        nodes = street_to_nodes.get(_normalise_street(street), set())
        if not nodes:
            continue

        # Find the node on this street closest to the straight line
        best_node = None
        best_dist = float("inf")
        best_proj = 0.0
        for nid in nodes:
            if nid not in G.nodes:
                continue
            n = G.nodes[nid]
            # Perpendicular distance to line (approximate, degrees)
            px, py = n["x"] - o["x"], n["y"] - o["y"]
            proj = (px * dx + py * dy) / line_len_sq  # scalar projection [0,1]
            # Only consider nodes roughly between origin and dest
            if proj < -0.1 or proj > 1.1:
                continue
            # Point on line at this projection
            lx = o["x"] + proj * dx
            ly = o["y"] + proj * dy
            dist = math.sqrt((n["x"] - lx) ** 2 + (n["y"] - ly) ** 2)
            if dist < best_dist:
                best_dist = dist
                best_node = nid
                best_proj = proj

        if best_node and best_node not in (origin_node, dest_node):
            waypoints_with_proj.append((best_proj, best_node))

    # Sort by projection (geographic order along the straight line)
    waypoints_with_proj.sort(key=lambda x: x[0])
    return [nid for _, nid in waypoints_with_proj]


# ---------------------------------------------------------------------------
# Merge waypoints
# ---------------------------------------------------------------------------

def _merge_waypoints_in_order(G, origin_node, dest_node, existing, new):
    """
    Merge ``new`` waypoints into ``existing`` list, sorted by distance from
    origin along the origin→dest line.  Removes duplicates.
    """
    o = G.nodes[origin_node]
    d = G.nodes[dest_node]
    dx = d["x"] - o["x"]
    dy = d["y"] - o["y"]
    line_len_sq = dx * dx + dy * dy

    def _repr_node_id(wp):
        if isinstance(wp, (set, list, tuple)):
            return next(iter(wp)) if wp else origin_node
        return wp

    def proj(wp):
        if line_len_sq == 0:
            return 0
        nid = _repr_node_id(wp)
        if nid not in G.nodes:
            return 0
        n = G.nodes[nid]
        px, py = n["x"] - o["x"], n["y"] - o["y"]
        return (px * dx + py * dy) / line_len_sq

    # Dedupe while supporting unhashable waypoint groups (e.g. roundabout node sets).
    combined = []
    seen = set()
    for wp in (existing + new):
        if isinstance(wp, set):
            key = ("set", tuple(sorted(wp)))
        elif isinstance(wp, list):
            key = ("list", tuple(wp))
        elif isinstance(wp, tuple):
            key = ("tuple", wp)
        else:
            key = ("node", wp)
        if key in seen:
            continue
        seen.add(key)
        combined.append(wp)

    combined.sort(key=proj)
    return combined


# ---------------------------------------------------------------------------
# Correction loop
# ---------------------------------------------------------------------------

def correct_and_validate(G, origin_node, dest_node, initial_waypoints,
                         expected_streets, prohibited_turns, street_to_nodes,
                         route_fn, validate_fn, config=None, exempted_turns=None):
    """
    Route → validate → correct loop.

    Parameters
    ----------
    route_fn : callable(G, origin_node, dest_node, waypoint_nodes) → (route_nodes, metadata)
    validate_fn : callable(G, route_nodes, origin_node, dest_node, prohibited_turns, expected_streets, config, waypoint_nodes, exempted_turns) → ValidationResult
    config : optional per-run overrides dict
    exempted_turns : optional set of (from, via, to) node triples that OSM
        marks prohibited but which are legal for this run (e.g. taxi/PSV
        exemptions). Forwarded to ``validate_fn``.

    Returns
    -------
    (route_nodes, validation_result, correction_log)
    """
    config = config or {}
    waypoints = list(initial_waypoints)
    best_route = None
    best_score = float("inf")
    best_result = None
    correction_log = []

    for attempt in range(MAX_CORRECTION_ATTEMPTS + 1):
        route_nodes, metadata = route_fn(G, origin_node, dest_node, waypoints)

        if not route_nodes or len(route_nodes) < 2:
            correction_log.append({"attempt": attempt, "error": "empty route"})
            break

        result = validate_fn(
            G, route_nodes, origin_node, dest_node,
            prohibited_turns, expected_streets, config, waypoints,
            exempted_turns,
        )

        # Track best by distance ratio or just fallback to the first attempt
        score = result.directness_metrics.get("ratio", metadata.get("total_distance", float("inf")))
        if score < best_score or best_route is None:
            best_score = score
            best_route = route_nodes
            best_result = result

        if result.passed:
            return route_nodes, result, correction_log

        if attempt == MAX_CORRECTION_ATTEMPTS:
            correction_log.append({
                "attempt": attempt,
                "status": "max_attempts_reached",
                "passed": False,
            })
            break

        # Apply corrections
        new_waypoints = []

        # Turn violations are handled by the router's edge-penalty mechanism
        # in get_constrained_route(), not by waypoint injection here.
        if not result.is_legal and result.turn_violations:
            correction_log.append({
                "attempt": attempt,
                "note": f"{len(result.turn_violations)} turn violations (handled by router penalties)",
            })

        if not result.has_sane_detours and getattr(result, 'detour_violations', None):
            dropped = 0
            for v in result.detour_violations:
                bad_idx = v["waypoint_idx"]
                if bad_idx < len(waypoints):
                    wpt_to_drop = waypoints[bad_idx]
                    if wpt_to_drop in waypoints:
                        waypoints.remove(wpt_to_drop)
                        dropped += 1
            if dropped > 0:
                correction_log.append({
                    "attempt": attempt,
                    "fix": "detour_sanity",
                    "dropped": dropped,
                })
                continue

        if not result.is_direct:
            fixes = fix_directness(
                G, route_nodes, origin_node, dest_node,
                result.directness_metrics,
            )
            new_waypoints.extend(fixes)
            correction_log.append({
                "attempt": attempt,
                "fix": "directness",
                "injected": len(fixes),
            })

        if not result.is_covered:
            # Semantic router automatically prefers expected streets; 
            # if they are skipped, forcing geometric waypoints causes massive loops.
            correction_log.append({
                "attempt": attempt,
                "note": "street_coverage check failed (handled gracefully by semantic router)",
            })

        if not new_waypoints:
            # No fix available — stop early
            correction_log.append({
                "attempt": attempt,
                "status": "no_fix_available",
            })
            break

        waypoints = _merge_waypoints_in_order(
            G, origin_node, dest_node, waypoints, new_waypoints
        )

    # Return best route found across all attempts
    return best_route, best_result, correction_log
