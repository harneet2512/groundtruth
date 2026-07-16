"""RED-first tests for the producer-attestation → delivered-ledger truth join.

Fixtures are built with the REAL ``producer_attestation`` dataclasses and persisted
with the REAL ``attestation_store.persist_attestation`` (never hand-written JSON), so
the canonical-byte / index-key / sha integrity checks exercise the true bundle shape.

Documented biting mutations (each verified to fail a test, then restored):

  * MUTATION A — join_truth: relax the identity to candidate_id only (drop the
    ``content_sha256_16``/``delivery_seal`` leg). ``test_seal_mismatch_produces_no_join``
    then WRONGLY joins the seal-mismatched row → ``syntax_result`` appears with
    ``truth=True`` instead of being absent. Bite confirmed.

  * MUTATION B — load_attestations: delete the exact canonical-byte integrity check
    (``canonical_bytes(attestation) != raw_attestation``).
    ``test_tampered_bundle_is_rejected_with_reason`` then ACCEPTS the tampered bundle →
    attestations non-empty, no diagnostic. Bite confirmed.
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
from groundtruth.runtime.attestation_store import persist_attestation  # noqa: E402
from groundtruth.runtime.producer_attestation import (  # noqa: E402
    ATTESTATION_SCHEMA,
    FAIL,
    FRESHNESS,
    PASS,
    ArtifactRef,
    DecisionBinding,
    PredicateAttestation,
    ProducerAttestation,
    ProofRef,
)


# --------------------------------------------------------------------------- #
# Fixture builders — REAL dataclasses, REAL store.
# --------------------------------------------------------------------------- #
def _syntax_attestation(
    *,
    candidate_id: str,
    delivery_seal: str,
    truth_verdict: str = PASS,
    freshness_verdict: str = PASS,
) -> tuple[ProducerAttestation, dict[str, bytes]]:
    """A valid ``syntax_result`` attestation (edit_check producer) + its artifact bytes.

    ``syntax_result`` is a registered §1 class: producer ``edit_check``, decision
    "is the edit acceptable", deliver_by ``edit_result``.
    """
    source_bytes = b'{"verdict":"syntax_error","line":12}'
    artifact_id = "diagnostic.json"
    ref = ArtifactRef(
        kind="producer_input",
        artifact_id=artifact_id,
        sha256=hashlib.sha256(source_bytes).hexdigest(),
        revision="edit:7",
    )
    proof = ProofRef("producer_observation", ref, "$.verdict")

    def _predicate(kind: str, pid: str, verdict: str) -> PredicateAttestation:
        # A PASS/FAIL predicate REQUIRES proof_refs + a non-empty observation;
        # UNMEASURED forbids proof_refs.
        if verdict in (PASS, FAIL):
            return PredicateAttestation(
                kind, pid, "edit", "acceptable", "observed", verdict, (proof,)
            )
        return PredicateAttestation(kind, pid, "edit", "acceptable", "", verdict, ())

    attestation = ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type="syntax_result",
        runtime_producer_id="edit_check",
        registered_producer_id="edit_check",
        candidate_id=candidate_id,
        delivery_seal=delivery_seal,
        source_artifacts=(ref,),
        truth_predicates=(_predicate("TRUTH", "truth.syntax", truth_verdict),),
        freshness_predicates=(
            _predicate(FRESHNESS, "fresh.syntax", freshness_verdict),
        ),
        decision=DecisionBinding("is the edit acceptable", "edit_result", "edit_result"),
    )
    return attestation, {artifact_id: source_bytes}


def _delivered_row(candidate_id: str, seal: str, *, chars: int = 36) -> dict:
    """A DELIVERED runtime-ledger row carrying the join identity the writer stamps."""
    return {
        "layer": "edit.syntax",
        "event_type": "edit_result",
        "file_path": "src/core.py",
        "outcome": "delivered",
        "reason": "lane_delivery_sealed",
        "chars_delivered": chars,
        "iteration": 5,
        "content_sha256_16": seal,
        "seal_scope": "block",
        "candidate_id": candidate_id,
    }


def _persist(task_dir: Path, attestation: ProducerAttestation, artifacts: dict) -> Path:
    root = task_dir / "art" / "producer_attestations"
    persist_attestation(attestation, artifacts, root)
    return root


# --------------------------------------------------------------------------- #
# load_attestations
# --------------------------------------------------------------------------- #
def test_load_accepts_a_valid_persisted_bundle(tmp_path: Path) -> None:
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal="a" * 16
    )
    _persist(tmp_path, attestation, artifacts)

    load = aj.load_attestations(str(tmp_path))

    assert len(load.attestations) == 1
    assert load.diagnostics == ()
    assert load.attestations[0].candidate_id == "edit:src/core.py:7"
    assert load.attestations[0].evidence_type == "syntax_result"


def test_load_of_absent_dir_is_empty_not_error(tmp_path: Path) -> None:
    load = aj.load_attestations(str(tmp_path))
    assert load == aj.AttestationLoad()


# --------------------------------------------------------------------------- #
# join_truth — the four required cases.
# --------------------------------------------------------------------------- #
def test_valid_join_yields_truth_true(tmp_path: Path) -> None:
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:src/core.py:7", seal)]

    load = aj.load_attestations(str(tmp_path))
    joins = aj.join_truth(load.attestations, rows)

    assert set(joins) == {"syntax_result"}
    tj = joins["syntax_result"]
    assert tj.truth is True
    assert tj.freshness is True
    assert tj.attestation_count == 1
    assert tj.joined_delivery_row_indices == (0,)


def test_fail_predicate_yields_truth_false(tmp_path: Path) -> None:
    seal = "b" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:9", delivery_seal=seal, truth_verdict=FAIL
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:src/core.py:9", seal)]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert joins["syntax_result"].truth is False
    # freshness was PASS and independent — it must NOT inherit the truth FAIL.
    assert joins["syntax_result"].freshness is True


def test_seal_mismatch_produces_no_join(tmp_path: Path) -> None:
    # MUTATION A bites here: a candidate_id-only join would wrongly match this row.
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal="a" * 16
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:src/core.py:7", "f" * 16)]  # seal differs

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert "syntax_result" not in joins
    assert joins == {}


def test_candidate_mismatch_produces_no_join(tmp_path: Path) -> None:
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:OTHER.py:1", seal)]  # candidate differs

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert joins == {}


def test_non_delivered_row_does_not_join(tmp_path: Path) -> None:
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    _persist(tmp_path, attestation, artifacts)
    row = _delivered_row("edit:src/core.py:7", seal)
    row["outcome"] = "suppressed_internal_only"  # not a delivered row
    rows = [row]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert joins == {}


def test_unattested_class_is_untouched(tmp_path: Path) -> None:
    # Only syntax_result is attested here; an unrelated delivered row for another
    # class must never appear in the join dict.
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [
        _delivered_row("edit:src/core.py:7", seal),
        {
            "layer": "l3.contract", "event_type": "file_view",
            "outcome": "delivered", "chars_delivered": 10,
            "content_sha256_16": "c" * 16, "candidate_id": "contract:x",
            "file_path": "src/other.py",
        },
    ]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert set(joins) == {"syntax_result"}
    assert "caller_contract" not in joins


# --------------------------------------------------------------------------- #
# Tampering / malformed bundles — fail closed.
# --------------------------------------------------------------------------- #
def _bundle_dir(root: Path) -> Path:
    (index,) = list((root / "index").iterdir())
    return index


def test_noncanonical_bytes_bundle_is_rejected(tmp_path: Path) -> None:
    # MUTATION B bites here: reformat attestation.json (pretty-print) WITHOUT changing
    # any content. The semantic sha (over canonical form) is unchanged, so only the
    # exact canonical-BYTES check distinguishes it. Removing that check accepts it.
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    root = _persist(tmp_path, attestation, artifacts)
    bundle = _bundle_dir(root)
    payload = json.loads((bundle / "attestation.json").read_bytes())
    # Pretty-print with indentation — different BYTES, identical content/sha.
    (bundle / "attestation.json").write_bytes(
        json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
    )

    load = aj.load_attestations(str(tmp_path))

    assert load.attestations == ()
    assert len(load.diagnostics) == 1
    assert "noncanonical_or_tampered" in load.diagnostics[0]
    # A rejected bundle never joins.
    rows = [_delivered_row("edit:src/core.py:7", seal)]
    assert aj.join_truth(load.attestations, rows) == {}


def test_tampered_content_bundle_is_rejected_with_reason(tmp_path: Path) -> None:
    # A content tamper (a flipped observation string) is caught by the entry sha:
    # the reconstructed canonical sha no longer equals the entry's recorded sha.
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    root = _persist(tmp_path, attestation, artifacts)
    bundle = _bundle_dir(root)
    payload = json.loads((bundle / "attestation.json").read_bytes())
    payload["truth_predicates"][0]["observation"] = "TAMPERED"
    (bundle / "attestation.json").write_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    load = aj.load_attestations(str(tmp_path))

    assert load.attestations == ()
    assert len(load.diagnostics) == 1
    assert "entry_sha_mismatch" in load.diagnostics[0]
    rows = [_delivered_row("edit:src/core.py:7", seal)]
    assert aj.join_truth(load.attestations, rows) == {}


def test_malformed_entry_json_is_skipped_not_crashing(tmp_path: Path) -> None:
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal="a" * 16
    )
    root = _persist(tmp_path, attestation, artifacts)
    bundle = _bundle_dir(root)
    (bundle / "entry.json").write_bytes(b"{ this is not json")

    load = aj.load_attestations(str(tmp_path))

    assert load.attestations == ()
    assert len(load.diagnostics) == 1
    assert "entry_not_json" in load.diagnostics[0]


def test_seal_length_forgery_fails_validation(tmp_path: Path) -> None:
    # A delivery_seal that is not 16 lower-hex is rejected by validate() at load,
    # so it can never reach the join.
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal="a" * 16
    )
    root = _persist(tmp_path, attestation, artifacts)
    bundle = _bundle_dir(root)
    payload = json.loads((bundle / "attestation.json").read_bytes())
    payload["delivery_seal"] = "NOT_HEX"
    (bundle / "attestation.json").write_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    load = aj.load_attestations(str(tmp_path))

    assert load.attestations == ()
    assert len(load.diagnostics) == 1
    assert "invalid:" in load.diagnostics[0]


def test_multiple_attestations_one_fail_aggregates_to_false(tmp_path: Path) -> None:
    seal_ok = "a" * 16
    seal_bad = "b" * 16
    ok, ok_art = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal_ok
    )
    bad, bad_art = _syntax_attestation(
        candidate_id="edit:src/core.py:9", delivery_seal=seal_bad, truth_verdict=FAIL
    )
    _persist(tmp_path, ok, ok_art)
    _persist(tmp_path, bad, bad_art)
    rows = [
        _delivered_row("edit:src/core.py:7", seal_ok),
        _delivered_row("edit:src/core.py:9", seal_bad),
    ]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert joins["syntax_result"].attestation_count == 2
    assert joins["syntax_result"].truth is False  # any FAIL → False
