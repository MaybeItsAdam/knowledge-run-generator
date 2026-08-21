"""Offline guards for the Points List name-to-category taxonomy.

Same charter as ``tests/test_pipeline_integrity.py``: no graph, no network, no
API tokens. The only input beyond the rules themselves is the Points List PDF
committed at the repo root, which is the source the extractor already reads.

Two layers:

* ``GoldenFileTests`` pins a curated set of names to exact answers, including
  the ones the rules currently get wrong. That file is the review surface: a
  rule change has to show up there as a line-by-line before/after.
* ``DistributionTests`` pins the shape of the whole corpus within a tolerance.
  A curated file of 80 names cannot notice a rule that quietly reclassifies
  four hundred; the histogram can.
"""

import importlib.util
import json
import unittest
from collections import Counter
from pathlib import Path

from knowledge_run_generator.poi_categories import infer_category

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = REPO_ROOT / "tests" / "golden" / "poi_categories.json"


def _golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _points_list_names() -> list[str]:
    """Every name the extractor pulls out of the committed Points List PDF.

    ``scripts/`` is not a package, so the extractor is loaded by file path.
    Only the PDF walk lives there; the rules under test are imported normally.
    """
    spec = importlib.util.spec_from_file_location(
        "extract_pois", REPO_ROOT / "scripts" / "extract_pois.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [record["name"] for record in module.extract(Path(module.DEFAULT_PDF))]


class GoldenFileTests(unittest.TestCase):
    """Exact answers for a curated set of names."""

    @classmethod
    def setUpClass(cls):
        cls.golden = _golden()

    def test_golden_file_is_well_formed(self):
        entries = self.golden["entries"]
        self.assertGreaterEqual(len(entries), 60)
        self.assertLessEqual(len(entries), 80)
        names = [e["name"] for e in entries]
        self.assertEqual(len(set(names)), len(names), "duplicate names in the golden file")
        for entry in entries:
            self.assertIsInstance(entry["expected_category"], str, entry["name"])

    def test_every_golden_name_classifies_as_recorded(self):
        wrong = [
            (e["name"], e["expected_category"], infer_category(e["name"]))
            for e in self.golden["entries"]
            if infer_category(e["name"]) != e["expected_category"]
        ]
        self.assertEqual(
            wrong, [],
            "categories moved without the golden file being updated:\n"
            + "\n".join(f"  {n}: {want} -> {got}" for n, want, got in wrong),
        )

    def test_every_emitted_category_has_a_representative(self):
        """The golden file has to cover the taxonomy, not just its awkward corners."""
        represented = {e["expected_category"] for e in self.golden["entries"]}
        missing = set(self.golden["distribution"]["counts"]) - represented
        self.assertEqual(missing, set(),
                         f"categories with no golden entry: {sorted(missing)}")


class DistributionTests(unittest.TestCase):
    """The shape of the whole corpus, held to a band rather than to the digit."""

    @classmethod
    def setUpClass(cls):
        cls.golden = _golden()
        try:
            cls.names = _points_list_names()
        except ImportError as exc:  # pdfplumber absent: nothing to measure
            raise unittest.SkipTest(f"cannot parse the Points List PDF: {exc}")
        cls.counts = Counter(infer_category(name) for name in cls.names)

    def _band(self, expected: int) -> int:
        spec = self.golden["distribution"]
        return max(spec["tolerance_floor"], round(expected * spec["tolerance_fraction"]))

    def test_the_points_list_still_yields_the_expected_number_of_names(self):
        spec = self.golden["distribution"]
        band = round(spec["total"] * spec["total_tolerance_fraction"])
        self.assertAlmostEqual(len(self.names), spec["total"], delta=band)

    def test_no_category_is_emitted_that_the_snapshot_does_not_know_about(self):
        unexpected = set(self.counts) - set(self.golden["distribution"]["counts"])
        self.assertEqual(
            unexpected, set(),
            f"new category values {sorted(unexpected)} appeared; add them to "
            f"{GOLDEN_PATH.name} in the same commit that introduces them",
        )

    def test_every_category_count_is_within_tolerance(self):
        drifted = []
        for category, expected in self.golden["distribution"]["counts"].items():
            actual = self.counts.get(category, 0)
            band = self._band(expected)
            if abs(actual - expected) > band:
                drifted.append(f"  {category}: {expected} -> {actual} (band +/-{band})")
        self.assertEqual(
            drifted, [],
            "category distribution moved beyond tolerance:\n" + "\n".join(drifted),
        )


if __name__ == "__main__":
    unittest.main()
