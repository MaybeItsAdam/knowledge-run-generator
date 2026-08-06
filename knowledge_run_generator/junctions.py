"""
Junction and gyratory name resolution.

The Blue Book calls out compass points ("COM ... ") at named junctions the
OSM graph has no single name for: VAUXHALL CROSS, HYDE PARK CORNER, BANK
JUNCTION, cab-trade shorthand like KING CHARLES ISLAND (the Trafalgar Square
roundabout with the Charles I statue) or BRIDGEND CIRCUS (the Wandsworth
Bridge south gyratory). None of these resolve through the alias index, so
every occurrence used to surface as an "unresolved street" and the waypoint
builder fell back to progressive word removal — which happily turns "VAUXHALL
CROSS" into whatever street "VAUXHALL" matches first.

``junction_definitions.json`` curates each name as the list of streets that
meet there. The junction's node set is the union of the pairwise
intersections of those streets' node sets — the same mechanism
``find_intersection_node`` uses for consecutive street pairs. The scheme is
fail-soft by construction: a misspelled or missing street contributes no
pairs, an entirely wrong definition produces an empty node set, and the
caller falls through to the previous behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

from .aliases import AliasIndex, normalise

DEFAULT_DEFINITIONS_PATH = (
    Path(__file__).resolve().parent / "blue_book_demo" / "junction_definitions.json"
)


def load_junction_definitions(path: str | Path | None = None) -> dict:
    """Load ``{"JUNCTION NAME": {"streets": [...]}}`` (empty dict on any error)."""
    path = Path(path) if path else DEFAULT_DEFINITIONS_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 — a broken file must not kill a build
        print(f"Warning: could not load junction definitions: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def build_junction_index(
    alias_index: AliasIndex,
    definitions: dict | None = None,
    G=None,
    fuzzy_radius_m: float = 150.0,
) -> dict:
    """Resolve junction definitions into ``{normalised name: node set}``.

    Node sets come from pairwise intersections of the constituent streets'
    node sets, so a junction maps to the exact graph nodes where its streets
    actually meet. Some gyratories (Wandsworth Bridge's BRIDGEND CIRCUS, BOW
    INTERCHANGE) have an *unnamed* ring road, so their streets touch the ring
    but never each other; when a graph is supplied, street pairs with no
    shared node fall back to their closest node pair within
    ``fuzzy_radius_m`` — the same tolerance idea as
    ``find_intersection_node``'s fuzzy mode. Junctions whose streets never
    come near each other (bad definition, street missing from this graph)
    are silently omitted.
    """
    if definitions is None:
        definitions = load_junction_definitions()

    index: dict = {}
    for name, spec in definitions.items():
        streets = spec.get("streets") if isinstance(spec, dict) else spec
        if not streets:
            continue
        node_sets = [alias_index.nodes_for(s) for s in streets]
        node_sets = [ns for ns in node_sets if ns]
        nodes: set = set()
        for i in range(len(node_sets)):
            for j in range(i + 1, len(node_sets)):
                nodes |= node_sets[i] & node_sets[j]
        if not nodes and G is not None:
            nodes = _fuzzy_meeting_nodes(G, node_sets, fuzzy_radius_m)
        if nodes:
            index[normalise(name)] = nodes
    return index


def _fuzzy_meeting_nodes(G, node_sets: list, radius_m: float) -> set:
    """Closest node pairs between streets that approach without touching."""
    # ~1 deg latitude = 111 km; a degrees-space threshold is fine at this
    # radius and keeps the pair scan cheap.
    threshold_deg = radius_m / 111_000.0
    threshold_sq = threshold_deg * threshold_deg

    nodes: set = set()
    for i in range(len(node_sets)):
        for j in range(i + 1, len(node_sets)):
            best_pair = None
            best_d = threshold_sq
            for u in node_sets[i]:
                if u not in G.nodes:
                    continue
                pu = G.nodes[u]
                for v in node_sets[j]:
                    if v not in G.nodes:
                        continue
                    pv = G.nodes[v]
                    d = (pu["x"] - pv["x"]) ** 2 + (pu["y"] - pv["y"]) ** 2
                    if d < best_d:
                        best_d = d
                        best_pair = (u, v)
            if best_pair:
                nodes.update(best_pair)
    return nodes
