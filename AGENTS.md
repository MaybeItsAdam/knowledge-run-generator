# AGENTS.md

## Scope
These instructions apply to the whole repository.

Before changing routing, validation or QA, read [ROADMAP.md](./ROADMAP.md) — it
holds the measured baseline, the staged plan to reach the Knowledge standard,
and the reasoning behind decisions that look wrong out of context (why the
regression diff gates at promotion rather than in CI; why geometry changes are
reported but non-gating; why both ordered metrics are tracked).

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
- Current baseline (`krg regression snapshot`, 320 runs, fresh graph): 281
  `passed`, mean `ordered_coverage` 0.991, mean `strict_ordered` 0.972,
  292/320 runs fully in Blue Book order, **0 legality failures** (either
  direction), 17 sanity failures, 15 preflight failures. Routing mode splits
  292 `ordered_strict` / 27 `ordered_relaxed` / 1 `shortest_path`.
- What is left: the 17 sanity failures are mostly endpoints kilometres from
  the start of their Blue Book sequence (150, 156, 166, 251, 300, 305, 319,
  56, 72, 14) and Blue Book runs that cross just outside the radius (37, 68,
  109, 189 — Chiswick Bridge); 27 runs carry a loop demotion (Blue Book
  order undrivable without a lap on today's OSM); directness (`is_direct:
  false`, triage only) is 76.
- Step text is not a fidelity metric. `ordered_coverage` / `strict_ordered` are
  computed from the graph edges the route traverses (`_route_edge_names`), not
  from `route.steps`, so changing how the call is worded cannot move them.
- `unreachable_legs` / `truncated_legs` are always 0 and mean nothing yet; the
  router's metadata is dropped before it reaches the QA record.
- Prefer `krg generate runs --fresh` when judging the corpus. A plain
  `generate runs` resumes, keeping runs produced by older code, which is how a
  build ended up quoting 299 fully-ordered when a clean rebuild scored 303.
