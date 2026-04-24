"""
Phase 4 regression harness.

Captures a compact per-run fingerprint of ``qa_report.json`` that is safe
to commit to the repo, and compares a later pipeline run against it.

Intended flow::

    krg bluebookdemo                # generates constants/qa_report.json
    krg regression snapshot         # freezes into tests/golden/qa_baseline.json
    # ... change code ...
    krg bluebookdemo
    krg regression diff             # shows which runs newly pass/fail and
                                    #  whether aggregate metrics regressed

A run's fingerprint intentionally omits the full geometry — that would
balloon the diff and couple it to upstream graph refreshes. What we
snapshot is the *classification*: did preflight pass, was the route
considered direct, was it legal, and a stable hash of the node
sequence for change detection.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

DEFAULT_BASELINE_PATH = Path("tests/golden/qa_baseline.json")
DEFAULT_REPORT_PATH = Path("constants/qa_report.json")


@dataclasses.dataclass(frozen=True)
class RunFingerprint:
    preflight_ok: bool
    is_direct: bool | None
    legal: bool | None
    passed: bool
    ratio: float | None
    total_distance_m: float | None
    node_count: int
    route_hash: str


@dataclasses.dataclass
class Snapshot:
    total: int
    passed: int
    preflight_fails: int
    directness_fails: int
    legality_fails: int
    runs: dict[str, RunFingerprint]

    def to_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "preflight_fails": self.preflight_fails,
            "directness_fails": self.directness_fails,
            "legality_fails": self.legality_fails,
            "runs": {k: dataclasses.asdict(v) for k, v in self.runs.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Snapshot":
        runs = {
            k: RunFingerprint(
                preflight_ok=v["preflight_ok"],
                is_direct=v["is_direct"],
                legal=v["legal"],
                passed=v["passed"],
                ratio=v["ratio"],
                total_distance_m=v["total_distance_m"],
                node_count=v["node_count"],
                route_hash=v["route_hash"],
            )
            for k, v in data.get("runs", {}).items()
        }
        return cls(
            total=data["total"],
            passed=data["passed"],
            preflight_fails=data["preflight_fails"],
            directness_fails=data["directness_fails"],
            legality_fails=data["legality_fails"],
            runs=runs,
        )


def _hash_route(route: Any) -> str:
    if not route:
        return ""
    if isinstance(route, list):
        payload = json.dumps(route, sort_keys=True, separators=(",", ":"))
    else:
        payload = json.dumps(route, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def fingerprint_run(qa_entry: dict[str, Any]) -> RunFingerprint:
    route_nodes = qa_entry.get("route_nodes") or qa_entry.get("route") or []
    return RunFingerprint(
        preflight_ok=bool(qa_entry.get("preflight_ok", True)),
        is_direct=qa_entry.get("is_direct"),
        legal=qa_entry.get("legal"),
        passed=bool(qa_entry.get("passed", False)),
        ratio=qa_entry.get("ratio"),
        total_distance_m=qa_entry.get("total_distance_m") or qa_entry.get("distance_m"),
        node_count=len(route_nodes) if isinstance(route_nodes, list) else 0,
        route_hash=_hash_route(route_nodes),
    )


def summarise(report_path: Path = DEFAULT_REPORT_PATH) -> Snapshot:
    data = json.loads(Path(report_path).read_text())
    runs = {str(k): fingerprint_run(v) for k, v in data.items()}
    passed = sum(1 for v in runs.values() if v.passed)
    preflight_fails = sum(1 for v in runs.values() if not v.preflight_ok)
    directness_fails = sum(
        1 for v in runs.values() if v.preflight_ok and v.is_direct is False
    )
    legality_fails = sum(
        1 for v in runs.values() if v.preflight_ok and v.legal is False
    )
    return Snapshot(
        total=len(runs),
        passed=passed,
        preflight_fails=preflight_fails,
        directness_fails=directness_fails,
        legality_fails=legality_fails,
        runs=runs,
    )


def save_snapshot(snap: Snapshot, path: Path = DEFAULT_BASELINE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap.to_json(), indent=2, sort_keys=True))


def load_snapshot(path: Path = DEFAULT_BASELINE_PATH) -> Snapshot:
    return Snapshot.from_json(json.loads(Path(path).read_text()))


@dataclasses.dataclass
class Diff:
    aggregate_regressions: list[str]
    aggregate_improvements: list[str]
    newly_failing: list[str]
    newly_passing: list[str]
    geometry_changed: list[str]

    @property
    def has_regressions(self) -> bool:
        return bool(self.aggregate_regressions or self.newly_failing)


def diff(baseline: Snapshot, current: Snapshot) -> Diff:
    aggregate_regressions: list[str] = []
    aggregate_improvements: list[str] = []

    def _track(name: str, base: int, curr: int, higher_is_better: bool) -> None:
        if curr == base:
            return
        line = f"{name}: {base} -> {curr}"
        better = curr > base if higher_is_better else curr < base
        (aggregate_improvements if better else aggregate_regressions).append(line)

    _track("passed", baseline.passed, current.passed, higher_is_better=True)
    _track("preflight_fails", baseline.preflight_fails, current.preflight_fails, higher_is_better=False)
    _track("directness_fails", baseline.directness_fails, current.directness_fails, higher_is_better=False)
    _track("legality_fails", baseline.legality_fails, current.legality_fails, higher_is_better=False)

    newly_failing: list[str] = []
    newly_passing: list[str] = []
    geometry_changed: list[str] = []

    for run_id, curr_fp in current.runs.items():
        base_fp = baseline.runs.get(run_id)
        if base_fp is None:
            continue  # new run — not a regression
        if base_fp.passed and not curr_fp.passed:
            newly_failing.append(run_id)
        elif not base_fp.passed and curr_fp.passed:
            newly_passing.append(run_id)
        if base_fp.route_hash and curr_fp.route_hash and base_fp.route_hash != curr_fp.route_hash:
            geometry_changed.append(run_id)

    return Diff(
        aggregate_regressions=aggregate_regressions,
        aggregate_improvements=aggregate_improvements,
        newly_failing=sorted(newly_failing, key=lambda s: int(s) if s.isdigit() else s),
        newly_passing=sorted(newly_passing, key=lambda s: int(s) if s.isdigit() else s),
        geometry_changed=sorted(geometry_changed, key=lambda s: int(s) if s.isdigit() else s),
    )


def format_diff(d: Diff) -> str:
    lines: list[str] = []
    if d.aggregate_improvements:
        lines.append("Improvements:")
        lines.extend(f"  + {x}" for x in d.aggregate_improvements)
    if d.aggregate_regressions:
        lines.append("Regressions:")
        lines.extend(f"  - {x}" for x in d.aggregate_regressions)
    if d.newly_passing:
        lines.append(f"Newly passing ({len(d.newly_passing)}):")
        lines.append("  " + ", ".join(d.newly_passing[:30])
                     + (" ..." if len(d.newly_passing) > 30 else ""))
    if d.newly_failing:
        lines.append(f"Newly FAILING ({len(d.newly_failing)}):")
        lines.append("  " + ", ".join(d.newly_failing[:30])
                     + (" ..." if len(d.newly_failing) > 30 else ""))
    if d.geometry_changed:
        lines.append(f"Geometry changed ({len(d.geometry_changed)} runs)")
    if not lines:
        lines.append("No differences vs baseline.")
    return "\n".join(lines)
