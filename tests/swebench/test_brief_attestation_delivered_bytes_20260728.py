"""C30 half 3a — the DELIVERED bytes and the SOURCE BLOCK are two different digests.

Both brief factories used to require `block_content_sha256[:16] == delivery_seal`. That was
correct while the brief block WAS the delivered byte string. It is not any more: the step-0
evidence ships inside a re-rendered capsule, so the delivered bytes are the capsule and the block
is only where the fact came from. Observed, not assumed — a probe on a two-block sealed brief:

    localization block content_hash   35400183dfabea29...
    obligations  block content_hash   f0f86c283f26302e...
    staged capsule rendered_content_hash 407deed35ea42658...   <- neither

WHY NOT JUST PASS THE CAPSULE DIGEST AS `block_content_sha256`. It satisfies the old guard
mechanically, and it makes the attestation ASSERT A FALSEHOOD: that the capsule digest is the
block digest. A reader reproducing the preimage would hash the brief block, get a different
value, and correctly conclude the bundle was forged. The seal and the provenance are two facts
and the bundle must be able to state both.

WHY NOT RENAME THE PARAMETER EITHER. That was my first plan and it DESTROYS information — the
block digest is real producer provenance (which block the fact was extracted from) and the
witness record still seals it. So: a new `delivered_bytes_sha256` carries the seal binding, and
`block_content_sha256` stays exactly what its name says.

BACKWARD COMPATIBILITY IS NOT A COURTESY HERE. Omitting the new argument falls back to the block
digest, which is the PRE-CAPSULE truth: when the delivered bytes ARE the block, one value
honestly serves both roles. Existing lane-shaped callers keep their exact meaning.

BITING MUTATIONS (applied, observed RED, reverted by targeted restore):
  M1 — guard the seal against `block_content_sha256` again: the capsule case returns None and
       `test_capsule_delivery_binds_the_delivered_bytes_not_the_block` goes RED.
  M2 — drop the fallback (require the new arg): `test_omitting_delivered_bytes_falls_back_to_
       the_block_digest` goes RED and every pre-capsule caller silently stops attesting.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from groundtruth.runtime import brief_attestation as ba  # noqa: E402


_BLOCK = hashlib.sha256(b"1. src/auth/session.py\n   resolved import path").hexdigest()
_CAPSULE = hashlib.sha256(b"[GroundTruth - SOURCE TARGET SELECTION]\n...").hexdigest()


def _localization(**overrides):
    kwargs = {
        "candidate_id": "ac032ea694307691",
        "delivery_seal": _CAPSULE[:16],
        "block_content_sha256": _BLOCK,
        "delivered_bytes_sha256": _CAPSULE,
        "path": "src/auth/session.py",
        "rank": 1,
        "witness": "resolved import path",
        "witness_verified": True,
    }
    kwargs.update(overrides)
    return ba.finalize_localization_attestation(**kwargs)


def _obligations(**overrides):
    kwargs = {
        "candidate_id": "ac032ea694307692",
        "delivery_seal": _CAPSULE[:16],
        "block_content_sha256": _BLOCK,
        "delivered_bytes_sha256": _CAPSULE,
        "issue_sha256": "c" * 64,
        "issue_revision": "issue:" + "c" * 64,
        "obligation_count": 2,
        "obligations_digest": "d" * 64,
    }
    kwargs.update(overrides)
    return ba.finalize_obligations_attestation(**kwargs)


def test_the_two_digests_are_actually_different() -> None:
    """CALIBRATION. If the fixture's block and capsule digests collided, every assertion
    below would pass for the wrong reason."""
    assert _BLOCK != _CAPSULE
    assert _BLOCK[:16] != _CAPSULE[:16]


def test_capsule_delivery_binds_the_delivered_bytes_not_the_block() -> None:
    """M1. The seal must follow the bytes the model actually received."""
    for final in (_localization(), _obligations()):
        assert final is not None
        assert final.attestation.delivery_seal == _CAPSULE[:16]


def test_the_block_digest_is_no_longer_required_to_match_the_seal() -> None:
    """The whole point: provenance and seal are independent facts now."""
    assert _localization(block_content_sha256="f" * 64) is not None


def test_omitting_delivered_bytes_falls_back_to_the_block_digest() -> None:
    """M2. Pre-capsule callers, where the delivered bytes ARE the block, keep working."""
    final = _localization(
        delivery_seal=_BLOCK[:16],
        delivered_bytes_sha256="",
    )
    assert final is not None
    assert final.attestation.delivery_seal == _BLOCK[:16]


def test_a_delivered_digest_that_does_not_carry_the_seal_is_refused() -> None:
    """Fail-closed: no sound binding -> no bundle, and the class stays UNMEASURED."""
    assert _localization(delivery_seal="a" * 16) is None
    assert _obligations(delivery_seal="a" * 16) is None


def test_a_malformed_delivered_digest_is_refused() -> None:
    for bad in ("", "not-hex", _CAPSULE[:32], _CAPSULE.upper()):
        assert _localization(delivered_bytes_sha256=bad, delivery_seal=bad[:16]) is None


def test_the_block_digest_is_still_sealed_into_the_witness_record() -> None:
    """Provenance must SURVIVE, not be replaced -- that is why this is not a rename."""
    final = _localization()
    assert final is not None
    artifacts = dict(final.artifact_mapping())
    payload = b"".join(artifacts.values())
    assert _BLOCK.encode() in payload
