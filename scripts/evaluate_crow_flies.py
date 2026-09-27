"""Crow-flies agreement study: how well does the unconstrained crow-flies
router reproduce the Blue Book?

The runs that pass QA under their Blue Book sequence are human-authored
ground truth. Each is re-routed with the crow-flies mode (no constraints) on
the taxi graph, for every configuration in the grid, and compared with the
shipped Blue Book route:

* ``recall``: share of the Blue Book route's length that the crow-flies route
  also drives (within ``--tolerance`` metres);
* ``precision``: share of the crow-flies route's length that lies on the Blue
  Book route;
* ``jaccard``: shared length / union length, with shared length the mean of
  the two covered lengths;
* ``length_ratio``: crow-flies length / Blue Book length;
* ``max_offset_m``: the crow-flies route's furthest point from the
  start-to-end line (and the Blue Book route's, for reference).

Usage::

    python scripts/evaluate_crow_flies.py --runs constants/runPoints.json \
        --qa constants/qa_report.json --out /tmp/crow_study.json --workers 4

The summary (per configuration: median / mean / quartiles, and the share of
runs at Jaccard >= 0.8 and >= 0.95) is printed and written into ``--out``.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shapely.geometry import LineString  # noqa: E402

G = None
TURNS = None

LON0, LAT0 = -0.127917, 51.507389
KX = 111_320.0 * math.cos(math.radians(LAT0))
KY = 110_540.0


def _proj(coords):
    return [((lon - LON0) * KX, (lat - LAT0) * KY) for lon, lat in coords]


def _dedupe(pts):
    out = []
    for p in pts:
        if not out or p != out[-1]:
            out.append(p)
    return out


def agreement(bb_coords, cf_coords, tolerance):
    bb = LineString(_dedupe(_proj(bb_coords)))
    cf = LineString(_dedupe(_proj(cf_coords)))
    a, b = bb.length, cf.length
    cov_bb = bb.intersection(cf.buffer(tolerance, cap_style=2)).length
    cov_cf = cf.intersection(bb.buffer(tolerance, cap_style=2)).length
    shared = (cov_bb + cov_cf) / 2.0
    union = a + b - shared
    return {
        "recall": round(cov_bb / a, 4) if a else None,
        "precision": round(cov_cf / b, 4) if b else None,
        "jaccard": round(shared / union, 4) if union > 0 else None,
    }


def max_offset(coords, start, end):
    from knowledge_run_generator.router import _point_segment_distance
    (sx, sy), (ex, ey) = _proj([start, end])
    bx, by = ex - sx, ey - sy
    b2 = bx * bx + by * by
    return round(max(_point_segment_distance(x - sx, y - sy, bx, by, b2)
                     for x, y in _proj(coords)), 1)


def _init(network_type):
    global G, TURNS
    from knowledge_run_generator.router import load_graph
    from knowledge_run_generator.validator import load_turn_restrictions
    from knowledge_run_generator.cache import cache_dir
    G = load_graph(network_type=network_type)
    TURNS = load_turn_restrictions(G, cache_dir=str(cache_dir()))


def _endpoint(run, which):
    import osmnx as ox
    route = run["route"]
    nodes = route.get("nodes") or []
    node = nodes[0] if which == "start" else nodes[-1]
    if node in G.nodes:
        return node
    lon, lat = run[which]["coordinates"]
    return ox.distance.nearest_nodes(G, lon, lat)


def evaluate_run(task):
    run, configs, tolerance = task
    from knowledge_run_generator.router import (
        nodes_to_coords_geometry, route_crow_flies, _route_length)
    o, d = _endpoint(run, "start"), _endpoint(run, "end")
    bb = run["route"]["geometry"]["coordinates"]
    start = (G.nodes[o]["x"], G.nodes[o]["y"])
    end = (G.nodes[d]["x"], G.nodes[d]["y"])
    rec = {"id": run["id"], "bb_length_m": run["route"]["distance"],
           "bb_max_offset_m": max_offset(bb, start, end), "configs": {}}
    for name, cfg in configs.items():
        t0 = time.time()
        route, meta = route_crow_flies(G, o, d, prohibited_turns=TURNS, **cfg)
        if not route:
            rec["configs"][name] = {"error": "no route",
                                    "states": meta.get("states_explored")}
            continue
        coords = nodes_to_coords_geometry(G, route)
        length = _route_length(G, route)
        m = agreement(bb, coords, tolerance)
        m.update({
            "length_m": round(length, 1),
            "length_ratio": round(length / run["route"]["distance"], 4),
            "max_offset_m": max_offset(coords, start, end),
            "states": meta.get("states_explored"),
            "seconds": round(time.time() - t0, 2),
        })
        rec["configs"][name] = m
    return rec


def _q(values, q):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    return round(values[f] + (values[c] - values[f]) * (k - f), 4)


def summarise(records, names):
    out = {}
    for name in names:
        rows = [r["configs"][name] for r in records
                if name in r["configs"] and "error" not in r["configs"][name]]
        s = {"runs": len(rows),
             "errors": sum(1 for r in records if "error" in r["configs"].get(name, {}))}
        for key in ("jaccard", "recall", "precision", "length_ratio", "max_offset_m"):
            vals = [r[key] for r in rows if r.get(key) is not None]
            s[key] = {"mean": round(statistics.fmean(vals), 4) if vals else None,
                      "p25": _q(vals, 0.25), "median": _q(vals, 0.5),
                      "p75": _q(vals, 0.75), "p10": _q(vals, 0.10)}
        jac = [r["jaccard"] for r in rows]
        s["jaccard_ge_0.95"] = round(sum(j >= 0.95 for j in jac) / len(jac), 4) if jac else None
        s["jaccard_ge_0.8"] = round(sum(j >= 0.8 for j in jac) / len(jac), 4) if jac else None
        s["jaccard_lt_0.5"] = round(sum(j < 0.5 for j in jac) / len(jac), 4) if jac else None
        # Offset excess over the Blue Book route's own offset.
        exc = [r["configs"][name]["max_offset_m"] - r["bb_max_offset_m"]
               for r in records if name in r["configs"] and "error" not in r["configs"][name]]
        s["offset_excess_over_bb_m"] = {"median": _q(exc, 0.5), "p90": _q(exc, 0.9)}
        out[name] = s
    return out


def parse_grid(spec):
    """``name=lam:mode[:class=w,class=w]`` items, comma-free between items
    (separated by spaces)."""
    configs = {}
    for item in spec:
        name, body = item.split("=", 1)
        parts = body.split(":")
        cfg = {"lam": float(parts[0]), "mode": parts[1] if len(parts) > 1 else "relative"}
        if len(parts) > 2 and parts[2]:
            cfg["class_weights"] = {k: float(v) for k, v in
                                    (kv.split("/") for kv in parts[2].split(","))}
        else:
            cfg["class_weights"] = {}
        configs[name] = cfg
    return configs


DEFAULT_GRID = [
    "shortest=0:relative",
    "rel0.25=0.25:relative", "rel0.5=0.5:relative", "rel1=1:relative",
    "rel2=2:relative", "rel4=4:relative", "rel8=8:relative",
    "abs0.25=0.25:absolute", "abs1=1:absolute",
]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", default=str(ROOT / "constants" / "runPoints.json"))
    ap.add_argument("--qa", default=str(ROOT / "constants" / "qa_report.json"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--network-type", default="taxi")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--tolerance", type=float, default=15.0)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ids", help="comma-separated run ids")
    ap.add_argument("--grid", nargs="*", default=DEFAULT_GRID)
    args = ap.parse_args(argv)

    runs = json.loads(Path(args.runs).read_text())
    qa = json.loads(Path(args.qa).read_text())
    passing = [r for r in runs if (qa.get(str(r["id"])) or {}).get("passed")]
    if args.ids:
        want = {int(x) for x in args.ids.split(",")}
        passing = [r for r in passing if r["id"] in want]
    if args.limit:
        passing = passing[:args.limit]
    configs = parse_grid(args.grid)
    print(f"{len(passing)} Blue Book runs x {len(configs)} configurations")

    ctx = mp.get_context("fork")
    tasks = [(r, configs, args.tolerance) for r in passing]
    records = []
    t0 = time.time()
    if args.workers <= 1:
        _init(args.network_type)
        for i, t in enumerate(tasks):
            records.append(evaluate_run(t))
            print(f"  {i + 1}/{len(tasks)} run {t[0]['id']}", flush=True)
    else:
        # Load once in the parent, then fork: the children share the graph.
        _init(args.network_type)
        with ctx.Pool(args.workers) as pool:
            for i, rec in enumerate(pool.imap_unordered(evaluate_run, tasks, chunksize=1)):
                records.append(rec)
                if (i + 1) % 10 == 0:
                    print(f"  {i + 1}/{len(tasks)} ({time.time() - t0:.0f}s)", flush=True)
    records.sort(key=lambda r: r["id"])
    summary = summarise(records, list(configs))
    Path(args.out).write_text(json.dumps(
        {"configs": configs, "tolerance_m": args.tolerance,
         "summary": summary, "runs": records}, indent=1))
    print(f"\n{'config':<14}{'jac med':>8}{'jac mean':>9}{'>=0.8':>7}{'>=0.95':>7}"
          f"{'<0.5':>6}{'recall':>8}{'prec':>7}{'len med':>8}{'off med':>8}")
    for name, s in summary.items():
        print(f"{name:<14}{s['jaccard']['median']:>8}{s['jaccard']['mean']:>9}"
              f"{s['jaccard_ge_0.8']:>7}{s['jaccard_ge_0.95']:>7}{s['jaccard_lt_0.5']:>6}"
              f"{s['recall']['median']:>8}{s['precision']['median']:>7}"
              f"{s['length_ratio']['median']:>8}{s['max_offset_m']['median']:>8}")
    print(f"done in {time.time() - t0:.0f}s -> {args.out}")


if __name__ == "__main__":
    main()
