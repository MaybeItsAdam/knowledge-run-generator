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

# Anchored to the repo, not the working directory. As bare relative paths these
# resolved differently depending on where the test runner was launched from,
# which is a silent skip rather than an error.
_REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_BASELINE_PATH = _REPO_ROOT / "tests" / "golden" / "qa_baseline.json"
DEFAULT_REPORT_PATH = _REPO_ROOT / "constants" / "qa_report.json"


@dataclasses.dataclass(frozen=True)
class RunFingerprint:
    preflight_ok: bool
    is_direct: bool | None
    legal: bool | None
    passed: bool
    ratio: float | None
    distance_m: float | None
    node_count: int
    route_hash: str
    # Blue Book fidelity. `passed` can be true for a route that traverses none
    # of the prescribed streets, so tracking it alone hides the regressions
    # that matter most.
    #
    # Both ordered metrics are kept because they move independently. They agree
    # at 1.0, but below it `ordered_coverage` (longest ordered subsequence) is
    # insensitive to *where* a gap falls, while `strict_ordered` (walk until
    # the first unmatched street) is dominated by it. A change that pushes gaps
    # earlier in the sequence shows up only in the strict figure.
    ordered_coverage: float | None = None
    strict_ordered: float | None = None
    street_coverage: float | None = None
    routing_mode: str | None = None


@dataclasses.dataclass
class Snapshot:
    total: int
    passed: int
    preflight_fails: int
    directness_fails: int
    legality_fails: int
    runs: dict[str, RunFingerprint]
    # Count of runs traversing their whole Blue Book sequence in order. This is
    # the number the Knowledge standard actually cares about; `passed` is a
    # weaker claim about legality and directness. (A run is fully ordered under
    # one ordered metric exactly when it is under the other, so one count
    # serves both.)
    fully_ordered: int = 0
    # Corpus means, tracked separately because they move independently below
    # 1.0 — see the note on RunFingerprint.
    mean_ordered: float = 0.0
    mean_strict: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "preflight_fails": self.preflight_fails,
            "directness_fails": self.directness_fails,
            "legality_fails": self.legality_fails,
            "fully_ordered": self.fully_ordered,
            "mean_ordered": self.mean_ordered,
            "mean_strict": self.mean_strict,
            "runs": {k: dataclasses.asdict(v) for k, v in self.runs.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Snapshot":
        # `.get` throughout: a baseline frozen by an older build predates the
        # fidelity fields, and refusing to load it would mean the first run
        # after an upgrade has nothing to diff against.
        runs = {
            k: RunFingerprint(
                preflight_ok=v.get("preflight_ok", True),
                is_direct=v.get("is_direct"),
                legal=v.get("legal"),
                passed=v.get("passed", False),
                ratio=v.get("ratio"),
                distance_m=v.get("distance_m"),
                node_count=v.get("node_count", 0),
                route_hash=v.get("route_hash", ""),
                ordered_coverage=v.get("ordered_coverage"),
                strict_ordered=v.get("strict_ordered"),
                street_coverage=v.get("street_coverage"),
                routing_mode=v.get("routing_mode"),
            )
            for k, v in data.get("runs", {}).items()
        }
        return cls(
            total=data["total"],
            passed=data["passed"],
            preflight_fails=data["preflight_fails"],
            directness_fails=data["directness_fails"],
            legality_fails=data["legality_fails"],
            fully_ordered=data.get("fully_ordered", 0),
            mean_ordered=data.get("mean_ordered", 0.0),
            mean_strict=data.get("mean_strict", 0.0),
            runs=runs,
        )


def hash_nodes(nodes: Any) -> str:
    """Stable short hash of a route's node sequence.

    Lives here rather than in the pipeline so the writer and the comparer can
    never disagree about what a route hash is.
    """
    if not nodes:
        return ""
    payload = ",".join(str(int(n)) for n in nodes)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def fingerprint_run(qa_entry: dict[str, Any]) -> RunFingerprint:
    """Compact, commit-safe summary of one QA record.

    Every key read here is one the QA writer actually emits. An earlier version
    read ``route_nodes``/``total_distance_m``, which it never did — so every
    fingerprint carried ``route_hash=""`` and ``node_count=0``, and the
    geometry-change check in :func:`diff` could not fire at all.
    """
    return RunFingerprint(
        preflight_ok=bool(qa_entry.get("preflight_ok", True)),
        is_direct=qa_entry.get("is_direct"),
        legal=qa_entry.get("legal"),
        passed=bool(qa_entry.get("passed", False)),
        ratio=qa_entry.get("ratio"),
        distance_m=qa_entry.get("fwd_distance_m"),
        node_count=qa_entry.get("node_count", 0),
        route_hash=qa_entry.get("route_hash", ""),
        ordered_coverage=qa_entry.get("ordered_coverage"),
        strict_ordered=qa_entry.get("strict_ordered"),
        street_coverage=qa_entry.get("street_coverage"),
        routing_mode=qa_entry.get("routing_mode"),
    )


def summarise(report_path: Path = DEFAULT_REPORT_PATH) -> Snapshot:
    data = json.loads(Path(report_path).read_text())
    # Per-run records are keyed by stringified run ids; the report also carries
    # meta blocks (_provenance, _completeness) that must not be fingerprinted
    # into the baseline. Same filter `krg qa` applies.
    runs = {
        str(k): fingerprint_run(v)
        for k, v in data.items()
        if str(k).lstrip("-").isdigit()
    }
    passed = sum(1 for v in runs.values() if v.passed)
    preflight_fails = sum(1 for v in runs.values() if not v.preflight_ok)
    directness_fails = sum(
        1 for v in runs.values() if v.preflight_ok and v.is_direct is False
    )
    legality_fails = sum(
        1 for v in runs.values() if v.preflight_ok and v.legal is False
    )
    fully_ordered = sum(
        1 for v in runs.values()
        if v.ordered_coverage is not None and v.ordered_coverage >= 1.0
    )

    def _mean(attr: str) -> float:
        vals = [getattr(v, attr) for v in runs.values() if getattr(v, attr) is not None]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    return Snapshot(
        total=len(runs),
        passed=passed,
        preflight_fails=preflight_fails,
        directness_fails=directness_fails,
        legality_fails=legality_fails,
        fully_ordered=fully_ordered,
        mean_ordered=_mean("ordered_coverage"),
        mean_strict=_mean("strict_ordered"),
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
    # Runs whose ordered Blue Book coverage dropped, and runs present in the
    # baseline but absent from the new report.
    fidelity_regressions: list[str] = dataclasses.field(default_factory=list)
    disappeared: list[str] = dataclasses.field(default_factory=list)

    @property
    def has_regressions(self) -> bool:
        return bool(
            self.aggregate_regressions
            or self.newly_failing
            or self.fidelity_regressions
            or self.disappeared
        )


def diff(baseline: Snapshot, current: Snapshot) -> Diff:
    aggregate_regressions: list[str] = []
    aggregate_improvements: list[str] = []

    # Rounding noise shouldn't trip the gate; a real per-run drop is a whole
    # street out of a sequence averaging ~15, i.e. ~0.06.
    FIDELITY_EPSILON = 0.01
    # Corpus means move in much smaller increments — one run losing a street
    # shifts the mean by ~0.0002 — so the aggregate threshold is tighter.
    MEAN_EPSILON = 0.001

    def _track(name: str, base: int, curr: int, higher_is_better: bool) -> None:
        if curr == base:
            return
        line = f"{name}: {base} -> {curr}"
        better = curr > base if higher_is_better else curr < base
        (aggregate_improvements if better else aggregate_regressions).append(line)

    def _track_mean(name: str, base: float, curr: float) -> None:
        if abs(curr - base) <= MEAN_EPSILON:
            return
        line = f"{name}: {base:.4f} -> {curr:.4f}"
        (aggregate_improvements if curr > base else aggregate_regressions).append(line)

    _track("passed", baseline.passed, current.passed, higher_is_better=True)
    _track("fully_ordered", baseline.fully_ordered, current.fully_ordered, higher_is_better=True)
    _track("preflight_fails", baseline.preflight_fails, current.preflight_fails, higher_is_better=False)
    _track("directness_fails", baseline.directness_fails, current.directness_fails, higher_is_better=False)
    _track("legality_fails", baseline.legality_fails, current.legality_fails, higher_is_better=False)
    # `total` was never tracked, so a build that dropped runs entirely could
    # report clean as long as the survivors held up.
    _track("total", baseline.total, current.total, higher_is_better=True)
    _track_mean("mean_ordered", baseline.mean_ordered, current.mean_ordered)
    _track_mean("mean_strict", baseline.mean_strict, current.mean_strict)

    newly_failing: list[str] = []
    newly_passing: list[str] = []
    geometry_changed: list[str] = []
    fidelity_regressions: list[str] = []

    def _dropped(base_v: float | None, curr_v: float | None) -> bool:
        return (
            base_v is not None
            and curr_v is not None
            and curr_v < base_v - FIDELITY_EPSILON
        )

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
        # Named per metric: a drop in `strict` alone means the gaps moved
        # earlier in the sequence rather than more streets being missed, which
        # is a different bug from a drop in `ordered`.
        for label, base_v, curr_v in (
            ("ordered", base_fp.ordered_coverage, curr_fp.ordered_coverage),
            ("strict", base_fp.strict_ordered, curr_fp.strict_ordered),
        ):
            if _dropped(base_v, curr_v):
                fidelity_regressions.append(f"{run_id} {label} {base_v} -> {curr_v}")

    # A run in the baseline and missing from the report is the most severe
    # regression there is; the old loop iterated `current` only and so was
    # structurally blind to it.
    disappeared = [rid for rid in baseline.runs if rid not in current.runs]

    def _by_id(s: str):
        head = s.split(" ")[0]
        return int(head) if head.isdigit() else s

    return Diff(
        aggregate_regressions=aggregate_regressions,
        aggregate_improvements=aggregate_improvements,
        newly_failing=sorted(newly_failing, key=_by_id),
        newly_passing=sorted(newly_passing, key=_by_id),
        geometry_changed=sorted(geometry_changed, key=_by_id),
        fidelity_regressions=sorted(fidelity_regressions, key=_by_id),
        disappeared=sorted(disappeared, key=_by_id),
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
    if d.fidelity_regressions:
        lines.append(f"Blue Book fidelity DROPPED ({len(d.fidelity_regressions)}):")
        lines.append("  " + ", ".join(d.fidelity_regressions[:30])
                     + (" ..." if len(d.fidelity_regressions) > 30 else ""))
    if d.disappeared:
        lines.append(f"Runs MISSING from the report ({len(d.disappeared)}):")
        lines.append("  " + ", ".join(d.disappeared[:30])
                     + (" ..." if len(d.disappeared) > 30 else ""))
    if d.geometry_changed:
        # Informational, not a regression: any router change moves geometry, so
        # gating on it would block every improvement. Worth reading, though —
        # geometry moving while every metric holds still means routes were
        # swapped for equally-scoring ones, which is worth a look.
        lines.append(
            f"Geometry changed ({len(d.geometry_changed)} runs) "
            "— informational, does not fail the diff"
        )
    if not lines:
        lines.append("No differences vs baseline.")
    return "\n".join(lines)
