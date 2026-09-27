# Roadmap — reaching the Knowledge standard

> **Status (2026-08-12): delivered.** All five stages are complete, the
> ordered-constraint router is the only router, and every acceptance target
> is met — see [Acceptance targets](#acceptance-targets--all-met-2026-08-12-build).
> The sections below are kept as the record of the problem and the design
> that closed it; figures in the problem statement describe the *pre-work*
> state.

## The problem this roadmap existed to fix

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

### Stage 2 — Constraint compiler ✅ **done**

Landed as `knowledge_run_generator/constraints.py`; measured over the full
corpus (`scripts/compile_report.py`): **4,827 constraints across 320 runs,
95.6% resolved by a hard tier (exact/junction), 82 rings, 65 explicit gaps
in 56 runs** — no line ever silently becomes an unsatisfiable constraint.

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

### Stage 3 — Ordered search ✅ **done**

- Landed as `get_ordered_route` / `route_ordered_with_ladder` in `router.py`:
  state `(node, idx, prev_node)`, the goal test *is* the ordering, prohibited
  turns filtered inside the expansion, admissible anchor heuristic memoised on
  `(node, idx)`, corridor bbox, per-run state cap.
- Degradation ladder T0–T3 with every demotion recorded. Victim selection
  prefers a *soft* constraint at or before the blocker — when a correctly
  resolved hard street looks unreachable, the culprit is usually an earlier
  low-confidence guess pinning the search to the wrong place.
- The `session` / `krg route --via` path compiles via names as soft
  constraints, keeping ad-hoc queries forgiving.
- The staging flag came and went inside the stage; with Stage 5's deletions
  there is nothing left for `KRG_ROUTING_MODE=legacy` to select.

### Stage 4 — Endpoint plausibility ✅ **done**

Landed as `gazetteer.DistrictModel` (public, median centre + p95 radius with a
1 km floor; districts under 5 points keep a centre for street disambiguation
but never *fail* anyone) wired into `preflight_run` — fail beyond
`max(p95 × 1.5, 2500 m)`, warn beyond p95. `_PoiTable._best` now flags a
wrong-district winner (`_district_mismatch`), and `Gazetteer.resolve` prefers
the street tier over a flagged point record when the name is a street in the
graph — which is exactly how "SHORTLANDS W6" stops resolving into Bromley.
Verified live: Runs 131 and 177 now fail preflight with
"start resolved 18789m / 17630m from the centre of W6 / W1 — wrong place".

### Stage 5 — Flip the default ✅ **done**

- The ordered search is the only router; `KRG_ROUTING_MODE` accepts nothing
  else, and the legacy machinery is deleted: `get_constrained_route`,
  `_route_through_waypoints`, `_clean_backtrack`, `_collapse_revisits`,
  `_remove_backtracks`, `build_waypoints_from_streets`,
  `find_intersection_node`, `get_best_street_match`, and the whole of
  `corrector.py`.
- `result.passed = is_legal and is_ordered and hard_gaps == 0`. `is_direct`
  stays in the record for triage only. Ordering is measured against the
  *compiled constraints* (`validator.check_constraint_order`), so junction and
  gyratory lines are satisfiable as NODE positions instead of permanently
  depressing a street-name walk, and unresolvable lines are explicit gaps
  rather than phantom missing streets.
- `excess_over_ordered_optimum` recorded per run (route length ÷ pure-length
  ordered search over the same constraint set).
- `check_directness` honours explicit config overrides at every distance; the
  `< 1000 m` band no longer hard-codes its thresholds.
- Laps are **reported** (`ring_laps`, via `diagnostics.detect_ring_traversals`)
  instead of being spliced away after validation.
- `"waypoints"` is derived from the routed path — the node where each
  constraint is first satisfied (`router.constraint_waypoints`).
- `CollapseRevisitsTests` replaced by ordered-traversal, ring-lap and
  no-prohibited-triple tests (`tests/test_ordered_router.py`,
  `tests/test_router_regression.py`).
- `tests/golden/qa_baseline.json` refreshed from the full ordered build;
  `promote_to_app.py --min-passed` reset to the new honest floor.
- `QA_SCHEMA_VERSION` → 4 (resumes re-route anything older).

---

## Stage 6: taxi-legal graph and crow-flies routes (2026-09)

The Blue Book sequence of some runs can no longer be driven (Hammersmith
Bridge, LTN filters, one-way changes that force a lap of 1 km or more). For
those runs the app ships a new route made by the Knowledge rule itself: the
legal taxi route that stays closest to the straight line.

### Taxi graph

`taxi_profile.py` replaces osmnx's private-car `drive` profile; its
docstring is the tag record. On the 2026-09 extract: 3,938 ways closed to
taxis, 662 access-only, 26 taxi contraflows, 1,653 blocking barrier nodes
cut. The `drive` graph the app shipped before this stage kept most of those
open: checked against these rules, **89 of the 320 shipped runs** (either
direction) passed a modal filter, drove a way closed to taxis (`access=no`
bus links at St George's Circus, Charing Cross Road and Waterloo Road,
`vehicle=no` on City Road and Buckingham Palace Road, The Mall's
`access=no` carriageway) or passed through an access-only street.
`scripts/check_taxi_legality.py` reproduces the list.

### Choosing lambda: agreement with the Blue Book

The 294 runs that pass under their Blue Book sequence are human-authored
ground truth. Each was re-routed unconstrained with crow-flies on the taxi
graph and compared with its shipped Blue Book route (15 m tolerance).
Jaccard is shared length over union length.

| Mode | Jaccard mean | median | >= 0.8 | < 0.5 | recall med | precision med | length / BB med |
|---|---|---|---|---|---|---|---|
| shortest legal (lambda 0) | 0.462 | 0.427 | 18.0% | 57.8% | 0.539 | 0.655 | 0.903 |
| lambda 0.5 | 0.493 | 0.460 | 18.4% | 54.8% | 0.592 | 0.677 | 0.905 |
| lambda 1 | 0.507 | 0.479 | 19.1% | 53.4% | 0.607 | 0.694 | 0.911 |
| lambda 2 | 0.493 | 0.457 | 16.0% | 55.4% | 0.590 | 0.665 | 0.916 |
| lambda 4 | 0.471 | 0.447 | 14.3% | 60.5% | 0.582 | 0.662 | 0.925 |
| lambda 1, minor roads x1.15 | 0.520 | 0.502 | 22.5% | 50.0% | 0.636 | 0.710 | 0.915 |
| lambda 1, minor roads x1.2 | 0.522 | 0.517 | 22.8% | 48.6% | 0.642 | 0.713 | 0.915 |
| lambda 1, minor roads x1.5 | 0.488 | 0.481 | 19.4% | 53.4% | 0.605 | 0.680 | 0.924 |
| **lambda 1.5, minor roads x1.15 (chosen)** | **0.528** | 0.504 | 22.5% | 49.7% | 0.636 | 0.717 | 0.915 |

Lambda is relative (offset normalised by the run's straight line, floored
at 1 km); an absolute-per-km form scored the same at its best (0.507).
"Minor roads" are residential, living_street and unclassified. The chosen
mode has the best mean agreement, ties for the most runs at Jaccard >= 0.8,
and keeps the offset tail close to the Blue Book's (90th percentile 127 m
further from the line than the Blue Book route, against 308 m for shortest
legal).

**Honest reading: agreement is moderate, not high.** Half the runs share
less than half their length with the Blue Book route; about one in five
reproduces it almost exactly. Where they disagree the Blue Book is longer
(crow-flies is a median 91.5% of its length) and runs on main roads: the
low-agreement runs are the ones whose Blue Book route is well over the
shortest legal route (median 1.18x, against 1.02x for the runs that agree).
The Blue Book is not a shortest or straightest route; it is a
main-road-first route taught to be recited. The road-class weight captures
some of that (+0.02 mean Jaccard, +4 points at >= 0.8); heavier weights
make it worse, so the remaining gap is not a simple class preference.
Crow-flies routes are legal, sane and close to the line, which is the
Knowledge rule, but they are not what a Blue Book author would have
written, and they should be presented as generated.

### Runs that ship crow-flies

26 (2026-09 build), each with its reason on the record:

- Hammersmith Bridge closed to motor vehicles: 188.
- Modal filter, mapped in OSM as the road re-tagged `highway=cycleway` or
  `pedestrian`: 16 (Greenwood Road), 177 (Vigo Street), 199 (Cowcross
  Street), 244 (Bloomsbury Square), 292 (Braes Street).
- The Blue Book route passes through an access-only street: 88 (Margery
  Street), 173 (Burleigh Street).
- The Blue Book order needs a lap of 1 km or more on today's roads (one-way
  or banned-turn changes): 13, 52 (Stepney High Street), 68, 102, 201 (Alie
  Street), 121, 169, 171, 175 (Old Palace Yard), 172, 256, 261 (Holborn
  Circus), 238, 250, 254, 260.
- The Blue Book route fails the sanity gate: 56 (TfL's "Spitalfields
  Market, E10" is in E1), 258.

Runs 55 and 231 now pass under the Blue Book on the taxi graph.

## Acceptance targets — **all met** (2026-08-12 build)

| Metric | Baseline (Stage 1) | Target | **Achieved** |
|---|---|---|---|
| `mean_ordered` (LCS) | 0.818 | ≥ 0.95 | **0.990** |
| `mean_strict` (walk-through) | 0.381 | ≥ 0.90 | **0.973** |
| Runs fully in order | 56 / 320 | ≥ 280 / 320 | **299 / 320** |
| Runs passing the ordered gate | — | — | **316 / 320** |
| Runs with a prohibited turn | 60 | **0** | **0** |
| Endpoints > 3 km from district | 12 | 0 (or explicitly overridden) | **0** unoverridden |
| `hit_state_cap` | unknown (never reported) | < 10 runs | **0** |

Both fidelity metrics are measured against **every compiled constraint**,
including any the ladder demoted — the router cannot inflate them by
retreating. The 4 runs failing the gate (46, 188, 189, 250) are OSM-vs-Blue-
Book drift: Hammersmith Bridge is closed to motor traffic, Lewisham's Station
Road was removed by the Gateway development, and Run 250's Bloomsbury squares
are LTN-restricted. Each ships as the best legal approximation
(`routing_mode: shortest_path` / `ordered_relaxed`) with the abandoned
constraints named in `demoted_constraints`.

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
