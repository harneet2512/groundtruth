"""AE-forward completeness pin (Task #62, 2026-07-12) — Profile-2 ⊆ gt_ae_block.sh.

THE INVARIANT (so the "member read in-container but never `--ae`-forwarded" class
cannot recur): every ``rl_profile.PROFILE_MEMBERS["2"]`` member — which is
``_PROFILE_1_MEMBERS | _SUPER_MODE_MEMBERS`` — MUST appear as an ``--ae "NAME=…"``
forward in the canonical ``artifact_deepswe/gt_integration/gt_ae_block.sh`` block.

WHY THIS PIN EXISTS: ``pier`` DROPS the host ``os.environ`` — only ``--ae KEY=VALUE``
crosses into the task container. The RL-profile resolver ``export``s a member host-side,
but on any caller that splices ONLY ``GT_AE_ARGS`` (the codespace witness path,
``railway/codespace_deepswe_run.sh``) an un-forwarded member is DARK in-container even
under an active ``GT_RL_PROFILE=2``. The pre-existing coverage was blind to this class:
``test_rl_profile.py::test_profile_members_cross_the_miniswe_ae_boundary`` checks only
the Profile-1 members, and the R1 parity invariant
(``test_adversarial_fixes_20260704.py::test_r1_ae_parity_invariant_failclosed``) scans
only ``gt_mini_patch.py`` reads — so a Super-Mode member read by the *gateway* module
(e.g. ``GT_XSESSION_RANKUP``, ``gateway.py``) sailed through both, un-forwarded.

RED-first: on the pre-fix tree this pin FAILS (GT_XSESSION_RANKUP + GT_REGISTRY_ENFORCE
+ GT_BRIEF_MINIMAL + GT_CONTENT_LEG + GT_SEM_BODY are members but absent from the block).
Mutation: removing any one member's ``--ae`` line from the block re-reddens it.

The pin reads the ``.sh`` as TEXT and extracts the forwarded NAMES (default value is
irrelevant to reachability — ``GT_CONTENT_LEG`` is forwarded ``:-1`` to match the
owned ``deepswe_full.yml`` default, all others ``:-0``), then asserts set-superset.
"""
from __future__ import annotations

import re
from pathlib import Path

from groundtruth.runtime.rl_profile import (
    PROFILE_MEMBERS,
    _SUPER_MODE_MEMBERS,
)

# Same NAME-extractor the R1 parity invariant uses (default-value agnostic).
_AE_RE = re.compile(r'--ae\s+"?(GT_[A-Z0-9_]+)=')

_AE_BLOCK = (
    Path(__file__).parents[2]
    / "artifact_deepswe"
    / "gt_integration"
    / "gt_ae_block.sh"
)


def _forwarded_names() -> set[str]:
    return set(_AE_RE.findall(_AE_BLOCK.read_text(encoding="utf-8")))


def test_super_mode_members_all_ae_forwarded():
    """Every Super-Mode (Profile-2 delta) member crosses the ``--ae`` boundary."""
    forwarded = _forwarded_names()
    missing = sorted(_SUPER_MODE_MEMBERS - forwarded)
    assert missing == [], (
        "Super-Mode member(s) read in-container but NOT --ae-forwarded in "
        f"gt_ae_block.sh (pier drops host env => DARK in-container): {missing}"
    )


def test_profile_2_members_all_ae_forwarded():
    """The strongest form: the WHOLE Profile-2 set (Profile-1 ∪ Super-Mode) is a
    subset of the block's forwarded names — no member can be added to the profile
    without also being forwarded."""
    forwarded = _forwarded_names()
    missing = sorted(PROFILE_MEMBERS["2"] - forwarded)
    assert missing == [], (
        "Profile-2 member(s) absent from gt_ae_block.sh --ae forwards: " f"{missing}"
    )


def test_known_hole_xsession_rankup_is_forwarded():
    """Regression sentinel for the exact hole Task #62 closed: GT_XSESSION_RANKUP
    (a member read by the synced gateway module, invisible to both prior pins)."""
    assert "GT_XSESSION_RANKUP" in _forwarded_names()
