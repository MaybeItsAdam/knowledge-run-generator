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
      --line-soft: #1f2937;
      --text: #0f172a;
      --muted: #334155;
      --accent: #facc15;
      --accent-2: #22c55e;
      --warn: #b91c1c;
      --shadow: 4px 4px 0 #111827;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
    }

    * { box-sizing: border-box; }

    html, body {
      margin: 0;
      height: 100%;
      color: var(--text);
      background: linear-gradient(145deg, var(--bg-1) 0%, var(--bg-2) 100%);
    }

    .app-shell {
      height: 100dvh;
      display: grid;
      grid-template-columns: minmax(320px, 380px) 1fr;
      overflow: hidden;
    }

    .sidebar {
      overflow: hidden;
      border-right: 3px solid var(--line);
      background: #fffef9;
      padding: .7rem;
      display: flex;
      flex-direction: column;
      gap: .55rem;
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
      border-radius: 0;
      box-shadow: var(--shadow);
      padding: .72rem;
    }

    .panel h1 {
      margin: 0 0 .2rem;
      font-size: 1.25rem;
      letter-spacing: .01em;
    }

    .panel h2 {
      margin: 0 0 .45rem;
      font-size: .95rem;
      letter-spacing: .01em;
    }

    .hierarchy-panel {
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
      gap: .45rem;
    }

    .meta {
      color: var(--muted);
      font-size: .84rem;
      line-height: 1.35;
    }

    .run-header-bar {
      display: grid;
      grid-template-columns: auto 1fr;
      align-items: center;
      gap: .42rem;
      border: 2px solid var(--line);
      background: #e8eefc;
      box-shadow: 3px 3px 0 #111827;
      padding: .42rem .5rem;
      min-height: 48px;
    }

    .run-header-label {
      display: inline-block;
      border: 2px solid var(--line);
      background: #fff;
      padding: .14rem .34rem;
      font-size: .68rem;
      font-weight: 800;
      letter-spacing: .08em;
    }

    .run-header-name {
      margin: 0;
      font-size: 1rem;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: #0f172a;
    }

    form {
      display: grid;
      gap: .42rem;
    }

    input, select {
      width: 100%;
      border: 2px solid var(--line);
      border-radius: 0;
      padding: .54rem .58rem;
      font: inherit;
      background: #fff;
      color: var(--text);
    }

    input:focus, select:focus {
      outline: 2px solid #93c5fd;
      border-color: var(--line);
    }

    button {
      border: 2px solid var(--line);
      border-radius: 0;
      padding: .48rem .66rem;
      font: inherit;
      font-weight: 650;
      cursor: pointer;
      color: #111827;
      background: var(--accent);
      box-shadow: 3px 3px 0 #111827;
    }

    button.secondary {
      background: #d1fae5;
    }

    button.ghost {
      background: #edf2fb;
      color: #1e293b;
      font-weight: 550;
    }

    button.tiny {
      padding: .24rem .42rem;
      border-radius: 0;
      font-size: .74rem;
      font-weight: 600;
      background: #fff;
      color: #172554;
      box-shadow: none;
    }

    .status-error {
      color: var(--warn);
      font-size: .83rem;
      min-height: 1.1rem;
    }

    .search-field {
      position: relative;
    }

    .suggestions {
      margin-top: .24rem;
      border: 2px solid var(--line-soft);
      background: #fff;
      border-radius: 0;
      overflow: hidden;
      max-height: 180px;
      overflow-y: auto;
      display: none;
    }

    .suggestions.show { display: block; }

    .suggestion-item {
      border: 0;
      border-bottom: 1px solid #e8eef8;
      width: 100%;
      text-align: left;
      background: #fff;
      color: #1e293b;
      padding: .42rem .5rem;
      font-size: .82rem;
      font-weight: 500;
      cursor: pointer;
      box-shadow: none;
    }

    .suggestion-item:last-child { border-bottom: 0; }
    .suggestion-item:hover { background: #f3f7ff; }

    .tree {
      border: 2px solid var(--line-soft);
      border-radius: 0;
      background: #ffffff;
      flex: 1;
      min-height: 0;
      overflow-y: auto;
      padding: .42rem;
      font-size: .8rem;
    }

    .tree-root-row {
      display: flex;
      align-items: center;
      gap: .34rem;
      color: #0f172a;
      font-weight: 700;
      padding: .32rem .4rem;
      border-bottom: 1px solid #e8eef8;
      margin-bottom: .18rem;
    }

    .tree-root-label {
      margin: 0;
    }

    .tree-children {
      padding-left: .18rem;
    }

    .tree-folder {
      margin: .12rem 0;
    }

    .tree-folder-row {
      min-height: 28px;
      display: flex;
      align-items: center;
      gap: .28rem;
      padding: .22rem .34rem;
      border-radius: 0;
      background: #fef3c7;
      border: 2px solid var(--line);
    }

    .disclosure {
      border: 0;
      background: transparent;
      color: #334155;
      width: 16px;
      min-width: 16px;
      text-align: center;
      padding: 0;
      font-size: .72rem;
      line-height: 1;
      cursor: pointer;
      box-shadow: none;
    }

    .folder-icon,
    .run-icon {
      width: 14px;
      min-width: 14px;
      color: #64748b;
      text-align: center;
      font-size: .72rem;
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
      font-size: .72rem;
      white-space: nowrap;
    }

    .tree-toggle {
      display: inline-flex;
      align-items: center;
      gap: .18rem;
      color: #64748b;
      font-size: .72rem;
      white-space: nowrap;
      user-select: none;
    }

    .tree-toggle input {
      width: 13px;
      height: 13px;
      margin: 0;
      padding: 0;
    }

    .tree-folder-children {
      margin-top: .12rem;
      padding-left: 1.2rem;
    }

    .tree-folder-children.hidden {
      display: none;
    }

    .tree-run {
      width: 100%;
      border: 2px solid transparent;
      background: #fff;
      color: #0f172a;
      border-radius: 0;
      padding: .28rem .32rem;
      display: flex;
      align-items: flex-start;
      gap: .28rem;
      text-align: left;
      cursor: pointer;
      box-shadow: none;
    }

    .tree-run:hover {
      background: #dbeafe;
      border-color: var(--line);
    }

    .tree-run-main {
      min-width: 0;
      flex: 1;
    }

    .tree-run-label {
      margin: 0;
      font-size: .79rem;
      line-height: 1.3;
      font-weight: 560;
      color: #1e293b;
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

    .empty {
      padding: .5rem;
      color: var(--muted);
      font-size: .8rem;
    }

    .details {
      border: 2px solid var(--line-soft);
      border-radius: 0;
      background: #f8fbff;
      padding: .6rem;
    }

    .details strong {
      font-size: .9rem;
    }

    .details ol {
      margin: .42rem 0 0;
      padding-left: 1.1rem;
      max-height: 170px;
      overflow-y: auto;
      font-size: .82rem;
      line-height: 1.38;
    }

    .settings-pane {
      position: absolute;
      top: 12px;
      right: 12px;
      width: min(430px, calc(100% - 24px));
      z-index: 650;
      box-shadow: var(--shadow);
      pointer-events: auto;
      display: grid;
      gap: .45rem;
    }

    .sidebar-footer {
      flex: 0 0 auto;
      display: grid;
      gap: .42rem;
    }

    .add-button {
      width: 100%;
      border-radius: 0;
      background: #fde047;
      box-shadow: 3px 3px 0 #111827;
    }

    .add-drawer {
      border: 2px solid var(--line);
      border-radius: 0;
      background: #ffffff;
      box-shadow: var(--shadow);
      padding: .5rem;
      display: grid;
      gap: .42rem;
    }

    .add-drawer.hidden {
      display: none;
    }

    @media (max-width: 980px) {
      .app-shell {
        grid-template-columns: 1fr;
        grid-template-rows: 50dvh 50dvh;
      }

      .sidebar {
        max-height: 50dvh;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }

      .settings-pane {
        width: calc(100% - 16px);
        top: 8px;
        right: 8px;
      }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <section class="panel hierarchy-panel">
        <div class="run-header-bar">
          <span class="run-header-label">RUN</span>
          <p id="currentRunName" class="run-header-name">untitled</p>
        </div>

        <form id="userRunForm" class="quick-run-form">
          <div class="search-field">
            <input id="originInput" required autocomplete="off" placeholder="Start point">
            <div id="originSuggestions" class="suggestions"></div>
          </div>
          <div class="search-field">
            <input id="destinationInput" required autocomplete="off" placeholder="End point">
            <div id="destinationSuggestions" class="suggestions"></div>
          </div>
          <select id="folderSelect"></select>
          <button id="saveRunButton" type="submit">Save Run</button>
        </form>
        <div id="userRunError" class="status-error"></div>

        <input id="blueBookSearch" type="search" placeholder="Filter file hierarchy">
        <div id="runTree" class="tree"></div>
      </section>

      <section class="sidebar-footer">
        <button id="addButton" class="add-button" type="button">+ Add Folder</button>
        <div id="addDrawer" class="add-drawer hidden">
          <form id="folderForm">
            <input id="folderNameInput" required maxlength="80" placeholder="Folder name">
            <button class="secondary" type="submit">Create Folder</button>
          </form>
          <div id="folderError" class="status-error"></div>
        </div>
      </section>
    </aside>

    <section class="map-pane">
      <div id="map"></div>
      <section id="settingsPane" class="panel settings-pane">
        <h2>Settings</h2>
        <div class="meta" id="bluebookStatus">Loading blue book runs...</div>
        <div class="meta" id="userStorage"></div>
        <section id="runDetails" class="details">
          <strong>No run selected.</strong>
          <div class="meta">Pick a run from the file hierarchy or edit start/end then save.</div>
        </section>
      </section>
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
      userRuns: [],
      userFolders: [],
      storageSource: "",
      blueBookSearch: "",
      searchSuggestions: { origin: [], destination: [] },
      searchSeq: { origin: 0, destination: 0 },
      debounceTimers: { origin: null, destination: null },
    };

    function escapeHtml(value) {
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function initMap() {
      state.map = L.map("map", { zoomControl: true }).setView([51.5074, -0.1278], 11);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      }).addTo(state.map);

      state.searchLayer = L.layerGroup().addTo(state.map);
      state.focusLayer = L.layerGroup().addTo(state.map);

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

    function setFolderVisibility(folderKey, visible, rerender = true) {
      state.visibleFolders[folderKey] = Boolean(visible);
      syncFolderLayers();
      if (rerender) {
        renderRunTree();
      }
    }

    function refreshSaveButtonLabel() {
      const saveButton = document.getElementById("saveRunButton");
      if (!saveButton) {
        return;
      }
      const isEditingUserRun = Boolean(state.selectedRun && state.selectedRun.source === "user");
      saveButton.textContent = isEditingUserRun ? "Save Changes" : "Save Run";
    }

    function populateRunEditor(runPayload) {
      const originInput = document.getElementById("originInput");
      const destinationInput = document.getElementById("destinationInput");
      const folderSelect = document.getElementById("folderSelect");

      if (!originInput || !destinationInput) {
        return;
      }

      originInput.value = (((runPayload || {}).start || {}).name) || "";
      destinationInput.value = (((runPayload || {}).end || {}).name) || "";

      if (!folderSelect) {
        return;
      }

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
        return;
      }

      const source = String((runPayload || {}).source || "").toLowerCase() === "blue_book" ? "blue_book" : "user";
      state.selectedRun = {
        id: runPayload.id,
        source,
        folder_id: runPayload.folder_id || null,
      };
      populateRunEditor(runPayload);
      refreshSaveButtonLabel();
    }

    function colorForFolder(folderKey) {
      if (folderKey === BLUE_BOOK_FOLDER_KEY) {
        return BLUE_BOOK_COLOR;
      }
      if (folderKey === ROOT_USER_FOLDER_KEY) {
        return "#0f766e";
      }

      const palette = ["#2563eb", "#0f766e", "#dc2626", "#7c3aed", "#c2410c", "#0e7490", "#be123c"];
      const text = String(folderKey || "folder");
      let hash = 0;
      for (let i = 0; i < text.length; i += 1) {
        hash = (hash * 31 + text.charCodeAt(i)) % 2147483647;
      }
      return palette[Math.abs(hash) % palette.length];
    }

    function renderDetails(runPayload, tagLabel) {
      const detailsEl = document.getElementById("runDetails");
      const currentRunNameEl = document.getElementById("currentRunName");
      if (!runPayload) {
        detailsEl.innerHTML = "<strong>No run selected.</strong><div class='meta'>Select a run from the file hierarchy.</div>";
        if (currentRunNameEl) {
          currentRunNameEl.textContent = "untitled";
        }
        return;
      }

      const route = runPayload.route || {};
      const steps = Array.isArray(route.steps) ? route.steps : [];
      const distance = Number(route.distance || 0).toFixed(1);
      const duration = Number(route.duration || 0).toFixed(1);
      const startName = ((runPayload.start || {}).name) || "Unknown";
      const endName = ((runPayload.end || {}).name) || "Unknown";
      const runTitle = String(runPayload.title || "Untitled");

      if (currentRunNameEl) {
        currentRunNameEl.textContent = runTitle;
      }

      detailsEl.innerHTML = `
        <strong>${escapeHtml(runTitle)} (${escapeHtml(tagLabel)})</strong>
        <div class="meta">${escapeHtml(startName)} -> ${escapeHtml(endName)}</div>
        <div class="meta">Distance ${distance}m | Duration ${duration}s | Steps ${steps.length}</div>
        <ol>${steps.map((step) => `<li>${escapeHtml(step.instruction || "")}</li>`).join("")}</ol>
      `;
    }

    function drawRunLayer(runPayload, targetLayer, color, tagLabel, onFocusClick) {
      const coords = ((((runPayload || {}).route || {}).geometry || {}).coordinates || []);
      if (!coords.length) {
        return null;
      }

      const latLngs = coords.map((point) => [point[1], point[0]]);
      const polyline = L.polyline(latLngs, {
        color,
        weight: 4,
        opacity: 0.5,
        lineJoin: "round",
      }).addTo(targetLayer);

      if (onFocusClick) {
        polyline.on("click", onFocusClick);
      }

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
        startMarker.bindTooltip(`Start: ${escapeHtml(start.name || "Unknown")}`);
        if (onFocusClick) {
          startMarker.on("click", onFocusClick);
        }
      }

      if (endCoords.length === 2) {
        const endMarker = L.circleMarker([endCoords[1], endCoords[0]], {
          radius: 4,
          color,
          fillColor: color,
          fillOpacity: 0.45,
          opacity: 0.95,
        }).addTo(targetLayer);
        endMarker.bindTooltip(`End: ${escapeHtml(end.name || "Unknown")}`);
        if (onFocusClick) {
          endMarker.on("click", onFocusClick);
        }
      }

      return {
        bounds: polyline.getBounds(),
        tagLabel,
      };
    }

    function focusRun(runPayload, tagLabel, color, fitBounds = true) {
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

        if (fitBounds && drawn.bounds && drawn.bounds.isValid()) {
          state.map.fitBounds(drawn.bounds, { padding: [34, 34] });
        }
      }

      renderDetails(runPayload, tagLabel);
      setSelectedRun(runPayload);
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
        drawRunLayer(run, layer, color, "User", () => focusRun(run, "User", color, true));
      });

      syncFolderLayers();
    }

    function redrawBlueBookLayer() {
      const layer = getFolderLayer(BLUE_BOOK_FOLDER_KEY);
      layer.clearLayers();

      if (!state.blueBookAllLoaded) {
        return;
      }

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
      if (folderKey === BLUE_BOOK_FOLDER_KEY) {
        return "blue book runs";
      }
      if (folderKey === ROOT_USER_FOLDER_KEY) {
        return "user root runs";
      }
      const folder = state.userFolders.find((item) => item.id === folderKey);
      return folder ? folder.name : "folder";
    }

    function renderFolderSelect() {
      const selectEl = document.getElementById("folderSelect");
      const previous = selectEl.value;
      const options = [
        `<option value="">Save At Root</option>`,
        ...state.userFolders
          .slice()
          .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")))
          .map((folder) => `<option value="${escapeHtml(folder.id)}">Folder: ${escapeHtml(folder.name)}</option>`),
      ];
      selectEl.innerHTML = options.join("");
      if (["", ...state.userFolders.map((folder) => folder.id)].includes(previous)) {
        selectEl.value = previous;
      }
    }

    function renderBlueBookRows() {
      const term = state.blueBookSearch.trim().toLowerCase();
      const filtered = state.blueBookSummaries.filter((run) => {
        if (!term) {
          return true;
        }
        const hay = `${run.id} ${run.title} ${run.start_name} ${run.end_name}`.toLowerCase();
        return hay.includes(term);
      });

      if (!filtered.length) {
        return "<div class='empty'>No matching blue book runs.</div>";
      }

      return filtered
        .map(
          (run) => `
            <button class="tree-run" type="button" data-action="show-blue" data-run-id="${escapeHtml(run.id)}">
              <span class="run-icon">-</span>
              <span class="tree-run-main">
                <p class="tree-run-label">Run ${escapeHtml(run.id)}: ${escapeHtml(run.title)}</p>
                <p class="tree-run-sub">${escapeHtml(run.start_name)} -> ${escapeHtml(run.end_name)}</p>
              </span>
            </button>
          `
        )
        .join("");
    }

    function renderUserRows(runs) {
      if (!runs.length) {
        return "<div class='empty'>No runs in this folder.</div>";
      }

      return runs
        .map(
          (run) => `
            <button class="tree-run" type="button" data-action="show-user" data-user-id="${escapeHtml(run.id)}">
              <span class="run-icon">-</span>
              <span class="tree-run-main">
                <p class="tree-run-label">${escapeHtml(run.title || `Run ${run.id}`)}</p>
                <p class="tree-run-sub">${escapeHtml((run.start || {}).name || "Unknown")} -> ${escapeHtml((run.end || {}).name || "Unknown")}</p>
              </span>
            </button>
          `
        )
        .join("");
    }

    function renderFolderNode(folderKey, label, count, childrenHtml, options = {}) {
      const collapsed = Boolean(state.collapsedFolders[folderKey]);
      const visible = Boolean(state.visibleFolders[folderKey]);
      const extraControl = options.extraControl || "";
      const folderMeta = options.meta || `${count} runs`;

      return `
        <section class="tree-folder">
          <div class="tree-folder-row">
            <button class="disclosure" type="button" data-action="toggle-collapse" data-folder-key="${escapeHtml(folderKey)}">${collapsed ? ">" : "v"}</button>
            <span class="folder-icon">[+]</span>
            <p class="tree-folder-label">${escapeHtml(label)}</p>
            <span class="tree-folder-meta">${escapeHtml(folderMeta)}</span>
            ${extraControl}
            <label class="tree-toggle">
              <input type="checkbox" data-action="toggle-folder" data-folder-key="${escapeHtml(folderKey)}" ${visible ? "checked" : ""}>
              map
            </label>
          </div>
          <div class="tree-folder-children ${collapsed ? "hidden" : ""}">
            ${childrenHtml}
          </div>
        </section>
      `;
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
        "blue book runs",
        state.blueBookSummaries.length,
        blueBookRows,
        {
          meta: `${state.blueBookSummaries.length} runs`,
          extraControl: `<button class="tiny" type="button" data-action="load-blue">${state.blueBookAllLoaded ? "Reload" : "Load"}</button>`,
        }
      );

      const rootNode = renderFolderNode(
        ROOT_USER_FOLDER_KEY,
        "user root runs",
        rootRuns.length,
        rootRows,
        {
          meta: `${rootRuns.length} runs`,
        }
      );

      const userFolderNodes = state.userFolders
        .slice()
        .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")))
        .map((folder) => {
          const folderRuns = userRunsForFolder(folder.id);
          return renderFolderNode(folder.id, folder.name, folderRuns.length, renderUserRows(folderRuns), {
            meta: `${folderRuns.length} runs`,
          });
        })
        .join("");

      treeEl.innerHTML = `
        <div class="tree-root-row">
          <span class="folder-icon">[+]</span>
          <p class="tree-root-label">runs</p>
        </div>
        <div class="tree-children">
          ${blueBookNode}
          ${rootNode}
          ${userFolderNodes}
        </div>
      `;
    }

    async function ensureBlueBookRunLoaded(runId) {
      const key = String(runId);
      if (state.blueBookRunsById[key]) {
        return state.blueBookRunsById[key];
      }

      const response = await fetch(`/api/bluebook/runs/${encodeURIComponent(runId)}?direction=forward`);
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || "Unable to load blue book run.");
      }
      state.blueBookRunsById[key] = payload;
      return payload;
    }

    async function loadAllBlueBookRuns() {
      const statusEl = document.getElementById("bluebookStatus");
      statusEl.textContent = "Loading blue book route geometry...";

      const response = await fetch("/api/bluebook/runs/all");
      const payload = await response.json();

      if (!response.ok) {
        statusEl.textContent = payload.error || "Failed to load blue book route geometry.";
        return false;
      }

      state.blueBookRunsById = {};
      (payload.runs || []).forEach((run) => {
        state.blueBookRunsById[String(run.id)] = run;
      });
      state.blueBookAllLoaded = true;

      const loadedCount = Object.keys(state.blueBookRunsById).length;
      statusEl.textContent = `Loaded geometry for ${loadedCount} blue book runs.`;

      redrawBlueBookLayer();
      return true;
    }

    async function loadBlueBookSummaries() {
      const statusEl = document.getElementById("bluebookStatus");
      const response = await fetch("/api/bluebook/runs");
      const payload = await response.json();

      if (!response.ok) {
        statusEl.textContent = payload.error || "Failed to load blue book runs.";
        state.blueBookSummaries = [];
        renderRunTree();
        return;
      }

      state.blueBookSummaries = payload.runs || [];
      if (!state.blueBookSummaries.length) {
        statusEl.textContent = payload.message || "No blue book file found.";
      } else {
        statusEl.textContent = `Loaded ${state.blueBookSummaries.length} summaries from ${payload.source}.`;
      }

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

      const storageEl = document.getElementById("userStorage");
      if (state.storageSource) {
        storageEl.textContent = `User runs are stored at ${state.storageSource}`;
      }
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
        .map((result, index) => `
          <button
            class="suggestion-item"
            type="button"
            data-action="pick-suggestion"
            data-kind="${kind}"
            data-index="${index}"
          >${escapeHtml(result.name || "Unnamed location")}</button>
        `)
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
          if (!Array.isArray(coords) || coords.length !== 2) {
            return;
          }

          const latLng = [coords[1], coords[0]];
          previewPoints.push(latLng);

          const marker = L.circleMarker([coords[1], coords[0]], {
            radius: 6,
            color,
            fillColor: color,
            fillOpacity: 0.82,
            opacity: 0.95,
          }).addTo(state.searchLayer);
          marker.bindTooltip(`${key}: ${result.name || "location"}`);
        });
      });

      if (!previewPoints.length) {
        return;
      }

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

      const response = await fetch(`/api/locations/search?q=${encodeURIComponent(normalized)}&limit=6`);
      const payload = await response.json();

      if (seq !== state.searchSeq[kind]) {
        return;
      }

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

    function wireRunTreeEvents() {
      const treeEl = document.getElementById("runTree");

      treeEl.addEventListener("change", async (event) => {
        const target = event.target;
        if (!(target instanceof HTMLInputElement)) {
          return;
        }
        if (target.dataset.action !== "toggle-folder") {
          return;
        }

        const folderKey = target.dataset.folderKey;
        const visible = target.checked;

        if (folderKey === BLUE_BOOK_FOLDER_KEY && visible && !state.blueBookAllLoaded) {
          const loaded = await loadAllBlueBookRuns();
          if (!loaded) {
            target.checked = false;
            setFolderVisibility(folderKey, false);
            return;
          }
        }

        setFolderVisibility(folderKey, visible);
      });

      treeEl.addEventListener("click", async (event) => {
        const button = event.target.closest("button[data-action]");
        if (!button) {
          return;
        }

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
            focusRun(run, "blue book", BLUE_BOOK_COLOR, true);
          } catch (error) {
            userError.textContent = error.message || "Unable to load blue book run.";
          }
          return;
        }

        if (action === "show-user") {
          const runId = button.dataset.userId;
          const run = state.userRuns.find((item) => String(item.id) === String(runId));
          if (!run) {
            return;
          }
          const folderKey = run.folder_id || ROOT_USER_FOLDER_KEY;
          focusRun(run, folderName(folderKey), colorForFolder(folderKey), true);
          return;
        }

        if (action === "load-blue") {
          await loadAllBlueBookRuns();
          renderRunTree();
        }
      });
    }

    function wireSuggestionEvents() {
      document.querySelector(".sidebar").addEventListener("click", (event) => {
        const button = event.target.closest("button[data-action='pick-suggestion']");
        if (!button) {
          return;
        }

        const kind = button.dataset.kind;
        const index = Number(button.dataset.index || -1);
        const options = state.searchSuggestions[kind] || [];
        const selected = options[index];
        if (!selected) {
          return;
        }

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
          if (folderInput) {
            folderInput.focus();
          }
        }
      });

      document.addEventListener("click", (event) => {
        if (drawer.classList.contains("hidden")) {
          return;
        }
        const target = event.target;
        if (!(target instanceof Node)) {
          return;
        }
        if (drawer.contains(target) || addButton.contains(target)) {
          return;
        }
        drawer.classList.add("hidden");
      });

      document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          drawer.classList.add("hidden");
        }
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
          ? `/api/user-runs/${encodeURIComponent(selected.id)}`
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
            if (String(item.id) !== String(payload.id)) {
              return item;
            }
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
        renderRunTree();

        const folderKey = payload.folder_id || ROOT_USER_FOLDER_KEY;
        focusRun(payload, folderName(folderKey), colorForFolder(folderKey), true);

        state.searchSuggestions.origin = [];
        state.searchSuggestions.destination = [];
        renderSuggestions("origin");
        renderSuggestions("destination");
        updateSearchPreviewMarkers();
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

    async function boot() {
      initMap();
      wireAddDrawer();
      wireRunTreeEvents();
      wireSuggestionEvents();
      wireBlueBookSearch();
      wireGenerateRunForm();
      wireCreateFolderForm();
      wireLocationSearch("originInput", "origin");
      wireLocationSearch("destinationInput", "destination");

      await Promise.all([loadBlueBookSummaries(), loadUserRunsAndFolders()]);
      syncFolderLayers();
      renderDetails(null, "");
      setSelectedRun(null);
      refreshSaveButtonLabel();

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


@app.get("/api/locations/search")
def location_search():
    query = str(request.args.get("q") or "").strip()

    try:
        limit = int(request.args.get("limit") or 6)
    except (TypeError, ValueError):
        limit = 6

    results = _search_locations(query, limit=limit)
    return jsonify({"results": results})


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
