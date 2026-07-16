"""RED-first tests for live_run_provenance — the fail-closed live-witness join.

Doctrine (commit ea0eb16c0): offline evidence never sets the live bit; gates 1-6
passing cannot distinguish a paid trajectory from a REPLAY. The live bit must come
from run-provenance artifacts, and a replay-mintable artifact (the seam receipt) can
never carry it alone.

Every test names its BITING MUTATION: the exact code change that would make it fail.
Fixtures are synthetic (built in-test) so the suite runs anywhere.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.swebench.live_run_provenance import (  # noqa: E402
    LiveRunProvenance,
    detect_live_run,
)


# --------------------------------------------------------------------------- #
# fixture builders — the three provenance artifacts as the live workflow writes them
# --------------------------------------------------------------------------- #
def _valid_receipt() -> dict:
    return {
        "schema": "gt.profile_receipt.v1",
        "gt_rl_profile": "2",
        "members_on": ["GT_GATEWAY", "GT_GLOBAL_ARBITER"],
        "member_source": {"GT_GATEWAY": "resolved"},
        "patched_classes": ["minisweagent.environments.local.LocalEnvironment"],
        "pid": 4242,
        "timestamp_ms": 1,
    }


def _valid_activation() -> dict:
    return {
        "schema": "gt.profile_activation.v1",
        "profile": "2",
        "members": ["GT_GATEWAY", "GT_GLOBAL_ARBITER"],
        "shadow_rate_requested": "",
        "shadow_rate_effective": "",
    }


def _valid_identity(baseline: bool = False) -> dict:
    return {
        "schema": "gt.run_identity.v1",
        "baseline": baseline,
        "workflow_run_id": "29999999999",
        "substrate_digest_actual": "deadbeef",
    }


def _write(task_dir: Path, name: str, data, *, subdir: str = "") -> None:
    base = task_dir / subdir if subdir else task_dir
    base.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        (base / name).write_text(data, encoding="utf-8")
    else:
        (base / name).write_text(json.dumps(data), encoding="utf-8")


def _all_valid(task_dir: Path, *, subdir: str = "") -> None:
    _write(task_dir, "gt_profile_receipt.json", _valid_receipt(), subdir=subdir)
    _write(task_dir, "gt_profile_activation.json", _valid_activation(), subdir=subdir)
    _write(task_dir, "gt_run_identity.json", _valid_identity(), subdir=subdir)


# =========================================================================== #
# happy path
# =========================================================================== #
def test_all_valid_is_live_paid(tmp_path: Path) -> None:
    _all_valid(tmp_path)
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "LIVE_PAID"
    assert prov.receipt_present is True
    assert prov.activation_present is True
    assert prov.baseline_excluded is True
    assert prov.replay_excluded is True
    assert prov.profile_token == "2"
    assert prov.reasons == ()
    # MUTATION: if detect_live_run returned LIVE on receipt alone, the replay-only
    # test below would also flip to LIVE — this pins the full conjunction.


def test_artifacts_in_gt_artifacts_subdir_are_found(tmp_path: Path) -> None:
    # the live Collect step copies /gt_out artifacts into <task>/gt_artifacts/
    _all_valid(tmp_path, subdir="gt_artifacts")
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "LIVE_PAID"
    # MUTATION: dropping "gt_artifacts" from _ARTIFACT_SUBDIRS makes this NOT_LIVE.


# =========================================================================== #
# THE REPLAY HAZARD — the seam receipt is replay-mintable, so it cannot prove live
# =========================================================================== #
def test_receipt_alone_is_not_live(tmp_path: Path) -> None:
    # A replay imports gt_mini_patch -> _write_profile_receipt regenerates THIS file.
    # Without the workflow-only activation artifact the run is NOT citable as live.
    _write(tmp_path, "gt_profile_receipt.json", _valid_receipt())
    _write(tmp_path, "gt_run_identity.json", _valid_identity())
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert prov.receipt_present is True
    assert prov.activation_present is False
    assert "activation_absent" in prov.reasons
    # MUTATION: gating on receipt without requiring activation_present would flip this
    # to LIVE_PAID — exactly the fabrication reverted in ea0eb16c0.


def test_replay_report_marker_forces_not_live(tmp_path: Path) -> None:
    _all_valid(tmp_path)
    _write(tmp_path, "ss_replay_oracle_report.json",
           {"schema": "ss.replay_oracle_report.v2", "gate": "ss_replay_oracle"})
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert prov.replay_excluded is False
    assert "replay_report_present" in prov.reasons
    # MUTATION: skipping _scan_replay_reports would grade a replayed dir as LIVE.


def test_replay_report_detected_by_schema_prefix(tmp_path: Path) -> None:
    _all_valid(tmp_path)
    # a differently-named JSON still betrays a replay via its schema
    _write(tmp_path, "some_other_name.json",
           {"schema": "ss.replay_oracle_report.v9", "x": 1})
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert "replay_report_present" in prov.reasons


# =========================================================================== #
# baseline arm must always be NOT_LIVE
# =========================================================================== #
def test_baseline_marked_identity_is_not_live(tmp_path: Path) -> None:
    _write(tmp_path, "gt_profile_receipt.json", _valid_receipt())
    _write(tmp_path, "gt_profile_activation.json", _valid_activation())
    _write(tmp_path, "gt_run_identity.json", _valid_identity(baseline=True))
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert prov.baseline_excluded is False
    assert "baseline_arm" in prov.reasons
    # MUTATION: ignoring identity["baseline"] would grade the control arm as live.


def test_missing_run_identity_is_not_live(tmp_path: Path) -> None:
    _write(tmp_path, "gt_profile_receipt.json", _valid_receipt())
    _write(tmp_path, "gt_profile_activation.json", _valid_activation())
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert prov.baseline_excluded is False
    assert "run_identity_absent" in prov.reasons
    # MUTATION: defaulting baseline_excluded=True when identity is absent removes the
    # positive non-baseline attestation and fails open.


# =========================================================================== #
# absence / malformed / ambiguous — every path names a reason, none throws
# =========================================================================== #
def test_all_absent_is_not_live(tmp_path: Path) -> None:
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert prov.receipt_present is False
    assert prov.activation_present is False
    assert "receipt_absent" in prov.reasons
    assert "activation_absent" in prov.reasons
    assert "run_identity_absent" in prov.reasons


def test_malformed_receipt_json_is_not_live(tmp_path: Path) -> None:
    _write(tmp_path, "gt_profile_receipt.json", "{not valid json")
    _write(tmp_path, "gt_profile_activation.json", _valid_activation())
    _write(tmp_path, "gt_run_identity.json", _valid_identity())
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert prov.receipt_present is False
    assert "receipt_malformed" in prov.reasons
    # MUTATION: treating a json.JSONDecodeError as present/empty would crash or fail open.


def test_malformed_activation_json_is_not_live(tmp_path: Path) -> None:
    _all_valid(tmp_path)
    _write(tmp_path, "gt_profile_activation.json", "not json at all")
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert prov.activation_present is False
    assert "activation_malformed" in prov.reasons


def test_receipt_with_empty_patched_classes_is_not_live(tmp_path: Path) -> None:
    receipt = _valid_receipt()
    receipt["patched_classes"] = []  # module loaded but no env class attached => GT off
    _write(tmp_path, "gt_profile_receipt.json", receipt)
    _write(tmp_path, "gt_profile_activation.json", _valid_activation())
    _write(tmp_path, "gt_run_identity.json", _valid_identity())
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert prov.receipt_present is False
    assert "receipt_no_patched_classes" in prov.reasons


def test_activation_with_zero_members_is_not_live(tmp_path: Path) -> None:
    activation = _valid_activation()
    activation["members"] = []  # the workflow aborts on 0 members
    _write(tmp_path, "gt_profile_receipt.json", _valid_receipt())
    _write(tmp_path, "gt_profile_activation.json", activation)
    _write(tmp_path, "gt_run_identity.json", _valid_identity())
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert prov.activation_present is False
    assert "activation_no_members" in prov.reasons


def test_wrong_schema_is_rejected(tmp_path: Path) -> None:
    receipt = _valid_receipt()
    receipt["schema"] = "gt.profile_receipt.v2"  # unknown version
    _write(tmp_path, "gt_profile_receipt.json", receipt)
    _write(tmp_path, "gt_profile_activation.json", _valid_activation())
    _write(tmp_path, "gt_run_identity.json", _valid_identity())
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert "receipt_schema_invalid" in prov.reasons


def test_profile_token_conflict_is_not_live(tmp_path: Path) -> None:
    receipt = _valid_receipt()
    receipt["gt_rl_profile"] = "2"
    activation = _valid_activation()
    activation["profile"] = "1"  # two witnesses disagree => forged/mismatched pairing
    _write(tmp_path, "gt_profile_receipt.json", receipt)
    _write(tmp_path, "gt_profile_activation.json", activation)
    _write(tmp_path, "gt_run_identity.json", _valid_identity())
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert "profile_token_conflict" in prov.reasons
    # MUTATION: dropping the cross-check accepts a receipt+activation that never
    # co-occurred in one process.


def test_ambiguous_two_location_copies_is_not_live(tmp_path: Path) -> None:
    _all_valid(tmp_path)  # task_dir copy
    conflicting = _valid_receipt()
    conflicting["pid"] = 9999  # differing content in the gt_artifacts copy
    _write(tmp_path, "gt_profile_receipt.json", conflicting, subdir="gt_artifacts")
    prov = detect_live_run(str(tmp_path))
    assert prov.verdict == "NOT_LIVE"
    assert "receipt_ambiguous" in prov.reasons


# =========================================================================== #
# dataclass contract
# =========================================================================== #
def test_provenance_dataclass_is_frozen(tmp_path: Path) -> None:
    prov = detect_live_run(str(tmp_path))
    assert isinstance(prov, LiveRunProvenance)
    with pytest.raises(dataclasses.FrozenInstanceError):
        prov.verdict = "LIVE_PAID"  # type: ignore[misc]
    assert set(f.name for f in dataclasses.fields(prov)) == {
        "receipt_present", "activation_present", "baseline_excluded",
        "replay_excluded", "profile_token", "verdict", "reasons",
    }


def test_detect_never_mutates_task_dir(tmp_path: Path) -> None:
    _all_valid(tmp_path)
    before = sorted(os.listdir(tmp_path))
    detect_live_run(str(tmp_path))
    detect_live_run(str(tmp_path))
    assert sorted(os.listdir(tmp_path)) == before  # pure: no artifact written


# =========================================================================== #
# HOOKUP — collect_task threads the live bit from provenance into ss_readiness
# =========================================================================== #
def _write_min_task(task_dir: Path, task: str) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({
            "messages": [{"role": "user", "content": "fix the issue"}],
            "info": {"submission": ""},
            "trajectory_format": "mini-swe-agent",
        }),
        encoding="utf-8",
    )
    (task_dir / f"gt_runtime_ledger_{task}.jsonl").write_text(
        json.dumps({
            "layer": "fixture", "event_type": "fixture", "file_path": "",
            "outcome": "shadow_holdout", "reason": "fixture",
            "chars_delivered": 0, "iteration": 0,
        }) + "\n",
        encoding="utf-8",
    )


def _import_metrics():
    scripts = str(_ROOT / "scripts" / "swebench")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import gt_feature_metrics  # noqa: E402
    return gt_feature_metrics


def _live_witness_flags(record: dict) -> list[bool]:
    return [
        feat["ss_readiness"]["live_witness"]
        for feat in record["ss_features"].values()
        if isinstance(feat.get("ss_readiness"), dict)
        and "live_witness" in feat["ss_readiness"]
    ]


def test_collect_task_sets_live_witness_from_provenance(tmp_path: Path) -> None:
    metrics = _import_metrics()
    task = "synthetic__live"
    _write_min_task(tmp_path, task)
    _all_valid(tmp_path)  # valid receipt + activation + non-baseline identity
    record = metrics.collect_task(task, str(tmp_path), profile="2")

    prov = record["ss_integrity"]["live_run_provenance"]
    assert prov["verdict"] == "LIVE_PAID"
    assert prov["activation_present"] is True and prov["receipt_present"] is True
    # at least one threaded readiness site now carries the live bit
    assert any(_live_witness_flags(record))
    # MUTATION: passing live_witness=False at the collect_task call sites (the reverted
    # state) makes every flag False and this assertion fails.


def test_collect_task_live_witness_false_without_provenance(tmp_path: Path) -> None:
    metrics = _import_metrics()
    task = "synthetic__offline"
    _write_min_task(tmp_path, task)  # NO provenance artifacts
    record = metrics.collect_task(task, str(tmp_path), profile="2")

    prov = record["ss_integrity"]["live_run_provenance"]
    assert prov["verdict"] == "NOT_LIVE"
    assert not any(_live_witness_flags(record))  # fail-closed default preserved
