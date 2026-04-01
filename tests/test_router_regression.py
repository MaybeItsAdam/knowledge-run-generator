import unittest

import networkx as nx

from knowledge_run_generator.router import _route_through_waypoints


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

        route = _route_through_waypoints(G, origin_node=1, dest_node=3, waypoint_nodes=[])
        self.assertEqual(route, [1, 2, 3])

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

        route = _route_through_waypoints(G, origin_node=1, dest_node=3, waypoint_nodes=[])
        self.assertEqual(route, [1, 3])


if __name__ == "__main__":
    unittest.main()
