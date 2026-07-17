"""RED-first tests for the W1b live-bit ORDERING contract (false-dark, twice-witnessed).

DEFECT: the SEALED per-task ``gt_feature_metrics_<task>.json`` records
``ss_integrity.live_run_provenance.verdict == NOT_LIVE`` even though all three
run-provenance identity artifacts (``gt_profile_activation.json``,
``gt_profile_receipt.json``, ``gt_run_identity.json``) were produced by the run.
The in-container collect evaluates provenance on the per-task dir (``/tmp/gt/<task>``)
BEFORE those artifacts are co-located there — the workflow writes them to the run's
SHARED agent-output dir (``/gt_out``) and copies them only into ``trial_results/
gt_artifacts`` AFTERWARD. ``detect_live_run`` itself is correct; the ORDER is wrong.
Since D9 the sealed record is authoritative and never rewritten, so the false-dark
bit is permanently sealed on every task.

FIX under test: ``gt_feature_metrics.stage_provenance_artifacts`` co-locates the three
identity artifacts from the shared dir into ``<task_dir>/gt_artifacts`` BEFORE
``detect_live_run`` runs — fail-closed, non-fabricating, create-pass only (never the
``--out`` re-grade, preserving the D9 completion-hash binding).

Every test names its BITING MUTATION. Fixtures are synthetic (built in-test).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench"),
           str(_ROOT / "scripts" / "metrics"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _import_metrics():
    import gt_feature_metrics  # noqa: E402
    return gt_feature_metrics


# --------------------------------------------------------------------------- #
# fixtures — the three provenance artifacts as the live workflow writes them,
# plus the minimal task inputs collect_task needs to build a record.
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


def _write_json(base: Path, name: str, data) -> None:
    base.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        (base / name).write_text(data, encoding="utf-8")
    else:
        (base / name).write_text(json.dumps(data), encoding="utf-8")


def _shared_dir_all_valid(shared: Path, *, baseline: bool = False) -> None:
    """Emulate /gt_out holding the run-provenance artifacts (NOT co-located)."""
    _write_json(shared, "gt_profile_receipt.json", _valid_receipt())
    _write_json(shared, "gt_profile_activation.json", _valid_activation())
    _write_json(shared, "gt_run_identity.json", _valid_identity(baseline=baseline))


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


def _verdict(task_dir: Path, task: str) -> str:
    m = _import_metrics()
    rec = m.collect_task(task, str(task_dir), profile="2")
    return rec["ss_integrity"]["live_run_provenance"]["verdict"]


# =========================================================================== #
# 1. the ORDERING DEFECT, reproduced: artifacts in the shared dir but not
#    co-located => the sealed record is false-dark NOT_LIVE.
# =========================================================================== #
def test_defect_reproduced_shared_dir_not_colocated_is_false_dark(tmp_path: Path) -> None:
    task = "synthetic__falsedark"
    task_dir = tmp_path / "run" / task
    shared = tmp_path / "gt_out"
    _write_min_task(task_dir, task)
    _shared_dir_all_valid(shared)  # produced, but NOT next to the task record
    assert _verdict(task_dir, task) == "NOT_LIVE"  # the twice-witnessed false-dark


# =========================================================================== #
# 2. THE FIX: staging co-locates the artifacts => the sealed record is LIVE_PAID.
# =========================================================================== #
def test_staging_makes_sealed_record_live(tmp_path: Path) -> None:
    m = _import_metrics()
    task = "synthetic__live"
    task_dir = tmp_path / "run" / task
    shared = tmp_path / "gt_out"
    _write_min_task(task_dir, task)
    _shared_dir_all_valid(shared)

    disp = m.stage_provenance_artifacts(str(task_dir), str(shared))
    assert disp == {
        "gt_profile_activation.json": "staged",
        "gt_profile_receipt.json": "staged",
        "gt_run_identity.json": "staged",
    }
    # the three artifacts now sit where detect_live_run scans
    ga = task_dir / "gt_artifacts"
    assert (ga / "gt_profile_activation.json").is_file()
    assert (ga / "gt_profile_receipt.json").is_file()
    assert (ga / "gt_run_identity.json").is_file()
    assert _verdict(task_dir, task) == "LIVE_PAID"
    # MUTATION: making stage_provenance_artifacts a no-op (or running it AFTER
    # detect_live_run) leaves the record NOT_LIVE — the exact defect.


# =========================================================================== #
# 3. FAIL-CLOSED / NON-FABRICATING — NEVER default-LIVE.
# =========================================================================== #
def test_staging_baseline_stays_not_live(tmp_path: Path) -> None:
    # baseline arm writes run_identity(baseline=True) and NO activation/receipt.
    m = _import_metrics()
    task = "synthetic__baseline"
    task_dir = tmp_path / "run" / task
    shared = tmp_path / "gt_out"
    _write_min_task(task_dir, task)
    _write_json(shared, "gt_run_identity.json", _valid_identity(baseline=True))

    disp = m.stage_provenance_artifacts(str(task_dir), str(shared))
    assert disp["gt_run_identity.json"] == "staged"
    assert disp["gt_profile_activation.json"] == "absent"
    assert disp["gt_profile_receipt.json"] == "absent"
    # baseline identity present but baseline=True + no activation/receipt => NOT_LIVE
    assert _verdict(task_dir, task) == "NOT_LIVE"
    # MUTATION: fabricating a default activation/receipt when the source is absent
    # would flip the control arm to LIVE_PAID — the fabrication reverted in ea0eb16c0.


def test_staging_absent_sources_skipped_and_no_files_created(tmp_path: Path) -> None:
    m = _import_metrics()
    task = "synthetic__absent"
    task_dir = tmp_path / "run" / task
    shared = tmp_path / "gt_out"
    shared.mkdir(parents=True, exist_ok=True)  # empty shared dir
    _write_min_task(task_dir, task)

    disp = m.stage_provenance_artifacts(str(task_dir), str(shared))
    assert set(disp.values()) == {"absent"}
    assert not (task_dir / "gt_artifacts").exists()  # nothing manufactured
    assert _verdict(task_dir, task) == "NOT_LIVE"
    # MUTATION: creating an empty/placeholder artifact for an absent source fails open.


def test_staging_malformed_source_skipped(tmp_path: Path) -> None:
    m = _import_metrics()
    task = "synthetic__malformed"
    task_dir = tmp_path / "run" / task
    shared = tmp_path / "gt_out"
    _write_min_task(task_dir, task)
    _shared_dir_all_valid(shared)
    _write_json(shared, "gt_profile_activation.json", "{not valid json")

    disp = m.stage_provenance_artifacts(str(task_dir), str(shared))
    assert disp["gt_profile_activation.json"] == "malformed"
    assert not (task_dir / "gt_artifacts" / "gt_profile_activation.json").is_file()
    assert _verdict(task_dir, task) == "NOT_LIVE"


def test_staging_no_shared_dir_is_noop(tmp_path: Path) -> None:
    m = _import_metrics()
    task = "synthetic__noshared"
    task_dir = tmp_path / "run" / task
    _write_min_task(task_dir, task)
    disp = m.stage_provenance_artifacts(str(task_dir), None)
    assert set(disp.values()) == {"no_shared_dir"}
    assert not (task_dir / "gt_artifacts").exists()


# =========================================================================== #
# 4. NON-CLOBBER — never overwrite an already co-located (authoritative) copy,
#    never manufacture a two-location ambiguity conflict.
# =========================================================================== #
def test_staging_never_clobbers_existing_colocated(tmp_path: Path) -> None:
    m = _import_metrics()
    task = "synthetic__noclobber"
    task_dir = tmp_path / "run" / task
    shared = tmp_path / "gt_out"
    _write_min_task(task_dir, task)
    _shared_dir_all_valid(shared)
    # an already-assembled copy with DIFFERENT bytes (e.g. the summarize path)
    ga = task_dir / "gt_artifacts"
    assembled = _valid_activation()
    assembled["members"] = ["GT_GATEWAY"]  # differs from the shared-dir copy
    _write_json(ga, "gt_profile_activation.json", assembled)

    disp = m.stage_provenance_artifacts(str(task_dir), str(shared))
    assert disp["gt_profile_activation.json"] == "present"  # skipped, not clobbered
    on_disk = json.loads((ga / "gt_profile_activation.json").read_text(encoding="utf-8"))
    assert on_disk["members"] == ["GT_GATEWAY"]  # authoritative copy preserved
    assert _verdict(task_dir, task) == "LIVE_PAID"
    # MUTATION: unconditional copy would overwrite the assembled copy and could inject
    # a two-location content conflict elsewhere -> detect_live_run ambiguity -> NOT_LIVE.


def test_staging_never_copies_replay_report(tmp_path: Path) -> None:
    m = _import_metrics()
    task = "synthetic__noreplay"
    task_dir = tmp_path / "run" / task
    shared = tmp_path / "gt_out"
    _write_min_task(task_dir, task)
    _shared_dir_all_valid(shared)
    _write_json(shared, "ss_replay_oracle_report.json",
                {"schema": "ss.replay_oracle_report.v2"})

    m.stage_provenance_artifacts(str(task_dir), str(shared))
    ga = task_dir / "gt_artifacts"
    assert not (ga / "ss_replay_oracle_report.json").is_file()  # replay never staged
    assert _verdict(task_dir, task) == "LIVE_PAID"  # replay_excluded stays honest
    # MUTATION: staging the whole shared dir would drag a replay marker into the task
    # dir and force NOT_LIVE (or worse, launder a replay as live).


# =========================================================================== #
# 5. D9 — the create pass stages+seals LIVE; the --out re-grade pass NEVER stages
#    (task dir byte-untouched, completion-hash binding intact).
# =========================================================================== #
def _read_sealed_verdict(record_path: Path) -> str:
    rec = json.loads(record_path.read_text(encoding="utf-8"))
    return rec["ss_integrity"]["live_run_provenance"]["verdict"]


def test_main_create_pass_seals_live_with_shared_dir(tmp_path: Path) -> None:
    m = _import_metrics()
    task = "synthetic__main_live"
    run_dir = tmp_path / "gt"
    task_dir = run_dir / task
    shared = tmp_path / "gt_out"
    _write_min_task(task_dir, task)
    _shared_dir_all_valid(shared)

    m.main([str(run_dir), "--profile", "2", "--run-id", task,
            "--shared-artifacts-dir", str(shared)])
    sealed = task_dir / f"gt_feature_metrics_{task}.json"
    assert sealed.is_file()
    assert _read_sealed_verdict(sealed) == "LIVE_PAID"
    # MUTATION: not threading --shared-artifacts-dir into the create-pass loop leaves
    # the sealed record NOT_LIVE (the defect); gating staging on args.out being set
    # would also skip the create pass and fail.


def test_main_out_pass_does_not_stage_preserving_d9(tmp_path: Path) -> None:
    m = _import_metrics()
    task = "synthetic__main_out"
    run_dir = tmp_path / "gt"
    task_dir = run_dir / task
    shared = tmp_path / "gt_out"
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_min_task(task_dir, task)
    _shared_dir_all_valid(shared)

    m.main([str(run_dir), "--profile", "2", "--run-id", task,
            "--out", str(out_dir), "--shared-artifacts-dir", str(shared)])
    # the --out re-grade pass NEVER stages provenance into the sealed task dir: no
    # gt_artifacts is co-located and none of the three identity artifacts appear there,
    # so the D9 completion-hash binding (sealed from the create-pass artifact set) holds.
    assert not (task_dir / "gt_artifacts").exists()
    for name in ("gt_profile_activation.json", "gt_profile_receipt.json",
                 "gt_run_identity.json"):
        assert not (task_dir / name).is_file()
    # the create-pass record already in the task dir is left untouched by the re-grade
    assert not (task_dir / f"gt_feature_metrics_{task}.json").is_file()
    # MUTATION: staging in the --out re-grade pass co-locates gt_artifacts into the
    # sealed task dir and breaks the D9 completion-hash binding for every task.
