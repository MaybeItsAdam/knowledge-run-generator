import unittest

import networkx as nx

from knowledge_run_generator.router import (
    _collapse_revisits,
    _route_through_waypoints,
)


class RouterRegressionTests(unittest.TestCase):
    def _node(self, G, nid, x, y):
        G.add_node(nid, x=x, y=y)

    def test_avoids_service_shortcut_when_mainline_available(self):
        G = nx.MultiDiGraph()

        # Mainline corridor
        self._node(G, 1, -0.10, 51.50)
        self._node(G, 2, -0.10, 51.505)
        self._node(G, 3, -0.10, 51.51)
        G.add_edge(1, 2, length=100.0, highway="primary")
        G.add_edge(2, 3, length=100.0, highway="primary")

        # Slightly shorter service-link alternative that should be disfavored.
        self._node(G, 4, -0.099, 51.503)
        self._node(G, 5, -0.099, 51.508)
        G.add_edge(1, 4, length=55.0, highway="service")
        G.add_edge(4, 5, length=55.0, highway="service")
        G.add_edge(5, 3, length=55.0, highway="service")

        route, stats = _route_through_waypoints(G, origin_node=1, dest_node=3, waypoint_nodes=[])
        self.assertEqual(route, [1, 2, 3])
        self.assertEqual(stats, {"unreachable_legs": 0, "truncated_legs": 0})

    def test_u_turn_loop_is_not_preferred(self):
        G = nx.MultiDiGraph()

        self._node(G, 1, -0.10, 51.50)
        self._node(G, 2, -0.10, 51.501)
        self._node(G, 3, -0.10, 51.503)

        # Correct path
        G.add_edge(1, 3, length=140.0, highway="primary")

        # Tempting but nonsensical immediate U-turn sequence 1->2->1->3.
        G.add_edge(1, 2, length=20.0, highway="primary")
        G.add_edge(2, 1, length=20.0, highway="primary")

        route, stats = _route_through_waypoints(G, origin_node=1, dest_node=3, waypoint_nodes=[])
        self.assertEqual(route, [1, 3])
        self.assertEqual(stats, {"unreachable_legs": 0, "truncated_legs": 0})


class CollapseRevisitsTests(unittest.TestCase):
    def test_no_revisits_unchanged(self):
        self.assertEqual(_collapse_revisits([1, 2, 3, 4]), [1, 2, 3, 4])

    def test_empty_input(self):
        self.assertEqual(_collapse_revisits([]), [])

    def test_single_revisit_is_collapsed(self):
        # A→B→C→D→A→E  → keep first A, drop the lap
        self.assertEqual(_collapse_revisits([1, 2, 3, 4, 1, 5]), [1, 5])

    def test_imax_lap_pattern(self):
        # Mirrors run 4: enter, orbit, spur back through start, orbit again,
        # exit. The final node sequence should keep the first orbit only.
        path = [
            10, 11, 12,        # approach
            20, 21, 22, 23,    # first orbit
            12,                # back to a previous node (the "lap" closure)
            30, 31, 32,        # second orbit (should be discarded)
            12,                # back again to same node
            40, 41,            # finally onto the bridge
        ]
        out = _collapse_revisits(path)
        self.assertEqual(out, [10, 11, 12, 40, 41])

    def test_inner_revisit_collapses_to_first_occurrence(self):
        # Node 3 appears twice; keep first occurrence and continue from after
        # the second.
        self.assertEqual(_collapse_revisits([1, 2, 3, 4, 5, 3, 6, 7]),
                         [1, 2, 3, 6, 7])


if __name__ == "__main__":
    unittest.main()
