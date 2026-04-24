"""
High-level ``Session`` bundling graph + gazetteer + alias index + turn
restrictions.

The goal is a one-import, one-instance, one-method-call API:

    >>> import krg
    >>> s = krg.Session()                        # loads everything (once)
    >>> run = s.run("Ritz Hotel", "Gibson Square")
    >>> run.distance_m, run.duration_s
    >>> run.steps[:3]
    >>> run.to_geojson()

Repeated calls reuse the loaded graph/indexes, so the first call pays the
cold-start cost and subsequent ones are fast.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .aliases import AliasIndex, load_or_build_alias_index
from .caller import generate_call
from .gazetteer import Gazetteer, preflight_run
from .geocoder import geocode_and_snap
from .router import (
    _extract_route_metadata,
    get_constrained_route,
    get_route,
    load_graph,
    nodes_to_coords_geometry,
)


@dataclass
class Run:
    """A computed run between two points, returned by ``Session.run``."""
    origin: str
    destination: str
    start_coords: tuple  # (lat, lon)
    end_coords: tuple
    route_nodes: list
    coordinates: list    # [[lon, lat], ...]
    distance_m: float
    duration_s: float
    steps: list          # The Call (turn-by-turn)
    preflight_ok: bool = True
    preflight_warnings: list = field(default_factory=list)
    preflight_reasons: list = field(default_factory=list)
    unresolved_streets: list = field(default_factory=list)

    # --- convenience serialisation ------------------------------------

    def to_geojson(self) -> dict:
        """Return a GeoJSON Feature for this run (LineString)."""
        return {
            "type": "Feature",
            "properties": {
                "origin": self.origin,
                "destination": self.destination,
                "distance_m": self.distance_m,
                "duration_s": self.duration_s,
            },
            "geometry": {
                "type": "LineString",
                "coordinates": self.coordinates,
            },
        }

    def save_geojson(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_geojson(), indent=2))


class Session:
    """
    Loaded-once container for the routing stack.

    Parameters
    ----------
    graph
        An already-loaded osmnx graph. If ``None``, Greater London is loaded
        (and cached on disk) on first use.
    poi_overrides
        Dict or path to JSON. Supports both the legacy ``[lat, lon]`` form and
        the richer ``{"lat":..., "lon":..., "on_street":..., "approach_from":...}``
        form.
    cache_dir
        Where to keep derived indexes (alias pickle, etc.). Defaults to
        ``/tmp/app_cache``.
    """

    def __init__(
        self,
        graph=None,
        poi_overrides: dict | str | Path | None = None,
        cache_dir: str | Path = "/tmp/app_cache",
        build_indexes: bool = True,
    ):
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._graph = graph
        self._alias_index: AliasIndex | None = None
        self._gazetteer: Gazetteer | None = None
        self._poi_overrides = self._load_overrides(poi_overrides)

        if build_indexes and graph is not None:
            # Build eagerly if caller supplied a graph
            self._ensure_indexes()

    # ------------------------------------------------------------------
    # Lazy loaders
    # ------------------------------------------------------------------

    @property
    def graph(self):
        if self._graph is None:
            self._graph = load_graph()
        return self._graph

    @property
    def alias_index(self) -> AliasIndex:
        self._ensure_indexes()
        return self._alias_index  # type: ignore[return-value]

    @property
    def gazetteer(self) -> Gazetteer:
        self._ensure_indexes()
        return self._gazetteer  # type: ignore[return-value]

    def _ensure_indexes(self) -> None:
        if self._alias_index is None:
            self._alias_index = load_or_build_alias_index(
                self.graph, self._cache_dir / "alias_index.pkl"
            )
        if self._gazetteer is None:
            self._gazetteer = Gazetteer(
                overrides=self._poi_overrides, alias_index=self._alias_index
            )

    @staticmethod
    def _load_overrides(src) -> dict:
        if src is None:
            return {}
        if isinstance(src, dict):
            return src
        p = Path(src)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                return {}
        return {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        origin: str,
        destination: str,
        via: list | None = None,
        prohibited_turns: set | None = None,
    ) -> Run:
        """
        Compute a Knowledge run from ``origin`` to ``destination``.

        If ``via`` is given (a list of street names), the sequential-discount
        router is used; otherwise a plain shortest-path route.

        Returns a :class:`Run`. If the preflight check fails, the ``Run``
        still carries whatever route could be produced, with
        ``preflight_ok = False`` and reasons listed — same triage model as
        the Blue Book pipeline.
        """
        G = self.graph

        start = geocode_and_snap(origin, G, self._poi_overrides, gazetteer=self.gazetteer)
        end = geocode_and_snap(destination, G, self._poi_overrides, gazetteer=self.gazetteer)

        if start is None:
            raise ValueError(f"Could not geocode origin: {origin!r}")
        if end is None:
            raise ValueError(f"Could not geocode destination: {destination!r}")

        start_lat, start_lon, start_node = start
        end_lat, end_lon, end_node = end

        start_entry = self.gazetteer.resolve(origin, G)
        end_entry = self.gazetteer.resolve(destination, G)
        pre = preflight_run(
            start_entry, end_entry, list(via or []), self.alias_index,
        )

        if via:
            route_nodes, _meta = get_constrained_route(
                G, start_node, end_node, [],
                prohibited_turns=prohibited_turns,
                intermediate_streets=list(via),
            )
        else:
            route_nodes = get_route(
                G, (start_lat, start_lon), (end_lat, end_lon),
                orig_node=start_node, dest_node=end_node,
            )

        if not route_nodes:
            raise RuntimeError(
                f"No route found between {origin!r} and {destination!r}"
            )

        coords = nodes_to_coords_geometry(G, route_nodes)
        meta = _extract_route_metadata(G, route_nodes)
        steps = generate_call(G, route_nodes)
        distance = meta["total_distance"]
        duration = round((distance / 1000) / 20 * 3600, 1)  # 20 km/h

        return Run(
            origin=origin,
            destination=destination,
            start_coords=(start_lat, start_lon),
            end_coords=(end_lat, end_lon),
            route_nodes=list(route_nodes),
            coordinates=coords,
            distance_m=distance,
            duration_s=duration,
            steps=steps,
            preflight_ok=pre.ok,
            preflight_warnings=pre.warnings,
            preflight_reasons=pre.reasons,
            unresolved_streets=pre.unresolved_streets,
        )

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    def resolve_street(self, raw: str) -> str | None:
        """Canonical form of a street name (or ``None`` if unknown)."""
        return self.alias_index.resolve(raw)

    def resolve_place(self, raw: str) -> Any:
        """Gazetteer lookup. Returns a ``GazetteerEntry`` or ``None``."""
        return self.gazetteer.resolve(raw, self.graph)
