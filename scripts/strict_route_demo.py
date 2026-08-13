"""
Build the 5 demo blue book routes by strictly walking the named-street sequence
from "The Blue Book Runs of the Knowledge of London.txt".

For each run:
  origin -> [street_1] -> intersection_1_2 -> [street_2] -> ... -> destination

Each leg is a Dijkstra search constrained to edges whose name matches the
current street, so the route is forced to traverse every named street in order.

Outputs: writes constants/runPoints_strict_demo.json (never the canonical
runPoints.json).
"""

from __future__ import annotations

import heapq
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from knowledge_run_generator import caller
from knowledge_run_generator.blue_book_demo.run_pipeline import (
    _ABBREVIATIONS,
    _JUNCTION_SUFFIXES,
    _normalise,
    build_street_index,
)
from knowledge_run_generator.router import (
    _extract_route_metadata,
    load_graph,
    nodes_to_coords_geometry,
)
import osmnx as ox

from knowledge_run_generator.cache import cache_dir as _krg_cache_dir

CACHE_DIR = _krg_cache_dir()
TXT_FILE = ROOT / "The Blue Book Runs of the Knowledge of London.txt"
# Deliberately NOT constants/runPoints.json: this demo must never clobber the
# canonical pipeline output.
OUTPUT_FILE = ROOT / "constants" / "runPoints_strict_demo.json"
DEMO_DIR = ROOT / "knowledge_run_generator" / "blue_book_demo"

# direction-verb tokens that appear before a street name in the txt file
DIR_VERBS = {
    "L", "R", "F", "COM", "L/BY", "B/R", "B/L", "L BY", "L BY F", "L BY R",
    "L BY L", "LOR", "LOL", "L & R", "L & L", "L & F", "F & R", "R & L", "R & F",
    "L/BY L", "L/BY F", "L/BY R", "L & R BY", "B/L", "B/R",
}

# trailing destination-marker tokens
END_MARKERS = re.compile(
    r"\s+(SDOL|SDOR|FACING|ON LEFT.*|ON RIGHT.*|ON LEFT & RIGHT.*)$"
)


def parse_blue_book_txt(path: Path) -> dict[int, dict]:
    """Return {run_id: {"origin": str, "destination": str, "streets": [str, ...]}}."""
    out: dict[int, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("RUN"):
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        header, body = parts
        m = re.match(r"RUN\s+(\d+)\s+(.+?)\s+TO\s+(.+)", header.strip())
        if not m:
            continue
        rid = int(m.group(1))
        origin, destination = m.group(2).strip(), m.group(3).strip()

        # Split the body on runs of 2+ underscores; each result is "<street_name> <next_dir_verb>"
        # The very first token is the leading direction verb (e.g. "LOL"), drop it.
        raw = re.split(r"_{2,}", body)
        streets: list[str] = []
        for idx, tok in enumerate(raw):
            t = tok.strip()
            if not t:
                continue
            if idx == 0:
                # first piece is the leading direction verb
                continue
            # strip trailing direction verb (last word(s)) — naive: drop last token if it's a known verb
            t_clean = END_MARKERS.sub("", t).strip()
            words = t_clean.split()
            # try to peel off trailing direction verbs greedily (could be multi-word like "L & R" or "F & R")
            while words:
                tail_3 = " ".join(words[-3:])
                tail_2 = " ".join(words[-2:])
                tail_1 = words[-1]
                if tail_3.upper() in DIR_VERBS:
                    words = words[:-3]
                elif tail_2.upper() in DIR_VERBS:
                    words = words[:-2]
                elif tail_1.upper() in DIR_VERBS:
                    words = words[:-1]
                elif tail_1 == "&":
                    # leftover ampersand from a partially-stripped multi-word verb
                    words = words[:-1]
                else:
                    break
            street = " ".join(words).strip()
            if not street:
                continue
            if "ROUNDABOUT" in street.upper():
                # skip generic roundabout markers (the actual route may pass through; not a named street)
                continue
            streets.append(street)
        out[rid] = {"origin": origin, "destination": destination, "streets": streets}
    return out


_EXTRA_ABBREV = {
    "GT ": "GREAT ",
    " ST ": " STREET ",
    " ST'S ": " STREET ",
    "BISHOP'S ": "BISHOPS ",
    "ST. ": "ST ",
}


def _expand_abbrev(name: str) -> str:
    out = name
    for a, full in _ABBREVIATIONS.items():
        out = out.replace(a, full)
    for a, full in _EXTRA_ABBREV.items():
        if out.startswith(a):
            out = full + out[len(a):]
        out = out.replace(a, full)
    return out


def resolve_street_to_canonical(name: str, street_to_nodes: dict) -> str | None:
    """Return the canonical key in street_to_nodes that matches *name*, or None."""
    cleaned = name.upper()
    for suffix in _JUNCTION_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            break
    base = _normalise(cleaned)
    if base in street_to_nodes:
        return base
    expanded = _normalise(_expand_abbrev(cleaned))
    if expanded in street_to_nodes:
        return expanded
    # progressive trim
    words = cleaned.split()
    while words:
        cand = _normalise(" ".join(words))
        if cand in street_to_nodes:
            return cand
        words.pop()
    return None


def edge_names(G, u, v) -> set[str]:
    bundle = G.get_edge_data(u, v) or {}
    out: set[str] = set()
    for data in bundle.values():
        n = data.get("name")
        if isinstance(n, str):
            out.add(_normalise(n))
        elif isinstance(n, list):
            for x in n:
                out.add(_normalise(str(x)))
    return out


def edge_length(G, u, v) -> float:
    bundle = G.get_edge_data(u, v) or {}
    if not bundle:
        return float("inf")
    return min(float(d.get("length", 1.0) or 1.0) for d in bundle.values())


def dijkstra_on_street(G, start: int, goal: int, street_canon: str) -> list[int] | None:
    """Shortest path from start to goal using ONLY edges whose name matches street_canon."""
    if start == goal:
        return [start]
    pq = [(0.0, start, None)]
    came_from: dict[int, int] = {}
    best: dict[int, float] = {start: 0.0}
    while pq:
        d, u, prev = heapq.heappop(pq)
        if u == goal:
            # reconstruct
            path = [u]
            cur = u
            while cur in came_from:
                cur = came_from[cur]
                path.append(cur)
            return list(reversed(path))
        if d > best.get(u, float("inf")):
            continue
        for v in G.successors(u):
            names = edge_names(G, u, v)
            if street_canon not in names:
                continue
            nd = d + edge_length(G, u, v)
            if nd < best.get(v, float("inf")):
                best[v] = nd
                came_from[v] = u
                heapq.heappush(pq, (nd, v, u))
    return None


def find_street_intersection(
    G,
    canon_a: str,
    canon_b: str,
    street_to_nodes: dict,
    near_node: int | None = None,
) -> int | None:
    """Find a node adjacent to edges named canon_a AND canon_b. Prefer one near *near_node*."""
    nodes_a = street_to_nodes.get(canon_a, set())
    nodes_b = street_to_nodes.get(canon_b, set())
    common = (nodes_a & nodes_b) & set(G.nodes)
    if not common:
        return None
    if near_node is None or near_node not in G.nodes:
        return next(iter(common))
    nx, ny = G.nodes[near_node]["x"], G.nodes[near_node]["y"]
    return min(
        common,
        key=lambda nid: (G.nodes[nid]["x"] - nx) ** 2 + (G.nodes[nid]["y"] - ny) ** 2,
    )


def snap_to_street(G, lat: float, lon: float, canon: str, street_to_nodes: dict) -> int | None:
    """Snap (lat, lon) to the nearest node that lies on the named street."""
    nodes = street_to_nodes.get(canon, set()) & set(G.nodes)
    if not nodes:
        return None
    return min(
        nodes,
        key=lambda nid: (G.nodes[nid]["x"] - lon) ** 2 + (G.nodes[nid]["y"] - lat) ** 2,
    )


def build_strict_route(
    G,
    origin_coords: tuple[float, float],
    dest_coords: tuple[float, float],
    streets: list[str],
    street_to_nodes: dict,
) -> tuple[list[int], list[str], list[str]]:
    """
    Build a route node sequence by walking each named street in order.

    Returns (route_nodes, resolved_street_canon_per_step, warnings).
    """
    warnings: list[str] = []

    # Resolve each named street to a canonical key
    canon: list[str | None] = []
    for s in streets:
        c = resolve_street_to_canonical(s, street_to_nodes)
        if c is None:
            warnings.append(f"unresolved street: {s!r}")
        canon.append(c)

    # Compute per-step entry/exit nodes
    # step i runs along canon[i] from entry[i] to exit[i]
    steps: list[tuple[str, int, int]] = []

    # Entry into the first street: snap origin to the first resolved canonical street
    first_idx = next((i for i, c in enumerate(canon) if c is not None), None)
    if first_idx is None:
        return [], [], ["no streets resolved at all"]

    entry = snap_to_street(G, origin_coords[0], origin_coords[1], canon[first_idx], street_to_nodes)
    if entry is None:
        return [], [], [f"could not snap origin to {streets[first_idx]!r}"]

    # Exit from the last street: snap dest to the last resolved canonical street
    last_idx = next((i for i, c in reversed(list(enumerate(canon))) if c is not None), first_idx)
    exit_dest = snap_to_street(G, dest_coords[0], dest_coords[1], canon[last_idx], street_to_nodes)
    if exit_dest is None:
        return [], [], [f"could not snap destination to {streets[last_idx]!r}"]

    # For each adjacent pair (canon[i], canon[i+1]) (where both are resolved), find the intersection
    intersections: dict[int, int] = {}  # i -> intersection node BETWEEN canon[i] and canon[i+1]
    last_known_node = entry
    for i in range(len(canon) - 1):
        a, b = canon[i], canon[i + 1]
        if a is None or b is None:
            continue
        if a == b:
            continue
        node = find_street_intersection(G, a, b, street_to_nodes, near_node=last_known_node)
        if node is None:
            warnings.append(f"no intersection between {streets[i]!r} and {streets[i+1]!r}")
            continue
        intersections[i] = node
        last_known_node = node

    # Now assemble the per-step list (street_canon, entry_node, exit_node)
    cur_entry = entry
    for i, c in enumerate(canon):
        if c is None:
            continue
        if i in intersections:
            step_exit = intersections[i]
        elif i == last_idx:
            step_exit = exit_dest
        else:
            # No intersection for this step; we'll allow the next step's entry to also serve as ours
            # Find the next intersection downstream
            next_int = next(
                (intersections[j] for j in range(i + 1, len(canon)) if j in intersections),
                exit_dest,
            )
            step_exit = next_int
        if cur_entry == step_exit:
            continue
        steps.append((c, cur_entry, step_exit))
        cur_entry = step_exit

    if cur_entry != exit_dest:
        # final hop within the last street
        steps.append((canon[last_idx], cur_entry, exit_dest))

    # Walk each step on its own street; if no in-street path, fall back to any-edge Dijkstra
    full_route: list[int] = []
    for c, u, v in steps:
        path = dijkstra_on_street(G, u, v, c)
        if path is None:
            warnings.append(
                f"no in-street path on {c!r} from {u} to {v}; falling back to graph Dijkstra"
            )
            try:
                import networkx as nx
                path = nx.shortest_path(G, u, v, weight="length")
            except Exception:
                path = [u, v]
        if full_route and path and path[0] == full_route[-1]:
            full_route.extend(path[1:])
        else:
            full_route.extend(path)

    # Resolved street labels (parallel to streets[])
    return full_route, [c or "" for c in canon], warnings


def _estimate_duration(distance_m: float) -> float:
    return round((distance_m / 1000) / 20 * 3600, 1)


def main(limit: int = 5):
    print("Loading graph...")
    G = load_graph()
    print(f"  {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    CACHE_DIR.mkdir(exist_ok=True)
    print("Loading street index...")
    street_to_nodes = build_street_index(G, CACHE_DIR)
    print(f"  {len(street_to_nodes)} canonical streets")

    # Reuse the previously-geocoded origin/dest coords from the existing
    # runPoints.json (avoids hammering Nominatim, which rate-limits at 1 req/s).
    print("Loading existing start/end coordinates from current runPoints.json...")
    existing = json.loads(OUTPUT_FILE.read_text())
    coords_by_id = {
        int(r["id"]): {
            "start_name": r["start"]["name"],
            "end_name": r["end"]["name"],
            # runPoints stores as [lon, lat]
            "start_lonlat": r["start"]["coordinates"],
            "end_lonlat": r["end"]["coordinates"],
        }
        for r in existing
    }
    print(f"  reused coords for {len(coords_by_id)} runs")

    print(f"Parsing {TXT_FILE.name}...")
    runs = parse_blue_book_txt(TXT_FILE)
    print(f"  {len(runs)} runs in source file")

    out_runs = []
    for rid in sorted(coords_by_id)[:limit]:
        meta = runs.get(rid)
        if not meta:
            print(f"\nRun {rid}: not in source txt — skipping")
            continue
        origin, destination = meta["origin"], meta["destination"]
        streets = meta["streets"]
        print(f"\nRun {rid}: {origin} -> {destination}  ({len(streets)} streets)")

        cached = coords_by_id[rid]
        s_lon, s_lat = cached["start_lonlat"]
        e_lon, e_lat = cached["end_lonlat"]
        s_node = ox.distance.nearest_nodes(G, s_lon, s_lat)
        e_node = ox.distance.nearest_nodes(G, e_lon, e_lat)

        route_nodes, canons, warnings = build_strict_route(
            G, (s_lat, s_lon), (e_lat, e_lon), streets, street_to_nodes
        )
        for w in warnings:
            print(f"  [warn] {w}")
        if not route_nodes:
            print("  SKIP: no route built")
            continue

        coords = nodes_to_coords_geometry(G, route_nodes)
        meta_fwd = _extract_route_metadata(G, route_nodes)
        distance = float(meta_fwd.get("total_distance") or 0)
        duration = _estimate_duration(distance)
        steps = caller.generate_call(G, route_nodes)

        # Build reverse: walk streets in reverse order
        rev_streets = list(reversed(streets))
        rev_route_nodes, _, rev_warnings = build_strict_route(
            G, (e_lat, e_lon), (s_lat, s_lon), rev_streets, street_to_nodes
        )
        for w in rev_warnings:
            print(f"  [warn-rev] {w}")
        rev_coords = nodes_to_coords_geometry(G, rev_route_nodes) if rev_route_nodes else []
        rev_meta = _extract_route_metadata(G, rev_route_nodes) if rev_route_nodes else {"total_distance": 0}
        rev_distance = float(rev_meta.get("total_distance") or 0)
        rev_duration = _estimate_duration(rev_distance)
        rev_steps = caller.generate_call(G, rev_route_nodes) if rev_route_nodes else []

        # Coverage check
        actual_streets_in_route: list[str] = []
        last = None
        for u, v in zip(route_nodes[:-1], route_nodes[1:]):
            ns = edge_names(G, u, v)
            if not ns:
                continue
            primary = sorted(ns)[0]
            if primary != last:
                actual_streets_in_route.append(primary)
                last = primary
        actual_set = set(actual_streets_in_route)
        coverage = sum(1 for c in canons if c and c in actual_set) / max(1, sum(1 for c in canons if c))
        print(f"  distance={distance:.0f}m  duration={duration:.0f}s  steps={len(steps)}  coverage={coverage:.2f}")

        out_runs.append(
            {
                "id": rid,
                "title": f"{origin} to {destination}",
                "waypoints": [
                    [G.nodes[n]["x"], G.nodes[n]["y"]]
                    for n in route_nodes[:: max(1, len(route_nodes) // 12)]
                ],
                "start": {"name": origin, "coordinates": [s_lon, s_lat]},
                "end": {"name": destination, "coordinates": [e_lon, e_lat]},
                "route": {
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "duration": duration,
                    "distance": distance,
                    "steps": steps,
                    "nodes": [int(n) for n in route_nodes],
                },
                "routeReverse": {
                    "geometry": {"type": "LineString", "coordinates": rev_coords},
                    "duration": rev_duration,
                    "distance": rev_distance,
                    "steps": rev_steps,
                    "nodes": [int(n) for n in (rev_route_nodes or [])],
                },
            }
        )

    OUTPUT_FILE.write_text(json.dumps(out_runs, indent=2))
    print(f"\nWrote {len(out_runs)} runs to {OUTPUT_FILE}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args()
    main(limit=args.limit)
