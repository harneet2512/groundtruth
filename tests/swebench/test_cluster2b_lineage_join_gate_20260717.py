"""Cluster-2b Defect-6 RED tests: the delivery-side truth join requires typed lineage.

Phase B: a DELIVERED ledger row/block may seat a truth join ONLY when it stamps valid typed
FACT lineage (native/gateway) or REGISTERED compound block lineage (brief). An identity-
complete row that lacks lineage is NOT seated — and surfaces a NAMED reason distinct from a
plain candidate/seal miss (which yields nothing at all).

Documented biting mutations (each verified to fail a test, then restored):

  * MUTATION L6-A — attestation_join._row_has_registered_lineage: make it return True
    unconditionally (drop the lineage gate). ``test_lineageless_row_does_not_seat_join``
    then WRONGLY seats the join on a row with no lineage → truth True. Bite confirmed.

  * MUTATION L6-B — attestation_join._block_is_registered: drop the
    ``isinstance(block.get("lineage"), dict)`` leg (accept a bare status string).
    ``test_forged_block_lineage_is_rejected`` then seats a forged block that claims
    REGISTERED without the registered lineage dict. Bite confirmed.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import attestation_join as aj  # noqa: E402
from groundtruth.runtime.attestation_store import persist_attestation  # noqa: E402
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    build_lineage,
    lineage_ledger_extra,
    lineage_to_dict,
)
from groundtruth.runtime.producer_attestation import (  # noqa: E402
    ATTESTATION_SCHEMA,
    FRESHNESS,
    PASS,
    ArtifactRef,
    DecisionBinding,
    PredicateAttestation,
    ProducerAttestation,
    ProofRef,
)

_SEAL = "a" * 16
_CID = "edit:src/core.py:7"


def _syntax_attestation(candidate_id=_CID, delivery_seal=_SEAL):
    source = b'{"verdict":"syntax_error","line":12}'
    ref = ArtifactRef(
        kind="producer_input", artifact_id="diag.json",
        sha256=hashlib.sha256(source).hexdigest(), revision="edit:7")
    proof = ProofRef("producer_observation", ref, "$.verdict")

    def _p(kind, pid):
        return PredicateAttestation(kind, pid, "edit", "acceptable", "observed", PASS, (proof,))

    att = ProducerAttestation(
        schema=ATTESTATION_SCHEMA, evidence_type="syntax_result",
        runtime_producer_id="edit_check", registered_producer_id="edit_check",
        candidate_id=candidate_id, delivery_seal=delivery_seal, source_artifacts=(ref,),
        truth_predicates=(_p("TRUTH", "t.syntax"),),
        freshness_predicates=(_p(FRESHNESS, "f.syntax"),),
        decision=DecisionBinding("is the edit acceptable", "edit_result", "edit_result"))
    return att, {"diag.json": source}


def _row(candidate_id, seal, *, with_lineage: bool) -> dict:
    row = {
        "layer": "edit.syntax", "event_type": "edit_result", "file_path": "src/core.py",
        "outcome": "delivered", "chars_delivered": 36, "iteration": 5,
        "content_sha256_16": seal, "seal_scope": "block", "candidate_id": candidate_id,
    }
    if with_lineage:
        row.update(lineage_ledger_extra(build_lineage(
            runtime_producer_id="edit_check", evidence_type="syntax_result",
            actual_event="edit_result")))
    return row


def _persist(task_dir: Path, att, artifacts) -> None:
    persist_attestation(att, artifacts, task_dir / "art" / "producer_attestations")


# --------------------------------------------------------------------------- #
# Phase B gate — typed FACT lineage required.
# --------------------------------------------------------------------------- #
def test_lineage_bearing_row_seats_join(tmp_path: Path) -> None:
    att, art = _syntax_attestation()
    _persist(tmp_path, att, art)
    joins = aj.join_truth(
        aj.load_attestations(str(tmp_path)).attestations,
        [_row(_CID, _SEAL, with_lineage=True)])
    assert joins["syntax_result"].truth is True
    assert joins["syntax_result"].authority is True


def test_lineageless_row_does_not_seat_join(tmp_path: Path) -> None:
    # MUTATION L6-A bites: a lineage-less row must NOT seat a truth join.
    att, art = _syntax_attestation()
    _persist(tmp_path, att, art)
    joins = aj.join_truth(
        aj.load_attestations(str(tmp_path)).attestations,
        [_row(_CID, _SEAL, with_lineage=False)])
    tj = joins["syntax_result"]
    assert tj.truth is None
    assert tj.authority is None
    assert tj.attestation_count == 0
    # The named reason is surfaced and is DISTINCT from a candidate/seal miss.
    assert tj.reasons
    assert any("lineage_rejected:delivered_row_missing_registered_lineage" in r
               for r in tj.reasons)


def test_plain_candidate_miss_surfaces_nothing(tmp_path: Path) -> None:
    # A pure candidate/seal miss (no matching delivered row at all) yields NO entry —
    # the lineage-rejection surfacing must not fire, so the two are distinguishable.
    att, art = _syntax_attestation()
    _persist(tmp_path, att, art)
    joins = aj.join_truth(
        aj.load_attestations(str(tmp_path)).attestations,
        [_row(_CID, "f" * 16, with_lineage=True)])  # seal differs entirely
    assert joins == {}


def test_wrong_lineage_schema_is_rejected(tmp_path: Path) -> None:
    att, art = _syntax_attestation()
    _persist(tmp_path, att, art)
    row = _row(_CID, _SEAL, with_lineage=True)
    row["lineage_schema"] = "gt.some_other.v9"  # not the feature lineage schema
    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, [row])
    assert joins["syntax_result"].truth is None
    assert any("lineage_rejected" in r for r in joins["syntax_result"].reasons)


def test_lineage_schema_without_fact_class_is_rejected(tmp_path: Path) -> None:
    att, art = _syntax_attestation()
    _persist(tmp_path, att, art)
    row = _row(_CID, _SEAL, with_lineage=True)
    row["fact_class"] = ""  # schema present but no fact class -> not typed FACT lineage
    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, [row])
    assert joins["syntax_result"].truth is None


# --------------------------------------------------------------------------- #
# Phase B gate — registered block lineage required; forged block rejected.
# --------------------------------------------------------------------------- #
def _brief_block_row(candidate_id: str, seal16: str, *, registered: bool,
                     with_lineage_dict: bool = True) -> dict:
    block = {
        "block_id": "localization-header:1",
        "candidate_id": candidate_id,
        "char_span": [0, 40],
        "content_sha256_16": seal16,
        "declared_fact_class": "localization",
        "label": "localization-header",
        "lineage_status": "REGISTERED" if registered else "UNREGISTERED_BLOCK_LABEL",
    }
    if registered and with_lineage_dict:
        block["lineage"] = lineage_to_dict(build_lineage(
            runtime_producer_id="v1r_brief", evidence_type="localization",
            actual_event="task_start"))
    return {
        "layer": "brief.task", "event_type": "task_start", "file_path": "",
        "outcome": "delivered", "chars_delivered": 475, "iteration": 0,
        "content_sha256_16": "6b0b3396854b0a26", "seal_scope": "block",
        "compound_delivery": True, "block_lineage": [block],
    }


def _localization_attestation(candidate_id, seal16):
    from groundtruth.runtime.brief_attestation import finalize_localization_attestation
    full = seal16 + "0" * 48  # a 64-hex whose prefix is the seal
    return finalize_localization_attestation(
        candidate_id=candidate_id, delivery_seal=seal16, block_content_sha256=full,
        path="sh.py", rank=1, witness="W [CALLS]", witness_verified=True)


def test_registered_block_seats_join(tmp_path: Path) -> None:
    seal16 = "abcdef0123456789"
    final = _localization_attestation("localization:sh.py", seal16)
    _persist(tmp_path, final.attestation, final.artifact_mapping())
    joins = aj.join_truth(
        aj.load_attestations(str(tmp_path)).attestations,
        [_brief_block_row("localization:sh.py", seal16, registered=True)])
    assert joins["localization"].truth is True


def test_forged_block_lineage_is_rejected(tmp_path: Path) -> None:
    # MUTATION L6-B bites: a block that claims REGISTERED but carries NO registered
    # lineage dict is a forgery and must not seat a join.
    seal16 = "abcdef0123456789"
    final = _localization_attestation("localization:sh.py", seal16)
    _persist(tmp_path, final.attestation, final.artifact_mapping())
    joins = aj.join_truth(
        aj.load_attestations(str(tmp_path)).attestations,
        [_brief_block_row("localization:sh.py", seal16, registered=True,
                          with_lineage_dict=False)])  # forged: status but no dict
    tj = joins["localization"]
    assert tj.truth is None
    assert any("lineage_rejected:block_lineage_unregistered" in r for r in tj.reasons)


def test_unregistered_block_is_rejected(tmp_path: Path) -> None:
    seal16 = "abcdef0123456789"
    final = _localization_attestation("localization:sh.py", seal16)
    _persist(tmp_path, final.attestation, final.artifact_mapping())
    joins = aj.join_truth(
        aj.load_attestations(str(tmp_path)).attestations,
        [_brief_block_row("localization:sh.py", seal16, registered=False)])
    assert joins["localization"].truth is None
    assert any("lineage_rejected:block_lineage_unregistered" in r
               for r in joins["localization"].reasons)


def test_index_returns_rejections_map() -> None:
    idx, rej = aj._delivered_row_index([_row(_CID, _SEAL, with_lineage=False)])
    assert idx == {}
    assert rej[(_CID, _SEAL)] == "delivered_row_missing_registered_lineage"
    idx2, rej2 = aj._delivered_row_index([_row(_CID, _SEAL, with_lineage=True)])
    assert idx2[(_CID, _SEAL)] == [0]
    assert rej2 == {}


def test_registered_profile_member_owner_seats_join(tmp_path: Path) -> None:
    # J6 third form: a reclassified byte owner (GT_SS_COHERENCE_V2 on detect.coherence)
    # carries a registered profile_member owner and NO FACT lineage by product decision
    # (P4). That registered attribution is a valid witness — the row seats the join.
    att, art = _syntax_attestation()
    _persist(tmp_path, att, art)
    row = _row(_CID, _SEAL, with_lineage=False)
    row["profile_member"] = "GT_SS_COHERENCE_V2"  # registered CAP owner, no FACT lineage
    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, [row])
    assert joins["syntax_result"].truth is True


def test_unregistered_profile_member_does_not_seat_join(tmp_path: Path) -> None:
    # A profile_member that is NOT a known CAP id is not a registered attribution.
    att, art = _syntax_attestation()
    _persist(tmp_path, att, art)
    row = _row(_CID, _SEAL, with_lineage=False)
    row["profile_member"] = "GT_NOT_A_REAL_FEATURE"
    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, [row])
    assert joins["syntax_result"].truth is None
    assert any("lineage_rejected" in r for r in joins["syntax_result"].reasons)
