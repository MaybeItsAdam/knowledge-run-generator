"""
Endpoint plausibility tests (ROADMAP Stage 4).

Snap distance cannot catch a cleanly-snapped endpoint in the wrong borough;
the district model can. These pin the model's robustness rules (median
centre, p95 radius, floor, n >= 5) and the preflight verdicts built on it,
against the exact failure class of Runs 131/177/206.
"""

import unittest

from knowledge_run_generator.gazetteer import (
    DistrictModel,
    GazetteerEntry,
    _PoiTable,
    preflight_run,
)

# A tight synthetic district around (51.50, -0.10) and a single-point one.
def _pois():
    out = {}
    for i in range(20):
        out[f"POINT {i} SE1"] = {
            "lat": 51.500 + (i % 5) * 0.002,
            "lon": -0.100 + (i // 5) * 0.002,
            "postal_district": "SE1",
        }
    out["LONELY SW2"] = {"lat": 51.45, "lon": -0.12, "postal_district": "SW2"}
    return out


def _entry(lat, lon, mismatch=False):
    # source deliberately not "override": curator-pinned overrides are exempt
    # from the district check (see test below).
    return GazetteerEntry(
        canonical_name="X", lat=lat, lon=lon, snapped_node=1,
        snap_distance_m=0.0, district_mismatch=mismatch,
        source="knowledge_poi",
    )


class DistrictModelTests(unittest.TestCase):
    def setUp(self):
        self.model = DistrictModel(_pois())

    def test_small_districts_have_a_centre_but_no_check(self):
        # A 1-point district can still anchor street-tier disambiguation,
        # but is never allowed to fail an endpoint.
        self.assertIsNotNone(self.model.centre("SW2"))
        self.assertIsNone(self.model.check("SW2", 51.45, -0.12))

    def test_point_inside_the_district_is_ok(self):
        verdict = self.model.check("SE1", 51.504, -0.096)
        self.assertEqual(verdict["status"], "ok")

    def test_point_far_away_fails(self):
        # 18 km away — the Run 131 class.
        verdict = self.model.check("SE1", 51.35, -0.05)
        self.assertEqual(verdict["status"], "fail")

    def test_radius_floor_keeps_tiny_districts_tolerant(self):
        # All 20 points span ~1 km; the floor guarantees at least 1 km of
        # slack, so a point 900 m out warns at worst, never fails.
        verdict = self.model.check("SE1", 51.5095, -0.104)
        self.assertIn(verdict["status"], ("ok", "warn"))

    def test_unknown_district_returns_none(self):
        self.assertIsNone(self.model.check("ZZ9", 51.5, -0.1))
        self.assertIsNone(self.model.check(None, 51.5, -0.1))


class PreflightDistrictTests(unittest.TestCase):
    def setUp(self):
        self.model = DistrictModel(_pois())

    def _preflight(self, entry, name):
        return preflight_run(
            entry, _entry(51.501, -0.099), [], None,
            district_model=self.model,
            start_name=name, end_name="OTHER SE1",
        )

    def test_wrong_borough_endpoint_fails_preflight(self):
        report = self._preflight(_entry(51.35, -0.05), "SOMEWHERE SE1")
        self.assertFalse(report.ok)
        self.assertTrue(any("wrong place" in r for r in report.reasons))
        self.assertIsNotNone(report.start_district_m)

    def test_in_district_endpoint_passes(self):
        report = self._preflight(_entry(51.503, -0.097), "SOMEWHERE SE1")
        self.assertTrue(report.ok)

    def test_endpoint_without_postcode_is_not_checked(self):
        report = self._preflight(_entry(51.35, -0.05), "SOMEWHERE")
        self.assertTrue(report.ok)
        self.assertIsNone(report.start_district_m)

    def test_district_mismatch_is_a_warning(self):
        report = self._preflight(
            _entry(51.503, -0.097, mismatch=True), "SOMEWHERE SE1")
        self.assertTrue(report.ok)
        self.assertTrue(any("disagrees" in w for w in report.warnings))

    def test_override_entries_are_exempt(self):
        # A curator-pinned coordinate is the explicit escape hatch — it also
        # covers Blue Book postcode typos where the *stated* district is wrong.
        pinned = GazetteerEntry(
            canonical_name="X", lat=51.35, lon=-0.05, snapped_node=1,
            snap_distance_m=0.0, source="override",
        )
        report = self._preflight(pinned, "SOMEWHERE SE1")
        self.assertTrue(report.ok)
        self.assertIsNone(report.start_district_m)


class PoiTableMismatchTests(unittest.TestCase):
    def test_single_candidate_wrong_district_is_flagged(self):
        table = _PoiTable(
            {"SHORTLANDS": {"lat": 51.4, "lon": 0.01, "postal_district": "BR2"}},
            "knowledge_poi",
        )
        hit = table.lookup("SHORTLANDS W6")
        self.assertIsNotNone(hit)
        self.assertTrue(hit.get("_district_mismatch"))

    def test_matching_district_is_not_flagged(self):
        table = _PoiTable(
            {"SHORTLANDS": {"lat": 51.49, "lon": -0.22, "postal_district": "W6"}},
            "knowledge_poi",
        )
        hit = table.lookup("SHORTLANDS W6")
        self.assertFalse(hit.get("_district_mismatch"))


if __name__ == "__main__":
    unittest.main()
