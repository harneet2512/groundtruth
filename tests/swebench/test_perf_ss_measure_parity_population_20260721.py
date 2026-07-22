"""RED-first proof for the PERF SS-MEASURE structural blocker (CODEX_129_BUGLIST).

Two shared structural gaps made all 58 PERF rows unable to meet the SS-MEASURE bar:

  1. SCHEMA OMITS CROSS-PROCESS PARITY. The measurement gate schema
     (``_MEASUREMENT_GATE_NAMES`` / ``TYPED_TERMINAL_GATES["measurement"]``) carried
     eight gates and NO parity terminal, so a per-task value produced inside the
     container (``gt_deep_metrics``) could launder to ``MEASURED`` on the host with no
     independent second-process agreement. RED: a readiness carrying only the legacy
     eight all-True gates passes ``_perf_status(scope="run")`` as ``MEASURED``.

  2. POPULATION OPTIONAL. ``aggregate_run_metrics`` only ran missing-task detection when
     an expected population was supplied, so an ARBITRARY SUBSET was marked
     ``mandatory_performance_collection_complete``. RED: an undeclared population is
     accepted as complete.

FIX (schema-level, covering all 58 PERF rows):
  * add ``cross_process_parity`` to the SS-MEASURE gate schema (writer + reader), a
    run-scope gate that binds True only when the value/partition an INDEPENDENT process
    (``gt_run_metrics.v2``) recorded is byte/value-identical to the reader's own per-task
    aggregation; adjudicated only at ``scope="run"`` (single-process task grain carries it
    False by construction), fail-closed with honest N/A.
  * require the complete single-run population in ``aggregate_run_metrics``: an undeclared
    population is itself a collection failure (``expected_population_unknown``).

This test asserts the FIXED behavior; run it against the pre-fix tree to see it RED.
"""
from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT, ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import gt_feature_metrics as metrics  # noqa: E402
import gt_run_metrics as run_metrics  # noqa: E402
import ss_live_diagnosis as diagnosis  # noqa: E402

_LEGACY_EIGHT_GATES = (
    "artifact_valid", "metric_structure_valid", "precision_8dp",
    "formula_provenance", "denominator_provenance",
    "applicability_resolved", "task_coverage", "aggregate_coverage",
)


def _row(task: str, resolved: bool, cost: float) -> dict:
    return {
        "task_id": task,
        "resolved": resolved,
        "performance": {"token_efficiency": {"total_cost_usd": cost}},
    }


def _readiness(*, parity, aggregate=True) -> dict:
    """A canonical measurement readiness built from the CURRENT schema (order-exact)."""
    gates = {name: True for name in metrics._MEASUREMENT_GATE_NAMES}
    gates["cross_process_parity"] = parity
    gates["aggregate_coverage"] = aggregate
    return {
        "role": "measurement", "live_witness": False, "ss_live": False, "gates": gates,
    }


def _population() -> dict:
    return {
        "expected_count": 1,
        "observed_record_count": 1,
        "observed_unique_count": 1,
        "missing_tasks": [],
        "duplicate_tasks": [],
        "unexpected_tasks": [],
        "invalid_task_records": [],
    }


def _run_payload(metric: dict, *, section: str, name: str) -> dict:
    return {
        "schema": "gt_run_metrics.v2",
        "run_id": "run",
        "precision_decimals": 8,
        "mandatory_performance_metric_count": 58,
        "mandatory_performance_collection_complete": True,
        "tasks": 1,
        "resolved": 1,
        "task_population": _population(),
        "invalid_deep_metric_records": {},
        "mandatory_performance": {section: {name: metric}},
    }


def _value_sha256(value: dict) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Gap 1 — SS-MEASURE schema now carries an enforceable cross-process parity gate
# --------------------------------------------------------------------------- #
def test_schema_carries_cross_process_parity_and_reader_writer_agree() -> None:
    assert "cross_process_parity" in metrics._MEASUREMENT_GATE_NAMES
    # reader (ss_live_diagnosis) and writer (gt_feature_metrics) schemas stay 1:1.
    assert diagnosis.TYPED_TERMINAL_GATES["measurement"] == metrics._MEASUREMENT_GATE_NAMES
    # parity is grouped with the other run-scope gate, immediately before aggregate_coverage.
    names = metrics._MEASUREMENT_GATE_NAMES
    assert names[-2:] == ("cross_process_parity", "aggregate_coverage")


def test_legacy_parityless_measurement_no_longer_launders_to_measured() -> None:
    """RED pre-fix: the eight-gate object returned ``MEASURED`` with no parity proof."""
    legacy = {
        "role": "measurement", "live_witness": False, "ss_live": False,
        "gates": {name: True for name in _LEGACY_EIGHT_GATES},
    }
    value = {"status": "MEASURED", "ss_readiness": legacy}
    # A parity-less measurement can no longer be laundered into MEASURED: its schema no
    # longer matches the required terminal.
    assert diagnosis._perf_status(value, scope="run") == "FAILED:measurement:gate_schema"


def test_cross_process_parity_is_enforced_run_scope_and_honest_na_task_scope() -> None:
    proven = {"status": "MEASURED", "ss_readiness": _readiness(parity=True)}
    assert diagnosis._perf_status(proven, scope="run") == "MEASURED"

    # Absent parity evidence is honest N/A (UNMEASURED), never a manufactured pass.
    unproven = {"status": "MEASURED", "ss_readiness": _readiness(parity=None)}
    assert diagnosis._perf_status(unproven, scope="run") == (
        "UNMEASURED:measurement:cross_process_parity"
    )

    # A real cross-process DISAGREEMENT fails closed, named distinctly (not buried under
    # aggregate_coverage): parity is adjudicated before it.
    disagree = {
        "status": "MEASURED",
        "ss_readiness": _readiness(parity=False, aggregate=False),
    }
    assert diagnosis._perf_status(disagree, scope="run") == (
        "FAILED:measurement:cross_process_parity"
    )

    # RUN-SCOPE ONLY: at task grain there is a single process view, so parity is not
    # adjudicated — a task row with parity False is still MEASURED at task scope.
    task_grain = {"status": "MEASURED", "ss_readiness": _readiness(parity=False)}
    assert diagnosis._perf_status(task_grain, scope="task") == "MEASURED"


def test_writer_threads_parity_into_ss_live_and_blockers() -> None:
    record = {
        "status": "MEASURED", "coverage_scope": "run", "source": "gt_run_metrics",
        "source_artifact": "gt_run_metrics_run.json",
        "artifact_schema_valid": True, "metric_structure_valid": True,
        "precision_decimals": 8, "value_precision_valid": True,
        "formula_provenance": "MANDATORY_METRICS.md#x",
        "denominator_provenance": "gt_run_metrics", "task_coverage_valid": True,
    }
    live = metrics._measurement_only_readiness(
        record, aggregate_coverage=True, cross_process_parity=True, live_witness=True,
    )
    assert live["gates"]["cross_process_parity"] is True
    assert live["ss_live"] is True
    assert live["blockers"] == []

    # Without parity evidence the writer does NOT fabricate it: ss_live stays False and the
    # gate is named as a blocker.
    no_parity = metrics._measurement_only_readiness(
        record, aggregate_coverage=True, cross_process_parity=False, live_witness=True,
    )
    assert no_parity["gates"]["cross_process_parity"] is False
    assert no_parity["ss_live"] is False
    assert "cross_process_parity" in no_parity["blockers"]


def test_distribution_parity_compares_exact_recomputed_values_not_only_partitions(
    tmp_path: Path,
) -> None:
    """RED on the first patch: local value 999 and host mean 1 shared the same
    MEASURED partition, so the alleged parity gate incorrectly returned True."""
    metric = {
        "status": "MEASURED",
        "value_type": "nonnegative_int",
        "aggregation": "per_task_distribution",
        "mean": 1.0,
        "median": 1.0,
        "measured_tasks": 1,
        "missing_tasks": [],
        "unmeasured_tasks": [],
        "failed_tasks": [],
        "not_applicable_tasks": [],
        "event_observed_tasks": ["task"],
        "right_censored_tasks": [],
        "applicability": {},
        # Independent gt_run_metrics-process commitment to its exact normalized values.
        "value_sha256": _value_sha256({
            "kind": "task_distribution",
            "section": "localization",
            "metric": "gold_rank",
            "value_type": "nonnegative_int",
            "tasks": [{"task_id": "task", "state": "measured", "value": 1.0}],
        }),
    }
    artifact = tmp_path / "gt_run_metrics_v2_run.json"
    artifact.write_text(
        json.dumps(_run_payload(metric, section="localization", name="gold_rank")),
        encoding="utf-8",
    )
    record = metrics._run_distribution_feature_record(
        "run",
        str(artifact),
        [{
            "_task": "task", "status": "MEASURED", "value": 999,
            "_parity_state": "measured", "_parity_value": 999.0,
        }],
        section="localization",
        name="gold_rank",
        value_type="nonnegative_int",
        expected_tasks=1,
    )
    assert record["status"] == "UNMEASURED"
    assert record["cross_process_parity_valid"] is False


def test_run_ratio_parity_recomputes_from_task_inputs_not_duplicate_host_fields(
    tmp_path: Path,
) -> None:
    """RED on the first patch: corrupting both duplicate host fields to 999 passed.
    The second process must independently derive 2/1 == 2 from task inputs."""
    commitment = {
        "kind": "run_ratio",
        "section": "token_efficiency",
        "metric": "cost_per_resolved",
        "value_type": "run_ratio",
        "task_count": 1,
        "resolved_count": 1,
        "total_cost_usd": 999.0,
        "status": "MEASURED",
        "value": 999.0,
    }
    ratio = {
        "status": "MEASURED",
        "value": 999.0,
        "value_type": "run_ratio",
        "aggregation": "ratio_of_run_total_cost_to_resolved_count",
        "mean": None,
        "median": None,
        "measured_tasks": 1,
        "missing_tasks": [],
        "applicability": {
            "applicable": True, "predicate": "p", "reason": "r",
        },
        "value_sha256": _value_sha256(commitment),
    }
    payload = _run_payload(
        ratio, section="token_efficiency", name="cost_per_resolved",
    )
    payload["token_efficiency"] = {
        "total_cost_usd": 999.0,
        "cost_per_resolved": 999.0,
        "cost_collection_complete": True,
    }
    artifact = tmp_path / "gt_run_metrics_v2_run.json"
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    record = metrics._run_ratio_feature_record(
        "run",
        str(artifact),
        [{
            "_task": "task",
            "_parity_ratio_input": {"resolved": True, "total_cost_usd": 2.0},
        }],
        section="token_efficiency",
        name="cost_per_resolved",
        value_type="run_ratio",
        expected_tasks=1,
    )
    assert record["status"] == "UNMEASURED"
    assert record["cross_process_parity_valid"] is False


def test_live_workflow_passes_feature_readiness_artifact_to_diagnosis() -> None:
    workflow = (ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml").read_text(
        encoding="utf-8",
    )
    expected = (
        '--feature-run-metrics '
        '"$GT_DIAG_DIR/gt_run_metrics_${GT_RUN_ID}.json"'
    )
    assert workflow.count(expected) == 2


# --------------------------------------------------------------------------- #
# Gap 2 — complete single-run population is REQUIRED (fail-closed)
# --------------------------------------------------------------------------- #
def test_undeclared_population_is_not_collection_complete() -> None:
    """RED pre-fix: an arbitrary subset with no declared population was marked complete."""
    result = run_metrics.aggregate_run_metrics([_row("a", True, 1.0)])
    assert result["mandatory_performance_collection_complete"] is False
    assert "expected_population_unknown" in result[
        "mandatory_performance_collection_failures"
    ]


def test_declared_full_population_clears_the_population_gate() -> None:
    """No false negative from the population gate: a declared, fully/uniquely covered
    population raises NONE of the population failures (any remaining failure — e.g. missing
    per-task metric data — is a different, pre-existing gate, not the population gate)."""
    result = run_metrics.aggregate_run_metrics(
        [_row("a", True, 1.0), _row("b", False, 1.0)],
        expected_task_ids=["a", "b"],
    )
    failures = set(result["mandatory_performance_collection_failures"])
    assert failures.isdisjoint({
        "expected_population_unknown", "missing_expected_tasks",
        "unexpected_tasks", "duplicate_task_records", "duplicate_expected_tasks",
    })
    assert result["task_population"]["expected_count"] == 2
    assert result["task_population"]["missing_tasks"] == []


def test_declared_partial_population_fails_closed_on_missing_tasks() -> None:
    result = run_metrics.aggregate_run_metrics(
        [_row("a", True, 1.0), _row("b", True, 1.0)],
        expected_task_ids=["a", "b", "c"],
    )
    assert result["mandatory_performance_collection_complete"] is False
    failures = result["mandatory_performance_collection_failures"]
    assert "missing_expected_tasks" in failures
    assert "expected_population_unknown" not in failures
