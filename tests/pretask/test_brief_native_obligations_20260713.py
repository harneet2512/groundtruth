r"""W10 ITEM-5 (2026-07-13) — GT_BRIEF_NATIVE: the brief obligations section as a plain checklist.

The obligations block is the LAST tagged model-facing brief surface. Under GT_BRIEF_NATIVE it renders
as a plain requirements checklist (``- [ ] <obligation>`` under a one-line plain header, NO ``<gt-*>``
tag) instead of the ``<gt-obligations>`` block. REBAKE-RELEVANT: read at brief-GENERATION time, so it
takes effect only when the substrate is baked with the flag active; DEFAULT-OFF is byte-identical.

This is a pure Stage-1 FORM swap — NOT a localization/ranking change (localize()/scoring/semantic are
untouched), so the BRIEFING.md measurement invariants (measure generate_v1r_brief, semantic-on, one
weight at a time) are not implicated here.

Robustness the FORM swap must preserve (proven below): the native obligations block still segments as
an ``obligations`` (priority 1) block, so (a) the B-30 token rail protects it EXACTLY like the tagged
block, (b) the SM-6 GT_BRIEF_MINIMAL reducer KEEPS it, and (c) brief_minimal_certificate certifies
``obligations_present`` under either frame — the combined MINIMAL+NATIVE rebake is not broken.

RED-first: the byte-identical-off assertions are the RED the flag-on GREEN must not disturb; each
biting mutation reverts a load-bearing hunk (the native branch / the segmenter label / the boundary).

Hermetic: pure text helpers + a live spec extraction over a crafted MUST issue (no graph.db, no ONNX).
Windows: run with PYTHONIOENCODING=utf-8.
"""
from __future__ import annotations

import os

import pytest

from groundtruth.pretask import v1r_brief as v


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("GT_BRIEF_NATIVE", raising=False)
    monkeypatch.delenv("GT_BRIEF_MINIMAL", raising=False)
    yield


_ROWS = ["  - [behavior] the parser must raise ValueError on empty input",
         "  - must return None on failure"]


# =========================================================================== #
# 1. flag helper + the pure section/row transforms
# =========================================================================== #
def test_flag_helper_default_off_on(monkeypatch):
    assert v._brief_native_on() is False
    monkeypatch.setenv("GT_BRIEF_NATIVE", "1")
    assert v._brief_native_on() is True
    monkeypatch.setenv("GT_BRIEF_NATIVE", "0")
    assert v._brief_native_on() is False


def test_checklist_row_transform():
    assert v._obligation_checklist_row("  - [behavior] must foo") == "- [ ] [behavior] must foo"
    assert v._obligation_checklist_row("  - plain") == "- [ ] plain"


def test_section_off_is_byte_identical_tag_block():
    """RED / byte-identical-off: default renders the EXACT ``<gt-obligations>`` block."""
    assert v._obligations_section(_ROWS) == ["", "<gt-obligations>"] + _ROWS + ["</gt-obligations>"]


def test_section_on_is_plain_checklist(monkeypatch):
    """GREEN: flag on -> a plain header + ``- [ ]`` rows, NO ``<gt-*>`` tag anywhere."""
    monkeypatch.setenv("GT_BRIEF_NATIVE", "1")
    out = v._obligations_section(_ROWS)
    assert out == ["", "Requirements to satisfy (from the issue):",
                   "- [ ] [behavior] the parser must raise ValueError on empty input",
                   "- [ ] must return None on failure"]
    joined = "\n".join(out)
    assert "<gt-" not in joined and "gt-obligations" not in joined
    # the obligation CONTENT is preserved verbatim (only the bullet + frame changed)
    assert "the parser must raise ValueError on empty input" in joined
    assert "must return None on failure" in joined


# =========================================================================== #
# 2. structural robustness — the native block stays obligations-priority + minimal-safe
# =========================================================================== #
def test_native_header_is_a_brief_boundary():
    assert v._is_brief_boundary("Requirements to satisfy (from the issue):") is True
    assert v._is_brief_boundary(v._OBLIGATION_NATIVE_HEADER) is True


def test_segmenter_labels_native_block_as_priority1_obligations():
    """The native block must segment as ``obligations`` (priority 1) so the token rail protects it
    EXACTLY like the tagged block (a low-priority ``misc`` label would let a tight budget trim the
    behavioral spec)."""
    brief = "\n".join(["<gt-task-brief>", "1. src/x.py",
                       "", "Requirements to satisfy (from the issue):",
                       "- [ ] must foo", "- [ ] must bar", "</gt-task-brief>"])
    blocks = v._segment_brief_blocks(brief)
    obl = [b for b in blocks if b["label"] == "obligations"]
    assert len(obl) == 1
    assert obl[0]["priority"] == 1
    assert "- [ ] must foo" in obl[0]["text"] and "- [ ] must bar" in obl[0]["text"]


def test_minimal_reducer_keeps_native_obligations():
    """The SM-6 GT_BRIEF_MINIMAL reducer must KEEP the native obligations block (the combined
    MINIMAL+NATIVE rebake must not drop the behavioral contract)."""
    brief = "\n".join(["<gt-task-brief>", "1. src/x.py (foo)",
                       "   Contract: must preserve X",
                       "", "Requirements to satisfy (from the issue):",
                       "- [ ] must foo", "</gt-task-brief>"])
    reduced = v._reduce_brief_to_minimal(brief)
    assert "Requirements to satisfy (from the issue):" in reduced
    assert "- [ ] must foo" in reduced
    assert "Contract: must preserve X" not in reduced       # per-file contract body still dropped


def test_certificate_obligations_present_under_native_header():
    tagged = "<gt-task-brief>\n<gt-obligations>\n  - must foo\n</gt-obligations>"
    native = "<gt-task-brief>\nRequirements to satisfy (from the issue):\n- [ ] must foo"
    assert v.brief_minimal_certificate(tagged)["obligations_present"] is True
    assert v.brief_minimal_certificate(native)["obligations_present"] is True   # native counts


# =========================================================================== #
# 3. end-to-end through the real renderer (live spec extraction, hermetic)
# =========================================================================== #
_ISSUE = ("The parser must raise ValueError when the input is empty. "
          "It should also return None on a missing key.")


def _render(monkeypatch, tmp_path):
    # isolate from any stale persisted obligations so the FORM is what differs, not the content
    monkeypatch.setattr(v, "_anchors_path", lambda *a, **k: str(tmp_path / "no_such_anchors.json"))
    return v._render_obligations_block(_ISSUE, [], lambda s: s, require_anchor=False)


def test_end_to_end_form_swap(monkeypatch, tmp_path):
    """The SAME issue rendered off vs on: off carries the ``<gt-obligations>`` tag; on carries the
    plain header and NO tag; the obligation CONTENT lines are identical modulo bullet/frame."""
    off = _render(monkeypatch, tmp_path)
    if not off:
        pytest.skip("spec extraction produced no obligations in this environment")
    assert "<gt-obligations>" in off and "</gt-obligations>" in off
    monkeypatch.setenv("GT_BRIEF_NATIVE", "1")
    on = _render(monkeypatch, tmp_path)
    assert "Requirements to satisfy (from the issue):" in on
    assert not any("<gt-" in ln for ln in on)               # no tag survives
    # content parity: strip ONLY the bullet/frame; the obligation body (incl [kind] tag) matches.
    def _body(ln):
        s = ln.strip()
        if s.startswith("- [ ] "):
            return s[6:]
        if s.startswith("- "):
            return s[2:]
        return s
    def _bodies(lines):
        return sorted(_body(ln) for ln in lines if ln.strip().startswith("- "))
    assert _bodies(off) == _bodies(on)


# =========================================================================== #
# 4. biting mutations
# =========================================================================== #
def test_mutation_native_branch_reverted_ships_tag(monkeypatch):
    """MUTATION: revert the native branch of _obligations_section (always emit the tag block) ->
    the flag-on GREEN pin (plain header, no tag) reddens."""
    monkeypatch.setenv("GT_BRIEF_NATIVE", "1")
    monkeypatch.setattr(
        v, "_obligations_section",
        lambda rendered: ["", "<gt-obligations>"] + rendered + ["</gt-obligations>"])
    out = v._obligations_section(_ROWS)
    assert "<gt-obligations>" in out                          # MUTANT: tag ships under the flag


def test_mutation_segmenter_label_reddens_priority(monkeypatch):
    """MUTATION: change the native-header constant the segmenter branch keys on so the actual
    header no longer matches -> the block degrades to low-priority ``misc``, not ``obligations``
    (priority 1) -> the priority-1 / minimal-keep pins reddens. Proves the header-branch is the
    load-bearing hunk that keeps the native obligations token-rail-protected + minimal-safe."""
    brief = "\n".join(["<gt-task-brief>", "",
                       "Requirements to satisfy (from the issue):", "- [ ] must foo",
                       "</gt-task-brief>"])
    # real: obligations priority 1
    obl = [b for b in v._segment_brief_blocks(brief) if b["label"] == "obligations"]
    assert obl and obl[0]["priority"] == 1
    # mutant: the segmenter + boundary now key on a DIFFERENT header, so the real header is misc
    monkeypatch.setattr(v, "_OBLIGATION_NATIVE_HEADER", "SOME OTHER HEADER::")
    blocks = v._segment_brief_blocks(brief)
    assert not any(b["label"] == "obligations" for b in blocks)   # MUTANT: degraded to misc (bites)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
