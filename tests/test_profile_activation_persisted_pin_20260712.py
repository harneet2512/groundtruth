"""Pin: Profile-2 activation must leave a DURABLE per-task artifact + a fail-closed liveness gate.

BUG (run 29217805592): the RL-profile activation + capability receipt existed ONLY as stdout log
lines in the trial docker block. The per-task collected artifacts (everything the Collect step
gathers out of $HOST_GT_OUT = /tmp/gt_out) carried NO durable record of activation, so the section-4
auditor read the run as profile-DARK even though Profile-2 was live in-container.

FIX (this file pins it):
  1. the trial/fan-out step (the one that computes _GT_PROFILE_EXPORTS + echoes the activation
     line) ALSO writes gt_profile_activation.json into the /gt_out bind-mount (collected into the
     task artifact) -- a schema-versioned record of {profile, members, capability_receipt}.
  2. the liveness/verdict step (the AGENT_DID_NOT_RUN gate) ALSO fails closed for the GT-on arm
     (GT_BASELINE != 1) when that proof file is absent: it emits GT_PROFILE_UNPROVEN + exit 1, the
     SAME mechanism as AGENT_DID_NOT_RUN, so a GT-on task with no activation proof is never
     citable as a GT-on result.
  3. the baseline (control) arm activates no profile, so it neither writes nor requires the file
     (byte-identical control arm -- no new requirement on the control).

CRITICAL invariant (the docker bash -c ' block trap): the trial step is ONE single-quoted
`bash -c '...'` argument. A single stray apostrophe inside it closes the quote early and the
agent silently never launches. The apostrophe-parity pin below guards that the persist code did
not introduce a bare apostrophe (see its docstring for the precise limits of the check).
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"

_APOS = "'"


def _steps() -> list[dict]:
    doc = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    out: list[dict] = []
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []) or []:
            if isinstance(step, dict):
                out.append(step)
    return out


def _step_run_containing(token: str) -> str:
    for step in _steps():
        run = step.get("run")
        if run and token in run:
            return run
    raise AssertionError(f"no workflow step run contains {token!r}")


def _code_lines(run: str) -> list[str]:
    # bash comment lines start with '#' (modulo indentation); exclude them so a token that only
    # appears in a comment cannot satisfy a code-presence assertion.
    return [ln for ln in run.splitlines() if not ln.strip().startswith("#")]


def _docker_block(run: str) -> str:
    """The single-quoted `bash -c '...'` argument, from its opening quote THROUGH its closing quote
    (inclusive). Delimited by the unique `bash -c '` opener and the terminating apostrophe that
    precedes `2>&1 | tee trial_output.log` (the docker block's tee sink)."""
    marker = "bash -c "
    i = run.index(marker) + len(marker)  # index of the opening quote
    assert run[i] == _APOS, "the docker invocation must open with a single-quoted bash -c argument"
    tail = run.index("2>&1 | tee -a trial_output.log", i)
    close = run.rindex(_APOS, i, tail)
    return run[i:close + 1]


# ── 1. PERSIST: the trial/fan-out step writes the durable activation record ──────────────────


def test_trial_step_writes_profile_activation_json() -> None:
    # The fan-out step (identified by _GT_PROFILE_EXPORTS, unique to the trial docker block) must
    # PERSIST gt_profile_activation.json in a real CODE line -- not merely mention it in a comment.
    run = _step_run_containing("_GT_PROFILE_EXPORTS")
    code = _code_lines(run)
    assert any("gt_profile_activation.json" in ln for ln in code), (
        "the trial/fan-out step must WRITE gt_profile_activation.json in a code line (found only in "
        "comments, or not at all) -- the durable activation record the section-4 auditor reads"
    )
    joined = "\n".join(code)
    assert "/gt_out/gt_profile_activation.json" in joined, (
        "the record must be written into the /gt_out bind-mount (the dir the Collect step gathers)"
    )
    assert "gt.profile_activation.v1" in joined, "the record must carry its schema tag"


def test_trial_step_persist_is_gt_only_and_collected() -> None:
    # The persist write lives inside the GT-on branch (the _GT_PROFILE_EXPORTS activation sits under
    # `if [ "${GT_BASELINE:-0}" != "1" ]`), and the Collect step copies it into the uploaded artifact
    # (durability -- the whole point: without collection the record never reaches the auditor).
    trial = _step_run_containing("_GT_PROFILE_EXPORTS")
    assert "GT_BASELINE" in trial, "the persist write must live under the GT-on (non-baseline) guard"
    # anchor the Collect step on its distinctive mkdir (the eval step also creates trial_results).
    collect = _step_run_containing("mkdir -p trial_results trial_results/gt_artifacts")
    assert "gt_profile_activation.json" in collect, (
        "the Collect step must copy gt_profile_activation.json into the uploaded task artifact"
    )


def test_profile_preflight_failure_aborts_before_agent_spend() -> None:
    """A failed Profile-2 preflight must stop inside the container before mini-swe-agent.

    The post-run ``GT_PROFILE_UNPROVEN`` gate protects citation integrity, but it is too late
    to protect model spend.  The resolver command therefore must not swallow its status with
    ``|| true``; both a non-zero resolver and an empty export set must exit before the seam is
    installed or the paid agent command can run.
    """
    run = _step_run_containing("_GT_PROFILE_EXPORTS")
    code = "\n".join(_code_lines(run))
    resolver_line = next(
        ln for ln in _code_lines(run)
        if "_GT_PROFILE_EXPORTS=" in ln and "rl_profile --emit-exports" in ln
    )
    assert "|| true" not in resolver_line, (
        "the in-container Profile-2 resolver must preserve its non-zero status; swallowing it "
        "spends model budget with the whole profile dark"
    )
    abort = code.index("GT_RL_PREFLIGHT_ABORT_BEFORE_SPEND")
    install = code.index('[ -f /opt/gt/gt_mini_patch.py ]')
    assert abort < install, "the profile-preflight abort must precede seam install / agent spend"
    assert "exit 1" in code[abort:install], (
        "the before-spend profile-preflight marker must be followed by a non-zero exit"
    )


def test_profile_activation_receipt_write_failure_aborts_before_agent_spend() -> None:
    """Activation without its durable proof must not proceed into a paid agent run."""
    run = _step_run_containing("_GT_PROFILE_EXPORTS")
    code = "\n".join(_code_lines(run))
    write = code.index("/gt_out/gt_profile_activation.json")
    install = code.index('[ -f /opt/gt/gt_mini_patch.py ]')
    window = code[write:install]
    assert "GT_PROFILE_RECEIPT_ABORT_BEFORE_SPEND" in window, (
        "failure to persist the durable activation proof must emit a before-spend abort marker"
    )
    marker = window.index("GT_PROFILE_RECEIPT_ABORT_BEFORE_SPEND")
    assert "exit 1" in window[marker:], (
        "a profile-activation receipt write failure must exit before seam install / agent spend"
    )
    write_line = next(
        ln for ln in _code_lines(run)
        if "/gt_out/gt_profile_activation.json" in ln and "GTPA_PROFILE=" in ln
    )
    assert "WARN gt_profile_activation.json write failed" not in write_line, (
        "the receipt writer must not reduce an uncitable activation to a warning"
    )


def test_in_seam_profile_receipt_is_collected_and_required() -> None:
    # WIRING (W2+W3 integration, orchestrator LIPI 2026-07-12): gt_mini_patch._install() writes the
    # IN-SEAM receipt gt_profile_receipt.json into dirname(GT_RUNTIME_LEDGER) = /gt_out — the
    # STRONGER activation proof (agent-process env + patched classes, vs the fan-out step's
    # shell-side record). The Collect step cherry-picks files, so without an explicit cp the
    # receipt is written in-container and silently dropped (a dark artifact — the exact defect
    # class this wave closes). The seam writer stays best-effort so artifact I/O cannot crash the
    # agent; the post-run gates still mark a run without this stronger proof as uncitable.
    collect = _step_run_containing("mkdir -p trial_results trial_results/gt_artifacts")
    code = "\n".join(_code_lines(collect))
    assert "/tmp/gt_out/gt_profile_receipt.json" in code, (
        "the Collect step must copy the in-seam gt_profile_receipt.json out of /tmp/gt_out "
        "(dirname of GT_RUNTIME_LEDGER) into the uploaded task artifact"
    )
    assert "trial_results/gt_artifacts/gt_profile_receipt.json" in code, (
        "the receipt must land in gt_artifacts/ next to gt_profile_activation.json"
    )
    # Both post-run gates require the receipt; shell activation alone is insufficient proof.
    liveness = _step_run_containing("AGENT_DID_NOT_RUN")
    assert "gt_profile_receipt.json" in "\n".join(_code_lines(liveness)), (
        "the liveness gate must fail closed when the in-agent profile receipt is absent"
    )
    metrics = _step_run_containing("GT_METRICS_COMPLETE")
    assert any(
        "[ -s" in ln and "gt_artifacts/gt_profile_receipt.json" in ln
        for ln in _code_lines(metrics)
    ), (
        "the uploaded-artifact completeness gate must require the in-agent receipt"
    )


def test_pre_matrix_lint_checks_this_workflow_fail_closed() -> None:
    step = next(s for s in _steps() if s.get("name", "").startswith("Workflow lint"))
    code_lines = _code_lines(step["run"])
    target = ".github/workflows/swebench_live_lite_full.yml"
    assert target in "\n".join(code_lines), (
        "the paid workflow must lint its own resolved revision before the matrix"
    )
    target_line = next(ln for ln in code_lines if target in ln)
    assert "||" not in target_line, "self-lint must fail closed before the paid matrix"


# ── 2. LIVENESS: fail closed on a GT-on task with no activation proof ─────────────────────────


def test_liveness_step_fails_closed_on_missing_profile_proof() -> None:
    run = _step_run_containing("AGENT_DID_NOT_RUN")  # the liveness/verdict step
    assert "gt_profile_activation.json" in run, (
        "the liveness step must CHECK for the durable activation proof file"
    )
    code = _code_lines(run)
    joined = "\n".join(code)
    assert "GT_PROFILE_UNPROVEN" in joined, (
        "an absent proof must emit the GT_PROFILE_UNPROVEN marker in a code line (same mechanism as "
        "AGENT_DID_NOT_RUN), so a GT-on task with no activation proof is never a citable GT-on result"
    )
    assert "GT_BASELINE" in joined, (
        "the proof gate must be scoped to the GT-on arm only (gated on GT_BASELINE) so the control "
        "arm neither writes nor requires the file (byte-identical control)"
    )


def test_liveness_profile_gate_preserves_runs_but_revokes_citability() -> None:
    # OLD contract: the FIRST GT_PROFILE_UNPROVEN was followed by `exit 1` -- ANY absent activation
    # proof hard-failed (DISCARDED) the task. NEW contract (2026-07-20 TAXONOMY SPLIT): an ABSENT
    # receipt means the agent RAN but activation was not DURABLY proven -> RECORD (relabel GT-off /
    # uncitable-as-GT-on, tee'd to the log) + set GT_PROFILE_ABSENT=1 + CONTINUE; never discard a
    # task that ran. A PRESENT-but-invalid receipt is likewise preserved for diagnosis, but the
    # workflow must route it through _gt_uncitable so it can never enter the citable population.
    run = _step_run_containing("GT_PROFILE_UNPROVEN")
    idx = run.index("GT_PROFILE_UNPROVEN")            # first match = GT_PROFILE_UNPROVEN_ABSENT
    absent_window = run[idx: idx + 400]
    # NEW: the ABSENT path records-and-continues -- no exit; it sets the relabel flag instead.
    assert "exit 1" not in absent_window, (
        "an ABSENT activation receipt must NOT fail-close -- the agent ran, so the task is recorded "
        "uncitable-as-GT-on and the run continues (TAXONOMY SPLIT), not discarded"
    )
    assert "GT_PROFILE_ABSENT=1" in absent_window, (
        "the absent path must set GT_PROFILE_ABSENT=1 (relabel GT-off / uncitable-as-GT-on)"
    )
    assert "tee -a trial_output.log" in absent_window, (
        "the absent relabel marker must be tee'd to the trial log so the outcome classifiers read it"
    )
    # A receipt PRESENT but MISLABELLING the arm is guarded to run only when a receipt is PRESENT
    # (GT_PROFILE_ABSENT != 1), then records an explicit uncitable reason without destroying the
    # trajectory/patch/reward needed to diagnose the validity defect.
    assert '[ "${GT_PROFILE_ABSENT:-0}" != "1" ]' in run, (
        "the present-but-mislabel anti-cheat path must be guarded to run only when a receipt is PRESENT"
    )
    mislabel = run.index("GT_BATCH_UNPROVEN: batch activation receipt is invalid")
    mislabel_window = run[mislabel - 80: mislabel + 240]
    assert "_gt_uncitable gt_batch_unproven" in mislabel_window, (
        "a PRESENT-but-mislabelled batch activation receipt must revoke citability with a durable "
        "reason while preserving the executed run for diagnosis"
    )
    assert "exit 1" not in mislabel_window


# ── 3. THE TRAP: the single-quoted docker bash -c block must stay quote-balanced ──────────────


def test_docker_block_single_quote_is_balanced() -> None:
    # THE TRAP: the trial step is ONE single-quoted `bash -c '...'`. A stray apostrophe inside it
    # closes the quote early and the agent silently never launches. The quote-break idiom used for
    # host-shell var injection (e.g. ="'"$INPUT_MODEL"'") always adds apostrophes in PAIRS, so a
    # correct block always has an EVEN number of apostrophes (opening..closing inclusive). A single
    # bare apostrophe -- the documented failure -- makes the count ODD.
    #
    # LIMITS (pragmatic, by design): this parity check catches an ODD number of stray apostrophes
    # (the single-apostrophe break, which is the actual trap). It does NOT catch a *pair* of stray
    # apostrophes that happen to re-balance -- an accepted blind spot for a cheap static guard. It is
    # paired with the yaml.safe_load structural parse (test_workflow_yaml_parses) below.
    run = _step_run_containing("_GT_PROFILE_EXPORTS")
    block = _docker_block(run)
    assert block.count(_APOS) % 2 == 0, (
        "the docker `bash -c '` block has an ODD number of apostrophes -- a stray single quote was "
        "introduced and will close the block early (the agent never launches)"
    )
    # exactly one docker `bash -c '` opener in the trial step (the pin's anchor stays unambiguous).
    assert run.count("bash -c " + _APOS) == 1, "expected exactly one `bash -c '` opener in the trial step"


def test_workflow_yaml_parses() -> None:
    # Structural integrity: a broken block-scalar / quote frequently makes the YAML itself
    # unparseable, so a clean safe_load is a cheap first line of defense on every edit.
    doc = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and doc.get("jobs"), "workflow must remain valid, parseable YAML"
