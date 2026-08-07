# Roadmap — reaching the Knowledge standard

## The problem this roadmap exists to fix

**A run is correct when its route traverses the Blue Book streets in order,
legally.** The pipeline does not currently produce that, and — more importantly
— its own quality gates could not tell you so.

`validator.py` defines:

```python
result.passed = result.is_legal and result.is_direct and result.has_sane_detours
```

Street coverage was deliberately removed from that gate. So `passed` means "a
legal, reasonably direct road route exists between two points". It is **not** a
claim that the route is the Blue Book run, and a run can be `passed` while
traversing none of its prescribed streets.

Measured against the shipped build:

| Measure | Value |
|---|---|
| `passed` | 227 / 320 |
| `mean_ordered` — longest in-order subsequence | **0.818** |
| `mean_strict` — walk without ever skipping a line | **0.381** |
| Runs fully in order | **56 / 320** |
| Runs containing a prohibited turn triple | **60 / 320** (66 triples) |
| Runs with ≥1 unresolved intermediate street | 47 |
| Endpoints > 3 km from their stated postcode district | 12 (3 of them > 17 km) |

Worked examples that ship as `passed: true`:

- **Run 206** (Franciscan Road SW17 → Wimbledon Park Station SW19):
  `street_coverage 0.0`, route **700 m**, traversing none of the prescribed
  streets. A prototype ordered router routes it at 8,104 m — so the 700 m figure
  is a bad destination geocode, not bad routing.
- **Run 131** (Shortlands **W6**): 21 km route, start point 18.5 km from W6.
- **Run 177** (Saville Row **W1**): resolved into Bromley, 21.8 km.

### Why ordering, specifically

Decomposing the gap between "touched the streets" and "drove the run", over the
same routes and the same name extraction:

| | overall |
|---|---|
| unordered + substring (the shipped `street_coverage`) | 0.851 |
| unordered + exact | 0.825 |
| ordered + exact, LCS (`ordered_coverage`) | 0.817 |
| ordered + exact, strict walk (`strict_ordered`) | 0.343 |

Exactness costs 2.6 points. **Ordering costs 48.** The routes largely touch the
right streets; they do not drive them as a sequence.

### Three root causes

1. **Ordering is never enforced.** `router.py` breaks the search on the first
   pop of *any* target node regardless of sequence index; it always pushes an
   undiscounted branch, so skipping a street is always legal; and the index can
   leap 0→25 free. The 0.1× discount is the entire ordering mechanism — a
   preference, not a constraint. The A\* heuristic is inadmissible under that
   discount (overestimates up to 10×) and measures distance to the *centroid* of
   the target set, so the search degrades toward greedy and fights the discount.
2. **Post-processing manufactures illegal turns.** `_clean_backtrack` and
   `_collapse_revisits` splice the concatenated route *after* validation
   (`_collapse_revisits([1,2,3,4,1,5]) == [1,5]`, pinned by a test). Across a leg
   boundary this deletes required-street traversals and creates `(pred, X, succ)`
   triples nobody checked. Combined with `prev_node` resetting to `None` at each
   leg start, this is why 60 runs are illegal despite a correct restriction filter.
3. **Nothing caught regressions.** Fixed in Stage 1 — see below.

---

## Design: one ordered-constraint A\* per run

### Constraint model

A Blue Book line is not always a street. Compile the sequence into ordered
constraints:

```python
@dataclass(frozen=True)
class Constraint:
    kind: str      # "STREET" | "NODE"
    key: object    # canonical name | frozenset[int]
    raw: str       # original Blue Book text, for QA
    source: str    # exact | junction | abbrev | fuzzy | word_removal | ring
    hard: bool     # False for low-confidence resolutions
```

- `STREET` — satisfied by traversing an edge whose normalised name set contains `key`.
- `NODE` — satisfied by reaching any member node (junctions, gyratory rings).
- Unresolvable line → no constraint, recorded as an explicit gap.

### Search

State `(node, idx, prev_node)`, where `idx` is the number of constraints satisfied.

```python
if p is not None and (p, u, v) in prohibited_turns:
    continue

names   = edge_names(u, v)          # memoised frozenset
advance = idx < K and ((C[idx].kind == "STREET" and C[idx].key in names) or
                       (C[idx].kind == "NODE"   and v in C[idx].key))
stay_on = idx > 0 and C[idx-1].kind == "STREET" and C[idx-1].key in names

if advance: push(v, settle(v, idx+1), u, base)
if stay_on: push(v, settle(v, idx),   u, base)
else:       push(v, settle(v, idx),   u, base * CONNECTOR_MULT)   # 3.0
```

**Goal test — this one line is the hard ordering:** pop `(u, idx, p)` with
`u == dest_node and idx >= K`. Nothing else terminates the search.

Supporting changes: drop the 0.1× discount and the progress-bias cost terms;
heuristic becomes `max(euclid(n, dest), min over anchors of C[idx])`, memoised on
`(node, idx)` and admissible; corridor-limit to the bbox of {origin, dest,
anchors} + 0.008°; `MAX_SEARCH_STATES` becomes per-run.

**Why not the simpler patch** (make matching edges mandatory inside the existing
leg search): the goal test still terminates on first target-node pop regardless
of index, the free index jump survives, suppressing the undiscounted branch makes
the search incomplete wherever a one-way blocks the street, and `prev_node` still
resets per leg. Separately, **8.9% of consecutive resolved street pairs share no
graph node at all**, so any intersection-chaining design fails on ~389 pairs. The
ordered-constraint search never needs an intersection.

### Prototype evidence (measured, all 320 runs)

| | |
|---|---|
| Routed with the full ordered sequence hard | **210 / 320** |
| Prohibited turns in output | **0** (vs 60 today) |
| Search time | mean 0.86 s, median 0.11 s, max 3.1 s |
| Length vs today | median 1.04×, mean 1.33× |

Nearly all 110 failures are junction names (BRIDGEND CIRCUS ×8, VAUXHALL CROSS
×6, HYDE PARK CORNER ×5…), traced to a single modelling bug — see Stage 2.

### Degradation ladder — every gap explicit

The search returns `max_idx`, naming the constraint that blocked it.

| Tier | Action | `routing_mode` |
|---|---|---|
| T0 | All constraints hard | `ordered_strict` |
| T1 | Demote `C[max_idx]`, record, retry (max 4) | `ordered_relaxed` |
| T2 | Keep only `exact`/`junction` hard | `ordered_partial` |
| T3 | `nx.shortest_path` fallback | `shortest_path` + `status: failed` |

---

## Stages

### Stage 1 — Measurement only ✅ **done**

Established an honest baseline before changing any behaviour, and repaired the
harness that was supposed to protect it.

- `check_street_order` in `validator.py` — reports **both** ordered metrics.
  `ordered_coverage` (LCS) degrades smoothly, so one absent street costs one
  place; `strict_ordered` stops at the first unmatched street, answering "could a
  driver follow this without skipping a line". They agree at 1.0, which is the
  gate. Matching is exact on normalised names and considers every name tag
  (`name`/`alt_name`/`old_name`/`official_name`/`ref`), so a way tagged
  `["Marylebone Road", "A501"]` matches whichever the Blue Book names.
- Both metrics plus `order_first_gap`, `order_missing`, `route_hash` and
  `node_count` written to the QA record; `QA_SCHEMA_VERSION` → 3.
- **Repaired the regression harness.** `fingerprint_run` read `route_nodes` /
  `total_distance_m` — keys the QA writer never emitted — so all 320 fingerprints
  carried `route_hash: ""` and the geometry check had never once fired. It now
  reads keys that exist, tracks both fidelity metrics per-run and as corpus
  means, tracks `total`, and reports runs that *vanish* (the old loop iterated
  the current report only, so it was structurally blind to a run disappearing).
- **Un-skipped the CI tests.** `tests/fixtures/run1_graph.graphml` was hidden by
  `*.graphml`, so the end-to-end test could not load its fixture on a clean
  checkout; regression paths were CWD-relative, making a silent skip look like a
  pass. A missing baseline now fails; only a missing *report* skips.
- **Closed a real-data leak**: an explicitly-set `KRG_KNOWLEDGE_POIS` is now
  authoritative even when empty. Emptying it used to fall through to the real
  5,530-entry list, so a test passed for the wrong reason.
- 26 tests in a new `tests/test_validator.py`; the validator previously had none
  — no test existed for `check_street_coverage`, `_extract_route_streets`,
  `check_directness`, `check_turn_legality` or `check_waypoint_detours`. Suite
  total 82 → 110.
- Regression diff now gates at **promotion** (`scripts/promote_to_app.py`), the
  only place it can — CI cannot produce a `qa_report.json` without the OSM graph.
  CI instead asserts the committed baseline is alive.

**Committed baseline** (`tests/golden/qa_baseline.json`, from a full 320-run build):

```
total 320   passed 227   fully_ordered 56
mean_ordered 0.8178   mean_strict 0.3810
preflight_fails 0   directness_fails 41   legality_fails 60
```

`passed` / `directness_fails` / `legality_fails` are unchanged from before the
stage, confirming it was measurement-only.

Verified by fault injection against a doctored report: fidelity loss on 20 runs
with `passed` unchanged → caught; gaps shifting earlier so only `strict` moves →
caught; 15 runs vanishing → caught; geometry change → reported but deliberately
**non-gating**, since every router change moves geometry and gating would block
all progress.

### Stage 2 — Constraint compiler (legacy router still routing)

- `compile_constraints(raw_streets, street_to_nodes, junction_index, G)`,
  replacing `build_waypoints_from_streets` as the routing input.
- **Un-merge the junction index** — currently merged into `street_to_nodes`, so
  `get_best_street_match("LILLIE BRIDGE")` returns a key with graph *nodes* but
  zero *edges* of that name, unsatisfiable by a name-matching constraint. This is
  the single highest-leverage fix here: it is what turns the prototype's 110 hard
  failures into successes. `known_junctions` is already threaded separately into
  `preflight_run`, so the plumbing exists.
- Tiered `get_best_street_match` returning `(match, source)`. Its final
  progressive-word-removal branch is unguarded and returns `base` on total
  failure — harmless under a discount, **fatal under a hard constraint**.
  Anything below `abbrev` confidence gets `hard=False`.
- **Restore roundabouts** as `NODE` constraints; `parse_intermediary_file`
  currently drops every line containing "ROUNDABOUT". Reuse the existing BFS ring
  collection.
- **Fix the multi-street parse**: only `parts[1]` is kept, so Run 160
  (`blue_book_runs_intermediary.txt:3380`, `R___ MORNING LANE R___ MARE STREET`)
  yields the phantom street `MORNING LANE R` and loses MARE STREET.
- Add `R/BOUT` to roundabout handling; add obvious slip-road names to
  `street_spelling_fixes.json` / `junction_definitions.json`.
- Memoise `edge_names`.
- Assertion test over a 320-run compile: kind histogram, source histogram, gap list.

### Stage 3 — Ordered search behind a flag

- `KRG_ROUTING_MODE ∈ {legacy, ordered}`, read once in `process_runs`, threaded
  into `_route_fn`. Defaults to `legacy`.
- Implement the ordered A\* in `router.py`, replacing `_route_through_waypoints`.
  `get_constrained_route` keeps its signature.
- For the `session` / `krg route --via` path, default all constraints to
  `hard=False` so ad-hoc queries keep today's forgiving semantics.
- Build `constants/runPoints.ordered.json` side by side and diff the two
  fidelity reports per run.

### Stage 4 — Endpoint plausibility

Independent of routing; catches the Run 131 / 177 / 206 class.

- Promote `Gazetteer._district_centroid` to a reusable public helper. Today it is
  private, lazy, mean-based, built from the Points List only, and consulted
  **solely by the street tier** — tiers 1–3 never see it.
- Switch from mean to a robust centre (median lat/lon) plus a per-district p95
  radius with a floor; skip districts with n < 5. The mean is noisy for W1
  (n=551, p90 = 2307 m) and useless for SW2 (n=1).
- Add a plausibility check to `preflight_run`, which today only checks snap
  distance and therefore passes all 320 runs. Fail beyond
  `max(p95_radius × 1.5, 2500 m)`; warn beyond p95.
- `_PoiTable._best` returns `pool[0]` on district mismatch and only filters when
  `len(pool) > 1` — make a single-candidate wrong-district hit a recorded warning
  rather than a silent accept.

Data is fully available: `postal_district` on 5530/5530 POIs, a parseable
district on 631/631 endpoints.

### Stage 5 — Flip the default (single atomic commit)

- `KRG_ROUTING_MODE` defaults to `ordered`.
- `result.passed = is_legal and is_ordered and no_hard_gaps`. `is_direct` stays
  in the record for triage only — a Knowledge run is by definition not the
  straight line, and ~25% of ordered routes exceed the 1.8 ratio.
- Add `excess_over_ordered_optimum` (route length ÷ the same ordered search with
  connector multiplier and structural penalties zeroed) as the honest
  wastefulness metric. Thresholds cleanly at ~1.15 without punishing Blue Book
  geometry, and is nearly free — the search already has `g` at the goal.
- Fix `check_directness` regardless: its `< 1000 m` branch hard-codes thresholds
  and **ignores config overrides entirely**, so a `run_specific_fixes.json` patch
  on a short run is silently a no-op.
- Delete together: `_clean_backtrack`, `_collapse_revisits`, `_remove_backtracks`,
  `build_waypoints_from_streets`, `find_intersection_node`, and the `fix_*`
  functions in `corrector.py`.
  - *Replacing the lapping suppression:* the IMAX lapping `_collapse_revisits`
    was written for is a symptom of the 0.1× discount making a second orbit
    cheaper than the exit. With the discount gone a lap costs full length plus
    the existing `junction=roundabout` penalty; structurally, the
    `(node, idx, prev)` key dominates a same-`idx` lap, and the U-turn term
    blocks immediate reversal. Rather than mutating laps away, **report** them —
    `diagnostics.detect_loops` / `detect_ring_traversals` already exist; surface
    the count as `ring_laps`.
- `correct_and_validate` becomes the tier ladder, and must **return** the router
  metadata it currently drops. That drop — plus the recompute in `run_pipeline` —
  is why `unreachable_legs` / `truncated_legs` are hardcoded 0 in every QA record
  today. The `best_score` unit bug (dimensionless ratio compared against metres)
  disappears with the ladder.
- `"waypoints"` becomes derived from the routed path — the node where each
  constraint was first satisfied. Strictly more accurate, and free.
- Replace `CollapseRevisitsTests` with an ordered-traversal test and a "no
  prohibited triple in output" test on the existing fixture graph.
- Refresh `tests/golden/qa_baseline.json`; reset `promote_to_app.py --min-passed`
  to the new honest floor.

---

## Acceptance targets

| Metric | Baseline (Stage 1) | Target |
|---|---|---|
| `mean_ordered` (LCS) | 0.818 | ≥ 0.95 |
| `mean_strict` (walk-through) | 0.381 | ≥ 0.90 |
| Runs fully in order | 56 / 320 | ≥ 280 / 320 |
| Runs with a prohibited turn | 60 | **0** |
| Endpoints > 3 km from district | 12 | 0 (or explicitly overridden) |
| `hit_state_cap` | unknown (never reported) | < 10 runs |

`mean_strict` is the demanding one and the one that matches what a driver on the
Knowledge actually has to do. Do not report progress on `mean_ordered` alone.

## Verification

```
krg generate runs                 # ~15-20 min serial
krg qa                            # both fidelity metrics surfaced
krg regression diff --strict      # must exit 0
python scripts/promote_to_app.py --app-dir ../the-blue-app
```

Spot-check the known-bad runs visually (`krg web`): **206, 131, 177, 198, 4, 160**.
Run 206 should go from 700 m to ~8 km.

**Diagnostic split to watch:** `excess_over_ordered_optimum ≈ 1.0` together with
a large directness `ratio` means the *endpoints* are wrong, not the routing. Feed
those runs into `krg audit-endpoints` (which today always exits 0 even with
unresolved endpoints).

## Risks

| Risk | Early warning |
|---|---|
| A wrong hard constraint makes a run infeasible — `get_best_street_match` ends in unguarded word removal | `constraint_sources` histogram; rising `word_removal` demotions mean the resolver is guessing |
| `NODE` constraints too weak — a gyratory satisfied by clipping one member node | Visual diff of the ~40 junction-bearing runs; mitigate by requiring two consecutive member nodes |
| `passed` collapses on the gate swap | Do the gate swap, the deletions and the golden refresh atomically in Stage 5 |
| Blue Book text genuinely inconsistent with OSM (`MORNING LANE R`, `TRAFALGAR SQUARE (EAST SIDE)`, `REDCLIFFE GARDENS CONTINUED`) | `constraint_gaps` grouped by raw text — anything appearing 2+ times is a parser or curation bug, not a one-off. Today: SERPENTINE ROAD ×4, HILLGROVE ROAD ×3, R/BOUT ×2, BOW INTERCHANGE ×2, HAMMERSMITH BRIDGE ×2 |
| State-cap thrash — an infeasible constraint burns the budget before demotion | `hit_state_cap` count; on cap-hit, demote at `max_idx` immediately rather than treating it as exhaustion |
| `krg route --via` regresses — loose street names become hard requirements | Default `hard=False` on the session path |

**Not doing in v1: multiprocessing.** ~830 searches × 0.86 s ≈ 12 min, comparable
to today. If added later, use `multiprocessing.get_context("fork")` and load the
graph and indexes in the parent before forking — macOS `spawn` reloads the 168 MB
graphml per worker. The existing resume machinery is the better lever.

## Out of scope (flagged, not planned)

Blue Book **direction verbs** (`L`, `R`, `F`, `COM`, `LOL`, `LOR`, `B/R`, `L/BY`)
are discarded at parse time and never validated against the produced route.
Ordered street traversal implies most turns, but not all. Worth a follow-up once
ordering is solid.
