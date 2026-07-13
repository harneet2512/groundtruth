"""Pin: GT_BRIEF_MINIMAL must NOT abort Profile-2 on a pre-rebake (non-minimal) brief.

BUG (micro-verify 29214296174, both tasks): Fix B added GT_BRIEF_MINIMAL to
``_MEMBER_CAPABILITY_RECEIPT`` with predicate ``brief_minimal is True``. But the reduced brief is
DORMANT until an SM-8 rebake bakes it (rl_profile header note + the SM-6 member comment), so
brief_minimal is False on every pre-rebake substrate → the member is "unavailable" → since it is
requested-ON under Profile-2, ``preflight`` lists it in ``missing`` → the WHOLE Profile-2 aborts →
GT runs dark. Unlike GT_SEM_BODY, GT_BRIEF_MINIMAL has NO runtime surface it can corrupt: setting it
on a non-minimal brief is byte-identical (the generator ignores it). So it must be assumed-available
by convention, never receipt-gated.

Pins:
  * GT_BRIEF_MINIMAL is absent from _MEMBER_CAPABILITY_RECEIPT (a re-add reddens);
  * with a receipt that has brief_minimal=false but the 3 REAL surfaces present, Profile-2 preflight
    returns no member-missing abort (the exact micro-verify scenario);
  * the fail-closed invariant is intact — a receipt missing the sem-body surface STILL aborts
    GT_SEM_BODY (guards against a blanket "assume everything available" regression).
"""
from __future__ import annotations

import json

from groundtruth.runtime import rl_profile as rp

# the exact receipt from micro-verify 29214296174 (sh-744): dormant brief + 3 real surfaces present.
_RECEIPT = {
    "brief_minimal": False,
    "gt_index_bin": "/opt/gt/gt-index",
    "sem_body_rows": 297,
    "symbol_content_fts_rows": 206,
}


def test_brief_minimal_is_not_receipt_gated() -> None:
    assert "GT_BRIEF_MINIMAL" not in rp._MEMBER_CAPABILITY_RECEIPT, (
        "GT_BRIEF_MINIMAL must NOT be receipt-gated — it is dormant-until-rebake and byte-identical "
        "on a non-minimal brief, so gating it aborts the whole profile pre-rebake"
    )


def test_brief_minimal_is_assumed_available_when_brief_not_minimal() -> None:
    env = {"GT_RL_PROFILE": "2", "GT_CAPABILITY_RECEIPT": json.dumps(_RECEIPT)}
    avail = rp._available_from_env(env)
    assert "GT_BRIEF_MINIMAL" in avail, "GT_BRIEF_MINIMAL must be assumed-available on a non-minimal brief"


def test_profile2_preflight_does_not_abort_on_dormant_brief() -> None:
    env = {"GT_RL_PROFILE": "2", "GT_CAPABILITY_RECEIPT": json.dumps(_RECEIPT)}
    missing = rp.preflight(env, rp._available_from_env(env))
    assert "GT_BRIEF_MINIMAL" not in missing, "GT_BRIEF_MINIMAL must not appear in the preflight abort list"
    # the ONLY abort we observed was GT_BRIEF_MINIMAL; the 3 real surfaces are present here, so no
    # substrate member should remain missing.
    substrate_missing = [
        m for m in missing if m in ("GT_BRIEF_MINIMAL", "GT_SEM_BODY", "GT_CONTENT_LEG", "GT_L6_FRESH")
    ]
    assert substrate_missing == [], f"unexpected substrate members still missing: {substrate_missing}"


def test_sem_body_fail_closed_still_intact() -> None:
    # the negative control: dropping the sem-body surface MUST still fail closed (the fix must not
    # have turned into a blanket "assume all substrate members available").
    env = {"GT_RL_PROFILE": "2", "GT_CAPABILITY_RECEIPT": json.dumps({**_RECEIPT, "sem_body_rows": 0})}
    missing = rp.preflight(env, rp._available_from_env(env))
    assert "GT_SEM_BODY" in missing, "GT_SEM_BODY must still fail closed when the graph has 0 body rows"
