"""Regenerate ``run1_graph.graphml``, the pipeline test's stand-in for London.

The real graph is a ~2 GB Overpass download of Greater London, so CI can't use
it and neither can a laptop running the test suite. This builds a tiny graph
that is nonetheless *shaped* like the real one — osmnx MultiDiGraph, `x`/`y`
node coordinates, `length`/`name`/`highway`/`osmid` edge attributes, both
directions — carrying the real street names of Blue Book Run 1 in their real
order, so ``process_runs`` can route Run 1 end to end against it.

Geometry is synthetic: the streets are laid out as a gentle north-east
diagonal, not their true positions. The test asserts pipeline mechanics
(routing, validation, call generation, QA records), not cartography.

    python tests/fixtures/build_fixture_graph.py
"""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import osmnx as ox

FIXTURE = Path(__file__).resolve().parent / "run1_graph.graphml"

# Run 1: Manor House Station N4 -> Gibson Square N1, plus the destination
# street itself so the gazetteer's street tier has something to resolve.
STREETS = [
    "Green Lanes",
    "Brownswood Road",
    "Blackstock Road",
    "Highbury Park",
    "Highbury Grove",
    "St Paul's Road",
    "Upper Street",
    "Barnsbury Street",
    "Milner Square",
    "Milner Place",
    "Gibson Square",
]

START_LAT, START_LON = 51.5713, -0.0944   # Manor House Station, roughly
SEGMENTS_PER_STREET = 3
SEGMENT_LENGTH_M = 150.0


def build() -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    G.graph["crs"] = "epsg:4326"
    G.graph["simplified"] = True

    lat, lon = START_LAT, START_LON
    node_id = 0
    G.add_node(node_id, x=lon, y=lat)
    previous = node_id

    for street_index, name in enumerate(STREETS):
        for _ in range(SEGMENTS_PER_STREET):
            node_id += 1
            lon += 0.002
            lat += 0.0005
            G.add_node(node_id, x=lon, y=lat)
            for u, v in ((previous, node_id), (node_id, previous)):
                G.add_edge(
                    u, v, 0,
                    length=SEGMENT_LENGTH_M,
                    name=name,
                    highway="residential",
                    osmid=1000 + street_index,
                    oneway=False,
                    reversed=False,
                )
            previous = node_id

    return G


if __name__ == "__main__":
    graph = build()
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(graph, FIXTURE)
    print(f"{graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges "
          f"-> {FIXTURE} ({FIXTURE.stat().st_size / 1024:.0f} KiB)")
