"""Pin: a P0 RUN-IDENTITY GATE must emit one resolved identity artifact + fail closed on a
substrate-digest mismatch BEFORE any paid agent spend.

WHY (gt_math R01-R04): a run needs per-run identity proof (substrate digest, build/seam/runner
hashes, resolved gt_ref). Today those facts are scattered (run_manifest.json, run_provenance.json)
and NOTHING fails closed on a mismatch, and no single artifact carries the resolved identity for the
auditor. The known trap this gate prevents (memory: "latest tag != latest good"): a run silently
using the WRONG substrate digest — a stale cached tarball / a :latest fallback that loaded a
different image than the pinned @sha256 — which fail-closed GT_REQUIRE_LSP 5/5 on the wrong image.

FIX (this file pins it), in the trial job of swebench_live_lite_full.yml:
  1. IDENTITY ARTIFACT: a NEW step, AFTER the substrate is pulled/loaded (substrate_proof.sh) and
     BEFORE the paid agent-launch step, writes gt_run_identity.json (schema gt.run_identity.v1) into
     the /gt_out bind-mount dir — substrate_digest_expected/actual, gt_ref_requested/resolved,
     seam_sha256, runner_sha256, workflow_run_id. Written on BOTH arms.
  2. FAIL-CLOSED PARITY: if the pinned substrate digest is not present locally before spend
     (actual != expected), emit GT_IDENTITY_MISMATCH + exit 1 (and write proof_status=failed so the
     paid step's existing PROOF_STATE!=ok gate refuses — no model spend). Gate ONLY on digest.
  3. COMMIT PARITY is RECORDED, NOT gated: baked substrate_build_commit vs synced run commit differ
     BY DESIGN (run_manifest.commit_parity == "mismatch" is EXPECTED). The gate must never exit on it.
  4. Collect copies gt_run_identity.json into trial_results/gt_artifacts/ (the dir is cherry-picked).

CRITICAL invariant kept green (the docker `bash -c '` block trap): the paid agent step is ONE
single-quoted `bash -c '...'`. A stray apostrophe closes the quote early and the agent silently
never launches. The identity gate is a SEPARATE step (never touches that block); the apostrophe-parity
pin below re-asserts the block stayed quote-balanced after this wave's edits.
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
    # appears in a comment cannot satisfy a code-presence / proximity assertion.
    return [ln for ln in run.splitlines() if not ln.strip().startswith("#")]


def _trial_steps() -> list[dict]:
    # The trial job = the job whose steps include the paid agent-launch step (uniquely identified by
    # _GT_PROFILE_EXPORTS, which lives only inside the single-quoted docker bash -c block).
    for job in _doc().get("jobs", {}).values():
        steps = [s for s in (job.get("steps", []) or []) if isinstance(s, dict)]
        if any("_GT_PROFILE_EXPORTS" in (s.get("run") or "") for s in steps):
            return steps
    raise AssertionError("no job contains the paid agent-launch step (_GT_PROFILE_EXPORTS)")


def _index_of(steps: list[dict], token: str) -> int:
    for i, s in enumerate(steps):
        if token in (s.get("run") or ""):
            return i
    raise AssertionError(f"no trial step run contains {token!r}")


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


# ── 1. ORDERING: identity gate AFTER substrate pull, BEFORE the paid agent step ────────────────


def test_identity_gate_after_substrate_pull_and_before_agent_launch() -> None:
    # The gate needs the pinned substrate loaded locally (docker inspect), so it must sit AFTER the
    # substrate proof step; and it must precede the paid agent step so no spend happens before
    # identity is proven. Asserted by STEP INDEX inside the trial job.
    steps = _trial_steps()
    proof_idx = _index_of(steps, "substrate_proof.sh")
    id_idx = _index_of(steps, "gt.run_identity.v1")
    agent_idx = _index_of(steps, "_GT_PROFILE_EXPORTS")
    assert proof_idx < id_idx, (
        f"run-identity gate (step {id_idx}) must run AFTER the substrate is pulled/loaded "
        f"(substrate_proof.sh at step {proof_idx}) — docker inspect needs the local image"
    )
    assert id_idx < agent_idx, (
        f"run-identity gate (step {id_idx}) must run BEFORE the paid agent-launch step "
        f"(step {agent_idx}) — no model spend before identity is proven"
    )


# ── 2. THE IDENTITY ARTIFACT ───────────────────────────────────────────────────────────────────


def test_trial_step_writes_run_identity_json() -> None:
    run = _step_run_containing("gt.run_identity.v1")
    code = _code_lines(run)
    joined = "\n".join(code)
    assert any("gt_run_identity.json" in ln for ln in code), (
        "the run-identity gate must WRITE gt_run_identity.json in a code line (found only in comments, "
        "or not at all)"
    )
    assert "gt.run_identity.v1" in joined, "the artifact must carry its schema tag in a code line"
    assert "/gt_out/gt_run_identity.json" in joined, (
        "the artifact must be written into the /gt_out bind-mount dir (the dir the Collect step gathers)"
    )


def test_identity_artifact_carries_the_resolved_identity_fields() -> None:
    joined = "\n".join(_code_lines(_step_run_containing("gt.run_identity.v1")))
    for field in (
        "substrate_digest_expected",
        "substrate_digest_actual",
        "gt_ref_requested",
        "gt_ref_resolved",
        "seam_sha256",
        "runner_sha256",
        "workflow_run_id",
    ):
        assert field in joined, f"run-identity artifact must record {field!r}"
    # seam/runner identity = the hashes of the staged seam + runner (both cp'd into /opt/gt).
    assert "gt_mini_patch.py" in joined, "seam_sha256 must hash artifact_deepswe/gt_mini_patch.py"
    assert "gt_headless_runner.py" in joined, "runner_sha256 must hash artifact_deepswe/gt_headless_runner.py"


def test_actual_digest_comes_from_docker_inspect_of_the_expected_ref() -> None:
    joined = "\n".join(_code_lines(_step_run_containing("gt.run_identity.v1")))
    assert "docker inspect" in joined, (
        "the ACTUAL substrate digest must be read from `docker inspect` of the locally-loaded image"
    )
    assert "GT_SUBSTRATE_DIGEST" in joined, (
        "the EXPECTED digest is the workflow's GT_SUBSTRATE_DIGEST (the pull input/var)"
    )


# ── 3. FAIL-CLOSED on digest mismatch ────────────────────────────────────────────────────────────


def test_identity_gate_emits_marker_and_exits_nonzero_on_mismatch() -> None:
    run = _step_run_containing("GT_IDENTITY_MISMATCH")
    code = _code_lines(run)
    joined = "\n".join(code)
    assert "GT_IDENTITY_MISMATCH" in joined, (
        "a digest mismatch must emit GT_IDENTITY_MISMATCH in a code line (the auditor/log marker)"
    )
    idx = joined.index("GT_IDENTITY_MISMATCH")
    window = joined[idx: idx + 400]
    assert "exit 1" in window, (
        "GT_IDENTITY_MISMATCH must be followed by `exit 1` (fail-closed before any model spend)"
    )


def test_digest_gate_is_scoped_to_the_non_baseline_arm() -> None:
    # The baseline (control) arm consumes NO substrate, so it must never fail-close on a substrate it
    # does not have. The digest gate is scoped by the baseline flag (identity is still recorded on
    # both arms — this only scopes the fail-close).
    joined = "\n".join(_code_lines(_step_run_containing("gt.run_identity.v1")))
    assert "GT_ID_BASELINE" in joined, (
        "the digest fail-close must be scoped by the baseline flag (baseline consumes no substrate)"
    )


# ── 4. COMMIT PARITY: recorded, never gated ──────────────────────────────────────────────────────


def test_commit_parity_is_recorded_but_not_gated() -> None:
    # gt_math known-good semantics: the baked substrate_build_commit and the synced run commit differ
    # BY DESIGN (run_manifest.commit_parity == "mismatch" is EXPECTED). The gate must NEVER fail-close
    # on commit parity — only on the substrate digest. Two-part pin:
    #   (1) LOAD-BEARING invariant: the step has EXACTLY ONE `exit 1`, and it belongs to the
    #       GT_IDENTITY_MISMATCH digest gate. A second `exit 1` would be another gate — the exact
    #       regression (a commit-parity gate) this test forbids.
    #   (2) PROXIMITY (the requested form): no `exit 1` within N chars of any `commit_parity` mention.
    #       LIMIT (documented, by design): this is a proximity heuristic keyed to the current layout
    #       (the commit_parity RECORD sites sit far above the lone digest gate). It does not prove
    #       semantic non-gating on its own — (1) is the invariant that does; (2) catches a regression
    #       that plants an exit next to the record site. Comments are excluded (code lines only), so a
    #       commit_parity MENTION in a comment cannot trip it.
    code = _code_lines(_step_run_containing("gt.run_identity.v1"))
    joined = "\n".join(code)
    # (1)
    assert joined.count("exit 1") == 1, (
        "the run-identity step must have EXACTLY ONE `exit 1` (the digest gate); a second exit would "
        "be a second gate — commit parity (or anything else) must never be a second fail-close"
    )
    ex = joined.index("exit 1")
    gate = joined.rfind("GT_IDENTITY_MISMATCH", 0, ex)
    assert gate != -1 and ex - gate < 400, (
        "the single `exit 1` must belong to the GT_IDENTITY_MISMATCH digest gate"
    )
    # commit parity must actually be RECORDED (present in the identity step).
    assert "commit_parity" in joined, "commit parity must be RECORDED in the identity artifact"
    # (2)
    N = 250
    start = 0
    while True:
        i = joined.find("commit_parity", start)
        if i == -1:
            break
        seg = joined[max(0, i - N): i + N]
        assert "exit 1" not in seg, (
            f"an `exit 1` sits within {N} chars of a commit_parity mention — commit parity must be "
            f"RECORDED, never GATED (see this test's LIMIT note for the check's precise scope)"
        )
        start = i + len("commit_parity")


# ── 5. COLLECT: the identity artifact reaches the uploaded task artifact ──────────────────────────


def test_collect_step_copies_run_identity_json() -> None:
    # The Collect step cherry-picks files, so without an explicit cp the /gt_out artifact is dropped
    # (a dark artifact). Anchor on the Collect step's distinctive mkdir.
    collect = _step_run_containing("mkdir -p trial_results trial_results/gt_artifacts")
    code = "\n".join(_code_lines(collect))
    assert "/tmp/gt_out/gt_run_identity.json" in code, (
        "the Collect step must copy gt_run_identity.json out of /tmp/gt_out (= /gt_out bind-mount)"
    )
    assert "trial_results/gt_artifacts/gt_run_identity.json" in code, (
        "the identity artifact must land in gt_artifacts/ next to run_provenance.json"
    )


# ── 6. THE TRAP: the paid step's single-quoted docker block stays quote-balanced ─────────────────


def test_docker_block_single_quote_is_still_balanced() -> None:
    # The identity gate is a SEPARATE step and never touches the docker `bash -c '` block. Re-assert
    # the block is still quote-balanced (even apostrophe count opener..closing inclusive). LIMIT: this
    # catches an ODD number of stray apostrophes (the single-apostrophe break, the actual trap); a
    # re-balancing PAIR is an accepted blind spot, paired with the yaml.safe_load parse above.
    run = _step_run_containing("_GT_PROFILE_EXPORTS")
    block = _docker_block(run)
    assert block.count(_APOS) % 2 == 0, (
        "the docker `bash -c '` block has an ODD number of apostrophes — a stray single quote was "
        "introduced and will close the block early (the agent never launches)"
    )
    assert run.count("bash -c " + _APOS) == 1, "expected exactly one `bash -c '` opener in the trial step"
