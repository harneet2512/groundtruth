"""D5: caller_breakage_count / p2p_regression_rate nullable-verifier-metric N/A contract.

Defect D5 (smoke30_ss128_20260716, geopandas__geopandas-3471): the standalone PERF
validator rejected the task's record with
``interface_preservation.caller_breakage_count:unmeasured``.  The producer emitted a
bare ``null`` for a metric that is legitimately not-applicable — the caller-aware
verifier join was absent for that task (``caller_join_complete: false``) — but dropped
the verifier's OWN explicit N/A declaration, so the null had no applicability contract
and validated as an unmeasured collection failure.

Fix: the producer propagates the verifier's objective denominators
(``valid`` / ``p2p_total`` / ``caller_join_complete``) and emits the canonical
explicit-N/A applicability contract that ``gt_run_metrics`` already accepts elsewhere.
Applicability keys on those objective signals, never on the null value, so a value that
should exist but is missing still fails closed as UNMEASURED.

RED-first: the real geopandas artifact fails pre-fix; the producer-rebuilt record passes;
several tamper cases prove a genuinely-missing/malformed value still fails validation.
The rule holds uniformly for both nullable verifier-derived siblings in the section.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gt_performance_metrics as performance  # noqa: E402
import gt_run_metrics as run_metrics  # noqa: E402


# The real failing artifact from smoke30_ss128_20260716 (LOCAL, gitignored). The
# hermetic cases below fully cover the contract; this binds the fix to the real bytes
# when the run directory is present.
_REAL_TASK_DIR = Path(
    "D:/gt_runs/smoke30_ss128_20260716/ll-full-geopandas__geopandas-3471"
)
_REAL_RECORD = _REAL_TASK_DIR / "gt_performance_metrics_geopandas__geopandas-3471.json"
_REAL_TRUTH = _REAL_TASK_DIR / "task_truth.json"


def _trajectory(tmp_path: Path) -> Path:
    path = tmp_path / "mini-swe-agent.trajectory.json"
    path.write_text(
        json.dumps({"messages": [], "info": {"model_stats": {}, "submission": ""}}),
        encoding="utf-8",
    )
    return path


def _produce(tmp_path: Path, verifier_truth: dict | None) -> dict:
    """Run the real producer path and return the standalone PERF record."""
    return performance.compute_performance_metrics(
        str(_trajectory(tmp_path)),
        str(tmp_path),
        gold_files=["src/x.py"],
        consumption_ledger={
            "schema": "gt.consumption_ledger.v2",
            "runtime_ledger_path": "ledger",
            "entries": [],
        },
        verifier_truth=verifier_truth,
    )


# Mirrors geopandas: valid verifier, PASS_TO_PASS denominator present, caller-aware
# join ABSENT -> caller_breakage_count legitimately null.
_GEOPANDAS_TRUTH = {
    "verifier_truth": {
        "schema": "gt.verifier_truth.v1",
        "authority": "official_swebench_report.tests_status.PASS_TO_PASS",
        "source_present": True,
        "valid": True,
        "p2p_total": 2227,
        "p2p_failed": 1,
        "caller_breakage_count": None,
        "caller_breakage_unmeasured_reason": "caller_aware_verifier_join_absent",
        "caller_joined_failures": 0,
        "caller_join_complete": False,
    }
}
# Mirrors beancount: caller-aware join COMPLETE -> a real measured count (0).
_BEANCOUNT_TRUTH = {
    "verifier_truth": {
        "schema": "gt.verifier_truth.v1",
        "valid": True,
        "p2p_total": 1076,
        "p2p_failed": 0,
        "caller_breakage_count": 0,
        "caller_joined_failures": 0,
        "caller_join_complete": True,
    }
}


def _caller_issues(record: dict) -> list[str]:
    return [
        i for i in run_metrics.validate_task_performance_record(record)
        if "caller_breakage_count" in i
    ]


def _p2p_issues(record: dict) -> list[str]:
    return [
        i for i in run_metrics.validate_task_performance_record(record)
        if "p2p_regression_rate" in i
    ]


# --------------------------------------------------------------------------- #
# RED-first: the pre-fix shape (null value, no applicability contract) fails.   #
# --------------------------------------------------------------------------- #

def test_red_pre_fix_bare_null_without_contract_is_unmeasured(tmp_path: Path) -> None:
    """A null caller_breakage_count WITHOUT the applicability contract fails (the D5 bug)."""
    record = _produce(tmp_path, _GEOPANDAS_TRUTH)
    assert record["interface_preservation"]["caller_breakage_count"] is None
    # Strip the producer's N/A contract to reconstruct the exact pre-fix artifact.
    pre_fix = copy.deepcopy(record)
    pre_fix["metric_applicability"]["interface_preservation"].pop(
        "caller_breakage_count", None
    )
    assert _caller_issues(pre_fix) == [
        "interface_preservation.caller_breakage_count:unmeasured"
    ]


def test_green_post_fix_producer_emits_canonical_na_contract(tmp_path: Path) -> None:
    """Post-fix: the producer emits the canonical explicit-N/A contract and the record passes."""
    record = _produce(tmp_path, _GEOPANDAS_TRUTH)
    contract = record["metric_applicability"]["interface_preservation"][
        "caller_breakage_count"
    ]
    assert contract == {
        "applicable": False,
        "predicate": "caller_aware_verifier_join_complete",
        "reason": "caller_aware_verifier_join_absent",
    }
    assert _caller_issues(record) == []


def test_measured_caller_breakage_still_validates(tmp_path: Path) -> None:
    """When the join completes, the count is a real measured int and stays applicable=True."""
    record = _produce(tmp_path, _BEANCOUNT_TRUTH)
    assert record["interface_preservation"]["caller_breakage_count"] == 0
    contract = record["metric_applicability"]["interface_preservation"][
        "caller_breakage_count"
    ]
    assert contract["applicable"] is True
    assert _caller_issues(record) == []


# --------------------------------------------------------------------------- #
# Mutations: a genuinely-missing / malformed value must STILL fail validation.  #
# --------------------------------------------------------------------------- #

def test_mutation_join_complete_but_null_fails_closed(tmp_path: Path) -> None:
    """Verifier claims the join completed but supplies no count -> collection failure.

    applicable=True (denominator present) + null value -> UNMEASURED, never laundered N/A.
    """
    truth = copy.deepcopy(_GEOPANDAS_TRUTH)
    truth["verifier_truth"]["caller_join_complete"] = True
    truth["verifier_truth"].pop("caller_breakage_unmeasured_reason", None)
    record = _produce(tmp_path, truth)  # count stays null
    assert record["interface_preservation"]["caller_breakage_count"] is None
    assert record["metric_applicability"]["interface_preservation"][
        "caller_breakage_count"
    ]["applicable"] is True
    assert _caller_issues(record) == [
        "interface_preservation.caller_breakage_count:unmeasured"
    ]


def test_mutation_join_absent_but_value_present_fails_closed(tmp_path: Path) -> None:
    """Contradiction: join declared absent yet a count materializes -> FAILED, not N/A."""
    record = _produce(tmp_path, _GEOPANDAS_TRUTH)
    tampered = copy.deepcopy(record)
    tampered["interface_preservation"]["caller_breakage_count"] = 5
    assert tampered["metric_applicability"]["interface_preservation"][
        "caller_breakage_count"
    ]["applicable"] is False
    assert _caller_issues(tampered) == [
        "interface_preservation.caller_breakage_count:failed"
    ]


def test_mutation_absent_verifier_truth_stays_unmeasured(tmp_path: Path) -> None:
    """No verifier truth at all -> NO N/A claim; both siblings stay UNMEASURED (fail-closed)."""
    record = _produce(tmp_path, None)
    iface_contracts = record["metric_applicability"].get("interface_preservation", {})
    assert "caller_breakage_count" not in iface_contracts
    assert "p2p_regression_rate" not in iface_contracts
    assert _caller_issues(record) == [
        "interface_preservation.caller_breakage_count:unmeasured"
    ]
    assert _p2p_issues(record) == [
        "interface_preservation.p2p_regression_rate:unmeasured"
    ]


def test_mutation_malformed_na_contract_reason_fails_closed(tmp_path: Path) -> None:
    """A malformed N/A contract (blank reason) cannot sneak a null through -> FAILED."""
    record = _produce(tmp_path, _GEOPANDAS_TRUTH)
    tampered = copy.deepcopy(record)
    tampered["metric_applicability"]["interface_preservation"][
        "caller_breakage_count"
    ]["reason"] = "   "
    assert _caller_issues(tampered) == [
        "interface_preservation.caller_breakage_count:failed"
    ]


# --------------------------------------------------------------------------- #
# Sibling uniformity: p2p_regression_rate shares the same reducer/verifier class.#
# --------------------------------------------------------------------------- #

def test_p2p_sibling_no_denominator_is_na_but_missing_value_fails_closed(
    tmp_path: Path,
) -> None:
    """p2p_regression_rate: p2p_total==0 -> legit N/A; denominator present + null -> UNMEASURED."""
    # p2p_total == 0: no PASS_TO_PASS denominator -> legitimate N/A, passes.
    na_truth = {
        "verifier_truth": {
            "schema": "gt.verifier_truth.v1", "valid": True,
            "p2p_total": 0, "p2p_failed": 0,
            "caller_join_complete": False,
            "caller_breakage_unmeasured_reason": "caller_aware_verifier_join_absent",
        }
    }
    na_record = _produce(tmp_path, na_truth)
    assert na_record["interface_preservation"]["p2p_regression_rate"] is None
    assert na_record["metric_applicability"]["interface_preservation"][
        "p2p_regression_rate"
    ]["applicable"] is False
    assert _p2p_issues(na_record) == []

    # denominator present (p2p_total>0) but value forced null -> fail closed.
    record = _produce(tmp_path, _GEOPANDAS_TRUTH)
    tampered = copy.deepcopy(record)
    tampered["interface_preservation"]["p2p_regression_rate"] = None
    assert tampered["metric_applicability"]["interface_preservation"][
        "p2p_regression_rate"
    ]["applicable"] is True
    assert _p2p_issues(tampered) == [
        "interface_preservation.p2p_regression_rate:unmeasured"
    ]


# --------------------------------------------------------------------------- #
# Bind the fix to the REAL failing artifact when the run directory is present.  #
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(
    not (_REAL_RECORD.exists() and _REAL_TRUTH.exists()),
    reason="real smoke30 geopandas artifact not present (local-only)",
)
def test_real_geopandas_artifact_red_then_green() -> None:
    record = json.loads(_REAL_RECORD.read_text(encoding="utf-8"))
    truth = json.loads(_REAL_TRUTH.read_text(encoding="utf-8"))

    # RED: the on-disk (pre-fix) artifact fails on exactly caller_breakage_count.
    assert _caller_issues(record) == [
        "interface_preservation.caller_breakage_count:unmeasured"
    ]

    # GREEN: applying the producer fix (markers + rebuilt applicability) clears it,
    # leaving p2p (already measured on this task) untouched and the record clean.
    record["interface_preservation"].update(
        performance.verifier_interface_denominators(truth)
    )
    record["metric_applicability"] = performance.build_metric_applicability(record)
    assert run_metrics.validate_task_performance_record(record) == []
