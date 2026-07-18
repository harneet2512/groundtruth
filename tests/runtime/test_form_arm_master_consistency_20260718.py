"""Form-arm ↔ master consistency (Cluster-5 ITEM 0b, 2026-07-18).

THE INVARIANT: every ``*_NATIVE`` FORM-arm member of Profile-2 whose delivery surface has a
MASTER enable flag must have that master ALSO present in Profile-2 — else the profile ships a
native render for a producer it never enables (the whole surface renders nothing). This is the
EXACT defect ITEM 0 closed: GT_POST_SEARCH_NATIVE was an active Profile-2 member while its
GT_POST_SEARCH master was absent, so the post_search lattice was dead behind it.

Pure ``*_NATIVE`` arms with no separate producer gate are documented master-less in
``rl_profile._MASTERLESS_FORM_ARMS`` and are exempt (never a false gap).

RED-first: on the pre-ITEM-0 tree (GT_POST_SEARCH absent) ``form_arm_master_gaps("2")`` returns
``{"GT_POST_SEARCH_NATIVE": "GT_POST_SEARCH"}`` — see the reproduction test below, which patches
that exact state and asserts the gap reappears.
"""
from __future__ import annotations

from groundtruth.runtime import rl_profile


def test_profile2_has_no_form_arm_master_gaps() -> None:
    """The whole invariant: post-ITEM-0, no form arm is missing its master (GT_POST_SEARCH_NATIVE
    now has GT_POST_SEARCH; GT_GATEWAY_NATIVE has GT_GATEWAY; the rest are documented master-less)."""
    assert rl_profile.form_arm_master_gaps("2") == {}


def test_gateway_and_post_search_masters_are_members_not_allowlisted() -> None:
    """The two form arms WITH a real producer-enable master resolve via membership, never via the
    master-less allowlist (which would silently re-open the ITEM-0 hole)."""
    members = rl_profile.PROFILE_MEMBERS["2"]
    assert "GT_GATEWAY_NATIVE" in members and "GT_GATEWAY" in members
    assert "GT_POST_SEARCH_NATIVE" in members and "GT_POST_SEARCH" in members
    assert "GT_GATEWAY_NATIVE" not in rl_profile._MASTERLESS_FORM_ARMS
    assert "GT_POST_SEARCH_NATIVE" not in rl_profile._MASTERLESS_FORM_ARMS


def test_red_reproduction_removing_post_search_master_reopens_the_gap(monkeypatch) -> None:
    """MUTATION 1 (the natural RED): drop GT_POST_SEARCH from Profile-2 (the exact pre-ITEM-0
    state) and the checker must flag GT_POST_SEARCH_NATIVE as an unmastered form arm."""
    pre_fix = {
        "1": rl_profile.PROFILE_MEMBERS["1"],
        "2": frozenset(rl_profile.PROFILE_MEMBERS["2"]) - {"GT_POST_SEARCH"},
    }
    monkeypatch.setattr(rl_profile, "PROFILE_MEMBERS", pre_fix)
    assert rl_profile.form_arm_master_gaps("2") == {"GT_POST_SEARCH_NATIVE": "GT_POST_SEARCH"}


def test_mutation_removing_an_allowlist_entry_flags_its_form_arm(monkeypatch) -> None:
    """MUTATION 2: the master-less allowlist is load-bearing — drop GT_STEER_NATIVE's exemption and
    the checker reports it as an (incorrect) gap, proving the allowlist actively suppresses a real
    false-positive rather than being dead decoration."""
    trimmed = dict(rl_profile._MASTERLESS_FORM_ARMS)
    trimmed.pop("GT_STEER_NATIVE")
    monkeypatch.setattr(rl_profile, "_MASTERLESS_FORM_ARMS", trimmed)
    gaps = rl_profile.form_arm_master_gaps("2")
    assert gaps == {"GT_STEER_NATIVE": "GT_STEER"}


def test_allowlist_is_not_stale() -> None:
    """Every documented master-less form arm is (a) actually a Profile-2 member and (b) genuinely
    master-less — its derived master is NOT a member. A violation of (b) means the arm gained a
    real master and must move OUT of the allowlist (else the ITEM-0 hole hides behind the exemption)."""
    members = rl_profile.PROFILE_MEMBERS["2"]
    for arm, reason in rl_profile._MASTERLESS_FORM_ARMS.items():
        assert arm.endswith("_NATIVE"), arm
        assert arm in members, f"stale allowlist: {arm} is not a Profile-2 member"
        assert reason.strip(), f"allowlist entry {arm} needs a reason"
        master = arm[: -len("_NATIVE")]
        assert master not in members, (
            f"{arm} has a real master member {master} — remove it from the master-less allowlist"
        )
