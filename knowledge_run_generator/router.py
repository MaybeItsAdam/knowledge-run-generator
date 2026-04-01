import os
import math
import geopandas as gpd
import networkx as nx
import osmnx as ox
from pathlib import Path
from shapely.geometry import Point, LineString

CACHE_DIR = Path("/tmp/app_cache")
GRAPH_FILENAME = "london_drive_v2.graphml"

ox.settings.cache_folder = "/tmp/ox_cache"


def _best_edge_data(edge_bundle):
    """
    Return the shortest parallel edge data dict from a MultiDiGraph edge bundle.
    """
    if not edge_bundle:
        return None
    return min(edge_bundle.values(), key=lambda d: d.get("length", float("inf")))

def load_graph(place_name="Greater London, UK"):
    """
    Load the street network graph for the given place name.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    graph_path = CACHE_DIR / GRAPH_FILENAME

    if graph_path.exists():
        print(f"Loading graph from cache: {graph_path}")
        return ox.load_graphml(graph_path)

    print(f"Downloading graph for {place_name}...")
    # Use 'drive_service' to include PSV/Taxi access, avoiding private car restrictions
    G = ox.graph_from_place(place_name, network_type='drive_service')

    print("Saving graph to cache...")
    ox.save_graphml(G, graph_path)
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

    If *prohibited_turns* is provided (a set of ``(from, via, to)`` triples),
    edges involved in violations found on a first pass are penalised and the
    route is recalculated.

    Returns ``(route_nodes, metadata)`` where metadata contains:
      - total_distance  (metres)
      - streets_traversed  (ordered unique street names)
    """
    route = _route_through_waypoints(G, origin_node, dest_node, waypoint_nodes, intermediate_streets)

    # If we have turn restrictions, check for violations and re-route with penalties
    if prohibited_turns and route:
        route = _reroute_avoiding_violations(
            G, route, origin_node, dest_node, waypoint_nodes, prohibited_turns, intermediate_streets
        )

    clean = _clean_backtrack(route)
    metadata = _extract_route_metadata(G, clean)
    return clean, metadata


def _route_through_waypoints(G, origin_node, dest_node, waypoint_nodes, intermediate_streets=None):
    """Shortest-distance route leg-by-leg through ordered waypoints, with sequential street discounts."""
    from heapq import heappush, heappop
    import itertools

    norm_streets = []
    if intermediate_streets:
        for s in intermediate_streets:
            norm_streets.append(str(s).upper().strip().replace("'", "").replace(".", ""))

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

    c = itertools.count()

    for i, target_set in enumerate(stages):
        if current_source in target_set:
            continue
            
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

        # queue: (priority, dist_from_start, tie-breaker, current_node, current_street_idx)
        queue = [(_h(current_source), 0.0, next(c), current_source, current_street_idx)]
        visited = {(current_source, current_street_idx): 0.0}
        parents = {} # (u, idx) -> (prev_u, prev_idx)
        found_target = None # (node, idx)
        
        states_explored = 0
        while queue:
            priority, dist, _, u, s_idx = heappop(queue)
            states_explored += 1

            if states_explored > 500000:
                # Still fail if we hit a massive search space, but silently
                break

            if u in target_set:
                found_target = (u, s_idx)
                break
            
            # Since we update visited on push, we only continue if we found a better 
            # path to this state *since* it was pushed.
            if visited.get((u, s_idx), float('inf')) < dist:
                continue
            
            for v, edges in G[u].items():
                for k, edge_data in edges.items():
                    length = edge_data.get('length', 1.0)
                    
                    # Get or compute normalized names
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
                        edge_norms = [str(n).upper().strip().replace("'", "").replace(".", "") for n in names]
                        edge_norm_cache[eid] = edge_norms
                    
                    # Option 1: Normal routing cost, stay at current index
                    new_dist_opt1 = dist + length
                    if visited.get((v, s_idx), float('inf')) > new_dist_opt1:
                        visited[(v, s_idx)] = new_dist_opt1
                        priority = new_dist_opt1 + _h(v)
                        heappush(queue, (priority, new_dist_opt1, next(c), v, s_idx))
                        parents[(v, s_idx)] = (u, s_idx)
                        
                    # Option 2: Attempt to find a sequence discount match
                    if norm_streets and s_idx < len(norm_streets):
                        found_j = None
                        for en in edge_norms:
                            if en in street_to_indices:
                                for idx in street_to_indices[en]:
                                    if idx >= s_idx:
                                        if found_j is None or idx < found_j:
                                            found_j = idx
                            
                            if found_j is None or found_j > s_idx:
                                for j in range(s_idx, min(s_idx + 10, len(norm_streets))):
                                    expected = norm_streets[j]
                                    if en in expected or expected in en:
                                        if found_j is None or j < found_j:
                                            found_j = j
                                            break
                                            
                        if found_j is not None:
                            penalty = edge_data.get('penalty', 0.0)
                            base_length = length - penalty
                            discounted_cost = (base_length * 0.1) + penalty
                            new_dist_opt2 = dist + discounted_cost
                            
                            if visited.get((v, found_j), float('inf')) > new_dist_opt2:
                                visited[(v, found_j)] = new_dist_opt2
                                priority = new_dist_opt2 + _h(v)
                                heappush(queue, (priority, new_dist_opt2, next(c), v, found_j))
                                parents[(v, found_j)] = (u, s_idx)
                                
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
            print(f"  No path for leg {i}: {current_source} -> {target_set}")

    return full_route


def _reroute_avoiding_violations(G, route_nodes, origin_node, dest_node,
                                  waypoint_nodes, prohibited_turns, intermediate_streets=None,
                                  max_attempts=3):
    """
    Check the route for turn restriction violations.  For each violation,
    add a massive penalty weight to the (via → to) edge so the router avoids
    it on the next pass.  Repeats up to *max_attempts* times.

    Uses a temporary 'penalty' edge attribute so the original 'length'
    is preserved for calculations, but the Dijkstra sees the penalty.
    """
    PENALTY = 10_000_000  # 10,000 km — absolute deterrent
    penalised_edges = set()
    best = list(route_nodes)
    max_attempts = 10 # Increase attempts to clear multiple violations

    for attempt in range(max_attempts):
        # Find violations
        violations = []
        for i in range(len(best) - 2):
            triple = (best[i], best[i + 1], best[i + 2])
            if triple in prohibited_turns:
                violations.append(triple)

        if not violations:
            break

        # Penalise the (via → to) edge of each violation
        for _frm, via, to in violations:
            edge_key = (via, to)
            if edge_key in penalised_edges:
                continue  # already penalised
            penalised_edges.add(edge_key)
            data = G.get_edge_data(via, to)
            if data:
                for k in data:
                    G[via][to][k]['penalty'] = PENALTY
                    G[via][to][k]['length'] += PENALTY

        # Re-route
        best = _route_through_waypoints(G, origin_node, dest_node, waypoint_nodes, intermediate_streets)

    # Restore original edge weights
    for via, to in penalised_edges:
        data = G.get_edge_data(via, to)
        if data:
            for k in data:
                if 'penalty' in G[via][to][k]:
                    G[via][to][k]['length'] -= G[via][to][k]['penalty']
                    del G[via][to][k]['penalty']

    return best


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
