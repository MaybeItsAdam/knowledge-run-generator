"""
Ordered-constraint A* tests (ROADMAP Stage 3).

The property under test is the one the legacy router lacked: **ordering is
the goal test**. A state only terminates the search at the destination with
every constraint satisfied, so a route that skips a street is unrepresentable
— it fails and descends the ladder instead, explicitly.
"""

import unittest

import networkx as nx

from knowledge_run_generator.aliases import normalise
from knowledge_run_generator.constraints import Constraint
from knowledge_run_generator.router import (
    constraint_waypoints,
    edge_name_set,
    get_ordered_route,
    route_ordered_with_ladder,
)


def _grid():
    """Bidirectional test net.

    Main Street runs 1-2-3-4-5 west to east. Alpha Road loops 2-6-3 to the
    north; Beta Road loops 3-7-4 to the south. Every edge is bidirectional,
    so a route can double back — at a cost.
    """
    G = nx.MultiDiGraph()
    G.graph["crs"] = "epsg:4326"
    coords = {
        1: (0.000, 0.0), 2: (0.001, 0.0), 3: (0.002, 0.0),
        4: (0.003, 0.0), 5: (0.004, 0.0),
        6: (0.0015, 0.001),   # Alpha Road apex
        7: (0.0025, -0.001),  # Beta Road apex
    }
    for n, (x, y) in coords.items():
        G.add_node(n, x=x, y=y)

    def road(u, v, name):
        for a, b in ((u, v), (v, u)):
            G.add_edge(a, b, length=100.0, name=name, highway="residential")

    road(1, 2, "Main Street")
    road(2, 3, "Main Street")
    road(3, 4, "Main Street")
    road(4, 5, "Main Street")
    road(2, 6, "Alpha Road")
    road(6, 3, "Alpha Road")
    road(3, 7, "Beta Road")
    road(7, 4, "Beta Road")
    return G


def street(name, hard=True, source="exact"):
    return Constraint("STREET", normalise(name), name, source, hard)


def _street_index(G):
    index = {}
    for u, v, data in G.edges(data=True):
        norm = normalise(data.get("name", ""))
        index.setdefault(norm, set()).update((u, v))
    return index


class OrderedSearchTests(unittest.TestCase):
    def setUp(self):
        self.G = _grid()
        self.index = _street_index(self.G)

    def route(self, constraints, origin=1, dest=5, **kw):
        return get_ordered_route(
            self.G, origin, dest, constraints,
            street_to_nodes=self.index, **kw,
        )

    def _first_traversal(self, route, name):
        norm = normalise(name)
        for i in range(len(route) - 1):
            if norm in edge_name_set(self.G, route[i], route[i + 1]):
                return i
        return None

    def test_unconstrained_route_is_the_shortest_path(self):
        route, info = self.route([])
        self.assertEqual(route, [1, 2, 3, 4, 5])
        self.assertTrue(info["reached_goal"])

    def test_single_constraint_forces_the_detour(self):
        route, info = self.route([street("Alpha Road")])
        self.assertTrue(info["reached_goal"])
        self.assertIn(6, route, "route must actually traverse Alpha Road")

    def test_order_is_enforced_even_against_geography(self):
        # Beta Road lies beyond Alpha Road; demanding Beta *first* forces the
        # route to overshoot and come back. The legacy router would have
        # quietly satisfied them in the wrong order.
        route, info = self.route([street("Beta Road"), street("Alpha Road")])
        self.assertTrue(info["reached_goal"])
        beta_at = self._first_traversal(route, "Beta Road")
        alpha_at = self._first_traversal(route, "Alpha Road")
        self.assertIsNotNone(beta_at)
        self.assertIsNotNone(alpha_at)
        self.assertLess(beta_at, alpha_at,
                        "Beta Road must be traversed before Alpha Road")

    def test_unsatisfiable_constraint_fails_instead_of_skipping(self):
        route, info = self.route([street("Nowhere Avenue")])
        self.assertIsNone(route)
        self.assertFalse(info["reached_goal"])
        self.assertEqual(info["max_idx"], 0,
                         "the blocking constraint is named by max_idx")

    def test_prohibited_turn_never_appears_in_output(self):
        prohibited = {(2, 3, 4)}
        route, info = self.route([], prohibited_turns=prohibited)
        self.assertTrue(info["reached_goal"])
        triples = set(zip(route, route[1:], route[2:]))
        self.assertFalse(triples & prohibited)

    def test_node_constraint_is_satisfied_by_reaching_a_member(self):
        node_c = Constraint("NODE", frozenset({6}), "ALPHA APEX", "junction", True)
        route, info = self.route([node_c])
        self.assertTrue(info["reached_goal"])
        self.assertIn(6, route)


class LadderTests(unittest.TestCase):
    def setUp(self):
        self.G = _grid()
        self.index = _street_index(self.G)

    def test_strict_mode_when_everything_resolves(self):
        route, meta = route_ordered_with_ladder(
            self.G, 1, 5, [street("Alpha Road"), street("Beta Road")],
            street_to_nodes=self.index,
        )
        self.assertEqual(meta["routing_mode"], "ordered_strict")
        self.assertEqual(meta["demoted_constraints"], [])

    def test_blocking_constraint_is_demoted_and_recorded(self):
        route, meta = route_ordered_with_ladder(
            self.G, 1, 5,
            [street("Alpha Road"), street("Nowhere Avenue"), street("Beta Road")],
            street_to_nodes=self.index,
        )
        self.assertIsNotNone(route)
        self.assertEqual(meta["routing_mode"], "ordered_relaxed")
        self.assertEqual(meta["demoted_constraints"],
                         [("Nowhere Avenue", "exact")])
        # The surviving constraints are still enforced in order.
        self.assertIn(6, route)
        self.assertIn(7, route)

    def test_optimum_is_computed_on_request(self):
        route, meta = route_ordered_with_ladder(
            self.G, 1, 5, [street("Alpha Road")],
            street_to_nodes=self.index, compute_optimum=True,
        )
        self.assertIsNotNone(meta.get("ordered_optimum_m"))
        self.assertGreater(meta["ordered_optimum_m"], 0)
        self.assertLessEqual(meta["ordered_optimum_m"],
                             meta["total_distance"] + 1e-6)


class ConstraintWaypointTests(unittest.TestCase):
    def test_waypoints_are_first_satisfaction_nodes(self):
        G = _grid()
        index = _street_index(G)
        constraints = [street("Alpha Road"), street("Beta Road")]
        route, _ = route_ordered_with_ladder(
            G, 1, 5, constraints, street_to_nodes=index)
        wps = constraint_waypoints(G, route, constraints)
        self.assertEqual(len(wps), 2)
        self.assertLess(route.index(wps[0]), route.index(wps[1]) + 1)


if __name__ == "__main__":
    unittest.main()
