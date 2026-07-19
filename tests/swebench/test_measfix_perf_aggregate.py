"""RED-first proof for the PERF run-scope aggregate-coverage join defect.

Defect (run-4 smoke artifacts): the run-grain PERF bucket for metrics whose
per-task values are fully computed (localization_precision, navigation_directness,
caller_breakage_count, scope_gap_files) collapses to a measurement-defect
(``FAILED:measurement:readiness_role``) instead of ``MEASURED``. Root cause: the
reader joined the run-scope SS-MEASURE readiness from ``run_metrics["ss_features"]``,
but the authoritative ``gt_run_metrics.v2`` artifact produced by ``gt_run_metrics.py``
carries the run DISTRIBUTION only — it has no ``ss_features`` and no run
``ss_readiness`` — so the join always misses and never reaches ``aggregate_coverage``,
even though the task grain (and the distribution partition) prove complete coverage.

Fix: ``_run_perf_status`` derives the run verdict from the distribution partition
joined to the reader's own per-task adjudication. The bar is preserved — a MEASURED
run row only binds when the population is complete AND every contributing task row is
itself a sound, resolved measurement.

The real run-4 tree has one genuinely missing task (wireservice__csvkit-1274 has no
usable deep-metric row for these metrics), so the full 36-task aggregate is honestly
UNMEASURED. This test builds the complete 35-task population (via the real
``gt_run_metrics.py`` aggregator over the real deep metrics) where coverage IS
provable, which is exactly where the join defect surfaces.
"""
from __future__ import annotations

import glob
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_run_metrics as run_metrics  # noqa: E402
import ss_live_diagnosis as diagnosis  # noqa: E402

RUN4_ART = Path("D:/gt_runs/run4_smoke_29694879462/art")
METRICS = (
    "localization_precision",
    "navigation_directness",
    "caller_breakage_count",
    "scope_gap_files",
)
# The one task whose deep-metric row is not a usable measurement for these metrics.
_MISSING_TASK = "wireservice__csvkit-1274"


def _require_run4() -> None:
    if not RUN4_ART.is_dir():
        pytest.skip(f"run-4 smoke artifacts unavailable at {RUN4_ART}")


def _load_deep_rows() -> list[dict]:
    rows = []
    for path in sorted(glob.glob(str(RUN4_ART / "**" / "gt_deep_metrics_*.json"), recursive=True)):
        with open(path, encoding="utf-8") as handle:
            rows.append(json.load(handle))
    return rows


def _load_task_buckets() -> dict[str, dict[str, str]]:
    """Reader task-grain buckets for the complete (35-task) population, per metric."""
    buckets: dict[str, dict[str, str]] = {metric: {} for metric in METRICS}
    for path in sorted(glob.glob(str(RUN4_ART / "**" / "gt_feature_metrics_*.json"), recursive=True)):
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        task = record.get("task")
        if task == _MISSING_TASK:
            continue
        ss_features = record.get("ss_features") or {}
        for metric in METRICS:
            buckets[metric][task] = diagnosis._perf_status(ss_features.get(metric))
    return buckets


def _complete_run_perf() -> dict[str, dict]:
    """Real complete-population run distribution rows, keyed by metric name."""
    rows = [row for row in _load_deep_rows() if row.get("task_id") != _MISSING_TASK]
    ids = [row.get("task_id") for row in rows]
    aggregate = run_metrics.aggregate_run_metrics(rows, expected_task_ids=ids)
    aggregate["run_id"] = "run4sub"
    assert aggregate["mandatory_performance_collection_complete"] is True
    assert "ss_features" not in aggregate  # the v2 artifact never carries run readiness
    return diagnosis._run_perf_rows(aggregate)


def test_task_grain_is_measured_but_legacy_run_join_drops_to_defect() -> None:
    """RED root-cause witness: task grain is sound, yet the legacy readiness join fails."""
    _require_run4()
    run_perf = _complete_run_perf()
    buckets = _load_task_buckets()
    for metric in METRICS:
        run_row = run_perf[metric]
        assert run_row["status"] == "MEASURED"
        assert run_row.get("missing_tasks") == []
        assert run_row.get("unmeasured_tasks") == []
        assert run_row.get("failed_tasks") == []

        per_task = Counter(buckets[metric].values())
        assert set(per_task) <= {"MEASURED", "NOT_APPLICABLE", "RIGHT_CENSORED"}
        assert per_task["MEASURED"] > 0

        # The old inline join (no ss_readiness present on the v2 artifact) never reaches
        # aggregate_coverage: it fails closed on readiness_role — the measurement-defect.
        legacy = diagnosis._perf_status(dict(run_row), scope="run")
        assert legacy == "FAILED:measurement:readiness_role"


def test_run_perf_status_binds_measured_from_present_task_grain() -> None:
    """GREEN: the fix reaches aggregate_coverage and binds MEASURED from real evidence."""
    _require_run4()
    run_perf = _complete_run_perf()
    buckets = _load_task_buckets()
    for metric in METRICS:
        assert diagnosis._run_perf_status(run_perf[metric], None, buckets[metric]) == "MEASURED"


def test_run_perf_status_fails_closed_on_incomplete_population() -> None:
    """The bar is not weakened: an unresolved contributing task never promotes."""
    _require_run4()
    run_perf = _complete_run_perf()
    buckets = _load_task_buckets()
    for metric in METRICS:
        run_row = run_perf[metric]
        # An UNMEASURED contributing task row keeps the run out of MEASURED.
        polluted = {**buckets[metric], "phantom__task-1": "UNMEASURED:visible_audit_incomplete"}
        assert diagnosis._run_perf_status(run_row, None, polluted) == (
            "UNMEASURED:measurement:aggregate_coverage"
        )
        # A FAILED contributing task row is a hard failure.
        failed = {**buckets[metric], "phantom__task-2": "FAILED:measurement:precision_8dp"}
        assert diagnosis._run_perf_status(run_row, None, failed) == (
            "FAILED:measurement:aggregate_coverage"
        )
        # No task grain at all cannot prove aggregate coverage.
        assert diagnosis._run_perf_status(run_row, None, {}) == (
            "UNMEASURED:measurement:aggregate_coverage"
        )


def test_run_perf_status_passes_through_honest_abstentions() -> None:
    """A genuinely incomplete run distribution stays an honest abstention, not MEASURED."""
    _require_run4()
    # The FULL 36-task run: csvkit-1274 is genuinely missing → status UNMEASURED.
    rows = _load_deep_rows()
    full = run_metrics.aggregate_run_metrics(rows)
    full["run_id"] = "run4full"
    run_perf = diagnosis._run_perf_rows(full)
    buckets = _load_task_buckets()
    for metric in METRICS:
        run_row = run_perf[metric]
        assert run_row["status"] == "UNMEASURED"
        assert diagnosis._run_perf_status(run_row, None, buckets[metric]) == "UNMEASURED"


def test_run_perf_status_preserves_explicit_readiness_path() -> None:
    """When a run readiness IS attached, adjudicate it verbatim (legacy rollup path)."""
    measurement = {"status": "MEASURED", "value_type": "rate"}
    sound = {
        "role": "measurement", "live_witness": False, "ss_live": False,
        "gates": {
            "artifact_valid": True, "metric_structure_valid": True,
            "precision_8dp": True, "formula_provenance": True,
            "denominator_provenance": True, "applicability_resolved": True,
            "task_coverage": True, "aggregate_coverage": True,
        },
    }
    assert diagnosis._run_perf_status(
        measurement, {"ss_readiness": sound}, {"t": "MEASURED"},
    ) == "MEASURED"
    unsound = {**sound, "gates": {**sound["gates"], "aggregate_coverage": False}}
    assert diagnosis._run_perf_status(
        measurement, {"ss_readiness": unsound}, {"t": "MEASURED"},
    ) == "FAILED:measurement:aggregate_coverage"
