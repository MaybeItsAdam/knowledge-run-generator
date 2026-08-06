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
    check_run_shape, load_turn_restrictions, validate_route, ValidationResult,
)
from knowledge_run_generator.corrector import correct_and_validate
from knowledge_run_generator.geojson_export import route_to_geojson_feature, export_all_runs_geojson
from knowledge_run_generator.aliases import (
    load_or_build_alias_index, normalise as _canonical_normalise,
)
from knowledge_run_generator.gazetteer import (
    DEFAULT_KNOWLEDGE_POIS_PATH, Gazetteer, load_knowledge_pois, preflight_run,
)
from knowledge_run_generator.cache import cache_dir as krg_cache_dir
from knowledge_run_generator.junctions import build_junction_index
from knowledge_run_generator.osm_pois import load_cached_pois
from knowledge_run_generator import caller


# Version stamp for qa_report.json, written into ``_provenance``. Bump whenever
# the per-run record schema changes shape (e.g. when "status" was introduced)
# so a resume can tell a current record from one written by older code and
# re-route the run instead of carrying the stale record forward.
QA_SCHEMA_VERSION = 2


def _json_default(o):
    """JSON fallback for numpy scalars.

    osmnx/validator math returns numpy types (``bool_``, ``int64``,
    ``float64``) that leak into the QA metrics. The stdlib encoder can't
    serialise them, which previously crashed the whole run *after* the
    routes were computed. Coerce anything with ``.item()`` to its Python
    scalar; everything else is a genuine bug worth surfacing.
    """
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _save_runs(output_file, runs_data):
    """Write ``runs_data`` to *output_file*, ordered by run id.

    Regenerated runs are appended to the end of the in-memory list, so without
    an explicit sort the file's order drifts from 1..320 after a resume.
    """
    ordered = sorted(runs_data, key=lambda r: r.get("id", 0))
    with open(output_file, "w") as f:
        json.dump(ordered, f, indent=2, default=_json_default)


def _partition_resumable_qa(existing_qa):
    """Split an existing qa_report dict into ``(carry_forward, stale_run_ids)``.

    A per-run record may only be carried across a resume when it is
    trustworthy: it must carry the ``"status"`` field (older reports predate
    it) and the report must have been written by the current
    :data:`QA_SCHEMA_VERSION`. Anything else is stale — the run has to be
    re-routed, not silently resumed past.

    Meta keys (``_provenance``, ``_completeness``) are regenerated at save
    time and are never treated as run records.
    """
    version = None
    provenance = existing_qa.get("_provenance")
    if isinstance(provenance, dict):
        version = provenance.get("qa_schema_version")

    carry = {}
    stale_ids = set()
    for key, value in existing_qa.items():
        if not str(key).lstrip("-").isdigit():
            continue
        if (version == QA_SCHEMA_VERSION
                and isinstance(value, dict)
                and "status" in value):
            carry[key] = value
        else:
            stale_ids.add(int(key))
    return carry, stale_ids


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_run_line(line):
    """Parse 'RUN 1 MANOR HOUSE STATION N4 TO GIBSON SQUARE N1' → (id, origin, dest)."""
    match = re.match(r"RUN\s+(\d+)\s+(.+?)\s+TO\s+(.+)", line.strip())
    if match:
        return int(match.group(1)), match.group(2).strip(), match.group(3).strip()
    return None


# Trailing set-down/annotation markers on a street line. Mirrors
# scripts/strict_route_demo.py: SDOL/SDOR (set down on left/right), FACING,
# and "<name> ON LEFT / ON RIGHT / ON LEFT & RIGHT" arrival notes.
_END_MARKERS = re.compile(
    r"\s+(SDOL|SDOR|FACING|ON LEFT.*|ON RIGHT.*|ON LEFT & RIGHT.*)$"
)

# "MANOR HOUSE STATION N4" -> stem "MANOR HOUSE STATION" (postcode dropped),
# used to peel the destination name off the final street line.
_POSTCODE_RE = re.compile(r"\s+[A-Z]{1,2}\d{1,2}[A-Z]?\s*[.,]?\s*$")


def _clean_street_line(raw_street, destination=None):
    """Reduce a Blue Book street line to the street name itself.

    Strips trailing arrival annotations (``... FACING``, ``... SDOL/SDOR``,
    ``... <NAME> ON LEFT/ON RIGHT``), then — because the final line of a run
    reads "<street> <destination> FACING/ON LEFT" — peels the run's
    destination name off the tail if it is still there. Finally drops a
    leading "CROSS " verb ("CROSS FULHAM ROAD" names Fulham Road, not a
    street called Cross), guarded so a street actually *named* "Cross
    Something" single-word remainder is left alone.
    """
    street = _END_MARKERS.sub("", raw_street).strip(" ._\n\r\t")
    if not street:
        return ""

    # "FACING" may be mid-token when the annotation carried extra words the
    # regex anchored off; the legacy split is kept as a belt-and-braces pass.
    street = street.split("FACING")[0].strip(" ._\n\r\t")

    if destination:
        dest_stem = _POSTCODE_RE.sub("", destination.upper()).strip()
        upper = street.upper()
        if dest_stem and upper != dest_stem and upper.endswith(" " + dest_stem):
            street = street[: -(len(dest_stem) + 1)].strip(" ._\n\r\t")

    if street.upper().startswith("CROSS ") and len(street.split()) >= 3:
        street = street[6:].strip()

    return street


def parse_intermediary_file(path):
    """
    Read ``blue-book-runs-intermediatery.txt`` and return:
      - run_titles: dict  {run_id: (origin, destination)}
      - intermediary_runs: dict  {run_id: [street_name, ...]}
    """
    intermediary_runs = {}
    run_titles = {}
    current_id = None
    spelling_fixes = load_street_spelling_fixes()

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
                    street_name = _clean_street_line(
                        parts[1], destination=run_titles[current_id][1]
                    )
                    street_name = spelling_fixes.get(street_name.upper(), street_name)

                    terminal_roads = {"DEPARTURES ROAD", "CAB ROAD", "ARRIVALS ROAD"}
                    if (street_name
                            and "ROUNDABOUT" not in street_name.upper()
                            and street_name.upper() not in terminal_roads):
                        intermediary_runs[current_id].append(street_name)

    return run_titles, intermediary_runs


_spelling_fixes_cache = None


def load_street_spelling_fixes(path=None):
    """Blue Book typo → correct street name map (upper-cased keys).

    Curated in ``street_spelling_fixes.json`` next to the run text. Applied
    at parse time so the preflight check, the waypoint builder and the
    coverage metric all see the same corrected names.
    """
    global _spelling_fixes_cache
    if path is None and _spelling_fixes_cache is not None:
        return _spelling_fixes_cache
    fixes_path = Path(path) if path else DEMO_DIR / "street_spelling_fixes.json"
    fixes = {}
    if fixes_path.exists():
        try:
            raw = json.loads(fixes_path.read_text())
            fixes = {str(k).upper(): str(v) for k, v in raw.items()}
        except Exception as exc:
            print(f"Warning: could not load street spelling fixes: {exc}")
    if path is None:
        _spelling_fixes_cache = fixes
    return fixes


# ---------------------------------------------------------------------------
# Street index
# ---------------------------------------------------------------------------

def build_street_index(G, cache_dir):
    """Build or load a street-name → node-set index from the graph.

    Keyed by the graph's fingerprint so an index built against a different
    graph (or by an older normaliser) is rebuilt rather than reused.
    """
    import pickle
    from knowledge_run_generator.aliases import graph_fingerprint

    index_path = cache_dir / "street_index.pkl"
    fingerprint = graph_fingerprint(G)

    if index_path.exists():
        try:
            with open(index_path, "rb") as f:
                blob = pickle.load(f)
            if isinstance(blob, dict) and blob.get("fingerprint") == fingerprint:
                print("Loading street index from cache...")
                return blob["index"]
        except Exception as exc:
            print(f"  Ignoring unreadable street index cache: {exc}")

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
        pickle.dump({"fingerprint": fingerprint, "index": street_to_nodes}, f)

    return street_to_nodes


# The street index, the alias index, the validator's coverage check and the
# corrector all key on street names; they used to do it four slightly
# different ways, so a name could be present in one and missing from another.
# aliases.normalise is the single canonical form.
_normalise = _canonical_normalise


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
    cleaned = load_street_spelling_fixes().get(cleaned, cleaned).upper()

    # Full-name match first: junction definitions ("VAUXHALL CROSS", "BANK
    # JUNCTION") and genuine full street names are keyed by their complete
    # normalised form, and stripping the junction suffix below would reduce
    # them to a different street entirely ("VAUXHALL", "BANK").
    full = _normalise(cleaned)
    if full in street_to_nodes:
        return full

    for suffix in _JUNCTION_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            break

    base = _normalise(cleaned)
    if base in street_to_nodes:
        return base

    # Expand abbreviations (in-string)
    expanded = base
    for abbr, full_form in _ABBREVIATIONS.items():
        if abbr in expanded:
            expanded = expanded.replace(abbr, full_form)
    if expanded != base and expanded in street_to_nodes:
        return expanded

    # Expand abbreviations (suffix-only)
    for abbr, full_form in _ABBREVIATIONS.items():
        if base.endswith(abbr):
            candidate = base[: -len(abbr)] + full_form
            if candidate in street_to_nodes:
                return candidate

    # Guarded fuzzy last-chance: a single close spelling for a reasonably
    # long name (edit distance <= 2) is almost always the Blue Book typo we
    # haven't curated yet. Runs *before* progressive word removal because
    # word removal can only make the match less specific.
    fuzzy = _fuzzy_street_match(full, street_to_nodes)
    if fuzzy is not None:
        return fuzzy

    # Progressive word removal
    words = cleaned.split()
    while words:
        norm = _normalise(" ".join(words))
        if norm in street_to_nodes:
            return norm
        words.pop()

    return base


# Fuzzy matches are cached per name: difflib over ~36k index keys is too slow
# to repeat for every street pair of every run.
_fuzzy_cache = {}


def _fuzzy_street_match(norm_name, street_to_nodes):
    """Return the index key within edit distance 2 of *norm_name*, but only
    when the name is >= 8 chars and exactly one candidate qualifies."""
    if len(norm_name) < 8:
        return None
    if norm_name in _fuzzy_cache:
        return _fuzzy_cache[norm_name]

    import difflib

    close = difflib.get_close_matches(norm_name, street_to_nodes.keys(), n=3, cutoff=0.85)

    def edit_distance(a, b):
        if abs(len(a) - len(b)) > 2:
            return 3
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            curr = [i]
            for j, cb in enumerate(b, 1):
                curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb)))
            if min(curr) > 2:
                return 3
            prev = curr
        return prev[-1]

    candidates = [c for c in close if edit_distance(norm_name, c) <= 2]
    result = candidates[0] if len(candidates) == 1 else None
    _fuzzy_cache[norm_name] = result
    return result


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

def process_runs(output_file, limit=None, export_geojson=False, network_type=None,
                 select_ids=None, cache_dir=None):
    """Process Blue Book runs into ``output_file``.

    ``select_ids`` (an iterable of run ids) restricts processing to those runs;
    when given, the completeness gate is skipped because the output is an
    intentional subset. Explicitly selected runs are *regenerated* even if they
    already exist in ``output_file`` — asking for a specific run is a request
    to redo it, and ``--fresh`` is deliberately unavailable for subsets.
    ``limit`` still caps the *first N* runs for quick demos.

    ``network_type`` of ``None`` defers to ``load_graph``, which honours
    ``KRG_GRAPH_NETWORK_TYPE``.
    """
    select_ids = set(select_ids) if select_ids is not None else None
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

    # Carry forward the QA records of runs we're not reprocessing. Without
    # this, a resume (or a single-run regeneration) rewrites qa_report.json
    # with only the runs touched *this* invocation, silently discarding the
    # QA record for every run already in runPoints.json.
    qa_path = output_file.parent / "qa_report.json"
    if qa_path.exists():
        try:
            existing_qa = json.loads(qa_path.read_text())
            if isinstance(existing_qa, dict):
                carry, stale_ids = _partition_resumable_qa(existing_qa)
                if select_ids is not None:
                    # A subset invocation only visits the selected ids, so
                    # invalidating anything else would drop those runs from
                    # runPoints.json without ever regenerating them. Selected
                    # ids are force-regenerated below regardless; keep the
                    # rest as-is and let the next full resume clean them up.
                    for rid in stale_ids - select_ids:
                        entry = existing_qa.get(str(rid))
                        if isinstance(entry, dict):
                            carry[str(rid)] = entry
                    stale_ids &= select_ids
                qa_results.update(carry)
                print(f"Loaded {len(carry)} existing QA records from {qa_path}")
                dropped = processed_ids & stale_ids
                if dropped:
                    # Stale-shaped records (no "status", or an older schema
                    # version) can't be trusted; re-route their runs rather
                    # than resuming past them with a stale verdict.
                    runs_data = [r for r in runs_data if r["id"] not in dropped]
                    processed_ids -= dropped
                    preview = sorted(dropped)[:10]
                    print(f"Invalidated {len(dropped)} stale QA record(s); "
                          f"re-routing {preview}{'...' if len(dropped) > 10 else ''}")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: could not read existing QA report: {exc}")

    # Drop the selected runs from the resume set so they are rebuilt rather
    # than skipped as "already processed".
    if select_ids:
        stale = processed_ids & select_ids
        if stale:
            runs_data = [r for r in runs_data if r["id"] not in select_ids]
            processed_ids -= select_ids
            print(f"Regenerating {len(stale)} already-present run(s): {sorted(stale)}")

    # Parse Blue Book directions (using local files in demo directory)
    inter_file = DEMO_DIR / "blue_book_runs_intermediary.txt"
    if not inter_file.exists():
        print(f"Error: {inter_file} not found.")
        return

    run_titles, intermediary_runs = parse_intermediary_file(inter_file)
    run_ids_sorted = sorted(run_titles.keys())

    # Load graph
    print("Loading graph...")
    G = load_graph(network_type=network_type)

    # Street index. cache_dir is a parameter so tests (and parallel builds)
    # can keep their derived indexes out of the shared location.
    cache_dir = Path(cache_dir) if cache_dir else krg_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
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

    # Alias index + gazetteer.  The alias index lets the gazetteer's semantic
    # snap recognise A-road numbers and old names, and the gazetteer owns
    # snap logic that avoids motorway/trunk segments.
    print("Building street alias index...")
    alias_index = load_or_build_alias_index(G, cache_dir / "alias_index.pkl")
    print(f"  {len(alias_index.canonical_to_nodes)} canonical streets, "
          f"{len(alias_index.alias_to_canonical)} aliases.")

    # Junction/gyratory names (VAUXHALL CROSS, BANK JUNCTION, ...) resolved to
    # the nodes where their constituent streets meet. Merged into the street
    # index (in memory only — the on-disk cache stays pure graph data) so
    # get_best_street_match hits them before its word-removal fallback.
    junction_index = build_junction_index(alias_index, G=G)
    for junction_name, junction_nodes in junction_index.items():
        street_to_nodes.setdefault(junction_name, set()).update(junction_nodes)
    known_junctions = set(junction_index)
    print(f"  {len(junction_index)} junction definitions resolved to graph nodes.")

    # Optional OSM POI harvest (Quick Win 8): if the cache exists we fold it
    # into the gazetteer as a second-chance lookup behind the curated
    # overrides. Populate it with `krg osm-pois`; we never auto-fetch here.
    osm_pois = load_cached_pois(
        os.environ.get("KRG_OSM_POIS"),
        output_file.parent / "osm_pois.json",
        PROJECT_ROOT / "constants" / "osm_pois.json",
        cache_dir / "osm_pois.json",
    )
    if not osm_pois and not os.environ.get("KRG_ALLOW_NO_OSM"):
        raise RuntimeError(
            "No OSM POI harvest found — the tier-3 gazetteer would be empty and "
            "station/hospital endpoints would silently fall through to Nominatim. "
            "Run `krg osm-pois` first (or set KRG_ALLOW_NO_OSM=1 to proceed anyway)."
        )

    # Geocoded Knowledge Points List (from `krg generate pois`). Most Blue Book
    # run endpoints are Points List entries, so this is what keeps the pipeline
    # off Nominatim — without it every unmatched endpoint costs a rate-limited
    # network round trip and fails preflight.
    knowledge_pois = {}
    for candidate in (
        os.environ.get("KRG_KNOWLEDGE_POIS"),
        output_file.parent / "knowledge_pois.json",
        DEFAULT_KNOWLEDGE_POIS_PATH,
    ):
        if not candidate:
            continue
        knowledge_pois = load_knowledge_pois(candidate)
        if knowledge_pois:
            print(f"Loaded {len(knowledge_pois)} geocoded Knowledge Points from {candidate}.")
            break
    if not knowledge_pois:
        print("  No knowledge_pois.json found — endpoints will fall back to the "
              "geocoder. Run `krg generate pois` first for a faster, offline resolve.")

    gazetteer = Gazetteer(
        overrides=poi_overrides,
        alias_index=alias_index,
        osm_pois=osm_pois,
        knowledge_pois=knowledge_pois,
    )

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

        if select_ids is not None and run_id not in select_ids:
            continue

        if run_id in processed_ids:
            continue

        origin, destination = run_titles[run_id]
        print(f"\n{'='*60}")
        print(f"Processing Run {run_id}: {origin} -> {destination}")

        # ----- Geocode with snapping -----
        start = geocode_and_snap(origin, G, poi_overrides, gazetteer=gazetteer)
        end = geocode_and_snap(destination, G, poi_overrides, gazetteer=gazetteer)

        if not start or not end:
            print(f"  SKIP: Failed to geocode Run {run_id}")
            qa_results[str(run_id)] = {
                "status": "failed",
                "failure_reason": "geocode failed for start or end",
                "passed": False,
                "preflight_ok": False,
                "preflight_reasons": ["geocode failed for start or end"],
            }
            continue

        start_lat, start_lon, start_node = start
        end_lat, end_lon, end_node = end

        # ----- Preflight: surface snap/resolve problems *before* we route -----
        start_entry = gazetteer.resolve(origin, G)
        end_entry = gazetteer.resolve(destination, G)
        intermediate_streets_raw = list(intermediary_runs.get(run_id, []))
        pre = preflight_run(
            start_entry, end_entry,
            intermediate_streets_raw, alias_index,
            known_junctions=known_junctions,
        )
        if pre.warnings:
            for w in pre.warnings:
                print(f"  [preflight-warn] {w}")
        if not pre.ok:
            for r in pre.reasons:
                print(f"  [preflight-fail] {r}")
            qa_results[str(run_id)] = {
                "status": "failed",
                "failure_reason": "preflight failed",
                "passed": False,
                "preflight_ok": False,
                "preflight_reasons": pre.reasons,
                "start_snap_m": pre.start_snap_m,
                "end_snap_m": pre.end_snap_m,
                "unresolved_streets": pre.unresolved_streets,
            }
            # Keep going but flag the route; users can triage from qa_report.json.

        # Per-run config from patches.
        # Directness is now *measured* (not skipped) so failing routes surface
        # in the QA report.  Individual runs with unusual legitimate geometry
        # can still widen the threshold via ``max_deviation_ratio`` in
        # ``run_specific_fixes.json``.
        run_patch = patches.get(str(run_id), {})
        run_config = {"skip_directness_check": False}

        if "max_deviation_ratio" in run_patch:
            run_config["max_deviation_ratio"] = run_patch["max_deviation_ratio"]
        if "max_lateral_offset_m" in run_patch:
            run_config["max_lateral_offset_m"] = run_patch["max_lateral_offset_m"]
        if run_patch.get("skip_directness_check"):
            run_config["skip_directness_check"] = True

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

            # Exempt-turns patch: lets a run explicitly whitelist a (from, via, to)
            # triple that OSM marks as prohibited but is legal for taxis/PSVs.
            # Spec in ``run_specific_fixes.json``:
            #   "exempt_turns": [[[lat,lon], [lat,lon], [lat,lon]], ...]
            exempted_turns = set()
            for triple in run_patch.get("exempt_turns", []) or []:
                try:
                    f_lat, f_lon = triple[0]
                    v_lat, v_lon = triple[1]
                    t_lat, t_lon = triple[2]
                    import osmnx as ox
                    f = ox.distance.nearest_nodes(G, f_lon, f_lat)
                    v = ox.distance.nearest_nodes(G, v_lon, v_lat)
                    t = ox.distance.nearest_nodes(G, t_lon, t_lat)
                    exempted_turns.add((f, v, t))
                except Exception as exc:
                    print(f"  [Patch] Could not resolve exempt_turns triple: {exc}")
            # The exemption has to reach the *router* too: validating a turn as
            # excused is pointless if the Dijkstra expansion still refuses to
            # traverse it, so route against the restriction set minus the
            # exempted triples.
            run_prohibited_turns = prohibited_turns
            if exempted_turns:
                print(f"  [Patch] {len(exempted_turns)} turns exempted")
                run_prohibited_turns = set(prohibited_turns) - exempted_turns

            def _route_fn(G, o, d, wps):
                return get_constrained_route(G, o, d, wps, prohibited_turns=run_prohibited_turns, intermediate_streets=intermediate_streets)

            def _validate_fn(G, nodes, o, d, turns, streets, cfg, wps, exempted_turns=None):
                return validate_route(
                    G, nodes, o, d, turns, streets, cfg,
                    waypoint_nodes=wps, exempted_turns=exempted_turns,
                )

            route_nodes, validation, corrections = correct_and_validate(
                G, start_node, end_node, waypoint_nodes,
                intermediate_streets, run_prohibited_turns, street_to_nodes,
                route_fn=_route_fn,
                validate_fn=_validate_fn,
                config=run_config,
                exempted_turns=exempted_turns or None,
            )

            if not route_nodes or len(route_nodes) < 2:
                print(f"  ERROR: No route produced for Run {run_id}")
                qa_results[str(run_id)] = {
                    "status": "failed",
                    "failure_reason": "no route produced",
                    "passed": False,
                    "preflight_ok": pre.ok,
                }
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
                return get_constrained_route(G, o, d, wps, prohibited_turns=run_prohibited_turns, intermediate_streets=reverse_streets)

            rev_route_nodes, rev_validation, rev_corrections = correct_and_validate(
                G, end_node, start_node, reverse_waypoints,
                reverse_streets, run_prohibited_turns, street_to_nodes,
                route_fn=_route_fn_rev,
                validate_fn=_validate_fn,
                config=run_config,
                exempted_turns=exempted_turns or None,
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
                    # Node sequence — used by `krg diagnose` to walk the route
                    # edge-by-edge against the live graph. Tiny size impact
                    # (one int list per run), and the webapp ignores it.
                    "nodes": [int(n) for n in route_nodes],
                },
                "routeReverse": {
                    "geometry": {
                        "type": "LineString",
                        "coordinates": rev_coords,
                    },
                    "duration": rev_duration,
                    "distance": rev_distance,
                    "steps": reverse_steps,
                    "nodes": [int(n) for n in rev_route_nodes] if rev_route_nodes else [],
                },
            }

            runs_data.append(run_obj)
            processed_ids.add(run_id)

            # QA record — expanded so failures can be triaged without re-running
            shape_problems = check_run_shape(run_obj)
            if shape_problems:
                print(f"  [shape] {len(shape_problems)} structural problem(s): "
                      f"{shape_problems[:3]}")

            qa_results[str(run_id)] = {
                "status": "ok" if not shape_problems else "failed",
                "shape_problems": shape_problems,
                "passed": validation.passed,
                "ratio": metrics.get("ratio"),
                "max_offset_m": metrics.get("max_lateral_offset_m"),
                "legal": validation.is_legal,
                "is_direct": validation.is_direct,
                "street_coverage": validation.coverage_metrics.get("coverage"),
                "corrections": len(corrections),
                # Legs the router abandoned. Non-zero means the geometry has a
                # gap even though a route was produced.
                "unreachable_legs": fwd_meta.get("unreachable_legs", 0),
                "truncated_legs": fwd_meta.get("truncated_legs", 0),
                "fwd_distance_m": fwd_distance,
                "rev_distance_m": rev_distance,
                # Preflight signal
                "preflight_ok": pre.ok,
                "preflight_warnings": pre.warnings,
                "preflight_reasons": pre.reasons,
                "start_snap_m": pre.start_snap_m,
                "end_snap_m": pre.end_snap_m,
                "unresolved_streets": pre.unresolved_streets,
                # Patch applied?
                "patched": bool(run_patch),
                "exempted_turn_count": len(exempted_turns) if exempted_turns else 0,
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
                _save_runs(output_file, runs_data)

        except Exception as e:
            print(f"  ERROR processing Run {run_id}: {e}")
            import traceback
            traceback.print_exc()
            qa_results[str(run_id)] = {
                "status": "failed",
                "failure_reason": f"{type(e).__name__}: {e}",
                "passed": False,
                "preflight_ok": pre.ok,
            }
            continue


    # ------------------------------------------------------------------
    # Final save
    # ------------------------------------------------------------------
    _save_runs(output_file, runs_data)
    print(f"\nSaved {len(runs_data)} runs to {output_file}")

    # Provenance: which graph produced this, so a change in the routes can be
    # attributed rather than guessed at.
    qa_results["_provenance"] = {
        "qa_schema_version": QA_SCHEMA_VERSION,
        "generated_at": int(time.time()),
        "network_type": network_type or os.environ.get("KRG_GRAPH_NETWORK_TYPE", "drive"),
        "graph_nodes": G.number_of_nodes(),
        "graph_edges": G.number_of_edges(),
        "street_names_indexed": len(street_to_nodes),
        "prohibited_turns": len(prohibited_turns),
        "knowledge_pois": len(knowledge_pois),
        "osm_pois": len(osm_pois or {}),
    }

    # QA report
    with open(qa_path, "w") as f:
        json.dump(qa_results, f, indent=2, default=_json_default)
    print(f"QA report saved to {qa_path}")

    # Optional GeoJSON export
    if export_geojson and geojson_features:
        geojson_path = output_file.parent / "routes.geojson"
        export_all_runs_geojson(geojson_features, geojson_path)

    # ------------------------------------------------------------------
    # Completeness gate: every Blue Book run must be present in the output.
    # A missing id means the run was skipped (geocode/route failure) and
    # silently dropped — the exact failure mode that left the app at 30/320.
    # When the full set is processed (no --limit) we surface the gap loudly
    # and write it into the QA report so it can be triaged without re-running.
    # ------------------------------------------------------------------
    expected_ids = set(run_ids_sorted)
    present_ids = {r["id"] for r in runs_data}
    missing_ids = sorted(expected_ids - present_ids)
    if limit is None and select_ids is None:
        # Any expected id with no record at all was dropped before it could
        # report anything; give it one so the report always covers all 320.
        for run_id in expected_ids:
            qa_results.setdefault(str(run_id), {
                "status": "failed",
                "failure_reason": "not processed",
                "passed": False,
            })
        unusable = sorted(
            int(k) for k, v in qa_results.items()
            if str(k).lstrip("-").isdigit() and v.get("status") == "failed"
        )
        qa_results["_completeness"] = {
            "expected": len(expected_ids),
            "present": len(present_ids),
            "missing_ids": missing_ids,
            "unusable_ids": unusable,
        }
        with open(qa_path, "w") as f:
            json.dump(qa_results, f, indent=2, default=_json_default)
        print(f"\n{'='*60}")
        if missing_ids:
            print(f"COMPLETENESS: {len(present_ids)}/{len(expected_ids)} runs present. "
                  f"MISSING {len(missing_ids)}: {missing_ids}")
        else:
            print(f"COMPLETENESS: all {len(expected_ids)} runs present. ✓")
        if unusable:
            print(f"USABILITY: {len(unusable)} run(s) present but flagged unusable: "
                  f"{unusable[:20]}{'...' if len(unusable) > 20 else ''}")

    # Summary with categorised failure reasons. Records are keyed by
    # stringified run id; exclude the non-run "_completeness" sentinel so the
    # pass/fail tally stays per-run.
    run_qa = {k: v for k, v in qa_results.items() if str(k).lstrip("-").isdigit()}
    total = len(run_qa)
    passed = sum(1 for v in run_qa.values() if v.get("passed"))
    failed = total - passed
    print(f"\n{'='*60}")
    print(f"SUMMARY: {passed}/{total} runs passed validation, {failed} failed.")
    if failed:
        fail_ids = [k for k, v in run_qa.items() if not v.get("passed")]
        print(f"  Failed runs: {fail_ids[:20]}{'...' if len(fail_ids) > 20 else ''}")

        # Triage buckets
        preflight_fails = sum(1 for v in run_qa.values() if not v.get("preflight_ok", True))
        directness_fails = sum(
            1 for v in run_qa.values()
            if v.get("preflight_ok", True) and v.get("is_direct") is False
        )
        legality_fails = sum(
            1 for v in run_qa.values()
            if v.get("preflight_ok", True) and v.get("legal") is False
        )
        print(f"  Triage: preflight={preflight_fails}  directness={directness_fails}  "
              f"legality={legality_fails}")


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
    parser.add_argument(
        "--network-type",
        choices=["drive", "drive_service"],
        help="OSM graph profile. Defaults to env KRG_GRAPH_NETWORK_TYPE or 'drive'.",
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

    network_type = os.environ.get("KRG_GRAPH_NETWORK_TYPE", "drive")
    if getattr(args, "network_type", None):
        network_type = args.network_type

    process_runs(
        output_file,
        limit=args.limit,
        export_geojson=export_geojson,
        network_type=network_type,
    )
