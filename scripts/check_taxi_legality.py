"""Check a runPoints.json against the taxi rules: does any route pass a modal
filter (bollard, planter, bus trap ...), use a way closed to taxis, or pass
through an access-only street?

The routes' ``nodes`` are walked on the graph they were built on (``drive``
for data built before the taxi profile), and each edge is checked with
``TaxiRules.check_route``, which matches barriers by node id and by the edge
geometry, so a filter inside a simplified edge is still found.

    python scripts/check_taxi_legality.py constants/runPoints.json \
        --network-type drive --out /tmp/taxi_check.json
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from knowledge_run_generator.router import load_graph, load_taxi_rules  # noqa: E402
from knowledge_run_generator.taxi_profile import describe_violation  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs")
    ap.add_argument("--network-type", default="drive")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    rules = load_taxi_rules()
    if rules is None:
        sys.exit("No taxi rules sidecar: build the taxi graph first (krg generate runs).")
    G = load_graph(network_type=args.network_type)
    runs = json.loads(Path(args.runs).read_text())
    report = {}
    for run in runs:
        found = {}
        for label, key in (("AB", "route"), ("BA", "routeReverse")):
            nodes = (run.get(key) or {}).get("nodes") or []
            missing = sum(1 for a, b in zip(nodes, nodes[1:]) if not G.has_edge(a, b))
            v = rules.check_route(G, nodes)
            if v or missing:
                found[label] = {"violations": v, "edges_not_in_graph": missing,
                                "summary": [describe_violation(x) for x in v]}
        if found:
            report[str(run["id"])] = {"title": run.get("title"), **found}
    for rid, rec in sorted(report.items(), key=lambda kv: int(kv[0])):
        for label in ("AB", "BA"):
            if label in rec:
                print(f"run {rid} {label}: {'; '.join(rec[label]['summary']) or ''}"
                      f"{' (edges missing: %d)' % rec[label]['edges_not_in_graph'] if rec[label]['edges_not_in_graph'] else ''}")
    print(f"{len(report)} of {len(runs)} runs have a taxi-legality problem")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
