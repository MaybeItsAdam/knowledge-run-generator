# Knowledge Run Generator

A Python engine for creating Knowledge of London "Runs" — validated, shortest-distance routes between points of interest in London.

## Features
- **Semantic Routing**: A custom Dijkstra implementation that applies 90% routing discounts to "known" street sequences.
- **Sequential Dijkstra**: Core logic that prevents "tractor beam" backtracks by requiring forward motion through sequence waypoints to unlock discounts.
- **Roundabout Aggregation**: Automatically identifies topological roundabout rings and treats them as single waypoint sets.
- **Automatic Legality**: Validates routes against turn restrictions and provides deterministic "Calls" (navigation instructions).
- **Cab-Mode Cost Model**: Penalizes service/slip-link detours, immediate U-turns, and backward stage progress to reduce divided-carriageway loop artifacts.

## CLI

Primary entry points (installed by the package):

```bash
krg --help            # short form
knowledge-run --help  # long form, same group
```

Subcommands:

| Command | Purpose |
|---------|---------|
| `krg generate all` | One-shot rebuild of every dataset the app consumes (POIs → OSM gazetteer → 320 runs → promote into the-blue-app). |
| `krg web` | Launch the local map-first web app (recommended). |
| `krg route ORIGIN DESTINATION` | Generate one run via the session API and print The Call. |
| `krg run ORIGIN DESTINATION` | Legacy run command (landmarks + caller pipeline). |
| `krg bluebookdemo [N]` | Run the Blue Book pipeline for the first N (or all 320) runs. |
| `krg resolve-street NAME` | Canonicalise a street name via the alias index. |
| `krg resolve-place NAME` | Look up a POI and show where it snaps to on the graph. |
| `krg qa` | Summarise the most recent `qa_report.json`. |
| `krg diagnose RUN_ID` | Walk a run's node sequence edge-by-edge to triage routing artefacts. |
| `krg osm-pois` | Harvest a gazetteer-ready POI dict from OpenStreetMap. |
| `krg regression snapshot` / `regression diff` | Freeze and compare QA baselines. |

### `krg web`

Launches the standalone web app on `127.0.0.1:7481` by default.

```bash
krg web
```

Open <http://127.0.0.1:7481>.

The page is map-first with a single sidebar that holds:
- **Mode banner** at the top — `New Run` (default), `Editing` (yellow) when a user run is selected, or `Fork From` (purple) when a Blue Book run is selected. The banner has a `Cancel` button that clears the form and deselection.
- **Form**: origin + destination inputs (Nominatim autocomplete), folder dropdown, `Save` button.
- **Filter input** + **run tree** with two built-in folders (`Blue Book`, `My Runs`) plus any user-created folders. Each folder row has a `Show` checkbox that toggles its routes on the map; ticking `Show` on Blue Book auto-loads the geometry on first use.
- **Inline actions** revealed on hover: `×` deletes a user run; `✎` renames a folder; `×` deletes a folder (its runs reparent to `My Runs`).
- **`+ New Folder`** drawer for creating folders.
- **Selected-run details** at the bottom (distance, duration, step list); collapsible via `Hide`/`Show`.

Two run sources:
- **Blue Book runs** loaded from `constants/runPoints.json` (override with `--blue-book-file`).
- **User runs** persisted to a per-user data file:
  - macOS: `~/Library/Application Support/knowledge-run-generator/user_runs.json`
  - Linux/other: `~/.local/share/knowledge-run-generator/user_runs.json`

Flags (`krg web` and the equivalent `krg-web` shim):
- `--host TEXT`: Host interface to bind (default: `127.0.0.1`; env: `KRG_WEB_HOST`).
- `--port INTEGER`: Port to bind (default: `7481`; env: `KRG_WEB_PORT`).
- `--blue-book-file TEXT`: Path to Blue Book `runPoints.json` (env: `KRG_BLUE_BOOK_FILE`).
- `--debug`: Enable Flask debug mode.

The direct module form supports one extra flag for non-default user storage:

```bash
python -m knowledge_run_generator.webapp --user-runs-file /tmp/my_runs.json
```

(env: `KRG_USER_RUNS_FILE`.)

Routing graph profile override (used by all commands):
- `KRG_GRAPH_NETWORK_TYPE` (`drive` default, or `drive_service`).

Examples:

```bash
krg web --port 8080
krg web --host 0.0.0.0 --port 7481
krg web --blue-book-file /absolute/path/to/runPoints.json
```

### `krg route` and `krg run`

Generate a single run between two locations.

```bash
krg route "Manor House Station" "Gibson Square"
krg route "Manor House Station" "Gibson Square" --via "GREEN LANES" --via "UPPER STREET"
krg route "Manor House Station" "Gibson Square" --geojson out.geojson
```

`route` flags:
- `--via, -v TEXT` (repeatable): force a route via this street.
- `--geojson, -g PATH`: save the route as GeoJSON.
- `--steps / --no-steps`: print turn-by-turn (default: on).

Note: `krg route` / `Session.run` do not fetch OSM turn restrictions (that would
put a multi-minute Overpass call in front of a single-run command), so their
output is not legality-checked. Pass a restriction set explicitly via
`Session.run(..., prohibited_turns=...)`, or use the Blue Book pipeline, which
loads and enforces them.

The legacy `krg run` command runs the older landmarks-pipeline and supports:
- `--plot, -p PATH`: save a route visualisation image.
- `--geojson, -g PATH`: save the route as GeoJSON.

### Compatibility commands

- `krg-web` → same as `krg web`.
- `knowledge-run` → same group as `krg`.
- `knowledge-run-web` → same as `krg web`.

## Library usage

The package provides a high-level API to generate runs from plaintext addresses:

```python
from knowledge_run_generator import generate_run

run = generate_run("Manor House Station", "Gibson Square")
print(f"Start: {run['start_coords']}")
for step in run["steps"]:
    print(f"- {step['instruction']}")
```

On first use the Greater London graph is downloaded and cached locally; a terminal progress bar is shown during this initial download/build step.

### POI overrides

To pin specific points of interest (theatres, restaurants, stations) you can pass a coordinate override map:

```python
overrides = {
    "MANOR HOUSE STATION": [51.571, -0.094],
}
run = generate_run("MANOR HOUSE STATION", "GIBSON SQUARE", poi_overrides=overrides)
```

This keeps the core engine data-agnostic while resolving complex locations precisely.

## HTTP API

The webapp exposes a small JSON API used by the front end. All payloads are JSON.

```bash
# Generate a new user run
curl -X POST http://127.0.0.1:7481/api/run \
  -H "Content-Type: application/json" \
  -d '{"origin":"Manor House Station","destination":"Gibson Square"}'

# Blue Book
curl http://127.0.0.1:7481/api/bluebook/runs                              # summary list
curl http://127.0.0.1:7481/api/bluebook/runs/all                          # full geometry for every run
curl "http://127.0.0.1:7481/api/bluebook/runs/1?direction=forward"        # one run, forward or reverse

# User runs and folders
curl http://127.0.0.1:7481/api/user-runs                                  # list runs + folders
curl -X PUT http://127.0.0.1:7481/api/user-runs/1 \
  -H "Content-Type: application/json" \
  -d '{"origin":"Manor House Station","destination":"Gibson Square"}'      # update existing user run
curl -X DELETE http://127.0.0.1:7481/api/user-runs/1                       # delete a user run

curl -X POST http://127.0.0.1:7481/api/folders \
  -H "Content-Type: application/json" \
  -d '{"name":"Evening Runs"}'                                              # create folder
curl -X PATCH http://127.0.0.1:7481/api/folders/evening-runs \
  -H "Content-Type: application/json" \
  -d '{"name":"Late Runs"}'                                                 # rename folder
curl -X DELETE http://127.0.0.1:7481/api/folders/late-runs                 # delete folder; runs move to root

# Location autocomplete (Nominatim)
curl "http://127.0.0.1:7481/api/locations/search?q=waterloo&limit=6"
```

---

## Blue Book Demo

`knowledge_run_generator/blue_book_demo/` funnels real-world data and per-run exceptions into the core library to produce 320 canonical Blue Book runs.

### How it works
1. **Load**: parses `blue_book_runs_intermediary.txt` for street sequences.
2. **Inject**: applies `poi_overrides.json` and `run_specific_fixes.json` from the demo folder.
3. **Route**: feeds both into `geocode_and_snap` and `get_constrained_route`.
4. **Validate**: writes `qa_report.json` next to the output, summarising preflight, directness, legality, and street-coverage per run.

### What the QA report records

`qa_report.json` has one entry per run id — always all 320, including runs that
failed before producing anything — plus two summary keys:

- `status` / `failure_reason`: `ok`, or why the run isn't usable (geocode
  failure, preflight failure, no route, an exception, or never processed).
- `shape_problems`: structural defects found by `check_run_shape` — geometry
  too short, not reaching the stated endpoints, a distance shorter than the
  straight line, missing steps. `krg generate all` gates on these, so an
  *unusable* data set fails the same way an *incomplete* one does.
- `unreachable_legs` / `truncated_legs`: legs the router abandoned (no path, or
  the search-state cap). Non-zero means the geometry has a gap.
- `_completeness`: `missing_ids` and `unusable_ids`.
- `_provenance`: the graph size, network type, index sizes and timestamp behind
  this build, so a change in the routes can be attributed rather than guessed at.

### How run endpoints resolve

Each run's origin and destination go through the `Gazetteer`, which tries four
tiers in order and only falls back to the network geocoder if all four miss:

| Tier | Source | Covers (of 640 endpoints) |
|------|--------|---------------------------|
| 1 | `poi_overrides.json` — curated corrections and name aliases | 74 |
| 2 | `constants/knowledge_pois.json` — the geocoded Points List | 432 |
| 3 | `constants/osm_pois.json` — the OSM harvest (`krg osm-pois`) | 31 (stations, prisons, markets, hospitals) |
| 4 | the alias index — endpoints that name a street | ~91 |

Names are matched verbatim, then without the postcode suffix, then
canonically normalised (so `FITZHARDINGE ST W1` finds *Fitzhardinge Street*).
Where several points share a name, the endpoint's postcode district picks the
right one; for streets, the district's centre of mass picks between same-named
streets across London.

Stations get their own matching pass, because OSM and the Blue Book name them
differently: OSM tags *Charing Cross*, *London Waterloo*, *Bow Church*, while
the Blue Book writes `CHARING CROSS STATION WC2` and even
`BETHNAL GREEN B_R STATION E2`. Both sides are reduced to a bare stem —
station/operator words dropped, a leading `LONDON` optional — and that index is
consulted **only** for station-shaped queries, so `FINSBURY PARK N4` and
`FINSBURY PARK STATION N4` don't collapse into the same answer.

Where a Blue Book name simply differs from the name in every data source, an
override value can be that other **name** rather than a position:

```json
{ "HOLLOWAY PRISON": "HM Prison Holloway" }
```

Resolution continues through the remaining tiers under the aliased name, so
there's no coordinate to maintain, and an alias pointing at something unknown
just falls through to the geocoder as before. Aliases may point at streets too.

**Build POIs and harvest OSM before runs.** `krg generate all` already does
both, writing `constants/knowledge_pois.json` and `constants/osm_pois.json`.
Running `krg generate runs` against an empty `constants/` still works, but
every unmatched endpoint costs a rate-limited Nominatim round trip and fails
preflight. Point the pipeline at existing data with `KRG_KNOWLEDGE_POIS` /
`KRG_OSM_POIS` if it lives elsewhere.

### Source data

The canonical street-by-street directions for all 320 runs live in two equivalent files:
- `knowledge_run_generator/blue_book_demo/blue_book_runs_intermediary.txt` — multi-line, one instruction per line. **Used by the pipeline.**
- `The Blue Book Runs of the Knowledge of London.txt` (repo root) — same content, tab-separated, one run per line. Used by `scripts/strict_route_demo.py`.

### Regenerating everything the app consumes

The generator is self-contained: `krg generate all` rebuilds every dataset into
its own `constants/` — the geocoded Knowledge Points List, the OSM gazetteer
seed, all 320 runs — and never reaches into a consumer project. Promotion into
`the-blue-app` is a separate, gated step (`scripts/promote_to_app.py`).

```bash
krg generate all                       # fresh rebuild into the generator's constants/
krg generate all --skip-pois           # reuse knowledge_pois.json; regenerate runs only
krg generate all --out-dir DIR         # also mirror the outputs verbatim into DIR
python scripts/promote_to_app.py --app-dir ../the-blue-app   # validate + copy into the app
```

The pipeline is gated end to end: a fresh `generate all` regenerates from
scratch and **exits non-zero** if any of the 320 runs are missing or the POI set
is empty (with `--out-dir`, nothing is mirrored on a failed gate).
`promote_to_app.py` refuses to overwrite the app's `constants/` unless
`runPoints.json` has all 320 runs and the POI set is non-empty (it copies
`knowledge_pois.json` → the app's `knowledgePois.json`; pass `--allow-partial`
to override). `--out-dir` is a raw mirror that keeps the generator's filenames,
so for the app use `promote_to_app.py` rather than `--out-dir`.

Requirements for a full rebuild:
- **Network**: Overpass (graph, turn restrictions, OSM POIs) and Nominatim
  (run endpoints not covered by `poi_overrides.json`).
- **Mapbox token** for POI geocoding: `--token`, `MAPBOX_TOKEN` /
  `EXPO_PUBLIC_MAPBOX_PK` in the environment, or `--env-file`.
- **Points List PDF**: defaults to `knowledge-of-london-points-list.pdf` at the
  repo root; override with `--pdf`.
- **Borough enrichment reference data** (optional):
  `constants/london_boroughs.geojson` and `constants/yellow_badge_sectors.json`.
  These are inputs the repo does not ship — when absent, enrichment is skipped
  with a message and the rest of the build still completes. Pass `--no-enrich`
  to skip it silently.
- `krg generate` shells out to `scripts/`, so it needs the repo checkout
  (`pip install -e .`), not a plain wheel install.

Individual runs can be rebuilt in place: `krg generate runs 12` regenerates run
12 and merges it back into the existing `runPoints.json`, preserving the QA
records of every other run.

### Running the demo

The friendliest entry point is the CLI:

```bash
krg bluebookdemo 5            # first 5 runs
krg bluebookdemo 20 --fresh   # delete existing output, redo first 20
krg bluebookdemo --geojson    # all 320 runs, also emit constants/routes.geojson
```

The full module form is equivalent and exposes a couple of extra flags:

```bash
python -m knowledge_run_generator.blue_book_demo.run_pipeline [OPTIONS]
```

Options:
- `--output, -o PATH`: output file (default: `constants/runPoints.json`).
- `--limit N`: only process the first N runs.
- `--format, -f {json,geojson}`: output format (default: `json`).
- `--geojson`: secondary export to `constants/routes.geojson`.
- `--network-type {drive,drive_service}`: graph profile override (default uses `KRG_GRAPH_NETWORK_TYPE` or `drive`).

### Strict street-walker (alternative builder)

`scripts/strict_route_demo.py` is a complementary builder that constructs each route by walking the named-street sequence directly: for every consecutive pair of streets it finds the OSM intersection node and runs an in-street Dijkstra restricted to edges with that street name, falling back to plain graph-Dijkstra only across short connector junctions where the two streets don't share a single node. This gets noticeably higher street-coverage (e.g. 89% on Run 4 vs. ~48% with the discount-based pipeline).

```bash
python scripts/strict_route_demo.py --limit 5
```

It reads origin/destination coordinates from the existing `constants/runPoints.json` (so you don't have to re-geocode and hit Nominatim's 1 req/s rate limit), reads street sequences from `The Blue Book Runs of the Knowledge of London.txt`, and overwrites `constants/runPoints.json` with the rebuilt runs.

### Files in the demo
- `run_pipeline.py` — the discount-based orchestrator (used by `krg bluebookdemo`).
- `blue_book_runs_intermediary.txt` — raw street-by-street directions.
- `poi_overrides.json` — geocoding exceptions injected into the library.
- `run_specific_fixes.json` — per-run constraint overrides.
