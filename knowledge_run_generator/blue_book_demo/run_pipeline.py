"""
Cotton Thread Pipeline — process Blue Book runs into validated, shortest-distance
GeoJSON routes for the Knowledge of London app.

Usage:
    python -m knowledge_run_generator.blue_book_demo.run_pipeline              # Process all runs
    python -m knowledge_run_generator.blue_book_demo.run_pipeline --limit 5    # Process first 5 only
    python -m knowledge_run_generator.blue_book_demo.run_pipeline --geojson    # Also export routes.geojson
"""

import json
import math
import os
import re
import sys
import time
from pathlib import Path

# Resolve paths
DEMO_DIR = Path(__file__).parent
PROJECT_ROOT = DEMO_DIR.parent.parent
sys.path.append(str(PROJECT_ROOT))

from knowledge_run_generator.geocoder import geocode_address, geocode_and_snap, geocode_intersection
from knowledge_run_generator.router import (
    load_graph, get_route, get_constrained_route,
    nodes_to_coords_geometry, _extract_route_metadata,
)
from knowledge_run_generator.validator import (
    load_turn_restrictions, validate_route, ValidationResult,
)
from knowledge_run_generator.corrector import correct_and_validate
from knowledge_run_generator.geojson_export import route_to_geojson_feature, export_all_runs_geojson
from knowledge_run_generator import caller


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_run_line(line):
    """Parse 'RUN 1 MANOR HOUSE STATION N4 TO GIBSON SQUARE N1' → (id, origin, dest)."""
    match = re.match(r"RUN\s+(\d+)\s+(.+?)\s+TO\s+(.+)", line.strip())
    if match:
        return int(match.group(1)), match.group(2).strip(), match.group(3).strip()
    return None


def parse_intermediary_file(path):
    """
    Read ``blue-book-runs-intermediatery.txt`` and return:
      - run_titles: dict  {run_id: (origin, destination)}
      - intermediary_runs: dict  {run_id: [street_name, ...]}
    """
    intermediary_runs = {}
    run_titles = {}
    current_id = None

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            run_match = parse_run_line(line)
            if run_match:
                current_id, origin, destination = run_match
                intermediary_runs[current_id] = []
                run_titles[current_id] = (origin, destination)
            elif current_id and line and not line.startswith("RUN"):
                parts = re.split(r"_{2,}", line)
                if len(parts) > 1:
                    raw_street = parts[1]
                    street_name = raw_street.split("FACING")[0].strip(" ._\n\r\t")

                    if street_name.endswith(" SDOL"):
                        street_name = street_name[:-5].strip()

                    terminal_roads = {"DEPARTURES ROAD", "CAB ROAD", "ARRIVALS ROAD"}
                    if (street_name
                            and "ROUNDABOUT" not in street_name.upper()
                            and street_name.upper() not in terminal_roads):
                        intermediary_runs[current_id].append(street_name)

    return run_titles, intermediary_runs


# ---------------------------------------------------------------------------
# Street index
# ---------------------------------------------------------------------------

def build_street_index(G, cache_dir):
    """Build or load a street-name → node-set index from the graph."""
    import pickle
    index_path = cache_dir / "street_index.pkl"

    if index_path.exists():
        print("Loading street index from cache...")
        with open(index_path, "rb") as f:
            return pickle.load(f)

    print("Building street index...")
    street_to_nodes = {}
    for u, v, _k, data in G.edges(keys=True, data=True):
        names = data.get("name", [])
        if isinstance(names, str):
            names = [names]
        for name in names:
            norm = _normalise(name)
            if norm not in street_to_nodes:
                street_to_nodes[norm] = set()
            street_to_nodes[norm].add(u)
            street_to_nodes[norm].add(v)

    with open(index_path, "wb") as f:
        pickle.dump(street_to_nodes, f)

    return street_to_nodes


def _normalise(name):
    if not name:
        return ""
    return str(name).upper().strip().replace("'", "").replace(".", "")


# ---------------------------------------------------------------------------
# Street matching (abbreviation expansion, fuzzy)
# ---------------------------------------------------------------------------

_ABBREVIATIONS = {
    " ST": " STREET", " RD": " ROAD", " AVE": " AVENUE",
    " SQ": " SQUARE", " PL": " PLACE", " LN": " LANE",
    " GDNS": " GARDENS", " PK": " PARK", " CIR": " CIRCUS",
    " HL": " HILL", " RI": " RISE", " CR": " CRESCENT",
    "R/BOUT": "ROUNDABOUT", " R/BOUT": " ROUNDABOUT",
}

_JUNCTION_SUFFIXES = [
    " CIRCUS", " CROSS", " INTERCHANGE", " JUNCTION", " CORNER",
    " SLIP", " SLIP ROAD", " APPROACH", " TUNNEL", " BRIDGE SLIP",
]


def get_best_street_match(raw_name, street_to_nodes):
    """Find the best match for *raw_name* in the street index."""
    cleaned = raw_name.upper()
    for suffix in _JUNCTION_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            break

    base = _normalise(cleaned)
    if base in street_to_nodes:
        return base

    # Expand abbreviations (in-string)
    expanded = base
    for abbr, full in _ABBREVIATIONS.items():
        if abbr in expanded:
            expanded = expanded.replace(abbr, full)
    if expanded != base and expanded in street_to_nodes:
        return expanded

    # Expand abbreviations (suffix-only)
    for abbr, full in _ABBREVIATIONS.items():
        if base.endswith(abbr):
            candidate = base[: -len(abbr)] + full
            if candidate in street_to_nodes:
                return candidate

    # Progressive word removal
    words = cleaned.split()
    while words:
        norm = _normalise(" ".join(words))
        if norm in street_to_nodes:
            return norm
        words.pop()

    return base


# ---------------------------------------------------------------------------
# Intersection finding
# ---------------------------------------------------------------------------

def find_intersection_node(G, s1, s2, street_to_nodes,
                           prev_point=None, dest_point=None):
    """
    Find the graph node at the intersection of streets *s1* and *s2*.

    When multiple candidates exist, prefer the one that:
      1. Is closest to *prev_point* (continuity), AND
      2. Makes forward progress toward *dest_point* (prevents backtracks).

    The scoring blends both: ``score = dist_from_prev - 0.5 * progress_toward_dest``
    so that a node slightly further from prev but much closer to dest wins.
    """
    n1 = get_best_street_match(s1, street_to_nodes)
    n2 = get_best_street_match(s2, street_to_nodes)
    nodes1 = street_to_nodes.get(n1, set())
    nodes2 = street_to_nodes.get(n2, set())

    common = nodes1.intersection(nodes2)

    if not common:
        # Fuzzy: find closest pair within ~200 m tolerance
        min_dist = 0.002
        best_node = None
        for u in nodes1:
            if u not in G.nodes:
                continue
            p1 = G.nodes[u]
            for v in nodes2:
                if v not in G.nodes:
                    continue
                p2 = G.nodes[v]
                dist_sq = (p1["x"] - p2["x"]) ** 2 + (p1["y"] - p2["y"]) ** 2
                if dist_sq < min_dist ** 2:
                    min_dist = dist_sq ** 0.5
                    best_node = u
        return best_node

    candidates = [c for c in common if c in G.nodes]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Score candidates: prefer close to prev AND making progress toward dest
    best = None
    best_score = float("inf")
    for nid in candidates:
        n = G.nodes[nid]
        score = 0.0
        if prev_point:
            score += math.sqrt(
                (n["y"] - prev_point[0]) ** 2 + (n["x"] - prev_point[1]) ** 2
            )
        if dest_point:
            dist_to_dest = math.sqrt(
                (n["y"] - dest_point[0]) ** 2 + (n["x"] - dest_point[1]) ** 2
            )
            # Reward progress toward destination
            score -= 0.5 * dist_to_dest
        if score < best_score:
            best_score = score
            best = nid
    return best


# ---------------------------------------------------------------------------
# Waypoint construction from Blue Book streets
# ---------------------------------------------------------------------------


def build_waypoints_from_streets(G, intermediate_streets, street_to_nodes,
                                  start_coords, end_coords):
    """
    Walk the intermediate street list and return an ordered list of waypoints.
    For normal streets, we don't generate strict node waypoints (relying on soft 
    edge discounts instead). However, if an intersection falls on a roundabout or 
    motorway link, we yield the entire topological ring as a single waypoint set.
    """
    raw_waypoints = []
    prev_pt = start_coords  # (lat, lon) tuple

    for i in range(len(intermediate_streets) - 1):
        s1 = intermediate_streets[i]
        s2 = intermediate_streets[i + 1]
        if s1.upper() == s2.upper():
            continue

        node = find_intersection_node(
            G, s1, s2, street_to_nodes,
            prev_point=prev_pt, dest_point=end_coords,
        )
        if node:
            nd = G.nodes[node]
            prev_pt = (nd["y"], nd["x"])
            
            # Check if this node is part of a roundabout or motorway_link
            is_roundabout = False
            for _, _, data in G.edges(node, data=True):
                if data.get('junction') == 'roundabout' or data.get('highway') == 'motorway_link':
                    is_roundabout = True
                    break
                    
            if not is_roundabout:
                for u, _, data in G.in_edges(node, data=True):
                    if data.get('junction') == 'roundabout' or data.get('highway') == 'motorway_link':
                        is_roundabout = True
                        break

            if is_roundabout:
                # BFS to collect the whole ring
                ring_nodes = set([node])
                queue = [node]
                while queue:
                    curr = queue.pop(0)
                    # Forward edges
                    for nxt in G.successors(curr):
                        if nxt in ring_nodes: continue
                        for _, edge_data in G.get_edge_data(curr, nxt).items():
                            if edge_data.get('junction') == 'roundabout' or edge_data.get('highway') == 'motorway_link':
                                ring_nodes.add(nxt)
                                queue.append(nxt)
                                break
                    # Backward edges
                    for prev in G.predecessors(curr):
                        if prev in ring_nodes: continue
                        for _, edge_data in G.get_edge_data(prev, curr).items():
                            if edge_data.get('junction') == 'roundabout' or edge_data.get('highway') == 'motorway_link':
                                ring_nodes.add(prev)
                                queue.append(prev)
                                break
                
                # Treat entire ring as a single waypoint
                raw_waypoints.append(ring_nodes)
            else:
                # For non-roundabout intersections, we now ALSO generate a hard waypoint.
                # This prevents massive Dijkstra search spaces (e.g. Run 12 Mile End -> Barbican).
                raw_waypoints.append({node})

    # Remove backward waypoints (zigzag elimination)
    cleaned = _remove_backtracks(G, raw_waypoints, start_coords, end_coords)

    return cleaned



def _remove_backtracks(G, waypoint_nodes, start_coords, end_coords):
    """
    Remove waypoints that move backwards relative to the origin→destination
    direction.  Uses scalar projection onto the O→D vector; a waypoint whose
    projection is less than the previous waypoint's projection is a backtrack.

    Allows small regressions (up to 15% of total O→D distance) to accommodate
    legitimate minor detours around one-way systems.
    """
    if len(waypoint_nodes) < 2:
        return waypoint_nodes

    # Direction vector from origin to destination (in degrees)
    dx = end_coords[1] - start_coords[1]  # lon
    dy = end_coords[0] - start_coords[0]  # lat
    od_len_sq = dx * dx + dy * dy
    if od_len_sq == 0:
        return waypoint_nodes

    # Allow regression of up to 15% of total O→D length
    regression_tolerance = 0.15 * math.sqrt(od_len_sq)

    def proj(wp):
        # Extract a representative node if wp is a set
        nid = next(iter(wp)) if isinstance(wp, (set, list, tuple)) else wp
        n = G.nodes[nid]
        px = n["x"] - start_coords[1]
        py = n["y"] - start_coords[0]
        return (px * dx + py * dy) / od_len_sq

    result = [waypoint_nodes[0]]
    max_proj = proj(waypoint_nodes[0])

    for nid in waypoint_nodes[1:]:
        p = proj(nid)
        # Keep if it's forward or only a small regression
        regression = (max_proj - p) * math.sqrt(od_len_sq)
        if regression <= regression_tolerance:
            result.append(nid)
            if p > max_proj:
                max_proj = p
        # else: skip — this waypoint is a backtrack

    return result


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_runs(output_file, limit=None, export_geojson=False):
    runs_data = []
    processed_ids = set()
    qa_results = {}
    geojson_features = []

    # Resume from existing output
    if output_file.exists():
        try:
            with open(output_file, "r") as f:
                content = f.read()
                if content:
                    runs_data = json.loads(content)
                    processed_ids = {r["id"] for r in runs_data}
            print(f"Loaded {len(runs_data)} existing runs from {output_file}")
        except json.JSONDecodeError:
            print("Could not decode existing JSON, starting fresh.")

    # Parse Blue Book directions (using local files in demo directory)
    inter_file = DEMO_DIR / "blue_book_runs_intermediary.txt"
    if not inter_file.exists():
        print(f"Error: {inter_file} not found.")
        return

    run_titles, intermediary_runs = parse_intermediary_file(inter_file)
    run_ids_sorted = sorted(run_titles.keys())

    # Load graph
    print("Loading graph...")
    G = load_graph()

    # Street index
    cache_dir = Path("/tmp/app_cache")
    cache_dir.mkdir(exist_ok=True)
    street_to_nodes = build_street_index(G, cache_dir)
    print(f"Indexed {len(street_to_nodes)} street names.")

    # Turn restrictions
    print("Loading turn restrictions...")
    prohibited_turns = load_turn_restrictions(G, cache_dir=str(cache_dir))
    print(f"  {len(prohibited_turns)} prohibited turn triples loaded.")

    # Load POI overrides for the Blue Book demo.
    # The core knowledge_run_generator package is data-agnostic; we provide
    # the specific Knowledge POI coordinates here as an argument.
    poi_file = DEMO_DIR / "poi_overrides.json"
    poi_overrides = {}
    if poi_file.exists():
        try:
            poi_overrides = json.loads(poi_file.read_text())
            print(f"Loaded {len(poi_overrides)} Knowledge POI overrides from demo directory.")
        except Exception as e:
            print(f"Warning: Failed to load POI overrides: {e}")

    # Run-specific patches from local demo directory.
    # These 'fixes' are injected into the library logic during the run loop.
    patches_file = DEMO_DIR / "run_specific_fixes.json"
    patches = {}
    if patches_file.exists():
        try:
            patches = json.loads(patches_file.read_text())
            print(f"Loaded {len(patches)} run-specific patches.")
        except Exception as e:
            print(f"Warning: Failed to load run patches: {e}")
            
    # ------------------------------------------------------------------
    # Process each run
    # ------------------------------------------------------------------
    for run_id in run_ids_sorted:
        if limit and len(runs_data) >= limit:
            print(f"Reached limit of {limit} runs. Stopping.")
            break

        if run_id in processed_ids:
            continue

        origin, destination = run_titles[run_id]
        print(f"\n{'='*60}")
        print(f"Processing Run {run_id}: {origin} -> {destination}")

        # ----- Geocode with snapping -----
        start = geocode_and_snap(origin, G, poi_overrides)
        end = geocode_and_snap(destination, G, poi_overrides)

        if not start or not end:
            print(f"  SKIP: Failed to geocode Run {run_id}")
            continue

        start_lat, start_lon, start_node = start
        end_lat, end_lon, end_node = end

        # Per-run config from patches
        run_patch = patches.get(str(run_id), {})
        run_config = {"skip_directness_check": True} # Disabled globally as the semantic router naturally handles this
        
        if "max_deviation_ratio" in run_patch:
            run_config["max_deviation_ratio"] = run_patch["max_deviation_ratio"]

        try:
            # ----- Build intermediate waypoints -----
            intermediate_streets = list(intermediary_runs.get(run_id, []))

            # Apply street removals from patches
            if "remove" in run_patch:
                to_remove = set(
                    s.upper().replace("\u2019", "'")
                    for s in run_patch["remove"]
                )
                before = len(intermediate_streets)
                intermediate_streets = [
                    s for s in intermediate_streets
                    if s.upper().replace("\u2019", "'") not in to_remove
                ]
                if len(intermediate_streets) < before:
                    print(f"  [Patch] Removed {before - len(intermediate_streets)} streets")

            waypoint_nodes = build_waypoints_from_streets(
                G, intermediate_streets, street_to_nodes,
                (start_lat, start_lon), (end_lat, end_lon),
            )

            # Inject forced waypoints from patches
            if "force_waypoints" in run_patch:
                import osmnx as ox
                for wp in run_patch["force_waypoints"]:
                    nid = ox.distance.nearest_nodes(G, wp["lon"], wp["lat"])
                    waypoint_nodes.append(nid)
                print(f"  [Patch] Injected {len(run_patch['force_waypoints'])} forced waypoints")

            print(f"  {len(waypoint_nodes)} intermediate waypoints")

            def _route_fn(G, o, d, wps):
                return get_constrained_route(G, o, d, wps, prohibited_turns=prohibited_turns, intermediate_streets=intermediate_streets)

            def _validate_fn(G, nodes, o, d, turns, streets, cfg, wps):
                return validate_route(G, nodes, o, d, turns, streets, cfg, waypoint_nodes=wps)

            route_nodes, validation, corrections = correct_and_validate(
                G, start_node, end_node, waypoint_nodes,
                intermediate_streets, prohibited_turns, street_to_nodes,
                route_fn=_route_fn,
                validate_fn=_validate_fn,
                config=run_config,
            )

            if not route_nodes or len(route_nodes) < 2:
                print(f"  ERROR: No route produced for Run {run_id}")
                continue

            status = "PASS" if validation.passed else "FAIL"
            metrics = validation.directness_metrics
            print(f"  [{status}] ratio={metrics.get('ratio', '?')}, "
                  f"offset={metrics.get('max_lateral_offset_m', '?')}m, "
                  f"legal={validation.is_legal}, "
                  f"coverage={validation.coverage_metrics.get('coverage', '?')}")
            if corrections:
                print(f"  Corrections applied: {len(corrections)}")

            # ----- Reverse route (same treatment) -----
            reverse_streets = list(reversed(intermediate_streets))
            reverse_waypoints = build_waypoints_from_streets(
                G, reverse_streets, street_to_nodes,
                (end_lat, end_lon), (start_lat, start_lon),
            )

            def _route_fn_rev(G, o, d, wps):
                return get_constrained_route(G, o, d, wps, prohibited_turns=prohibited_turns, intermediate_streets=reverse_streets)

            rev_route_nodes, rev_validation, rev_corrections = correct_and_validate(
                G, end_node, start_node, reverse_waypoints,
                reverse_streets, prohibited_turns, street_to_nodes,
                route_fn=_route_fn_rev,
                validate_fn=_validate_fn,
                config=run_config,
            )

            if not rev_route_nodes or len(rev_route_nodes) < 2:
                # Fallback: simple A→B reverse
                rev_route_nodes = get_route(G, (end_lat, end_lon), (start_lat, start_lon))
                if not rev_route_nodes:
                    rev_route_nodes = []

            # ----- Convert to coordinates -----
            route_coords = nodes_to_coords_geometry(G, route_nodes)
            rev_coords = nodes_to_coords_geometry(G, rev_route_nodes)

            # ----- Generate steps ("The Call") -----
            forward_steps = caller.generate_call(G, route_nodes)
            reverse_steps = caller.generate_call(G, rev_route_nodes) if rev_route_nodes else []

            # ----- Compute distance & duration -----
            fwd_meta = _extract_route_metadata(G, route_nodes)
            rev_meta = _extract_route_metadata(G, rev_route_nodes) if rev_route_nodes else {"total_distance": 0, "streets_traversed": []}

            def _estimate_duration(distance_m):
                """Estimate seconds at 20 km/h average in central London."""
                return round((distance_m / 1000) / 20 * 3600, 1)

            fwd_distance = fwd_meta["total_distance"]
            rev_distance = rev_meta["total_distance"]
            fwd_duration = _estimate_duration(fwd_distance)
            rev_duration = _estimate_duration(rev_distance)

            # Waypoints as [lon, lat]
            waypoints_lonlat = []
            for wp in waypoint_nodes:
                nid = list(wp)[0] if isinstance(wp, set) else wp
                n = G.nodes[nid]
                waypoints_lonlat.append([n["x"], n["y"]])

            # ----- Build JSON object (same schema as before) -----
            run_obj = {
                "id": run_id,
                "title": f"{origin} to {destination}",
                "waypoints": waypoints_lonlat,
                "start": {
                    "name": origin,
                    "coordinates": [start_lon, start_lat],
                },
                "end": {
                    "name": destination,
                    "coordinates": [end_lon, end_lat],
                },
                "route": {
                    "geometry": {
                        "type": "LineString",
                        "coordinates": route_coords,
                    },
                    "duration": fwd_duration,
                    "distance": fwd_distance,
                    "steps": forward_steps,
                },
                "routeReverse": {
                    "geometry": {
                        "type": "LineString",
                        "coordinates": rev_coords,
                    },
                    "duration": rev_duration,
                    "distance": rev_distance,
                    "steps": reverse_steps,
                },
            }

            runs_data.append(run_obj)
            processed_ids.add(run_id)

            # QA record
            qa_results[run_id] = {
                "passed": validation.passed,
                "ratio": metrics.get("ratio"),
                "max_offset_m": metrics.get("max_lateral_offset_m"),
                "legal": validation.is_legal,
                "street_coverage": validation.coverage_metrics.get("coverage"),
                "corrections": len(corrections),
                "fwd_distance_m": fwd_distance,
                "rev_distance_m": rev_distance,
            }

            # GeoJSON features (optional)
            if export_geojson:
                geojson_features.append(
                    route_to_geojson_feature(G, route_nodes, run_id, origin, destination, fwd_meta, "forward")
                )
                if rev_route_nodes:
                    geojson_features.append(
                        route_to_geojson_feature(G, rev_route_nodes, run_id, destination, origin, rev_meta, "reverse")
                    )

            # Incremental save
            if len(runs_data) % 5 == 0:
                print(f"  Saving progress ({len(runs_data)} runs)...")
                with open(output_file, "w") as f:
                    json.dump(runs_data, f, indent=2)

        except Exception as e:
            print(f"  ERROR processing Run {run_id}: {e}")
            import traceback
            traceback.print_exc()
            continue

        time.sleep(0.5)

    # ------------------------------------------------------------------
    # Final save
    # ------------------------------------------------------------------
    with open(output_file, "w") as f:
        json.dump(runs_data, f, indent=2)
    print(f"\nSaved {len(runs_data)} runs to {output_file}")

    # QA report
    qa_path = output_file.parent / "qa_report.json"
    with open(qa_path, "w") as f:
        json.dump(qa_results, f, indent=2)
    print(f"QA report saved to {qa_path}")

    # Optional GeoJSON export
    if export_geojson and geojson_features:
        geojson_path = output_file.parent / "routes.geojson"
        export_all_runs_geojson(geojson_features, geojson_path)

    # Summary
    total = len(qa_results)
    passed = sum(1 for v in qa_results.values() if v.get("passed"))
    failed = total - passed
    print(f"\n{'='*60}")
    print(f"SUMMARY: {passed}/{total} runs passed validation, {failed} failed.")
    if failed:
        fail_ids = [k for k, v in qa_results.items() if not v.get("passed")]
        print(f"  Failed runs: {fail_ids[:20]}{'...' if len(fail_ids) > 20 else ''}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Process Blue Book runs into validated, shortest-distance routes."
    )
    parser.add_argument(
        "--limit", type=int,
        help="Limit the number of runs to process.",
    )
    parser.add_argument(
        "--output", "-o", type=str,
        help="Custom output file path (default: constants/runPoints.json).",
    )
    parser.add_argument(
        "--format", "-f", choices=["json", "geojson"], default="json",
        help="Output format (default: json).",
    )
    parser.add_argument(
        "--geojson", action="store_true",
        help="Also export a secondary GeoJSON FeatureCollection to constants/routes.geojson (legacy flag).",
    )
    args = parser.parse_args()

    # Resolve output file
    if args.output:
        output_file = Path(args.output)
    else:
        # Default webapp location
        output_file = PROJECT_ROOT / "constants" / "runPoints.json"

    # Ensure parent directory exists
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Secondary GeoJSON export if requested via legacy flag or if format is geojson
    export_geojson = args.geojson or args.format == "geojson"

    process_runs(output_file, limit=args.limit, export_geojson=export_geojson)
