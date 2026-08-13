"""Compile all 320 Blue Book runs into constraints and report health.

Loads the cached graph and indexes (no network), runs the Stage 2 constraint
compiler over every run, and prints kind/source histograms plus every gap
grouped by raw text. Writes the full per-run detail to
``constants/constraint_report.json`` for diffing.

    .venv/bin/python scripts/compile_report.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from knowledge_run_generator.aliases import load_or_build_alias_index
from knowledge_run_generator.cache import cache_dir
from knowledge_run_generator.constraints import compile_constraints
from knowledge_run_generator.junctions import build_junction_index
from knowledge_run_generator.router import load_graph
from knowledge_run_generator.blue_book_demo.run_pipeline import (
    build_street_index,
    load_street_spelling_fixes,
    parse_intermediary_lines,
)

DEMO_DIR = ROOT / "knowledge_run_generator" / "blue_book_demo"


def main():
    print("Loading graph...")
    G = load_graph()
    cdir = cache_dir()
    street_index = build_street_index(G, cdir)
    alias_index = load_or_build_alias_index(G, cdir / "alias_index.pkl")
    junction_index = build_junction_index(alias_index, G=G)
    spelling_fixes = load_street_spelling_fixes()

    titles, run_lines = parse_intermediary_lines(
        DEMO_DIR / "blue_book_runs_intermediary.txt"
    )

    kinds, sources = Counter(), Counter()
    gap_counter = Counter()
    per_run = {}
    runs_with_gaps = 0
    for rid in sorted(titles):
        compiled = compile_constraints(
            run_lines[rid], street_index,
            junction_index=junction_index, G=G,
            spelling_fixes=spelling_fixes,
        )
        kinds.update(compiled.kind_histogram())
        sources.update(compiled.source_histogram())
        if compiled.gaps:
            runs_with_gaps += 1
            gap_counter.update(g.upper() for g in compiled.gaps)
        per_run[rid] = {
            "constraints": len(compiled.constraints),
            "kinds": compiled.kind_histogram(),
            "sources": compiled.source_histogram(),
            "gaps": compiled.gaps,
            "soft": [c.raw for c in compiled.constraints if not c.hard],
        }

    total_c = sum(kinds.values())
    print(f"\n{len(titles)} runs, {total_c} constraints")
    print(f"kinds:   {dict(kinds)}")
    print(f"sources: {dict(sources)}")
    print(f"runs with gaps: {runs_with_gaps}")
    print(f"\ngaps grouped by text ({sum(gap_counter.values())} total):")
    for text, n in gap_counter.most_common():
        print(f"  {n:3d}  {text}")

    out = ROOT / "constants" / "constraint_report.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(
        {"kinds": dict(kinds), "sources": dict(sources),
         "runs_with_gaps": runs_with_gaps, "per_run": per_run},
        indent=2, default=str,
    ))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
