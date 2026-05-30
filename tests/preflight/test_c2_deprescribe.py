"""C2 de-prescribe regression locks.

The imperative phrasings VERIFIED from raw output.jsonl bytes — the beets brief
`Edit beets/util/pipeline.py first. Verify: pytest ...` (to the WRONG file) and the
conan L5 rescue `Consider starting with a small edit.` — must not return. The agent
decides; GT presents evidence. Research: SWE-PRM NeurIPS 2025 (imperative mid-task
guidance lowers success; on a mislocalized rank it actively misdirects).

Source-invariant (same lock pattern as test_brief_delivery_invariants' no-reorder /
no-double-wrap locks). Comment lines are excluded, so a comment that documents the
removed phrasing is allowed — only an EXECUTABLE re-introduction fails.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
V1R = ROOT / "src" / "groundtruth" / "pretask" / "v1r_brief.py"
WRAP = ROOT / "scripts" / "swebench" / "oh_gt_full_wrapper.py"


def _code_lines(p: Path) -> str:
    return "\n".join(ln for ln in p.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#"))


def test_b7a_brief_no_edit_first_imperative():
    code = _code_lines(V1R)
    assert "Edit {top.path} first" not in code, "brief 'Edit X first' command must not return"
    assert "Verify: pytest {top.test_mappings" not in code, "brief 'Verify: pytest' command must not return"
    assert "Highest-confidence candidate" in V1R.read_text(encoding="utf-8"), \
        "the de-prescribed evidence form must be present"


def test_b7b_rescue_no_imperatives():
    code = _code_lines(WRAP)
    for bad in (
        "Consider starting with a small edit",
        "Run targeted tests after editing",
        "Make the smallest source edit",
        "Do not edit unrelated files",
        'Edit {top_base}.',
    ):
        assert bad not in code, f"rescue imperative must not return: {bad!r}"
    full = WRAP.read_text(encoding="utf-8")
    assert "was confirmed earlier" in full and "Highest-confidence target" in full, \
        "the de-prescribed evidence forms must be present"
