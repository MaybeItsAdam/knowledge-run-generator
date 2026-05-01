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

### Source data

The canonical street-by-street directions for all 320 runs live in two equivalent files:
- `knowledge_run_generator/blue_book_demo/blue_book_runs_intermediary.txt` — multi-line, one instruction per line. **Used by the pipeline.**
- `The Blue Book Runs of the Knowledge of London.txt` (repo root) — same content, tab-separated, one run per line. Used by `scripts/strict_route_demo.py`.

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
