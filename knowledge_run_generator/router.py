import os
import math
import sys
import geopandas as gpd
import networkx as nx
import osmnx as ox
from pathlib import Path
from shapely.geometry import Point, LineString

from .aliases import normalise as _normalise_street_name

CACHE_DIR = Path("/tmp/app_cache")
GRAPH_FILENAME_TEMPLATE = "london_{network_type}_v3.graphml"

ox.settings.cache_folder = "/tmp/ox_cache"

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
    CACHE_DIR.mkdir(exist_ok=True)
    safe_network_type = str(resolved_network_type).replace("/", "_").replace(" ", "_")
    graph_path = CACHE_DIR / GRAPH_FILENAME_TEMPLATE.format(network_type=safe_network_type)

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
# Constrained multi-leg routing
# ---------------------------------------------------------------------------

def get_constrained_route(G, origin_node, dest_node, waypoint_nodes,
                          prohibited_turns=None, intermediate_streets=None):
    """
    Compute the shortest-distance route from *origin_node* to *dest_node*
    passing through *waypoint_nodes* in order.

    *prohibited_turns* is an optional set of ``(from, via, to)`` triples
    (from OSM ``no_*`` / ``only_*`` relations). They are enforced as a
    transition filter inside the Dijkstra expansion itself — never
    mutating the graph — so illegal turns cannot appear in the output,
    and the graph is safe to share across threads and crash-resilient.

    Returns ``(route_nodes, metadata)`` where metadata contains:
      - total_distance  (metres)
      - streets_traversed  (ordered unique street names)
    """
    route, search_stats = _route_through_waypoints(
        G, origin_node, dest_node, waypoint_nodes,
        intermediate_streets=intermediate_streets,
        prohibited_turns=prohibited_turns,
    )
    clean = _clean_backtrack(route)
    clean = _collapse_revisits(clean)
    metadata = _extract_route_metadata(G, clean)
    # Legs the search gave up on. Previously both failure modes were silent,
    # so a route stitched together from partial legs was indistinguishable
    # from a clean one in the output.
    metadata.update(search_stats)
    return clean, metadata


def _route_through_waypoints(G, origin_node, dest_node, waypoint_nodes,
                             intermediate_streets=None,
                             prohibited_turns=None):
    """Shortest-distance route leg-by-leg through ordered waypoints, with
    sequential street discounts.

    Returns ``(route_nodes, stats)``. ``stats`` reports legs that failed to
    reach their target: ``unreachable_legs`` (the search exhausted the queue)
    and ``truncated_legs`` (it hit the state cap first). Both leave a gap in
    the route, and both used to pass silently.
    """
    from heapq import heappush, heappop
    import itertools

    # Use the canonical normaliser so the router and the upstream alias index
    # agree on exactly one form per street — eliminates the need for fuzzy
    # substring matching below, which produced false discounts (e.g.
    # "KING STREET" spuriously matching "KINGSWAY").
    norm_streets = []
    if intermediate_streets:
        for s in intermediate_streets:
            n = _normalise_street_name(s)
            if n:
                norm_streets.append(n)

    stages = []
    for wp in waypoint_nodes:
        if isinstance(wp, (set, list, tuple)):
            stages.append(set(wp))
        else:
            stages.append({wp})
    stages.append({dest_node})

    full_route = [origin_node]
    current_source = origin_node
    current_street_idx = 0
    unreachable_legs = 0
    truncated_legs = 0

    c = itertools.count()

    # Pre-index norm_streets for faster lookup
    # street_to_indices: name -> list of indices where it appears in the sequence
    street_to_indices = {}
    if norm_streets:
        for idx, s in enumerate(norm_streets):
            if s not in street_to_indices:
                street_to_indices[s] = []
            street_to_indices[s].append(idx)

    # Cache for edge name normalization (within this run)
    edge_norm_cache = {}

    for i, target_set in enumerate(stages):
        if current_source in target_set:
            continue
            
        # Heuristic for A*: distance to nearest node in target_set
        # Using a simple mean coordinate for the target_set set to approximate.
        target_pts = []
        for tid in target_set:
            if tid in G.nodes:
                tn = G.nodes[tid]
                target_pts.append((tn['y'], tn['x']))
        
        target_lat = sum(p[0] for p in target_pts) / len(target_pts) if target_pts else 0
        target_lon = sum(p[1] for p in target_pts) / len(target_pts) if target_pts else 0

        # Fast Euclidean heuristic (admissible for shortest distance)
        def _h(node):
            if not target_pts: return 0
            n = G.nodes[node]
            # 51.5 degrees N: 1 degree lon ~ 69km, 1 degree lat ~ 111km
            # Ratio is ~0.62. Use 0.6 for admissibility.
            dx = (n['x'] - target_lon) * 0.6
            dy = n['y'] - target_lat
            return math.sqrt(dx*dx + dy*dy) * 111000

        target_latlon = (target_lat, target_lon)

        def _dist_to_target(node_id):
            n = G.nodes[node_id]
            dx = (n['x'] - target_latlon[1]) * 0.6
            dy = n['y'] - target_latlon[0]
            return math.sqrt(dx * dx + dy * dy) * 111000

        # queue: (priority, dist_from_start, tie-breaker, current_node, current_street_idx, prev_node)
        queue = [(_h(current_source), 0.0, next(c), current_source, current_street_idx, None)]
        visited = {(current_source, current_street_idx, None): 0.0}
        parents = {} # (u, idx, prev_u) -> (prev_u, prev_idx, prev_prev_u)
        found_target = None # (node, idx, prev_node)
        
        states_explored = 0
        hit_state_cap = False
        while queue:
            priority, dist, _, u, s_idx, prev_u = heappop(queue)
            states_explored += 1

            if states_explored > MAX_SEARCH_STATES:
                # Give up on a runaway search, but record it — see stats below.
                hit_state_cap = True
                break

            if u in target_set:
                found_target = (u, s_idx, prev_u)
                break
            
            # Since we update visited on push, we only continue if we found a better 
            # path to this state *since* it was pushed.
            if visited.get((u, s_idx, prev_u), float('inf')) < dist:
                continue

            dist_u_to_target = _dist_to_target(u)
            
            for v, edges in G[u].items():
                # Turn-restriction filter: reject the transition (prev_u, u, v)
                # directly — no graph mutation, no edge-penalty bookkeeping,
                # and no risk of leaking state on exception.
                if (prohibited_turns
                        and prev_u is not None
                        and (prev_u, u, v) in prohibited_turns):
                    continue

                for k, edge_data in edges.items():
                    length = float(edge_data.get('length', 1.0) or 1.0)
                    dist_v_to_target = _dist_to_target(v)
                    
                    # Get or compute normalised names (shared normaliser)
                    eid = (u, v, k)
                    if eid in edge_norm_cache:
                        edge_norms = edge_norm_cache[eid]
                    else:
                        name = edge_data.get('name', '')
                        if isinstance(name, str):
                            names = [name]
                        elif isinstance(name, list):
                            names = name
                        else:
                            names = []
                        edge_norms = [_normalise_street_name(n) for n in names if n]
                        edge_norms = [n for n in edge_norms if n]
                        edge_norm_cache[eid] = edge_norms
                    
                    # Option 1: Normal routing cost, stay at current index
                    step_cost = _edge_traversal_cost(
                        edge_data,
                        prev_node=prev_u,
                        next_node=v,
                        dist_u_to_target=dist_u_to_target,
                        dist_v_to_target=dist_v_to_target,
                    )
                    new_dist_opt1 = dist + step_cost
                    if visited.get((v, s_idx, u), float('inf')) > new_dist_opt1:
                        visited[(v, s_idx, u)] = new_dist_opt1
                        priority = new_dist_opt1 + _h(v)
                        heappush(queue, (priority, new_dist_opt1, next(c), v, s_idx, u))
                        parents[(v, s_idx, u)] = (u, s_idx, prev_u)
                        
                    # Option 2: Attempt to find a sequence discount match.
                    # Exact match first; then a conservative *token-prefix*
                    # check (one street's tokens form a prefix of the other's)
                    # so "KINGS CROSS" still matches "KINGS CROSS ROAD" while
                    # the old bug — "KING STREET" matching "KINGSWAY" as raw
                    # substring — can no longer happen.
                    if norm_streets and s_idx < len(norm_streets):
                        found_j = None
                        for en in edge_norms:
                            if en in street_to_indices:
                                for idx in street_to_indices[en]:
                                    if idx >= s_idx:
                                        if found_j is None or idx < found_j:
                                            found_j = idx

                            if found_j is None or found_j > s_idx:
                                en_tokens = en.split()
                                for j in range(s_idx, min(s_idx + 10, len(norm_streets))):
                                    expected = norm_streets[j]
                                    if expected == en:
                                        if found_j is None or j < found_j:
                                            found_j = j
                                            break
                                        continue
                                    exp_tokens = expected.split()
                                    if not en_tokens or not exp_tokens:
                                        continue
                                    # Token-prefix match in either direction
                                    if (len(en_tokens) <= len(exp_tokens)
                                            and exp_tokens[: len(en_tokens)] == en_tokens):
                                        if found_j is None or j < found_j:
                                            found_j = j
                                            break
                                    elif (len(exp_tokens) < len(en_tokens)
                                            and en_tokens[: len(exp_tokens)] == exp_tokens):
                                        if found_j is None or j < found_j:
                                            found_j = j
                                            break
                                            
                        if found_j is not None:
                            penalty = float(edge_data.get('penalty', 0.0) or 0.0)
                            base_length = max(0.0, length - penalty)
                            structural_penalty = _edge_traversal_cost(
                                edge_data,
                                prev_node=prev_u,
                                next_node=v,
                                dist_u_to_target=dist_u_to_target,
                                dist_v_to_target=dist_v_to_target,
                            ) - length
                            discounted_cost = (base_length * 0.1) + penalty + structural_penalty
                            new_dist_opt2 = dist + discounted_cost
                            
                            if visited.get((v, found_j, u), float('inf')) > new_dist_opt2:
                                visited[(v, found_j, u)] = new_dist_opt2
                                priority = new_dist_opt2 + _h(v)
                                heappush(queue, (priority, new_dist_opt2, next(c), v, found_j, u))
                                parents[(v, found_j, u)] = (u, s_idx, prev_u)
                                
        if found_target:
            # Reconstruct path from parents
            leg_path = []
            curr = found_target
            while curr in parents:
                leg_path.append(curr[0])
                curr = parents[curr]
            leg_path.append(current_source)
            leg_path.reverse()
            
            full_route.extend(leg_path[1:])
            current_source = found_target[0]
            current_street_idx = found_target[1]
        else:
            if hit_state_cap:
                truncated_legs += 1
                print(f"  Leg {i} hit the {MAX_SEARCH_STATES}-state search cap: "
                      f"{current_source} -> {len(target_set)} target node(s)")
            else:
                unreachable_legs += 1
                print(f"  No path for leg {i}: {current_source} -> {target_set}")

    return full_route, {
        "unreachable_legs": unreachable_legs,
        "truncated_legs": truncated_legs,
    }


def _clean_backtrack(route_nodes):
    """Remove consecutive duplicates and A→B→A out-and-back artifacts."""
    if not route_nodes:
        return []
    clean = [route_nodes[0]]
    for i in range(1, len(route_nodes)):
        curr = route_nodes[i]
        if curr == clean[-1]:
            continue
        if len(clean) >= 2 and curr == clean[-2]:
            clean.pop()
        else:
            clean.append(curr)
    return clean


def _collapse_revisits(route_nodes):
    """Drop revisit-loops by truncating to the first occurrence of any
    repeated node.

    Sequence-state Dijkstra (state = ``(node, sequence_idx, prev_node)``)
    permits a node to be reached multiple times via different predecessor
    chains. On junctions modelled as small circular street rings (e.g.
    the BFI IMAX, which is tagged ``junction=circular``, not
    ``junction=roundabout``, so the pipeline's ring-aggregation never
    fires), the sequence discount makes a second orbit competitive with
    the natural exit and the router happily laps.

    The collapse is structurally safe: the second occurrence of node
    ``X`` was reached from some predecessor and continued to some
    successor; that successor edge now follows the *first* occurrence
    of ``X`` in the collapsed list, and the edge ``X → successor``
    exists in the graph by construction.
    """
    if not route_nodes:
        return []
    out = [route_nodes[0]]
    seen = {route_nodes[0]: 0}
    for n in route_nodes[1:]:
        if n in seen:
            keep_until = seen[n]
            del out[keep_until + 1:]
            seen = {x: i for i, x in enumerate(out)}
        else:
            out.append(n)
            seen[n] = len(out) - 1
    return out


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
