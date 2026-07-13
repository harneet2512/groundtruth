"""Pin: the Profile-2 preflight is RECEIPT-based + fail-closed (readiness spec §2).

Regression guard for the 2026-07-12 fix of the 11-member SELF-ATTESTATION defect: unmapped
Profile-2 members were added to the available set UNCONDITIONALLY, so `preflight()` could never
abort on them even when a stale substrate lacked the baked surface.

After the fix:
  * SUBSTRATE-PROPERTY members (GT_CONTENT_LEG/GT_SEM_BODY/GT_L6_FRESH) are available IFF a
    capability RECEIPT proves the baked surface — absent receipt => fail-closed.
  * GT_BRIEF_MINIMAL is DELIBERATELY NOT receipt-gated (2026-07-12): it is dormant until an SM-8
    rebake and byte-identical on a non-minimal brief, so gating it aborted the whole profile on
    every pre-rebake run (micro-verify 29214296174). It is assumed-available by convention.
  * the cleanly-importable native-form members (GT_POST_SEARCH_NATIVE/GT_SCOPE_NATIVE/
    GT_CERT_DELIVERY/GT_LOC_RESLOT) are module-probed.
  * only the 3 mini-seam-spread flags (GT_D7_RELATEDNESS/GT_CONTRACT_MODE/GT_CONTRACT_BILATERAL)
    remain available-by-convention.

The first test BITES on the pre-fix code (those 3 self-attested => in the available set).
"""
from __future__ import annotations

import json

from groundtruth.runtime import rl_profile
from groundtruth.runtime.rl_profile import preflight, resolve_profile

_SUBSTRATE_PROPERTY = ("GT_CONTENT_LEG", "GT_SEM_BODY", "GT_L6_FRESH")
_RENDERER_MAPPED = ("GT_POST_SEARCH_NATIVE", "GT_SCOPE_NATIVE", "GT_CERT_DELIVERY", "GT_LOC_RESLOT")
_SEAM_CONVENTION = ("GT_D7_RELATEDNESS", "GT_CONTRACT_MODE", "GT_CONTRACT_BILATERAL")

_FULL_RECEIPT = json.dumps(
    {
        "symbol_content_fts_rows": 5,
        "sem_body_rows": 3,
        "brief_minimal": True,
        "gt_index_bin": "/opt/gt/bin/gt-index",
    }
)


def test_substrate_property_members_fail_closed_without_receipt() -> None:
    # THE bite: Profile-2 with NO receipt => none of the 4 baked-surface members are available.
    avail = rl_profile._available_from_env({"GT_RL_PROFILE": "2"})
    for m in _SUBSTRATE_PROPERTY:
        assert m not in avail, f"{m} self-attested without a receipt (the pre-fix defect)"


def test_preflight_aborts_on_missing_substrate_receipt() -> None:
    env = {"GT_RL_PROFILE": "2"}
    missing = preflight(env, rl_profile._available_from_env(env))
    for m in _SUBSTRATE_PROPERTY:
        assert m in missing, f"{m} must abort the Profile-2 preflight when its receipt is absent"


def test_substrate_property_members_available_with_full_receipt() -> None:
    avail = rl_profile._available_from_env({"GT_RL_PROFILE": "2", "GT_CAPABILITY_RECEIPT": _FULL_RECEIPT})
    for m in _SUBSTRATE_PROPERTY:
        assert m in avail, f"{m} must be available once its receipt proves the baked surface"


def test_receipt_mutation_bites_per_member() -> None:
    # Flip each receipt field to an absent/false value and assert exactly that member drops.
    mutations = {
        "GT_CONTENT_LEG": {"symbol_content_fts_rows": 0},
        "GT_SEM_BODY": {"sem_body_rows": 0},
        "GT_L6_FRESH": {"gt_index_bin": ""},
    }
    base = json.loads(_FULL_RECEIPT)
    for member, patch in mutations.items():
        r = dict(base)
        r.update(patch)
        avail = rl_profile._available_from_env({"GT_RL_PROFILE": "2", "GT_CAPABILITY_RECEIPT": json.dumps(r)})
        assert member not in avail, f"{member} must fail closed when its receipt field is false/absent"
        # the other three stay available (isolation)
        for other in _SUBSTRATE_PROPERTY:
            if other != member:
                assert other in avail, f"{other} wrongly dropped by {member}'s mutation"


def test_malformed_receipt_fails_all_closed() -> None:
    avail = rl_profile._available_from_env({"GT_RL_PROFILE": "2", "GT_CAPABILITY_RECEIPT": "{not json"})
    for m in _SUBSTRATE_PROPERTY:
        assert m not in avail, "a malformed receipt must fail closed, not pass"


def test_renderer_members_are_module_probed() -> None:
    # native_render + gateway import in-interpreter => these are available WITHOUT a receipt.
    avail = rl_profile._available_from_env({"GT_RL_PROFILE": "2"})
    for m in _RENDERER_MAPPED:
        assert m in avail, f"{m} should be module-probed available (native_render/gateway importable)"


def test_seam_spread_members_available_by_convention() -> None:
    avail = rl_profile._available_from_env({"GT_RL_PROFILE": "2"})
    for m in _SEAM_CONVENTION:
        assert m in avail, f"{m} is synced-seam code, available by documented convention"


def test_brief_minimal_assumed_available_not_receipt_gated() -> None:
    # GT_BRIEF_MINIMAL is dormant-until-rebake + byte-identical on a non-minimal brief, so it must be
    # assumed-available WITHOUT a receipt (never a FALSE abort). Full pin: test_brief_minimal_not_
    # receipt_gated_20260712.py.
    assert "GT_BRIEF_MINIMAL" not in rl_profile._MEMBER_CAPABILITY_RECEIPT
    avail = rl_profile._available_from_env({"GT_RL_PROFILE": "2"})  # NO receipt
    assert "GT_BRIEF_MINIMAL" in avail, "GT_BRIEF_MINIMAL must be assumed-available without a receipt"


def test_profile1_unaffected_no_receipt_needed() -> None:
    # Profile-1 has NO substrate-property member => it must still preflight-clean via the probe alone.
    env = {"GT_RL_PROFILE": "1"}
    assert preflight(env, rl_profile._available_from_env(env)) == [], "Profile-1 must not require any receipt"


def test_available_override_still_wins() -> None:
    # An explicit GT_RL_PROFILE_AVAILABLE stamp bypasses the probe entirely (precedence preserved).
    env = {"GT_RL_PROFILE": "2", "GT_RL_PROFILE_AVAILABLE": "GT_CONTENT_LEG,GT_L6_FRESH"}
    avail = rl_profile._available_from_env(env)
    assert avail == {"GT_CONTENT_LEG", "GT_L6_FRESH"}


def test_profile_off_byte_identical() -> None:
    # SM-7 gate fix (2026-07-13): only an EXPLICIT off token skips preflight; unset/"" now
    # resolve the W8 production default and fail-close. resolve_profile stays {} on all three
    # (the resolver law is unchanged — the DEFAULT routing lives in preflight + the seam).
    for env in ({}, {"GT_RL_PROFILE": ""}, {"GT_RL_PROFILE": "0"}):
        assert resolve_profile(env) == {}
    assert preflight({"GT_RL_PROFILE": "0"}, rl_profile._available_from_env({})) == []
    assert preflight({"GT_RL_PROFILE": "off"}, rl_profile._available_from_env({})) == []
    assert preflight({}, set()) != []  # unset -> production default -> fail-closed
