"""
Call generation tests.

The property under test is that the emitted call reads like something an
examiner would accept. OSM leaves ~4% of drivable London edges unnamed —
mostly 30 m slip roads and roundabout arms — and naming those "Unknown Road"
both produced uncallable steps and collapsed them into one hub node in the
consuming app's connection graph. An unnamed leg must therefore be absorbed
into the step around it, never emitted as its own.
"""

import unittest

import networkx as nx

from knowledge_run_generator.caller import generate_call, resolve_street_name


def _line(names, spacing=0.001):
    """A straight west-to-east chain of nodes.

    ``names`` gives the street name of each leg; None means the way carries no
    ``name`` tag, as unnamed OSM connectors do. Returns (graph, route_nodes).
    """
    G = nx.MultiDiGraph()
    G.graph["crs"] = "epsg:4326"
    for i in range(len(names) + 1):
        G.add_node(i, x=i * spacing, y=0.0)
    for i, name in enumerate(names):
        attrs = {"length": 100.0}
        if name is not None:
            attrs["name"] = name
        G.add_edge(i, i + 1, **attrs)
    return G, list(range(len(names) + 1))


class ResolveStreetNameTests(unittest.TestCase):
    def test_prefers_name(self):
        self.assertEqual(resolve_street_name({"name": "Green Lanes"}), "Green Lanes")

    def test_takes_first_of_a_multi_valued_tag(self):
        self.assertEqual(resolve_street_name({"name": ["Strand", "A4"]}), "Strand")

    def test_falls_back_to_the_signed_route_number(self):
        # The plate says A501 even where OSM has no name for the way.
        self.assertEqual(resolve_street_name({"ref": "A501"}), "A501")

    def test_unnamed_edge_has_no_name(self):
        self.assertIsNone(resolve_street_name({"highway": "residential"}))
        self.assertIsNone(resolve_street_name({}))


class GenerateCallTests(unittest.TestCase):
    def _names(self, steps):
        return [s["name"] for s in steps]

    def test_never_emits_unknown_road(self):
        G, route = _line(["Green Lanes", None, "Brownswood Road"])
        steps = generate_call(G, route)
        self.assertNotIn("Unknown Road", self._names(steps))

    def test_unnamed_connector_does_not_split_a_single_street(self):
        # Green Lanes -> 30 m unnamed junction arm -> Green Lanes is one street
        # to a driver, and must stay one step.
        G, route = _line(["Green Lanes", None, "Green Lanes"])
        steps = generate_call(G, route)
        self.assertEqual(self._names(steps), ["Green Lanes", "Destination"])

    def test_unnamed_leg_distance_is_absorbed_not_lost(self):
        G, route = _line(["Green Lanes", None, "Green Lanes"])
        steps = generate_call(G, route)
        # Three 100 m legs, all on the one callable street.
        self.assertEqual(steps[0]["distance"], 300.0)

    def test_call_opens_on_the_first_named_street(self):
        # Leaving a forecourt onto Green Lanes: the call says Green Lanes.
        G, route = _line([None, "Green Lanes", "Brownswood Road"])
        steps = generate_call(G, route)
        self.assertEqual(steps[0]["name"], "Green Lanes")
        self.assertTrue(steps[0]["instruction"].startswith("Leave Origin on Green Lanes"))

    def test_named_street_changes_still_split(self):
        G, route = _line(["Green Lanes", "Brownswood Road"])
        steps = generate_call(G, route)
        self.assertEqual(
            self._names(steps), ["Green Lanes", "Brownswood Road", "Destination"]
        )

    def test_total_step_distance_matches_the_route(self):
        G, route = _line(["Green Lanes", None, None, "Brownswood Road", None])
        steps = generate_call(G, route)
        self.assertEqual(sum(s["distance"] for s in steps), 500.0)

    def test_route_of_entirely_unnamed_edges_degrades_visibly(self):
        # Nothing to call — better to say so than to invent a street.
        G, route = _line([None, None])
        steps = generate_call(G, route)
        self.assertEqual(self._names(steps), ["Unknown Road", "Destination"])

    def test_short_route_returns_nothing(self):
        G, route = _line(["Green Lanes"])
        self.assertEqual(generate_call(G, []), [])
        self.assertEqual(generate_call(G, route[:1]), [])


if __name__ == "__main__":
    unittest.main()
