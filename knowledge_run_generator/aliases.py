"""
Street alias index built from the OSM graph.

For every edge in the graph we collect every known name variant:
``name``, ``alt_name``, ``old_name``, ``official_name``, ``ref`` (A-road
designator). A single canonical form (the upper-cased ``name``) is picked and
every variant is mapped to it. This lets the pipeline resolve "A4",
"CROMWELL RD" and "CROMWELL ROAD" to the same street without hand-maintained
dictionaries.

Abbreviation expansion is also handled here — rather than the previous
suffix-only dict, we expand every token in the name, so "ST JOHNS WD RD"
canonicalises to "SAINT JOHNS WOOD ROAD".
"""

from __future__ import annotations

import pickle
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


_TOKEN_ABBREVIATIONS = {
    "ST": "STREET",
    "RD": "ROAD",
    "AVE": "AVENUE",
    "AV": "AVENUE",
    "SQ": "SQUARE",
    "PL": "PLACE",
    "LN": "LANE",
    "GDNS": "GARDENS",
    "GDN": "GARDEN",
    "PK": "PARK",
    "CIR": "CIRCUS",
    "HL": "HILL",
    "RI": "RISE",
    "CR": "CRESCENT",
    "CRES": "CRESCENT",
    "CT": "COURT",
    "DR": "DRIVE",
    "EMB": "EMBANKMENT",
    "TER": "TERRACE",
    "GT": "GREAT",
    "LT": "LITTLE",
    "UPP": "UPPER",
    "WD": "WOOD",
    "BLDGS": "BUILDINGS",
    "MKT": "MARKET",
    "R/BOUT": "ROUNDABOUT",
}

# Tokens that expand but only when they appear as the whole first word of a
# multi-word name (avoid mangling "ST JAMES" or "GT ORMOND STREET")
_LEADING_EXPANSIONS = {
    "ST": "SAINT",
    "GT": "GREAT",
    "LT": "LITTLE",
    "UPP": "UPPER",
}


_PUNCT_RE = re.compile(r"[.,'’]")
_WS_RE = re.compile(r"\s+")


def normalise(name: str) -> str:
    """Canonical form: upper-case, punctuation stripped, abbreviations expanded."""
    if not name:
        return ""
    s = _PUNCT_RE.sub("", str(name).upper().strip())
    s = _WS_RE.sub(" ", s)
    tokens = s.split(" ")
    if not tokens:
        return ""

    # Leading-only expansions
    if tokens[0] in _LEADING_EXPANSIONS:
        tokens[0] = _LEADING_EXPANSIONS[tokens[0]]

    expanded = []
    for i, t in enumerate(tokens):
        if i > 0 and t in _TOKEN_ABBREVIATIONS:
            expanded.append(_TOKEN_ABBREVIATIONS[t])
        else:
            expanded.append(t)
    return " ".join(expanded)


@dataclass
class AliasIndex:
    """Maps any known name variant of a street to its canonical normalised form."""
    canonical_to_nodes: dict = field(default_factory=dict)
    alias_to_canonical: dict = field(default_factory=dict)

    def resolve(self, raw: str) -> str | None:
        """Return the canonical name for *raw*, or None if unknown."""
        if not raw:
            return None
        norm = normalise(raw)
        if norm in self.canonical_to_nodes:
            return norm
        if norm in self.alias_to_canonical:
            return self.alias_to_canonical[norm]
        # Trailing-word drop (handles "KINGS CROSS ROAD" vs "KINGS CROSS")
        parts = norm.split(" ")
        while len(parts) > 1:
            parts.pop()
            candidate = " ".join(parts)
            if candidate in self.canonical_to_nodes:
                return candidate
            if candidate in self.alias_to_canonical:
                return self.alias_to_canonical[candidate]
        return None

    def nodes_for(self, raw: str) -> set:
        canonical = self.resolve(raw)
        if canonical is None:
            return set()
        return self.canonical_to_nodes.get(canonical, set())


def _iter_names(edge_data: dict) -> Iterable[str]:
    """Yield every string-like name hint attached to an edge."""
    for key in ("name", "alt_name", "old_name", "official_name", "ref"):
        value = edge_data.get(key)
        if not value:
            continue
        if isinstance(value, (list, tuple, set)):
            for v in value:
                if v:
                    yield str(v)
        else:
            yield str(value)


def build_alias_index(G) -> AliasIndex:
    """
    Walk every edge once and build a canonical-name → node-set map plus an
    alias → canonical map. The canonical form for a street is the normalised
    ``name`` tag; every other variant (``ref``, ``alt_name``, ...) points at
    it so the router can still find the street when the Blue Book refers to
    it by an A-road number or an older name.
    """
    canonical_to_nodes: dict[str, set] = {}
    alias_to_canonical: dict[str, str] = {}

    for u, v, _k, data in G.edges(keys=True, data=True):
        variants = [normalise(n) for n in _iter_names(data) if n]
        variants = [n for n in variants if n]
        if not variants:
            continue

        # Prefer the edge's ``name`` as canonical; fall back to the first variant
        raw_name = data.get("name")
        if isinstance(raw_name, (list, tuple)) and raw_name:
            raw_name = raw_name[0]
        canonical = normalise(raw_name) if raw_name else variants[0]
        if not canonical:
            canonical = variants[0]

        bucket = canonical_to_nodes.setdefault(canonical, set())
        bucket.add(u)
        bucket.add(v)

        for variant in variants:
            if variant and variant != canonical:
                # Only register an alias if it doesn't clash with a real street
                # of that name — otherwise we'd collapse two distinct streets.
                existing = alias_to_canonical.get(variant)
                if existing is None and variant not in canonical_to_nodes:
                    alias_to_canonical[variant] = canonical

    return AliasIndex(
        canonical_to_nodes=canonical_to_nodes,
        alias_to_canonical=alias_to_canonical,
    )


# Bump when the normaliser or the index layout changes, so caches built by an
# older version are rebuilt instead of silently answering with stale data.
INDEX_FORMAT_VERSION = 1


def graph_fingerprint(G) -> tuple:
    """Cheap identity for a graph, used to invalidate derived index caches.

    Size alone won't catch every edit, but it catches the ones that matter in
    practice — a different place, a different ``network_type``, or a refreshed
    OSM extract — and costs nothing to compute.
    """
    return (INDEX_FORMAT_VERSION, G.number_of_nodes(), G.number_of_edges())


def load_or_build_alias_index(G, cache_path: Path) -> AliasIndex:
    """Pickle-cached wrapper for :func:`build_alias_index`.

    The cache is keyed by :func:`graph_fingerprint`; a mismatch (or a cache
    written before the key existed) rebuilds rather than returning an index
    that disagrees with the graph it's being used against.
    """
    cache_path = Path(cache_path)
    fingerprint = graph_fingerprint(G)
    if cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                blob = pickle.load(f)
            if isinstance(blob, dict) and blob.get("fingerprint") == fingerprint:
                return blob["index"]
        except Exception:
            pass

    index = build_alias_index(G)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "wb") as f:
        pickle.dump({"fingerprint": fingerprint, "index": index}, f)
    return index
