# Knowledge Run Generator

A Python engine for creating Knowledge of London "Runs" — validated, shortest-distance routes between points of interest in London.

## Features
- **Semantic Routing**: A custom Dijkstra implementation that applies 90% routing discounts to "known" street sequences.
- **Sequential Dijkstra**: Core logic that prevents "tractor beam" backtracks by requiring forward motion through sequence waypoints to unlock discounts.
- **Roundabout Aggregation**: Automatically identifies topological roundabout rings and treats them as single waypoint sets.
- **Automatic Legality**: Validates routes against turn restrictions and provides deterministic "Calls" (navigation instructions).

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

### Files in Demo:
- `run_pipeline.py`: The orchestrator script.
- `blue_book_runs_intermediary.txt`: Raw street-by-street directions.
- `poi_overrides.json`: Geocoding exceptions injected into the library.
- `run_specific_fixes.json`: Per-run constraint overrides.
