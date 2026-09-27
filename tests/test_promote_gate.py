"""The promotion QA gate with crow-flies runs in the build.

A crow-flies run passes QA, so counting ``passed`` would let the Blue Book
pass count slide unseen: ``--min-passed`` is a floor on Blue Book passes.
Every crow-flies run must say why and be taxi-legal.
"""

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("promote_to_app", ROOT / "scripts" / "promote_to_app.py")
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)


def _qa(bb_pass=3, crow=1, crow_reason="Hammersmith Bridge is closed to motor vehicles",
        crow_legal=True):
    qa = {"_provenance": {"osm_pois": 10}}
    for i in range(bb_pass):
        qa[str(i + 1)] = {"status": "ok", "passed": True, "route_source": "blue_book",
                          "taxi_legal": True, "ordered_coverage": 1.0, "strict_ordered": 1.0}
    for j in range(crow):
        qa[str(100 + j)] = {"status": "ok", "passed": True, "route_source": "crow_flies",
                            "route_source_reason": crow_reason, "taxi_legal": crow_legal,
                            "ordered_coverage": 0.4, "strict_ordered": 0.3}
    return qa


class PromoteGateTests(unittest.TestCase):
    def run_gate(self, qa, min_passed):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "qa_report.json"
            path.write_text(json.dumps(qa))
            old = promote.QA_SRC
            promote.QA_SRC = path
            try:
                out = io.StringIO()
                with redirect_stdout(out):
                    ok = promote.validate_qa(min_passed)
                return ok, out.getvalue()
            finally:
                promote.QA_SRC = old

    def test_crow_flies_runs_do_not_count_towards_the_floor(self):
        ok, out = self.run_gate(_qa(bb_pass=3, crow=2), min_passed=4)
        self.assertFalse(ok)
        self.assertIn("3 blue_book passed", out)

    def test_floor_met_by_blue_book_passes(self):
        ok, _ = self.run_gate(_qa(bb_pass=3, crow=2), min_passed=3)
        self.assertTrue(ok)

    def test_crow_flies_run_needs_a_reason(self):
        ok, out = self.run_gate(_qa(crow_reason=None), min_passed=1)
        self.assertFalse(ok)
        self.assertIn("without a reason", out)

    def test_crow_flies_run_must_be_taxi_legal(self):
        ok, _ = self.run_gate(_qa(crow_legal=False), min_passed=1)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
