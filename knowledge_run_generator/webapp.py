from __future__ import annotations

import argparse
import json
import os
import platform
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template_string, request
from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from geopy.geocoders import Nominatim

from .api import generate_run
from .router import _extract_route_metadata, load_graph, nodes_to_coords_geometry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 7481
DEFAULT_BLUE_BOOK_FILE = PROJECT_ROOT / "constants" / "runPoints.json"
DEFAULT_POIS_FILE = PROJECT_ROOT / "constants" / "knowledge_pois.json"
BLUE_BOOK_FOLDER_KEY = "blue-book-runs"
ROOT_USER_FOLDER_KEY = "__root_user__"


def _default_user_runs_file() -> Path:
    override = os.environ.get("KRG_USER_RUNS_FILE")
    if override:
        return Path(override).expanduser()

    if platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"

    return base / "knowledge-run-generator" / "user_runs.json"


DEFAULT_USER_RUNS_FILE = _default_user_runs_file()

app = Flask(__name__)
app.config["BLUE_BOOK_FILE"] = DEFAULT_BLUE_BOOK_FILE
app.config["USER_RUNS_FILE"] = DEFAULT_USER_RUNS_FILE
app.config["POIS_FILE"] = DEFAULT_POIS_FILE

_GRAPH = None
_GRAPH_LOCK = threading.Lock()

_BLUE_BOOK_CACHE: dict[str, Any] = {
    "path": None,
    "mtime": None,
    "runs": [],
    "by_id": {},
}
_BLUE_BOOK_LOCK = threading.Lock()

_USER_STORE_CACHE: dict[str, Any] | None = None
_USER_STORE_LOCK = threading.RLock()

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
      --bg-1: #fffaf0;
      --bg-2: #f6f1e6;
      --panel: #fffcf5;
      --line: #1f2937;
      --line-soft: #94a3b8;
      --text: #0f172a;
      --muted: #475569;
      --accent: #facc15;
      --accent-2: #22c55e;
      --warn: #b91c1c;
      --shadow: 3px 3px 0 #111827;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
    }

    * { box-sizing: border-box; }

    html, body {
      margin: 0;
      height: 100%;
      color: var(--text);
      background: linear-gradient(145deg, var(--bg-1) 0%, var(--bg-2) 100%);
    }

    button, input, select { font: inherit; color: var(--text); }

    .app-shell {
      height: 100dvh;
      display: grid;
      grid-template-columns: minmax(340px, 400px) 1fr;
      overflow: hidden;
    }

    .sidebar {
      overflow: hidden;
      border-right: 3px solid var(--line);
      background: #fffef9;
      padding: .6rem;
      display: flex;
      flex-direction: column;
      gap: .55rem;
      min-height: 0;
    }

    .map-pane {
      position: relative;
      height: 100%;
    }

    #map {
      width: 100%;
      height: 100%;
    }

    .panel {
      background: var(--panel);
      border: 2px solid var(--line);
      box-shadow: var(--shadow);
      padding: .6rem;
    }

    .mode-banner {
      display: flex;
      align-items: center;
      gap: .5rem;
      padding: .42rem .55rem;
      border: 2px solid var(--line);
      box-shadow: var(--shadow);
      background: #e8eefc;
    }

    .mode-banner[data-mode="edit"] {
      background: #fde68a;
    }

    .mode-banner[data-mode="fork"] {
      background: #ddd6fe;
    }

    .mode-tag {
      display: inline-block;
      border: 2px solid var(--line);
      background: #fff;
      padding: .12rem .38rem;
      font-size: .66rem;
      font-weight: 800;
      letter-spacing: .08em;
      text-transform: uppercase;
      white-space: nowrap;
    }

    .mode-title {
      flex: 1;
      min-width: 0;
      font-size: .92rem;
      font-weight: 650;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .mode-cancel {
      border: 2px solid var(--line);
      background: #fff;
      padding: .22rem .5rem;
      font-size: .76rem;
      font-weight: 600;
      cursor: pointer;
      box-shadow: 2px 2px 0 #111827;
    }

    .mode-cancel.hidden { display: none; }

    .form-section {
      display: grid;
      gap: .42rem;
    }

    input, select {
      width: 100%;
      border: 2px solid var(--line);
      padding: .5rem .55rem;
      background: #fff;
    }

    input:focus, select:focus {
      outline: 2px solid #93c5fd;
      border-color: var(--line);
    }

    button {
      border: 2px solid var(--line);
      padding: .46rem .66rem;
      font-weight: 650;
      cursor: pointer;
      background: var(--accent);
      box-shadow: var(--shadow);
    }

    button.secondary { background: #d1fae5; }
    button.ghost { background: #edf2fb; font-weight: 550; }

    .status-error {
      color: var(--warn);
      font-size: .82rem;
      min-height: 1rem;
    }

    .search-field {
      position: relative;
    }

    .suggestions {
      position: absolute;
      top: calc(100% + 2px);
      left: 0;
      right: 0;
      z-index: 800;
      border: 2px solid var(--line);
      background: #fff;
      max-height: 200px;
      overflow-y: auto;
      display: none;
      box-shadow: var(--shadow);
    }

    .suggestions.show { display: block; }

    .suggestion-item {
      border: 0;
      border-bottom: 1px solid #e8eef8;
      width: 100%;
      text-align: left;
      background: #fff;
      padding: .42rem .55rem;
      font-size: .82rem;
      font-weight: 500;
      cursor: pointer;
      box-shadow: none;
    }

    .suggestion-item:last-child { border-bottom: 0; }
    .suggestion-item:hover { background: #f3f7ff; }

    .filter-input {
      margin: 0;
    }

    .tree {
      border: 2px solid var(--line);
      background: #ffffff;
      flex: 1 1 auto;
      min-height: 120px;
      overflow-y: auto;
      padding: .42rem;
      font-size: .82rem;
      box-shadow: var(--shadow);
    }

    .tree-empty {
      padding: 1rem .6rem;
      color: var(--muted);
      font-size: .85rem;
      text-align: center;
      line-height: 1.45;
    }

    .tree-folder { margin: .14rem 0; }

    .tree-folder-row {
      display: flex;
      align-items: center;
      gap: .3rem;
      padding: .26rem .36rem;
      background: #fef3c7;
      border: 2px solid var(--line);
      min-height: 30px;
    }

    .tree-folder[data-builtin="true"] .tree-folder-row {
      background: #e0e7ff;
    }

    .disclosure {
      border: 0;
      background: transparent;
      width: 18px;
      min-width: 18px;
      text-align: center;
      padding: 0;
      font-size: .8rem;
      cursor: pointer;
      box-shadow: none;
      color: #334155;
    }

    .tree-folder-label {
      margin: 0;
      color: #172554;
      font-weight: 650;
      flex: 1;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .tree-folder-meta {
      color: var(--muted);
      font-size: .7rem;
      white-space: nowrap;
    }

    .tree-toggle {
      display: inline-flex;
      align-items: center;
      gap: .22rem;
      color: #334155;
      font-size: .72rem;
      font-weight: 600;
      white-space: nowrap;
      user-select: none;
      cursor: pointer;
    }

    .tree-toggle input {
      width: 14px;
      height: 14px;
      margin: 0;
      padding: 0;
      cursor: pointer;
    }

    .row-action {
      border: 1px solid var(--line);
      background: #fff;
      width: 22px;
      height: 22px;
      padding: 0;
      font-size: .8rem;
      line-height: 1;
      box-shadow: none;
      cursor: pointer;
      opacity: 0;
      transition: opacity .12s ease;
    }

    .tree-folder-row:hover .row-action,
    .tree-run-wrap:hover .row-action,
    .tree-run-wrap:focus-within .row-action {
      opacity: 1;
    }

    .row-action:hover { background: #fee2e2; }
    .row-action.rename:hover { background: #dbeafe; }

    .tree-folder-children {
      margin-top: .12rem;
      padding-left: 1.15rem;
    }

    .tree-folder-children.hidden { display: none; }

    .tree-run-wrap {
      display: flex;
      align-items: stretch;
      gap: .25rem;
      margin: .14rem 0;
    }

    .tree-run {
      flex: 1;
      min-width: 0;
      border: 2px solid transparent;
      background: #fff;
      color: #0f172a;
      padding: .3rem .4rem;
      display: flex;
      align-items: flex-start;
      gap: .3rem;
      text-align: left;
      cursor: pointer;
      box-shadow: none;
    }

    .tree-run:hover {
      background: #dbeafe;
      border-color: var(--line);
    }

    .tree-run.is-selected {
      background: #fde68a;
      border-color: var(--line);
    }

    .tree-run-main { min-width: 0; flex: 1; }

    .tree-run-label {
      margin: 0;
      font-size: .8rem;
      line-height: 1.3;
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .tree-run-sub {
      margin: .06rem 0 0;
      color: var(--muted);
      font-size: .7rem;
      line-height: 1.25;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .add-button {
      width: 100%;
      background: #fde047;
    }

    .add-drawer {
      border: 2px solid var(--line);
      background: #ffffff;
      box-shadow: var(--shadow);
      padding: .5rem;
      display: grid;
      gap: .42rem;
    }

    .add-drawer.hidden { display: none; }

    .details {
      border: 2px solid var(--line);
      background: #f8fbff;
      padding: .55rem;
      box-shadow: var(--shadow);
      display: flex;
      flex-direction: column;
      gap: .35rem;
      max-height: 38dvh;
      overflow: hidden;
    }

    .details-header {
      display: flex;
      align-items: center;
      gap: .4rem;
    }

    .details-title {
      flex: 1;
      min-width: 0;
      margin: 0;
      font-size: .92rem;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .details-toggle {
      border: 1px solid var(--line);
      background: #fff;
      padding: .14rem .42rem;
      font-size: .72rem;
      box-shadow: none;
    }

    .details-body {
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: .32rem;
    }

    .details-body.hidden { display: none; }

    .poi-controls {
      display: flex;
      flex-direction: column;
      gap: .4rem;
      padding: .55rem .65rem;
      margin-bottom: .6rem;
      border: 1px solid var(--border, #e2e8f0);
      border-radius: 8px;
      font-size: .82rem;
    }
    .poi-toggle {
      display: flex;
      align-items: center;
      gap: .45rem;
      cursor: pointer;
    }
    .poi-radius {
      display: flex;
      flex-direction: column;
      gap: .25rem;
      color: var(--muted);
    }
    .poi-radius.disabled { opacity: .45; }
    .poi-radius input[type="range"] { width: 100%; }
    .details-poi { margin-top: .5rem; font-size: .8rem; }
    .details-poi .poi-group-start { color: #16a34a; font-weight: 600; }
    .details-poi .poi-group-end { color: #dc2626; font-weight: 600; }
    .details-poi ul { margin: .15rem 0 .5rem; padding-left: 1rem; }

    .details-meta {
      color: var(--muted);
      font-size: .8rem;
      line-height: 1.35;
    }

    .details-stats {
      display: flex;
      flex-wrap: wrap;
      gap: .28rem;
    }

    .stat-chip {
      border: 1px solid var(--line);
      background: #fff;
      padding: .14rem .42rem;
      font-size: .72rem;
      font-weight: 600;
      white-space: nowrap;
    }

    .step-list {
      margin: .15rem 0 0;
      padding-left: 1.05rem;
      font-size: .8rem;
      line-height: 1.4;
    }

    .step-list li { margin: .08rem 0; }

    .empty-hint {
      color: var(--muted);
      font-size: .82rem;
      line-height: 1.45;
    }

    .map-floating-status {
      position: absolute;
      bottom: 8px;
      left: 8px;
      z-index: 600;
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid var(--line);
      padding: .22rem .5rem;
      font-size: .72rem;
      color: var(--muted);
      pointer-events: none;
      max-width: calc(100% - 16px);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .map-floating-status.hidden { display: none; }

    @media (max-width: 980px) {
      .app-shell {
        grid-template-columns: 1fr;
        grid-template-rows: minmax(0, 1fr) 50dvh;
      }

      .map-pane { order: -1; }

      .sidebar {
        border-right: 0;
        border-top: 3px solid var(--line);
      }

      .details { max-height: 30dvh; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div id="modeBanner" class="mode-banner" data-mode="new">
        <span id="modeTag" class="mode-tag">New Run</span>
        <span id="modeTitle" class="mode-title">Enter a start and end</span>
        <button id="cancelEditButton" class="mode-cancel hidden" type="button">Cancel</button>
      </div>

      <form id="userRunForm" class="form-section">
        <div class="search-field">
          <input id="originInput" required autocomplete="off" placeholder="Start (e.g. King's Cross)">
          <div id="originSuggestions" class="suggestions"></div>
        </div>
        <div class="search-field">
          <input id="destinationInput" required autocomplete="off" placeholder="End (e.g. Tower Bridge)">
          <div id="destinationSuggestions" class="suggestions"></div>
        </div>
        <select id="folderSelect" aria-label="Save to folder"></select>
        <button id="saveRunButton" type="submit">Save Run</button>
      </form>
      <div id="userRunError" class="status-error"></div>

      <input id="blueBookSearch" class="filter-input" type="search" placeholder="Filter runs">
      <div id="runTree" class="tree"></div>

      <button id="addButton" class="add-button" type="button">+ New Folder</button>
      <div id="addDrawer" class="add-drawer hidden">
        <form id="folderForm" class="form-section">
          <input id="folderNameInput" required maxlength="80" placeholder="Folder name">
          <button class="secondary" type="submit">Create Folder</button>
        </form>
        <div id="folderError" class="status-error"></div>
      </div>

      <div id="poiControls" class="poi-controls">
        <label class="poi-toggle">
          <input id="poiHighlight" type="checkbox">
          Highlight Surrounding POIs
        </label>
        <div class="poi-radius">
          <span id="poiRadiusLabel">Search radius: 0.25 miles</span>
          <input id="poiRadius" type="range" min="0.05" max="1.0" step="0.05" value="0.25">
        </div>
      </div>

      <section id="runDetails" class="details">
        <header class="details-header">
          <p id="detailsTitle" class="details-title">No run selected</p>
          <button id="detailsToggle" class="details-toggle" type="button" aria-expanded="true">Hide</button>
        </header>
        <div id="detailsBody" class="details-body">
          <div class="empty-hint">Pick a run from the list to view its route, or save a new one above.</div>
        </div>
      </section>
    </aside>

    <section class="map-pane">
      <div id="map"></div>
      <div id="mapStatus" class="map-floating-status hidden"></div>
    </section>
  </div>

  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    crossorigin=""
  ></script>
  <script>
    const BLUE_BOOK_FOLDER_KEY = "blue-book-runs";
    const ROOT_USER_FOLDER_KEY = "__root_user__";
    const BLUE_BOOK_COLOR = "#1d4ed8";

    const state = {
      map: null,
      searchLayer: null,
      focusLayer: null,
      folderLayers: {},
      visibleFolders: {},
      collapsedFolders: {},
      selectedRun: null,
      blueBookSummaries: [],
      blueBookRunsById: {},
      blueBookAllLoaded: false,
      blueBookLoadInFlight: null,
      userRuns: [],
      userFolders: [],
      storageSource: "",
      blueBookSearch: "",
      searchSuggestions: { origin: [], destination: [] },
      searchSeq: { origin: 0, destination: 0 },
      debounceTimers: { origin: null, destination: null },
      detailsHidden: false,
      poiLayer: null,
      pois: [],
      poisLoaded: false,
      poiHighlightEnabled: false,
      poiRadiusMiles: 0.25,
      selectedRunPayload: null,
      poiMatches: null,
    };

    const POI_START_COLOR = "#16a34a"; // green: within radius of the run start
    const POI_END_COLOR = "#dc2626";   // red: within radius of the run end
    const METERS_PER_MILE = 1609.34;

    function escapeHtml(value) {
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function setMapStatus(message) {
      const el = document.getElementById("mapStatus");
      if (!el) return;
      if (!message) {
        el.classList.add("hidden");
        el.textContent = "";
        return;
      }
      el.textContent = message;
      el.classList.remove("hidden");
    }

    function initMap() {
      state.map = L.map("map", { zoomControl: true }).setView([51.5074, -0.1278], 11);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      }).addTo(state.map);

      state.searchLayer = L.layerGroup().addTo(state.map);
      state.focusLayer = L.layerGroup().addTo(state.map);
      state.poiLayer = L.layerGroup().addTo(state.map);

      window.addEventListener("resize", () => {
        state.map.invalidateSize();
      });
    }

    function getFolderLayer(folderKey) {
      if (!state.folderLayers[folderKey]) {
        state.folderLayers[folderKey] = L.layerGroup();
      }

      const layer = state.folderLayers[folderKey];
      const visible = Boolean(state.visibleFolders[folderKey]);

      if (visible && !state.map.hasLayer(layer)) {
        layer.addTo(state.map);
      }
      if (!visible && state.map.hasLayer(layer)) {
        state.map.removeLayer(layer);
      }

      return layer;
    }

    function syncFolderLayers() {
      const knownKeys = new Set([BLUE_BOOK_FOLDER_KEY, ROOT_USER_FOLDER_KEY]);
      state.userFolders.forEach((folder) => knownKeys.add(folder.id));
      Object.keys(state.folderLayers).forEach((folderKey) => knownKeys.add(folderKey));

      knownKeys.forEach((folderKey) => {
        getFolderLayer(folderKey);
      });
    }

    function ensureFolderVisibilityDefaults() {
      if (state.visibleFolders[BLUE_BOOK_FOLDER_KEY] === undefined) {
        state.visibleFolders[BLUE_BOOK_FOLDER_KEY] = false;
      }
      if (state.visibleFolders[ROOT_USER_FOLDER_KEY] === undefined) {
        state.visibleFolders[ROOT_USER_FOLDER_KEY] = true;
      }
      state.userFolders.forEach((folder) => {
        if (state.visibleFolders[folder.id] === undefined) {
          state.visibleFolders[folder.id] = folder.visible !== false;
        }
      });
    }

    function ensureFolderCollapseDefaults() {
      if (state.collapsedFolders[BLUE_BOOK_FOLDER_KEY] === undefined) {
        state.collapsedFolders[BLUE_BOOK_FOLDER_KEY] = false;
      }
      if (state.collapsedFolders[ROOT_USER_FOLDER_KEY] === undefined) {
        state.collapsedFolders[ROOT_USER_FOLDER_KEY] = false;
      }
      state.userFolders.forEach((folder) => {
        if (state.collapsedFolders[folder.id] === undefined) {
          state.collapsedFolders[folder.id] = false;
        }
      });
    }

    async function setFolderVisibility(folderKey, visible, rerender = true) {
      state.visibleFolders[folderKey] = Boolean(visible);

      if (folderKey === BLUE_BOOK_FOLDER_KEY && visible && !state.blueBookAllLoaded) {
        const loaded = await loadAllBlueBookRuns();
        if (!loaded) {
          state.visibleFolders[folderKey] = false;
        }
      }

      syncFolderLayers();
      if (rerender) {
        renderRunTree();
      }
    }

    function refreshSaveButtonLabel() {
      const saveButton = document.getElementById("saveRunButton");
      if (!saveButton) return;
      const isEditingUserRun = Boolean(state.selectedRun && state.selectedRun.source === "user");
      saveButton.textContent = isEditingUserRun ? "Save Changes" : "Save Run";
    }

    function refreshModeBanner() {
      const banner = document.getElementById("modeBanner");
      const tag = document.getElementById("modeTag");
      const title = document.getElementById("modeTitle");
      const cancel = document.getElementById("cancelEditButton");
      if (!banner || !tag || !title || !cancel) return;

      const selected = state.selectedRun;

      if (!selected) {
        banner.dataset.mode = "new";
        tag.textContent = "New Run";
        title.textContent = "Enter a start and end";
        cancel.classList.add("hidden");
        return;
      }

      if (selected.source === "user") {
        banner.dataset.mode = "edit";
        tag.textContent = "Editing";
        title.textContent = selected.title || ("Run " + selected.id);
        cancel.classList.remove("hidden");
        return;
      }

      banner.dataset.mode = "fork";
      tag.textContent = "Fork From";
      title.textContent = selected.title || ("Run " + selected.id);
      cancel.classList.remove("hidden");
    }

    function clearForm() {
      const originInput = document.getElementById("originInput");
      const destinationInput = document.getElementById("destinationInput");
      const folderSelect = document.getElementById("folderSelect");
      if (originInput) originInput.value = "";
      if (destinationInput) destinationInput.value = "";
      if (folderSelect) folderSelect.value = "";
      const errorEl = document.getElementById("userRunError");
      if (errorEl) errorEl.textContent = "";
      state.searchSuggestions.origin = [];
      state.searchSuggestions.destination = [];
      renderSuggestions("origin");
      renderSuggestions("destination");
      updateSearchPreviewMarkers();
    }

    function deselectRun() {
      state.selectedRun = null;
      state.selectedRunPayload = null;
      state.poiMatches = null;
      state.focusLayer.clearLayers();
      if (state.poiLayer) state.poiLayer.clearLayers();
      clearForm();
      refreshSaveButtonLabel();
      refreshModeBanner();
      renderDetails(null, "");
      renderRunTree();
    }

    function populateRunEditor(runPayload) {
      const originInput = document.getElementById("originInput");
      const destinationInput = document.getElementById("destinationInput");
      const folderSelect = document.getElementById("folderSelect");
      if (!originInput || !destinationInput) return;

      originInput.value = (((runPayload || {}).start || {}).name) || "";
      destinationInput.value = (((runPayload || {}).end || {}).name) || "";

      if (!folderSelect) return;

      if ((runPayload || {}).source === "user") {
        folderSelect.value = (runPayload || {}).folder_id || "";
      } else {
        folderSelect.value = "";
      }
    }

    function setSelectedRun(runPayload) {
      if (!runPayload) {
        state.selectedRun = null;
        refreshSaveButtonLabel();
        refreshModeBanner();
        return;
      }

      const source = String((runPayload || {}).source || "").toLowerCase() === "blue_book" ? "blue_book" : "user";
      state.selectedRun = {
        id: runPayload.id,
        source,
        folder_id: runPayload.folder_id || null,
        title: runPayload.title || null,
      };
      populateRunEditor(runPayload);
      refreshSaveButtonLabel();
      refreshModeBanner();
    }

    function colorForFolder(folderKey) {
      if (folderKey === BLUE_BOOK_FOLDER_KEY) return BLUE_BOOK_COLOR;
      if (folderKey === ROOT_USER_FOLDER_KEY) return "#0f766e";

      const palette = ["#2563eb", "#0f766e", "#dc2626", "#7c3aed", "#c2410c", "#0e7490", "#be123c"];
      const text = String(folderKey || "folder");
      let hash = 0;
      for (let i = 0; i < text.length; i += 1) {
        hash = (hash * 31 + text.charCodeAt(i)) % 2147483647;
      }
      return palette[Math.abs(hash) % palette.length];
    }

    function renderDetails(runPayload, tagLabel) {
      const titleEl = document.getElementById("detailsTitle");
      const bodyEl = document.getElementById("detailsBody");
      if (!titleEl || !bodyEl) return;

      if (!runPayload) {
        titleEl.textContent = "No run selected";
        bodyEl.innerHTML = '<div class="empty-hint">Pick a run from the list to view its route, or save a new one above.</div>';
        return;
      }

      const route = runPayload.route || {};
      const steps = Array.isArray(route.steps) ? route.steps : [];
      const distance = Number(route.distance || 0);
      const duration = Number(route.duration || 0);
      const startName = ((runPayload.start || {}).name) || "Unknown";
      const endName = ((runPayload.end || {}).name) || "Unknown";
      const runTitle = String(runPayload.title || "Untitled");

      titleEl.textContent = runTitle;

      const stepsBlock = steps.length
        ? '<ol class="step-list">' + steps.map((step) => '<li>' + escapeHtml(step.instruction || "") + '</li>').join("") + '</ol>'
        : '';

      bodyEl.innerHTML =
        '<div class="details-meta">' + escapeHtml(tagLabel || "") + '</div>' +
        '<div class="details-meta">' + escapeHtml(startName) + ' &rarr; ' + escapeHtml(endName) + '</div>' +
        '<div class="details-stats">' +
          '<span class="stat-chip">' + distance.toFixed(0) + ' m</span>' +
          '<span class="stat-chip">' + duration.toFixed(0) + ' s</span>' +
          '<span class="stat-chip">' + steps.length + ' step' + (steps.length === 1 ? '' : 's') + '</span>' +
        '</div>' +
        renderPoiMatchesBlock() +
        stepsBlock;
    }

    function renderPoiMatchesBlock() {
      const matches = state.poiMatches;
      if (!matches) return '';
      const group = (label, cls, items) =>
        '<div class="' + cls + '">' + label + ' (' + items.length + ')</div>' +
        (items.length
          ? '<ul>' + items.map((p) => '<li>' + escapeHtml(p.name || '') + '</li>').join('') + '</ul>'
          : '');
      return '<div class="details-poi">' +
        group('Start POIs', 'poi-group-start', matches.start) +
        group('End POIs', 'poi-group-end', matches.end) +
        '</div>';
    }

    function drawRunLayer(runPayload, targetLayer, color, tagLabel, onFocusClick) {
      const coords = ((((runPayload || {}).route || {}).geometry || {}).coordinates || []);
      if (!coords.length) return null;

      const latLngs = coords.map((point) => [point[1], point[0]]);
      const polyline = L.polyline(latLngs, {
        color,
        weight: 4,
        opacity: 0.55,
        lineJoin: "round",
      }).addTo(targetLayer);

      if (onFocusClick) polyline.on("click", onFocusClick);

      const start = (runPayload || {}).start || {};
      const end = (runPayload || {}).end || {};
      const startCoords = start.coordinates || [];
      const endCoords = end.coordinates || [];

      if (startCoords.length === 2) {
        const startMarker = L.circleMarker([startCoords[1], startCoords[0]], {
          radius: 4,
          color,
          fillColor: color,
          fillOpacity: 0.9,
          opacity: 0.9,
        }).addTo(targetLayer);
        startMarker.bindTooltip("Start: " + escapeHtml(start.name || "Unknown"));
        if (onFocusClick) startMarker.on("click", onFocusClick);
      }

      if (endCoords.length === 2) {
        const endMarker = L.circleMarker([endCoords[1], endCoords[0]], {
          radius: 4,
          color,
          fillColor: color,
          fillOpacity: 0.45,
          opacity: 0.95,
        }).addTo(targetLayer);
        endMarker.bindTooltip("End: " + escapeHtml(end.name || "Unknown"));
        if (onFocusClick) endMarker.on("click", onFocusClick);
      }

      return { bounds: polyline.getBounds(), tagLabel };
    }

    function focusRun(runPayload, tagLabel, color, fitBounds) {
      const shouldFit = fitBounds !== false;
      state.focusLayer.clearLayers();
      const drawn = drawRunLayer(runPayload, state.focusLayer, color, tagLabel, null);
      if (drawn) {
        state.focusLayer.eachLayer((layer) => {
          if (layer instanceof L.Polyline) {
            layer.setStyle({ weight: 7, opacity: 0.95 });
          }
          if (layer instanceof L.CircleMarker) {
            layer.setStyle({ radius: 7, fillOpacity: 0.95 });
          }
        });

        if (shouldFit && drawn.bounds && drawn.bounds.isValid()) {
          state.map.fitBounds(drawn.bounds, { padding: [34, 34] });
        }
      }

      state.selectedRunPayload = runPayload;
      state.selectedRunTag = tagLabel;
      renderPoiHighlight();
      renderDetails(runPayload, tagLabel);
      setSelectedRun(runPayload);
      renderRunTree();
    }

    async function ensurePoisLoaded() {
      if (state.poisLoaded) return;
      try {
        const resp = await fetch("/api/pois");
        const data = await resp.json();
        state.pois = Array.isArray(data.pois) ? data.pois : [];
      } catch (err) {
        state.pois = [];
      }
      state.poisLoaded = true;
    }

    function haversineMeters(lat1, lon1, lat2, lon2) {
      const R = 6371000;
      const toRad = (d) => (d * Math.PI) / 180;
      const dLat = toRad(lat2 - lat1);
      const dLon = toRad(lon2 - lon1);
      const a =
        Math.sin(dLat / 2) ** 2 +
        Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
      return 2 * R * Math.asin(Math.sqrt(a));
    }

    // Recompute the POI circles, markers and match lists for the selected run.
    function renderPoiHighlight() {
      if (state.poiLayer) state.poiLayer.clearLayers();
      state.poiMatches = null;
      const run = state.selectedRunPayload;
      if (!state.poiHighlightEnabled || !run) return;

      const start = (run.start || {}).coordinates;
      const end = (run.end || {}).coordinates;
      const radiusM = state.poiRadiusMiles * METERS_PER_MILE;
      const hasStart = Array.isArray(start) && start.length === 2;
      const hasEnd = Array.isArray(end) && end.length === 2;

      if (hasStart) {
        L.circle([start[1], start[0]], { radius: radiusM, color: POI_START_COLOR, weight: 1, fillOpacity: 0.05 }).addTo(state.poiLayer);
      }
      if (hasEnd) {
        L.circle([end[1], end[0]], { radius: radiusM, color: POI_END_COLOR, weight: 1, fillOpacity: 0.05 }).addTo(state.poiLayer);
      }

      const startMatches = [];
      const endMatches = [];
      for (const poi of state.pois) {
        const c = poi.coordinates;
        if (!Array.isArray(c) || c.length !== 2) continue;
        const lon = c[0];
        const lat = c[1];
        const inStart = hasStart && haversineMeters(lat, lon, start[1], start[0]) <= radiusM;
        const inEnd = hasEnd && haversineMeters(lat, lon, end[1], end[0]) <= radiusM;
        if (!inStart && !inEnd) continue;
        const color = inStart ? POI_START_COLOR : POI_END_COLOR;
        if (inStart) startMatches.push(poi);
        else endMatches.push(poi);
        const marker = L.circleMarker([lat, lon], {
          radius: 4,
          color,
          fillColor: color,
          fillOpacity: 0.8,
          weight: 1,
        }).addTo(state.poiLayer);
        marker.bindTooltip(escapeHtml(poi.name || ""));
      }
      state.poiMatches = { start: startMatches, end: endMatches };
    }

    // Toggle/slider entry point: lazily fetch the dataset, then redraw.
    async function refreshPoiHighlight() {
      if (state.poiHighlightEnabled) await ensurePoisLoaded();
      renderPoiHighlight();
      if (state.selectedRunPayload) {
        renderDetails(state.selectedRunPayload, state.selectedRunTag || "");
      }
    }

    function redrawUserFolderLayers() {
      const usedKeys = new Set([ROOT_USER_FOLDER_KEY, ...state.userFolders.map((folder) => folder.id)]);
      usedKeys.forEach((folderKey) => {
        const layer = getFolderLayer(folderKey);
        layer.clearLayers();
      });

      state.userRuns.forEach((run) => {
        const folderKey = run.folder_id || ROOT_USER_FOLDER_KEY;
        const color = colorForFolder(folderKey);
        const layer = getFolderLayer(folderKey);
        drawRunLayer(run, layer, color, "User", () => focusRun(run, folderName(folderKey), color, true));
      });

      syncFolderLayers();
    }

    function redrawBlueBookLayer() {
      const layer = getFolderLayer(BLUE_BOOK_FOLDER_KEY);
      layer.clearLayers();
      if (!state.blueBookAllLoaded) return;

      Object.values(state.blueBookRunsById).forEach((run) => {
        drawRunLayer(run, layer, BLUE_BOOK_COLOR, "blue book", () => focusRun(run, "blue book", BLUE_BOOK_COLOR, true));
      });

      syncFolderLayers();
    }

    function userRunsForFolder(folderIdOrRoot) {
      if (folderIdOrRoot === ROOT_USER_FOLDER_KEY) {
        return state.userRuns.filter((run) => !run.folder_id);
      }
      return state.userRuns.filter((run) => String(run.folder_id || "") === String(folderIdOrRoot || ""));
    }

    function folderName(folderKey) {
      if (folderKey === BLUE_BOOK_FOLDER_KEY) return "Blue Book";
      if (folderKey === ROOT_USER_FOLDER_KEY) return "My Runs";
      const folder = state.userFolders.find((item) => item.id === folderKey);
      return folder ? folder.name : "folder";
    }

    function renderFolderSelect() {
      const selectEl = document.getElementById("folderSelect");
      const previous = selectEl.value;
      const options = [
        '<option value="">Save to: My Runs (root)</option>',
        ...state.userFolders
          .slice()
          .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")))
          .map((folder) => '<option value="' + escapeHtml(folder.id) + '">Save to: ' + escapeHtml(folder.name) + '</option>'),
      ];
      selectEl.innerHTML = options.join("");
      if (["", ...state.userFolders.map((folder) => folder.id)].includes(previous)) {
        selectEl.value = previous;
      }
    }

    function isRunSelected(source, runId) {
      const sel = state.selectedRun;
      if (!sel) return false;
      return sel.source === source && String(sel.id) === String(runId);
    }

    function renderBlueBookRows() {
      const term = state.blueBookSearch.trim().toLowerCase();
      const filtered = state.blueBookSummaries.filter((run) => {
        if (!term) return true;
        const hay = (run.id + " " + run.title + " " + run.start_name + " " + run.end_name).toLowerCase();
        return hay.includes(term);
      });

      if (!filtered.length) {
        return '<div class="tree-empty">No matching blue book runs.</div>';
      }

      return filtered
        .map((run) => {
          const selected = isRunSelected("blue_book", run.id) ? " is-selected" : "";
          return (
            '<div class="tree-run-wrap">' +
              '<button class="tree-run' + selected + '" type="button" data-action="show-blue" data-run-id="' + escapeHtml(run.id) + '">' +
                '<span class="tree-run-main">' +
                  '<p class="tree-run-label">' + escapeHtml(run.title || ("Run " + run.id)) + '</p>' +
                  '<p class="tree-run-sub">' + escapeHtml(run.start_name) + ' &rarr; ' + escapeHtml(run.end_name) + '</p>' +
                '</span>' +
              '</button>' +
            '</div>'
          );
        })
        .join("");
    }

    function renderUserRows(runs) {
      if (!runs.length) {
        return '<div class="tree-empty">No runs in this folder.</div>';
      }

      return runs
        .map((run) => {
          const selected = isRunSelected("user", run.id) ? " is-selected" : "";
          return (
            '<div class="tree-run-wrap">' +
              '<button class="tree-run' + selected + '" type="button" data-action="show-user" data-user-id="' + escapeHtml(run.id) + '">' +
                '<span class="tree-run-main">' +
                  '<p class="tree-run-label">' + escapeHtml(run.title || ("Run " + run.id)) + '</p>' +
                  '<p class="tree-run-sub">' + escapeHtml((run.start || {}).name || "Unknown") + ' &rarr; ' + escapeHtml((run.end || {}).name || "Unknown") + '</p>' +
                '</span>' +
              '</button>' +
              '<button class="row-action" type="button" data-action="delete-user" data-user-id="' + escapeHtml(run.id) + '" title="Delete run">&times;</button>' +
            '</div>'
          );
        })
        .join("");
    }

    function renderFolderNode(folderKey, label, count, childrenHtml, options) {
      const opts = options || {};
      const collapsed = Boolean(state.collapsedFolders[folderKey]);
      const visible = Boolean(state.visibleFolders[folderKey]);
      const meta = opts.meta || (count + " run" + (count === 1 ? "" : "s"));
      const builtin = opts.builtin === true;
      const actions = opts.actions || "";

      return (
        '<section class="tree-folder" data-builtin="' + (builtin ? "true" : "false") + '">' +
          '<div class="tree-folder-row">' +
            '<button class="disclosure" type="button" data-action="toggle-collapse" data-folder-key="' + escapeHtml(folderKey) + '" aria-label="Expand or collapse">' + (collapsed ? '▸' : '▾') + '</button>' +
            '<p class="tree-folder-label">' + escapeHtml(label) + '</p>' +
            '<span class="tree-folder-meta">' + escapeHtml(meta) + '</span>' +
            actions +
            '<label class="tree-toggle">' +
              '<input type="checkbox" data-action="toggle-folder" data-folder-key="' + escapeHtml(folderKey) + '" ' + (visible ? "checked" : "") + '>' +
              'Show' +
            '</label>' +
          '</div>' +
          '<div class="tree-folder-children ' + (collapsed ? "hidden" : "") + '">' +
            childrenHtml +
          '</div>' +
        '</section>'
      );
    }

    function renderRunTree() {
      const treeEl = document.getElementById("runTree");
      ensureFolderVisibilityDefaults();
      ensureFolderCollapseDefaults();

      const blueBookRows = renderBlueBookRows();
      const rootRuns = userRunsForFolder(ROOT_USER_FOLDER_KEY);
      const rootRows = renderUserRows(rootRuns);

      const blueBookNode = renderFolderNode(
        BLUE_BOOK_FOLDER_KEY,
        "Blue Book",
        state.blueBookSummaries.length,
        blueBookRows,
        { meta: state.blueBookSummaries.length + " runs", builtin: true }
      );

      const rootNode = renderFolderNode(
        ROOT_USER_FOLDER_KEY,
        "My Runs",
        rootRuns.length,
        rootRows,
        { meta: rootRuns.length + " run" + (rootRuns.length === 1 ? "" : "s"), builtin: true }
      );

      const userFolderNodes = state.userFolders
        .slice()
        .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")))
        .map((folder) => {
          const folderRuns = userRunsForFolder(folder.id);
          const actions =
            '<button class="row-action rename" type="button" data-action="rename-folder" data-folder-key="' + escapeHtml(folder.id) + '" title="Rename folder">&#9998;</button>' +
            '<button class="row-action" type="button" data-action="delete-folder" data-folder-key="' + escapeHtml(folder.id) + '" title="Delete folder">&times;</button>';
          return renderFolderNode(folder.id, folder.name, folderRuns.length, renderUserRows(folderRuns), { actions });
        })
        .join("");

      const isCompletelyEmpty = !state.userRuns.length && !state.userFolders.length && !state.blueBookSummaries.length;
      if (isCompletelyEmpty) {
        treeEl.innerHTML = '<div class="tree-empty">No runs yet. Enter a start and end above, then click Save Run to create your first one.</div>';
        return;
      }

      treeEl.innerHTML = blueBookNode + rootNode + userFolderNodes;
    }

    async function ensureBlueBookRunLoaded(runId) {
      const key = String(runId);
      if (state.blueBookRunsById[key]) return state.blueBookRunsById[key];

      const response = await fetch("/api/bluebook/runs/" + encodeURIComponent(runId) + "?direction=forward");
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Unable to load blue book run.");
      state.blueBookRunsById[key] = payload;
      return payload;
    }

    async function loadAllBlueBookRuns() {
      if (state.blueBookLoadInFlight) return state.blueBookLoadInFlight;

      const promise = (async () => {
        setMapStatus("Loading blue book route geometry...");
        const response = await fetch("/api/bluebook/runs/all");
        const payload = await response.json();

        if (!response.ok) {
          setMapStatus(payload.error || "Failed to load blue book route geometry.");
          return false;
        }

        state.blueBookRunsById = {};
        (payload.runs || []).forEach((run) => {
          state.blueBookRunsById[String(run.id)] = run;
        });
        state.blueBookAllLoaded = true;

        const loadedCount = Object.keys(state.blueBookRunsById).length;
        setMapStatus("Loaded geometry for " + loadedCount + " blue book runs.");
        setTimeout(() => setMapStatus(""), 3000);

        redrawBlueBookLayer();
        return true;
      })();

      state.blueBookLoadInFlight = promise;
      try {
        return await promise;
      } finally {
        state.blueBookLoadInFlight = null;
      }
    }

    async function loadBlueBookSummaries() {
      const response = await fetch("/api/bluebook/runs");
      const payload = await response.json();

      if (!response.ok) {
        state.blueBookSummaries = [];
        renderRunTree();
        return;
      }

      state.blueBookSummaries = payload.runs || [];
      renderRunTree();
    }

    async function loadUserRunsAndFolders() {
      const response = await fetch("/api/user-runs");
      const payload = await response.json();

      if (!response.ok) {
        document.getElementById("userRunError").textContent = payload.error || "Failed to load user runs.";
        return;
      }

      state.userRuns = Array.isArray(payload.runs) ? payload.runs : [];
      state.userFolders = Array.isArray(payload.folders) ? payload.folders : [];
      state.storageSource = payload.source || "";

      ensureFolderVisibilityDefaults();
      renderFolderSelect();
      redrawUserFolderLayers();
      renderRunTree();
    }

    function renderSuggestions(kind) {
      const container = document.getElementById(kind === "origin" ? "originSuggestions" : "destinationSuggestions");
      const options = state.searchSuggestions[kind] || [];

      if (!options.length) {
        container.classList.remove("show");
        container.innerHTML = "";
        return;
      }

      container.innerHTML = options
        .map((result, index) =>
          '<button class="suggestion-item" type="button" data-action="pick-suggestion" data-kind="' + kind + '" data-index="' + index + '">' + escapeHtml(result.name || "Unnamed location") + '</button>'
        )
        .join("");
      container.classList.add("show");
    }

    function updateSearchPreviewMarkers() {
      state.searchLayer.clearLayers();
      const previewPoints = [];

      const byKind = [
        { key: "origin", color: "#0f766e" },
        { key: "destination", color: "#b91c1c" },
      ];

      byKind.forEach(({ key, color }) => {
        (state.searchSuggestions[key] || []).forEach((result) => {
          const coords = result.coordinates || [];
          if (!Array.isArray(coords) || coords.length !== 2) return;

          const latLng = [coords[1], coords[0]];
          previewPoints.push(latLng);

          const marker = L.circleMarker([coords[1], coords[0]], {
            radius: 6,
            color,
            fillColor: color,
            fillOpacity: 0.82,
            opacity: 0.95,
          }).addTo(state.searchLayer);
          marker.bindTooltip(key + ": " + (result.name || "location"));
        });
      });

      if (!previewPoints.length) return;

      const bounds = L.latLngBounds(previewPoints);
      if (bounds.isValid()) {
        state.map.fitBounds(bounds, {
          padding: [52, 52],
          maxZoom: 15,
          animate: true,
        });
      }
    }

    async function searchLocations(kind, query) {
      const normalized = String(query || "").trim();
      const seq = (state.searchSeq[kind] || 0) + 1;
      state.searchSeq[kind] = seq;

      if (normalized.length < 2) {
        state.searchSuggestions[kind] = [];
        renderSuggestions(kind);
        updateSearchPreviewMarkers();
        return;
      }

      const response = await fetch("/api/locations/search?q=" + encodeURIComponent(normalized) + "&limit=6");
      const payload = await response.json();

      if (seq !== state.searchSeq[kind]) return;

      if (!response.ok) {
        state.searchSuggestions[kind] = [];
        renderSuggestions(kind);
        updateSearchPreviewMarkers();
        return;
      }

      state.searchSuggestions[kind] = Array.isArray(payload.results) ? payload.results : [];
      renderSuggestions(kind);
      updateSearchPreviewMarkers();
    }

    function wireLocationSearch(inputId, kind) {
      const inputEl = document.getElementById(inputId);

      inputEl.addEventListener("input", () => {
        const value = inputEl.value;
        clearTimeout(state.debounceTimers[kind]);
        state.debounceTimers[kind] = setTimeout(() => {
          searchLocations(kind, value);
        }, 250);
      });

      inputEl.addEventListener("blur", () => {
        setTimeout(() => {
          const container = document.getElementById(kind === "origin" ? "originSuggestions" : "destinationSuggestions");
          container.classList.remove("show");
        }, 160);
      });

      inputEl.addEventListener("focus", () => {
        renderSuggestions(kind);
      });
    }

    async function deleteUserRun(runId) {
      if (!window.confirm("Delete this run? This cannot be undone.")) return;
      const response = await fetch("/api/user-runs/" + encodeURIComponent(runId), { method: "DELETE" });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        document.getElementById("userRunError").textContent = payload.error || "Could not delete run.";
        return;
      }
      state.userRuns = state.userRuns.filter((run) => String(run.id) !== String(runId));
      if (state.selectedRun && state.selectedRun.source === "user" && String(state.selectedRun.id) === String(runId)) {
        deselectRun();
      } else {
        redrawUserFolderLayers();
        renderRunTree();
      }
    }

    async function renameFolder(folderId) {
      const folder = state.userFolders.find((item) => item.id === folderId);
      const current = folder ? folder.name : "";
      const next = window.prompt("Rename folder:", current);
      if (next === null) return;
      const trimmed = String(next).trim();
      if (!trimmed || trimmed === current) return;

      const response = await fetch("/api/folders/" + encodeURIComponent(folderId), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: trimmed }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        document.getElementById("userRunError").textContent = payload.error || "Could not rename folder.";
        return;
      }
      state.userFolders = state.userFolders.map((item) => (item.id === folderId ? payload.folder : item));
      renderFolderSelect();
      renderRunTree();
    }

    async function deleteFolder(folderId) {
      const folder = state.userFolders.find((item) => item.id === folderId);
      const label = folder ? folder.name : folderId;
      const folderRunCount = state.userRuns.filter((run) => run.folder_id === folderId).length;
      const message = folderRunCount > 0
        ? 'Delete folder "' + label + '"? Its ' + folderRunCount + ' run(s) will move to My Runs.'
        : 'Delete folder "' + label + '"?';
      if (!window.confirm(message)) return;

      const response = await fetch("/api/folders/" + encodeURIComponent(folderId), { method: "DELETE" });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        document.getElementById("userRunError").textContent = payload.error || "Could not delete folder.";
        return;
      }

      state.userFolders = state.userFolders.filter((item) => item.id !== folderId);
      state.userRuns = state.userRuns.map((run) => (run.folder_id === folderId ? { ...run, folder_id: null } : run));
      delete state.visibleFolders[folderId];
      delete state.collapsedFolders[folderId];
      if (state.folderLayers[folderId]) {
        state.folderLayers[folderId].clearLayers();
        if (state.map.hasLayer(state.folderLayers[folderId])) {
          state.map.removeLayer(state.folderLayers[folderId]);
        }
        delete state.folderLayers[folderId];
      }
      renderFolderSelect();
      redrawUserFolderLayers();
      renderRunTree();
    }

    function wireRunTreeEvents() {
      const treeEl = document.getElementById("runTree");

      treeEl.addEventListener("change", async (event) => {
        const target = event.target;
        if (!(target instanceof HTMLInputElement)) return;
        if (target.dataset.action !== "toggle-folder") return;

        const folderKey = target.dataset.folderKey;
        const visible = target.checked;

        await setFolderVisibility(folderKey, visible);

        if (folderKey === BLUE_BOOK_FOLDER_KEY && visible && !state.blueBookAllLoaded) {
          target.checked = false;
        }
      });

      treeEl.addEventListener("click", async (event) => {
        const button = event.target.closest("button[data-action]");
        if (!button) return;

        const action = button.dataset.action;
        const userError = document.getElementById("userRunError");
        userError.textContent = "";

        if (action === "toggle-collapse") {
          const folderKey = button.dataset.folderKey;
          state.collapsedFolders[folderKey] = !Boolean(state.collapsedFolders[folderKey]);
          renderRunTree();
          return;
        }

        if (action === "show-blue") {
          const runId = button.dataset.runId;
          try {
            const run = await ensureBlueBookRunLoaded(runId);
            focusRun(run, "Blue Book", BLUE_BOOK_COLOR, true);
          } catch (error) {
            userError.textContent = error.message || "Unable to load blue book run.";
          }
          return;
        }

        if (action === "show-user") {
          const runId = button.dataset.userId;
          const run = state.userRuns.find((item) => String(item.id) === String(runId));
          if (!run) return;
          const folderKey = run.folder_id || ROOT_USER_FOLDER_KEY;
          focusRun(run, folderName(folderKey), colorForFolder(folderKey), true);
          return;
        }

        if (action === "delete-user") {
          event.stopPropagation();
          await deleteUserRun(button.dataset.userId);
          return;
        }

        if (action === "rename-folder") {
          event.stopPropagation();
          await renameFolder(button.dataset.folderKey);
          return;
        }

        if (action === "delete-folder") {
          event.stopPropagation();
          await deleteFolder(button.dataset.folderKey);
          return;
        }
      });
    }

    function wireSuggestionEvents() {
      document.querySelector(".sidebar").addEventListener("click", (event) => {
        const button = event.target.closest("button[data-action='pick-suggestion']");
        if (!button) return;

        const kind = button.dataset.kind;
        const index = Number(button.dataset.index || -1);
        const options = state.searchSuggestions[kind] || [];
        const selected = options[index];
        if (!selected) return;

        const inputEl = document.getElementById(kind === "origin" ? "originInput" : "destinationInput");
        inputEl.value = selected.name || "";
        state.searchSuggestions[kind] = [selected];
        renderSuggestions(kind);
        updateSearchPreviewMarkers();

        const coords = selected.coordinates || [];
        if (coords.length === 2) {
          state.map.panTo([coords[1], coords[0]], { animate: true });
        }
      });
    }

    function wireBlueBookSearch() {
      const searchInput = document.getElementById("blueBookSearch");
      searchInput.addEventListener("input", () => {
        state.blueBookSearch = searchInput.value || "";
        renderRunTree();
      });
    }

    function wireAddDrawer() {
      const addButton = document.getElementById("addButton");
      const drawer = document.getElementById("addDrawer");

      addButton.addEventListener("click", () => {
        drawer.classList.toggle("hidden");
        if (!drawer.classList.contains("hidden")) {
          const folderInput = document.getElementById("folderNameInput");
          if (folderInput) folderInput.focus();
        }
      });

      document.addEventListener("click", (event) => {
        if (drawer.classList.contains("hidden")) return;
        const target = event.target;
        if (!(target instanceof Node)) return;
        if (drawer.contains(target) || addButton.contains(target)) return;
        drawer.classList.add("hidden");
      });

      document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") drawer.classList.add("hidden");
      });
    }

    function wireGenerateRunForm() {
      const form = document.getElementById("userRunForm");
      const errorEl = document.getElementById("userRunError");

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        errorEl.textContent = "";

        const origin = document.getElementById("originInput").value.trim();
        const destination = document.getElementById("destinationInput").value.trim();
        const folderIdValue = document.getElementById("folderSelect").value;
        const folderId = folderIdValue ? folderIdValue : null;
        const selected = state.selectedRun;
        const updatingSelectedUserRun = Boolean(
          selected && selected.source === "user" && selected.id !== null && selected.id !== undefined
        );

        if (!origin || !destination) {
          errorEl.textContent = "Both origin and destination are required.";
          return;
        }

        const endpoint = updatingSelectedUserRun
          ? "/api/user-runs/" + encodeURIComponent(selected.id)
          : "/api/run";
        const response = await fetch(endpoint, {
          method: updatingSelectedUserRun ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ origin, destination, folder_id: folderId }),
        });
        const payload = await response.json();

        if (!response.ok) {
          errorEl.textContent = payload.error || "Could not generate route.";
          return;
        }

        if (updatingSelectedUserRun) {
          let replaced = false;
          state.userRuns = state.userRuns.map((item) => {
            if (String(item.id) !== String(payload.id)) return item;
            replaced = true;
            return payload;
          });
          if (!replaced) {
            state.userRuns = [payload, ...state.userRuns];
          }
        } else {
          state.userRuns = [payload, ...state.userRuns.filter((item) => item.id !== payload.id)];
        }
        redrawUserFolderLayers();

        const folderKey = payload.folder_id || ROOT_USER_FOLDER_KEY;
        focusRun(payload, folderName(folderKey), colorForFolder(folderKey), true);

        state.searchSuggestions.origin = [];
        state.searchSuggestions.destination = [];
        renderSuggestions("origin");
        renderSuggestions("destination");
        updateSearchPreviewMarkers();
      });
    }

    function wireCancelEdit() {
      const button = document.getElementById("cancelEditButton");
      if (!button) return;
      button.addEventListener("click", () => {
        deselectRun();
      });
    }

    function wireCreateFolderForm() {
      const form = document.getElementById("folderForm");
      const input = document.getElementById("folderNameInput");
      const errorEl = document.getElementById("folderError");

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        errorEl.textContent = "";

        const name = input.value.trim();
        if (!name) {
          errorEl.textContent = "Folder name is required.";
          return;
        }

        const response = await fetch("/api/folders", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        const payload = await response.json();

        if (!response.ok) {
          errorEl.textContent = payload.error || "Could not create folder.";
          return;
        }

        state.userFolders = [...state.userFolders, payload.folder];
        state.visibleFolders[payload.folder.id] = payload.folder.visible !== false;
        renderFolderSelect();
        renderRunTree();
        input.value = "";
        document.getElementById("addDrawer").classList.add("hidden");
      });
    }

    function wireDetailsToggle() {
      const button = document.getElementById("detailsToggle");
      const body = document.getElementById("detailsBody");
      if (!button || !body) return;
      button.addEventListener("click", () => {
        state.detailsHidden = !state.detailsHidden;
        body.classList.toggle("hidden", state.detailsHidden);
        button.textContent = state.detailsHidden ? "Show" : "Hide";
        button.setAttribute("aria-expanded", state.detailsHidden ? "false" : "true");
      });
    }

    function wirePoiControls() {
      const checkbox = document.getElementById("poiHighlight");
      const slider = document.getElementById("poiRadius");
      const label = document.getElementById("poiRadiusLabel");
      const radiusWrap = document.querySelector(".poi-radius");
      const updateLabel = () => {
        if (label) label.textContent = "Search radius: " + state.poiRadiusMiles.toFixed(2) + " miles";
        if (radiusWrap) radiusWrap.classList.toggle("disabled", !state.poiHighlightEnabled);
      };
      updateLabel();
      if (checkbox) {
        checkbox.addEventListener("change", () => {
          state.poiHighlightEnabled = checkbox.checked;
          updateLabel();
          refreshPoiHighlight();
        });
      }
      if (slider) {
        slider.addEventListener("input", () => {
          state.poiRadiusMiles = parseFloat(slider.value);
          updateLabel();
          if (state.poiHighlightEnabled) refreshPoiHighlight();
        });
      }
    }

    async function boot() {
      initMap();
      wireAddDrawer();
      wireRunTreeEvents();
      wireSuggestionEvents();
      wireBlueBookSearch();
      wireGenerateRunForm();
      wireCreateFolderForm();
      wireCancelEdit();
      wireDetailsToggle();
      wirePoiControls();
      wireLocationSearch("originInput", "origin");
      wireLocationSearch("destinationInput", "destination");

      await Promise.all([loadBlueBookSummaries(), loadUserRunsAndFolders()]);
      syncFolderLayers();
      renderDetails(null, "");
      setSelectedRun(null);
      refreshSaveButtonLabel();
      refreshModeBanner();

      setTimeout(() => {
        state.map.invalidateSize();
      }, 40);
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


def _normalize_folder_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()


def _slugify_folder_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "folder"


def _empty_user_store() -> dict[str, Any]:
    return {
        "version": 2,
        "folders": [],
        "runs": [],
    }


def _coerce_user_store(payload: Any) -> dict[str, Any]:
    raw_folders: list[Any]
    raw_runs: list[Any]

    if isinstance(payload, list):
        raw_folders = []
        raw_runs = payload
    elif isinstance(payload, dict):
        raw_folders = payload.get("folders") if isinstance(payload.get("folders"), list) else []
        raw_runs = payload.get("runs") if isinstance(payload.get("runs"), list) else []
    else:
        raw_folders = []
        raw_runs = []

    folders: list[dict[str, Any]] = []
    folder_ids: set[str] = set()

    for index, folder in enumerate(raw_folders, start=1):
        if not isinstance(folder, dict):
            continue

        name = _normalize_folder_name(str(folder.get("name") or folder.get("title") or ""))
        if not name:
            continue

        base_id = str(folder.get("id") or _slugify_folder_id(name)).strip().lower()
        if not base_id or base_id in {BLUE_BOOK_FOLDER_KEY, ROOT_USER_FOLDER_KEY}:
            base_id = f"folder-{index}"

        folder_id = base_id
        suffix = 2
        while folder_id in folder_ids:
            folder_id = f"{base_id}-{suffix}"
            suffix += 1

        folder_ids.add(folder_id)

        folders.append(
            {
                "id": folder_id,
                "name": name,
                "visible": bool(folder.get("visible", True)),
                "created_at": str(folder.get("created_at") or datetime.now(timezone.utc).isoformat()),
            }
        )

    runs: list[dict[str, Any]] = []
    used_run_ids: set[int] = set()
    fallback_id = 1

    for run in raw_runs:
        if not isinstance(run, dict):
            continue

        run_id = _normalize_run_id(run.get("id"), fallback=fallback_id)
        fallback_id += 1

        while run_id in used_run_ids:
            run_id += 1
        used_run_ids.add(run_id)

        folder_id_raw = run.get("folder_id")
        folder_id = None
        if folder_id_raw is not None:
            candidate = str(folder_id_raw).strip().lower()
            if candidate in folder_ids:
                folder_id = candidate

        normalized_run = dict(run)
        normalized_run["id"] = run_id
        normalized_run["folder_id"] = folder_id
        runs.append(normalized_run)

    runs.sort(key=lambda item: _normalize_run_id(item.get("id"), 0), reverse=True)

    return {
        "version": 2,
        "folders": folders,
        "runs": runs,
    }


def _ensure_user_store_loaded() -> dict[str, Any]:
    global _USER_STORE_CACHE

    if _USER_STORE_CACHE is None:
        with _USER_STORE_LOCK:
            if _USER_STORE_CACHE is None:
                user_runs_path = Path(app.config["USER_RUNS_FILE"])
                if user_runs_path.exists():
                    payload = _read_json(user_runs_path, default=_empty_user_store())
                    _USER_STORE_CACHE = _coerce_user_store(payload)
                else:
                    _USER_STORE_CACHE = _empty_user_store()

    return _USER_STORE_CACHE


def _save_user_store() -> None:
    user_runs_path = Path(app.config["USER_RUNS_FILE"])
    user_runs_path.parent.mkdir(parents=True, exist_ok=True)
    with user_runs_path.open("w") as handle:
        json.dump(_ensure_user_store_loaded(), handle, indent=2)


def _next_folder_id(name: str, existing_ids: set[str]) -> str:
    base = _slugify_folder_id(name)
    folder_id = base
    suffix = 2

    while folder_id in existing_ids or folder_id in {BLUE_BOOK_FOLDER_KEY, ROOT_USER_FOLDER_KEY}:
        folder_id = f"{base}-{suffix}"
        suffix += 1

    return folder_id


def _create_user_folder(name: str) -> dict[str, Any]:
    normalized_name = _normalize_folder_name(name)
    if not normalized_name:
        raise ValueError("Folder name is required.")

    with _USER_STORE_LOCK:
        store = _ensure_user_store_loaded()
        existing_ids = {str(folder.get("id")) for folder in store["folders"] if isinstance(folder, dict)}
        folder_id = _next_folder_id(normalized_name, existing_ids)
        folder = {
            "id": folder_id,
            "name": normalized_name,
            "visible": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        store["folders"].append(folder)
        _save_user_store()
        return folder


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
                "route": {
                    "geometry": {"type": "LineString", "coordinates": []},
                    "distance": 0,
                    "duration": 0,
                    "steps": [],
                },
                "routeReverse": {
                    "geometry": {"type": "LineString", "coordinates": []},
                    "distance": 0,
                    "duration": 0,
                    "steps": [],
                },
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
        if _BLUE_BOOK_CACHE["path"] == str(blue_book_path) and _BLUE_BOOK_CACHE["mtime"] == mtime:
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
        fallback_geometry = (
            fallback_route.get("geometry") if isinstance(fallback_route.get("geometry"), dict) else {}
        )
        fallback_coords = (
            fallback_geometry.get("coordinates")
            if isinstance(fallback_geometry.get("coordinates"), list)
            else []
        )
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
        "folder_id": BLUE_BOOK_FOLDER_KEY,
        "start": {
            "name": payload_start.get("name") or "Unknown",
            "coordinates": payload_start.get("coordinates")
            or (route_coords[0] if route_coords else []),
        },
        "end": {
            "name": payload_end.get("name") or "Unknown",
            "coordinates": payload_end.get("coordinates")
            or (route_coords[-1] if route_coords else []),
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


def _make_user_run(origin: str, destination: str, folder_id: str | None = None) -> dict[str, Any]:
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

    with _USER_STORE_LOCK:
        store = _ensure_user_store_loaded()
        runs = store["runs"]
        folders = store["folders"]
        valid_folder_ids = {str(item.get("id")) for item in folders if isinstance(item, dict)}

        normalized_folder_id = None
        if folder_id is not None:
            candidate = str(folder_id).strip().lower()
            if candidate:
                if candidate not in valid_folder_ids:
                    return {"error": f"Folder '{folder_id}' does not exist."}
                normalized_folder_id = candidate

        next_id = max([_normalize_run_id(run.get("id"), 0) for run in runs], default=0) + 1

        run_obj = {
            "id": next_id,
            "title": f"{origin} to {destination}",
            "source": "user",
            "folder_id": normalized_folder_id,
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

        runs.insert(0, run_obj)
        _save_user_store()

    return run_obj


def _update_user_run(run_id: int, origin: str, destination: str, folder_id: str | None = None) -> dict[str, Any]:
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

    with _USER_STORE_LOCK:
        store = _ensure_user_store_loaded()
        runs = store["runs"]
        folders = store["folders"]
        valid_folder_ids = {str(item.get("id")) for item in folders if isinstance(item, dict)}

        normalized_folder_id = None
        if folder_id is not None:
            candidate = str(folder_id).strip().lower()
            if candidate:
                if candidate not in valid_folder_ids:
                    return {"error": f"Folder '{folder_id}' does not exist."}
                normalized_folder_id = candidate

        existing_index = next(
            (index for index, run in enumerate(runs) if _normalize_run_id(run.get("id"), 0) == run_id),
            None,
        )
        if existing_index is None:
            return {"error": f"User run {run_id} not found."}

        existing_run = runs[existing_index]
        updated_run = {
            **existing_run,
            "id": run_id,
            "title": f"{origin} to {destination}",
            "source": "user",
            "folder_id": normalized_folder_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
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

        runs[existing_index] = updated_run
        _save_user_store()

    return updated_run


def _delete_user_run(run_id: int) -> dict[str, Any]:
    with _USER_STORE_LOCK:
        store = _ensure_user_store_loaded()
        runs = store["runs"]
        index = next(
            (idx for idx, run in enumerate(runs) if _normalize_run_id(run.get("id"), 0) == run_id),
            None,
        )
        if index is None:
            return {"error": f"User run {run_id} not found."}
        runs.pop(index)
        _save_user_store()
        return {"deleted": run_id}


def _rename_user_folder(folder_id: str, new_name: str) -> dict[str, Any]:
    normalized_name = _normalize_folder_name(new_name)
    if not normalized_name:
        return {"error": "Folder name is required."}

    with _USER_STORE_LOCK:
        store = _ensure_user_store_loaded()
        folder = next(
            (item for item in store["folders"] if isinstance(item, dict) and str(item.get("id")) == str(folder_id)),
            None,
        )
        if folder is None:
            return {"error": f"Folder '{folder_id}' not found."}
        folder["name"] = normalized_name
        _save_user_store()
        return {"folder": folder}


def _delete_user_folder(folder_id: str) -> dict[str, Any]:
    with _USER_STORE_LOCK:
        store = _ensure_user_store_loaded()
        folders = store["folders"]
        index = next(
            (idx for idx, folder in enumerate(folders) if isinstance(folder, dict) and str(folder.get("id")) == str(folder_id)),
            None,
        )
        if index is None:
            return {"error": f"Folder '{folder_id}' not found."}
        folders.pop(index)

        for run in store["runs"]:
            if isinstance(run, dict) and str(run.get("folder_id") or "") == str(folder_id):
                run["folder_id"] = None

        _save_user_store()
        return {"deleted": folder_id}


def _search_locations(query: str, limit: int = 6) -> list[dict[str, Any]]:
    cleaned_query = _normalize_folder_name(query)
    if len(cleaned_query) < 2:
        return []

    max_results = max(1, min(int(limit), 10))
    geolocator = Nominatim(user_agent="knowledge_run_generator_search", timeout=8)

    search_terms = [
        f"{cleaned_query}, London, UK",
        cleaned_query,
    ]

    results: list[dict[str, Any]] = []
    seen_coords: set[tuple[float, float]] = set()

    for term in search_terms:
        try:
            matches = geolocator.geocode(
                term,
                exactly_one=False,
                limit=max_results,
                country_codes="gb",
                addressdetails=False,
            )
        except (GeocoderTimedOut, GeocoderServiceError, ValueError):
            matches = None

        if not matches:
            continue

        match_list = matches if isinstance(matches, list) else [matches]

        for location in match_list:
            if not location:
                continue

            lat = float(location.latitude)
            lon = float(location.longitude)
            dedupe_key = (round(lat, 6), round(lon, 6))
            if dedupe_key in seen_coords:
                continue
            seen_coords.add(dedupe_key)

            results.append(
                {
                    "name": str(location.address or cleaned_query),
                    "coordinates": [lon, lat],
                }
            )

            if len(results) >= max_results:
                return results

    return results


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


@app.get("/api/bluebook/runs/all")
def blue_book_runs_all():
    loaded = _load_blue_book_runs()
    runs_by_id = loaded["by_id"]

    payload_runs = []
    for run_id in sorted(runs_by_id):
        payload = _make_direction_payload(runs_by_id[run_id], direction="forward")
        coords = payload.get("route", {}).get("geometry", {}).get("coordinates", [])
        if coords:
            payload_runs.append(payload)

    return jsonify(
        {
            "runs": payload_runs,
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
    store = _ensure_user_store_loaded()
    source = str(Path(app.config["USER_RUNS_FILE"]).expanduser())
    return jsonify(
        {
            "runs": store["runs"],
            "folders": store["folders"],
            "source": source,
        }
    )


@app.post("/api/folders")
def create_folder():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()

    if not name:
        return jsonify({"error": "Folder name is required."}), 400

    try:
        folder = _create_user_folder(name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"folder": folder}), 201


@app.post("/api/run")
def generate_user_run():
    payload = request.get_json(silent=True) or {}
    origin = str(payload.get("origin") or "").strip()
    destination = str(payload.get("destination") or "").strip()
    folder_id = payload.get("folder_id")

    if not origin or not destination:
        return jsonify({"error": "Both origin and destination are required."}), 400

    normalized_folder_id = None
    if folder_id is not None:
        candidate = str(folder_id).strip().lower()
        normalized_folder_id = candidate or None

    run_obj = _make_user_run(origin, destination, folder_id=normalized_folder_id)
    if "error" in run_obj:
        return jsonify(run_obj), 422

    return jsonify(run_obj)


@app.put("/api/user-runs/<int:run_id>")
def update_user_run(run_id: int):
    payload = request.get_json(silent=True) or {}
    origin = str(payload.get("origin") or "").strip()
    destination = str(payload.get("destination") or "").strip()
    folder_id = payload.get("folder_id")

    if not origin or not destination:
        return jsonify({"error": "Both origin and destination are required."}), 400

    normalized_folder_id = None
    if folder_id is not None:
        candidate = str(folder_id).strip().lower()
        normalized_folder_id = candidate or None

    run_obj = _update_user_run(run_id, origin, destination, folder_id=normalized_folder_id)
    if "error" in run_obj:
        message = str(run_obj.get("error") or "")
        if "not found" in message.lower():
            return jsonify(run_obj), 404
        return jsonify(run_obj), 422

    return jsonify(run_obj)


@app.delete("/api/user-runs/<int:run_id>")
def delete_user_run_endpoint(run_id: int):
    result = _delete_user_run(run_id)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


@app.patch("/api/folders/<folder_id>")
def rename_folder_endpoint(folder_id: str):
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    result = _rename_user_folder(folder_id, name)
    if "error" in result:
        message = str(result.get("error") or "")
        if "not found" in message.lower():
            return jsonify(result), 404
        return jsonify(result), 400
    return jsonify(result)


@app.delete("/api/folders/<folder_id>")
def delete_folder_endpoint(folder_id: str):
    result = _delete_user_folder(folder_id)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


@app.get("/api/locations/search")
def location_search():
    query = str(request.args.get("q") or "").strip()

    try:
        limit = int(request.args.get("limit") or 6)
    except (TypeError, ValueError):
        limit = 6

    results = _search_locations(query, limit=limit)
    return jsonify({"results": results})


@app.get("/api/pois")
def api_pois():
    """Serve the geocoded Knowledge points of interest for map highlighting.

    Returns an empty list when knowledge_pois.json has not been generated yet
    (run scripts/extract_pois.py then scripts/geocode_pois.py), so the UI and
    the smoke tests work without the dataset present.
    """
    path = Path(app.config.get("POIS_FILE", DEFAULT_POIS_FILE))
    if not path.exists():
        return jsonify({"pois": []})
    try:
        pois = json.loads(path.read_text())
    except (ValueError, OSError):
        return jsonify({"pois": []})
    return jsonify({"pois": pois})


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
    parser.add_argument(
        "--user-runs-file",
        default=os.environ.get("KRG_USER_RUNS_FILE", str(DEFAULT_USER_RUNS_FILE)),
        help="Path to user run storage JSON (env: KRG_USER_RUNS_FILE).",
    )
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode.")
    args = parser.parse_args()

    run_webapp(
        host=args.host,
        port=args.port,
        debug=args.debug,
        blue_book_file=args.blue_book_file,
        user_runs_file=args.user_runs_file,
    )


def run_webapp(
    host: str | None = None,
    port: int | None = None,
    debug: bool = False,
    blue_book_file: str | None = None,
    user_runs_file: str | None = None,
) -> None:
    global _USER_STORE_CACHE

    resolved_host = host or os.environ.get("KRG_WEB_HOST", DEFAULT_WEB_HOST)
    resolved_port = port or int(os.environ.get("KRG_WEB_PORT", str(DEFAULT_WEB_PORT)))
    resolved_blue_book_file = Path(
        blue_book_file or os.environ.get("KRG_BLUE_BOOK_FILE", str(DEFAULT_BLUE_BOOK_FILE))
    )
    resolved_user_runs_file = Path(
        user_runs_file or os.environ.get("KRG_USER_RUNS_FILE", str(DEFAULT_USER_RUNS_FILE))
    ).expanduser()

    app.config["BLUE_BOOK_FILE"] = resolved_blue_book_file
    app.config["USER_RUNS_FILE"] = resolved_user_runs_file

    with _USER_STORE_LOCK:
        _USER_STORE_CACHE = None

    app.run(host=resolved_host, port=resolved_port, debug=debug)


if __name__ == "__main__":
    main()
