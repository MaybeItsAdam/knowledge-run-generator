"""
Offline guards for the data-generation contract.

Everything here runs without a graph, network access or API tokens: these are
the invariants that decide whether ``krg generate all`` can produce all 320
canonical runs and the full Knowledge Points List in the first place.
"""

import inspect
import json
import tempfile
import unittest
from pathlib import Path

from knowledge_run_generator.blue_book_demo.run_pipeline import (
    _save_runs,
    parse_intermediary_file,
    process_runs,
)
from knowledge_run_generator.corrector import correct_and_validate
from knowledge_run_generator.validator import ValidationResult

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RUNS = 320


class SourceDataTests(unittest.TestCase):
    """The canonical inputs must be present and complete in any checkout."""

    def test_points_list_pdf_is_the_extractor_default(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "extract_pois", REPO_ROOT / "scripts" / "extract_pois.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(
            Path(module.DEFAULT_PDF).exists(),
            f"extract_pois.DEFAULT_PDF must resolve inside the checkout, got {module.DEFAULT_PDF}",
        )

    def test_all_320_runs_parse_with_street_sequences(self):
        path = (
            REPO_ROOT
            / "knowledge_run_generator"
            / "blue_book_demo"
            / "blue_book_runs_intermediary.txt"
        )
        titles, streets = parse_intermediary_file(path)
        self.assertEqual(sorted(titles), list(range(1, EXPECTED_RUNS + 1)))
        empty = [rid for rid, seq in streets.items() if not seq]
        self.assertEqual(empty, [], f"runs with no street sequence: {empty}")


class GraphProfileTests(unittest.TestCase):
    def test_process_runs_defers_network_type_to_load_graph(self):
        # A concrete default here silently overrides KRG_GRAPH_NETWORK_TYPE for
        # every CLI caller, which is the documented way to switch profiles.
        default = inspect.signature(process_runs).parameters["network_type"].default
        self.assertIsNone(default)


class SaveOrderingTests(unittest.TestCase):
    def test_runs_are_written_in_id_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "runPoints.json"
            _save_runs(out, [{"id": 9}, {"id": 2}, {"id": 40}])
            self.assertEqual([r["id"] for r in json.loads(out.read_text())], [2, 9, 40])


class ExemptedTurnsTests(unittest.TestCase):
    def test_exempted_turns_reach_the_validator(self):
        seen = {}

        def route_fn(G, o, d, wps):
            return [1, 2, 3], {"total_distance": 100.0}

        def validate_fn(G, nodes, o, d, turns, streets, cfg, wps, exempted_turns=None):
            seen["exempted_turns"] = exempted_turns
            return ValidationResult(passed=True)

        exempted = {(1, 2, 3)}
        correct_and_validate(
            None, 1, 3, [], [], set(), {},
            route_fn=route_fn,
            validate_fn=validate_fn,
            exempted_turns=exempted,
        )
        self.assertEqual(seen.get("exempted_turns"), exempted)


class EnrichmentInputTests(unittest.TestCase):
    def test_missing_inputs_reports_absent_reference_files(self):
        from knowledge_run_generator.poi_enrichment import missing_inputs

        with tempfile.TemporaryDirectory() as tmp:
            boroughs = Path(tmp) / "london_boroughs.geojson"
            sectors = Path(tmp) / "yellow_badge_sectors.json"
            self.assertEqual(
                [p.name for p in missing_inputs(boroughs, sectors)],
                ["london_boroughs.geojson", "yellow_badge_sectors.json"],
            )
            boroughs.write_text('{"features": []}')
            sectors.write_text("{}")
            self.assertEqual(missing_inputs(boroughs, sectors), [])


if __name__ == "__main__":
    unittest.main()
