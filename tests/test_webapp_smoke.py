"""
Graph-free webapp smoke tests.

Covers the routes that don't require the cached OSM graph:
  - GET /                       (template renders)
  - GET /api/bluebook/runs      (reads constants/runPoints.json)
  - GET /api/user-runs          (reads/initialises a user-store JSON file)
  - POST /api/folders           (folder CRUD)
  - PATCH /api/folders/<id>
  - DELETE /api/folders/<id>
  - DELETE /api/user-runs/<id>  (404 path; doesn't touch routing)

Endpoints that need the OSM graph (`/api/run`, PUT `/api/user-runs/<id>`)
are intentionally not exercised here — those are covered by the pipeline
regression test instead.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from knowledge_run_generator import webapp


class WebappSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.user_runs_path = Path(self.tmp_dir.name) / "user_runs.json"
        webapp.app.config["USER_RUNS_FILE"] = self.user_runs_path
        webapp._USER_STORE_CACHE = None
        self.client = webapp.app.test_client()

    def tearDown(self):
        self.tmp_dir.cleanup()
        webapp._USER_STORE_CACHE = None

    def test_index_renders_with_expected_anchors(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode()
        # Anchors that prove the rebuilt UI shipped (not the older floating panel).
        for anchor in (
            "modeBanner",
            "cancelEditButton",
            'data-action="delete-user"',
            'data-action="rename-folder"',
            'data-action="delete-folder"',
            "detailsToggle",
        ):
            self.assertIn(anchor, body, f"missing anchor {anchor!r} in rendered HTML")

        # Hard-fail if the obsolete UI returns.
        for stale in ('"settings-pane"', "load-blue", "currentRunName"):
            self.assertNotIn(stale, body, f"stale UI artefact {stale!r} reappeared")

    def test_inline_script_has_no_obvious_python_escape_corruption(self):
        body = self.client.get("/").data.decode()
        scripts = re.findall(r"<script>([\s\S]*?)</script>", body)
        self.assertTrue(scripts, "no inline <script> block found")
        js = scripts[-1]
        # The Python triple-quoted template can eat \" inside JS string literals.
        # Two adjacent unescaped quotes followed by a `+` is the signature of that
        # bug (e.g. "Delete folder \"" + label + "\"?" → "Delete folder "" + ...).
        self.assertNotRegex(
            js,
            r'""\s*\+',
            "JS appears to contain `\"\" +` — Python likely consumed a \\\" escape.",
        )

    def test_bluebook_runs_endpoint_returns_summaries(self):
        response = self.client.get("/api/bluebook/runs")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("runs", payload)
        self.assertIsInstance(payload["runs"], list)

    def test_user_runs_endpoint_starts_empty(self):
        response = self.client.get("/api/user-runs")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["runs"], [])
        self.assertEqual(payload["folders"], [])

    def test_folder_crud_round_trip(self):
        # Create
        r = self.client.post("/api/folders", json={"name": "Evening Runs"})
        self.assertEqual(r.status_code, 201, r.data)
        folder = r.get_json()["folder"]
        self.assertEqual(folder["name"], "Evening Runs")
        folder_id = folder["id"]

        # Rename
        r = self.client.patch(
            f"/api/folders/{folder_id}", json={"name": "Late Runs"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["folder"]["name"], "Late Runs")

        # Empty rename rejected
        r = self.client.patch(f"/api/folders/{folder_id}", json={"name": "  "})
        self.assertEqual(r.status_code, 400)

        # Unknown folder
        r = self.client.patch("/api/folders/missing", json={"name": "X"})
        self.assertEqual(r.status_code, 404)
        r = self.client.delete("/api/folders/missing")
        self.assertEqual(r.status_code, 404)

        # Delete
        r = self.client.delete(f"/api/folders/{folder_id}")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/user-runs")
        self.assertEqual(r.get_json()["folders"], [])

    def test_delete_user_run_404_when_missing(self):
        r = self.client.delete("/api/user-runs/9999")
        self.assertEqual(r.status_code, 404)

    def test_delete_folder_reparents_runs_to_root(self):
        # Create a folder, hand-insert a run in it, then delete the folder.
        r = self.client.post("/api/folders", json={"name": "Tmp"})
        folder_id = r.get_json()["folder"]["id"]

        store = webapp._ensure_user_store_loaded()
        store["runs"].insert(
            0,
            {
                "id": 7,
                "title": "fake",
                "source": "user",
                "folder_id": folder_id,
                "start": {"name": "A", "coordinates": [0, 0]},
                "end": {"name": "B", "coordinates": [0, 0]},
                "route": {
                    "geometry": {"type": "LineString", "coordinates": []},
                    "distance": 0,
                    "duration": 0,
                    "steps": [],
                },
            },
        )
        webapp._save_user_store()

        r = self.client.delete(f"/api/folders/{folder_id}")
        self.assertEqual(r.status_code, 200)

        payload = self.client.get("/api/user-runs").get_json()
        run = next(x for x in payload["runs"] if x["id"] == 7)
        self.assertIsNone(run["folder_id"])

        # Cleanup
        r = self.client.delete("/api/user-runs/7")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
