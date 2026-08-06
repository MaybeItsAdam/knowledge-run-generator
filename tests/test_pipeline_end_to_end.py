"""
End-to-end pipeline test against a committed graph fixture.

Everything else in the suite tests a function; this drives ``process_runs``
itself — parse, resolve, route, correct, validate, call, QA — so the loop that
actually produces the 320 runs is exercised on every commit rather than only
when someone runs a multi-hour build by hand.

The geocoder is deliberately unavailable: any endpoint that fails to resolve
from the fixture data is a test failure, not a silent network call.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

import osmnx as ox

import knowledge_run_generator.blue_book_demo.run_pipeline as rp
from knowledge_run_generator.validator import check_run_shape

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "run1_graph.graphml"


class PipelineEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = ox.load_graphml(FIXTURE)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.output = self.tmp / "runPoints.json"

        # The Points List supplies the origin; the destination is a plain
        # street, so it can only come from the gazetteer's street tier.
        (self.tmp / "knowledge_pois.json").write_text(json.dumps([
            {
                "name": "Manor House Station",
                "postal_district": "N4",
                "category": "station",
                "coordinates": [self.graph.nodes[0]["x"], self.graph.nodes[0]["y"]],
            },
        ]))

        self._patched = {
            "load_graph": rp.load_graph,
            "load_turn_restrictions": rp.load_turn_restrictions,
            "geocode_and_snap": rp.geocode_and_snap,
            "load_cached_pois": rp.load_cached_pois,
        }
        rp.load_graph = lambda network_type=None, **kw: self.graph
        rp.load_turn_restrictions = lambda G, cache_dir=None: set()
        rp.geocode_and_snap = self._offline_only_resolve
        # The real constants/osm_pois.json must not leak into the fixture
        # graph: its real-London coordinates are kilometres from the synthetic
        # geometry and would hijack the street-tier resolution this test
        # exercises. Emptying the harvest triggers the pipeline's empty-OSM
        # guard, so opt out of it explicitly rather than weakening the check.
        rp.load_cached_pois = lambda *candidates: {}
        self._old_allow_no_osm = os.environ.get("KRG_ALLOW_NO_OSM")
        os.environ["KRG_ALLOW_NO_OSM"] = "1"
        self.geocoder_calls = []

    def tearDown(self):
        for name, original in self._patched.items():
            setattr(rp, name, original)
        if self._old_allow_no_osm is None:
            os.environ.pop("KRG_ALLOW_NO_OSM", None)
        else:
            os.environ["KRG_ALLOW_NO_OSM"] = self._old_allow_no_osm
        self._tmp.cleanup()

    def _offline_only_resolve(self, address, G, poi_overrides=None, gazetteer=None):
        """geocode_and_snap with the network path removed."""
        if gazetteer is not None:
            entry = gazetteer.resolve(address, G)
            if entry is not None:
                node = G.nodes[entry.snapped_node]
                return (node["y"], node["x"], entry.snapped_node)
        self.geocoder_calls.append(address)
        return None

    def _run(self):
        rp.process_runs(self.output, select_ids={1}, cache_dir=self.tmp / "cache")
        runs = json.loads(self.output.read_text())
        qa = json.loads((self.tmp / "qa_report.json").read_text())
        return runs, qa

    # -- the run itself -------------------------------------------------

    def test_run_1_is_produced_without_touching_the_geocoder(self):
        runs, _qa = self._run()
        self.assertEqual(self.geocoder_calls, [])
        self.assertEqual([r["id"] for r in runs], [1])

    def test_produced_run_is_structurally_valid(self):
        runs, _qa = self._run()
        self.assertEqual(check_run_shape(runs[0]), [])

    def test_run_carries_geometry_steps_and_a_reverse_direction(self):
        runs, _qa = self._run()
        run = runs[0]
        self.assertGreater(len(run["route"]["geometry"]["coordinates"]), 2)
        self.assertGreater(run["route"]["distance"], 0)
        self.assertTrue(run["route"]["steps"])
        self.assertTrue(run["routeReverse"]["geometry"]["coordinates"])
        self.assertEqual(run["route"]["steps"][-1]["instruction"], "Arrive Destination")

    def test_route_follows_the_blue_book_streets(self):
        runs, _qa = self._run()
        named = {s["name"].upper() for s in runs[0]["route"]["steps"]}
        for street in ("GREEN LANES", "UPPER STREET", "MILNER SQUARE"):
            with self.subTest(street=street):
                self.assertIn(street, named)

    def test_step_distances_sum_to_the_route_distance(self):
        # The call and the metadata must agree; they used to pick different
        # edges out of a parallel bundle.
        runs, _qa = self._run()
        route = runs[0]["route"]
        self.assertAlmostEqual(
            sum(s.get("distance", 0) for s in route["steps"]),
            route["distance"],
            delta=1.0,
        )

    # -- the QA record --------------------------------------------------

    def test_qa_record_reports_a_usable_run(self):
        _runs, qa = self._run()
        record = qa["1"]
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["shape_problems"], [])
        self.assertTrue(record["preflight_ok"])
        self.assertTrue(record["passed"])

    def test_qa_reports_router_search_failures_and_provenance(self):
        _runs, qa = self._run()
        self.assertEqual(qa["1"]["unreachable_legs"], 0)
        self.assertEqual(qa["1"]["truncated_legs"], 0)
        provenance = qa["_provenance"]
        self.assertEqual(provenance["graph_nodes"], self.graph.number_of_nodes())
        self.assertEqual(provenance["network_type"], "drive")

    def test_unresolvable_endpoint_is_recorded_not_dropped(self):
        # Strip the Points List: the origin can no longer resolve, and the run
        # must leave a failure record behind rather than vanishing.
        (self.tmp / "knowledge_pois.json").write_text("[]")
        runs, qa = self._run()
        self.assertEqual(runs, [])
        self.assertEqual(qa["1"]["status"], "failed")
        self.assertIn("geocode", qa["1"]["failure_reason"])
        self.assertIn("MANOR HOUSE STATION N4", self.geocoder_calls)


if __name__ == "__main__":
    unittest.main()
