"""RED-first tests for B-SEAL: the step-0 brief localization block attestation + join.

Covers the pure factory (``brief_attestation.finalize_localization_attestation``), the
compound-row block-lineage join extension (``attestation_join._index_block_lineage``),
and the end-to-end truth/authority population for the ``localization`` fact class.

Fixtures use the REAL dataclasses + REAL ``attestation_store.persist_attestation`` and a
REAL compound brief ledger row shaped exactly like ``gt_headless_runner`` writes it.

Documented biting mutations (each verified to fail a test, then restored):

  * MUTATION L-A — brief_attestation: attest truth PASS regardless of ``witness_verified``
    (drop the ``truth_complete`` gate → always complete). ``test_unverified_candidate_is_unmeasured``
    then WRONGLY reports ``truth_verdict == PASS`` for an unverified candidate. Bite confirmed.

  * MUTATION L-B — attestation_join._delivered_row_index: remove the
    ``_index_block_lineage(...)`` call (index only the top-level seal).
    ``test_compound_brief_block_joins_localization`` then finds NO join for the compound
    brief row (it has no top-level candidate_id) → ``localization`` absent. Bite confirmed.

  * MUTATION L-C — brief_attestation: bind an attestation to a seal that is not the
    delivered block's hash (drop the ``full[:16] != seal`` guard).
    ``test_seal_prefix_mismatch_returns_none`` then builds a bundle whose seal cannot
    join the delivered block. Bite confirmed.

  * MUTATION L-D — attestation_join.ATTESTED_FACT_CLASSES: remove ``"localization"``.
    ``test_collect_task_localization_correct_info_goes_true`` then keeps
    ``localization`` truth/authority UNMEASURED → the gate never moves. Bite confirmed.
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
from groundtruth.runtime.brief_attestation import (  # noqa: E402
    finalize_localization_attestation,
)
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    build_lineage,
    lineage_to_dict,
)
from groundtruth.runtime.producer_attestation import (  # noqa: E402
    PASS,
    UNMEASURED,
    canonical_bytes,
    validate,
)


# --------------------------------------------------------------------------- #
# Helpers — build a real delivered block seal + a compound brief ledger row.
# --------------------------------------------------------------------------- #
def _block_seal(block_bytes: str) -> tuple[str, str]:
    """Return ``(full_sha256_hex, content_sha256_16)`` over the exact block bytes."""
    digest = hashlib.sha256(block_bytes.encode("utf-8", "surrogatepass")).hexdigest()
    return digest, digest[:16]


def _compound_brief_row(candidate_id: str, seal16: str, *, fact_class: str = "localization") -> dict:
    """A DELIVERED compound step-0 brief row with one sealed block under block_lineage."""
    return {
        "layer": "brief.task",
        "event_type": "task_start",
        "file_path": "",
        "outcome": "delivered",
        "reason": "step0_brief_prepend",
        "chars_delivered": 475,
        "iteration": 0,
        "content_sha256_16": "6b0b3396854b0a26",  # whole-brief seal (NOT a block)
        "seal_scope": "block",
        "compound_delivery": True,
        "compound_lineage_schema": "gt.compound_feature_lineage.v1",
        "block_lineage": [
            {
                "block_id": "localization-header:candidate-1",
                "candidate_id": candidate_id,
                "char_span": [118, 161],
                "chars_delivered": 43,
                "content_sha256_16": seal16,
                "declared_fact_class": fact_class,
                "label": "localization-header",
                "lineage_status": "REGISTERED",
                # J6: the real writer (_brief_delivery_extra) always pairs a REGISTERED
                # status with the registered lineage dict; a registered block must carry
                # both (a bare status alone can no longer seat a truth join).
                "lineage": lineage_to_dict(build_lineage(
                    runtime_producer_id="v1r_brief",
                    evidence_type=fact_class,
                    actual_event="task_start",
                )),
                "transport_producer_id": "v1r_brief",
            }
        ],
    }


def _localization_attestation(candidate_id: str, full: str, seal16: str, *, verified: bool = True):
    return finalize_localization_attestation(
        candidate_id=candidate_id,
        delivery_seal=seal16,
        block_content_sha256=full,
        path="sh.py",
        rank=1,
        witness="TimeoutException called by wait [CALLS]",
        witness_verified=verified,
    )


def _persist(task_dir: Path, final) -> None:
    root = task_dir / "art" / "producer_attestations"
    persist_attestation(final.attestation, final.artifact_mapping(), root)


# --------------------------------------------------------------------------- #
# Pure factory.
# --------------------------------------------------------------------------- #
def test_verified_candidate_builds_valid_pass_attestation() -> None:
    full, seal16 = _block_seal("1. sh.py  (TimeoutException called by wait)")
    final = _localization_attestation("localization:sh.py", full, seal16)
    assert final is not None
    assert validate(final.attestation) == ()
    assert final.attestation.truth_verdict == PASS
    # freshness is honestly dark (no runtime graph sub-revision proof).
    assert final.attestation.freshness_verdict == UNMEASURED
    assert final.attestation.evidence_type == "localization"
    assert final.attestation.registered_producer_id == "v1r_brief"
    assert final.attestation.delivery_seal == seal16


def test_unverified_candidate_is_unmeasured() -> None:
    # MUTATION L-A bites here.
    full, seal16 = _block_seal("1. mystery.py")
    final = _localization_attestation("localization:mystery.py", full, seal16, verified=False)
    assert final is not None
    assert validate(final.attestation) == ()
    assert final.attestation.truth_verdict == UNMEASURED


def test_build_is_byte_identical_across_two_independent_acquisitions() -> None:
    # The B-ACQ D1/D2/D6/D7 invariant: a pure function of hashed inputs — two builds match.
    full, seal16 = _block_seal("1. sh.py  (TimeoutException called by wait)")
    a = _localization_attestation("localization:sh.py", full, seal16)
    b = _localization_attestation("localization:sh.py", full, seal16)
    assert canonical_bytes(a.attestation) == canonical_bytes(b.attestation)
    assert a.artifacts == b.artifacts


def test_seal_not_16_hex_returns_none() -> None:
    full, _ = _block_seal("x")
    assert _localization_attestation("localization:sh.py", full, "NOT_HEX") is None


def test_seal_prefix_mismatch_returns_none() -> None:
    # MUTATION L-C bites here: the seal must be the delivered block's own hash prefix.
    full, _ = _block_seal("1. sh.py")
    assert finalize_localization_attestation(
        candidate_id="localization:sh.py",
        delivery_seal="a" * 16,  # not a prefix of full
        block_content_sha256=full,
        path="sh.py",
        rank=1,
        witness="w",
        witness_verified=True,
    ) is None


def test_empty_candidate_returns_none() -> None:
    full, seal16 = _block_seal("1. sh.py")
    assert _localization_attestation("", full, seal16) is None


# --------------------------------------------------------------------------- #
# Join over a compound brief row.
# --------------------------------------------------------------------------- #
def test_compound_brief_block_joins_localization(tmp_path: Path) -> None:
    # MUTATION L-B bites here: without _index_block_lineage the compound row is skipped.
    full, seal16 = _block_seal("1. sh.py  (TimeoutException called by wait)")
    final = _localization_attestation("localization:sh.py", full, seal16)
    _persist(tmp_path, final)
    rows = [_compound_brief_row("localization:sh.py", seal16)]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert set(joins) == {"localization"}
    tj = joins["localization"]
    assert tj.truth is True
    assert tj.authority is True
    assert tj.freshness is None  # honest-dark
    assert tj.joined_delivery_row_indices == (0,)


def test_seal_mismatch_on_block_does_not_join(tmp_path: Path) -> None:
    full, seal16 = _block_seal("1. sh.py")
    final = _localization_attestation("localization:sh.py", full, seal16)
    _persist(tmp_path, final)
    # the delivered block carries a DIFFERENT seal → no join.
    rows = [_compound_brief_row("localization:sh.py", "f" * 16)]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)
    assert joins == {}


def test_unattested_compound_row_stays_unmeasured(tmp_path: Path) -> None:
    # An old/real compound brief row with NO persisted attestation joins nothing:
    # localization stays honestly UNMEASURED (the offline-old-rows invariant).
    full, seal16 = _block_seal("1. sh.py")
    rows = [_compound_brief_row("localization:sh.py", seal16)]
    joins = aj.join_truth((), rows)  # no attestations loaded
    assert joins == {}


def test_non_compound_rows_unaffected(tmp_path: Path) -> None:
    # A plain lane row without block_lineage indexes exactly as before (top-level seal).
    full, seal16 = _block_seal("1. sh.py")
    final = _localization_attestation("localization:sh.py", full, seal16)
    _persist(tmp_path, final)
    rows = [{
        "layer": "l3.contract", "event_type": "file_view", "outcome": "delivered",
        "chars_delivered": 10, "content_sha256_16": "c" * 16, "candidate_id": "contract:x",
        "file_path": "src/other.py",
    }]
    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)
    assert joins == {}  # the localization attestation joins no non-compound row


# --------------------------------------------------------------------------- #
# End-to-end through collect_task: the localization correct_info gate MOVES.
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


def test_collect_task_localization_correct_info_goes_true(tmp_path: Path) -> None:
    # THE load-bearing test: a valid PASS localization brief-block bundle + a matching
    # DELIVERED compound row drive truth ∧ authority to True for `localization`.
    # MUTATION L-D (remove "localization" from ATTESTED_FACT_CLASSES) keeps it UNMEASURED.
    full, seal16 = _block_seal("1. sh.py  (TimeoutException called by wait)")
    final = _localization_attestation("localization:sh.py", full, seal16)
    _persist(tmp_path, final)
    _write_trajectory(tmp_path)
    _write_ledger(tmp_path, [_compound_brief_row("localization:sh.py", seal16)])

    record = gfm.collect_task("synthetic__brief-loc-pass", str(tmp_path), profile="2")

    fc = record["fact_classes"]["localization"]
    assert fc["truth_valid"]["value"] is True
    assert fc["authority_valid"]["value"] is True

    diag = record["ss_integrity"]["attestation_join"]
    assert "localization" in diag["applied_truth_overrides"]
    assert "localization" in diag["applied_authority_overrides"]


def test_collect_task_unverified_localization_stays_unmeasured(tmp_path: Path) -> None:
    # An UNMEASURED-truth bundle (unverified candidate) joins but never mints truth.
    full, seal16 = _block_seal("1. mystery.py")
    final = _localization_attestation("localization:mystery.py", full, seal16, verified=False)
    _persist(tmp_path, final)
    _write_trajectory(tmp_path)
    _write_ledger(tmp_path, [_compound_brief_row("localization:mystery.py", seal16)])

    record = gfm.collect_task("synthetic__brief-loc-unmeasured", str(tmp_path), profile="2")

    fc = record["fact_classes"]["localization"]
    assert fc["truth_valid"]["value"] is not True
    assert fc["authority_valid"]["value"] is not True
