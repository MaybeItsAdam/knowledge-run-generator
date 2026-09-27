"""
Ordered constraint compilation for Blue Book runs (ROADMAP Stage 2).

A Blue Book line is not always a street: it can be a named junction
("VAUXHALL CROSS"), a gyratory the trade names but OSM doesn't ("BRIDGEND
CIRCUS"), or a bare "ROUNDABOUT" marker whose identity is only recoverable
from the streets either side of it. Compiling the sequence into typed
constraints keeps those distinctions explicit instead of forcing everything
through a street-name index that cannot represent them:

  * ``STREET`` — satisfied by traversing an edge whose normalised name set
    contains ``key``.
  * ``NODE``   — satisfied by reaching any member of ``key`` (a frozenset of
    graph nodes: a junction's meeting points, a roundabout ring).

A line that resolves through nothing becomes an explicit *gap* — never a
guessed constraint. The legacy resolver returned its input on total failure,
which was harmless under a routing discount and is fatal under a hard
constraint.

``source`` records which tier resolved the line, because the tier is the
confidence: ``exact``/``junction``/``abbrev`` are trustworthy enough to make
a constraint *hard* (the ordered router must satisfy it); ``fuzzy``,
``word_removal`` and ``ring`` are guesses the router may demote when they
prove unsatisfiable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .aliases import normalise

# Sources confident enough that the ordered router treats their constraints
# as hard requirements. Everything else starts soft (``hard=False``).
HARD_SOURCES = frozenset({"exact", "junction", "abbrev"})

# In-string abbreviation expansions used by the street matcher. The canonical
# normaliser already expands whole tokens; these catch the forms it can't
# (shared with the legacy matcher in run_pipeline, which imports them here).
_ABBREVIATIONS = {
    " ST": " STREET", " RD": " ROAD", " AVE": " AVENUE",
    " SQ": " SQUARE", " PL": " PLACE", " LN": " LANE",
    " GDNS": " GARDENS", " PK": " PARK", " CIR": " CIRCUS",
    " HL": " HILL", " RI": " RISE", " CR": " CRESCENT",
    "R/BOUT": "ROUNDABOUT", " R/BOUT": " ROUNDABOUT",
}

_JUNCTION_SUFFIXES = [
    " CIRCUS", " CROSS", " INTERCHANGE", " JUNCTION", " CORNER",
    " SLIP", " SLIP ROAD", " APPROACH", " TUNNEL", " BRIDGE SLIP",
]

_ROUNDABOUT_TOKENS = ("ROUNDABOUT", "R/BOUT")

# Ring-edge predicate: OSM tags most gyratories ``junction=roundabout``, but
# some (the BFI IMAX) are ``junction=circular``, and big interchanges hang
# their circulation off motorway links.
_RING_JUNCTIONS = {"roundabout", "circular"}
_RING_HIGHWAYS = {"motorway_link"}


def is_roundabout_line(raw: str) -> bool:
    """True when a Blue Book line marks a roundabout rather than a street."""
    upper = (raw or "").upper()
    return any(tok in upper for tok in _ROUNDABOUT_TOKENS)


@dataclass(frozen=True)
class Constraint:
    kind: str      # "STREET" | "NODE"
    key: object    # canonical street name (str) | frozenset[int]
    raw: str       # original Blue Book text, for QA
    source: str    # exact | junction | abbrev | fuzzy | word_removal | ring
    hard: bool     # False for low-confidence resolutions
    # STREET only: the graph nodes of the instance(s) of the name this run
    # drives (``locality.localise_constraints``). ``None`` matches the name
    # anywhere — London reuses street names, so an unlocalised constraint can
    # be satisfied by a namesake on the other side of the city.
    nodes: frozenset | None = None

    def matches_edge(self, names, u, v) -> bool:
        """True when traversing (u, v), whose name set is *names*, meets this
        STREET constraint — right name, and the localised instance."""
        if self.key not in names:
            return False
        return self.nodes is None or (u in self.nodes and v in self.nodes)


@dataclass
class CompiledRun:
    """Result of compiling one run's line sequence."""
    constraints: list = field(default_factory=list)
    gaps: list = field(default_factory=list)  # raw lines with no constraint

    def kind_histogram(self) -> dict:
        out: dict[str, int] = {}
        for c in self.constraints:
            out[c.kind] = out.get(c.kind, 0) + 1
        return out

    def source_histogram(self) -> dict:
        out: dict[str, int] = {}
        for c in self.constraints:
            out[c.source] = out.get(c.source, 0) + 1
        return out


# ---------------------------------------------------------------------------
# Tiered street resolution
# ---------------------------------------------------------------------------

# Fuzzy matches are cached per name: difflib over ~36k index keys is too slow
# to repeat for every street of every run. Keyed by name only, which assumes
# one street index per process — true for the pipeline and the tests.
_fuzzy_cache: dict[str, str | None] = {}


def fuzzy_street_match(norm_name: str, street_keys) -> str | None:
    """Return the index key within edit distance 2 of *norm_name*, but only
    when the name is >= 8 chars and exactly one candidate qualifies."""
    if len(norm_name) < 8:
        return None
    if norm_name in _fuzzy_cache:
        return _fuzzy_cache[norm_name]

    import difflib

    close = difflib.get_close_matches(norm_name, street_keys, n=3, cutoff=0.85)

    def edit_distance(a, b):
        if abs(len(a) - len(b)) > 2:
            return 3
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            curr = [i]
            for j, cb in enumerate(b, 1):
                curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb)))
            if min(curr) > 2:
                return 3
            prev = curr
        return prev[-1]

    candidates = [c for c in close if edit_distance(norm_name, c) <= 2]
    result = candidates[0] if len(candidates) == 1 else None
    _fuzzy_cache[norm_name] = result
    return result


def resolve_street_tiered(raw_name: str, street_keys,
                          spelling_fixes: dict | None = None) -> tuple[str | None, str]:
    """Resolve *raw_name* against a **pure** street index, tier by tier.

    Returns ``(canonical_key, source)`` or ``(None, "unresolved")``. Unlike
    the legacy ``get_best_street_match`` this never falls back to returning
    its own input: an unresolvable name must surface as a gap, not become an
    unsatisfiable constraint.
    """
    cleaned = (raw_name or "").upper().strip()
    if spelling_fixes:
        cleaned = str(spelling_fixes.get(cleaned, cleaned)).upper()
    if not cleaned:
        return None, "unresolved"

    full = normalise(cleaned)
    if full in street_keys:
        return full, "exact"

    # Abbreviation expansion (in-string, then suffix-only)
    expanded = full
    for abbr, full_form in _ABBREVIATIONS.items():
        if abbr in expanded:
            expanded = expanded.replace(abbr, full_form)
    if expanded != full and expanded in street_keys:
        return expanded, "abbrev"
    for abbr, full_form in _ABBREVIATIONS.items():
        if full.endswith(abbr):
            candidate = full[: -len(abbr)] + full_form
            if candidate in street_keys:
                return candidate, "abbrev"

    fuzzy = fuzzy_street_match(full, street_keys)
    if fuzzy is not None:
        return fuzzy, "fuzzy"

    # Junction-suffix strip ("HOLBORN CIRCUS" -> "HOLBORN") then progressive
    # word removal. Both are guesses — a match here is soft by definition.
    stripped = cleaned
    for suffix in _JUNCTION_SUFFIXES:
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)].strip()
            break
    words = stripped.split()
    while words:
        candidate = normalise(" ".join(words))
        if candidate in street_keys:
            return candidate, "word_removal"
        words.pop()

    return None, "unresolved"


# ---------------------------------------------------------------------------
# Roundabout ring collection
# ---------------------------------------------------------------------------

def _tag_values(value) -> set:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(v).lower().strip() for v in value if str(v).strip()}
    return {str(value).lower().strip()}


def _is_ring_edge(data: dict) -> bool:
    if _tag_values(data.get("junction")) & _RING_JUNCTIONS:
        return True
    return bool(_tag_values(data.get("highway")) & _RING_HIGHWAYS)


def _touches_ring(G, node) -> bool:
    # Mini-roundabouts are tagged on the *node*, not on any edge — a large
    # share of the Blue Book's bare "COM ROUNDABOUT" lines are these.
    if "mini_roundabout" in _tag_values(G.nodes[node].get("highway")):
        return True
    for _, _, data in G.out_edges(node, data=True):
        if _is_ring_edge(data):
            return True
    for _, _, data in G.in_edges(node, data=True):
        if _is_ring_edge(data):
            return True
    return False


def collect_ring(G, seed) -> set:
    """BFS from *seed* over ring edges, returning the whole gyratory.

    Same traversal the legacy waypoint builder used for roundabout
    aggregation, widened to ``junction=circular``.
    """
    ring = {seed}
    queue = [seed]
    while queue:
        curr = queue.pop(0)
        for nxt in G.successors(curr):
            if nxt in ring:
                continue
            for data in (G.get_edge_data(curr, nxt) or {}).values():
                if _is_ring_edge(data):
                    ring.add(nxt)
                    queue.append(nxt)
                    break
        for prev in G.predecessors(curr):
            if prev in ring:
                continue
            for data in (G.get_edge_data(prev, curr) or {}).values():
                if _is_ring_edge(data):
                    ring.add(prev)
                    queue.append(prev)
                    break
    return ring


def _ring_seeds(G, nodes) -> list:
    return [n for n in nodes if n in G.nodes and _touches_ring(G, n)]


def _candidate_rings(G, seed_nodes) -> list:
    """Distinct rings reachable from *seed_nodes* (each node's gyratory)."""
    rings: list = []
    seen: set = set()
    for seed in seed_nodes:
        if seed in seen:
            continue
        ring = collect_ring(G, seed)
        seen |= ring
        rings.append(ring)
    return rings


def _ring_between(G, prev_nodes: set, next_nodes: set,
                  candidate_nodes: set | None = None) -> set | None:
    """Find the gyratory a run passes between two neighbouring constraints.

    Candidate rings come from ``candidate_nodes`` when given (a named street's
    own nodes — "HARROW ROAD ROUNDABOUT" means a ring *on Harrow Road*),
    otherwise from the neighbours. A long street can touch several gyratories,
    so candidates are scored by contact with the neighbouring constraints:
    touching both neighbours beats touching one, which beats touching none —
    picking an arbitrary first ring on the street is how four runs ended up
    demoting HARROW ROAD ROUNDABOUT.
    """
    prev_set = set(_ring_seeds(G, prev_nodes or set()))
    next_set = set(_ring_seeds(G, next_nodes or set()))

    if candidate_nodes is not None:
        rings = _candidate_rings(G, _ring_seeds(G, candidate_nodes))
    else:
        rings = _candidate_rings(G, sorted(prev_set) + sorted(next_set))
    if not rings:
        return None

    def score(ring: set) -> tuple:
        return (bool(ring & prev_set) + bool(ring & next_set),
                bool(ring & next_set))

    best = max(rings, key=score)
    return best


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------

def _strip_roundabout_tokens(name: str) -> str:
    """"HARROW ROAD ROUNDABOUT" -> "HARROW ROAD"; handles the source file's
    run-together forms ("ARMSROUNDABOUT") and "R/BOUT"."""
    upper = (name or "").upper()
    for tok in _ROUNDABOUT_TOKENS:
        upper = upper.replace(tok, " ")
    return re.sub(r"\s+", " ", upper).strip()


def _resolve_roundabout(raw: str, street_keys, junction_index: dict) -> Constraint | None:
    """Resolve a roundabout line by *name* alone, if its name allows it.

    Ring inference is deliberately not done here: even a named roundabout
    ("HARROW ROAD ROUNDABOUT") only says which street the ring is on, and a
    long street can carry several — the choice needs the neighbouring
    constraints, which the compiler's second pass has.
    """
    full = normalise(raw)
    # Some gyratories are literally named ways ("Bricklayers Arms Roundabout").
    if full in street_keys:
        return Constraint("STREET", full, raw, "exact", True)
    if full in junction_index:
        return Constraint("NODE", frozenset(junction_index[full]), raw,
                          "junction", True)
    stem = _strip_roundabout_tokens(raw)
    if stem:
        stem_norm = normalise(stem)
        if stem_norm in junction_index:
            return Constraint("NODE", frozenset(junction_index[stem_norm]),
                              raw, "junction", True)
    return None


def _neighbour_nodes(constraint: Constraint | None, street_to_nodes: dict) -> set:
    if constraint is None:
        return set()
    if constraint.kind == "NODE":
        return set(constraint.key)
    return set(street_to_nodes.get(constraint.key, ()))


def compile_constraints(raw_lines, street_to_nodes, junction_index=None,
                        G=None, spelling_fixes: dict | None = None) -> CompiledRun:
    """Compile a run's ordered Blue Book lines into routing constraints.

    ``street_to_nodes`` must be the **pure** street index (edge names only).
    Junction names live in ``junction_index``; merging them into the street
    index is exactly the bug this compiler exists to avoid — a merged key has
    graph *nodes* but zero *edges* of that name, so a STREET constraint on it
    can never be satisfied.

    Unresolvable lines are recorded in ``gaps`` and produce no constraint.
    Consecutive duplicate constraints collapse to one.
    """
    junction_index = junction_index or {}
    result = CompiledRun()

    # First pass: resolve every line that can stand alone. Roundabout markers
    # keep their slot (with the named street's nodes as ring candidates, when
    # the line names one) so the second pass can see their neighbours.
    _PENDING_RING = object()
    seq: list = []
    for raw in raw_lines:
        if not raw or not str(raw).strip():
            continue
        raw = str(raw)
        if is_roundabout_line(raw):
            resolved = _resolve_roundabout(raw, street_to_nodes, junction_index)
            if resolved is not None:
                seq.append(resolved)
            else:
                stem = _strip_roundabout_tokens(raw)
                candidates = None
                if stem and hasattr(street_to_nodes, "get"):
                    candidates = street_to_nodes.get(normalise(stem)) or None
                seq.append((_PENDING_RING, raw, candidates))
            continue

        # Tier order matters: the junction index must be consulted before the
        # *soft* street tiers, or "HOLLAND PARK CIRCUS" word-removes to the
        # street "HOLLAND PARK" and the curated junction definition is never
        # seen. An exact street name still wins — a line that names a real
        # street is that street.
        fixed = str(spelling_fixes.get(raw.upper(), raw)) if spelling_fixes else raw
        norm = normalise(fixed)
        if norm in street_to_nodes:
            seq.append(Constraint("STREET", norm, raw, "exact", True))
            continue
        if norm in junction_index:
            seq.append(Constraint("NODE", frozenset(junction_index[norm]), raw,
                                  "junction", True))
            continue

        match, source = resolve_street_tiered(raw, street_to_nodes, spelling_fixes)
        if match is not None:
            seq.append(Constraint("STREET", match, raw, source,
                                  source in HARD_SOURCES))
            continue

        seq.append((None, raw, None))  # gap

    # Second pass: locate roundabouts from their neighbours (and, for named
    # ones, from the named street's own nodes).
    for i, item in enumerate(seq):
        if isinstance(item, Constraint):
            result.constraints.append(item)
            continue
        marker, raw, candidates = item
        if marker is _PENDING_RING and G is not None:
            prev_c = next((c for c in reversed(seq[:i]) if isinstance(c, Constraint)),
                          None)
            next_c = next((c for c in seq[i + 1:] if isinstance(c, Constraint)),
                          None)
            ring = _ring_between(
                G,
                _neighbour_nodes(prev_c, street_to_nodes),
                _neighbour_nodes(next_c, street_to_nodes),
                candidate_nodes=candidates,
            )
            if ring:
                result.constraints.append(
                    Constraint("NODE", frozenset(ring), raw, "ring", False))
                continue
        result.gaps.append(raw)

    # Consecutive duplicates are one requirement, not two.
    deduped: list = []
    for c in result.constraints:
        if deduped and deduped[-1].kind == c.kind and deduped[-1].key == c.key:
            continue
        deduped.append(c)
    result.constraints = deduped
    return result
