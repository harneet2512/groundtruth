"""SS-1 — the 8 SUPER-SEAM members are registered, forwarded, and unmapped-by-convention.

SS-1 owns rl_profile.py, so it registers ALL eight SS members from the 29-task causal audit
of run 29236533134. GT_SS_ARBITER_V2's BEHAVIOR lives in SS-1's owner files (proven by
test_ss1_arbiter_v2 / test_ss1_gateway_empty_payload); the other seven are default-OFF
seam-code-property flags whose behavior is owned by the implementing seam agents. This file
pins the MEMBERSHIP contract for all eight (Profile-2 delta, --ae forwarded, no false-abort
capability mapping) so a drop cannot go silent.
"""
from __future__ import annotations

import re
from pathlib import Path

from groundtruth.runtime import rl_profile as rp

SS_MEMBERS = frozenset({
    "GT_SS_NOVELTY",
    "GT_SS_DEDUP2",
    "GT_SS_COHERENCE_V2",
    "GT_SS_RECOVERY_V2",
    "GT_SS_PROVENANCE",
    "GT_SS_LATE_DROP",
    "GT_SS_ACK_METRICS",
    "GT_SS_ARBITER_V2",
})

_AE_BLOCK = (
    Path(__file__).parents[2] / "artifact_deepswe" / "gt_integration" / "gt_ae_block.sh"
)
_AE_RE = re.compile(r'--ae\s+"?(GT_[A-Z0-9_]+)=')


def _forwarded_names() -> set[str]:
    return set(_AE_RE.findall(_AE_BLOCK.read_text(encoding="utf-8")))


def test_all_ss_members_are_profile2():
    # Super-Mode (Profile-2) delta — present in "2", absent from "1" (they never activate under
    # a bare Profile-1). MUTATION[drop a member from _SUPER_MODE_MEMBERS] -> RED.
    assert SS_MEMBERS <= rp.PROFILE_MEMBERS["2"]
    assert SS_MEMBERS.isdisjoint(rp.PROFILE_MEMBERS["1"])


def test_all_ss_members_ae_forwarded():
    # pier DROPS host env -> only --ae crosses in; every SS member must be forwarded or it is
    # DARK in-container even under an active GT_RL_PROFILE=2. MUTATION[drop an --ae line] -> RED.
    missing = sorted(SS_MEMBERS - _forwarded_names())
    assert missing == [], f"SS member(s) not --ae-forwarded in gt_ae_block.sh: {missing}"


def test_profile2_fans_all_ss_members_to_one():
    got = rp.resolve_profile({"GT_RL_PROFILE": "2"})
    for m in SS_MEMBERS:
        assert got.get(m) == "1", m


def test_profile1_leaves_ss_members_off():
    got = rp.resolve_profile({"GT_RL_PROFILE": "1"})
    for m in SS_MEMBERS:
        assert m not in got, m


def test_ss_members_unmapped_by_convention_no_false_abort():
    # per the SS-1 directive: no _MEMBER_CAPABILITY_MODULE / _MEMBER_CAPABILITY_RECEIPT entry
    # for the seam-property flags (available-by-convention -> never a FALSE preflight abort).
    for m in SS_MEMBERS:
        assert m not in rp._MEMBER_CAPABILITY_MODULE, m
        assert m not in rp._MEMBER_CAPABILITY_RECEIPT, m
    # the capability PROBE (which builds the availability set from the real substrate) treats an
    # unmapped member as AVAILABLE -> the SS members are never a false-abort cause. MUTATION[map
    # an SS member to a missing module] would drop it from this set (-> a false abort).
    probed = rp._available_from_env({"GT_RL_PROFILE": "2"})
    assert SS_MEMBERS <= probed


def test_explicit_member_override_off_survives_profile2():
    # a member is a KILL-SWITCH: an explicit "0" wins over the profile fan-out.
    got = rp.resolve_profile({"GT_RL_PROFILE": "2", "GT_SS_ARBITER_V2": "0"})
    assert got["GT_SS_ARBITER_V2"] == "0"
    assert all(got[m] == "1" for m in SS_MEMBERS if m != "GT_SS_ARBITER_V2")
