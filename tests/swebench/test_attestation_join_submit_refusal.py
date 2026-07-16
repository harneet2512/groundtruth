"""RED-first tests for the submit_refusal producer-attestation -> ledger truth join.

Mirrors ``test_attestation_join.py``: fixtures use the REAL
``submit_attestation.finalize_submit_refusal_attestation`` factory (bound to the REAL
``submit_gate.gate_verdict`` kernel) and the REAL ``persist_attestation`` store — never
hand-written JSON. The delivered ledger row carries the EXACT ``(candidate_id,
content_sha256_16)`` identity the seam stamps (``completion_control.submit_refusal_candidate_id``
+ the truncated ``surrogatepass`` sha the runtime ledger writes).

Documented biting mutations (each verified to fail a test, then restored):

  * MUTATION SR-A — attestation_join.join_truth: relax the identity to candidate_id only
    (drop the ``content_sha256_16``/``delivery_seal`` leg).
    ``test_submit_refusal_seal_mismatch_produces_no_join`` then WRONGLY joins the
    seal-mismatched row → ``submit_refusal`` appears with ``truth=True``. Bite confirmed.

  * MUTATION SR-B — attestation_join.ATTESTED_FACT_CLASSES: remove ``"submit_refusal"``
    from the tuple. ``test_collect_task_submit_refusal_correct_info_goes_true`` then keeps
    ``correct_info`` at ``None`` (the grader loop never applies the join for the class) —
    the gate never moves even though a valid PASS bundle joined the delivered row. Bite
    confirmed.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import attestation_join as aj  # noqa: E402
import gt_feature_metrics as gfm  # noqa: E402
from groundtruth.runtime.attestation_store import persist_attestation  # noqa: E402
from groundtruth.runtime.completion_control import (  # noqa: E402
    submit_refusal_candidate_id,
)
from groundtruth.runtime.submit_attestation import (  # noqa: E402
    finalize_submit_refusal_attestation,
)
from groundtruth.runtime.submit_gate import gate_verdict  # noqa: E402

_REFUSAL = "a covering test is failing — re-run the repo's own tests and fix before submitting"


def _seal(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _block_attestation(refusal: str = _REFUSAL):
    """A complete submit_refusal attestation for a genuine covering BLOCK + its artifacts."""
    verdict = gate_verdict(
        covering={"verdict": "fail", "reason": "red", "failing_test_names": ["t_x"]},
        hygiene=None,
        bounce_count=0,
        max_bounces=1,
    )
    cid = submit_refusal_candidate_id(refusal)
    seal = _seal(refusal)
    final = finalize_submit_refusal_attestation(
        verdict, refusal_text=refusal, candidate_id=cid, delivery_seal=seal
    )
    return final.attestation, final.artifact_mapping(), cid, seal


def _delivered_row(candidate_id: str, seal: str, payload: str = _REFUSAL) -> dict:
    """A DELIVERED submit_refusal ledger row exactly as the seam writes it."""
    return {
        "layer": "submit_refusal",
        "event_type": "submit",
        "file_path": "",
        "outcome": "delivered",
        "reason": "",
        "chars_delivered": len(payload),
        "iteration": 7,
        "content_sha256_16": seal,
        "seal_scope": "block",
        "candidate_id": candidate_id,
    }


def _persist(task_dir: Path, attestation, artifacts: dict) -> Path:
    root = task_dir / "art" / "producer_attestations"
    persist_attestation(attestation, artifacts, root)
    return root


# --------------------------------------------------------------------------- #
# join_truth
# --------------------------------------------------------------------------- #
def test_submit_refusal_valid_join_yields_truth_true(tmp_path: Path) -> None:
    attestation, artifacts, cid, seal = _block_attestation()
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row(cid, seal)]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert set(joins) == {"submit_refusal"}
    tj = joins["submit_refusal"]
    assert tj.truth is True
    assert tj.authority is True  # rides the truth-PASS join
    # freshness is honestly UNMEASURED (no patch/graph revision in the gate verdict).
    assert tj.freshness is None
    assert tj.attestation_count == 1
    assert tj.joined_delivery_row_indices == (0,)


def test_submit_refusal_seal_mismatch_produces_no_join(tmp_path: Path) -> None:
    # MUTATION SR-A bites here.
    attestation, artifacts, cid, _ = _block_attestation()
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row(cid, "f" * 16)]  # seal differs from the attested seal

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert "submit_refusal" not in joins
    assert joins == {}


def test_submit_refusal_candidate_mismatch_produces_no_join(tmp_path: Path) -> None:
    attestation, artifacts, _, seal = _block_attestation()
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("submit_refusal:other", seal)]  # candidate differs

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert joins == {}


def test_submit_refusal_non_delivered_row_does_not_join(tmp_path: Path) -> None:
    attestation, artifacts, cid, seal = _block_attestation()
    _persist(tmp_path, attestation, artifacts)
    row = _delivered_row(cid, seal)
    row["outcome"] = "allow"  # a clean-allow submit, not a delivered refusal
    rows = [row]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert joins == {}


# --------------------------------------------------------------------------- #
# End-to-end through collect_task: the submit_refusal gate MOVES.
# --------------------------------------------------------------------------- #
def _write_trajectory(task_dir: Path) -> None:
    (task_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({
            "messages": [{"role": "user", "content": "fixture task"}],
            "info": {"submission": ""},
            "trajectory_format": "mini-swe-agent",
        }),
        encoding="utf-8",
    )


def _write_ledger(task_dir: Path, rows: list[dict]) -> None:
    (task_dir / "gt_runtime_ledger_synthetic.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def test_collect_task_submit_refusal_correct_info_goes_true(tmp_path: Path) -> None:
    # THE load-bearing test: a valid PASS bundle + a matching DELIVERED submit_refusal row
    # drives correct_info (truth ∧ authority) to True. MUTATION SR-B (drop submit_refusal
    # from ATTESTED_FACT_CLASSES) keeps correct_info at None.
    attestation, artifacts, cid, seal = _block_attestation()
    _persist(tmp_path, attestation, artifacts)
    _write_trajectory(tmp_path)
    _write_ledger(tmp_path, [_delivered_row(cid, seal)])

    record = gfm.collect_task("synthetic__submit-refusal-pass", str(tmp_path), profile="2")

    fc = record["fact_classes"]["submit_refusal"]
    assert fc["truth_valid"]["value"] is True
    assert fc["authority_valid"]["value"] is True

    readiness = record["ss_features"]["submit_refusal"]["ss_readiness"]
    assert readiness["gates"]["correct_info"] is True  # the gate MOVES

    diag = record["ss_integrity"]["attestation_join"]
    assert "submit_refusal" in diag["applied_truth_overrides"]
    assert "submit_refusal" in diag["applied_authority_overrides"]
    assert diag["joined_fact_classes"]["submit_refusal"]["authority"] is True


def test_collect_task_submit_refusal_unmeasured_without_attestation(tmp_path: Path) -> None:
    # BYTE-IDENTITY / absent-artifact pin: a DELIVERED submit_refusal row with NO persisted
    # attestation keeps truth/authority at their honest hard-wired UNMEASURED — the override
    # is inert without a validated joined attestation.
    cid = submit_refusal_candidate_id(_REFUSAL)
    seal = _seal(_REFUSAL)
    _write_trajectory(tmp_path)
    _write_ledger(tmp_path, [_delivered_row(cid, seal)])

    record = gfm.collect_task("synthetic__submit-refusal-noatt", str(tmp_path), profile="2")

    fc = record["fact_classes"]["submit_refusal"]
    assert fc["truth_valid"]["value"] is None
    assert fc["authority_valid"]["value"] is None
    assert record["ss_features"]["submit_refusal"]["ss_readiness"]["gates"]["correct_info"] is None
    assert record["ss_integrity"]["attestation_join"]["applied_truth_overrides"] == []
