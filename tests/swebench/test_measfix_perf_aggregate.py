"""Real-artifact compatibility checks for the PERF readiness/value join.

The frozen run predates the current readiness schema, so these tests recompute both
the canonical run distribution and the independent feature readiness from its real
deep-metric inputs. Schema drift is exercised, never hidden behind a skip.
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_feature_metrics as feature_metrics  # noqa: E402
import gt_run_metrics as run_metrics  # noqa: E402
import ss_live_diagnosis as diagnosis  # noqa: E402

RUN4_ART = Path("D:/gt_runs/run4_smoke_29694879462/art")
METRICS = (
    "localization_precision",
    "navigation_directness",
    "caller_breakage_count",
    "scope_gap_files",
)
_MISSING_TASK = "wireservice__csvkit-1274"


def _require_run4() -> None:
    if not RUN4_ART.is_dir():
        pytest.skip(f"run-4 smoke artifacts unavailable at {RUN4_ART}")


def _deep_paths() -> list[Path]:
    return [
        Path(path) for path in sorted(glob.glob(
            str(RUN4_ART / "**" / "gt_deep_metrics_*.json"), recursive=True,
        ))
    ]


def _load_deep_rows() -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in _deep_paths()]


def _current_task_rows() -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {metric: [] for metric in METRICS}
    for path in _deep_paths():
        task = json.loads(path.read_text(encoding="utf-8")).get("task_id")
        if task == _MISSING_TASK:
            continue
        records, _missing, _source = feature_metrics._performance_feature_records(
            task, str(path.parent),
        )
        for metric in METRICS:
            result[metric].append({**records[metric], "_task": task})
    return result


def _current_run_rows(tmp_path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    rows = [row for row in _load_deep_rows() if row.get("task_id") != _MISSING_TASK]
    ids = [row["task_id"] for row in rows]
    aggregate = run_metrics.aggregate_run_metrics(rows, expected_task_ids=ids)
    aggregate["run_id"] = "run4sub"
    assert aggregate["mandatory_performance_collection_complete"] is True
    artifact = tmp_path / "gt_run_metrics_v2_run4sub.json"
    artifact.write_text(json.dumps(aggregate), encoding="utf-8")

    definitions = {
        name: (section, value_type)
        for section, definitions in feature_metrics.performance_metric_definitions().items()
        for name, value_type in definitions
    }
    task_rows = _current_task_rows()
    readiness_rows: dict[str, dict] = {}
    for name in METRICS:
        section, value_type = definitions[name]
        measurement = feature_metrics._run_distribution_feature_record(
            "run4sub", str(artifact), task_rows[name], section=section, name=name,
            value_type=value_type, expected_tasks=len(ids),
        )
        readiness_rows[name] = {
            "ss_readiness": feature_metrics._measurement_only_readiness(
                measurement,
                aggregate_coverage=measurement["aggregate_coverage_valid"],
                cross_process_parity=measurement["cross_process_parity_valid"],
                live_witness=False,
            ),
        }
    return diagnosis._run_perf_rows(aggregate), readiness_rows


def test_real_artifacts_recompute_exact_value_parity(tmp_path: Path) -> None:
    _require_run4()
    run_perf, readiness_rows = _current_run_rows(tmp_path)
    for metric in METRICS:
        assert run_perf[metric]["status"] == "MEASURED"
        gates = readiness_rows[metric]["ss_readiness"]["gates"]
        assert gates["cross_process_parity"] is True
        assert gates["aggregate_coverage"] is True
        assert diagnosis._run_perf_status(
            run_perf[metric], readiness_rows[metric], {},
        ) == "MEASURED"


def test_distribution_without_independent_readiness_cannot_promote(tmp_path: Path) -> None:
    _require_run4()
    run_perf, _readiness_rows = _current_run_rows(tmp_path)
    for metric in METRICS:
        assert diagnosis._run_perf_status(run_perf[metric], None, {}) == (
            "UNMEASURED:measurement:run_readiness"
        )


def test_run_perf_status_passes_through_honest_abstentions() -> None:
    _require_run4()
    rows = _load_deep_rows()
    ids = [row["task_id"] for row in rows]
    run_perf = diagnosis._run_perf_rows(
        run_metrics.aggregate_run_metrics(rows, expected_task_ids=ids)
    )
    for metric in METRICS:
        assert run_perf[metric]["status"] == "UNMEASURED"
        assert diagnosis._run_perf_status(run_perf[metric], None, {}) == "UNMEASURED"


def test_run_perf_status_preserves_explicit_readiness_path() -> None:
    measurement = {"status": "MEASURED", "value_type": "rate"}
    sound = {
        "role": "measurement", "live_witness": False, "ss_live": False,
        "gates": {
            "artifact_valid": True, "metric_structure_valid": True,
            "precision_8dp": True, "formula_provenance": True,
            "denominator_provenance": True, "applicability_resolved": True,
            "task_coverage": True, "cross_process_parity": True,
            "aggregate_coverage": True,
        },
    }
    assert diagnosis._run_perf_status(
        measurement, {"ss_readiness": sound}, {},
    ) == "MEASURED"
    unsound = {**sound, "gates": {**sound["gates"], "aggregate_coverage": False}}
    assert diagnosis._run_perf_status(
        measurement, {"ss_readiness": unsound}, {},
    ) == "FAILED:measurement:aggregate_coverage"
