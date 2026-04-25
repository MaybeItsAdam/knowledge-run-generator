"""
Per-run diagnostics.

``krg diagnose RUN_ID`` walks a generated route edge-by-edge against the
live graph and reports street name, junction class, and length for each
step. It also detects *ring traversals* — consecutive runs of
``junction=roundabout`` edges — and prints the arc length and the number
of unique nodes visited on the ring, so a "lap" (>50% of the ring's
perimeter) is obvious in the output.

The intent is to make routing artefacts like the IMAX-roundabout lap in
Run 4 reproducible, with a piece of evidence that points at exactly which
edges the router chose, before we change cost-model behaviour.

Reads ``constants/runPoints.json`` (the pipeline output) for the node
sequence. Re-routes from scratch if the JSON is missing the ``nodes``
field — older snapshots only have geometry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class EdgeFact:
    u: int
    v: int
    length_m: float
    name: str
    highway: str
    junction: str
    oneway: bool

    @property
    def is_roundabout(self) -> bool:
        return self.junction == "roundabout"


@dataclass
class RingTraversal:
    start_index: int            # edge index where ring run begins
    end_index: int              # last edge index of the ring run
    edges: int                  # how many roundabout edges in a row
    arc_length_m: float
    name: str
    unique_nodes: int           # unique node IDs visited on this ring run
    revisits: int               # nodes visited more than once on this ring run


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return str(value)


def _best_edge(G, u, v) -> dict | None:
    bundle = G.get_edge_data(u, v)
    if not bundle:
        return None
    return min(bundle.values(), key=lambda d: d.get("length", float("inf")))


def walk_route(G, nodes: list[int]) -> list[EdgeFact]:
    """Return one EdgeFact per consecutive (u, v) pair in *nodes*."""
    facts: list[EdgeFact] = []
    for u, v in zip(nodes, nodes[1:]):
        data = _best_edge(G, u, v) or {}
        facts.append(EdgeFact(
            u=int(u),
            v=int(v),
            length_m=float(data.get("length", 0.0) or 0.0),
            name=_coerce_str(data.get("name")),
            highway=_coerce_str(data.get("highway")),
            junction=_coerce_str(data.get("junction")),
            oneway=bool(data.get("oneway", False)),
        ))
    return facts


def detect_ring_traversals(facts: list[EdgeFact]) -> list[RingTraversal]:
    """Group consecutive roundabout edges and report each run."""
    out: list[RingTraversal] = []
    i = 0
    while i < len(facts):
        if not facts[i].is_roundabout:
            i += 1
            continue
        j = i
        seen: dict[int, int] = {}
        while j < len(facts) and facts[j].is_roundabout:
            seen[facts[j].u] = seen.get(facts[j].u, 0) + 1
            j += 1
        if j > i:
            seen[facts[j - 1].v] = seen.get(facts[j - 1].v, 0) + 1
        arc = sum(f.length_m for f in facts[i:j])
        # Pick the most common name on the run.
        names: dict[str, int] = {}
        for f in facts[i:j]:
            if f.name:
                names[f.name] = names.get(f.name, 0) + 1
        ring_name = max(names, key=names.get) if names else ""
        revisits = sum(c - 1 for c in seen.values() if c > 1)
        out.append(RingTraversal(
            start_index=i,
            end_index=j - 1,
            edges=j - i,
            arc_length_m=arc,
            name=ring_name,
            unique_nodes=len(seen),
            revisits=revisits,
        ))
        i = j
    return out


def estimate_full_ring_perimeter(G, ring_node: int) -> float | None:
    """Walk the connected ``junction=roundabout`` component containing
    *ring_node* and sum its edge lengths.

    Returns ``None`` if *ring_node* isn't on a roundabout. The perimeter
    estimate lets us label a traversal as a "lap" when the arc covers
    >50% of the ring.
    """
    if ring_node not in G:
        return None

    def _is_ring_edge(data: dict) -> bool:
        return _coerce_str(data.get("junction")) == "roundabout"

    visited_nodes: set[int] = {ring_node}
    visited_edges: set[tuple] = set()
    stack = [ring_node]
    while stack:
        n = stack.pop()
        for nbr, edges in G[n].items():
            for k, data in edges.items():
                if not _is_ring_edge(data):
                    continue
                eid = (n, nbr, k)
                if eid in visited_edges:
                    continue
                visited_edges.add(eid)
                if nbr not in visited_nodes:
                    visited_nodes.add(nbr)
                    stack.append(nbr)
        # Backwards too — roundabouts are typically one-way, but the
        # representation may be split.
        if hasattr(G, "predecessors"):
            for prev in G.predecessors(n):
                edges = G.get_edge_data(prev, n) or {}
                for k, data in edges.items():
                    if not _is_ring_edge(data):
                        continue
                    eid = (prev, n, k)
                    if eid in visited_edges:
                        continue
                    visited_edges.add(eid)
                    if prev not in visited_nodes:
                        visited_nodes.add(prev)
                        stack.append(prev)

    if not visited_edges:
        return None
    total = 0.0
    for u, v, k in visited_edges:
        bundle = G.get_edge_data(u, v) or {}
        data = bundle.get(k, {})
        total += float(data.get("length", 0.0) or 0.0)
    # Each ring is typically a single one-way circle; if it's been
    # represented as a doubled-up pair we'd be double-counting. Halve
    # the total in that case (heuristic: count incoming + outgoing
    # edges on a sample node).
    sample = next(iter(visited_nodes))
    in_edges = sum(
        1 for _, _, d in G.in_edges(sample, data=True)
        if _coerce_str(d.get("junction")) == "roundabout"
    )
    out_edges = sum(
        1 for _, _, d in G.out_edges(sample, data=True)
        if _coerce_str(d.get("junction")) == "roundabout"
    )
    if in_edges == 1 and out_edges == 1:
        return total
    # If both sides are populated symmetrically, treat as bidirectional.
    return total / 2.0


# ---------------------------------------------------------------------------
# Top-level entry — load + format
# ---------------------------------------------------------------------------

def load_run_nodes(runs_json: Path, run_id: int, direction: str = "forward") -> list[int] | None:
    data = json.loads(Path(runs_json).read_text())
    target = next((r for r in data if r.get("id") == run_id), None)
    if target is None:
        return None
    key = "route" if direction == "forward" else "routeReverse"
    nodes = (target.get(key) or {}).get("nodes")
    if not nodes:
        return None
    return [int(n) for n in nodes]


def format_route(
    facts: list[EdgeFact],
    rings: list[RingTraversal],
    G,
    *,
    only_rings: bool = False,
    context_edges: int = 3,
) -> str:
    """Render a per-edge dump.

    With ``only_rings=True``, prints just each ring traversal with
    ``context_edges`` of surrounding context. That's the slice you
    actually want when triaging a roundabout artefact like Run 4.
    """
    lines: list[str] = []

    def _row(idx: int, f: EdgeFact, marker: str = "") -> str:
        flags = []
        if f.is_roundabout:
            flags.append("R")
        if f.oneway:
            flags.append("1")
        flag_s = "".join(flags).rjust(2)
        return (
            f"  [{idx:4d}] {f.length_m:5.1f}m {flag_s} "
            f"{f.name[:30]:30s} hwy={f.highway[:18]:18s} "
            f"junction={f.junction[:12]:12s}"
            + (f"  {marker}" if marker else "")
        )

    if only_rings:
        for r_idx, r in enumerate(rings):
            lo = max(0, r.start_index - context_edges)
            hi = min(len(facts), r.end_index + 1 + context_edges)
            perim = estimate_full_ring_perimeter(G, facts[r.start_index].u)
            pct = (r.arc_length_m / perim * 100.0) if perim else None
            lap_label = " <<< LAP" if pct is not None and pct > 50 else ""
            lines.append("")
            lines.append(
                f"--- Ring traversal #{r_idx + 1}: '{r.name}' "
                f"edges={r.edges} arc={r.arc_length_m:.0f}m "
                f"unique_nodes={r.unique_nodes} revisits={r.revisits}"
                + (f" perimeter≈{perim:.0f}m ({pct:.0f}%)" if perim else "")
                + lap_label
            )
            for i in range(lo, hi):
                marker = ""
                if i == r.start_index:
                    marker = "→ ring entry"
                elif i == r.end_index:
                    marker = "← ring exit"
                lines.append(_row(i, facts[i], marker))
        return "\n".join(lines) if lines else "  (no roundabout edges in this route)"

    for idx, f in enumerate(facts):
        lines.append(_row(idx, f))
    return "\n".join(lines)


def diagnose(runs_json: Path, run_id: int, G, direction: str = "forward",
             only_rings: bool = True, context_edges: int = 3) -> str:
    nodes = load_run_nodes(runs_json, run_id, direction=direction)
    if not nodes:
        return (
            f"Run {run_id} has no '{direction}' route nodes in {runs_json}.\n"
            "If this run was generated before the 'nodes' field was added,\n"
            "regenerate it: `krg bluebookdemo {} --fresh`"
        ).format(run_id)
    facts = walk_route(G, nodes)
    rings = detect_ring_traversals(facts)

    header = [
        f"Run {run_id} ({direction})  —  {len(nodes)} nodes / {len(facts)} edges",
        f"Total distance: {sum(f.length_m for f in facts):.0f} m",
        f"Roundabout edges: {sum(1 for f in facts if f.is_roundabout)} "
        f"across {len(rings)} ring traversal(s)",
    ]
    body = format_route(facts, rings, G,
                        only_rings=only_rings, context_edges=context_edges)
    return "\n".join(header) + "\n" + body
