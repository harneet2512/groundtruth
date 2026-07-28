"""C30 half 2 — a canonical capsule delivery must be joinable on the SAME identity contract.

`attestation_join._delivered_row_index` keys DELIVERED rows on `(candidate_id,
content_sha256_16)`. `gt.canonical_delivery.v1` carried neither, so EVERY capsule delivery was
skipped and the two classes that ship on the step-0 capsule path — localization and obligations —
could never receive attested truth. `correct_info` stayed `None` and their SS-LIVE terminal was
SEALED_DELIVERED_UNGRADED on every run, however good the run. The eight classes that DO have
working attestations all ship on the gateway/native lanes, which stamp `candidate_id` = the
envelope's `dedup_key`. That asymmetry was the bug.

J6 AND WHY THIS IS A FOURTH REGISTERED FORM, not a loophole. J6 admits a row only when the ROW
proves its own attribution. The cheap alternative — parse the dedup key out of `evidence_ids`
offline, no runtime change — was rejected precisely here: the registration check in
`canonical_evidence_from_envelope` is real but UPSTREAM and invisible to an offline reader, so
indexing on it would downgrade J6 from "the row proves it" to "trust that someone checked
earlier". Instead the runtime carries `(candidate_id, fact_class)` forward onto the row, and this
reader CHECKS the class against the registry itself. That is strictly stronger than form 1, which
checks only that a lineage schema tag and a non-empty class string are present.

BITING MUTATIONS (applied, observed RED, reverted by targeted restore):
  M1 — index a canonical entry without checking the registry: `test_unregistered_fact_class_is_
       rejected_not_indexed` goes RED, and a row could claim any class it liked.
  M2 — index canonical rows regardless of `outcome`: `test_undelivered_canonical_row_is_not_
       indexed` goes RED — a COMPILED-but-never-delivered capsule would seat truth.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import attestation_join as aj  # noqa: E402


_SEAL = "a" * 16


def _canonical_row(*, outcome: str = "delivered", lineage=None) -> dict:
    return {
        "schema": "gt.canonical_delivery.v1",
        "layer": "canonical.provider_delivery",
        "event_type": "canonical_provider_delivery",
        "outcome": outcome,
        "content_sha256_16": _SEAL,
        "evidence_ids": ["GT-E-ac032ea694307691-g" + "b" * 64],
        "evidence_lineage": (
            [{"candidate_id": "ac032ea694307691", "fact_class": "localization"}]
            if lineage is None
            else lineage
        ),
    }


def _lane_row() -> dict:
    """A gateway-lane row, which has always been indexable — the CALIBRATION."""
    return {
        "outcome": "delivered",
        "candidate_id": "lane-candidate",
        "content_sha256_16": _SEAL,
        "lineage_schema": aj.LINEAGE_SCHEMA,
        "fact_class": "localization",
    }


def test_the_instrument_can_produce_a_non_zero() -> None:
    """CALIBRATION. Without a row that DOES index, every absence below is unreadable."""
    index, _ = aj._delivered_row_index([_lane_row()])
    assert ("lane-candidate", _SEAL) in index


def test_canonical_row_is_indexed_by_its_evidence_lineage() -> None:
    index, rejections = aj._delivered_row_index([_canonical_row()])
    assert ("ac032ea694307691", _SEAL) in index, rejections
    assert index[("ac032ea694307691", _SEAL)] == [0]


def test_every_entry_in_a_multi_evidence_capsule_is_indexed() -> None:
    """One capsule can deliver a coalition; each member must be joinable on the shared seal."""
    row = _canonical_row(
        lineage=[
            {"candidate_id": "aaaa000000000001", "fact_class": "localization"},
            {"candidate_id": "aaaa000000000002", "fact_class": "obligations"},
        ]
    )
    index, _ = aj._delivered_row_index([row])
    assert ("aaaa000000000001", _SEAL) in index
    assert ("aaaa000000000002", _SEAL) in index


def test_undelivered_canonical_row_is_not_indexed() -> None:
    """M2. A compiled-but-never-delivered capsule must never seat truth."""
    index, _ = aj._delivered_row_index([_canonical_row(outcome="withheld")])
    assert index == {}


def test_unregistered_fact_class_is_rejected_not_indexed() -> None:
    """M1. The row does not get to name its own class into existence.

    A rejection (not a silent skip) so an attestation that matches surfaces a NAMED reason
    rather than looking like a plain candidate/seal miss.
    """
    row = _canonical_row(
        lineage=[{"candidate_id": "aaaa000000000003", "fact_class": "not_a_fact_class"}]
    )
    index, rejections = aj._delivered_row_index([row])
    assert ("aaaa000000000003", _SEAL) not in index
    assert rejections.get(("aaaa000000000003", _SEAL))


def test_malformed_entries_never_index_and_never_raise() -> None:
    """Identity-incomplete entries are skipped; a reader over run artifacts must not crash."""
    row = _canonical_row(
        lineage=[
            {"candidate_id": "", "fact_class": "localization"},
            {"candidate_id": "aaaa000000000004"},
            {"fact_class": "localization"},
            "scalar",
            None,
        ]
    )
    index, _ = aj._delivered_row_index([row])
    assert index == {}


def test_a_canonical_row_without_a_seal_is_not_indexed() -> None:
    """No seal, no byte binding, no join — the identity is only half present."""
    row = _canonical_row()
    row.pop("content_sha256_16")
    index, _ = aj._delivered_row_index([row])
    assert index == {}
