"""C30 half 3b (producer side) — capture what the brief attestation will need, at delivery identity.

The factories need producer facts the DELIVERY site does not have: for localization the ranked
path / rank / witness / witness_verified, for obligations the issue identity, count and digest.
Those exist only while the brief records are being built. The established way to carry them across
that gap is `gateway._stash_newfile_precedent_snapshot` / `pop_newfile_precedent_snapshot` — a
bounded dict that never raises into the delivery path — and this is the same shape for the brief.

KEYED BY THE ENVELOPE `dedup_key`, WHICH IS THE TRAP. The brief's own `candidate_id` ("obl-1",
"file-entry-1") is NOT the delivery identity: the row and the join both use the envelope
`dedup_key`, which does not exist until `EvidenceEnvelope.build` has run. Keying the stash by the
brief id would produce a stash nothing ever pops — a silent no-op that looks implemented and
whose tests would still pass if they used the same wrong key on both sides. So the key here is
asserted to be the SAME string the canonical row carries.

POP, NOT PEEK. One delivery consumes its snapshot. A second delivery for the same candidate must
not re-attest stale producer facts against different bytes.

BITING MUTATIONS (applied, observed RED, reverted by targeted restore):
  M1 — `pop` becomes `get` (leaves the entry): `test_a_snapshot_is_consumed_exactly_once` RED.
  M2 — drop the cap (unbounded growth): `test_the_stash_is_bounded` RED. A per-candidate dict on
       a long trajectory is a slow leak in the delivery path, which is the one place that must
       never degrade.
"""

from __future__ import annotations

from groundtruth.runtime import brief_attestation as ba


def _clear() -> None:
    ba._BRIEF_SNAPSHOTS.clear()


def test_round_trips_a_localization_snapshot() -> None:
    _clear()
    payload = {
        "fact_class": "localization",
        "path": "src/auth/session.py",
        "rank": 1,
        "witness": "resolved import path",
        "witness_verified": True,
        "block_content_sha256": "a" * 64,
    }
    ba.stash_brief_snapshot("ac032ea694307691", payload)
    assert ba.pop_brief_snapshot("ac032ea694307691") == payload


def test_round_trips_an_obligations_snapshot() -> None:
    _clear()
    payload = {
        "fact_class": "obligations",
        "issue_sha256": "c" * 64,
        "issue_revision": "issue:" + "c" * 64,
        "obligation_count": 3,
        "obligations_digest": "d" * 64,
        "block_content_sha256": "b" * 64,
    }
    ba.stash_brief_snapshot("ac032ea694307692", payload)
    assert ba.pop_brief_snapshot("ac032ea694307692") == payload


def test_a_snapshot_is_consumed_exactly_once() -> None:
    """M1. A second delivery must not re-attest stale producer facts against other bytes."""
    _clear()
    ba.stash_brief_snapshot("k", {"fact_class": "localization"})
    assert ba.pop_brief_snapshot("k") is not None
    assert ba.pop_brief_snapshot("k") is None


def test_missing_and_empty_keys_are_quiet() -> None:
    """The delivery path asks for every delivered candidate; most have no snapshot."""
    _clear()
    assert ba.pop_brief_snapshot("never-stashed") is None
    assert ba.pop_brief_snapshot("") is None


def test_stashing_nothing_is_a_no_op_not_a_none_entry() -> None:
    """A None entry would later read as 'present but empty' and attest from nothing."""
    _clear()
    ba.stash_brief_snapshot("k", None)
    ba.stash_brief_snapshot("", {"fact_class": "localization"})
    assert ba._BRIEF_SNAPSHOTS == {}


def test_the_stash_is_bounded() -> None:
    """M2. An unbounded per-candidate dict is a slow leak in the delivery path."""
    _clear()
    cap = ba._BRIEF_SNAPSHOT_CAP
    for index in range(cap + 25):
        ba.stash_brief_snapshot(f"k{index}", {"fact_class": "localization", "n": index})
    assert len(ba._BRIEF_SNAPSHOTS) <= cap
    # the OLDEST are dropped, so the most recent deliveries -- the ones still in flight --
    # are the ones that survive.
    assert ba.pop_brief_snapshot(f"k{cap + 24}") is not None
    assert ba.pop_brief_snapshot("k0") is None


def test_a_stash_failure_never_raises_into_the_delivery_path() -> None:
    """Audit plumbing may not break a delivery. An unhashable key is a producer bug, not a crash."""
    _clear()
    ba.stash_brief_snapshot(["unhashable"], {"fact_class": "localization"})  # type: ignore[arg-type]
    assert ba._BRIEF_SNAPSHOTS == {}
