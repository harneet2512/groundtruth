"""W1a — the producer-attestation STORE must be harvested under the graded task dir.

Root defect (validation run 29553735978): every GT-on delivery persists immutable
``ProducerAttestation`` bundles to ``<GT_C_OUT>/producer_attestations`` — which, with
``GT_C_OUT`` unset in the docker-run agent container, defaults to ``/gt_out/producer_
attestations`` = host ``/tmp/gt_out/producer_attestations`` (the ``/gt_out`` bind-mount).
The offline grader (``attestation_join.load_attestations``) globs
``task_dir/**/producer_attestations/index/*/entry.json``. But the graded task dir is
``/tmp/gt/<task>`` ( = ``$TASK_ARTIFACT_ROOT`` ) in-workflow and the uploaded
``trial_results/`` ( = ``ll-full-<task>`` ) offline — NEITHER of which is the
``/tmp/gt_out`` bind-mount. The Collect-results step copied an enumerated file list and
never the store DIRECTORY, so the store never reached either graded location. Result:
the ``correct_info`` FACT join is structurally impossible for all delivery features
(store persisted in-container, dropped at harvest).

Two layers of proof:

1. BEHAVIORAL co-location contract (reader/writer): a store persisted at the bind-mount
   SIBLING of the graded task dir is invisible to the join (reproduces the bug); the same
   store copied UNDER the task dir joins and drives truth PASS (the fix outcome). Uses the
   REAL submit-refusal factory + REAL ``persist_attestation`` store — never hand-written
   JSON.

2. WORKFLOW WIRING: the Collect-results step of ``swebench_live_lite_full.yml`` must copy
   ``/tmp/gt_out/producer_attestations`` into BOTH ``trial_results/`` (offline ll-full
   artifact) AND ``$TASK_ARTIFACT_ROOT`` (in-workflow gt_feature_metrics run_dir), BEFORE
   the ``gt_feature_metrics.py`` invocation that reads it. RED before the YAML fix.

Documented biting mutations (each verified to fail a test, then restored):

  * MUTATION H-A — delete the ``trial_results/producer_attestations`` cp line from the
    Collect step. ``test_workflow_collect_harvests_store_into_ll_full`` goes RED (the
    offline grading harvest gap returns).
  * MUTATION H-B — delete the ``$TASK_ARTIFACT_ROOT/producer_attestations`` cp line.
    ``test_workflow_collect_harvests_store_into_task_root`` goes RED (the in-workflow join
    gap returns).
  * MUTATION H-C — in ``_harvest_under`` copy to a SIBLING dir instead of under task_dir.
    ``test_harvest_under_task_dir_makes_join_possible`` goes RED: the recursive glob never
    reaches the store, so no class joins (identical to the pre-fix production reality).
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import attestation_join as aj  # noqa: E402
from groundtruth.runtime.attestation_store import persist_attestation  # noqa: E402
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    build_lineage,
    lineage_ledger_extra,
)
from groundtruth.runtime.completion_control import (  # noqa: E402
    submit_refusal_candidate_id,
)
from groundtruth.runtime.submit_attestation import (  # noqa: E402
    finalize_submit_refusal_attestation,
)
from groundtruth.runtime.submit_gate import gate_verdict  # noqa: E402

_WORKFLOW = ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"
_REFUSAL = "a covering test is failing — re-run the repo's own tests and fix before submitting"


def _seal(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _real_attestation():
    """A complete, valid submit_refusal attestation via the REAL factory + gate kernel."""
    verdict = gate_verdict(
        covering={"verdict": "fail", "reason": "red", "failing_test_names": ["t_x"]},
        hygiene=None,
        bounce_count=0,
        max_bounces=1,
    )
    cid = submit_refusal_candidate_id(_REFUSAL)
    seal = _seal(_REFUSAL)
    final = finalize_submit_refusal_attestation(
        verdict, refusal_text=_REFUSAL, candidate_id=cid, delivery_seal=seal
    )
    return final.attestation, final.artifact_mapping(), cid, seal


def _delivered_row(candidate_id: str, seal: str) -> dict:
    return {
        "layer": "submit_refusal",
        "event_type": "submit",
        "outcome": "delivered",
        "content_sha256_16": seal,
        "candidate_id": candidate_id,
        # J6: the seam stamps typed FACT lineage on the delivered submit-refusal row.
        **lineage_ledger_extra(build_lineage(
            runtime_producer_id="submit_gate", evidence_type="submit_refusal",
            actual_event="submit")),
    }


def _persist_bind_mount(gt_out: Path):
    """Persist exactly as the seam does: <GT_C_OUT default /gt_out>/producer_attestations."""
    attestation, artifacts, cid, seal = _real_attestation()
    persist_attestation(attestation, artifacts, gt_out / "producer_attestations")
    return cid, seal


def _harvest_under(gt_out: Path, dest_dir: Path) -> None:
    """Model the FIXED Collect-step harvest: copy the store UNDER the graded task dir."""
    shutil.copytree(
        gt_out / "producer_attestations", dest_dir / "producer_attestations"
    )


# --------------------------------------------------------------------------- #
# Behavioral co-location contract.
# --------------------------------------------------------------------------- #
def test_store_on_bind_mount_sibling_is_invisible_to_join(tmp_path: Path) -> None:
    """Reproduces the production bug: store persisted on the /gt_out bind-mount, graded
    task dir is a SIBLING → the recursive glob never sees it → nothing joins."""
    gt_out = tmp_path / "gt_out"            # the /gt_out bind-mount (host /tmp/gt_out)
    task_dir = tmp_path / "gt" / "task__x"  # the graded dir (/tmp/gt/<task>)
    task_dir.mkdir(parents=True)
    cid, seal = _persist_bind_mount(gt_out)

    load = aj.load_attestations(str(task_dir))
    joins = aj.join_truth(load.attestations, [_delivered_row(cid, seal)])

    assert load.attestations == ()          # store never harvested under task_dir
    assert joins == {}                      # correct_info can never move


def test_harvest_under_task_dir_makes_join_possible(tmp_path: Path) -> None:
    """The fix outcome: harvesting the store UNDER the graded task dir restores the join.
    MUTATION H-C (copy to a sibling instead) bites here."""
    gt_out = tmp_path / "gt_out"
    task_dir = tmp_path / "gt" / "task__x"
    task_dir.mkdir(parents=True)
    cid, seal = _persist_bind_mount(gt_out)

    _harvest_under(gt_out, task_dir)        # <-- the Collect-step copy the YAML now performs

    load = aj.load_attestations(str(task_dir))
    joins = aj.join_truth(load.attestations, [_delivered_row(cid, seal)])

    assert len(load.attestations) == 1
    assert set(joins) == {"submit_refusal"}
    tj = joins["submit_refusal"]
    assert tj.truth is True
    assert tj.authority is True


# --------------------------------------------------------------------------- #
# Workflow wiring: the Collect step must perform the harvest, before grading.
# --------------------------------------------------------------------------- #
def _collect_step_body() -> str:
    text = _WORKFLOW.read_text(encoding="utf-8")
    start = text.index("name: Collect results")
    # Bound the slice at the first grader invocation that reads the store.
    end = text.index("gt_feature_metrics.py", start)
    return text[start:end]


def test_workflow_collect_harvests_store_into_ll_full() -> None:
    """Offline grading reads the uploaded ll-full-<task> = trial_results/. RED = MUTATION H-A."""
    body = _collect_step_body()
    assert "trial_results/producer_attestations" in body, (
        "Collect step must copy /tmp/gt_out/producer_attestations into trial_results/ "
        "so the store ships inside the ll-full-<task> artifact for offline grading"
    )
    assert "/tmp/gt_out/producer_attestations" in body


def test_workflow_collect_harvests_store_into_task_root() -> None:
    """In-workflow gt_feature_metrics.py reads run_dir=/tmp/gt, task_dir=$TASK_ARTIFACT_ROOT.
    RED = MUTATION H-B."""
    body = _collect_step_body()
    assert "TASK_ARTIFACT_ROOT/producer_attestations" in body, (
        "Collect step must copy the store into $TASK_ARTIFACT_ROOT so the in-workflow "
        "gt_feature_metrics join (run_dir=/tmp/gt) can discover it"
    )
