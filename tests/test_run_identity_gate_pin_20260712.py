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


def _job(name: str) -> dict:
    return _doc()["jobs"][name]


def _trial_step_named(name: str) -> dict:
    for step in _trial_steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"no trial step named {name!r}")


def _step_named(name: str) -> dict:
    for step in _all_steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"no workflow step named {name!r}")


def _index_of(steps: list[dict], token: str) -> int:
    for i, s in enumerate(steps):
        if token in (s.get("run") or ""):
            return i
    raise AssertionError(f"no trial step run contains {token!r}")


def _docker_block(run: str) -> str:
    """The single-quoted `bash -c '...'` argument, opening quote THROUGH closing quote (inclusive),
    delimited by the unique `bash -c '` opener and the terminating apostrophe that precedes
    `2>&1 | tee -a trial_output.log` (the append-only docker log sink)."""
    marker = "bash -c "
    i = run.index(marker) + len(marker)
    assert run[i] == _APOS, "the docker invocation must open with a single-quoted bash -c argument"
    tail = run.index("2>&1 | tee -a trial_output.log", i)
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
        "gt_ref_prepared_sha",
        "gt_ref_resolved",
        "seam_sha256",
        "runner_sha256",
        "workflow_run_id",
    ):
        assert field in joined, f"run-identity artifact must record {field!r}"
    # seam/runner identity = the hashes of the staged seam + runner (both cp'd into /opt/gt).
    assert "gt_mini_patch.py" in joined, "seam_sha256 must hash artifact_deepswe/gt_mini_patch.py"
    assert "gt_headless_runner.py" in joined, "runner_sha256 must hash artifact_deepswe/gt_headless_runner.py"


def test_checkout_commit_is_resolved_from_git_before_substrate_proof() -> None:
    steps = _trial_steps()
    resolve = _trial_step_named("Resolve checked-out GT commit")
    run = "\n".join(_code_lines(resolve.get("run") or ""))
    assert "git rev-parse --verify 'HEAD^{commit}'" in run
    assert "^[0-9a-f]{40,64}$" in run, "the resolved object must be validated as a canonical commit SHA"
    assert 'GT_CHECKOUT_SHA=$GT_CHECKOUT_SHA' in run and 'GT_GITHUB_SHA=$GT_CHECKOUT_SHA' in run
    assert "$GITHUB_ENV" in run, "the exact checkout identity must propagate to later trial steps"
    resolve_idx = steps.index(resolve)
    assert resolve_idx < _index_of(steps, "substrate_proof.sh")
    assert resolve_idx < _index_of(steps, "gt.run_identity.v1")


def test_prepare_resolves_gt_ref_once_and_downstream_checkouts_are_immutable() -> None:
    prepare = _job("prepare")
    assert prepare["outputs"]["gt_sha"] == "${{ steps.resolve_gt.outputs.gt_sha }}"
    resolve = next(step for step in prepare["steps"] if step.get("id") == "resolve_gt")
    run = "\n".join(_code_lines(resolve.get("run") or ""))
    assert "git rev-parse --verify 'HEAD^{commit}'" in run
    assert "^[0-9a-f]{40,64}$" in run
    assert "gt_sha=$GT_PREPARED_SHA" in run and "$GITHUB_OUTPUT" in run

    trial_checkout = _trial_step_named("Checkout GroundTruth")
    summarize_checkout = next(
        step for step in _job("summarize")["steps"]
        if step.get("uses") == "actions/checkout@v4"
    )
    immutable = "${{ needs.prepare.outputs.gt_sha }}"
    assert trial_checkout["with"]["ref"] == immutable
    assert summarize_checkout["with"]["ref"] == immutable


def test_trial_checkout_must_equal_prepare_resolved_sha() -> None:
    resolve = _trial_step_named("Resolve checked-out GT commit")
    run = "\n".join(_code_lines(resolve.get("run") or ""))
    assert (resolve.get("env") or {}).get("GT_PREPARED_SHA") == (
        "${{ needs.prepare.outputs.gt_sha }}"
    )
    assert '"$GT_CHECKOUT_SHA" != "$GT_PREPARED_SHA"' in run
    assert "GT_CHECKOUT_IDENTITY_MISMATCH" in run
    assert "exit 1" in run
    assert "GT_PREPARED_SHA=$GT_PREPARED_SHA" in run


def test_identity_uses_checkout_sha_and_keeps_dispatch_sha_separate() -> None:
    step = next(s for s in _trial_steps() if "gt.run_identity.v1" in (s.get("run") or ""))
    run = "\n".join(_code_lines(step.get("run") or ""))
    env = step.get("env") or {}
    assert "GT_CHECKOUT_SHA" in run
    assert "gt_ref_resolved_source" in run and "git_rev_parse_head" in run
    assert "workflow_dispatch_sha" in run
    assert env.get("GT_ID_GT_REF_REQUESTED") == "${{ inputs.gt_ref || 'gt-trial' }}"
    assert env.get("GT_ID_WORKFLOW_DISPATCH_SHA") == "${{ github.sha }}"
    assert env.get("GT_ID_GT_REF_RESOLVED") != "${{ github.sha }}", (
        "github.sha identifies the workflow dispatch ref, not necessarily the checked-out gt_ref"
    )


def test_identity_producer_fails_closed_on_writer_or_content_failure() -> None:
    run = "\n".join(_code_lines(_step_run_containing("gt.run_identity.v1")))
    assert '|| echo "WARN: gt_run_identity.json write failed"' not in run
    assert "IDENTITY_WRITE_FAILED" in run
    assert "IDENTITY_CONTENT_INVALID" in run
    assert "rm -f /tmp/gt_out/gt_run_identity.json" in run, (
        "a writer failure must not leave a stale prior identity available to the paid recheck"
    )
    assert "os.replace" in run, "identity publication must be atomic"
    for token in (
        "gt.run_identity.v1",
        "re.fullmatch",
        "gt_ref_prepared_sha",
        "gt_ref_resolved",
        "seam_sha256",
        "runner_sha256",
        "workflow_run_id",
        "substrate_digest_expected",
        "substrate_digest_actual",
    ):
        assert token in run
    assert "IDENTITY_FAILURE_CODE" in run and "gt.proof_status.v1" in run
    assert run.index("IDENTITY_WRITE_FAILED") < run.index('if [ -n "$IDENTITY_FAILURE_CODE" ]')


def test_paid_step_independently_rechecks_identity_before_proof_and_spend() -> None:
    paid = _step_run_containing("_GT_PROFILE_EXPORTS")
    pre_spend = paid[: paid.index("HOST_GT_INJECT=/tmp/gt_inject/opt/gt")]
    assert "if ! python3 <<'PY'" in pre_spend, "paid launch must execute the identity validator"
    assert "/tmp/gt_out/gt_run_identity.json" in pre_spend
    assert "GT_RUN_IDENTITY_FAIL" in pre_spend
    assert "re.fullmatch" in pre_spend
    assert "re.compile(r'^[0-9a-f]{40,64}$')" in pre_spend
    assert "re.compile(r'^[0-9a-f]{64}$')" in pre_spend
    for token in (
        "gt.run_identity.v1",
        "GT_CHECKOUT_SHA",
        "gt_ref_prepared_sha",
        "GITHUB_RUN_ID",
        "seam_sha256",
        "runner_sha256",
        "substrate_digest_expected",
        "substrate_digest_actual",
    ):
        assert token in pre_spend
    assert pre_spend.index("gt_run_identity.json") < pre_spend.index("PROOF_STATE=")
    marker = pre_spend.index("GT_RUN_IDENTITY_FAIL")
    assert "exit 1" in pre_spend[marker: marker + 300]


def test_substrate_and_per_task_provenance_use_exact_checkout_identity() -> None:
    proof = next(s for s in _trial_steps() if "substrate_proof.sh" in (s.get("run") or ""))
    assert (proof.get("env") or {}).get("GT_GITHUB_SHA") != "${{ github.sha }}", (
        "substrate proof must inherit checkout-derived GT_GITHUB_SHA from GITHUB_ENV"
    )
    collect = _step_run_containing("gt.live_lite_run_provenance.v1")
    assert "gt_run_identity.json" in collect
    assert "identity.get('gt_ref_resolved')" in collect
    assert "os.environ.get('GITHUB_SHA')" not in collect


def test_summary_gt_commit_is_consensus_from_trial_identity_artifacts() -> None:
    run = _step_named("Aggregate SWE-bench Pro results").get("run") or ""
    assert "gt_run_identity.json" in run
    assert "GT_IDENTITY_CONSENSUS_FAILED" in run
    assert "missing" in run and "invalid" in run
    provenance_line = next(ln for ln in run.splitlines() if "**Provenance**" in ln)
    assert "$GT_COMMIT" in provenance_line
    assert "${{ github.sha }}" not in provenance_line


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
