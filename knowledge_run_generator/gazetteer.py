"""
POI gazetteer and semantic snap.

Previously the pipeline treated ``poi_overrides.json`` as a flat ``name → [lat,
lon]`` map and snapped every POI to the geometrically nearest graph node. That
produced two recurring map-point bugs:

  1. POIs adjacent to a dual carriageway (motorway, A-road trunk) snapped to
     the wrong side of the central reservation — taxis physically cannot make
     the U-turn, so the router either failed or produced absurd detours.
  2. Stations snapped to the platform-centre node, stranded on a pedestrian
     segment of the graph that has no drive connection.

The gazetteer fixes both by:

  * Supporting an enriched POI schema with optional ``on_street`` /
    ``approach_from`` hints.
  * A ``semantic_snap`` that prefers a node on the hinted street, then any
     node on a drive-permitted edge, and only falls back to the geometric
    nearest node when nothing better is available.

The old ``[lat, lon]`` schema is still accepted transparently, so existing
``poi_overrides.json`` files work unchanged.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .aliases import AliasIndex, normalise as _normalise_name

# Trailing punctuation is tolerated: the Blue Book source has entries like
# "BROMYARD AVENUE W3." that would otherwise keep their postcode forever.
_POSTCODE_SUFFIX_RE = re.compile(r"\s+([A-Z]{1,2}\d{1,2}[A-Z]?)\s*[.,]?\s*$")

# The geocoded Knowledge Points List, produced by ``krg generate pois``. It is
# not committed (``constants/`` is gitignored), so every lookup path treats it
# as optional.
DEFAULT_KNOWLEDGE_POIS_PATH = (
    Path(__file__).resolve().parent.parent / "constants" / "knowledge_pois.json"
)


def _split_postcode(name: str) -> tuple[str, str | None]:
    """``"MANOR HOUSE STATION N4"`` -> ``("MANOR HOUSE STATION", "N4")``."""
    upper = (name or "").upper().strip()
    match = _POSTCODE_SUFFIX_RE.search(upper)
    if not match:
        return upper, None
    return upper[: match.start()].strip(), match.group(1)


# Words that qualify a station rather than name it. OSM almost never includes
# "Station" in a station's name tag — Charing Cross is "Charing Cross", Waterloo
# is "London Waterloo" — while the Blue Book always does, and sometimes adds the
# operator ("BETHNAL GREEN B_R STATION"). Matching the two means reducing both
# sides to the bare name.
_STATION_TOKENS = frozenset({
    "STATION", "STATIONS", "STN",
    "BR", "B_R", "BRITISH", "RAIL", "RAILWAY", "MAINLINE",
    "TUBE", "UNDERGROUND", "OVERGROUND", "DLR", "LU",
})

# Kinds that make a candidate station-like when a query asks for a station.
# ``station`` is the OSM harvest's kind; the two bus/coach leaves come from the
# Points List taxonomy (``knowledge_run_generator.poi_categories``), which used
# to file them as plain ``station`` too. Listing them here keeps the
# station-like set a strict subset of what it was: the only records that leave
# it are the fire, police, ambulance, lifeboat and petrol "stations", which are
# not places a run can start or finish at under that name.
_STATION_KINDS = frozenset({"station", "bus_station", "coach_station"})


def _looks_like_station(name: str) -> bool:
    return any(t in _STATION_TOKENS for t in _normalise_name(name).split())


def _station_stems(name: str) -> list[str]:
    """Bare-name keys for a station, most specific first.

    ``"London Waterloo"`` -> ``["LONDON WATERLOO", "WATERLOO"]`` so a query for
    "WATERLOO STATION" finds it, while "London Bridge" stays reachable under
    its full name.
    """
    tokens = [t for t in _normalise_name(name).split() if t not in _STATION_TOKENS]
    if not tokens:
        return []
    stems = [" ".join(tokens)]
    if len(tokens) > 1 and tokens[0] == "LONDON":
        stems.append(" ".join(tokens[1:]))
    return stems

# A point-tier record whose snap is worse than this, for a name that is
# *exactly* a street in the graph, is almost always a rooftop geocode somewhere
# along that street (the Points List geocodes "YORK WAY N1" to a building).
# Beyond it we prefer the street tier, whose snap is 0 by construction.
# Matches preflight's ``max_snap_m`` — the point past which the record would
# fail the run anyway.
_STREET_FALLBACK_SNAP_M = 50.0

# Highway classes we avoid snapping to when a better option exists. Dual
# carriageways and slip roads are the sources of the "wrong side" bug.
_AVOID_HIGHWAY_CLASSES = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
}


@dataclass(frozen=True)
class GazetteerEntry:
    canonical_name: str
    lat: float
    lon: float
    snapped_node: int
    snap_distance_m: float
    on_street: str | None = None
    approach_node: int | None = None
    source: str = "override"
    # The winning record's postal district disagreed with the one in the
    # query. Not fatal on its own — Points List districts have gaps — but
    # preflight surfaces it, because it is how wrong-borough endpoints happen.
    district_mismatch: bool = False


class DistrictModel:
    """Robust per-district geometry, built from the geocoded Points List.

    For each postal district with at least ``min_points`` members: the
    *median* centre (the mean is dragged by outliers — W1 has 551 points with
    a p90 spread of 2.3 km) and the 95th-percentile radius around that
    centre, floored at ``radius_floor_m`` so tiny dense districts don't fail
    everything a street away. Districts with fewer members are unmodelled —
    a 1-point district (SW2) can't say anything about plausibility.
    """

    def __init__(self, knowledge_pois: dict | None, min_points: int = 5,
                 radius_floor_m: float = 1000.0):
        self._centres: dict[str, tuple[float, float]] = {}
        self._radii: dict[str, float] = {}

        by_district: dict[str, list[tuple[float, float]]] = {}
        for value in (knowledge_pois or {}).values():
            parsed = _parse_override(value)
            if parsed is None:
                continue
            district = str(parsed.get("postal_district", "")).upper()
            if not district:
                continue
            by_district.setdefault(district, []).append(
                (parsed["lat"], parsed["lon"]))

        for district, points in by_district.items():
            lats = sorted(p[0] for p in points)
            lons = sorted(p[1] for p in points)
            centre = (lats[len(lats) // 2], lons[len(lons) // 2])
            # A centre helps even at n=1 (street-tier disambiguation just
            # needs *an* anchor in the right part of town); a plausibility
            # radius is only trustworthy with enough points behind it, so
            # districts under ``min_points`` are never *checked*.
            self._centres[district] = centre
            if len(points) < min_points:
                continue
            dists = sorted(
                _haversine(centre[0], centre[1], la, lo) for la, lo in points
            )
            p95 = dists[min(len(dists) - 1, int(0.95 * len(dists)))]
            self._radii[district] = max(p95, radius_floor_m)

    def centre(self, district: str | None) -> tuple[float, float] | None:
        if not district:
            return None
        return self._centres.get(district.upper())

    def check(self, district: str | None, lat: float, lon: float,
              fail_floor_m: float = 2500.0) -> dict | None:
        """Plausibility of a point claiming to be in *district*.

        Returns ``None`` when the district is unknown/unmodelled, else
        ``{"distance_m", "p95_radius_m", "status"}`` with status ``ok`` (inside
        p95), ``warn`` (beyond p95) or ``fail`` (beyond
        ``max(p95 × 1.5, fail_floor_m)`` — the Run 131/177/206 class).
        """
        if not district:
            return None
        centre = self._centres.get(district.upper())
        radius = self._radii.get(district.upper())
        if centre is None or radius is None:
            return None
        distance = _haversine(centre[0], centre[1], lat, lon)
        if distance > max(radius * 1.5, fail_floor_m):
            status = "fail"
        elif distance > radius:
            status = "warn"
        else:
            status = "ok"
        return {
            "distance_m": round(distance, 1),
            "p95_radius_m": round(radius, 1),
            "status": status,
        }


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _edge_highway_class(edge_data: dict) -> str:
    hwy = edge_data.get("highway")
    if isinstance(hwy, (list, tuple)) and hwy:
        hwy = hwy[0]
    return str(hwy or "")


def _node_is_routable(G, node: int) -> bool:
    """True if *node* can be both entered and left via non-avoided drivable
    edges.

    Touching one drivable edge is not enough: a node at the upstream tip of a
    one-way (0 in-edges) can never be *arrived at*, and a sink node (0
    out-edges) strands the reverse leg — Run 48's reverse route died on a
    Lime Street sink node, and Runs 43/253 ended hundreds of metres short of
    source-only end nodes. Requiring both directions keeps the snap on nodes
    a route can actually terminate at and depart from.
    """
    try:
        has_out = False
        for _, _, data in G.out_edges(node, data=True):
            klass = _edge_highway_class(data)
            if klass and klass not in _AVOID_HIGHWAY_CLASSES:
                has_out = True
                break
        if not has_out:
            return False
        for _, _, data in G.in_edges(node, data=True):
            klass = _edge_highway_class(data)
            if klass and klass not in _AVOID_HIGHWAY_CLASSES:
                return True
    except Exception:
        pass
    return False


def _parse_override(value: Any) -> dict | None:
    """Normalise a raw ``poi_overrides.json`` value into a dict.

    Accepted forms:
      * ``[lat, lon]`` or ``(lat, lon)``
      * ``{"lat": ..., "lon": ..., "on_street"?: ..., "approach_from"?: ...}``
      * ``{"coordinates": [lon, lat], ...}`` — the GeoJSON-ordered form written
        by ``scripts/geocode_pois.py`` for the Knowledge Points List.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return {"lat": float(value[0]), "lon": float(value[1])}
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        if "lat" in value and "lon" in value:
            try:
                out = {"lat": float(value["lat"]), "lon": float(value["lon"])}
            except (TypeError, ValueError):
                return None
        else:
            coords = value.get("coordinates")
            if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
                return None
            try:
                # [lng, lat], per the GeoJSON convention used across the repo.
                out = {"lat": float(coords[1]), "lon": float(coords[0])}
            except (TypeError, ValueError):
                return None
        for k in ("on_street", "approach_from", "postal_district"):
            if value.get(k):
                out[k] = str(value[k])
        # ``kind`` comes from the OSM harvest, ``category`` from the Points List
        # extractor; either identifies a station for name matching.
        kind = value.get("kind") or value.get("category")
        if kind:
            out["kind"] = str(kind)
        return out
    return None


def load_knowledge_pois(path: str | Path | None = None) -> dict[str, dict]:
    """Load ``knowledge_pois.json`` into ``{NAME_UPPER: record}``.

    The file is a *list* of geocoded Points List entries; points that failed to
    geocode carry ``coordinates: null`` and are skipped. Returns an empty dict
    if the file is absent or unreadable — the caller degrades to the geocoder.
    """
    path = Path(path) if path else DEFAULT_KNOWLEDGE_POIS_PATH
    if not path.exists():
        return {}
    try:
        records = json.loads(path.read_text())
    except Exception:
        return {}
    if not isinstance(records, list):
        return {}

    out: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        name = str(record.get("name") or "").strip().upper()
        if not name or not record.get("coordinates"):
            continue
        # First writer wins, matching the override semantics elsewhere; the
        # per-postcode disambiguation in _PoiTable handles same-name points.
        out.setdefault(name, record)
    return out


class _PoiTable:
    """One lookup tier over a ``{NAME: record}`` dict.

    Three keys are indexed per entry, tried in order by :meth:`lookup`:
    the verbatim upper-cased name, the name with its postcode suffix
    stripped, and the canonically normalised stem (which expands
    abbreviations, so "FITZHARDINGE ST" finds "Fitzhardinge Street").

    Normalised keys can collide — the Points List has several same-named
    points in different districts — so they map to a *list* and the query's
    own postcode picks the winner.
    """

    __slots__ = ("source", "_exact", "_normalised", "_stations")

    def __init__(self, records: dict | None, source: str):
        self.source = source
        self._exact: dict[str, dict] = {}
        self._normalised: dict[str, list[dict]] = {}
        # Station bare-name -> records. Consulted only for station-shaped
        # queries, so "FINSBURY PARK N4" still means the park and only
        # "FINSBURY PARK STATION N4" reaches the station.
        self._stations: dict[str, list[dict]] = {}

        for key, value in (records or {}).items():
            parsed = _parse_override(value)
            if parsed is None:
                continue

            # Same-named alternates kept by the OSM harvest (a station and a
            # park both called "Finsbury Park"). The primary stays first.
            variants = [parsed]
            if isinstance(value, dict):
                for other in value.get("_others") or []:
                    parsed_other = _parse_override(other)
                    if parsed_other is not None:
                        variants.append(parsed_other)

            upper = str(key).upper().strip()
            stem, postcode = _split_postcode(upper)
            for variant in variants:
                if postcode and not variant.get("postal_district"):
                    variant["postal_district"] = postcode

            self._exact.setdefault(upper, parsed)
            self._exact.setdefault(stem, parsed)

            norm = _normalise_name(stem)
            if norm:
                self._normalised.setdefault(norm, []).extend(variants)

            for variant in variants:
                if variant.get("kind") in _STATION_KINDS or _looks_like_station(stem):
                    for station_key in _station_stems(stem):
                        self._stations.setdefault(station_key, []).append(variant)

    def __len__(self) -> int:
        return len(self._exact)

    def lookup(self, address: str) -> dict | None:
        upper = address.upper().strip()
        stem, postcode = _split_postcode(upper)
        wants_station = _looks_like_station(stem)

        # Gather every record this name could mean — an exact hit doesn't get
        # to short-circuit, because the name may have same-named alternates of
        # a different kind and only the query says which one is wanted.
        # Exact matches go first so they win ties in _best().
        candidates: list[dict] = []
        seen: set[int] = set()

        def add(record):
            if record is not None and id(record) not in seen:
                seen.add(id(record))
                candidates.append(record)

        for key in (upper, stem):
            add(self._exact.get(key))
        for record in self._normalised.get(_normalise_name(stem)) or []:
            add(record)
        if wants_station:
            for station_key in _station_stems(stem):
                for record in self._stations.get(station_key) or []:
                    add(record)

        if not candidates:
            return None
        return self._best(candidates, postcode, wants_station)

    @staticmethod
    def _best(candidates: list[dict], postcode: str | None, wants_station: bool) -> dict:
        """Pick among same-named places: kind first, then postal district.

        A winner whose recorded district disagrees with the query's postcode
        is returned *flagged* (``_district_mismatch``) rather than silently
        accepted — the single-candidate wrong-district hit is exactly how
        "SHORTLANDS W6" resolved to a point 18.5 km away in Bromley.
        """
        pool = candidates
        if wants_station:
            stations = [c for c in pool if c.get("kind") in _STATION_KINDS]
            if stations:
                pool = stations
        else:
            # "FINSBURY PARK N4" means the park, not the station of that name.
            non_stations = [c for c in pool if c.get("kind") not in _STATION_KINDS]
            if non_stations:
                pool = non_stations

        if postcode:
            for candidate in pool:
                if str(candidate.get("postal_district", "")).upper() == postcode:
                    return candidate
            chosen = pool[0]
            recorded = str(chosen.get("postal_district", "")).upper()
            if recorded and recorded != postcode:
                flagged = dict(chosen)
                flagged["_district_mismatch"] = True
                return flagged
        return pool[0]


class Gazetteer:
    """
    Resolves human-readable place names into a ``GazetteerEntry`` containing a
    graph node chosen by :func:`semantic_snap`.

    Four lookup tiers, tried in order:
      1. ``overrides`` — the manually curated dict (existing
         ``poi_overrides.json``). This is the authoritative source.
      2. ``knowledge_pois`` — the geocoded Knowledge Points List produced by
         ``krg generate pois``. Roughly three quarters of Blue Book run
         endpoints are Points List entries, so this is what keeps the run
         pipeline off the rate-limited public geocoder.
      3. ``osm_pois`` — harvested from OpenStreetMap via
         :mod:`knowledge_run_generator.osm_pois`. Fills in the long tail
         (pubs, stations, theatres, ...).
      4. the ``alias_index`` — endpoints that are plain streets ("ABERDEEN
         ROAD N5") resolve straight off the graph, with the postcode
         district picking between same-named streets across London.

    Earlier tiers always win, so human corrections can't be silently
    overwritten by a fresh harvest or a Points List re-geocode.

    Resolution is cached per graph so repeated lookups in a pipeline run are
    near-free.
    """

    def __init__(
        self,
        overrides: dict | None = None,
        alias_index: AliasIndex | None = None,
        osm_pois: dict | None = None,
        knowledge_pois: dict | None = None,
    ):
        self._overrides_raw: dict[str, Any] = overrides or {}
        # An override whose value is a name rather than a position is an alias:
        # "HOLLOWAY PRISON": "HM Prison Holloway" says the Blue Book's name for
        # a point differs from the one in the data, without pinning a
        # coordinate we'd then have to maintain.
        self._aliases: dict[str, str] = {}
        for key, value in self._overrides_raw.items():
            target = None
            if isinstance(value, str):
                target = value
            elif isinstance(value, dict) and isinstance(value.get("same_as"), str):
                target = value["same_as"]
            if target:
                self._aliases[_normalise_name(_split_postcode(key)[0])] = target

        self._tables = (
            _PoiTable(self._overrides_raw, "override"),
            _PoiTable(knowledge_pois, "knowledge_poi"),
            _PoiTable(osm_pois, "osm"),
        )

        self.alias_index = alias_index
        self._resolve_cache: dict[tuple[int, str], GazetteerEntry | None] = {}
        self._district_model: DistrictModel | None = None
        self._knowledge_pois_raw: dict[str, Any] = knowledge_pois or {}

    @property
    def district_model(self) -> DistrictModel:
        """Lazily built per-district geometry (median centre, p95 radius).

        Public because preflight consumes it for endpoint plausibility —
        the old private mean-based centroid was consulted only by the street
        tier, so tiers 1–3 could resolve an endpoint 18 km from its stated
        district without anything noticing.
        """
        if self._district_model is None:
            self._district_model = DistrictModel(self._knowledge_pois_raw)
        return self._district_model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup_coords(self, address: str) -> dict | None:
        """Return the raw coord dict for *address* from the first tier that
        knows it, tagged with ``_source``.

        Aliases are followed first, so a Blue Book name can be pointed at the
        name a data source actually uses without duplicating its coordinates.
        """
        if not address:
            return None
        address = self._follow_aliases(address)
        for table in self._tables:
            hit = table.lookup(address)
            if hit is not None:
                return self._tag_source(dict(hit), table.source)
        return None

    def _follow_aliases(self, address: str, max_hops: int = 4) -> str:
        """Rewrite *address* through the alias chain, if any.

        An alias that points at a name nothing knows is harmless: resolution
        simply continues to fail exactly as it did before, and falls through
        to the geocoder.
        """
        seen = set()
        for _ in range(max_hops):
            key = _normalise_name(_split_postcode(address)[0])
            target = self._aliases.get(key)
            if target is None or key in seen:
                break
            seen.add(key)
            address = target
        return address

    @staticmethod
    def _tag_source(record: dict, source: str) -> dict:
        record["_source"] = source
        return record

    def resolve(self, address: str, G) -> GazetteerEntry | None:
        """Resolve *address* → ``GazetteerEntry`` using semantic snap."""
        if not address:
            return None
        cache_key = (id(G), address.upper())
        if cache_key in self._resolve_cache:
            return self._resolve_cache[cache_key]

        record = self.lookup_coords(address)
        if record is None:
            # Tier 4: the endpoint may be a street rather than a named point.
            entry = self._resolve_street(address, G)
            self._resolve_cache[cache_key] = entry
            return entry

        lat = record["lat"]
        lon = record["lon"]
        on_street = record.get("on_street")
        approach_from = record.get("approach_from")

        snapped_node, snap_m = semantic_snap(
            G,
            lat,
            lon,
            on_street=on_street,
            alias_index=self.alias_index,
        )

        # A point-tier record in the *wrong postal district* whose name is a
        # street in the graph: the stated district is the Blue Book's, the
        # record's is somewhere else in London (or beyond), and the street
        # tier — which uses the district to pick the right same-named street —
        # is strictly more trustworthy. This is how "SHORTLANDS W6" stops
        # resolving into Bromley.
        if record.get("_district_mismatch") and not on_street and self.alias_index is not None:
            stem, _postcode = _split_postcode(self._follow_aliases(address))
            norm = _normalise_name(stem)
            if (norm in self.alias_index.canonical_to_nodes
                    or norm in self.alias_index.alias_to_canonical):
                street_entry = self._resolve_street(address, G)
                if street_entry is not None:
                    self._resolve_cache[cache_key] = street_entry
                    return street_entry

        # The same fallback when the record *claims* the right district but
        # its coordinate is implausible for it: a fresh Points List geocode
        # put "WARWICK GARDENS W14" in Battersea, 4.5 km from W14, while its
        # postal_district still read W14. Only machine tiers (Points List,
        # OSM) are second-guessed; a curated override means what it says.
        if (record.get("_source") != "override" and not on_street
                and self.alias_index is not None):
            stem, postcode = _split_postcode(self._follow_aliases(address))
            postcode = postcode or _split_postcode(address)[1]
            plausibility = self.district_model.check(postcode, lat, lon)
            norm = _normalise_name(stem)
            if (plausibility is not None and plausibility["status"] == "fail"
                    and (norm in self.alias_index.canonical_to_nodes
                         or norm in self.alias_index.alias_to_canonical)):
                street_entry = self._resolve_street(address, G)
                if street_entry is not None:
                    self._resolve_cache[cache_key] = street_entry
                    return street_entry

        # Street-name endpoints hijacked by a point tier: "YORK WAY N1" hits
        # the Points List's rooftop geocode before the street tier ever runs,
        # and the rooftop snaps 50m+ from the kerb. When the snap is that bad
        # and the name *is* a street (exact match only — no word-dropping, so
        # "KINGS CROSS STATION" can't collapse onto "KINGS CROSS"), the street
        # tier's answer is strictly better. ``on_street`` hints opt out: a
        # curated record knows where it wants to snap.
        if snap_m > _STREET_FALLBACK_SNAP_M and not on_street and self.alias_index is not None:
            stem, _postcode = _split_postcode(self._follow_aliases(address))
            norm = _normalise_name(stem)
            if (norm in self.alias_index.canonical_to_nodes
                    or norm in self.alias_index.alias_to_canonical):
                street_entry = self._resolve_street(address, G)
                if street_entry is not None:
                    self._resolve_cache[cache_key] = street_entry
                    return street_entry

        approach_node: int | None = None
        if approach_from and self.alias_index is not None:
            approach_candidates = self.alias_index.nodes_for(approach_from)
            approach_node = _closest_node(G, lat, lon, approach_candidates)

        entry = GazetteerEntry(
            canonical_name=address.upper(),
            lat=lat,
            lon=lon,
            snapped_node=snapped_node,
            snap_distance_m=snap_m,
            on_street=on_street,
            approach_node=approach_node,
            source=record.get("_source", "override"),
            district_mismatch=bool(record.get("_district_mismatch")),
        )
        self._resolve_cache[cache_key] = entry
        return entry

    # ------------------------------------------------------------------
    # Tier 4 — street endpoints
    # ------------------------------------------------------------------

    def _district_centroid(self, postcode: str | None) -> tuple[float, float] | None:
        """Robust centre of a postal district (see :class:`DistrictModel`).

        Used to pick between same-named streets: "HIGH STREET N1" and "HIGH
        STREET SE1" share a canonical name and therefore a single node set in
        the alias index, and the district is the only signal in the endpoint
        that separates them.
        """
        return self.district_model.centre(postcode)

    def _resolve_street(self, address: str, G) -> GazetteerEntry | None:
        """Resolve an endpoint that names a street rather than a point.

        Returns the node on that street closest to the endpoint's postal
        district, or the street's medoid node when there's no district to go
        on. The entry's coordinate *is* a graph node, so ``snap_distance_m``
        is 0 by construction.
        """
        if self.alias_index is None:
            return None

        # An alias may point at a street name, not only at a named point.
        _original_stem, original_postcode = _split_postcode(address)
        stem, postcode = _split_postcode(self._follow_aliases(address))
        postcode = postcode or original_postcode
        canonical = self.alias_index.resolve(stem)
        if canonical is None:
            return None

        nodes = [n for n in self.alias_index.nodes_for(stem) if n in G.nodes]
        if not nodes:
            return None
        # Same routability rule as semantic_snap: never anchor an endpoint on
        # a node the router can only enter or only leave.
        routable = [n for n in nodes if _node_is_routable(G, n)]
        nodes = routable or nodes

        anchor = self._district_centroid(postcode)
        if anchor is not None:
            node = _closest_node(G, anchor[0], anchor[1], nodes)
        else:
            node = _medoid_node(G, nodes)
        if node is None:
            return None

        n = G.nodes[node]
        return GazetteerEntry(
            canonical_name=canonical,
            lat=n["y"],
            lon=n["x"],
            snapped_node=node,
            snap_distance_m=0.0,
            on_street=canonical,
            source="street",
        )


# ---------------------------------------------------------------------------
# Semantic snap
# ---------------------------------------------------------------------------


def _medoid_node(G, nodes) -> int | None:
    """The node of *nodes* closest to their mean position.

    A centre-of-the-street anchor, which is also what a plain geocoder returns
    for a bare street name — but deterministic and offline.
    """
    nodes = list(nodes)
    if not nodes:
        return None
    mean_lat = sum(G.nodes[n]["y"] for n in nodes) / len(nodes)
    mean_lon = sum(G.nodes[n]["x"] for n in nodes) / len(nodes)
    return _closest_node(G, mean_lat, mean_lon, nodes)

def _closest_node(G, lat: float, lon: float, candidates) -> int | None:
    best: int | None = None
    best_d = float("inf")
    for nid in candidates:
        if nid not in G.nodes:
            continue
        n = G.nodes[nid]
        d = _haversine(lat, lon, n["y"], n["x"])
        if d < best_d:
            best_d = d
            best = nid
    return best


def semantic_snap(
    G,
    lat: float,
    lon: float,
    on_street: str | None = None,
    alias_index: AliasIndex | None = None,
    max_street_radius_m: float = 250.0,
    max_drivable_radius_m: float = 400.0,
) -> tuple[int, float]:
    """
    Snap (lat, lon) to a graph node with context awareness.

    Resolution order:

      1. If ``on_street`` is given and we have an alias index, find the
         closest node lying on that street. Bail if nothing is within
         ``max_street_radius_m`` — we'd rather fall through than snap to the
         wrong instance of a repeated street name.
      2. Snap to the nearest node that sits on a drive-permitted edge whose
         highway class is not in :data:`_AVOID_HIGHWAY_CLASSES` (i.e., not a
         motorway/trunk/slip). This is what eliminates the "wrong side of
         the dual carriageway" class of failures.
      3. Geometric nearest node (legacy behaviour).

    Returns ``(node_id, snap_distance_metres)``.
    """
    # 1. on_street hint
    if on_street and alias_index is not None:
        nodes = alias_index.nodes_for(on_street)
        if nodes:
            # Prefer nodes a route can terminate at and depart from; a street's
            # one-way tip is as much of a trap here as in the radius search.
            routable = [n for n in nodes if n in G.nodes and _node_is_routable(G, n)]
            best = _closest_node(G, lat, lon, routable or nodes)
            if best is not None:
                n = G.nodes[best]
                d = _haversine(lat, lon, n["y"], n["x"])
                if d <= max_street_radius_m:
                    return best, d

    # Fall-throughs iterate the whole graph once; cache a BallTree for speed.
    tree, ids = _get_ball_tree(G)
    import numpy as np

    # 2. Prefer drivable non-avoided nodes within a reasonable radius
    target = np.deg2rad([[lat, lon]])
    # query the k nearest then filter; k=25 is enough for dense London
    dists, idxs = tree.query(target, k=min(25, len(ids)))
    best_id: int | None = None
    best_d = float("inf")
    for dist_rad, idx in zip(dists[0], idxs[0]):
        nid = int(ids[idx])
        d = dist_rad * 6_371_000
        if d > max_drivable_radius_m:
            break
        if _node_is_routable(G, nid):
            best_id = nid
            best_d = d
            break
    if best_id is not None:
        return best_id, best_d

    # 3. Absolute nearest node (may be on a motorway/trunk; caller will see the
    #    distance and can warn).
    nid = int(ids[idxs[0][0]])
    n = G.nodes[nid]
    d = _haversine(lat, lon, n["y"], n["x"])
    return nid, d


# Cached BallTree per graph instance — mirrors the existing geocoder cache but
# lives here so the snap logic is self-contained.
_graph_trees: dict[int, tuple[Any, Any]] = {}


def _get_ball_tree(G):
    import numpy as np
    import osmnx as ox
    from sklearn.neighbors import BallTree

    gid = id(G)
    cached = _graph_trees.get(gid)
    if cached is not None:
        return cached
    nodes = ox.graph_to_gdfs(G, edges=False, node_geometry=False)[["x", "y"]]
    nodes_rad = np.deg2rad(nodes[["y", "x"]].values)
    tree = BallTree(nodes_rad, metric="haversine")
    _graph_trees[gid] = (tree, nodes.index.values)
    return _graph_trees[gid]


def load_overrides(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

@dataclass
class PreflightReport:
    ok: bool
    reasons: list = None
    warnings: list = None
    start_snap_m: float | None = None
    end_snap_m: float | None = None
    unresolved_streets: list = None
    start_district_m: float | None = None
    end_district_m: float | None = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []
        if self.warnings is None:
            self.warnings = []
        if self.unresolved_streets is None:
            self.unresolved_streets = []


def _check_endpoint_district(report, label, entry, name, district_model):
    """Plausibility of one endpoint against its stated postal district."""
    if entry is None or district_model is None or not name:
        return
    if entry.source == "override":
        # Curator-pinned coordinates are the explicit-override escape hatch:
        # they also cover the handful of Blue Book postcode typos
        # ("SPITALFIELDS MARKET E10") where the *stated* district is the
        # wrong party in the disagreement.
        return
    district = _split_postcode(name)[1]
    if not district:
        return
    verdict = district_model.check(district, entry.lat, entry.lon)
    if verdict is None:
        return
    setattr(report, f"{label}_district_m", verdict["distance_m"])
    if verdict["status"] == "fail":
        report.ok = False
        report.reasons.append(
            f"{label} resolved {verdict['distance_m']:.0f}m from the centre of "
            f"{district} (p95 radius {verdict['p95_radius_m']:.0f}m) — wrong place"
        )
        return
    if verdict["status"] == "warn":
        report.warnings.append(
            f"{label} is {verdict['distance_m']:.0f}m from the centre of "
            f"{district} (p95 {verdict['p95_radius_m']:.0f}m)"
        )
    if entry.district_mismatch:
        report.warnings.append(
            f"{label} record's postal district disagrees with {district}"
        )


def preflight_run(
    start_entry: GazetteerEntry | None,
    end_entry: GazetteerEntry | None,
    intermediate_streets: list,
    alias_index: AliasIndex | None,
    max_snap_m: float = 50.0,
    warn_snap_m: float = 20.0,
    known_junctions: set | None = None,
    district_model: DistrictModel | None = None,
    start_name: str | None = None,
    end_name: str | None = None,
) -> PreflightReport:
    """
    Sanity-check a run *before* we spend time routing it.

    Fails the run (``ok = False``) when:
      * the start or end didn't resolve through the gazetteer, or
      * the snap distance is ``> max_snap_m`` (the POI probably points at the
        wrong side of a motorway or onto a pedestrian-only platform), or
      * an endpoint resolved implausibly far from its stated postal district
        (``district_model`` given): beyond ``max(p95 × 1.5, 2500 m)`` of the
        district's robust centre. This is what catches "SHORTLANDS W6"
        resolving into Bromley — snap distance alone cannot.

    Warns (``ok = True`` with entries in ``warnings``) when:
      * the snap distance is ``> warn_snap_m``, or
      * an intermediate street can't be resolved by the alias index, or
      * an endpoint is beyond the district's p95 radius (but under the fail
        threshold), or its record's district disagrees with the stated one.

    The distinction matters for triage: failures should be fixed in
    ``poi_overrides.json``; warnings are usually OK but flag runs to audit.
    """
    report = PreflightReport(ok=True)

    if start_entry is None:
        report.ok = False
        report.reasons.append("start not resolved by gazetteer")
    else:
        report.start_snap_m = start_entry.snap_distance_m
        if start_entry.snap_distance_m > max_snap_m:
            report.ok = False
            report.reasons.append(
                f"start snapped {start_entry.snap_distance_m:.0f}m away (>{max_snap_m:.0f})"
            )
        elif start_entry.snap_distance_m > warn_snap_m:
            report.warnings.append(
                f"start snap distance {start_entry.snap_distance_m:.0f}m"
            )

    if end_entry is None:
        report.ok = False
        report.reasons.append("end not resolved by gazetteer")
    else:
        report.end_snap_m = end_entry.snap_distance_m
        if end_entry.snap_distance_m > max_snap_m:
            report.ok = False
            report.reasons.append(
                f"end snapped {end_entry.snap_distance_m:.0f}m away (>{max_snap_m:.0f})"
            )
        elif end_entry.snap_distance_m > warn_snap_m:
            report.warnings.append(
                f"end snap distance {end_entry.snap_distance_m:.0f}m"
            )

    _check_endpoint_district(report, "start", start_entry, start_name, district_model)
    _check_endpoint_district(report, "end", end_entry, end_name, district_model)

    if alias_index is not None and intermediate_streets:
        # Junction names resolved by the curated junction index count as
        # resolved here too, so preflight agrees with the waypoint builder.
        unresolved = [
            s for s in intermediate_streets
            if alias_index.resolve(s) is None
            and _normalise_name(s) not in (known_junctions or ())
        ]
        report.unresolved_streets = unresolved
        if unresolved:
            report.warnings.append(
                f"{len(unresolved)} unresolved streets: {unresolved[:5]}"
                + ("..." if len(unresolved) > 5 else "")
            )

    return report
