"""Crow-flies routing (``router.route_crow_flies`` / ``CrowFliesCost``), the
route-source reason text, and the pipeline's substitution of a crow-flies
route for a Blue Book run whose sequence can't be driven.

The rule being encoded: a Knowledge run is the legal route for a London taxi
that stays closest to the straight line between start and end.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

import networkx as nx
import osmnx as ox

import knowledge_run_generator.blue_book_demo.run_pipeline as rp
from knowledge_run_generator.router import (
    CrowFliesCost,
    get_ordered_route,
    route_crow_flies,
)
from knowledge_run_generator.route_source import (
    HAMMERSMITH_BRIDGE_REASON,
    blue_book_failure_reason,
)
from knowledge_run_generator.taxi_profile import TaxiRules, parse_filtered_streets

M_LAT = 1 / 110_540.0
M_LON = 1 / (111_320.0 * 0.6225)   # cos(51.5)


def _grid_graph():
    """Start S at (0, 0), end E at (1000, 0) metres.

    * the *straight* route S-a-E runs along the line but is 1,300 m long
      (a slow wiggle, modelled as extra length);
    * the *bulge* route S-b-c-E is 1,200 m long but swings 400 m off it.
    """
    G = nx.MultiDiGraph()
    pts = {"S": (0, 0), "a": (500, 0), "E": (1000, 0),
           "b": (100, 400), "c": (900, 400)}
    for n, (x, y) in pts.items():
        G.add_node(n, x=-0.1 + x * M_LON, y=51.5 + y * M_LAT)

    def edge(u, v, length, **kw):
        G.add_edge(u, v, length=length, highway="residential", **kw)
        G.add_edge(v, u, length=length, highway="residential", **kw)

    edge("S", "a", 650.0)
    edge("a", "E", 650.0)
    edge("S", "b", 400.0)
    edge("b", "c", 400.0)
    edge("c", "E", 400.0)
    return G


class CrowFliesCostTests(unittest.TestCase):
    def test_lateral_integral_of_an_edge_on_the_line_is_zero(self):
        G = _grid_graph()
        cost = CrowFliesCost(G, "S", "E", lam=1.0)
        data = G.get_edge_data("S", "a")[0]
        self.assertAlmostEqual(cost.lateral_integral("S", "a", data), 0.0, delta=1.0)

    def test_lateral_integral_of_a_parallel_edge(self):
        G = _grid_graph()
        cost = CrowFliesCost(G, "S", "E", lam=1.0)
        data = G.get_edge_data("b", "c")[0]
        # 800 m long, 400 m off the line throughout.
        self.assertAlmostEqual(cost.lateral_integral("b", "c", data) / 1000,
                               800 * 400 / 1000, delta=3.0)

    def test_cost_never_below_length(self):
        G = _grid_graph()
        cost = CrowFliesCost(G, "S", "E", lam=4.0)
        for u, v, data in G.edges(data=True):
            self.assertGreaterEqual(cost(u, v, data, None), data["length"])

    def test_scale_is_relative_to_the_run_and_floored(self):
        G = _grid_graph()
        self.assertAlmostEqual(CrowFliesCost(G, "S", "E").scale, 1000.0, delta=5)
        self.assertEqual(CrowFliesCost(G, "S", "a").scale, 1000.0)  # 500 m run
        self.assertEqual(CrowFliesCost(G, "S", "E", mode="absolute").scale, 1000.0)


class RouteCrowFliesTests(unittest.TestCase):
    def test_lambda_zero_is_shortest_legal(self):
        route, meta = route_crow_flies(_grid_graph(), "S", "E", lam=0.0)
        self.assertEqual(route, ["S", "b", "c", "E"])
        self.assertEqual(meta["routing_mode"], "crow_flies")

    def test_lambda_pulls_the_route_onto_the_line(self):
        route, _ = route_crow_flies(_grid_graph(), "S", "E", lam=1.0)
        self.assertEqual(route, ["S", "a", "E"])

    def test_prohibited_turns_are_honoured(self):
        route, _ = route_crow_flies(_grid_graph(), "S", "E", lam=1.0,
                                    prohibited_turns={("S", "a", "E")})
        self.assertEqual(route, ["S", "b", "c", "E"])

    def test_road_class_weight_prefers_main_roads(self):
        G = _grid_graph()
        for u, v, k in list(G.edges(keys=True)):
            if {u, v} <= {"S", "b", "c", "E"}:
                G.edges[u, v, k]["highway"] = "primary"
        route, _ = route_crow_flies(G, "S", "E", lam=0.0,
                                    class_weights={"residential": 1.2})
        self.assertEqual(route, ["S", "b", "c", "E"])
        G2 = _grid_graph()
        for u, v, k in list(G2.edges(keys=True)):
            if {u, v} <= {"S", "a", "E"}:
                G2.edges[u, v, k]["highway"] = "primary"
        route, _ = route_crow_flies(G2, "S", "E", lam=0.0,
                                    class_weights={"residential": 1.2})
        self.assertEqual(route, ["S", "a", "E"])

    def test_access_only_street_is_not_used_to_pass_through(self):
        G = _grid_graph()
        for k in G["S"]["a"]:
            G.edges["S", "a", k]["taxi_access"] = "yes"
        for k in G["a"]["E"]:
            G.edges["a", "E", k]["taxi_access"] = "yes"
        # Make the bulge access-only in the middle: through traffic.
        for k in G["b"]["c"]:
            G.edges["b", "c", k]["taxi_access"] = "destination"
        route, _ = get_ordered_route(G, "S", "E", [], corridor_margin_deg=None,
                                     pure_length_cost=True)
        self.assertEqual(route, ["S", "a", "E"])


class FailureReasonTests(unittest.TestCase):
    def test_hammersmith_bridge(self):
        self.assertEqual(
            blue_book_failure_reason(demoted=[("HAMMERSMITH BRIDGE ROAD", "exact")],
                                     no_route=True),
            HAMMERSMITH_BRIDGE_REASON)

    def test_barrier_on_a_demoted_street_is_named(self):
        rules = TaxiRules(barriers={"9": {"lat": 51.5, "lon": -0.1,
                                          "reason": "barrier=bollard",
                                          "names": ["Braes Street"]}})
        reason = blue_book_failure_reason(
            loop_demotions=[("BRAES STREET", "exact")], rules=rules,
            run_box=(51.49, -0.11, 51.51, -0.09))
        self.assertEqual(reason, "Modal filter (bollard) on Braes Street")

    def test_road_retagged_as_cycleway_is_named_as_a_filter(self):
        # Braes Street N1: no barrier node, the filtered stretch is mapped as
        # a named highway=cycleway carrying road tags.
        rules = TaxiRules(filtered_streets=parse_filtered_streets([
            {"tags": {"highway": "cycleway", "name": "Braes Street",
                      "maxspeed": "20 mph", "emergency": "yes"},
             "center": {"lat": 51.5419, "lon": -0.0983}}]))
        reason = blue_book_failure_reason(
            loop_demotions=[("BRAES STREET", "exact")], rules=rules,
            run_box=(51.53, -0.11, 51.55, -0.03))
        self.assertEqual(reason, "Modal filter on Braes Street (closed to motor traffic)")

    def test_named_pavement_or_cycle_track_is_not_a_filter(self):
        streets = parse_filtered_streets([
            {"tags": {"highway": "footway", "name": "Wilton Road"},
             "center": {"lat": 51.495, "lon": -0.143}},
            {"tags": {"highway": "cycleway", "name": "Temple Place",
                      "oneway": "no"},
             "center": {"lat": 51.511, "lon": -0.112}},
            {"tags": {"highway": "footway", "name": "Riverside Walkway",
                      "maxspeed": "5 mph"},
             "center": {"lat": 51.5, "lon": -0.1}},
        ])
        self.assertEqual(streets, {})

    def test_far_away_namesake_barrier_is_not_blamed(self):
        rules = TaxiRules(barriers={"9": {"lat": 51.6, "lon": 0.1,
                                          "reason": "barrier=bollard",
                                          "names": ["High Street"]}})
        reason = blue_book_failure_reason(
            loop_demotions=[("HIGH STREET", "exact")], rules=rules,
            run_box=(51.49, -0.11, 51.51, -0.09))
        self.assertIn("needs a loop of 1 km or more", reason)

    def test_taxi_violation_on_the_blue_book_route(self):
        reason = blue_book_failure_reason(taxi_violations=[
            {"kind": "closed_way", "name": "Bus Road", "reason": "access=no"}])
        self.assertEqual(reason, "Bus Road is closed to taxis (access=no)")

    def test_sanity(self):
        reason = blue_book_failure_reason(
            sanity_reasons=["AB route 12294m is 2.0x the 6067m shortest legal route"])
        self.assertEqual(reason, "The Blue Book route fails the sanity check "
                                 "(route 12294m is 2.0x the 6067m shortest legal route)")

    def test_reasons_have_no_em_dash(self):
        samples = [
            blue_book_failure_reason(no_route=True),
            blue_book_failure_reason(hard_gap_names=[("ALIE STREET", "exact")]),
            blue_book_failure_reason(legal=False),
            blue_book_failure_reason(),
        ]
        for s in samples:
            self.assertNotIn("—", s)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "run1_graph.graphml"


class PipelineSubstitutionTests(unittest.TestCase):
    """End to end: a Blue Book run whose sequence the ladder had to break
    ships the crow-flies route, marked, and still passes every gate."""

    @classmethod
    def setUpClass(cls):
        cls.graph = ox.load_graphml(FIXTURE)
        cls.graph.graph["krg_profile"] = "taxi"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.output = self.tmp / "runPoints.json"
        (self.tmp / "knowledge_pois.json").write_text(json.dumps([{
            "name": "Manor House Station", "postal_district": "N4",
            "category": "station",
            "coordinates": [self.graph.nodes[0]["x"], self.graph.nodes[0]["y"]],
        }]))
        self._patched = {name: getattr(rp, name) for name in (
            "load_graph", "load_turn_restrictions", "geocode_and_snap",
            "load_cached_pois", "load_taxi_rules", "route_ordered_with_ladder")}
        rp.load_graph = lambda network_type=None, **kw: self.graph
        rp.load_turn_restrictions = lambda G, cache_dir=None: set()
        rp.load_cached_pois = lambda *candidates: {}
        rp.load_taxi_rules = lambda: TaxiRules()

        def resolve(address, G, poi_overrides=None, gazetteer=None):
            entry = gazetteer.resolve(address, G) if gazetteer else None
            if entry is None:
                return None
            node = G.nodes[entry.snapped_node]
            return (node["y"], node["x"], entry.snapped_node)
        rp.geocode_and_snap = resolve
        self._env = {k: os.environ.get(k) for k in ("KRG_ALLOW_NO_OSM", "KRG_KNOWLEDGE_POIS")}
        os.environ["KRG_ALLOW_NO_OSM"] = "1"
        os.environ["KRG_KNOWLEDGE_POIS"] = str(self.tmp / "knowledge_pois.json")

    def tearDown(self):
        for name, original in self._patched.items():
            setattr(rp, name, original)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _run(self):
        rp.process_runs(self.output, select_ids={1}, cache_dir=self.tmp / "cache")
        return (json.loads(self.output.read_text()),
                json.loads((self.tmp / "qa_report.json").read_text()))

    def test_passing_blue_book_run_keeps_its_route(self):
        runs, qa = self._run()
        self.assertEqual(runs[0]["route_source"], "blue_book")
        self.assertIsNone(runs[0]["route_source_reason"])
        self.assertEqual(qa["1"]["route_source"], "blue_book")
        self.assertTrue(qa["1"]["taxi_legal"])
        self.assertTrue(qa["1"]["passed"])

    def test_undrivable_blue_book_sequence_ships_crow_flies(self):
        original = self._patched["route_ordered_with_ladder"]

        def broken_ladder(*args, **kwargs):
            route, meta = original(*args, **kwargs)
            if kwargs.get("compute_optimum"):   # the forward Blue Book run
                meta["demoted_constraints"] = [("UPPER STREET", "exact")]
                meta["loop_demotions"] = [("UPPER STREET", "exact")]
                meta["routing_mode"] = "ordered_relaxed"
            return route, meta
        rp.route_ordered_with_ladder = broken_ladder

        runs, qa = self._run()
        run, rec = runs[0], qa["1"]
        self.assertEqual(run["route_source"], "crow_flies")
        self.assertEqual(run["route_source_reason"],
                         "The Blue Book order at Upper Street needs a loop of "
                         "1 km or more on today's roads")
        self.assertEqual(rec["route_source"], "crow_flies")
        self.assertEqual(rec["routing_mode"], "crow_flies")
        self.assertTrue(rec["passed"])
        self.assertTrue(rec["legal"] and rec["rev_legal"] and rec["sane"])
        self.assertFalse(rec["blue_book"]["passed"])
        self.assertEqual(rec["blue_book"]["hard_gaps"], 1)
        self.assertEqual(rec["hard_gaps"], 0)
        self.assertNotIn("—", json.dumps(run))


if __name__ == "__main__":
    unittest.main()
