# Knowledge Run Generator

A Python engine for creating Knowledge of London "Runs" — validated, shortest-distance routes between points of interest in London.

## Features
- **Semantic Routing**: A custom Dijkstra implementation that applies 90% routing discounts to "known" street sequences.
- **Sequential Dijkstra**: Core logic that prevents "tractor beam" backtracks by requiring forward motion through sequence waypoints to unlock discounts.
- **Roundabout Aggregation**: Automatically identifies topological roundabout rings and treats them as single waypoint sets.
- **Automatic Legality**: Validates routes against turn restrictions and provides deterministic "Calls" (navigation instructions).

## CLI

Primary command:

```bash
krg --help
```

### `krg web` (recommended)

Launches the standalone map-first web app on port `7481` by default.

```bash
krg web
```

Open <http://127.0.0.1:7481>.

The homepage is map-first and supports two run sources:
- **Blue Book runs** loaded from `constants/runPoints.json` (or a custom file).
- **User generated runs** created from origin/destination input and persisted to `.context/user_runs.json`.

Flags:
- `--host TEXT`: Host interface to bind (default: `127.0.0.1`).
- `--port INTEGER`: Port to bind (default: `7481`).
- `--blue-book-file TEXT`: Path to Blue Book `runPoints.json`.
- `--debug`: Enable Flask debug mode.

Examples:

```bash
krg web --port 8080
krg web --host 0.0.0.0 --port 7481
krg web --blue-book-file /absolute/path/to/runPoints.json
```

Environment variable overrides for both `krg web` and `krg-web`:
- `KRG_WEB_HOST`
- `KRG_WEB_PORT`
- `KRG_BLUE_BOOK_FILE`

### `krg run`

Generate a terminal run/call between two locations.

```bash
krg run "Manor House Station" "Gibson Square"
```

Flags:
- `--plot`, `-p PATH`: Save route visualization image.
- `--geojson`, `-g PATH`: Save route as GeoJSON.

### Compatibility Commands

- `krg-web`: Direct web command (same defaults as `krg web`).
- `knowledge-run`: Legacy run command.
- `knowledge-run-web`: Legacy web command.

## 🛠 Usage (Library)

The package provides a high-level API to generate runs from plaintext addresses:

```python
from knowledge_run_generator import generate_run

# Simple A->B run
run = generate_run("Manor House Station", "Gibson Square")

print(f"Start: {run['start_coords']}")
for step in run['steps']:
    print(f"- {step['instruction']}")
```

### Data Injection (POI Overrides)
To ensure high accuracy for specific points of interest (theaters, restaurants, stations), you can pass a dictionary of coordinate overrides to the geocoder:

```python
overrides = {
    "MANOR HOUSE STATION": [51.571, -0.094]
}
run = generate_run("MANOR HOUSE STATION", "GIBSON SQUARE", poi_overrides=overrides)
```

This allows the core engine to remain data-agnostic while still resolving complex locations precisely.

## Webapp Wrapper (Standalone)

Run a simple local web interface:

```bash
krg web
```

Then open <http://127.0.0.1:7481>.

If installed as a package, you can also run:

```bash
krg-web
```

Optional JSON API endpoint:

```bash
curl -X POST http://127.0.0.1:7481/api/run \\
  -H "Content-Type: application/json" \\
  -d '{"origin":"Manor House Station","destination":"Gibson Square"}'
```

Blue Book endpoints used by the web UI:

```bash
curl http://127.0.0.1:7481/api/bluebook/runs
curl "http://127.0.0.1:7481/api/bluebook/runs/1?direction=forward"
curl http://127.0.0.1:7481/api/user-runs
```

---

## 🏎 Blue Book Demo

The `blue_book_demo` directory contains a robust implementation of how to funnel real-world data and exceptions into the core library.

### How it works:
The demo orchestrates the generation of 320 canonical Blue Book runs by:
1. **Loading Data**: Reading `blue_book_runs_intermediary.txt` for street sequences.
2. **Injecting Fixes**: Loading `poi_overrides.json` and `run_specific_fixes.json` from the local folder.
3. **Funneling Logic**: Passing these local exceptions into the library functions (`geocode_and_snap`, `get_constrained_route`).

### Running the Demo:
From the project root:
```bash
python -m knowledge_run_generator.blue_book_demo.run_pipeline [OPTIONS]
```

**Options:**
- `--output`, `-o PATH`: Specify the output file path. Defaults to `constants/runPoints.json` for webapp compatibility.
- `--format`, `-f {json,geojson}`: Output format (default: `json`).
- `--limit N`: Only process the first N runs.
- `--geojson`: Secondary export to `constants/routes.geojson`.

To generate Blue Book data directly for the web app:

```bash
python -m knowledge_run_generator.blue_book_demo.run_pipeline --output constants/runPoints.json
```

### Files in Demo:
- `run_pipeline.py`: The orchestrator script.
- `blue_book_runs_intermediary.txt`: Raw street-by-street directions.
- `poi_overrides.json`: Geocoding exceptions injected into the library.
- `run_specific_fixes.json`: Per-run constraint overrides.
