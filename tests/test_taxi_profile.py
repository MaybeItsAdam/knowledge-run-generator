"""Taxi-legal graph: tag rules, barrier cutting, contraflow, turn exemptions
and the per-route legality gate (``knowledge_run_generator.taxi_profile``).

London specifics these pin: bus gates that admit taxis are open, bus-only
roads are not, an LTN bollard is a dead end from both sides, a taxi
contraflow is drivable the wrong way, and a banned turn with ``except=taxi``
does not bind a cab.
"""

import unittest

import networkx as nx
from shapely.geometry import LineString

from knowledge_run_generator.taxi_profile import (
    TEMPORARY_CLOSURES,
    TaxiRules,
    apply_taxi_rules,
    barrier_blocks_taxi,
    describe_violation,
    restriction_exempts_taxi,
    resolve_taxi_access,
    route_notices,
    taxi_contraflow,
    taxi_way_access,
    temporary_closure,
)
from knowledge_run_generator.validator import _build_prohibited_set


class WayAccessTests(unittest.TestCase):
    def verdict(self, **tags):
        tags.setdefault("highway", "residential")
        return taxi_way_access(tags)[0]

    def test_plain_road_is_open(self):
        self.assertEqual(self.verdict(), "open")

    def test_bus_gate_admitting_psv_is_open(self):
        self.assertEqual(self.verdict(motor_vehicle="no", psv="yes"), "open")

    def test_bus_gate_admitting_taxi_is_open(self):
        self.assertEqual(self.verdict(access="no", bus="yes", taxi="yes"), "open")

    def test_bus_only_road_is_closed(self):
        # ``bus`` does not cover taxis.
        self.assertEqual(self.verdict(access="no", bus="yes"), "closed")

    def test_motor_vehicle_no_is_closed(self):
        self.assertEqual(self.verdict(motor_vehicle="no"), "closed")

    def test_taxi_no_overrides_psv_yes(self):
        self.assertEqual(self.verdict(motor_vehicle="no", psv="yes", taxi="no"), "closed")

    def test_motorcar_no_binds_a_cab_without_a_psv_tag(self):
        self.assertEqual(self.verdict(motorcar="no"), "closed")
        self.assertEqual(self.verdict(motorcar="no", psv="yes"), "open")

    def test_private_and_permit_are_closed(self):
        self.assertEqual(self.verdict(access="private"), "closed")
        self.assertEqual(self.verdict(motor_vehicle="permit"), "closed")

    def test_destination_is_destination_only(self):
        self.assertEqual(self.verdict(motor_vehicle="destination"), "destination")
        self.assertEqual(self.verdict(access="delivery"), "destination")

    def test_busway_is_closed_unless_opened(self):
        self.assertEqual(self.verdict(highway="busway"), "closed")
        self.assertEqual(self.verdict(highway="busway", psv="designated"), "open")

    def test_pedestrian_street_needs_explicit_taxi_access(self):
        self.assertEqual(self.verdict(highway="pedestrian"), "closed")
        self.assertEqual(self.verdict(highway="pedestrian", taxi="yes"), "open")

    def test_bus_lane_tags_do_not_close_the_road(self):
        self.assertEqual(self.verdict(highway="primary", **{"busway:left": "lane"}), "open")

    def test_list_valued_attributes(self):
        # osmnx merges tags into lists when it simplifies edges.
        self.assertEqual(resolve_taxi_access({"access": ["no", "yes"]})[0], "open")
        self.assertEqual(resolve_taxi_access({"access": "no;destination"})[0], "destination")

    def test_deciding_tag_is_reported(self):
        self.assertEqual(taxi_way_access({"highway": "residential",
                                          "motor_vehicle": "no"})[1],
                         "motor_vehicle=no")

    def test_hammersmith_bridge_as_mapped_is_closed(self):
        # OSM way 7587403 as tagged in 2026-09: the deck is a cycleway with
        # the old primary road recorded as disused, motor vehicles barred.
        tags = {"alt_name": "Hammersmith Bridge Road", "bridge": "yes",
                "disused:highway": "primary", "foot": "no",
                "highway": "cycleway", "motor_vehicle": "no",
                "name": "Hammersmith Bridge", "ref": "A306"}
        self.assertEqual(taxi_way_access(tags)[0], "closed")
        # Even with the motor_vehicle tag removed, a cycleway stays closed.
        tags.pop("motor_vehicle")
        self.assertEqual(taxi_way_access(tags)[0], "closed")


class ContraflowTests(unittest.TestCase):
    def test_psv_and_taxi_contraflow(self):
        self.assertTrue(taxi_contraflow({"oneway": "yes", "oneway:psv": "no"}))
        self.assertTrue(taxi_contraflow({"oneway": "yes", "oneway:taxi": "no"}))

    def test_bus_contraflow_does_not_admit_taxis(self):
        self.assertFalse(taxi_contraflow({"oneway": "yes", "oneway:bus": "no"}))

    def test_taxi_tag_overrides_psv(self):
        self.assertFalse(taxi_contraflow({"oneway:psv": "no", "oneway:taxi": "yes"}))


class BarrierTests(unittest.TestCase):
    def blocks(self, **tags):
        return barrier_blocks_taxi(tags)[0]

    def test_ltn_filters_block(self):
        for kind in ("bollard", "bus_trap", "block", "planter", "jersey_barrier",
                     "sump_buster", "chain", "cycle_barrier"):
            self.assertTrue(self.blocks(barrier=kind), kind)

    def test_filter_that_admits_taxis_does_not_block(self):
        self.assertFalse(self.blocks(barrier="bollard", taxi="yes"))
        self.assertFalse(self.blocks(barrier="bus_trap", psv="yes"))
        self.assertFalse(self.blocks(barrier="bollard", motor_vehicle="yes"))

    def test_rising_bollard_for_permit_holders_blocks(self):
        self.assertTrue(self.blocks(barrier="bollard", bollard="rising",
                                    motor_vehicle="private"))

    def test_gates_are_open_unless_closed_by_access(self):
        self.assertFalse(self.blocks(barrier="gate"))
        self.assertTrue(self.blocks(barrier="gate", access="private"))
        self.assertTrue(self.blocks(barrier="lift_gate", motor_vehicle="no"))

    def test_non_blocking_barriers(self):
        for kind in ("kerb", "height_restrictor", "cattle_grid", "entrance",
                     "toll_booth"):
            self.assertFalse(self.blocks(barrier=kind), kind)

    def test_not_a_barrier(self):
        self.assertFalse(self.blocks(highway="traffic_signals"))


class TurnExemptionTests(unittest.TestCase):
    def test_except_psv_or_taxi(self):
        self.assertTrue(restriction_exempts_taxi({"except": "psv"}))
        self.assertTrue(restriction_exempts_taxi({"except": "bicycle;bus;taxi"}))
        self.assertFalse(restriction_exempts_taxi({"except": "bicycle;bus"}))
        self.assertFalse(restriction_exempts_taxi({}))

    def test_prohibited_set_honours_exemptions_on_taxi_profile_only(self):
        G = nx.MultiDiGraph()
        for n in (1, 2, 3):
            G.add_node(n, x=0.0, y=0.0)
        G.add_edge(1, 2, osmid=10, length=1)
        G.add_edge(2, 3, osmid=20, length=1)
        r = [{"type": "no_right_turn", "from_way": 10, "via_node": 2,
              "to_way": 20, "except": "psv;bicycle"}]
        self.assertEqual(_build_prohibited_set(G, r, profile="drive"), {(1, 2, 3)})
        self.assertEqual(_build_prohibited_set(G, r, profile="taxi"), set())


def _raw_graph():
    """Unsimplified toy graph: a residential street 1-2-3-4 with a bollard at
    node 3, a bus gate 4-5 (motor_vehicle=no, psv=yes), a bus-only road 5-6,
    and a one-way 6->7 with a taxi contraflow."""
    G = nx.MultiDiGraph()
    for n in range(1, 8):
        G.add_node(n, x=-0.1 + n * 0.001, y=51.5)
    G.nodes[3]["barrier"] = "bollard"

    def two_way(u, v, **tags):
        G.add_edge(u, v, length=100.0, oneway=False, reversed=False, **tags)
        G.add_edge(v, u, length=100.0, oneway=False, reversed=True, **tags)

    two_way(1, 2, osmid=100, highway="residential", name="Filter Road")
    two_way(2, 3, osmid=100, highway="residential", name="Filter Road")
    two_way(3, 4, osmid=100, highway="residential", name="Filter Road")
    two_way(4, 5, osmid=200, highway="residential", name="Gate Street",
            motor_vehicle="no", psv="yes")
    two_way(5, 6, osmid=300, highway="residential", name="Bus Only",
            access="no", bus="yes")
    G.add_edge(6, 7, length=100.0, oneway=True, reversed=False, osmid=400,
               highway="residential", name="Contra Lane",
               **{"oneway:psv": "no"})
    return G


class ApplyRulesTests(unittest.TestCase):
    def setUp(self):
        self.G = _raw_graph()
        self.sidecar = apply_taxi_rules(self.G)

    def test_bollard_cuts_passage_both_ways(self):
        for u, v in ((2, 3), (3, 2), (3, 4), (4, 3)):
            self.assertFalse(self.G.has_edge(u, v), (u, v))
        self.assertTrue(self.G.has_edge(1, 2))
        self.assertIn("3", self.sidecar["barriers"])
        self.assertEqual(self.sidecar["barriers"]["3"]["names"], ["Filter Road"])

    def test_bus_gate_kept_bus_only_dropped(self):
        self.assertTrue(self.G.has_edge(4, 5))
        self.assertFalse(self.G.has_edge(5, 6))
        self.assertIn("300", self.sidecar["closed_ways"])

    def test_contraflow_edge_added(self):
        self.assertTrue(self.G.has_edge(6, 7))
        self.assertTrue(self.G.has_edge(7, 6))
        self.assertIn("400", self.sidecar["contraflow_ways"])


class RouteCheckTests(unittest.TestCase):
    """The gate, run on a *drive*-style graph that still has everything."""

    def setUp(self):
        self.G = _raw_graph()
        raw = _raw_graph()
        self.rules = TaxiRules.from_sidecar(apply_taxi_rules(raw))

    def test_route_through_bollard_is_flagged(self):
        v = self.rules.check_route(self.G, [1, 2, 3, 4])
        kinds = [x["kind"] for x in v]
        self.assertIn("barrier", kinds)
        self.assertIn("Modal filter (bollard) on Filter Road",
                      [describe_violation(x) for x in v])

    def test_barrier_inside_a_simplified_edge_is_seen_by_geometry(self):
        G = nx.MultiDiGraph()
        for n in (1, 4):
            G.add_node(n, x=self.G.nodes[n]["x"], y=51.5)
        coords = [(self.G.nodes[n]["x"], 51.5) for n in (1, 2, 3, 4)]
        G.add_edge(1, 4, osmid=100, length=300.0, geometry=LineString(coords))
        v = self.rules.check_route(G, [1, 4])
        self.assertEqual([x["kind"] for x in v], ["barrier"])

    def test_closed_way_is_flagged(self):
        v = self.rules.check_route(self.G, [4, 5, 6])
        self.assertEqual([x["kind"] for x in v], ["closed_way"])
        self.assertIn("closed to taxis", describe_violation(v[0]))

    def test_bus_gate_route_is_legal(self):
        self.assertEqual(self.rules.check_route(self.G, [5, 4]), [])

    def test_destination_only_through_traffic(self):
        G = nx.MultiDiGraph()
        for n in range(1, 6):
            G.add_node(n, x=float(n), y=0.0)
        for u, v, acc in ((1, 2, "yes"), (2, 3, "destination"), (3, 4, "yes"),
                          (4, 5, "destination")):
            G.add_edge(u, v, length=1.0, taxi_access=acc, osmid=u, name=f"S{u}")
        rules = TaxiRules()
        # Ending in an access-only street is fine; passing through is not.
        self.assertEqual(rules.check_route(G, [3, 4, 5]), [])
        self.assertEqual(rules.check_route(G, [2, 3, 4]), [])
        v = rules.check_route(G, [1, 2, 3, 4])
        self.assertEqual([x["kind"] for x in v], ["destination_through"])


class TemporaryClosureTests(unittest.TestCase):
    """A temporary closure stays open in the graph and puts a notice on the
    route, so it never permanently reroutes a run or moves its endpoint."""

    ALBERT = 591291850  # the south span of Albert Bridge, access=no

    def _graph(self):
        G = nx.MultiDiGraph()
        for n in range(1, 5):
            G.add_node(n, x=-0.166 - n * 0.0005, y=51.48 + n * 0.0005)
        # OSM tags of way 591291850 as of 2026-09-27: plain access=no.
        G.add_edge(1, 2, length=50.0, osmid=self.ALBERT, highway="primary",
                   name="Albert Bridge", access="no")
        G.add_edge(2, 1, length=50.0, osmid=self.ALBERT, highway="primary",
                   name="Albert Bridge", access="no")
        G.add_edge(2, 3, length=80.0, osmid=[4000, 4001], highway="primary",
                   name="Chelsea Embankment")
        G.add_edge(3, 4, length=90.0, osmid=5000, highway="primary",
                   name="Other Bridge", access="no")
        return G

    def test_albert_bridge_is_listed_with_a_note(self):
        c = temporary_closure(self.ALBERT)
        self.assertIsNotNone(c)
        self.assertEqual(c.name, "Albert Bridge")
        self.assertEqual(c.notice,
                         "Albert Bridge is temporarily closed. The route shown is the normal one.")
        self.assertIn("Remove once OSM reopens it", c.note)
        self.assertIsNone(temporary_closure(5000))
        self.assertIsNone(temporary_closure(None))

    def test_hammersmith_bridge_is_not_temporary(self):
        # Long-running: routes that need it ship crow-flies instead.
        for c in TEMPORARY_CLOSURES:
            self.assertNotIn("hammersmith", c.name.lower())
        for wid in (7587403, 314914065):
            self.assertIsNone(temporary_closure(wid))

    def test_notices_are_app_safe(self):
        for c in TEMPORARY_CLOSURES:
            self.assertTrue(c.note)
            for text in (c.notice, c.note):
                self.assertNotIn("\u2014", text)

    def test_graph_keeps_it_open_and_records_it(self):
        G = self._graph()
        sidecar = apply_taxi_rules(G)
        self.assertTrue(G.has_edge(1, 2) and G.has_edge(2, 1))
        self.assertNotIn(str(self.ALBERT), sidecar["closed_ways"])
        rec = sidecar["temporary_closures"][str(self.ALBERT)]
        self.assertEqual((rec["tagged"], rec["reason"]), ("closed", "access=no"))
        # An ordinary access=no way is still closed.
        self.assertFalse(G.has_edge(3, 4))
        self.assertIn("5000", sidecar["closed_ways"])

    def test_route_over_it_is_legal_and_carries_the_notice(self):
        rules = TaxiRules.from_sidecar(apply_taxi_rules(self._graph()))
        G = self._graph()
        self.assertEqual(rules.check_route(G, [1, 2, 3]), [])
        self.assertEqual(route_notices(G, [1, 2, 3, 2, 1]),
                         ["Albert Bridge is temporarily closed. The route shown is the normal one."])
        self.assertEqual(route_notices(G, [2, 3]), [])
        self.assertEqual(route_notices(G, []), [])


if __name__ == "__main__":
    unittest.main()
