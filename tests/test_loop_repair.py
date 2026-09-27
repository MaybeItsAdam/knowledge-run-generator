"""Forced-loop repair and the reverse-run budget (router.py).

The ordered search's goal test is the ordering, so when the Blue Book
sequence cannot be driven in order without going round the block — text
written for a gyratory since remodelled (Archway), or a reversed run meeting
a one-way from the wrong end — it laps rather than fails. Run 238 lapped the
Archway gyratory three times; run 214's reverse drove 16.4 km for a 6.2 km
trip. The ladder now demotes the constraint that forced the loop (on trial:
it stays demoted only if the route gets shorter), and the reverse run falls
back to the shortest legal route when the reversed sequence is out of budget.
"""

import unittest

import networkx as nx

from knowledge_run_generator.aliases import normalise
from knowledge_run_generator.constraints import Constraint
from knowledge_run_generator.router import (
    find_forced_loop,
    get_ordered_route,
    route_ordered_with_ladder,
    route_reverse,
)


def street(name, hard=True):
    return Constraint("STREET", normalise(name), name, "exact", hard)


def _ring_net():
    """A one-way square ring 10->11->12->13->10 (Ash, Birch, Cedar, Dale
    Roads, 100 m each). The origin 1 feeds in at 10; the ring lets out at 12
    towards the destination 2."""
    G = nx.MultiDiGraph()
    G.graph["crs"] = "epsg:4326"
    s = 0.001
    coords = {1: (-s, 0), 10: (0, 0), 11: (s, 0), 12: (s, s), 13: (0, s), 2: (2 * s, s)}
    for n, (x, y) in coords.items():
        G.add_node(n, x=x, y=y)

    def one_way(u, v, name):
        G.add_edge(u, v, length=100.0, name=name, highway="residential")

    one_way(1, 10, "Entry Road")
    one_way(10, 11, "Ash Road")
    one_way(11, 12, "Birch Road")
    one_way(12, 13, "Cedar Road")
    one_way(13, 10, "Dale Road")
    one_way(12, 2, "Exit Road")
    return G


class ForcedLoopTests(unittest.TestCase):
    def test_out_of_order_sequence_laps_the_ring(self):
        # Cedar before Ash can only be met by going round twice.
        G = _ring_net()
        route, _ = get_ordered_route(G, 1, 2, [street("Cedar Road"), street("Ash Road")])
        self.assertEqual(route, [1, 10, 11, 12, 13, 10, 11, 12, 2])

    def test_lap_is_found_and_blamed_on_what_it_delivered(self):
        G = _ring_net()
        cons = [street("Cedar Road"), street("Ash Road")]
        route = [1, 10, 11, 12, 13, 10, 11, 12, 2]
        # Cedar is met inside the lap (10->11 ... 10->11): it is the suspect.
        self.assertEqual(find_forced_loop(G, route, cons), 0)
        self.assertEqual(find_forced_loop(G, route, cons, all_suspects=True)[0], 0)

    def test_clean_route_has_no_loop(self):
        G = _ring_net()
        self.assertIsNone(find_forced_loop(G, [1, 10, 11, 12, 2], [street("Ash Road")]))
        self.assertEqual(
            find_forced_loop(G, [1, 10, 11, 12, 2], [street("Ash Road")], all_suspects=True),
            [])

    def test_ladder_demotes_the_lap_constraint(self):
        G = _ring_net()
        route, meta = route_ordered_with_ladder(
            G, 1, 2, [street("Cedar Road"), street("Ash Road")])
        self.assertEqual(route, [1, 10, 11, 12, 2])
        self.assertEqual(meta["routing_mode"], "ordered_relaxed")
        self.assertEqual(meta["loop_demotions"], [("Cedar Road", "exact")])
        self.assertIn(("Cedar Road", "exact"), meta["demoted_constraints"])

    def test_short_lap_is_left_alone_when_a_minimum_is_set(self):
        # The Blue Book run only repairs laps of a kilometre or more.
        G = _ring_net()
        route, meta = route_ordered_with_ladder(
            G, 1, 2, [street("Cedar Road"), street("Ash Road")],
            min_lap_m=1000.0, leg_loops=False)
        self.assertEqual(route, [1, 10, 11, 12, 13, 10, 11, 12, 2])
        self.assertNotIn("loop_demotions", meta)

    def test_loop_repair_can_be_disabled(self):
        G = _ring_net()
        route, meta = route_ordered_with_ladder(
            G, 1, 2, [street("Cedar Road"), street("Ash Road")], demote_loops=False)
        self.assertEqual(route, [1, 10, 11, 12, 13, 10, 11, 12, 2])
        self.assertEqual(meta["routing_mode"], "ordered_strict")

    def test_loop_the_restrictions_force_is_not_blamed_on_a_constraint(self):
        # No right turn from Entry Road onto the exit at 10: every legal
        # route goes round the ring, with or without the constraint, so the
        # constraint did not cause the loop and stays enforced.
        G = _ring_net()
        G.add_node(3, x=0.0, y=-0.001)
        G.add_edge(10, 3, length=100.0, name="South Exit", highway="residential")
        banned = {(1, 10, 3)}
        route, meta = route_ordered_with_ladder(
            G, 1, 3, [street("Birch Road")], prohibited_turns=banned)
        self.assertEqual(route, [1, 10, 11, 12, 13, 10, 3])
        self.assertEqual(meta["routing_mode"], "ordered_strict")
        self.assertNotIn("loop_demotions", meta)

    def test_in_order_sequence_is_untouched(self):
        G = _ring_net()
        route, meta = route_ordered_with_ladder(
            G, 1, 2, [street("Ash Road"), street("Birch Road")])
        self.assertEqual(route, [1, 10, 11, 12, 2])
        self.assertEqual(meta["routing_mode"], "ordered_strict")
        self.assertNotIn("loop_demotions", meta)


def _detour_net():
    """Main Street 1-2-3-4-5 (two-way, 100 m edges) and Far Road, a one-way
    3 km excursion 4->20->21->2."""
    G = nx.MultiDiGraph()
    G.graph["crs"] = "epsg:4326"
    for n in range(1, 6):
        G.add_node(n, x=0.001 * n, y=0.0)
    G.add_node(20, x=0.004, y=0.006)
    G.add_node(21, x=0.002, y=0.006)
    for u, v in ((1, 2), (2, 3), (3, 4), (4, 5)):
        for a, b in ((u, v), (v, u)):
            G.add_edge(a, b, length=100.0, name="Main Street", highway="residential")
    G.add_edge(4, 20, length=1300.0, name="Far Road", highway="residential")
    G.add_edge(20, 21, length=400.0, name="Far Road", highway="residential")
    G.add_edge(21, 2, length=1300.0, name="Far Road", highway="residential")
    return G


class ReverseBudgetTests(unittest.TestCase):
    def test_reverse_out_of_budget_falls_back_to_shortest(self):
        G = _detour_net()
        route, meta = route_reverse(G, 5, 1, [street("Far Road")], forward_length_m=400.0)
        self.assertEqual(route, [5, 4, 3, 2, 1])
        self.assertEqual(meta["routing_mode"], "shortest_path")
        self.assertIn("over budget", meta["reverse_fallback"])
        self.assertEqual(meta["shortest_m"], 400.0)

    def test_reverse_within_budget_keeps_the_sequence(self):
        # A forward run this long makes the reversed sequence proportionate.
        G = _detour_net()
        route, meta = route_reverse(G, 5, 1, [street("Far Road")], forward_length_m=5000.0)
        self.assertEqual(route, [5, 4, 20, 21, 2, 1])
        self.assertNotEqual(meta["routing_mode"], "shortest_path")
        self.assertNotIn("reverse_fallback", meta)


if __name__ == "__main__":
    unittest.main()
