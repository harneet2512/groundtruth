"""RED-first proof for the PERF run-scope aggregate-coverage join defect.

The run-grain PERF bucket for fully computed task metrics used to collapse to
``FAILED:measurement:readiness_role`` instead of ``MEASURED``. The reader joined
run-scope SS-MEASURE readiness from ``run_metrics["ss_features"]``, but the
authoritative ``gt_run_metrics.v2`` artifact carries the run distribution only.

The regression fixture is a complete two-task ``gt_deep_metrics.v2`` population.
It traverses the real aggregator and task-grain adjudicator without depending on
a developer's private run archive.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_run_metrics as run_metrics  # noqa: E402
import ss_live_diagnosis as diagnosis  # noqa: E402

METRICS = (
    "localization_precision",
    "navigation_directness",
    "caller_breakage_count",
    "scope_gap_files",
)


def _complete_deep_row(task: str) -> dict:
    """Build one schema-valid record with all 58 task-grain PERF contracts."""
    record: dict = {
        "task_id": task,
        "schema": "gt_deep_metrics.v2",
        "precision_decimals": 8,
        "resolved": False,
        "performance": {
            "schema": "gt_performance_metrics.v1",
            "metric_applicability": {},
        },
        "behavioral_impact": {},
        "metric_applicability": {},
    }
    for section, definitions in run_metrics._MANDATORY_METRICS.items():
        target = (
            record["behavioral_impact"]
            if section == "behavioral_impact"
            else record["performance"].setdefault(section, {})
        )
        for name, value_type in definitions:
            if value_type == "run_ratio":
                target[name] = None
                target[f"{name}_scope"] = "run_aggregate"
            elif value_type == "bool":
                target[name] = False
            elif value_type == "bool_per_file":
                target[name] = {"src/example.py": True}
            elif value_type == "per_tag_rate_dict":
                target[name] = {}
                record["metric_applicability"].setdefault(section, {})[name] = {
                    "applicable": False,
                    "predicate": "fixture_gt_delivery_present",
                    "reason": "fixture has no GT deliveries",
                }
            else:
                target[name] = 0
    return record


def _load_deep_rows() -> list[dict]:
    return [
        _complete_deep_row("fixture__aggregate-1"),
        _complete_deep_row("fixture__aggregate-2"),
    ]


def _task_measurement() -> dict:
    gates = {
        gate: gate != "aggregate_coverage"
        for gate in diagnosis.TYPED_TERMINAL_GATES["measurement"]
    }
    return {
        "status": "MEASURED",
        "ss_readiness": {"role": "measurement", "gates": gates},
    }


def _load_task_buckets() -> dict[str, dict[str, str]]:
    """Run real task-grain adjudication for the complete fixture population."""
    tasks = [row["task_id"] for row in _load_deep_rows()]
    status = diagnosis._perf_status(_task_measurement())
    assert status == "MEASURED"
    return {
        metric: {task: status for task in tasks}
        for metric in METRICS
    }


def _complete_run_perf() -> dict[str, dict]:
    rows = _load_deep_rows()
    ids = [row["task_id"] for row in rows]
    aggregate = run_metrics.aggregate_run_metrics(rows, expected_task_ids=ids)
    aggregate["run_id"] = "fixture-complete"
    assert aggregate["mandatory_performance_collection_complete"] is True
    assert "ss_features" not in aggregate
    return diagnosis._run_perf_rows(aggregate)


def test_regression_fixture_never_reads_external_run_data(monkeypatch) -> None:
    """The regression proof must execute in CI without a developer's run archive."""
    def reject_external_read(*_args, **_kwargs):
        raise AssertionError("regression fixture attempted an external file read")

    monkeypatch.setattr("builtins.open", reject_external_read)
    run_perf = _complete_run_perf()
    buckets = _load_task_buckets()
    assert set(run_perf) >= set(METRICS)
    assert all(buckets[metric] for metric in METRICS)


def test_task_grain_is_measured_but_legacy_run_join_drops_to_defect() -> None:
    """Task grain is sound, yet the legacy readiness join fails."""
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

        legacy = diagnosis._perf_status(dict(run_row), scope="run")
        assert legacy == "FAILED:measurement:readiness_role"


def test_run_perf_status_binds_measured_from_present_task_grain() -> None:
    run_perf = _complete_run_perf()
    buckets = _load_task_buckets()
    for metric in METRICS:
        assert diagnosis._run_perf_status(
            run_perf[metric], None, buckets[metric],
        ) == "MEASURED"


def test_run_perf_status_fails_closed_on_incomplete_population() -> None:
    run_perf = _complete_run_perf()
    buckets = _load_task_buckets()
    for metric in METRICS:
        run_row = run_perf[metric]
        polluted = {
            **buckets[metric],
            "phantom__task-1": "UNMEASURED:visible_audit_incomplete",
        }
        assert diagnosis._run_perf_status(run_row, None, polluted) == (
            "UNMEASURED:measurement:aggregate_coverage"
        )
        failed = {
            **buckets[metric],
            "phantom__task-2": "FAILED:measurement:precision_8dp",
        }
        assert diagnosis._run_perf_status(run_row, None, failed) == (
            "FAILED:measurement:aggregate_coverage"
        )
        assert diagnosis._run_perf_status(run_row, None, {}) == (
            "UNMEASURED:measurement:aggregate_coverage"
        )


def test_run_perf_status_passes_through_honest_abstentions() -> None:
    rows = _load_deep_rows()
    missing = rows[-1]
    metric_sections = {
        metric: section
        for section, definitions in run_metrics._MANDATORY_METRICS.items()
        for metric, _value_type in definitions
    }
    for metric in METRICS:
        section = metric_sections[metric]
        target = (
            missing["behavioral_impact"]
            if section == "behavioral_impact"
            else missing["performance"][section]
        )
        target[metric] = None
    full = run_metrics.aggregate_run_metrics(rows)
    full["run_id"] = "fixture-incomplete"
    run_perf = diagnosis._run_perf_rows(full)
    buckets = _load_task_buckets()
    for metric in METRICS:
        run_row = run_perf[metric]
        assert run_row["status"] == "UNMEASURED"
        assert diagnosis._run_perf_status(
            run_row, None, buckets[metric],
        ) == "UNMEASURED"


def test_run_perf_status_preserves_explicit_readiness_path() -> None:
    measurement = {"status": "MEASURED", "value_type": "rate"}
    sound = {
        "role": "measurement",
        "live_witness": False,
        "ss_live": False,
        "gates": {
            "artifact_valid": True,
            "metric_structure_valid": True,
            "precision_8dp": True,
            "formula_provenance": True,
            "denominator_provenance": True,
            "applicability_resolved": True,
            "task_coverage": True,
            "aggregate_coverage": True,
        },
    }
    assert diagnosis._run_perf_status(
        measurement, {"ss_readiness": sound}, {"t": "MEASURED"},
    ) == "MEASURED"
    unsound = {
        **sound,
        "gates": {**sound["gates"], "aggregate_coverage": False},
    }
    assert diagnosis._run_perf_status(
        measurement, {"ss_readiness": unsound}, {"t": "MEASURED"},
    ) == "FAILED:measurement:aggregate_coverage"
