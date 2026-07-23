from __future__ import annotations

from pathlib import Path
import importlib.util

import yaml

from groundtruth.runtime.rl_profile import resolve_profile


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deepswe_full.yml"
PROOF = ROOT / "scripts" / "ci" / "substrate_proof.sh"
BRIEF_TIME_MEMBERS = ("GT_LOC_RESLOT", "GT_BRIEF_MINIMAL", "GT_BRIEF_NATIVE")


def _proof_step() -> dict:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return next(
        step for step in document["jobs"]["trial"]["steps"]
        if "substrate_proof.sh" in (step.get("run") or "")
    )


def _step_named(name: str) -> dict:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return next(
        step for step in document["jobs"]["trial"]["steps"]
        if step.get("name") == name
    )


def test_profile_token_is_bound_on_the_pretask_proof_step() -> None:
    step = _proof_step()
    assert "GT_RL_PROFILE" in step.get("env", {})
    assert "rl_profile" in str(step["env"]["GT_RL_PROFILE"])


def test_canonical_fanout_precedes_brief_generation_and_crosses_container() -> None:
    shell = PROOF.read_text(encoding="utf-8")
    resolver = shell.index("from groundtruth.runtime.rl_profile import profile_members, resolve_profile")
    apply_exports = shell.index('eval "$_GT_PRETASK_PROFILE_EXPORTS"')
    proof_container = shell.index("docker run --rm")
    brief_generation = shell.index("gt-run-proof --source-root")
    assert resolver < apply_exports < proof_container < brief_generation
    assert '-e GT_RL_PROFILE="${GT_RL_PROFILE:-}"' in shell
    for member in BRIEF_TIME_MEMBERS:
        assert f'-e {member}="${{{member}:-' in shell


def test_profile2_activates_brief_members_and_explicit_zero_survives() -> None:
    resolved = resolve_profile({"GT_RL_PROFILE": "2"})
    assert {member: resolved[member] for member in BRIEF_TIME_MEMBERS} == {
        member: "1" for member in BRIEF_TIME_MEMBERS
    }

    killed = resolve_profile({"GT_RL_PROFILE": "2", "GT_LOC_RESLOT": "0"})
    assert killed["GT_LOC_RESLOT"] == "0"
    assert killed["GT_BRIEF_MINIMAL"] == "1"
    assert killed["GT_BRIEF_NATIVE"] == "1"

    assert resolve_profile({"GT_RL_PROFILE": "0"}) == {}
    assert resolve_profile({}) == {}


def test_gt_run_proof_clean_projection_preserves_resolved_brief_members() -> None:
    path = ROOT / "scripts" / "swebench" / "gt_run_proof.py"
    spec = importlib.util.spec_from_file_location("gt_run_proof_profile_projection", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resolved = resolve_profile({"GT_RL_PROFILE": "2", "GT_LOC_RESLOT": "0"})
    projected = module.clean_env_projection({"GT_RL_PROFILE": "2", **resolved})
    assert projected["GT_RL_PROFILE"] == "2"
    assert projected["GT_LOC_RESLOT"] == "0"
    assert projected["GT_BRIEF_MINIMAL"] == "1"
    assert projected["GT_BRIEF_NATIVE"] == "1"


def test_paid_trial_requires_successful_run_identity_output() -> None:
    identity = _step_named("Publish and validate paid-run identity (pre-agent, fail-closed)")
    trial = _step_named("Run GT trial")
    assert identity["id"] == "gt_run_identity"
    assert 'echo "validated=true" >> "$GITHUB_OUTPUT"' in identity["run"]
    assert "steps.gt_run_identity.outputs.validated == 'true'" in trial["if"]


def test_run_identity_uses_wrapper_proof_policy_not_raw_inner_verdict() -> None:
    identity = _step_named("Publish and validate paid-run identity (pre-agent, fail-closed)")
    run = identity["run"]
    assert "Path('/tmp/gt/proof_status.json')" in run
    assert "Path('/tmp/gt/proof_verdict.json')" not in run
    assert "'proof_status_state': os.environ['GT_ID_PROOF_STATE']" in run
    assert "'proof_status_code': os.environ['GT_ID_PROOF_CODE']" in run


def test_paid_trial_spend_gate_uses_wrapper_proof_status_not_raw_verdict() -> None:
    """Quality-only PROOF_DEGRADED must not false-block spend after identity passed."""
    trial = _step_named("Run GT trial")
    run = trial["run"]
    assert 'Path("/tmp/gt/proof_status.json")' in run
    assert 'Path("/tmp/gt/proof_verdict.json")' not in run
    assert 'PROOF_DEGRADED' in run
