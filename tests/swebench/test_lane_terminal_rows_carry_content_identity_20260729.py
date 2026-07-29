"""P1-4: a suppressed Lane-A candidate's terminal row must be JOINABLE.

THE DEFECT (codex audit 2026-07-29): the ss_step_behind / ss_semantic_dup terminal
rows carried kind+reason+file_path only — no candidate identity — while the content
sha (the dedup key ``hc``) was computed AFTER the screen had already ``continue``d.
Producer rows and terminal rows could only be joined by near-identical timestamps.
Companion defect: ``_inseam_eligible``/``_inseam_stamp`` stamped the raw
``_action_count``, which FREEZES at the canonical-observer bootstrap (the 5ff2ed2a5
family), so producer rows could not even be ORDERED against their terminals.

Source-pin + unit, same technique as test_precommit_staging_leaves_a_row_20260729.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SEAM = _REPO / "artifact_deepswe" / "gt_mini_patch.py"
if str(_REPO / "artifact_deepswe") not in sys.path:
    sys.path.insert(0, str(_REPO / "artifact_deepswe"))


def test_lane_suppression_terminal_row_carries_content_sha() -> None:
    source = _SEAM.read_text(encoding="utf-8")
    # anchor: the Lane-A screen call followed by its suppression ledger row
    anchor = source.find("_supp, _reason = _ss_screen_delivery(")
    assert anchor != -1, "Lane-A screen call not found — source drifted"
    window = source[anchor - 2000: anchor + 1200]
    assert "_cand_sha16" in window and '"content_sha256_16": _cand_sha16' in window, (
        "the Lane-A suppression terminal row must stamp the candidate's content "
        "sha — without it the producer->terminal join has no identity"
    )
    # the sha must be computed BEFORE the screen call, not after the continue
    sha_pos = source.find("_cand_sha16 = ")
    assert sha_pos != -1 and sha_pos < anchor, (
        "the content identity must exist before the screen decides"
    )


def test_inseam_rows_use_current_iteration_authority() -> None:
    """Both inseam helpers must consult _current_iteration (falling back to the raw
    counter only when it is absent), never stamp the frozen counter unconditionally."""
    source = _SEAM.read_text(encoding="utf-8")
    for helper in ("_inseam_eligible", "_inseam_stamp"):
        m = re.search(rf"def {helper}\(.*?\n(?:.*?\n)+?\n\n", source)
        assert m, f"{helper} not found"
        body = m.group(0)
        assert "_current_iteration" in body, (
            f"{helper} stamps iteration without consulting _current_iteration — "
            "the raw _action_count freezes at the canonical-observer bootstrap"
        )
