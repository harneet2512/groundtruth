"""Pin: a P1 MANDATORY-METRICS gate — every paid task must carry its FULL v3 metric set, and a
task that does not is fail-closed (NOT citable), per MANDATORY_METRICS.md line 1 ("A run without
its metrics is NOT done").

WHY (MANDATORY_METRICS.md §12, the v3 `gt.feature_metrics.v1` per-feature contract): the Collect
step already runs `gt_deep_metrics.py` (sections 1-11). §12 adds the per-FEATURE behavioural
contract, emitted by `scripts/swebench/gt_feature_metrics.py` into `gt_feature_metrics_<task>.json`.
Before this wave, a paid GT-on task could finish, upload, and be audited with NO feature-metrics
file and no hard stop — a run silently "not done" but treated as citable.

FIX (this file pins it), in the Collect step of swebench_live_lite_full.yml:
  1. INVOKE the v3 CLI: after the existing `gt_deep_metrics.py` line, run
     `scripts/swebench/gt_feature_metrics.py` (same PYTHONPATH pattern), copy its outputs into
     trial_results/.
  2. METRICS-COMPLETE gate (GT-on arm, GT_BASELINE!=1): after BOTH metric steps, require the full
     per-task set to exist non-empty — gt_deep_metrics_<task>.json, gt_feature_metrics_<task>.json,
     gt_profile_activation.json, gt_run_identity.json. Any missing => emit GT_METRICS_INCOMPLETE
     (tee'd to the trial log, citing MANDATORY_METRICS.md) + `exit 1`, the SAME fail-closed style as
     GT_PROFILE_UNPROVEN.
  3. BASELINE (control) arm: only gt_deep_metrics_<task>.json is required (no GT artifacts exist
     there) — the GT-only three sit under the GT_BASELINE guard.

WHERE the gate lives (justified): NOT in the liveness step but at the END of Collect, BECAUSE the
per-task metric artifacts are PRODUCED in Collect (the two metric CLIs run there); the liveness step
runs earlier (before the evaluator + before Collect) and cannot gate on files that do not yet exist.
Collect is `if: always()` so it runs on partial failures; its `exit 1` fails the job (fail-closed)
while the later Upload (`if: always()`) still captures every collected artifact — the same "run
captured but not citable" semantics as GT_PROFILE_UNPROVEN.

CRITICAL invariant kept green (the docker `bash -c '` block trap): the paid agent step is ONE
single-quoted `bash -c '...'`. A stray apostrophe closes the quote early and the agent silently never
launches. This gate is added to the Collect step and never touches that block; the apostrophe-parity
pin below re-asserts the block stayed quote-balanced.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"

_APOS = "'"


def _doc() -> dict:
    return yaml.safe_load(_WF.read_text(encoding="utf-8"))


def _all_steps() -> list[dict]:
    out: list[dict] = []
    for job in _doc().get("jobs", {}).values():
        for step in job.get("steps", []) or []:
            if isinstance(step, dict):
                out.append(step)
    return out


def _step_run_containing(token: str) -> str:
    for step in _all_steps():
        run = step.get("run")
        if run and token in run:
            return run
    raise AssertionError(f"no workflow step run contains {token!r}")


def _code_lines(run: str) -> list[str]:
    # bash comment lines start with '#' (modulo indentation); exclude them so a token that only
    # appears in a comment cannot satisfy a code-presence assertion.
    return [ln for ln in run.splitlines() if not ln.strip().startswith("#")]


def _collect_run() -> str:
    # The Collect step, anchored on its distinctive mkdir (the eval step also creates trial_results).
    return _step_run_containing("mkdir -p trial_results trial_results/gt_artifacts")


def _gate_block(run: str) -> str:
    """The METRICS-COMPLETE gate: from its sentinel `_MM_MISSING` (the gate's first line) THROUGH the
    trailing GT_METRICS_COMPLETE success echo (inclusive). Isolated from the `cp` lines ABOVE it, so a
    token found here is CHECKED BY THE GATE — not merely copied earlier in the Collect step."""
    start = run.index("_MM_MISSING")
    ok = run.index("GT_METRICS_COMPLETE")
    end = run.index("\n", ok)
    return run[start:end]


def _docker_block(run: str) -> str:
    """The single-quoted `bash -c '...'` argument, opening quote THROUGH closing quote (inclusive),
    delimited by the unique `bash -c '` opener and the terminating apostrophe that precedes
    `2>&1 | tee trial_output.log` (the docker block's tee sink)."""
    marker = "bash -c "
    i = run.index(marker) + len(marker)
    assert run[i] == _APOS, "the docker invocation must open with a single-quoted bash -c argument"
    tail = run.index("2>&1 | tee trial_output.log", i)
    close = run.rindex(_APOS, i, tail)
    return run[i:close + 1]


# ── 0. structural integrity ───────────────────────────────────────────────────────────────────


def test_workflow_yaml_parses() -> None:
    doc = _doc()
    assert isinstance(doc, dict) and doc.get("jobs"), "workflow must remain valid, parseable YAML"


# ── 1. INVOKE the v3 feature-metrics CLI in Collect, with the task id ─────────────────────────


def test_collect_runs_gt_feature_metrics_with_task_id() -> None:
    collect = _collect_run()
    code = _code_lines(collect)
    hits = [ln for ln in code if "gt_feature_metrics.py" in ln and "matrix.task" in ln]
    assert hits, (
        "the Collect step must INVOKE scripts/swebench/gt_feature_metrics.py in a CODE line carrying "
        "the task id (${{ matrix.task }}) — found only in a comment, or without the task id, or not "
        "at all"
    )
    # it must run AFTER the existing gt_deep_metrics invocation (the v3 pass builds on the deep pass).
    joined = "\n".join(code)
    assert joined.index("gt_deep_metrics.py") < joined.index("gt_feature_metrics.py"), (
        "gt_feature_metrics.py must be invoked AFTER the existing gt_deep_metrics.py line"
    )


# ── 2. the gate: all 4 GT-arm artifacts, GT_METRICS_INCOMPLETE, exit 1 (fail-closed) ──────────


def test_gate_checks_all_four_gt_artifacts_and_fails_closed() -> None:
    run = _step_run_containing("GT_METRICS_INCOMPLETE")
    gate = _gate_block(run)
    for artifact in (
        "gt_deep_metrics_${{ matrix.task }}.json",
        "gt_feature_metrics_${{ matrix.task }}.json",
        "gt_profile_activation.json",
        "gt_run_identity.json",
    ):
        assert artifact in gate, (
            f"the METRICS-COMPLETE gate must CHECK for {artifact!r} in its per-task artifact set "
            f"(a run without its metrics is NOT done)"
        )
    assert "GT_METRICS_INCOMPLETE" in gate, "the gate must emit the GT_METRICS_INCOMPLETE marker"
    idx = gate.index("GT_METRICS_INCOMPLETE")
    window = gate[idx: idx + 400]
    assert "exit 1" in window, (
        "GT_METRICS_INCOMPLETE must be followed by `exit 1` (fail-closed, same mechanism as "
        "GT_PROFILE_UNPROVEN) — an emitted marker with no exit would let a metrics-incomplete task "
        "pass as citable"
    )


# ── 3. baseline (control) arm requires ONLY gt_deep_metrics (GT_BASELINE guard shape) ─────────


def test_baseline_arm_requires_only_deep_metrics() -> None:
    gate = _gate_block(_step_run_containing("GT_METRICS_INCOMPLETE"))
    assert "GT_BASELINE" in gate, (
        "the gate must carry a GT_BASELINE guard so the baseline (control) arm requires ONLY "
        "gt_deep_metrics (no GT artifacts exist on the baseline arm)"
    )
    guard = gate.index("GT_BASELINE")
    # gt_deep_metrics is required on BOTH arms → checked BEFORE the guard (unconditional).
    assert gate.index("gt_deep_metrics_${{ matrix.task }}.json") < guard, (
        "gt_deep_metrics_<task>.json must be required unconditionally (both arms), i.e. BEFORE the "
        "GT_BASELINE guard"
    )
    # the three GT-only artifacts are required only on the GT arm → checked AFTER the guard.
    for gt_only in (
        "gt_feature_metrics_${{ matrix.task }}.json",
        "gt_profile_activation.json",
        "gt_run_identity.json",
    ):
        assert gt_only in gate, f"the gate must CHECK {gt_only!r} (a GT-only artifact)"
        assert gate.index(gt_only) > guard, (
            f"{gt_only!r} is a GT-only artifact and must be required only under the GT_BASELINE guard "
            f"(after it), never on the baseline arm"
        )


# ── 4. the marker cites MANDATORY_METRICS.md ("a run without its metrics is NOT done") ────────


def test_marker_cites_mandatory_metrics_not_done() -> None:
    gate = _gate_block(_step_run_containing("GT_METRICS_INCOMPLETE"))
    idx = gate.index("GT_METRICS_INCOMPLETE")
    marker_line = gate[idx: gate.index("\n", idx)]
    assert "MANDATORY_METRICS.md" in marker_line, (
        "the GT_METRICS_INCOMPLETE marker must cite MANDATORY_METRICS.md (line 1: a run without its "
        "metrics is NOT done)"
    )
    assert "NOT done" in marker_line or "not done" in marker_line, (
        "the marker must state the run is NOT done"
    )
    assert "tee -a trial_output.log" in marker_line, (
        "the marker must `tee -a trial_output.log` so the section-4 auditor sees it in the run log "
        "(same as GT_PROFILE_UNPROVEN's kin)"
    )


# ── 5. THE TRAP: the paid step's single-quoted docker block stays quote-balanced ──────────────


def test_docker_block_single_quote_is_balanced() -> None:
    # This gate is added to the Collect step and never touches the paid step's docker `bash -c '`
    # block. Re-assert the block is still quote-balanced (even apostrophe count opener..closing
    # inclusive). LIMIT: this catches an ODD number of stray apostrophes (the single-apostrophe
    # break, the actual trap); a re-balancing PAIR is an accepted blind spot, paired with the
    # yaml.safe_load parse above.
    run = _step_run_containing("_GT_PROFILE_EXPORTS")
    block = _docker_block(run)
    assert block.count(_APOS) % 2 == 0, (
        "the docker `bash -c '` block has an ODD number of apostrophes — a stray single quote was "
        "introduced and will close the block early (the agent never launches)"
    )
    assert run.count("bash -c " + _APOS) == 1, "expected exactly one `bash -c '` opener in the trial step"
