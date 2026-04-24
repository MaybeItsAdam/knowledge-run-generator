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
    def test_no_regressions_vs_baseline(self):
        baseline_path = Path(DEFAULT_BASELINE_PATH)
        report_path = Path(DEFAULT_REPORT_PATH)
        if not baseline_path.exists() or not report_path.exists():
            self.skipTest(
                f"Need both {baseline_path} and {report_path}. "
                "Run `krg bluebookdemo` then `krg regression snapshot`."
            )

        baseline = load_snapshot(baseline_path)
        current = summarise(report_path)
        result = diff(baseline, current)

        if result.has_regressions:
            self.fail("Regression detected vs baseline:\n" + format_diff(result))


if __name__ == "__main__":
    unittest.main()
