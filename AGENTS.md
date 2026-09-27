# AGENTS.md

## Scope
These instructions apply to the whole repository.

Before changing routing, validation or QA, read [ROADMAP.md](./ROADMAP.md) — it
holds the measured baseline, the staged plan to reach the Knowledge standard,
and the reasoning behind decisions that look wrong out of context (why the
regression diff gates at promotion rather than in CI; why geometry changes are
reported but non-gating; why both ordered metrics are tracked).

## Project Defaults
- Routing runs on the **taxi-legal graph** (`taxi` profile,
  `taxi_profile.py`): bus gates that admit taxis and taxi contraflows are
  open, `access=no` bus-only roads and busways are closed, and every edge
  touching a motor-vehicle `barrier` node (bollard, planter, bus trap, ...)
  is cut, which is how OSM maps LTN modal filters. The module docstring is
  the full record of tag decisions; change a rule there and in its test.
  Turn restrictions with `except=psv`/`taxi` do not bind the taxi profile.
- The taxi graph is cached as `london_taxi_v1.graphml` with a sidecar
  `london_taxi_v1.taxi_rules.json` (closed ways, destination-only ways,
  contraflows, blocking barriers). Delete both to rebuild: the 2026-09
  build took 19 minutes wall clock and 3.1 GB peak memory, almost all of it
  waiting on Overpass and writing the 179 MB graphml (98 s of CPU). The
  named-filter harvest used only to explain failures is cached separately
  as `london_filtered_streets_v2.json`.
- Override the profile with `KRG_GRAPH_NETWORK_TYPE` or `--network-type`:
  `taxi` (default), `drive`, `drive_service`.

## Route Source: Blue Book or Crow-Flies
- Every run ships `route_source`: `blue_book` when its Blue Book sequence
  passes every gate, else `crow_flies` with `route_source_reason` (plain
  English, no em dashes: "Hammersmith Bridge is closed to motor vehicles",
  "Modal filter on Braes Street (closed to motor traffic)", ...). The
  failed Blue Book attempt stays on the QA record under `blue_book`.
- Crow-flies is the Knowledge rule for a run the Blue Book can't give us:
  the legal taxi route closest to the straight line
  (`router.route_crow_flies`: length x road-class weight + lambda x the
  lateral offset integrated along the route, normalised by the run's
  length). Lambda and the weights were chosen on the Blue Book agreement
  study (`scripts/evaluate_crow_flies.py`; numbers in ROADMAP.md). Re-run
  the study before changing them.
- A crow-flies route faces the same hard gates (legal both ways, sane both
  ways) plus the taxi-legality gate (`TaxiRules.check_route`: no blocking
  barrier, no closed way, no access-only street passed through). So does a
  Blue Book route: a Blue Book route that fails taxi legality ships
  crow-flies.
- `scripts/check_taxi_legality.py <runPoints.json> --network-type drive`
  checks an older data set against the taxi rules.

## Run Names and Endpoints
- TfL Annex B (`blue_book_demo/tfl_blue_book_annex_b.txt`, vendored with its
  sha256) is the run list: numbers, names, districts and order. The Anki
  export only supplies the street directions; `annex_b.py` refuses a build
  whose run ids differ. A TfL name that the data knows under another spelling
  resolves through `annex_b_geocode_names.json` (each entry says why); the run
  still displays TfL's name.
- Endpoint resolution order: curated overrides, then (stations) OSM stations
  before the Points List, then the Points List, then the street tier. A name
  that *is* a street resolves to the street (the stretch nearest its district).
- Area endpoints (stations, parks, museums) set down at a way in from
  `constants/osm_access.json` (`krg osm-access`), not the road nearest their
  centre.
- Build guards: two different names on one coordinate, or names that differ
  from Annex B, fail `krg generate all` and `promote_to_app.py`.
- `poi_overrides.json` is for genuinely ambiguous places only. Every new entry
  carries `note` and `source`.

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
- After changing anything that affects routing, regenerate and re-diff:
  `krg generate runs && krg regression diff`. The per-run diff only gates at
  promotion time (`scripts/promote_to_app.py`) — CI cannot run it, because it
  needs a `qa_report.json` and therefore the OSM graph.
- When a change moves the baseline deliberately, refresh it in the *same*
  commit (`krg regression snapshot`) and say in the message what moved and why.

## Judging Run Quality
- `passed` means legal + direct + sane detours. It does **not** mean the route
  is the Blue Book run — a run can be `passed` while traversing none of its
  prescribed streets. Never quote it as a correctness figure on its own.
- Blue Book fidelity is `ordered_coverage` (track this) and `strict_ordered`
  (triage with this). Both are in `qa_report.json` and surfaced by `krg qa`.
- `passed` also requires both directions to pass the hard sanity gate
  (`validator.check_route_sanity`): no route over 3x the straight line or 2x
  the shortest legal route (each with >= 2 km excess), and none straying more
  than 500 m past the six-mile radius (or past an endpoint that is itself
  outside it). The reverse must also be legal. This is what the independent
  app verifier kept catching and our gate did not (run 150: 35.9 km for a
  2.7 km run, `passed: true`).
- STREET constraints are **localised** (`locality.localise_constraints`) to
  the instance of the name near the run. Unlocalised, a constraint was met by
  any namesake in London, so an unreachable local street sent the search
  across the city. A name with no instance near the run is recorded in
  `remote_constraints` and dropped as a gap.
- Laps of >= 1 km (the same directed edge driven twice) are repaired by
  demoting the constraint that forced them (`loop_demotions`); forward, that
  is a hard gap and fails the run. The reverse run (not prescribed) also
  repairs leg loops and falls back to the shortest legal route when the
  reversed sequence stays over budget (`rev_fallback`).
- Current baseline (`krg regression snapshot`, 320 runs, taxi graph,
  2026-09): 320 `passed`, of which **294 under their Blue Book sequence** and
  **26 `crow_flies`** (see "Route Source" above; the failed Blue Book
  attempt is on each record under `blue_book`). 293/320 runs fully in Blue
  Book order; mean `ordered_coverage` 0.954 and `strict_ordered` 0.926 over
  all 320 (the crow-flies runs pull these down by design: they are measured
  against the sequence they no longer follow). 0 legality failures, 0
  sanity failures, 0 taxi-legality failures, either direction. Routing
  mode: 293 `ordered_strict` / 1 `ordered_relaxed` / 26 `crow_flies`.
- 5 preflight warnings (endpoint snapped > 50 m: runs 24, 90, 121, 124,
  150) are the taxi graph at work: each endpoint's old snap point is on a
  way now closed or access-only (Albert Bridge is `access=no` in OSM since
  2026, Station Approach SW12 is `motor_vehicle=destination`, a gate at
  Manor Fields), so the cab sets down at the nearest taxi-legal point.
  `promote_to_app.py --min-passed` counts Blue Book passes only.
- Step text is not a fidelity metric. `ordered_coverage` / `strict_ordered` are
  computed from the graph edges the route traverses (`_route_edge_names`), not
  from `route.steps`, so changing how the call is worded cannot move them.
- `unreachable_legs` / `truncated_legs` are always 0 and mean nothing yet; the
  router's metadata is dropped before it reaches the QA record.
- Prefer `krg generate runs --fresh` when judging the corpus. A plain
  `generate runs` resumes, keeping runs produced by older code, which is how a
  build ended up quoting 299 fully-ordered when a clean rebuild scored 303.
