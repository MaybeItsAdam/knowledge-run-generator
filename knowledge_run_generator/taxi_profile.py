"""The taxi-legal street graph: which OSM ways and nodes a London cab may use.

osmnx's stock ``drive`` profile is a private-car profile, and it is wrong for a
taxi in both directions:

* it **drops** ways a cab may use: bus gates tagged ``motor_vehicle=no`` +
  ``psv=yes`` / ``taxi=yes``, and it has no notion of taxi contraflows
  (``oneway:psv=no`` / ``oneway:taxi=no``);
* it **keeps** ways and nodes a cab may not use: ``access=no`` bus-only roads
  and ``highway=busway``, and every bollard, planter and bus trap. OSM maps a
  low-traffic-neighbourhood (LTN) modal filter as a ``barrier=*`` *node* on an
  otherwise ordinary residential way, so osmnx sees a drivable street straight
  through the filter.

This module owns the tag rules (pure functions, unit-tested), the build of the
``taxi`` graph from them, and :class:`TaxiRules`, the per-route legality check
that the QA gate runs on every shipped route.

Tag rules, in full (the README-grade record of every decision):

Ways
    Downloaded: every ``highway`` a motor vehicle can be on
    (:data:`ROAD_HIGHWAYS`, including ``busway``), whatever its access tags,
    plus ``highway=pedestrian`` only where it explicitly admits taxis. Access
    is then decided here, not in the Overpass filter.

    * The access value for a taxi is the *most specific* tag present, in the
      order ``taxi``, ``psv``, ``motorcar``, ``motor_vehicle``, ``vehicle``,
      ``access`` (:data:`TAXI_ACCESS_CHAIN`). OSM puts ``taxi`` under ``psv``;
      a London cab is also a motor car, so ``motorcar=no`` binds it when no
      psv/taxi tag says otherwise. So ``motor_vehicle=no`` + ``psv=yes`` (a
      bus gate that admits taxis) is open; ``access=no`` + ``bus=yes`` (a
      bus-only road) is closed, because ``bus`` does not cover taxis.
    * ``yes``/``designated``/``permissive``/``official``/``discouraged``
      (legal, if unwelcome) are open. ``destination``/``delivery``/
      ``customers``/``local`` are *destination-only*: kept in the graph, but a
      route may only use them at its start or its end, never to pass through.
      ``no``/``private``/``permit``/``residents``/``agricultural``/
      ``forestry``/``emergency``/``military`` are closed. Unrecognised values
      default to open (they are almost all typos of ``yes``).
    * No tag at all: open, except ``highway=busway`` (implied bus-only, closed)
      and ``highway=pedestrian`` (implied closed; only downloaded when a tag
      opens it).
    * ``*:conditional`` tags (timed school streets, peak-hour bans) are
      ignored: a Knowledge run is recited without a clock.
    * Bus lanes are *lane* tags (``busway:left=lane``, ``lanes:psv``) on a way
      that stays open to everyone, so they need no handling: taxis may use
      London bus lanes.
    * ``highway=service`` (forecourts, estates, car parks, taxi ranks) stays
      out, as in the ``drive`` profile: a call is made on public highways.
    * Contraflow: a one-way way tagged ``oneway:taxi=no``, or
      ``oneway:psv=no`` without ``oneway:taxi=yes``, gets its reverse edge.
      ``oneway:bus=no`` alone does not: a bus contraflow does not admit taxis.
    * Any other ``highway`` (cycleway, footway, path, ...) is implied closed.
    * Hammersmith Bridge: OSM maps the closed deck as ``highway=cycleway``,
      ``disused:highway=primary``, ``motor_vehicle=no`` (ways 7587403 and
      314914065, checked 2026-09), and the Castelnau approach the same way.
      It is not downloaded (cycleway), and would be closed by
      ``motor_vehicle=no`` if it were. No special case is needed; a unit test
      pins the real tags. It is long-running, so it is deliberately **not**
      a temporary closure (below): runs that need it ship crow-flies.
    * Temporary closures (:data:`TEMPORARY_CLOSURES`): OSM ways closed only
      for a while (a bridge shut for repairs) are treated as **open**, because
      a temporary closure must not permanently reroute a run or move its
      endpoint. A run whose shipped route crosses one carries a
      ``route_notice`` saying so ("Albert Bridge is temporarily closed. The
      route shown is the normal one."). Each entry records why it is there
      and when to remove it: once OSM reopens the way, delete the entry.

Nodes
    A ``barrier`` node in :data:`BLOCKING_BARRIERS` (bollard, bus_trap, block,
    planter, jersey_barrier, sump_buster, chain, cycle_barrier, ...) blocks
    passage unless the node itself admits a taxi through the same access
    chain (``taxi``/``psv``/``motorcar``/``motor_vehicle``/``vehicle``/
    ``access`` = yes, designated, permissive, destination). Gates
    (:data:`GATE_BARRIERS`) are the other way round: open unless the chain
    says ``no``/``private``/... . Anything else (kerb, height_restrictor,
    cattle_grid, entrance, toll_booth, ...) is passable. Every edge touching a
    blocking node is cut, which is what makes an LTN filter a dead end from
    both sides.

Turns
    ``no_*`` restriction relations are enforced, except where the relation's
    ``except`` tag lists ``psv`` or ``taxi`` (see
    :func:`restriction_exempts_taxi`); ``only_*`` relations stay unenforced,
    as before (see ``validator._build_prohibited_set``).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Tag rules
# ---------------------------------------------------------------------------

ROAD_HIGHWAYS = (
    "motorway", "motorway_link", "trunk", "trunk_link", "primary",
    "primary_link", "secondary", "secondary_link", "tertiary",
    "tertiary_link", "unclassified", "residential", "living_street", "road",
    "busway",
)

# Highways whose *implied* access for a taxi is "no" when no tag opens them.
IMPLIED_CLOSED_HIGHWAYS = {"busway", "pedestrian", "bus_guideway"}

TAXI_ACCESS_CHAIN = ("taxi", "psv", "motorcar", "motor_vehicle", "vehicle",
                     "access")

OPEN_VALUES = {"yes", "designated", "permissive", "official", "discouraged",
               "opposite_lane", "use_sidepath"}
DESTINATION_VALUES = {"destination", "delivery", "customers", "local"}
CLOSED_VALUES = {"no", "private", "permit", "residents", "agricultural",
                 "forestry", "emergency", "military"}

# Barrier node types that stop a motor vehicle unless the node says a taxi
# may pass. ``yes`` is the unspecified barrier.
BLOCKING_BARRIERS = {
    "bollard", "bus_trap", "block", "planter", "jersey_barrier",
    "sump_buster", "chain", "cycle_barrier", "motorcycle_barrier",
    "kissing_gate", "stile", "turnstile", "full-height_turnstile", "wedge",
    "spikes", "rocks", "log", "debris", "fence", "wall", "yes",
    "pedestrian only",
}
# Barrier node types that are open unless the node's access says otherwise.
GATE_BARRIERS = {"gate", "lift_gate", "swing_gate", "sliding_gate",
                 "hampshire_gate", "bump_gate", "gate;entrance"}

# Extra OSM tags osmnx must keep for the rules above.
WAY_TAGS = ("access", "motor_vehicle", "motorcar", "vehicle", "psv", "taxi",
            "bus", "oneway:psv", "oneway:taxi", "oneway:bus")
NODE_TAGS = ("barrier", "bollard", "access", "motor_vehicle", "motorcar",
             "vehicle", "psv", "taxi", "bus")


@dataclass(frozen=True)
class TemporaryClosure:
    """A closure OSM maps with plain (unconditional) access tags, but which
    is temporary. The taxi graph treats its ways as open, and a route that
    crosses them carries :attr:`notice`. App-facing strings: no em dashes."""

    name: str
    ways: tuple
    notice: str
    note: str


TEMPORARY_CLOSURES: tuple = (
    TemporaryClosure(
        name="Albert Bridge",
        # Every way named Albert Bridge tagged access=no, checked against
        # the OSM API on 2026-09-27 (edited in changesets 178515756 and
        # later, 2026-02-13 onwards). No barrier nodes were added with them.
        ways=(23017722, 247573028, 326272665, 536751562, 591291850,
              591291853, 591291859, 1188809166, 1259294888, 1259294890,
              1259294891, 1477849562, 1477849563, 1477849584, 1477849587,
              1477849588),
        notice="Albert Bridge is temporarily closed. The route shown is the normal one.",
        note=("Closed to motor vehicles since 2026-02-07 for repairs (RBKC), "
              "reopening expected in 2027. Temporarily closed as of 2026-09, "
              "per the user; treat as open. Remove once OSM reopens it."),
    ),
)

_TEMPORARY_BY_WAY = {int(w): c for c in TEMPORARY_CLOSURES for w in c.ways}


def temporary_closure(way_id) -> "TemporaryClosure | None":
    """The temporary closure an OSM way belongs to, if any."""
    try:
        return _TEMPORARY_BY_WAY.get(int(way_id))
    except (TypeError, ValueError):
        return None


def _edge_osmids(data) -> set:
    osmids = data.get("osmid")
    osmids = osmids if isinstance(osmids, list) else [osmids]
    ids = set()
    for o in osmids:
        try:
            ids.add(int(o))
        except (TypeError, ValueError):
            pass
    return ids


def route_notices(G, route_nodes) -> list[str]:
    """The notices of every temporary closure a route crosses, in route
    order, each once."""
    out: list[str] = []
    for u, v in zip(route_nodes or [], (route_nodes or [])[1:]):
        bundle = G.get_edge_data(u, v) or {}
        if not bundle:
            continue
        data = min(bundle.values(), key=lambda d: d.get("length", float("inf")))
        for wid in sorted(_edge_osmids(data)):
            c = temporary_closure(wid)
            if c is not None and c.notice not in out:
                out.append(c.notice)
    return out


def _values(raw) -> list[str]:
    """A tag value as a list of lower-case parts (``a;b`` and list attrs)."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple, set)) else [raw]
    out = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, float) and math.isnan(item):
            continue
        for part in str(item).split(";"):
            part = part.strip().lower()
            if part:
                out.append(part)
    return out


def classify_access_value(value: str) -> str:
    """``open`` / ``destination`` / ``closed`` for one access value."""
    v = (value or "").strip().lower()
    if v in CLOSED_VALUES:
        return "closed"
    if v in DESTINATION_VALUES:
        return "destination"
    return "open"


def resolve_taxi_access(tags: dict, implied: str = "open") -> tuple[str, str | None]:
    """Resolve a taxi's access from an OSM tag dict.

    Returns ``(verdict, deciding_tag)`` where verdict is ``open``,
    ``destination`` or ``closed`` and ``deciding_tag`` is e.g.
    ``"motor_vehicle=no"`` (``None`` when the implied default decided).
    """
    for key in TAXI_ACCESS_CHAIN:
        vals = _values(tags.get(key))
        if not vals:
            continue
        # ``yes;destination`` style lists: the most permissive part wins,
        # because each part is an alternative the sign allows.
        verdicts = [classify_access_value(v) for v in vals]
        for want in ("open", "destination", "closed"):
            if want in verdicts:
                return want, f"{key}={vals[verdicts.index(want)]}"
    return implied, None


def taxi_way_access(tags: dict) -> tuple[str, str | None]:
    """Access verdict for a way: ``open`` / ``destination`` / ``closed``."""
    highways = set(_values(tags.get("highway")))
    open_roads = set(ROAD_HIGHWAYS) - IMPLIED_CLOSED_HIGHWAYS
    implied = "open" if (not highways or highways & open_roads) else "closed"
    verdict, why = resolve_taxi_access(tags, implied=implied)
    if why is None and implied == "closed":
        why = f"highway={sorted(highways)[0]}"
    return verdict, why


def taxi_contraflow(tags: dict) -> bool:
    """True when a one-way way is two-way for taxis."""
    taxi = _values(tags.get("oneway:taxi"))
    if taxi:
        return "no" in taxi
    return "no" in _values(tags.get("oneway:psv"))


def barrier_blocks_taxi(tags: dict) -> tuple[bool, str | None]:
    """Does a node stop a taxi? Returns ``(blocks, reason)``."""
    kinds = _values(tags.get("barrier"))
    if not kinds:
        return False, None
    kind = kinds[0]
    raw = str(tags.get("barrier")).strip().lower()
    if raw in GATE_BARRIERS:
        kind = raw
    if kind in BLOCKING_BARRIERS:
        verdict, why = resolve_taxi_access(tags, implied="closed")
        if verdict == "closed":
            return True, f"barrier={kind}" + (f" ({why})" if why else "")
        return False, None
    if kind in GATE_BARRIERS:
        verdict, why = resolve_taxi_access(tags, implied="open")
        if verdict == "closed":
            return True, f"barrier={kind} ({why})"
        return False, None
    return False, None


def restriction_exempts_taxi(tags: dict) -> bool:
    """A turn restriction relation that does not bind a taxi."""
    return bool({"psv", "taxi"} & set(_values(tags.get("except"))))


# ---------------------------------------------------------------------------
# Graph build
# ---------------------------------------------------------------------------

# v2: temporary closures (Albert Bridge) are kept open.
TAXI_GRAPH_VERSION = 2
SIDECAR_SUFFIX = ".taxi_rules.json"

_ROAD_RE = "|".join(ROAD_HIGHWAYS)
TAXI_OVERPASS_FILTERS = [
    f'["highway"~"^({_ROAD_RE})$"]["area"!~"yes"]',
] + [
    f'["highway"="pedestrian"]["area"!~"yes"]["{key}"~"^(yes|designated|permissive)$"]'
    for key in ("taxi", "psv", "motorcar", "motor_vehicle")
]


def _edge_tags(data: dict) -> dict:
    return {k: data.get(k) for k in ("highway",) + WAY_TAGS if data.get(k) is not None}


def _first_osmid(data):
    oid = data.get("osmid")
    if isinstance(oid, list):
        oid = oid[0]
    try:
        return int(oid)
    except (TypeError, ValueError):
        return None


def _first_name(data):
    name = data.get("name")
    if isinstance(name, list):
        name = name[0] if name else None
    if isinstance(name, float):
        return None
    return name


def apply_taxi_rules(G) -> dict:
    """Apply the tag rules to an **unsimplified** osmnx graph, in place.

    Returns the sidecar record: every way closed or destination-only, every
    contraflow added and every barrier node cut, so that a route can be
    checked (and a failure explained) without the graph.
    """
    closed_ways: dict = {}
    destination_ways: dict = {}
    contraflow_ways: dict = {}
    barriers: dict = {}
    temporary: dict = {}

    # 1. Way access. A temporary closure is kept open (see the docstring).
    drop = []
    for u, v, k, data in G.edges(keys=True, data=True):
        verdict, why = taxi_way_access(_edge_tags(data))
        wid = _first_osmid(data)
        closure = temporary_closure(wid)
        if closure is not None:
            temporary[str(wid)] = {"name": closure.name, "notice": closure.notice,
                                   "tagged": verdict, "reason": why}
            verdict = "open"
        if verdict == "closed":
            drop.append((u, v, k))
            if wid is not None:
                closed_ways[str(wid)] = {"name": _first_name(data), "reason": why,
                                         "highway": data.get("highway")}
        elif verdict == "destination":
            data["taxi_access"] = "destination"
            if wid is not None:
                destination_ways[str(wid)] = {"name": _first_name(data), "reason": why}
        else:
            data["taxi_access"] = "yes"
    G.remove_edges_from(drop)

    # 2. Contraflow for taxis on one-way ways.
    add = []
    for u, v, k, data in G.edges(keys=True, data=True):
        if not data.get("oneway") or not taxi_contraflow(_edge_tags(data)):
            continue
        if G.has_edge(v, u):
            continue
        rev = dict(data)
        rev["reversed"] = not bool(data.get("reversed", False))
        rev["taxi_contraflow"] = "yes"
        if "geometry" in rev:
            from shapely.geometry import LineString
            rev["geometry"] = LineString(list(rev["geometry"].coords)[::-1])
        add.append((v, u, rev))
        wid = _first_osmid(data)
        if wid is not None:
            contraflow_ways[str(wid)] = {"name": _first_name(data)}
    for v, u, rev in add:
        G.add_edge(v, u, **rev)

    # 3. Barrier nodes: cut every edge touching a blocking node.
    for n, ndata in list(G.nodes(data=True)):
        if ndata.get("barrier") is None:
            continue
        tags = {k: ndata.get(k) for k in NODE_TAGS if ndata.get(k) is not None}
        blocks, why = barrier_blocks_taxi(tags)
        if not blocks:
            continue
        incident = list(G.in_edges(n, keys=True, data=True)) + list(
            G.out_edges(n, keys=True, data=True))
        names = sorted({str(_first_name(d)) for *_e, d in incident if _first_name(d)})
        ways = sorted({_first_osmid(d) for *_e, d in incident if _first_osmid(d)})
        barriers[str(n)] = {
            "lat": ndata["y"], "lon": ndata["x"], "reason": why,
            "tags": {k: str(v) for k, v in tags.items()},
            "names": names, "ways": ways,
        }
        G.remove_edges_from([(a, b, k) for a, b, k, _d in incident])

    return {
        "version": TAXI_GRAPH_VERSION,
        "closed_ways": closed_ways,
        "destination_ways": destination_ways,
        "contraflow_ways": contraflow_ways,
        "barriers": barriers,
        "temporary_closures": temporary,
    }


def build_taxi_graph(place_name: str = "Greater London, UK"):
    """Download and build the taxi graph. Returns ``(G, sidecar)``."""
    import osmnx as ox

    for tag in WAY_TAGS:
        if tag not in ox.settings.useful_tags_way:
            ox.settings.useful_tags_way = list(ox.settings.useful_tags_way) + [tag]
    for tag in NODE_TAGS:
        if tag not in ox.settings.useful_tags_node:
            ox.settings.useful_tags_node = list(ox.settings.useful_tags_node) + [tag]

    G = ox.graph_from_place(place_name, custom_filter=TAXI_OVERPASS_FILTERS,
                            simplify=False, retain_all=True)
    sidecar = apply_taxi_rules(G)
    G.remove_nodes_from([n for n in list(G.nodes) if G.degree(n) == 0])
    # Keep open gates as graph nodes, so the router can see them; split
    # edges where the access regime changes so destination-only stretches
    # stay separately identifiable.
    G = ox.simplify_graph(G, node_attrs_include=["barrier"],
                          edge_attrs_differ=["taxi_access", "taxi_contraflow"])
    G = ox.truncate.largest_component(G, strongly=False)
    G.graph["krg_profile"] = "taxi"
    G.graph["krg_taxi_graph_version"] = TAXI_GRAPH_VERSION
    return G, sidecar


# ---------------------------------------------------------------------------
# Route legality against the rules
# ---------------------------------------------------------------------------

def _coord_key(lon, lat):
    return (round(float(lon), 7), round(float(lat), 7))


@dataclass
class TaxiRules:
    """Closed ways, destination ways and blocking barriers, for checking a
    route drawn on *any* osmnx graph of the same OSM data (the ``taxi`` graph
    the router uses, or an older ``drive`` graph a shipped route came from).
    """

    closed_ways: dict = field(default_factory=dict)
    destination_ways: dict = field(default_factory=dict)
    barriers: dict = field(default_factory=dict)
    contraflow_ways: dict = field(default_factory=dict)
    # Upper-cased street name -> [[lat, lon, highway], ...] of named sections
    # that are not motor roads (see :func:`fetch_filtered_streets`).
    filtered_streets: dict = field(default_factory=dict)

    def __post_init__(self):
        self._barrier_ids = {int(k) for k in self.barriers}
        self._barrier_coords = {
            _coord_key(b["lon"], b["lat"]): int(k) for k, b in self.barriers.items()}
        self._closed = {int(k) for k in self.closed_ways}
        self._dest = {int(k) for k in self.destination_ways}

    @classmethod
    def from_sidecar(cls, blob: dict) -> "TaxiRules":
        return cls(closed_ways=blob.get("closed_ways", {}),
                   destination_ways=blob.get("destination_ways", {}),
                   barriers=blob.get("barriers", {}),
                   contraflow_ways=blob.get("contraflow_ways", {}))

    @classmethod
    def load(cls, path) -> "TaxiRules | None":
        path = Path(path)
        if not path.exists():
            return None
        return cls.from_sidecar(json.loads(path.read_text()))

    def barrier_street(self, node_id) -> str:
        b = self.barriers.get(str(node_id)) or {}
        names = b.get("names") or []
        return names[0] if names else "an unnamed street"

    def filtered_sections(self, name: str) -> list:
        """Non-motor sections (cycleway, footway, ...) carrying ``name``."""
        return self.filtered_streets.get(str(name).upper(), [])

    def names_with_barriers(self) -> dict:
        """Upper-cased street name -> list of blocking barrier node ids."""
        out: dict = {}
        for k, b in self.barriers.items():
            for name in b.get("names") or []:
                out.setdefault(str(name).upper(), []).append(int(k))
        return out

    def check_route(self, G, route_nodes) -> list[dict]:
        """Every taxi-legality violation along ``route_nodes`` on graph ``G``.

        * ``barrier``: the route passes a blocking barrier node (matched by
          node id, or by coordinate against the edge geometry, so a barrier
          inside a simplified edge is still seen);
        * ``closed_way``: an edge belongs to a way closed to taxis;
        * ``destination_through``: a destination-only edge used between two
          open edges, i.e. to pass through rather than to start or finish.
        """
        violations: list[dict] = []
        if not route_nodes or len(route_nodes) < 2:
            return violations
        dest_flags = []
        seen_barriers = set()
        for i in range(len(route_nodes) - 1):
            u, v = route_nodes[i], route_nodes[i + 1]
            bundle = G.get_edge_data(u, v) or {}
            if not bundle:
                dest_flags.append(False)
                continue
            data = min(bundle.values(), key=lambda d: d.get("length", float("inf")))
            osmids = data.get("osmid")
            osmids = osmids if isinstance(osmids, list) else [osmids]
            ids = set()
            for o in osmids:
                try:
                    ids.add(int(o))
                except (TypeError, ValueError):
                    pass
            closed = ids & self._closed
            if closed:
                wid = sorted(closed)[0]
                info = self.closed_ways.get(str(wid), {})
                violations.append({"kind": "closed_way", "index": i, "way": wid,
                                   "name": info.get("name"),
                                   "reason": info.get("reason")})
            is_dest = (str(data.get("taxi_access")) == "destination"
                       or bool(ids & self._dest))
            dest_flags.append(is_dest)
            hits = set()
            for n in (u, v):
                if int(n) in self._barrier_ids:
                    hits.add(int(n))
            geom = data.get("geometry")
            if geom is not None:
                for x, y in list(geom.coords):
                    hit = self._barrier_coords.get(_coord_key(x, y))
                    if hit is not None:
                        hits.add(hit)
            for hit in sorted(hits - seen_barriers):
                seen_barriers.add(hit)
                b = self.barriers.get(str(hit), {})
                violations.append({"kind": "barrier", "index": i, "node": hit,
                                   "name": self.barrier_street(hit),
                                   "reason": b.get("reason"),
                                   "lat": b.get("lat"), "lon": b.get("lon")})
        # Destination-only edges may lead in from the start or out to the
        # end, never sit between two open stretches.
        open_idx = [i for i, d in enumerate(dest_flags) if not d]
        if open_idx:
            first_open, last_open = open_idx[0], open_idx[-1]
            for i, d in enumerate(dest_flags):
                if d and first_open < i < last_open:
                    data = min((G.get_edge_data(route_nodes[i], route_nodes[i + 1]) or {"_": {}}).values(),
                               key=lambda e: e.get("length", float("inf")))
                    violations.append({"kind": "destination_through", "index": i,
                                       "name": _first_name(data),
                                       "reason": "access only"})
                    break
        return violations


def describe_barrier(reason, name) -> str:
    """"Modal filter (bollard) on X" / "Closed gate (lift gate) on X"."""
    raw = str(reason or "barrier=yes").split("=", 1)[-1].split(" ", 1)[0]
    kind = raw.replace("_", " ")
    if raw in GATE_BARRIERS:
        return f"Closed gate ({kind}) on {name}"
    if raw == "yes":
        return f"Barrier on {name}"
    return f"Modal filter ({kind}) on {name}"


# A road section re-tagged as ``highway=cycleway`` (or footway, pedestrian,
# path) under the street's own name is the other way OSM maps a modal filter:
# Braes Street and Halton Road N1 are mapped like that, with no barrier node.
FILTERED_SECTION_HIGHWAYS = ("cycleway", "footway", "pedestrian", "path")


ROAD_SECTION_SIGNS = ("maxspeed", "motor_vehicle", "motorcar", "vehicle",
                      "emergency", "disabled", "disused:highway",
                      "was:highway", "note:covid19")


def filtered_streets_query(bbox) -> str:
    south, west, north, east = bbox
    hw = "|".join(FILTERED_SECTION_HIGHWAYS)
    return (f"[out:json][timeout:180];\n"
            f'way["highway"~"^({hw})$"]["name"]({south},{west},{north},{east});\n'
            f"out tags center;")


def parse_filtered_streets(elements) -> dict:
    """Overpass ``out tags center`` elements -> name -> [[lat, lon, highway]].

    Only names ending in a road word (Street, Road, ...) are kept, and only
    sections carrying a tag that belongs to a carriageway rather than a path
    (:data:`ROAD_SECTION_SIGNS`: ``maxspeed``, ``motor_vehicle``,
    ``emergency``, ``disabled``, ``disused:highway``, ...). That is what
    separates a closed stretch of road (Braes Street N1: ``highway=cycleway``
    + ``maxspeed`` + ``emergency=yes``) from a pavement or a cycle track that
    shares the street's name (Wilton Road SW1, Temple Place WC2).
    """
    road_words = ("STREET", "ROAD", "LANE", "AVENUE", "GROVE", "PLACE",
                  "GARDENS", "TERRACE", "SQUARE", "HILL", "WAY", "WALK",
                  "CRESCENT", "ROW", "PARADE", "RISE", "MEWS", "YARD", "CLOSE")
    out: dict = {}
    for e in elements:
        tags = e.get("tags") or {}
        name = str(tags.get("name") or "").strip()
        centre = e.get("center") or {}
        if not name or "lat" not in centre:
            continue
        if not name.upper().split()[-1] in road_words:
            continue
        if not any(k in tags for k in ROAD_SECTION_SIGNS):
            continue
        out.setdefault(name.upper(), []).append(
            [round(centre["lat"], 6), round(centre["lon"], 6), tags.get("highway")])
    return out


def describe_filtered_section(name: str) -> str:
    return f"Modal filter on {name} (closed to motor traffic)"


def describe_violation(v: dict) -> str:
    """App-facing sentence for one violation (no em dashes, plain English)."""
    name = v.get("name") or "an unnamed street"
    if v["kind"] == "barrier":
        return describe_barrier(v.get("reason"), name)
    if v["kind"] == "closed_way":
        return f"{name} is closed to taxis ({v.get('reason')})"
    return f"{name} is access only"
