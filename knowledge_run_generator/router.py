import os
import math
import sys
import geopandas as gpd
import networkx as nx
import osmnx as ox
from heapq import heappush, heappop
from itertools import count as _tie_counter
from pathlib import Path
from shapely.geometry import Point, LineString

from .aliases import normalise as _normalise_street_name, _iter_names
from .cache import cache_dir, cache_path

CACHE_DIR = cache_dir()
GRAPH_FILENAME_TEMPLATE = "london_{network_type}_v3.graphml"

ox.settings.cache_folder = str(cache_dir() / "ox_cache")

SERVICE_HIGHWAYS = {
    "service",
    "living_street",
    "track",
    "unclassified",
}

# Cap on states explored per leg. Hitting it means the search space blew up
# (usually a waypoint the router can't legally reach); the leg is abandoned and
# counted rather than silently dropped.
MAX_SEARCH_STATES = 500_000

LINK_HIGHWAYS = {
    "motorway_link",
    "trunk_link",
    "primary_link",
}


def _normalise_tag_values(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).lower().strip() for v in value if str(v).strip()]
    return [str(value).lower().strip()]


def _edge_traversal_cost(edge_data, prev_node=None, next_node=None,
                         dist_u_to_target=None, dist_v_to_target=None):
    """
    Cost model tuned for cab-legal routing in dense divided-carriageway areas.
    """
    length = float(edge_data.get("length", 1.0) or 1.0)
    penalty = float(edge_data.get("penalty", 0.0) or 0.0)
    cost = length + penalty

    highways = set(_normalise_tag_values(edge_data.get("highway")))
    junctions = set(_normalise_tag_values(edge_data.get("junction")))

    if highways & SERVICE_HIGHWAYS:
        cost += max(30.0, length * 0.35)
    if highways & LINK_HIGHWAYS:
        cost += max(18.0, length * 0.18)

    if "roundabout" in junctions:
        cost += 12.0

    if prev_node is not None and next_node is not None and prev_node == next_node:
        cost += 5000.0

    if dist_u_to_target is not None and dist_v_to_target is not None:
        delta = dist_v_to_target - dist_u_to_target
        if delta > 15.0:
            cost += min(220.0, delta * 0.45)

    return cost


def _best_edge_data(edge_bundle):
    if not edge_bundle:
        return None
    return min(edge_bundle.values(), key=lambda d: d.get("length", float("inf")))


def _print_progress(percent: int, stage: str, width: int = 30) -> None:
    filled = int(width * max(0, min(100, percent)) / 100)
    bar = "#" * filled + "-" * (width - filled)
    print(f"\r[{bar}] {percent:3d}% {stage}", end="", flush=True)


def load_graph(place_name="Greater London, UK", network_type=None):
    """
    Load the street network graph for the given place name.
    """
    resolved_network_type = network_type or os.environ.get("KRG_GRAPH_NETWORK_TYPE", "drive")
    safe_network_type = str(resolved_network_type).replace("/", "_").replace(" ", "_")
    graph_path = cache_path(GRAPH_FILENAME_TEMPLATE.format(network_type=safe_network_type))

    if graph_path.exists():
        print(f"Loading graph from cache: {graph_path}")
        return ox.load_graphml(graph_path)

    is_tty = sys.stdout.isatty()
    if is_tty:
        _print_progress(5, "Starting download")
        _print_progress(15, f"Requesting map data ({resolved_network_type})")
    else:
        print(f"Downloading graph for {place_name} (network_type={resolved_network_type})...")

    # Default to strict drive graph; optionally allow drive_service via env/arg.
    G = ox.graph_from_place(place_name, network_type=resolved_network_type)

    if is_tty:
        _print_progress(85, "Processing graph")
        _print_progress(95, "Saving graph to cache")
    else:
        print("Saving graph to cache...")

    ox.save_graphml(G, graph_path)
    if is_tty:
        _print_progress(100, "Ready")
        print()

    return G


def get_route(G, origin_point, destination_point, orig_node=None, dest_node=None):
    """
    Calculate the shortest path between two points (lat, lon).
    Returns the list of node IDs forming the route.
    If orig_node or dest_node IDs are provided, skips nearest_node lookup.
    """
    if orig_node is None:
        orig_node = ox.distance.nearest_nodes(G, origin_point[1], origin_point[0])
    if dest_node is None:
        dest_node = ox.distance.nearest_nodes(G, destination_point[1], destination_point[0])

    try:
        # Strictly shortest distance for The Knowledge
        route = nx.shortest_path(G, orig_node, dest_node, weight='length')
        return route
    except nx.NetworkXNoPath:
        print("No path found between the given points.")
        return None


# ---------------------------------------------------------------------------
# Ordered-constraint routing (ROADMAP Stage 3)
# ---------------------------------------------------------------------------

# Memoised edge-name sets. Keyed by graph identity so tests with several
# small graphs don't cross-contaminate; the pipeline holds one graph for its
# whole lifetime, so in practice this is a single-graph cache.
_edge_name_cache: dict = {}


def edge_name_set(G, u, v) -> frozenset:
    """Every normalised name attached to the (u, v) node pair.

    Unions across parallel edges and reads every name tag
    (``name``/``alt_name``/``old_name``/``official_name``/``ref``) — the same
    semantics the validator's ordered-coverage check uses, so the router and
    the metric cannot disagree about whether an edge is the named street.
    """
    key = (id(G), u, v)
    cached = _edge_name_cache.get(key)
    if cached is not None:
        return cached
    names = set()
    for data in (G.get_edge_data(u, v) or {}).values():
        for raw in _iter_names(data):
            norm = _normalise_street_name(raw)
            if norm:
                names.add(norm)
    result = frozenset(names)
    _edge_name_cache[key] = result
    return result


# Multiplier applied to edges that neither advance the constraint sequence
# nor stay on the street just advanced through. This is a *preference* for
# staying on Blue Book streets; the hard ordering lives in the goal test.
CONNECTOR_MULT = 3.0

# Ordered-search state budget. Per *run*, not per leg — the ordered search
# has no legs.
MAX_ORDERED_STATES = 400_000


def _ordered_edge_cost(edge_data, prev_node=None, next_node=None):
    """Structural cost of an edge for the ordered search.

    Same shape as :func:`_edge_traversal_cost` minus the backward-progress
    bias — the ordered search has real constraints, so it does not need (and
    must not fight) a geometric nudge toward the destination.
    """
    length = float(edge_data.get("length", 1.0) or 1.0)
    penalty = float(edge_data.get("penalty", 0.0) or 0.0)
    cost = length + penalty

    highways = set(_normalise_tag_values(edge_data.get("highway")))
    junctions = set(_normalise_tag_values(edge_data.get("junction")))

    if highways & SERVICE_HIGHWAYS:
        cost += max(30.0, length * 0.35)
    if highways & LINK_HIGHWAYS:
        cost += max(18.0, length * 0.18)
    if "roundabout" in junctions:
        cost += 12.0
    if prev_node is not None and next_node is not None and prev_node == next_node:
        cost += 5000.0
    return cost


def _constraint_anchor_summary(G, constraint, street_to_nodes):
    """(centroid_lat, centroid_lon, radius_m, node_set|None) for a constraint.

    ``max(0, dist(n, centroid) - radius)`` is an admissible lower bound on the
    distance from ``n`` to the nearest anchor, and O(1) per state — a full
    min-over-anchors scan is exact but unaffordable inside the search loop.
    """
    if constraint.kind == "NODE":
        nodes = [n for n in constraint.key if n in G.nodes]
    elif getattr(constraint, "nodes", None) is not None:
        # Localised: the anchor is the instance the run drives, not the
        # centroid of every same-named street in London (which also made
        # the corridor bbox span the city).
        nodes = [n for n in constraint.nodes if n in G.nodes]
    else:
        nodes = [n for n in (street_to_nodes or {}).get(constraint.key, ())
                 if n in G.nodes]
    if not nodes:
        return None
    lats = [G.nodes[n]["y"] for n in nodes]
    lons = [G.nodes[n]["x"] for n in nodes]
    clat = sum(lats) / len(lats)
    clon = sum(lons) / len(lons)
    radius = max(_euclid_m(clat, clon, la, lo) for la, lo in zip(lats, lons))
    node_set = set(nodes) if constraint.kind == "NODE" else None
    return (clat, clon, radius, node_set)


def _euclid_m(lat1, lon1, lat2, lon2):
    # Equirectangular metres at London's latitude; 0.62 lon scale, floored to
    # 0.6 for admissibility (matches the legacy heuristic).
    dx = (lon2 - lon1) * 0.6
    dy = lat2 - lat1
    return math.sqrt(dx * dx + dy * dy) * 111_000


def get_ordered_route(G, origin_node, dest_node, constraints,
                      prohibited_turns=None, street_to_nodes=None,
                      connector_mult=CONNECTOR_MULT,
                      max_states=MAX_ORDERED_STATES,
                      corridor_margin_deg=0.008,
                      pure_length_cost=False):
    """One ordered-constraint A* over the whole run.

    State is ``(node, idx, prev_node)`` where ``idx`` counts satisfied
    constraints; ``prev_node`` exists so prohibited turn triples can be
    filtered inside the expansion (and never appear in the output).

    **The goal test is the ordering**: a state terminates the search only
    when ``node == dest_node`` and ``idx == len(constraints)``. Skipping a
    constraint is not representable.

    Returns ``(route_nodes | None, info)``. ``info.max_idx`` names the
    furthest constraint index any explored state reached — on failure, the
    constraint at that index is the blocker.

    ``pure_length_cost`` runs the identical search with the connector
    multiplier and structural penalties zeroed; its goal-cost is the ordered
    optimum used by the ``excess_over_ordered_optimum`` QA metric.
    """
    C = list(constraints)
    K = len(C)
    dest = G.nodes[dest_node]
    dest_lat, dest_lon = dest["y"], dest["x"]

    # Anchor summaries per constraint, for the heuristic and the corridor.
    anchors = [_constraint_anchor_summary(G, c, street_to_nodes) for c in C]

    # Corridor: bbox of origin, dest and every anchor centroid (+ radius),
    # padded. States outside it are not expanded — Blue Book runs are
    # corridors by construction, and this is what keeps 26-constraint runs
    # from flooding Greater London.
    o = G.nodes[origin_node]
    lats = [o["y"], dest_lat]
    lons = [o["x"], dest_lon]
    for summary in anchors:
        if summary is None:
            continue
        clat, clon, radius, _ = summary
        # `radius` is metres under _euclid_m's 0.6 longitude scale, so a
        # degree of longitude is only 0.6 x 111 km: padding both axes by
        # radius / 111 km clipped the far ends of long east-west streets out
        # of the corridor. It went unnoticed while unlocalised anchors
        # spanned half of London.
        pad_lat = radius / 111_000.0
        pad_lon = radius / (111_000.0 * 0.6)
        lats.extend((clat - pad_lat, clat + pad_lat))
        lons.extend((clon - pad_lon, clon + pad_lon))
    if corridor_margin_deg is None:
        # No corridor. The T3 fallback needs this: a run whose prescribed
        # river crossing is closed (Hammersmith Bridge) can only route via
        # the next bridge, far outside the origin–destination bbox.
        def in_corridor(nid):
            return True
    else:
        lat_min = min(lats) - corridor_margin_deg
        lat_max = max(lats) + corridor_margin_deg
        lon_min = min(lons) - corridor_margin_deg
        lon_max = max(lons) + corridor_margin_deg

        def in_corridor(nid):
            n = G.nodes[nid]
            return lat_min <= n["y"] <= lat_max and lon_min <= n["x"] <= lon_max

    h_cache: dict = {}

    def h(nid, idx):
        key = (nid, idx)
        cached = h_cache.get(key)
        if cached is not None:
            return cached
        n = G.nodes[nid]
        value = _euclid_m(n["y"], n["x"], dest_lat, dest_lon)
        if idx < K:
            summary = anchors[idx]
            if summary is not None:
                clat, clon, radius, _ = summary
                to_anchor = _euclid_m(n["y"], n["x"], clat, clon) - radius
                if to_anchor > value:
                    value = to_anchor
        h_cache[key] = value
        return value

    # Constraints already satisfied by standing at the origin (a NODE
    # constraint containing the origin cannot be advanced by an arrival).
    start_idx = 0
    while start_idx < K:
        summary = anchors[start_idx]
        if (C[start_idx].kind == "NODE" and summary is not None
                and summary[3] is not None and origin_node in summary[3]):
            start_idx += 1
        else:
            break

    tie = _tie_counter()
    start_state = (origin_node, start_idx, None)
    queue = [(h(origin_node, start_idx), 0.0, next(tie)) + start_state]
    best = {start_state: 0.0}
    parents: dict = {}

    states = 0
    hit_cap = False
    max_idx = start_idx
    dest_reached_idx = None
    goal_state = None
    goal_cost = None

    while queue:
        f, g, _, u, idx, prev = heappop(queue)
        states += 1
        if states > max_states:
            hit_cap = True
            break

        if best.get((u, idx, prev), float("inf")) < g:
            continue

        if idx > max_idx:
            max_idx = idx
        if u == dest_node:
            # Even short of the goal, the best idx *at the destination* is
            # the honest blocker signal: `max_idx` alone can reflect a wrong-
            # instance branch that got further along the sequence somewhere
            # unreachable, sending the ladder after the wrong victim.
            if dest_reached_idx is None or idx > dest_reached_idx:
                dest_reached_idx = idx
            if idx >= K:
                goal_state = (u, idx, prev)
                goal_cost = g
                break

        stay_key = C[idx - 1].key if (idx > 0 and C[idx - 1].kind == "STREET") else None

        for v in G.successors(u):
            if prohibited_turns and prev is not None and (prev, u, v) in prohibited_turns:
                continue
            if not in_corridor(v):
                continue

            bundle = G.get_edge_data(u, v)
            edge_data = _best_edge_data(bundle)
            if edge_data is None:
                continue
            if pure_length_cost:
                base = float(edge_data.get("length", 1.0) or 1.0)
            else:
                base = _ordered_edge_cost(edge_data, prev_node=prev, next_node=v)

            names = edge_name_set(G, u, v)
            advance = False
            if idx < K:
                c = C[idx]
                if c.kind == "STREET":
                    advance = c.key in names and (
                        c.nodes is None or (u in c.nodes and v in c.nodes))
                else:
                    summary = anchors[idx]
                    advance = (summary is not None and summary[3] is not None
                               and v in summary[3])
            stay_on = stay_key is not None and stay_key in names

            def push(next_idx, cost):
                state = (v, next_idx, u)
                new_g = g + cost
                if best.get(state, float("inf")) > new_g:
                    best[state] = new_g
                    parents[state] = (u, idx, prev)
                    heappush(queue, (new_g + h(v, next_idx), new_g, next(tie),
                                     v, next_idx, u))

            # The non-advancing branch is always pushed (at connector price
            # unless the edge is the street we're already on): an edge that
            # *could* advance the sequence may be a stray brush with a street
            # the run needs later, and consuming the constraint there would
            # be wrong — or fatal, if the early match is a dead end.
            if advance:
                push(idx + 1, base)
            if stay_on:
                push(idx, base)
            else:
                push(idx, base if pure_length_cost else base * connector_mult)

    info = {
        "reached_goal": goal_state is not None,
        "max_idx": max_idx,
        "dest_reached_idx": dest_reached_idx,
        "constraints": K,
        "states_explored": states,
        "hit_state_cap": hit_cap,
        "goal_cost": goal_cost,
    }
    if goal_state is None:
        return None, info

    path = []
    cur = goal_state
    while cur in parents:
        path.append(cur[0])
        cur = parents[cur]
    path.append(cur[0])
    path.reverse()
    return path, info


def route_ordered_with_ladder(G, origin_node, dest_node, constraints,
                              prohibited_turns=None, street_to_nodes=None,
                              max_demotions=8, max_states=MAX_ORDERED_STATES,
                              compute_optimum=False, demote_loops=True,
                              min_lap_m=0.0, leg_loops=True):
    """Degradation ladder around :func:`get_ordered_route` — every gap explicit.

    T0  all constraints enforced                        -> ``ordered_strict``
    T1  demote the blocking constraint, retry (<= 4)    -> ``ordered_relaxed``
    T2  keep only hard (exact/junction) constraints     -> ``ordered_partial``
    T3  plain shortest path                             -> ``shortest_path``

    A route that forces a loop (:func:`find_forced_loop`) is repaired by
    demoting the constraint responsible, on trial: the demotion stands only
    if the route gets shorter. Those demotions are also listed in
    ``meta.loop_demotions``. ``min_lap_m`` / ``leg_loops`` narrow what counts
    as a loop (see :func:`find_forced_loop`); the Blue Book run itself uses
    laps of at least LOOP_EXCESS_M only, because a prescribed run may
    legitimately go round a block to reach its next street.

    Returns ``(route_nodes, meta)``; ``meta.demoted`` lists every constraint
    dropped on the way to a route, as ``(raw, source)`` pairs.
    """
    active = list(constraints)
    demoted = []
    loop_demotions = []
    mode = "ordered_strict"
    attempts = 0
    last_info = {}

    def search(cons):
        nonlocal attempts
        attempts += 1
        return get_ordered_route(
            G, origin_node, dest_node, cons,
            prohibited_turns=prohibited_turns,
            street_to_nodes=street_to_nodes,
            max_states=max_states,
        )

    def repair_loops(route, info, cons):
        """Demote constraints that force loops while that shortens the route.

        A route that exists can still be absurd: the ordered search will
        happily lap a gyratory, or drive a kilometre round the block, to meet
        a constraint in sequence. Each suspect is demoted on trial and kept
        demoted only if the route gets at least LOOP_MIN_GAIN_M shorter — an
        innocent suspect is reinstated.
        """
        cons = list(cons)
        dropped = []
        while demote_loops and len(dropped) < max_demotions:
            suspects = find_forced_loop(G, route, cons, prohibited_turns,
                                        all_suspects=True, min_lap_m=min_lap_m,
                                        leg_loops=leg_loops)
            if not suspects:
                break
            length = _route_length(G, route)
            for k in suspects[:LOOP_MAX_TRIALS]:
                trial = cons[:k] + cons[k + 1:]
                r2, i2 = search(trial)
                if r2 is not None and _route_length(G, r2) <= length - LOOP_MIN_GAIN_M:
                    dropped.append(cons[k])
                    cons, route, info = trial, r2, i2
                    break
            else:
                break
        return route, info, cons, dropped

    def finish(route, info, cons, mode_now):
        meta = _ordered_meta(G, route, info, mode_now, demoted, attempts)
        if loop_demotions:
            meta["loop_demotions"] = list(loop_demotions)
        if compute_optimum:
            meta["ordered_optimum_m"] = _ordered_optimum(
                G, origin_node, dest_node, cons,
                prohibited_turns, street_to_nodes, max_states)
        return route, meta

    for _ in range(max_demotions + 1):
        route, info = search(active)
        last_info = info
        if route is not None:
            route, info, active, dropped = repair_loops(route, info, active)
            for victim in dropped:
                demoted.append((victim.raw, victim.source))
                loop_demotions.append((victim.raw, victim.source))
                mode = "ordered_relaxed"
            return finish(route, info, active, mode)
        if not active:
            break
        # Pick the demotion victim. Demoting on cap-hit rather than treating
        # it as exhaustion is deliberate — see ROADMAP risks.
        #
        # If the search *reached the destination* short of the goal, the
        # unsatisfied suffix [dest_idx..K) is what blocked it — demote the
        # first soft constraint in that suffix, else the suffix head. This is
        # what stops a bad *final* constraint (a fuzzy destination street)
        # from cascading demotions through a perfectly satisfiable middle.
        #
        # Otherwise fall back to max_idx and prefer a *soft* constraint at or
        # before it: when a hard, correctly resolved street appears
        # unreachable, the usual culprit is an earlier low-confidence guess
        # (wrong ring, word-removal mismatch) that pinned the search to the
        # wrong part of town.
        dest_idx = info.get("dest_reached_idx")
        if dest_idx is not None and dest_idx < len(active):
            victim_idx = next(
                (i for i in range(dest_idx, len(active)) if not active[i].hard),
                dest_idx,
            )
        else:
            blocked = min(info["max_idx"], len(active) - 1)
            victim_idx = next(
                (i for i in range(blocked, -1, -1) if not active[i].hard),
                blocked,
            )
        victim = active.pop(victim_idx)
        demoted.append((victim.raw, victim.source))
        mode = "ordered_relaxed"

    hard_only = [c for c in constraints if c.hard]
    if len(hard_only) < len(constraints):
        for c in constraints:
            if not c.hard and (c.raw, c.source) not in demoted:
                demoted.append((c.raw, c.source))
        route, info = search(hard_only)
        last_info = info
        if route is not None:
            route, info, hard_only, dropped = repair_loops(route, info, hard_only)
            for victim in dropped:
                demoted.append((victim.raw, victim.source))
                loop_demotions.append((victim.raw, victim.source))
            return finish(route, info, hard_only, "ordered_partial")

    # T3 — the honest fallback. Still filtered for prohibited turns via the
    # ordered search with zero constraints (plain A* with the same expansion),
    # so even the fallback cannot emit an illegal triple. Uncorridored: when
    # the run's prescribed crossing is physically closed, the only legal route
    # lies well outside the origin–destination bbox.
    route, info = get_ordered_route(
        G, origin_node, dest_node, [],
        prohibited_turns=prohibited_turns,
        street_to_nodes=street_to_nodes,
        max_states=max_states,
        corridor_margin_deg=None,
    )
    attempts += 1
    if route is not None:
        for c in constraints:
            if (c.raw, c.source) not in demoted:
                demoted.append((c.raw, c.source))
        meta = _ordered_meta(G, route, info, "shortest_path", demoted, attempts)
        meta["status"] = "failed"
        return route, meta

    meta = _ordered_meta(G, None, last_info, "unroutable", demoted, attempts)
    meta["status"] = "failed"
    return None, meta


def shortest_legal_length(G, origin_node, dest_node, prohibited_turns=None,
                          max_states=MAX_ORDERED_STATES):
    """Length (m) of the unconstrained shortest legal route, or ``None``.

    Pure edge length, prohibited turns honoured, no corridor — the yardstick
    the gross-detour gate measures a run against.
    """
    _, info = get_ordered_route(
        G, origin_node, dest_node, [],
        prohibited_turns=prohibited_turns,
        max_states=max_states,
        corridor_margin_deg=None,
        pure_length_cost=True,
    )
    cost = info.get("goal_cost")
    return round(cost, 1) if cost is not None else None


# Budget for the reverse run. The reverse is not prescribed by the Blue Book;
# reversing the constraint sequence is only a proxy for it, and where one-ways
# make the reversal a tour of the neighbourhood the student is better served
# by the shortest legal route. The reversed-sequence route is kept while it is
# within REVERSE_FWD_SLACK x the forward run or REVERSE_SHORTEST_RATIO x the
# shortest legal route, whichever allows more.
REVERSE_FWD_SLACK = 1.25
REVERSE_SHORTEST_RATIO = 1.5


def route_reverse(G, origin_node, dest_node, constraints, forward_length_m=None,
                  prohibited_turns=None, street_to_nodes=None,
                  max_states=MAX_ORDERED_STATES,
                  fwd_slack=REVERSE_FWD_SLACK,
                  shortest_ratio=REVERSE_SHORTEST_RATIO):
    """Route the reverse of a run: the reversed constraint sequence through
    the ladder, unless that is out of budget, in which case the shortest
    legal route (``routing_mode: shortest_path``, ``reverse_fallback`` set).

    ``origin_node``/``dest_node`` are the reverse's own start and end, i.e.
    the forward run's end and start.
    """
    route, meta = route_ordered_with_ladder(
        G, origin_node, dest_node, list(constraints),
        prohibited_turns=prohibited_turns,
        street_to_nodes=street_to_nodes,
        max_states=max_states,
    )
    shortest = shortest_legal_length(G, origin_node, dest_node,
                                     prohibited_turns=prohibited_turns,
                                     max_states=max_states)
    meta["shortest_m"] = shortest
    if route is None or shortest is None or meta.get("routing_mode") == "shortest_path":
        return route, meta
    length = _route_length(G, route)
    budget = shortest * shortest_ratio
    if forward_length_m:
        budget = max(budget, forward_length_m * fwd_slack)
    if length <= budget:
        return route, meta
    fallback, info = get_ordered_route(
        G, origin_node, dest_node, [],
        prohibited_turns=prohibited_turns,
        max_states=max_states,
        corridor_margin_deg=None,
    )
    if fallback is None:
        return route, meta
    demoted = [(c.raw, c.source) for c in constraints]
    fb_meta = _ordered_meta(G, fallback, info, "shortest_path", demoted,
                            meta.get("ladder_attempts", 0) + 1)
    fb_meta["shortest_m"] = shortest
    fb_meta["reverse_fallback"] = (
        f"reversed sequence {length:.0f}m over budget {budget:.0f}m")
    return fallback, fb_meta


def _ordered_optimum(G, origin_node, dest_node, active_constraints,
                     prohibited_turns, street_to_nodes, max_states):
    """Length of the shortest route satisfying the same constraint sequence,
    with the connector multiplier and structural penalties zeroed. The route
    length divided by this is the honest wastefulness metric — Blue Book
    geometry is by definition not the straight line, so comparing against the
    straight line punishes correct runs."""
    _, info = get_ordered_route(
        G, origin_node, dest_node, active_constraints,
        prohibited_turns=prohibited_turns,
        street_to_nodes=street_to_nodes,
        max_states=max_states,
        pure_length_cost=True,
    )
    cost = info.get("goal_cost")
    return round(cost, 1) if cost is not None else None


def _ordered_meta(G, route, info, mode, demoted, attempts):
    meta = {
        "routing_mode": mode,
        "demoted_constraints": list(demoted),
        "ladder_attempts": attempts,
        "max_idx": info.get("max_idx"),
        "constraints_total": info.get("constraints"),
        "states_explored": info.get("states_explored"),
        "hit_state_cap": bool(info.get("hit_state_cap")),
        "unreachable_legs": 0,
        "truncated_legs": 0,
    }
    if route:
        meta.update(_extract_route_metadata(G, route))
    else:
        meta.update({"total_distance": 0.0, "streets_traversed": []})
    return meta


def constraint_waypoints(G, route_nodes, constraints):
    """Derive display waypoints from the routed path: the node at which each
    constraint is first satisfied, in order. Strictly more accurate than the
    old intersection guesses, and free."""
    return [route_nodes[i] for i in _constraint_positions(G, route_nodes, constraints)]


def _constraint_positions(G, route_nodes, constraints):
    """Route indices at which each constraint is first satisfied, in order
    (greedy walk; stops at the first constraint the route never meets)."""
    positions = []
    idx = 0
    C = list(constraints)
    # Leading junctions the route starts on are satisfied at the origin, as
    # in get_ordered_route. Without this the walk waits for an arrival at the
    # junction that never comes, and every later constraint goes unplaced.
    if route_nodes:
        while idx < len(C) and C[idx].kind != "STREET" and route_nodes[0] in C[idx].key:
            positions.append(0)
            idx += 1
    for i in range(1, len(route_nodes)):
        if idx >= len(C):
            break
        u, v = route_nodes[i - 1], route_nodes[i]
        c = C[idx]
        if c.kind == "STREET":
            if c.matches_edge(edge_name_set(G, u, v), u, v):
                positions.append(i)
                idx += 1
        else:
            if v in c.key:
                positions.append(i)
                idx += 1
    return positions


# A leg (route between two consecutive constraint positions) longer than the
# shortest legal route between its ends by more than this — and by more than
# LOOP_LEG_RATIO x — is a loop the constraint sequence forced: the reversed
# run approaching a one-way from the wrong end, or Blue Book text written for
# a gyratory that has since been remodelled (Archway). A Knowledge run never
# drives round the block twice to tick off a street.
LOOP_EXCESS_M = 1000.0
LOOP_LEG_RATIO = 2.0
# A loop demotion is kept only if it shortens the route by at least this;
# otherwise the suspect was innocent and is reinstated.
LOOP_MIN_GAIN_M = 250.0
# Suspects tried per loop before the loop is accepted as unavoidable.
LOOP_MAX_TRIALS = 4


def _route_length(G, route_nodes):
    return _leg_length(G, route_nodes, 0, len(route_nodes) - 1)


def _leg_length(G, route_nodes, a, b):
    total = 0.0
    for i in range(a, b):
        best = _best_edge_data(G.get_edge_data(route_nodes[i], route_nodes[i + 1]))
        if best:
            total += float(best.get("length", 0) or 0)
    return total


def find_forced_loop(G, route_nodes, constraints, prohibited_turns=None,
                     loop_excess_m=LOOP_EXCESS_M, loop_leg_ratio=LOOP_LEG_RATIO,
                     all_suspects=False, min_lap_m=0.0, leg_loops=True):
    """The constraint that forced a loop into *route_nodes*, or None.

    Two symptoms (a *leg* is the connector the route drives to reach a
    constraint, after it leaves the previous one; the final leg runs on to
    the destination and is blamed on the last constraint):

    * the route drives the same directed edge twice — a lap (of at least
      ``min_lap_m``, measured from the first traversal to the repeat).
      Suspects are the constraints satisfied inside the lap, latest first
      (the lap exists to deliver them), then the one whose leg contains the
      repeat;
    * with ``leg_loops``, a leg is more than ``loop_leg_ratio`` x, and
      ``loop_excess_m`` longer than, the shortest legal route between its two
      ends. Suspects are the owners of such legs, worst first.

    Returns the index of the prime suspect, or with ``all_suspects`` the list
    of suspect indices in order (empty when there is no loop).
    """
    C = list(constraints)
    none = [] if all_suspects else None
    if not C or not route_nodes or len(route_nodes) < 3:
        return none
    positions = _constraint_positions(G, route_nodes, C)
    if not positions:
        return none
    last = len(route_nodes) - 1
    # Leg k ends at bounds[k+1] and is blamed on owners[k].
    bounds = [0] + positions + [last]
    owners = list(range(len(positions))) + [len(positions) - 1]

    def owner_of(edge_end):
        for k in range(len(owners)):
            if bounds[k] < edge_end <= bounds[k + 1]:
                return owners[k]
        return owners[-1]

    def result(suspects):
        ordered = list(dict.fromkeys(suspects))
        if all_suspects:
            return ordered
        return ordered[0] if ordered else None

    first_seen = {}
    for i in range(1, len(route_nodes)):
        edge = (route_nodes[i - 1], route_nodes[i])
        if edge in first_seen:
            if _leg_length(G, route_nodes, first_seen[edge], i) >= min_lap_m:
                inside = [k for k, p in enumerate(positions)
                          if first_seen[edge] < p < i]
                return result(list(reversed(inside)) + [owner_of(i)])
            continue
        first_seen[edge] = i

    # Leg starts move past the stretch the route keeps driving on the street
    # it just satisfied: a Blue Book run drives the whole street, which is
    # prescribed, not a loop. What is left is the connector to the next one.
    def stay_end(k, limit):
        j = positions[k]
        c = C[k]
        while j < limit:
            u, v = route_nodes[j], route_nodes[j + 1]
            on = (c.key in edge_name_set(G, u, v)) if c.kind == "STREET" else (v in c.key)
            if not on:
                break
            j += 1
        return j

    if not leg_loops:
        return result([])
    starts = [0] + [stay_end(k, bounds[k + 2]) for k in range(len(positions))]

    loops = []
    for k, owner in enumerate(owners):
        a, b = starts[k], bounds[k + 1]
        if b - a < 2:
            continue
        leg = _leg_length(G, route_nodes, a, b)
        if leg < loop_excess_m:
            continue
        shortest = shortest_legal_length(
            G, route_nodes[a], route_nodes[b], prohibited_turns=prohibited_turns)
        if shortest is None:
            continue
        excess = leg - shortest
        if excess > loop_excess_m and leg > shortest * loop_leg_ratio:
            loops.append((excess, owner))
    loops.sort(reverse=True)
    return result([owner for _excess, owner in loops])


def _extract_route_metadata(G, route_nodes):
    """
    Walk the route and collect total distance and ordered street names.
    """
    total_dist = 0.0
    streets = []
    for i in range(len(route_nodes) - 1):
        edge = G.get_edge_data(route_nodes[i], route_nodes[i + 1])
        best = _best_edge_data(edge)
        if best:
            d = best.get('length', 0)
            total_dist += d
            name = best.get('name', 'Unknown Road')
            if isinstance(name, list):
                name = name[0]
            if not streets or streets[-1] != name:
                streets.append(name)
    return {
        'total_distance': round(total_dist, 1),
        'streets_traversed': streets,
    }


# ---------------------------------------------------------------------------
# Coordinate geometry helpers
# ---------------------------------------------------------------------------

def nodes_to_coords_geometry(G, nodes):
    """
    Convert a list of graph node IDs to a [lon, lat] coordinate array,
    using edge geometry where available for detailed curves.
    """
    coords = []
    for i in range(len(nodes) - 1):
        u = nodes[i]
        v = nodes[i + 1]

        data = G.get_edge_data(u, v)
        if data:
            edge = _best_edge_data(data)
            if 'geometry' in edge:
                seg_coords = [[p[0], p[1]] for p in edge['geometry'].coords]
                if coords:
                    if seg_coords[0] == coords[-1]:
                        coords.extend(seg_coords[1:])
                    else:
                        coords.extend(seg_coords)
                else:
                    coords.extend(seg_coords)
            else:
                p_v = [G.nodes[v]['x'], G.nodes[v]['y']]
                if not coords:
                    p_u = [G.nodes[u]['x'], G.nodes[u]['y']]
                    coords.append(p_u)
                coords.append(p_v)

    if not coords and len(nodes) > 0:
        coords = [[G.nodes[n]['x'], G.nodes[n]['y']] for n in nodes]

    return coords


# ---------------------------------------------------------------------------
# Legacy helpers (kept for backward compatibility)
# ---------------------------------------------------------------------------

def save_route_geojson(G, route, filepath):
    """Save the route as a GeoJSON file."""
    try:
        node_points = [Point(G.nodes[n]['x'], G.nodes[n]['y']) for n in route]
        line = LineString(node_points)
        gdf = gpd.GeoDataFrame(geometry=[line], crs=G.graph['crs'])
        gdf.to_file(filepath, driver='GeoJSON')
        print(f"Route GeoJSON saved to {filepath}")
    except Exception as e:
        print(f"Error saving GeoJSON: {e}")


def plot_route(G, route, filepath):
    """Plot the route on the graph and save to file."""
    try:
        if not route:
            return

        x_coords = [G.nodes[u]['x'] for u in route]
        y_coords = [G.nodes[u]['y'] for u in route]

        margin = 0.002
        west, east = min(x_coords) - margin, max(x_coords) + margin
        south, north = min(y_coords) - margin, max(y_coords) + margin

        nodes_in_bbox = [
            n for n, data in G.nodes(data=True)
            if south < data['y'] < north and west < data['x'] < east
        ]

        G_sub = G.subgraph(nodes_in_bbox)
        if len(G_sub) < len(route):
            G_sub = G

        fig, ax = ox.plot_graph_route(
            G_sub, route,
            show=False, close=False,
            route_color='blue', route_linewidth=5, route_alpha=0.6,
            node_size=0, edge_linewidth=0.5, edge_color='#999999'
        )

        fig.patch.set_facecolor('white')
        ax.set_facecolor('white')

        fig.savefig(filepath, dpi=300, bbox_inches='tight')
        print(f"Route visualization saved to {filepath}")
    except Exception as e:
        print(f"Error plotting route: {e}")
        import traceback
        traceback.print_exc()
