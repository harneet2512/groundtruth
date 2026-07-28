"""C30 half 3d — the LAST link: finalize the brief attestations at the delivered capsule.

Everything before this was inert plumbing. The identity is on the row, the producer facts are
stashed under the delivery key, and the hook fires when both halves exist. This is the handler
the seam registers: pop the snapshot for each delivered evidence, finalize with the CAPSULE's
digest as the delivered bytes, and persist through the seam's own attestation root.

Until this exists, `localization` and `obligations` have no production caller at all, so their
`truth_valid`/`authority_valid` stay UNMEASURED, `correct_info` is None, and their SS-LIVE
terminal is SEALED_DELIVERED_UNGRADED on EVERY run however good the run.

WHAT THIS STILL DOES NOT DO. It does not promote anything and it does not make either class
PASS. It makes them GRADABLE: an attestation now exists to join, and its verdict is whatever the
producer's build-time facts honestly support (PASS only on a graph-verified candidate / a real
obligations record; UNMEASURED otherwise, never fabricated).

BITING MUTATIONS (applied, observed RED, reverted by targeted restore):
  M1 — drop the `fact_class` cross-check between the lineage entry and the popped snapshot:
       `test_a_snapshot_of_the_wrong_class_is_never_attested` goes RED, and a stale
       localization snapshot could be attested as obligations against the wrong producer facts.
  M2 — pass `block_content_sha256` as the delivered digest (the overload I nearly shipped):
       `test_the_capsule_digest_is_what_gets_sealed` goes RED — the factory refuses, because the
       block digest does not carry the capsule's seal, and every brief attestation silently
       vanishes.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import brief_attestation as ba
from groundtruth.runtime import attestation_store as _store


_CAPSULE = hashlib.sha256(b"[GroundTruth - SOURCE TARGET SELECTION] ...").hexdigest()
_BLOCK = hashlib.sha256(b"1. src/auth/session.py").hexdigest()
_LOC_KEY = "fea09fdd0f098616"
_OBL_KEY = "ac032ea694307691"


def _compilation(lineage):
    return SimpleNamespace(
        rendered_content_hash=_CAPSULE,
        capsule_hash="c" * 64,
        evidence_lineage=tuple(lineage),
    )


def _localization_snapshot(**over):
    payload = {
        "fact_class": "localization",
        "path": "src/auth/session.py",
        "rank": 1,
        "witness": "resolved import path",
        "witness_verified": True,
        "block_content_sha256": _BLOCK,
        "graph_revision": "graph-rev",
    }
    payload.update(over)
    return payload


def _obligations_snapshot(**over):
    payload = {
        "fact_class": "obligations",
        "issue_sha256": "c" * 64,
        "issue_revision": "issue:" + "c" * 64,
        "obligation_count": 2,
        "obligations_digest": "d" * 64,
        "block_content_sha256": _BLOCK,
    }
    payload.update(over)
    return payload


@pytest.fixture()
def persisted(monkeypatch):
    """Capture persistence instead of writing a store; the store has its own tests."""
    calls: list[tuple] = []
    # Patch the STORE, not the seam: the seam imports `persist_attestation` locally at call
    # time (its house style), so a seam-level attribute would never be consulted and this
    # fixture would silently capture nothing.
    monkeypatch.setattr(
        _store,
        "persist_attestation",
        lambda attestation, artifacts, root: calls.append((attestation, artifacts, root)),
    )
    ba._BRIEF_SNAPSHOTS.clear()
    return calls


def test_the_probe_can_produce_a_non_zero(persisted) -> None:
    """CALIBRATION. Every 'nothing was persisted' assertion below is unreadable without it."""
    ba.stash_brief_snapshot(_LOC_KEY, _localization_snapshot())
    seam._persist_brief_producer_attestations(
        _compilation([(_LOC_KEY, "localization")])
    )
    assert len(persisted) == 1


def test_the_capsule_digest_is_what_gets_sealed(persisted) -> None:
    """M2. The seal must follow the bytes the model received, not the source block."""
    ba.stash_brief_snapshot(_LOC_KEY, _localization_snapshot())
    seam._persist_brief_producer_attestations(
        _compilation([(_LOC_KEY, "localization")])
    )
    attestation = persisted[0][0]
    assert attestation.delivery_seal == _CAPSULE[:16]
    assert attestation.candidate_id == _LOC_KEY


def test_both_brief_classes_are_attested_from_one_capsule(persisted) -> None:
    ba.stash_brief_snapshot(_LOC_KEY, _localization_snapshot())
    ba.stash_brief_snapshot(_OBL_KEY, _obligations_snapshot())
    seam._persist_brief_producer_attestations(
        _compilation([(_LOC_KEY, "localization"), (_OBL_KEY, "obligations")])
    )
    assert len(persisted) == 2
    assert {call[0].candidate_id for call in persisted} == {_LOC_KEY, _OBL_KEY}


def test_a_snapshot_of_the_wrong_class_is_never_attested(persisted) -> None:
    """M1. A stale snapshot must not be attested under another class's name.

    The lineage says obligations; the stash holds localization facts. Attesting that pairing
    would bind a producer's ranked-path record to a behavioural-contract claim.
    """
    ba.stash_brief_snapshot(_OBL_KEY, _localization_snapshot())
    seam._persist_brief_producer_attestations(
        _compilation([(_OBL_KEY, "obligations")])
    )
    assert persisted == []


def test_an_evidence_with_no_snapshot_is_quiet(persisted) -> None:
    """Most delivered evidence is not brief evidence; absence is normal, not a fault."""
    seam._persist_brief_producer_attestations(
        _compilation([("deadbeefdeadbeef", "caller_contract")])
    )
    assert persisted == []


def test_a_snapshot_is_consumed_so_a_redelivery_cannot_reattest(persisted) -> None:
    ba.stash_brief_snapshot(_LOC_KEY, _localization_snapshot())
    compilation = _compilation([(_LOC_KEY, "localization")])
    seam._persist_brief_producer_attestations(compilation)
    seam._persist_brief_producer_attestations(compilation)
    assert len(persisted) == 1


def test_an_unverified_candidate_still_attests_but_never_claims_PASS(persisted) -> None:
    """Correct-or-quiet: a weak producer fact yields an HONEST UNMEASURED verdict."""
    ba.stash_brief_snapshot(
        _LOC_KEY, _localization_snapshot(witness_verified=False, witness="")
    )
    seam._persist_brief_producer_attestations(
        _compilation([(_LOC_KEY, "localization")])
    )
    assert len(persisted) == 1
    verdicts = {p.verdict for p in persisted[0][0].truth_predicates}
    assert "PASS" not in verdicts


def test_a_persist_fault_never_raises_into_the_delivery_path(monkeypatch) -> None:
    """The bytes are already with the model. Audit persistence cannot retract them."""
    ba._BRIEF_SNAPSHOTS.clear()
    rows: list[str] = []

    def _boom(*_args, **_kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(_store, "persist_attestation", _boom)
    monkeypatch.setattr(
        seam,
        "_attestation_persist_failure_row",
        lambda kind, candidate_id, exc: rows.append(kind),
        raising=False,
    )
    ba.stash_brief_snapshot(_LOC_KEY, _localization_snapshot())
    seam._persist_brief_producer_attestations(
        _compilation([(_LOC_KEY, "localization")])
    )
    assert rows, "a swallowed persist must still record its cause"


def test_a_malformed_compilation_is_quiet(persisted) -> None:
    """A reader over live objects must not assume shape."""
    seam._persist_brief_producer_attestations(SimpleNamespace())
    seam._persist_brief_producer_attestations(None)
    assert persisted == []
