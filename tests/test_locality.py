"""
Constraint localisation (``locality.localise_constraints``).

The failure this guards: a STREET constraint matched by *name* is satisfied
by any namesake in London. When the local instance could not be driven — not
in OSM under that name, one-way the wrong way, a wrong resolution — the
ordered search drove to a namesake kilometres away instead of failing, and
the ladder never demoted it because the constraint was met. Shipped examples:
run 150 via a Station Yard in Twickenham (35.9 km for a 2.7 km run), run 47
through the Blackwall Tunnel to Lea Bridge Road, run 215 to Artillery Lane in
Spitalfields, and every reverse run whose local one-way sent it to a
same-named street elsewhere (run 5 BA, run 46 BA's Station Road).
"""

import unittest

import networkx as nx

from knowledge_run_generator.aliases import normalise
from knowledge_run_generator.constraints import Constraint
from knowledge_run_generator.locality import (
    REMOTE_EXCESS_M,
    localise_constraints,
    street_instances,
)
from knowledge_run_generator.router import (
    constraint_waypoints,
    route_ordered_with_ladder,
)
from knowledge_run_generator.validator import check_constraint_order

# ~0.0144 deg of longitude is 1 km at London's latitude.
KM_LON = 0.0144
KM_LAT = 0.009
LAT = 51.5


def street(name, hard=True, source="exact"):
    return Constraint("STREET", normalise(name), name, source, hard)


def _net(local_drivable=False):
    """A local run with a far-off namesake.

    Main Street runs 1-2-3-4-5 west to east (400 m, bidirectional). Oak Road
    is a spur 2-6-3 north of it. By default it can't be driven: its two
    one-way halves both lead *out* of node 6, whose entry isn't in the drive
    graph (the shape of a one-way pair or restricted street half-mapped in
    OSM). ``local_drivable`` makes it an ordinary two-way loop. A second
    "Oak Road", bidirectional, sits 20 km north (nodes 20-21), reached by a
    fast road 5-10-20 / 21-11-5.
    """
    G = nx.MultiDiGraph()
    G.graph["crs"] = "epsg:4326"
    step = 0.1 * KM_LON
    pos = {
        1: (0, 0), 2: (step, 0), 3: (2 * step, 0), 4: (3 * step, 0), 5: (4 * step, 0),
        6: (1.5 * step, 0.0005),
        10: (4 * step, 5 * KM_LAT), 11: (4 * step + step, 5 * KM_LAT),
        20: (0, 20 * KM_LAT), 21: (step, 20 * KM_LAT),
    }
    for n, (x, y) in pos.items():
        G.add_node(n, x=-0.1 + x, y=LAT + y)

    def road(u, v, name, length, oneway=False):
        G.add_edge(u, v, length=length, name=name, highway="residential")
        if not oneway:
            G.add_edge(v, u, length=length, name=name, highway="residential")

    for a, b in ((1, 2), (2, 3), (3, 4), (4, 5)):
        road(a, b, "Main Street", 100.0)
    if local_drivable:
        road(2, 6, "Oak Road", 80.0)
        road(6, 3, "Oak Road", 80.0)
    else:
        road(6, 3, "Oak Road", 80.0, oneway=True)
        road(6, 2, "Oak Road", 80.0, oneway=True)
    road(5, 10, "Express Way", 5000.0)
    road(10, 20, "Express Way", 15000.0)
    road(20, 21, "Oak Road", 100.0)
    road(21, 11, "Express Way", 15000.0)
    road(11, 5, "Express Way", 5000.0)
    return G


def _index(G):
    index = {}
    for u, v, data in G.edges(data=True):
        index.setdefault(normalise(data["name"]), set()).update((u, v))
    return index


class InstanceTests(unittest.TestCase):
    def test_namesakes_far_apart_are_separate_instances(self):
        G = _net()
        inst = street_instances(G, "OAK ROAD")
        self.assertEqual(len(inst), 2)
        self.assertIn({2, 3, 6}, inst)
        self.assertIn({20, 21}, inst)

    def test_pieces_of_one_street_close_together_are_one_instance(self):
        # A street interrupted by a square is still one street.
        G = nx.MultiDiGraph()
        for n, x in ((1, 0.0), (2, 0.001), (3, 0.002), (4, 0.003)):
            G.add_node(n, x=x, y=LAT)
        G.add_edge(1, 2, name="Elm Row", length=70.0)
        G.add_edge(2, 3, name="The Square", length=70.0)
        G.add_edge(3, 4, name="Elm Row", length=70.0)
        self.assertEqual(street_instances(G, "ELM ROW"), [{1, 2, 3, 4}])


class LocaliseTests(unittest.TestCase):
    def setUp(self):
        self.G = _net()

    def test_constraint_is_pinned_to_the_local_instance(self):
        loc = localise_constraints(self.G, [street("Oak Road")], 1, 5)
        self.assertEqual(loc.remote, [])
        self.assertEqual(loc.constraints[0].nodes, frozenset({2, 3, 6}))

    def test_the_instance_on_the_run_wins_even_when_it_is_not_nearest_the_start(self):
        # Chain O -> Oak Road -> Main Street -> D. With the start at the far
        # namesake's end of town, the local instance still wins because the
        # *chain* through it is shorter.
        loc = localise_constraints(
            self.G, [street("Main Street"), street("Oak Road")], 1, 5)
        self.assertEqual(loc.constraints[1].nodes, frozenset({2, 3, 6}))

    def test_name_with_no_instance_near_the_run_is_remote(self):
        G = self.G
        G.add_edge(20, 21, length=100.0, name="Pine Walk", highway="residential")
        loc = localise_constraints(
            G, [street("Main Street"), street("Pine Walk")], 1, 5)
        self.assertEqual([c.raw for c in loc.constraints], ["Main Street"])
        self.assertEqual(len(loc.remote), 1)
        self.assertEqual(loc.remote[0]["raw"], "Pine Walk")
        self.assertGreater(loc.remote[0]["excess_m"], REMOTE_EXCESS_M)

    def test_node_constraints_pass_through(self):
        junction = Constraint("NODE", frozenset({3}), "OAK CORNER", "junction", True)
        loc = localise_constraints(self.G, [junction], 1, 5)
        self.assertEqual(loc.constraints, [junction])


class LocalisedRoutingTests(unittest.TestCase):
    """The end-to-end property: an undrivable local street is demoted, never
    satisfied by a namesake across town."""

    def setUp(self):
        self.G = _net()
        self.index = _index(self.G)

    def test_unlocalised_constraint_drives_to_the_namesake(self):
        # Documents the bug: the local Oak Road can't be driven, so name
        # matching sends the route 40 km round the far Oak Road.
        route, meta = route_ordered_with_ladder(
            self.G, 1, 5, [street("Oak Road")], street_to_nodes=self.index)
        self.assertIn(20, route)
        self.assertGreater(meta["total_distance"], 40_000)

    def test_localised_constraint_is_demoted_instead(self):
        loc = localise_constraints(self.G, [street("Oak Road")], 1, 5)
        route, meta = route_ordered_with_ladder(
            self.G, 1, 5, loc.constraints, street_to_nodes=self.index)
        self.assertNotIn(20, route)
        self.assertEqual(route, [1, 2, 3, 4, 5])
        self.assertEqual(meta["demoted_constraints"], [("Oak Road", "exact")])

    def test_localised_constraint_is_still_driven_when_it_can_be(self):
        G = _net(local_drivable=True)
        loc = localise_constraints(G, [street("Oak Road")], 1, 5)
        route, meta = route_ordered_with_ladder(
            G, 1, 5, loc.constraints, street_to_nodes=_index(G))
        self.assertEqual(meta["routing_mode"], "ordered_strict")
        self.assertIn(6, route)
        self.assertNotIn(20, route)

    def test_metric_and_waypoints_ignore_a_namesake(self):
        loc = localise_constraints(self.G, [street("Oak Road")], 1, 5)
        far_route = [1, 2, 3, 4, 5, 10, 20, 21, 11, 5]
        ok, m = check_constraint_order(self.G, far_route, loc.constraints)
        self.assertFalse(ok, "driving the far Oak Road is not driving the run")
        self.assertEqual(m["missing"], ["Oak Road"])
        self.assertEqual(constraint_waypoints(self.G, far_route, loc.constraints), [])


if __name__ == "__main__":
    unittest.main()
