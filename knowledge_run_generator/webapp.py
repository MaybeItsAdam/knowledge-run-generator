from __future__ import annotations

import argparse
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template_string, request

from .api import generate_run
from .router import _extract_route_metadata, load_graph, nodes_to_coords_geometry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 7481
DEFAULT_BLUE_BOOK_FILE = PROJECT_ROOT / "constants" / "runPoints.json"
DEFAULT_USER_RUNS_FILE = PROJECT_ROOT / ".context" / "user_runs.json"

app = Flask(__name__)
app.config["BLUE_BOOK_FILE"] = DEFAULT_BLUE_BOOK_FILE
app.config["USER_RUNS_FILE"] = DEFAULT_USER_RUNS_FILE

_GRAPH = None
_GRAPH_LOCK = threading.Lock()

_BLUE_BOOK_CACHE: dict[str, Any] = {
    "path": None,
    "mtime": None,
    "runs": [],
    "by_id": {},
}
_BLUE_BOOK_LOCK = threading.Lock()

_USER_RUNS_CACHE: list[dict[str, Any]] | None = None
_USER_RUNS_LOCK = threading.Lock()

_PAGE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>krg web</title>
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
    crossorigin=""
  >
  <style>
    :root {
      color-scheme: light;
      --bg: #f3f6fa;
      --panel: #ffffff;
      --line: #d4ddeb;
      --text: #172033;
      --muted: #556079;
      --accent: #0f6ad9;
      --accent-2: #0f766e;
      --warn: #b91c1c;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      background: linear-gradient(160deg, #ecf3ff 0%, #f8fbff 45%, #eff6f3 100%);
      color: var(--text);
    }

    main {
      width: min(1400px, 100%);
      margin: 0 auto;
      padding: .75rem;
    }

    h1 {
      margin: .35rem 0 .25rem;
      font-size: clamp(1.2rem, 2.5vw, 1.7rem);
      letter-spacing: .01em;
    }

    .subhead {
      margin: 0 0 .65rem;
      color: var(--muted);
      font-size: .95rem;
    }

    #map {
      height: 68vh;
      min-height: 420px;
      border-radius: 14px;
      border: 1px solid var(--line);
      box-shadow: 0 16px 40px rgba(17, 31, 58, 0.14);
      overflow: hidden;
    }

    .grid {
      margin-top: .75rem;
      display: grid;
      grid-template-columns: 1.1fr 1fr;
      gap: .75rem;
      align-items: start;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: .75rem;
      box-shadow: 0 8px 20px rgba(17, 31, 58, 0.07);
    }

    h2 {
      margin: 0 0 .5rem;
      font-size: 1rem;
    }

    h3 {
      margin: .9rem 0 .4rem;
      font-size: .95rem;
      color: var(--muted);
    }

    .meta {
      margin-bottom: .45rem;
      color: var(--muted);
      font-size: .88rem;
    }

    input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: .55rem .6rem;
      font: inherit;
      background: #f8fbff;
    }

    form {
      display: grid;
      gap: .45rem;
    }

    button {
      border: 0;
      border-radius: 8px;
      padding: .5rem .7rem;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      color: #fff;
      background: var(--accent);
    }

    button.secondary {
      background: #3c4a6b;
    }

    button.ghost {
      background: #eef3fb;
      color: #1e2a41;
      font-weight: 500;
    }

    .run-list {
      margin-top: .55rem;
      max-height: 320px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #f9fbff;
    }

    .run-item {
      padding: .55rem .6rem;
      border-bottom: 1px solid #e3eaf6;
    }

    .run-item:last-child { border-bottom: 0; }

    .run-item-title {
      margin: 0;
      font-size: .92rem;
      font-weight: 650;
    }

    .run-item-sub {
      margin: .15rem 0 .35rem;
      color: var(--muted);
      font-size: .84rem;
    }

    .run-actions {
      display: flex;
      gap: .35rem;
      flex-wrap: wrap;
    }

    .details {
      margin-top: .75rem;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #f8fbff;
      padding: .65rem;
    }

    .details ol {
      margin: .45rem 0 0;
      padding-left: 1.2rem;
      max-height: 180px;
      overflow: auto;
      font-size: .9rem;
    }

    .status-error {
      color: var(--warn);
      font-size: .88rem;
      margin-top: .35rem;
    }

    .tag {
      display: inline-block;
      padding: .16rem .4rem;
      border-radius: 999px;
      font-size: .72rem;
      font-weight: 700;
      color: #fff;
      margin-left: .35rem;
    }

    .tag-blue { background: #1e40af; }
    .tag-user { background: var(--accent-2); }

    @media (max-width: 980px) {
      .grid {
        grid-template-columns: 1fr;
      }

      #map {
        height: 56vh;
        min-height: 360px;
      }
    }
  </style>
</head>
<body>
  <main>
    <h1>krg web</h1>
    <p class="subhead">Map-first explorer for Blue Book runs and user-generated routes.</p>

    <div id="map"></div>

    <div class="grid">
      <section class="panel">
        <h2>Blue Book Runs <span class="tag tag-blue">Reference</span></h2>
        <div class="meta" id="bluebookStatus">Loading Blue Book runs...</div>
        <input id="bluebookSearch" type="search" placeholder="Search by run id, title, start, or end">
        <div id="bluebookList" class="run-list"></div>
      </section>

      <section class="panel">
        <h2>Generate User Run <span class="tag tag-user">Custom</span></h2>
        <form id="userRunForm">
          <input id="originInput" required placeholder="Origin (example: Manor House Station)">
          <input id="destinationInput" required placeholder="Destination (example: Gibson Square)">
          <button type="submit">Generate + Show on Map</button>
        </form>
        <div id="userRunError" class="status-error"></div>

        <h3>User Generated Runs</h3>
        <div id="userRunList" class="run-list"></div>
      </section>
    </div>

    <section class="details" id="runDetails">
      <strong>No run selected.</strong>
      <div class="meta">Pick a Blue Book route or generate a user route.</div>
    </section>
  </main>

  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    crossorigin=""
  ></script>
  <script>
    const state = {
      map: null,
      routeLayer: null,
      markerLayer: null,
      blueBookRuns: [],
      userRuns: [],
    };

    function initMap() {
      state.map = L.map("map", { zoomControl: true }).setView([51.5074, -0.1278], 11);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
      }).addTo(state.map);
      state.routeLayer = L.layerGroup().addTo(state.map);
      state.markerLayer = L.layerGroup().addTo(state.map);
    }

    function clearMapRoute() {
      state.routeLayer.clearLayers();
      state.markerLayer.clearLayers();
    }

    function drawRun(runPayload, color) {
      clearMapRoute();
      const coords = (((runPayload || {}).route || {}).geometry || {}).coordinates || [];
      if (!coords.length) {
        return;
      }

      const latLngs = coords.map((point) => [point[1], point[0]]);
      const polyline = L.polyline(latLngs, {
        color,
        weight: 6,
        opacity: 0.9,
        lineJoin: "round",
      }).addTo(state.routeLayer);

      const start = (runPayload || {}).start || {};
      const end = (runPayload || {}).end || {};
      const startCoords = start.coordinates || [];
      const endCoords = end.coordinates || [];

      if (startCoords.length === 2) {
        L.circleMarker([startCoords[1], startCoords[0]], {
          radius: 7,
          color: "#0f766e",
          fillColor: "#0f766e",
          fillOpacity: 0.95,
        }).bindPopup(`Start: ${start.name || "Unknown"}`).addTo(state.markerLayer);
      }

      if (endCoords.length === 2) {
        L.circleMarker([endCoords[1], endCoords[0]], {
          radius: 7,
          color: "#b91c1c",
          fillColor: "#b91c1c",
          fillOpacity: 0.95,
        }).bindPopup(`End: ${end.name || "Unknown"}`).addTo(state.markerLayer);
      }

      state.map.fitBounds(polyline.getBounds(), { padding: [24, 24] });
    }

    function renderDetails(runPayload, tagLabel) {
      const detailsEl = document.getElementById("runDetails");
      const route = (runPayload || {}).route || {};
      const steps = Array.isArray(route.steps) ? route.steps : [];
      const distance = Number(route.distance || 0).toFixed(1);
      const duration = Number(route.duration || 0).toFixed(1);

      const header = `${runPayload.title || "Untitled"} (${tagLabel})`;
      const startName = ((runPayload || {}).start || {}).name || "Unknown";
      const endName = ((runPayload || {}).end || {}).name || "Unknown";

      detailsEl.innerHTML = `
        <strong>${header}</strong>
        <div class="meta">${startName} -> ${endName}</div>
        <div class="meta">Distance: ${distance}m | Duration: ${duration}s | Steps: ${steps.length}</div>
        <ol>${steps.map((step) => `<li>${step.instruction || ""}</li>`).join("")}</ol>
      `;
    }

    function renderBlueBookList(runs) {
      const listEl = document.getElementById("bluebookList");
      if (!runs.length) {
        listEl.innerHTML = "<div class='run-item'>No Blue Book runs loaded.</div>";
        return;
      }

      listEl.innerHTML = runs.map((run) => {
        const fd = Number(run.forward_distance_m || 0).toFixed(1);
        const rd = Number(run.reverse_distance_m || 0).toFixed(1);
        return `
          <div class="run-item">
            <p class="run-item-title">Run ${run.id}: ${run.title}</p>
            <p class="run-item-sub">${run.start_name} -> ${run.end_name} | fwd ${fd}m | rev ${rd}m</p>
            <div class="run-actions">
              <button class="ghost" data-run-id="${run.id}" data-direction="forward">Show Forward</button>
              <button class="ghost" data-run-id="${run.id}" data-direction="reverse">Show Reverse</button>
            </div>
          </div>
        `;
      }).join("");

      listEl.querySelectorAll("button[data-run-id]").forEach((button) => {
        button.addEventListener("click", async () => {
          const runId = button.getAttribute("data-run-id");
          const direction = button.getAttribute("data-direction");
          const response = await fetch(`/api/bluebook/runs/${runId}?direction=${direction}`);
          const payload = await response.json();
          if (!response.ok) {
            document.getElementById("bluebookStatus").textContent = payload.error || "Unable to load run.";
            return;
          }
          drawRun(payload, "#1e40af");
          renderDetails(payload, `Blue Book ${direction}`);
        });
      });
    }

    function renderUserRunList(runs) {
      const listEl = document.getElementById("userRunList");
      if (!runs.length) {
        listEl.innerHTML = "<div class='run-item'>No user runs yet.</div>";
        return;
      }

      listEl.innerHTML = runs.map((run) => `
        <div class="run-item">
          <p class="run-item-title">${run.title}</p>
          <p class="run-item-sub">${run.start.name} -> ${run.end.name}</p>
          <div class="run-actions">
            <button class="secondary" data-user-id="${run.id}">Show on Map</button>
          </div>
        </div>
      `).join("");

      listEl.querySelectorAll("button[data-user-id]").forEach((button) => {
        button.addEventListener("click", () => {
          const runId = button.getAttribute("data-user-id");
          const run = state.userRuns.find((item) => String(item.id) === String(runId));
          if (!run) {
            return;
          }
          drawRun(run, "#0f766e");
          renderDetails(run, "User");
        });
      });
    }

    async function loadBlueBookRuns() {
      const statusEl = document.getElementById("bluebookStatus");
      const response = await fetch("/api/bluebook/runs");
      const payload = await response.json();

      if (!response.ok) {
        statusEl.textContent = payload.error || "Failed to load Blue Book runs.";
        renderBlueBookList([]);
        return;
      }

      state.blueBookRuns = payload.runs || [];
      const loadedCount = state.blueBookRuns.length;
      if (loadedCount === 0) {
        statusEl.textContent = payload.message || "No Blue Book data file found. Set --blue-book-file or KRG_BLUE_BOOK_FILE.";
      } else {
        statusEl.textContent = `Loaded ${loadedCount} Blue Book runs from ${payload.source}.`;
      }
      renderBlueBookList(state.blueBookRuns);
    }

    async function loadUserRuns() {
      const response = await fetch("/api/user-runs");
      const payload = await response.json();
      if (!response.ok) {
        return;
      }
      state.userRuns = payload.runs || [];
      renderUserRunList(state.userRuns);
    }

    function wireBlueBookSearch() {
      const input = document.getElementById("bluebookSearch");
      input.addEventListener("input", () => {
        const term = input.value.trim().toLowerCase();
        if (!term) {
          renderBlueBookList(state.blueBookRuns);
          return;
        }
        const filtered = state.blueBookRuns.filter((run) => {
          const hay = `${run.id} ${run.title} ${run.start_name} ${run.end_name}`.toLowerCase();
          return hay.includes(term);
        });
        renderBlueBookList(filtered);
      });
    }

    function wireUserRunForm() {
      const form = document.getElementById("userRunForm");
      const errorEl = document.getElementById("userRunError");

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        errorEl.textContent = "";

        const origin = document.getElementById("originInput").value.trim();
        const destination = document.getElementById("destinationInput").value.trim();
        if (!origin || !destination) {
          errorEl.textContent = "Both origin and destination are required.";
          return;
        }

        const response = await fetch("/api/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ origin, destination }),
        });
        const payload = await response.json();
        if (!response.ok) {
          errorEl.textContent = payload.error || "Could not generate route.";
          return;
        }

        state.userRuns = [payload, ...state.userRuns.filter((run) => run.id !== payload.id)];
        renderUserRunList(state.userRuns);
        drawRun(payload, "#0f766e");
        renderDetails(payload, "User");
        form.reset();
      });
    }

    async function boot() {
      initMap();
      wireBlueBookSearch();
      wireUserRunForm();
      await Promise.all([loadBlueBookRuns(), loadUserRuns()]);
    }

    boot();
  </script>
</body>
</html>
"""


def _estimate_duration_seconds(distance_m: float) -> float:
    return round((distance_m / 1000.0) / 20.0 * 3600.0, 1)


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        with _GRAPH_LOCK:
            if _GRAPH is None:
                _GRAPH = load_graph()
    return _GRAPH


def _normalize_run_id(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r") as handle:
            return json.load(handle)
    except Exception:
        return default


def _ensure_user_runs_loaded() -> list[dict[str, Any]]:
    global _USER_RUNS_CACHE

    if _USER_RUNS_CACHE is None:
        with _USER_RUNS_LOCK:
            if _USER_RUNS_CACHE is None:
                user_runs_path = Path(app.config["USER_RUNS_FILE"])
                if user_runs_path.exists():
                    payload = _read_json(user_runs_path, default=[])
                    _USER_RUNS_CACHE = payload if isinstance(payload, list) else []
                else:
                    _USER_RUNS_CACHE = []

    return _USER_RUNS_CACHE


def _save_user_runs() -> None:
    user_runs_path = Path(app.config["USER_RUNS_FILE"])
    user_runs_path.parent.mkdir(parents=True, exist_ok=True)
    with user_runs_path.open("w") as handle:
        json.dump(_ensure_user_runs_loaded(), handle, indent=2)


def _convert_feature_collection_to_runs(collection: dict[str, Any]) -> list[dict[str, Any]]:
    features = collection.get("features") if isinstance(collection, dict) else None
    if not isinstance(features, list):
        return []

    grouped: dict[int, dict[str, Any]] = {}
    fallback_id = 1

    for feature in features:
        if not isinstance(feature, dict):
            continue

        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {"type": "LineString", "coordinates": []}

        run_id = _normalize_run_id(properties.get("run_id"), fallback=fallback_id)
        fallback_id += 1

        direction = str(properties.get("direction") or "forward").lower().strip()
        route_key = "routeReverse" if direction == "reverse" else "route"

        if run_id not in grouped:
            origin_name = str(properties.get("origin") or f"Run {run_id} origin")
            destination_name = str(properties.get("destination") or f"Run {run_id} destination")
            grouped[run_id] = {
                "id": run_id,
                "title": f"Run {run_id}: {origin_name} to {destination_name}",
                "start": {"name": origin_name, "coordinates": []},
                "end": {"name": destination_name, "coordinates": []},
                "route": {"geometry": {"type": "LineString", "coordinates": []}, "distance": 0, "duration": 0, "steps": []},
                "routeReverse": {"geometry": {"type": "LineString", "coordinates": []}, "distance": 0, "duration": 0, "steps": []},
            }

        distance_m = float(properties.get("total_distance_m") or 0)
        grouped[run_id][route_key] = {
            "geometry": geometry,
            "distance": distance_m,
            "duration": _estimate_duration_seconds(distance_m),
            "steps": [],
        }

        coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if isinstance(coords, list) and coords:
            grouped[run_id]["start"]["coordinates"] = coords[0]
            grouped[run_id]["end"]["coordinates"] = coords[-1]

    return [grouped[key] for key in sorted(grouped)]


def _load_blue_book_runs() -> dict[str, Any]:
    blue_book_path = Path(app.config["BLUE_BOOK_FILE"])

    if not blue_book_path.exists():
        return {
            "runs": [],
            "by_id": {},
            "source": str(blue_book_path),
            "message": "Blue Book run file not found.",
        }

    mtime = blue_book_path.stat().st_mtime

    with _BLUE_BOOK_LOCK:
        if (
            _BLUE_BOOK_CACHE["path"] == str(blue_book_path)
            and _BLUE_BOOK_CACHE["mtime"] == mtime
        ):
            return {
                "runs": _BLUE_BOOK_CACHE["runs"],
                "by_id": _BLUE_BOOK_CACHE["by_id"],
                "source": str(blue_book_path),
                "message": "",
            }

        payload = _read_json(blue_book_path, default=[])

        if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
            loaded_runs = _convert_feature_collection_to_runs(payload)
        elif isinstance(payload, list):
            loaded_runs = payload
        else:
            loaded_runs = []

        summaries = []
        by_id: dict[int, dict[str, Any]] = {}

        for index, run in enumerate(loaded_runs, start=1):
            if not isinstance(run, dict):
                continue
            run_id = _normalize_run_id(run.get("id"), fallback=index)
            run["id"] = run_id

            start = run.get("start") if isinstance(run.get("start"), dict) else {}
            end = run.get("end") if isinstance(run.get("end"), dict) else {}
            route = run.get("route") if isinstance(run.get("route"), dict) else {}
            reverse_route = run.get("routeReverse") if isinstance(run.get("routeReverse"), dict) else {}

            summaries.append(
                {
                    "id": run_id,
                    "title": str(run.get("title") or f"Run {run_id}"),
                    "start_name": str(start.get("name") or "Unknown"),
                    "end_name": str(end.get("name") or "Unknown"),
                    "forward_distance_m": float(route.get("distance") or 0),
                    "reverse_distance_m": float(reverse_route.get("distance") or 0),
                }
            )
            by_id[run_id] = run

        summaries.sort(key=lambda item: item["id"])

        _BLUE_BOOK_CACHE["path"] = str(blue_book_path)
        _BLUE_BOOK_CACHE["mtime"] = mtime
        _BLUE_BOOK_CACHE["runs"] = summaries
        _BLUE_BOOK_CACHE["by_id"] = by_id

    return {
        "runs": _BLUE_BOOK_CACHE["runs"],
        "by_id": _BLUE_BOOK_CACHE["by_id"],
        "source": str(blue_book_path),
        "message": "",
    }


def _make_direction_payload(run: dict[str, Any], direction: str) -> dict[str, Any]:
    use_reverse = direction == "reverse"

    forward_route = run.get("route") if isinstance(run.get("route"), dict) else {}
    reverse_route = run.get("routeReverse") if isinstance(run.get("routeReverse"), dict) else {}

    route = reverse_route if use_reverse else forward_route
    fallback_route = forward_route if use_reverse else reverse_route

    route_geometry = route.get("geometry") if isinstance(route.get("geometry"), dict) else {}
    route_coords = route_geometry.get("coordinates") if isinstance(route_geometry.get("coordinates"), list) else []

    if not route_coords:
        fallback_geometry = fallback_route.get("geometry") if isinstance(fallback_route.get("geometry"), dict) else {}
        fallback_coords = fallback_geometry.get("coordinates") if isinstance(fallback_geometry.get("coordinates"), list) else []
        route_coords = list(reversed(fallback_coords)) if use_reverse else fallback_coords

    start = run.get("start") if isinstance(run.get("start"), dict) else {}
    end = run.get("end") if isinstance(run.get("end"), dict) else {}

    payload_start = end if use_reverse else start
    payload_end = start if use_reverse else end

    return {
        "id": run.get("id"),
        "title": str(run.get("title") or f"Run {run.get('id', '?')}"),
        "source": "blue_book",
        "direction": direction,
        "start": {
            "name": payload_start.get("name") or "Unknown",
            "coordinates": payload_start.get("coordinates") or (route_coords[0] if route_coords else []),
        },
        "end": {
            "name": payload_end.get("name") or "Unknown",
            "coordinates": payload_end.get("coordinates") or (route_coords[-1] if route_coords else []),
        },
        "route": {
            "geometry": {
                "type": "LineString",
                "coordinates": route_coords,
            },
            "distance": float(route.get("distance") or 0),
            "duration": float(route.get("duration") or 0),
            "steps": route.get("steps") if isinstance(route.get("steps"), list) else [],
        },
    }


def _make_user_run(origin: str, destination: str) -> dict[str, Any]:
    graph = _get_graph()
    result = generate_run(origin, destination, G=graph)

    if "error" in result:
        return result

    route_nodes = result.get("route_nodes") or []
    route_coords = nodes_to_coords_geometry(graph, route_nodes)
    metadata = _extract_route_metadata(graph, route_nodes)
    distance_m = float(metadata.get("total_distance") or 0)
    duration_s = _estimate_duration_seconds(distance_m)

    start_coords = result.get("start_coords") or [None, None]
    end_coords = result.get("end_coords") or [None, None]

    runs = _ensure_user_runs_loaded()
    next_id = max([_normalize_run_id(run.get("id"), 0) for run in runs], default=0) + 1

    run_obj = {
        "id": next_id,
        "title": f"{origin} to {destination}",
        "source": "user",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "start": {
            "name": origin,
            "coordinates": [start_coords[1], start_coords[0]],
        },
        "end": {
            "name": destination,
            "coordinates": [end_coords[1], end_coords[0]],
        },
        "route": {
            "geometry": {
                "type": "LineString",
                "coordinates": route_coords,
            },
            "distance": distance_m,
            "duration": duration_s,
            "steps": result.get("steps") if isinstance(result.get("steps"), list) else [],
        },
    }

    with _USER_RUNS_LOCK:
        runs.insert(0, run_obj)
        _save_user_runs()

    return run_obj


@app.get("/")
def index():
    return render_template_string(_PAGE_TEMPLATE)


@app.get("/api/bluebook/runs")
def blue_book_runs():
    loaded = _load_blue_book_runs()
    return jsonify(
        {
            "runs": loaded["runs"],
            "source": loaded["source"],
            "message": loaded["message"],
        }
    )


@app.get("/api/bluebook/runs/<int:run_id>")
def blue_book_run_detail(run_id: int):
    direction = (request.args.get("direction") or "forward").strip().lower()
    if direction not in {"forward", "reverse"}:
        return jsonify({"error": "direction must be 'forward' or 'reverse'."}), 400

    loaded = _load_blue_book_runs()
    run = loaded["by_id"].get(run_id)
    if not run:
        return jsonify({"error": f"Run {run_id} not found."}), 404

    payload = _make_direction_payload(run, direction)
    coords = payload.get("route", {}).get("geometry", {}).get("coordinates", [])
    if not coords:
        return jsonify({"error": f"Run {run_id} has no route geometry for {direction}."}), 422

    return jsonify(payload)


@app.get("/api/user-runs")
def user_runs():
    runs = _ensure_user_runs_loaded()
    return jsonify({"runs": runs})


@app.post("/api/run")
def generate_user_run():
    payload = request.get_json(silent=True) or {}
    origin = str(payload.get("origin") or "").strip()
    destination = str(payload.get("destination") or "").strip()

    if not origin or not destination:
        return jsonify({"error": "Both origin and destination are required."}), 400

    run_obj = _make_user_run(origin, destination)
    if "error" in run_obj:
        return jsonify(run_obj), 422

    return jsonify(run_obj)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the krg web wrapper.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("KRG_WEB_HOST", DEFAULT_WEB_HOST),
        help="Host interface to bind (env: KRG_WEB_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("KRG_WEB_PORT", str(DEFAULT_WEB_PORT))),
        help="Port to bind (env: KRG_WEB_PORT).",
    )
    parser.add_argument(
        "--blue-book-file",
        default=os.environ.get("KRG_BLUE_BOOK_FILE", str(DEFAULT_BLUE_BOOK_FILE)),
        help="Path to Blue Book runPoints JSON (env: KRG_BLUE_BOOK_FILE).",
    )
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode.")
    args = parser.parse_args()

    run_webapp(
        host=args.host,
        port=args.port,
        debug=args.debug,
        blue_book_file=args.blue_book_file,
    )


def run_webapp(
    host: str | None = None,
    port: int | None = None,
    debug: bool = False,
    blue_book_file: str | None = None,
) -> None:
    resolved_host = host or os.environ.get("KRG_WEB_HOST", DEFAULT_WEB_HOST)
    resolved_port = port or int(os.environ.get("KRG_WEB_PORT", str(DEFAULT_WEB_PORT)))
    resolved_blue_book_file = Path(
        blue_book_file or os.environ.get("KRG_BLUE_BOOK_FILE", str(DEFAULT_BLUE_BOOK_FILE))
    )

    app.config["BLUE_BOOK_FILE"] = resolved_blue_book_file

    app.run(host=resolved_host, port=resolved_port, debug=debug)


if __name__ == "__main__":
    main()
