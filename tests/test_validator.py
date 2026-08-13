"""Unit tests for the validator's route checks.

These checks decide whether a generated run is acceptable, and until now none
of them had a test. The ordered-traversal check is the one that encodes the
Knowledge standard, so it gets the most coverage here.

Graphs are built by hand rather than loaded from a fixture: the checks only
need edge ``name`` tags and node coordinates, and a hand-built graph makes the
street layout under test readable in the test itself.
"""

import unittest

import networkx as nx

from knowledge_run_generator.regression import (
    Diff,
    RunFingerprint,
    Snapshot,
    diff,
    fingerprint_run,
    hash_nodes,
)
from knowledge_run_generator.validator import (
    check_street_coverage,
    check_street_order,
    check_turn_legality,
)


def _linear_graph(street_names, start_lat=51.5, start_lon=-0.1, step=0.002):
    """A straight chain of nodes, one edge per entry in ``street_names``.

    ``street_names`` entries may be a string or a dict of edge attributes, so a
    test can attach ``ref``/``alt_name`` or leave an edge unnamed.
    """
    G = nx.MultiDiGraph()
    for i in range(len(street_names) + 1):
        G.add_node(i, x=start_lon + i * step, y=start_lat)
    for i, entry in enumerate(street_names):
        attrs = {"name": entry} if isinstance(entry, str) else dict(entry)
        attrs.setdefault("length", 100.0)
        G.add_edge(i, i + 1, **attrs)
        G.add_edge(i + 1, i, **attrs)
    return G


class StreetOrderTests(unittest.TestCase):
    def test_route_following_the_sequence_scores_one(self):
        G = _linear_graph(["Green Lanes", "Brownswood Road", "Blackstock Road"])
        ok, m = check_street_order(
            G, [0, 1, 2, 3], ["GREEN LANES", "BROWNSWOOD ROAD", "BLACKSTOCK ROAD"]
        )
        self.assertTrue(ok)
        self.assertEqual(m["ordered_coverage"], 1.0)
        self.assertEqual(m["strict_ordered"], 1.0)
        self.assertEqual(m["missing"], [])
        self.assertIsNone(m["first_gap"])

    def test_right_streets_wrong_order_fails_ordered_but_passes_coverage(self):
        """The distinction this whole check exists to draw.

        The route touches every prescribed street, so the unordered coverage
        check is satisfied; it drives them in the wrong sequence, so it is not
        the Blue Book run.
        """
        G = _linear_graph(["Green Lanes", "Brownswood Road", "Blackstock Road"])
        expected = ["BLACKSTOCK ROAD", "BROWNSWOOD ROAD", "GREEN LANES"]

        covered, cm = check_street_coverage(G, [0, 1, 2, 3], expected)
        self.assertTrue(covered)
        self.assertEqual(cm["coverage"], 1.0)

        ok, m = check_street_order(G, [0, 1, 2, 3], expected)
        self.assertFalse(ok)
        # Only one of the three can be picked up by an in-order walk.
        self.assertEqual(m["matched"], 1)
        self.assertEqual(m["missing"], [])

    def test_missing_street_costs_one_place_not_the_whole_tail(self):
        """LCS degrades smoothly; the greedy walk cliff-edges.

        Both numbers are reported because the difference between them is the
        diagnosis: a single absent street, or genuine disorder.
        """
        G = _linear_graph(["Green Lanes", "Blackstock Road", "Highbury Park"])
        expected = [
            "GREEN LANES",
            "BROWNSWOOD ROAD",   # absent from the route
            "BLACKSTOCK ROAD",
            "HIGHBURY PARK",
        ]
        ok, m = check_street_order(G, [0, 1, 2, 3], expected)

        self.assertFalse(ok)
        self.assertEqual(m["matched"], 3)
        self.assertEqual(m["ordered_coverage"], 0.75)
        # The greedy walk stalls on the missing street and loses everything
        # after it, so it sees only GREEN LANES.
        self.assertEqual(m["strict_ordered"], 0.25)
        self.assertEqual(m["first_gap"], "BROWNSWOOD ROAD")
        self.assertEqual(m["missing"], ["BROWNSWOOD ROAD"])

    def test_matches_secondary_names_on_an_edge(self):
        """`name` alone would be a false miss on a dual-tagged way.

        OSM commonly carries ["Marylebone Road", "A501"]; the Blue Book may
        name either one.
        """
        G = _linear_graph([
            {"name": ["Marylebone Road", "A501"]},
            {"name": "Euston Road", "ref": "A501"},
        ])
        ok, m = check_street_order(G, [0, 1, 2], ["A501", "EUSTON ROAD"])
        self.assertTrue(ok)
        self.assertEqual(m["ordered_coverage"], 1.0)

    def test_unnamed_edges_do_not_break_the_sequence(self):
        """Slip roads and unnamed connectors are not gaps in the run."""
        G = _linear_graph([
            "Green Lanes",
            {"highway": "residential"},   # unnamed connector
            "Blackstock Road",
        ])
        ok, m = check_street_order(
            G, [0, 1, 2, 3], ["GREEN LANES", "BLACKSTOCK ROAD"]
        )
        self.assertTrue(ok)
        self.assertEqual(m["ordered_coverage"], 1.0)

    def test_consecutive_duplicates_count_once(self):
        G = _linear_graph(["Green Lanes", "Blackstock Road"])
        ok, m = check_street_order(
            G, [0, 1, 2], ["GREEN LANES", "GREEN LANES", "BLACKSTOCK ROAD"]
        )
        self.assertTrue(ok)
        self.assertEqual(m["expected"], 2)

    def test_non_consecutive_repeat_must_be_traversed_twice(self):
        """"STRAND, ALDWYCH, STRAND" is a real Blue Book shape.

        Only an ordered check can express it — a set-based one cannot tell the
        difference between driving Strand once and driving it twice.
        """
        G = _linear_graph(["Strand", "Aldwych", "Strand", "Lancaster Place"])
        expected = ["STRAND", "ALDWYCH", "STRAND", "LANCASTER PLACE"]

        ok, m = check_street_order(G, [0, 1, 2, 3, 4], expected)
        self.assertTrue(ok)
        self.assertEqual(m["expected"], 4)

        # A route that drives Strand only once cannot satisfy it.
        short = _linear_graph(["Strand", "Aldwych", "Lancaster Place"])
        ok2, m2 = check_street_order(short, [0, 1, 2, 3], expected)
        self.assertFalse(ok2)
        self.assertEqual(m2["matched"], 3)

    def test_substring_names_do_not_match(self):
        """`check_street_coverage` credits this; the ordered check must not.

        Its bidirectional substring test lets a short name claim a traversal of
        a longer one, which inflates the reported figure.
        """
        G = _linear_graph(["High Street Kensington"])
        covered, cm = check_street_coverage(G, [0, 1], ["HIGH STREET"])
        self.assertTrue(covered)
        self.assertEqual(cm["coverage"], 1.0)

        ok, m = check_street_order(G, [0, 1], ["HIGH STREET"])
        self.assertFalse(ok)
        self.assertEqual(m["matched"], 0)

    def test_empty_expectation_is_vacuously_satisfied(self):
        G = _linear_graph(["Green Lanes"])
        ok, m = check_street_order(G, [0, 1], [])
        self.assertTrue(ok)
        self.assertEqual(m["ordered_coverage"], 1.0)
        self.assertEqual(m["expected"], 0)

    def test_min_coverage_is_configurable(self):
        G = _linear_graph(["Green Lanes", "Blackstock Road"])
        expected = ["GREEN LANES", "BROWNSWOOD ROAD", "BLACKSTOCK ROAD"]
        self.assertFalse(check_street_order(G, [0, 1, 2], expected)[0])
        self.assertTrue(
            check_street_order(G, [0, 1, 2], expected, min_coverage=0.6)[0]
        )


class TurnLegalityTests(unittest.TestCase):
    def test_prohibited_triple_is_caught(self):
        legal, violations = check_turn_legality([1, 2, 3], {(1, 2, 3)})
        self.assertFalse(legal)
        self.assertEqual(len(violations), 1)

    def test_same_nodes_in_a_different_order_are_legal(self):
        legal, _ = check_turn_legality([3, 2, 1], {(1, 2, 3)})
        self.assertTrue(legal)

    def test_exempted_turn_is_allowed(self):
        legal, violations = check_turn_legality(
            [1, 2, 3], {(1, 2, 3)}, exempted_turns={(1, 2, 3)}
        )
        self.assertTrue(legal)
        self.assertEqual(violations, [])

    def test_route_too_short_to_turn_is_legal(self):
        legal, _ = check_turn_legality([1, 2], {(1, 2, 3)})
        self.assertTrue(legal)


class FingerprintTests(unittest.TestCase):
    """The fingerprint used to read keys the QA writer never emitted, so every
    baseline entry carried an empty hash and a zero node count."""

    def _qa(self, **overrides):
        base = {
            "preflight_ok": True,
            "is_direct": True,
            "legal": True,
            "passed": True,
            "ratio": 1.2,
            "fwd_distance_m": 4321.0,
            "node_count": 57,
            "route_hash": "abc123def456",
            "ordered_coverage": 0.9,
            "strict_ordered": 0.7,
            "street_coverage": 0.95,
        }
        base.update(overrides)
        return base

    def test_reads_the_keys_the_pipeline_actually_writes(self):
        fp = fingerprint_run(self._qa())
        self.assertEqual(fp.distance_m, 4321.0)
        self.assertEqual(fp.node_count, 57)
        self.assertEqual(fp.route_hash, "abc123def456")
        self.assertEqual(fp.ordered_coverage, 0.9)

    def test_hash_nodes_is_stable_and_order_sensitive(self):
        self.assertEqual(hash_nodes([1, 2, 3]), hash_nodes([1, 2, 3]))
        self.assertNotEqual(hash_nodes([1, 2, 3]), hash_nodes([3, 2, 1]))
        self.assertEqual(hash_nodes([]), "")

    def _snapshot(self, runs, **agg):
        def _mean(attr):
            vals = [getattr(v, attr) for v in runs.values()
                    if getattr(v, attr) is not None]
            return round(sum(vals) / len(vals), 4) if vals else 0.0

        params = {
            "total": len(runs),
            "passed": sum(1 for v in runs.values() if v.passed),
            "preflight_fails": 0,
            "directness_fails": 0,
            "legality_fails": 0,
            "fully_ordered": sum(
                1 for v in runs.values()
                if v.ordered_coverage is not None and v.ordered_coverage >= 1.0
            ),
            "mean_ordered": _mean("ordered_coverage"),
            "mean_strict": _mean("strict_ordered"),
            "runs": runs,
        }
        params.update(agg)
        return Snapshot(**params)

    def test_fidelity_drop_is_a_regression_even_when_passed_holds(self):
        """The failure mode that made all of this necessary: a route can stay
        legal and direct while ceasing to be the Blue Book run."""
        base = self._snapshot({"1": fingerprint_run(self._qa(ordered_coverage=1.0))})
        curr = self._snapshot({"1": fingerprint_run(self._qa(ordered_coverage=0.4))})
        d = diff(base, curr)
        self.assertTrue(d.has_regressions)
        self.assertEqual(len(d.fidelity_regressions), 1)
        self.assertEqual(d.newly_failing, [])

    def test_rounding_noise_is_not_a_regression(self):
        """Sub-street jitter on one run must not trip the gate.

        Sized like the real corpus: with 320 runs, one moving by 0.005 shifts
        the mean by ~0.00002, far under the aggregate threshold. A one-run
        corpus would move both by the same amount and prove nothing.
        """
        def corpus(first):
            runs = {"1": fingerprint_run(self._qa(ordered_coverage=first))}
            runs.update({
                str(i): fingerprint_run(self._qa(ordered_coverage=0.9))
                for i in range(2, 321)
            })
            return self._snapshot(runs)

        self.assertFalse(diff(corpus(0.900), corpus(0.895)).has_regressions)

    def test_a_single_run_losing_a_street_does_trip_the_gate(self):
        """The other side of the epsilon: real movement must not be absorbed.

        A street dropping out of a ~15-street sequence is ~0.067 — comfortably
        above the per-run threshold, and the smallest change worth catching.
        """
        def corpus(first):
            runs = {"1": fingerprint_run(self._qa(ordered_coverage=first))}
            runs.update({
                str(i): fingerprint_run(self._qa(ordered_coverage=0.9))
                for i in range(2, 321)
            })
            return self._snapshot(runs)

        d = diff(corpus(0.933), corpus(0.867))
        self.assertTrue(d.has_regressions)
        self.assertEqual(len(d.fidelity_regressions), 1)

    def test_strict_ordered_drop_is_caught_on_its_own(self):
        """Gaps moving earlier in the sequence is a distinct failure.

        `ordered_coverage` is insensitive to *where* a gap falls, so a change
        that shifts gaps toward the start of the run leaves it unmoved while
        `strict_ordered` collapses. Tracking only the former would miss it.
        """
        base = self._snapshot({"1": fingerprint_run(self._qa(strict_ordered=0.9))})
        curr = self._snapshot({"1": fingerprint_run(self._qa(strict_ordered=0.2))})
        d = diff(base, curr)

        self.assertTrue(d.has_regressions)
        self.assertEqual(len(d.fidelity_regressions), 1)
        self.assertIn("strict", d.fidelity_regressions[0])
        # The forgiving metric was untouched, so it must not be implicated.
        self.assertNotIn("ordered", d.fidelity_regressions[0].split("strict")[0])

    def test_both_metrics_are_named_when_both_drop(self):
        base = self._snapshot({
            "1": fingerprint_run(self._qa(ordered_coverage=1.0, strict_ordered=1.0))
        })
        curr = self._snapshot({
            "1": fingerprint_run(self._qa(ordered_coverage=0.5, strict_ordered=0.3))
        })
        d = diff(base, curr)
        self.assertEqual(len(d.fidelity_regressions), 2)

    def test_corpus_mean_regressions_are_tracked(self):
        base = self._snapshot({
            str(i): fingerprint_run(self._qa(strict_ordered=0.8)) for i in range(10)
        })
        curr = self._snapshot({
            str(i): fingerprint_run(self._qa(strict_ordered=0.5)) for i in range(10)
        })
        d = diff(base, curr)
        self.assertTrue(any("mean_strict" in x for x in d.aggregate_regressions))

    def test_mean_improvement_is_not_a_regression(self):
        base = self._snapshot({
            str(i): fingerprint_run(self._qa(strict_ordered=0.5)) for i in range(10)
        })
        curr = self._snapshot({
            str(i): fingerprint_run(self._qa(strict_ordered=0.9)) for i in range(10)
        })
        d = diff(base, curr)
        self.assertTrue(any("mean_strict" in x for x in d.aggregate_improvements))
        self.assertEqual(d.aggregate_regressions, [])

    def test_a_run_vanishing_from_the_report_is_a_regression(self):
        base = self._snapshot({
            "1": fingerprint_run(self._qa()),
            "2": fingerprint_run(self._qa()),
        })
        curr = self._snapshot({"1": fingerprint_run(self._qa())})
        d = diff(base, curr)
        self.assertTrue(d.has_regressions)
        self.assertEqual(d.disappeared, ["2"])

    def test_geometry_change_is_detected(self):
        base = self._snapshot({"1": fingerprint_run(self._qa(route_hash="aaa"))})
        curr = self._snapshot({"1": fingerprint_run(self._qa(route_hash="bbb"))})
        self.assertEqual(diff(base, curr).geometry_changed, ["1"])

    def test_older_baseline_without_fidelity_fields_still_loads(self):
        old = {
            "total": 1, "passed": 1, "preflight_fails": 0,
            "directness_fails": 0, "legality_fails": 0,
            "runs": {"1": {"preflight_ok": True, "is_direct": True, "legal": True,
                           "passed": True, "ratio": 1.1}},
        }
        snap = Snapshot.from_json(old)
        self.assertEqual(snap.fully_ordered, 0)
        self.assertIsNone(snap.runs["1"].ordered_coverage)


if __name__ == "__main__":
    unittest.main()
