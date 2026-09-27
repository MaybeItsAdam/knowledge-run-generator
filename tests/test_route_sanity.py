"""The hard route-sanity gate (``validator.check_route_sanity``).

Directness (`is_direct`, ratio 1.8) is triage only, because Blue Book runs are
legitimately indirect. That left nothing to stop the absurd: run 150 shipped
at 35.9 km for a 2.7 km run, run 47 at 32.5 km through the Blackwall Tunnel,
run 311's route left the six-mile radius — all `passed`. These tests pin the
floor under the gate.
"""

import unittest

import networkx as nx

from knowledge_run_generator.validator import (
    CHARING_CROSS,
    SIX_MILES_M,
    check_route_sanity,
    validate_route,
)

CX_LAT, CX_LON = CHARING_CROSS
KM_LAT = 1 / 111.195          # degrees of latitude per km


def _chain(points, length_scale=1.0):
    """A path graph through ``points`` [(lat, lon), ...], edges bidirectional,
    each edge's length the crow-flies distance times ``length_scale``."""
    from knowledge_run_generator.validator import _haversine
    G = nx.MultiDiGraph()
    for i, (lat, lon) in enumerate(points):
        G.add_node(i, y=lat, x=lon)
    for i in range(len(points) - 1):
        (a_lat, a_lon), (b_lat, b_lon) = points[i], points[i + 1]
        length = _haversine(a_lat, a_lon, b_lat, b_lon) * length_scale
        G.add_edge(i, i + 1, length=length, name="Test Road")
        G.add_edge(i + 1, i, length=length, name="Test Road")
    return G


def _north(km):
    return (CX_LAT + km * KM_LAT, CX_LON)


class RouteSanityTests(unittest.TestCase):
    def test_direct_route_is_sane(self):
        G = _chain([_north(0), _north(1), _north(2)])
        ok, m, reasons = check_route_sanity(G, [0, 1, 2], 0, 2, shortest_m=2000)
        self.assertTrue(ok, reasons)
        self.assertAlmostEqual(m["straight_ratio"], 1.0, places=2)

    def test_gross_detour_against_the_straight_line_fails(self):
        # 2 km apart, 8 km driven (a 3 km excursion and back).
        G = _chain([_north(0), _north(3), _north(5), _north(2)])
        ok, m, reasons = check_route_sanity(G, [0, 1, 2, 3], 0, 3)
        self.assertFalse(ok)
        self.assertTrue(any("straight line" in r for r in reasons), reasons)

    def test_gross_detour_against_the_shortest_route_fails(self):
        # Winding streets make the straight-line ratio useless (2.5x), but
        # the route is still 2.5x the shortest legal route on the graph.
        G = _chain([_north(0), _north(1), _north(2)], length_scale=2.5)
        ok, _, reasons = check_route_sanity(G, [0, 1, 2], 0, 2, shortest_m=2000)
        self.assertFalse(ok)
        self.assertTrue(any("shortest legal route" in r for r in reasons), reasons)

    def test_small_absolute_excess_is_not_gross(self):
        # A short run looping round a one-way block: 3.5x on ratio, but only
        # 1.25 km extra — not the failure this gate exists for.
        G = _chain([_north(0), _north(1.0), _north(0.5)])
        ok, _, reasons = check_route_sanity(G, [0, 1, 2], 0, 2, shortest_m=600)
        self.assertTrue(ok, reasons)

    def test_leaving_the_six_mile_radius_fails(self):
        r_km = SIX_MILES_M / 1000
        G = _chain([_north(r_km - 1), _north(r_km + 0.8), _north(r_km - 0.5)])
        ok, m, reasons = check_route_sanity(G, [0, 1, 2], 0, 2)
        self.assertFalse(ok)
        self.assertGreater(m["radius_excess_m"], 500)
        self.assertTrue(any("six-mile radius" in r for r in reasons), reasons)

    def test_an_endpoint_outside_the_radius_raises_the_allowance(self):
        # The endpoint itself is 1 km outside; the route may reach it.
        r_km = SIX_MILES_M / 1000
        G = _chain([_north(r_km - 1), _north(r_km + 1)])
        ok, _, reasons = check_route_sanity(G, [0, 1], 0, 1)
        self.assertTrue(ok, reasons)

    def test_validate_route_fails_an_insane_route(self):
        G = _chain([_north(0), _north(3), _north(5), _north(2)])
        result = validate_route(G, [0, 1, 2, 3], 0, 3, config={})
        self.assertTrue(result.is_legal)
        self.assertFalse(result.is_sane)
        self.assertFalse(result.passed)

    def test_validate_route_passes_a_sane_route(self):
        G = _chain([_north(0), _north(1), _north(2)])
        result = validate_route(G, [0, 1, 2], 0, 2, config={"shortest_m": 2000})
        self.assertTrue(result.is_sane)
        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()
