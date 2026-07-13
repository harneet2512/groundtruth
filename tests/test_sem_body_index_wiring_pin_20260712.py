"""Pin: Super Mode (Profile-2) must mine body channels at INDEX time, or GT_SEM_BODY fails closed.

GAP (measured on substrate f8908805 + confirmed structural on bbd0a884): the receipt's base
graph.db is built at runtime by scripts/ci/substrate_proof.sh (gt-run-proof, writes /tmp/gt/graph.db
== the agent container's read-only GT_HOST_GRAPH_DB). Body-channel mining (body_terms /
string_literals) is index-time gated on GT_SEM_BODY (parser.go semBodyMiningEnabled; graph is
byte-identical when off). substrate_proof.sh:637 forwards `${GT_SEM_BODY:-0}` to the proof
container, and the workflow step never set GT_SEM_BODY -> default 0 -> sem_body_rows=0 -> the
Profile-2 GT_SEM_BODY member fails closed on EVERY substrate. An image rebake cannot fix it (the
graph is built here at runtime, not baked).

Two-sided seam pin:
  * the swebench_live_lite_full.yml "GT substrate proof" step env MUST set GT_SEM_BODY=1 (a revert
    to unset/0 reddens this) -- so gt-run-proof mines the channels into the base graph;
  * substrate_proof.sh MUST forward that host value to the proof container (a rename/removal of the
    `-e GT_SEM_BODY="${GT_SEM_BODY:-1}"` line reddens this) -- so the step env actually reaches the
    indexer. (Default hardened :-0 -> :-1 on 2026-07-13, S4; see test_sem_body_brief_gen_pin_20260713.)

Once sem_body_rows>0, the existing receipt -> rl_profile --emit-exports -> eval cascade propagates
GT_SEM_BODY=1 into the agent container ONLY when the receipt admits it (fail-closed preserved).
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


def test_substrate_proof_step_enables_sem_body() -> None:
    step = _substrate_proof_step()
    val = str((step.get("env") or {}).get("GT_SEM_BODY", "")).strip()
    assert val == "1", (
        "the substrate-proof step env must set GT_SEM_BODY=1 so the base graph carries body "
        f"channels; got {val!r}. Without it sem_body_rows=0 and Profile-2 GT_SEM_BODY fails closed"
    )


def test_substrate_proof_step_runs_the_indexer_script() -> None:
    # the env only matters if this step actually runs substrate_proof.sh (which forwards it).
    step = _substrate_proof_step()
    assert "substrate_proof.sh" in (step.get("run") or ""), "step must run scripts/ci/substrate_proof.sh"


def test_substrate_proof_forwards_sem_body_to_the_proof_container() -> None:
    # 2026-07-13 (S4): the default was hardened from :-0 to :-1 so the body-semantic leg is ON by
    # default at brief generation regardless of caller. The forward line must still exist so the
    # step env reaches the indexer; the default value is now 1 (see test_sem_body_brief_gen_pin_20260713).
    sh = _SP.read_text(encoding="utf-8")
    assert 'GT_SEM_BODY="${GT_SEM_BODY:-1}"' in sh, (
        "substrate_proof.sh must forward -e GT_SEM_BODY=\"${GT_SEM_BODY:-1}\" to gt-run-proof; "
        "without this line the step env never reaches the indexer"
    )
