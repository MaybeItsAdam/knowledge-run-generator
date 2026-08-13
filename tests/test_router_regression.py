import unittest

import networkx as nx

from knowledge_run_generator.router import get_ordered_route


class RouterRegressionTests(unittest.TestCase):
    """Cost-model regressions, re-pinned against the ordered router.

    The legacy multi-leg router (and its post-hoc route mutations,
    ``_clean_backtrack`` / ``_collapse_revisits``) is gone: routes are now
    emitted exactly as searched, so what used to be spliced away afterwards
    has to be uneconomical *inside* the search. These tests pin that.
    """

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

        route, info = get_ordered_route(G, 1, 3, [])
        self.assertEqual(route, [1, 2, 3])
        self.assertTrue(info["reached_goal"])

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

        route, info = get_ordered_route(G, 1, 3, [])
        self.assertEqual(route, [1, 3])
        self.assertTrue(info["reached_goal"])

    def test_ring_is_not_lapped_without_a_reason(self):
        """The IMAX pattern: with no discount to game, one orbit of a circular
        junction is strictly cheaper than two, so laps never form and there is
        nothing for a post-processor to splice away (the splicing post-
        processors are deleted)."""
        G = nx.MultiDiGraph()
        # Approach -> 4-node circular ring (one-way) -> exit.
        self._node(G, 10, -0.101, 51.500)
        self._node(G, 20, -0.100, 51.5005)
        self._node(G, 21, -0.0995, 51.501)
        self._node(G, 22, -0.100, 51.5015)
        self._node(G, 23, -0.1005, 51.501)
        self._node(G, 40, -0.099, 51.5015)
        G.add_edge(10, 20, length=60.0, highway="primary")
        for u, v in ((20, 21), (21, 22), (22, 23), (23, 20)):
            G.add_edge(u, v, length=25.0, highway="primary", junction="circular")
        G.add_edge(22, 40, length=60.0, highway="primary")

        route, info = get_ordered_route(G, 10, 40, [])
        self.assertTrue(info["reached_goal"])
        self.assertEqual(len(route), len(set(route)),
                         f"route revisits a node (lap): {route}")


if __name__ == "__main__":
    unittest.main()
