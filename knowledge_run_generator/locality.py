"""
Constraint localisation — pin every constraint to the instance the run drives.

A ``STREET`` constraint is a *name*, and London reuses names: there are dozens
of Station Roads, High Streets and Chestnut Groves, several Cavendish Roads,
and a Station Yard in Twickenham. Matched by name alone, a constraint is
satisfied by *any* same-named edge in Greater London. Whenever the instance the
Blue Book means is unreachable — it isn't in OSM under that name, the line was
mis-resolved, the endpoint is wrong, or (for the reverse run) its one-way
cannot be driven backwards — the ordered search does not fail; it quietly
drives to the nearest namesake that *is* satisfiable, kilometres away, and the
ladder never demotes it because the constraint was met. That is how run 150
went to Twickenham and back (35.9 km for a 2.7 km run), run 47 through the
Blackwall Tunnel to Lea Bridge Road, and run 215 to Spitalfields.

It also blew up the search corridor, which was the bbox of every instance of
every named street — for a run naming a common street, most of London.

:func:`localise_constraints` fixes both at the source:

1. Each name's edges are split into *instances*: connected components of the
   edges carrying the name, with components closer than
   :data:`INSTANCE_MERGE_M` merged (a street interrupted by a square is still
   one street).
2. A Viterbi pass over the constraint sequence picks, per constraint, the
   instances on the cheapest crow-flies chain origin -> C0 -> ... -> dest.
   Instances within :data:`KEEP_SLACK_M` of the best chain are kept too, so a
   near-tie between two local pieces is left to the router.
3. A constraint whose best instance still costs the chain more than
   :data:`REMOTE_EXCESS_M` over skipping it is **remote**: no instance of that
   name lies anywhere near the run. It is dropped from the sequence and
   reported, exactly like a compile-time gap — never routed to.

The chosen instances are carried on ``Constraint.nodes``; the router, the
validator's ordered metric and the waypoint walk all honour it.
"""

from __future__ import annotations

import dataclasses
import math

from .aliases import _iter_names, normalise

# Components of one name closer than this are the same street.
INSTANCE_MERGE_M = 250.0

# Instances whose best chain is within this of the overall best are kept.
KEEP_SLACK_M = 400.0

# A constraint that adds more than this to the crow-flies chain, even through
# its best instance, names a street that isn't near the run at all. Measured on
# the 320-run corpus: every genuinely local constraint costs < 1.3 km, every
# wrong-instance resolution > 4 km.
REMOTE_EXCESS_M = 2500.0

_M_PER_DEG_LAT = 111_195.0
_LON_SCALE = math.cos(math.radians(51.5))


def _xy(G, n):
    d = G.nodes[n]
    return (d["x"] * _LON_SCALE * _M_PER_DEG_LAT, d["y"] * _M_PER_DEG_LAT)


# name -> list[(u, v)] over every name tag (the same semantics as
# router.edge_name_set), memoised per graph.
_name_edge_index: dict = {}


def name_edge_index(G) -> dict:
    key = id(G)
    cached = _name_edge_index.get(key)
    if cached is not None and cached[0] is G:
        return cached[1]
    index: dict = {}
    norm_cache: dict = {}
    for u, v, data in G.edges(data=True):
        for raw in _iter_names(data):
            norm = norm_cache.get(raw)
            if norm is None:
                norm = normalise(raw)
                norm_cache[raw] = norm
            if norm:
                index.setdefault(norm, []).append((u, v))
    _name_edge_index[key] = (G, index)
    _instance_cache.pop(key, None)
    return index


# (id(G)) -> name -> list[set]; reset whenever the name index is rebuilt.
_instance_cache: dict = {}


def street_instances(G, name: str) -> list:
    """The spatially separate instances of *name*, as a list of node sets."""
    index = name_edge_index(G)  # first: it invalidates a stale instance cache
    cache = _instance_cache.setdefault(id(G), {})
    if name in cache:
        return [set(s) for s in cache[name]]
    edges = index.get(name) or []
    parent: dict = {}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for u, v in edges:
        parent.setdefault(u, u)
        parent.setdefault(v, v)
        ru, rv = find(u), find(v)
        if ru != rv:
            parent[ru] = rv
    groups: dict = {}
    for n in parent:
        groups.setdefault(find(n), set()).add(n)
    comps = list(groups.values())

    # Merge components that nearly touch (single linkage).
    link = list(range(len(comps)))

    def lfind(a):
        while link[a] != a:
            link[a] = link[link[a]]
            a = link[a]
        return a

    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            if lfind(i) != lfind(j) and \
                    _set_distance(G, comps[i], comps[j]) <= INSTANCE_MERGE_M:
                link[lfind(i)] = lfind(j)
    out: dict = {}
    for i, comp in enumerate(comps):
        out.setdefault(lfind(i), set()).update(comp)
    result = list(out.values())
    cache[name] = result
    return [set(s) for s in result]


def _set_distance(G, a, b) -> float:
    pa = [_xy(G, n) for n in a]
    pb = [_xy(G, n) for n in b]
    best = float("inf")
    for ax, ay in pa:
        for bx, by in pb:
            d = (ax - bx) ** 2 + (ay - by) ** 2
            if d < best:
                best = d
    return math.sqrt(best)


@dataclasses.dataclass
class Localised:
    constraints: list
    # One entry per dropped constraint: raw text, source, and how far off the
    # run its nearest instance was.
    remote: list


def localise_constraints(G, constraints, origin_node, dest_node,
                         remote_excess_m: float = REMOTE_EXCESS_M,
                         keep_slack_m: float = KEEP_SLACK_M) -> Localised:
    """Pin each constraint to the instances near the run; drop remote ones.

    Constraints are returned in order with ``nodes`` set for STREET
    constraints (the kept instances' nodes). NODE constraints are already
    local by construction and pass through unchanged, but they take part in
    the chain (and can be found remote, e.g. a junction on the wrong side of
    London).
    """
    C = list(constraints)
    remote: list = []

    cands: list = []
    for c in C:
        if c.kind == "STREET":
            inst = street_instances(G, c.key)
            if not inst:
                # Name tag present but no edge carries it — leave the
                # constraint unrestricted; the router will prove it
                # unsatisfiable and the ladder will record it.
                inst = [None]
        else:
            inst = [set(n for n in c.key if n in G.nodes) or None]
        cands.append(inst)

    alive = list(range(len(C)))
    while True:
        excess, choice = _chain(G, origin_node, dest_node,
                                [cands[i] for i in alive], keep_slack_m)
        if not alive:
            break
        worst = max(range(len(alive)), key=lambda k: excess[k])
        if excess[worst] <= remote_excess_m:
            break
        i = alive.pop(worst)
        remote.append({
            "raw": C[i].raw,
            "source": C[i].source,
            "excess_m": round(excess[worst], 0),
        })

    out = []
    for pos, i in enumerate(alive):
        c = C[i]
        if c.kind == "STREET":
            kept = choice[pos]
            if kept is not None:
                c = dataclasses.replace(c, nodes=frozenset(kept))
        out.append(c)
    return Localised(constraints=out, remote=remote)


def _chain(G, origin, dest, cands, keep_slack_m):
    """Forward-backward Viterbi over instance candidates.

    Returns ``(excess, kept)``: per position, how much the cheapest chain
    through it exceeds the cheapest chain that skips it, and the union of the
    instances within ``keep_slack_m`` of the best chain (None if the
    constraint has no located instance).
    """
    K = len(cands)
    if K == 0:
        return [], []
    o = [_xy(G, origin)]
    d = [_xy(G, dest)]
    pts = []
    for inst in cands:
        pts.append([None if s is None else [_xy(G, n) for n in s] for s in inst])

    def dist(a, b):
        # a, b: point lists (None = unlocated -> free)
        if a is None or b is None:
            return 0.0
        best = float("inf")
        for ax, ay in a:
            for bx, by in b:
                v = (ax - bx) ** 2 + (ay - by) ** 2
                if v < best:
                    best = v
        return math.sqrt(best)

    # Layers: 0 = origin, 1..K = constraints, K+1 = dest.
    layers = [[o]] + pts + [[d]]
    # Pairwise transition matrices between consecutive layers and, for the
    # skip computation, between layers two apart.
    trans = [[[dist(a, b) for b in layers[i + 1]] for a in layers[i]]
             for i in range(K + 1)]
    fwd = [[0.0]]
    for i in range(1, K + 2):
        row = []
        for j in range(len(layers[i])):
            row.append(min(fwd[i - 1][p] + trans[i - 1][p][j]
                           for p in range(len(layers[i - 1]))))
        fwd.append(row)
    bwd = [None] * (K + 2)
    bwd[K + 1] = [0.0]
    for i in range(K, -1, -1):
        row = []
        for j in range(len(layers[i])):
            row.append(min(trans[i][j][q] + bwd[i + 1][q]
                           for q in range(len(layers[i + 1]))))
        bwd[i] = row
    best_total = fwd[K + 1][0]

    excess = []
    kept = []
    for k in range(K):
        li = k + 1
        through = [fwd[li][j] + bwd[li][j] for j in range(len(layers[li]))]
        best_through = min(through)
        skip = min(
            fwd[li - 1][p] + dist(layers[li - 1][p], layers[li + 1][q]) + bwd[li + 1][q]
            for p in range(len(layers[li - 1]))
            for q in range(len(layers[li + 1]))
        )
        excess.append(best_through - skip)
        chosen = [cands[k][j] for j, t in enumerate(through)
                  if t <= best_total + keep_slack_m and cands[k][j] is not None]
        if not chosen:
            # Fall back to the single best instance.
            j = min(range(len(through)), key=through.__getitem__)
            chosen = [cands[k][j]] if cands[k][j] is not None else []
        kept.append(set().union(*chosen) if chosen else None)
    return excess, kept
