"""
Pipeline-level regression test.

Runs only when both the baseline snapshot and the latest qa_report.json
exist on disk. The normal developer loop is:

    krg bluebookdemo
    krg regression snapshot          # freeze baseline (one-off per version)
    # ... make changes, rerun pipeline ...
    pytest tests/test_qa_regression.py

Without a frozen baseline, the test skips — there is nothing to compare
against. Snapshots live at tests/golden/qa_baseline.json.
"""

import unittest
from pathlib import Path

from knowledge_run_generator.regression import (
    DEFAULT_BASELINE_PATH,
    DEFAULT_REPORT_PATH,
    diff,
    format_diff,
    load_snapshot,
    summarise,
)


class QARegressionTests(unittest.TestCase):
    def test_baseline_is_committed_and_usable(self):
        """A missing or empty baseline is a repo defect, not an environment one.

        Distinguishing the two matters: the report is a build artefact that CI
        legitimately doesn't have, but the baseline is committed, so its
        absence must fail rather than skip. An earlier version skipped on
        either, which meant a baseline of 320 empty fingerprints went unnoticed.
        """
        baseline_path = Path(DEFAULT_BASELINE_PATH)
        self.assertTrue(
            baseline_path.exists(),
            f"{baseline_path} is missing. Regenerate with `krg regression snapshot`.",
        )

        baseline = load_snapshot(baseline_path)
        self.assertGreater(baseline.total, 0, "baseline has no runs")

        # The fingerprints must actually carry geometry. They previously did
        # not — `fingerprint_run` read keys the QA writer never emitted — so
        # every route_hash was "" and the geometry check could never fire.
        hashed = sum(1 for fp in baseline.runs.values() if fp.route_hash)
        self.assertGreater(
            hashed, 0,
            "no baseline fingerprint carries a route_hash; the geometry-change "
            "check cannot fire. Regenerate the baseline from a current report.",
        )

    def test_no_regressions_vs_baseline(self):
        baseline_path = Path(DEFAULT_BASELINE_PATH)
        report_path = Path(DEFAULT_REPORT_PATH)
        if not report_path.exists():
            # The report is a build artefact requiring the OSM graph, so CI
            # genuinely cannot produce one. Promotion runs this same diff via
            # `krg regression diff --strict`, which is where it gates.
            self.skipTest(
                f"No {report_path} to compare. Run `krg generate runs` first."
            )

        baseline = load_snapshot(baseline_path)
        current = summarise(report_path)
        result = diff(baseline, current)

        if result.has_regressions:
            self.fail("Regression detected vs baseline:\n" + format_diff(result))


if __name__ == "__main__":
    unittest.main()
