# AGENTS.md

## Scope
These instructions apply to the whole repository.

## Project Defaults
- Routing is tuned for cab-legal behavior on a strict `drive` graph by default.
- Override graph profile with `KRG_GRAPH_NETWORK_TYPE` or `--network-type` in the Blue Book pipeline.
  - Supported values: `drive`, `drive_service`.

## Web App Expectations
- Sidebar is file-hierarchy-first.
- Top bar shows selected run name.
- Start/end editor sits directly below the run name and saves runs.
- Selecting a run from the hierarchy populates start/end fields.
- Operational metadata (Blue Book load status, user storage path, run details) lives in the settings pane.

## User Run Storage
- Default user run store:
  - macOS: `~/Library/Application Support/knowledge-run-generator/user_runs.json`
  - Linux/other: `~/.local/share/knowledge-run-generator/user_runs.json`
- Override path with `KRG_USER_RUNS_FILE` or `--user-runs-file`.

## Regression Discipline
- Keep router behavior protected with tests in `tests/test_router_regression.py`.
- Do not rely on one-off run-specific patches when a reusable routing heuristic can solve the class of issue.
