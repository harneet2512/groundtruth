"""Pin (S4): the body-semantic leg (GT_SEM_BODY) must be ON by DEFAULT at brief generation.

GAP (2026-07-13): the RL-Profile-2 fan-out that turns GT_SEM_BODY on reaches only the AGENT
container. The substrate-proof / brief-generation step runs gt-run-proof via
scripts/ci/substrate_proof.sh, which projected `-e GT_SEM_BODY="${GT_SEM_BODY:-0}"` on BOTH the
deepswe branch (:637) and the else branch (:675). So unless a caller explicitly exported
GT_SEM_BODY, the body-semantic leg -- the stratum-B localization lever whose rows ARE baked
(sem_body_rows=1789) -- was OFF exactly where the brief is generated. This is a PRE-RUN workflow
fix (no rebake): the graph.db is built at runtime by this step, so the default reaching gt-run-proof
IS the switch.

FIX: flip both substrate_proof.sh defaults to `${GT_SEM_BODY:-1}` (default-on everywhere the brief
is generated, regardless of caller), and -- for explicitness in the live workflow -- carry the
GT_SEM_BODY=1 and GT_CONTENT_LEG=1 exports on the "GT substrate proof" step env.

MUTATION: revert either default back to `${GT_SEM_BODY:-0}` -> the `:-1` count drops to 1 and the
`:-0` count rises to 1 -> both assertions below redden.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"
_SP = _ROOT / "scripts" / "ci" / "substrate_proof.sh"


def _substrate_proof_step() -> dict:
    # select the step that RUNS the indexer script -- NOT the look-alike "...before substrate
    # proof" dataset-prep step, whose name also contains the phrase but carries no env.
    doc = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []) or []:
            if isinstance(step, dict) and "substrate_proof.sh" in (step.get("run") or ""):
                return step
    raise AssertionError("no step runs scripts/ci/substrate_proof.sh in swebench_live_lite_full.yml")


def test_substrate_proof_defaults_sem_body_to_one_on_both_branches() -> None:
    sh = _SP.read_text(encoding="utf-8")
    on = sh.count('GT_SEM_BODY="${GT_SEM_BODY:-1}"')
    off = sh.count('GT_SEM_BODY="${GT_SEM_BODY:-0}"')
    assert on == 2, (
        "substrate_proof.sh must default GT_SEM_BODY to 1 on BOTH the deepswe (:637) and else "
        f"(:675) gt-run-proof invocations; found {on} `${{GT_SEM_BODY:-1}}` forwards (want 2). "
        "The brief graph.db is built by THIS step, so the default IS the body-semantic switch."
    )
    assert off == 0, (
        f"substrate_proof.sh still projects `${{GT_SEM_BODY:-0}}` on {off} line(s) -- the "
        "body-semantic leg would default OFF at brief generation. Flip every default to :-1."
    )


def test_substrate_proof_step_carries_sem_body_and_content_leg() -> None:
    env = _substrate_proof_step().get("env") or {}
    assert str(env.get("GT_SEM_BODY", "")).strip() == "1", (
        "the substrate-proof step env must set GT_SEM_BODY=1 so the base graph carries body channels; "
        f"got {env.get('GT_SEM_BODY')!r}"
    )
    assert str(env.get("GT_CONTENT_LEG", "")).strip() == "1", (
        "the substrate-proof step env must set GT_CONTENT_LEG=1 (explicit content-leg on at brief "
        f"generation); got {env.get('GT_CONTENT_LEG')!r}"
    )


def test_brief_form_flags_projected_into_the_proof_container():
    """Smoke-#3 finding (run 29234478242): the brief GENERATOR flags must survive ALL THREE
    projection links (workflow step env -> substrate_proof.sh env -> docker -e) or the brief
    ships the legacy verbose form. Pins the docker -e projections (both gt-run-proof sites)
    + the workflow step env."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sh = (root / "scripts" / "ci" / "substrate_proof.sh").read_text(encoding="utf-8")
    assert sh.count('-e GT_BRIEF_MINIMAL="${GT_BRIEF_MINIMAL:-1}"') == 2
    assert sh.count('-e GT_BRIEF_NATIVE="${GT_BRIEF_NATIVE:-1}"') == 2
    wf = (root / ".github" / "workflows" / "swebench_live_lite_full.yml").read_text(encoding="utf-8")
    assert "GT_BRIEF_MINIMAL: '1'" in wf and "GT_BRIEF_NATIVE: '1'" in wf
