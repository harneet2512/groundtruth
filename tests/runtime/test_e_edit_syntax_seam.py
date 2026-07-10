"""Stage-1: the E (edit.syntax) SEAM — the wiring of ``check_edit_syntax`` into the
mini-swe-agent delivery gate.

The ENGINE (``check_edit_syntax`` / ``caller_diff_advisory``) is pinned by
``test_e_edit_syntax_check.py``. This file pins the SEAM the engine was missing:
- ``native_render.render_syntax_error_native`` — the toolchain diagnostic as a
  native, ``<gt-*>``-free, identity-free compiler observation (correct-or-quiet).
- ``gt_mini_patch._edit_syntax_candidate`` — the producer: flag-gated GT_EDIT_CHECK
  (byte-identical + zero work off), correct-or-quiet, leak-law test-file skip.
- the phase exemption: an ``edit.syntax`` world-fact survives a phase with no policy
  entry (the same exemption ``verify.horizon.executed`` earns).

Red-before-green is proven by the mutation notes on each assertion cluster; the
committed mutation check bites the flag guard and the verdict guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ARTIFACT = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
if _ARTIFACT not in sys.path:
    sys.path.insert(0, _ARTIFACT)

from groundtruth.runtime.native_render import (  # noqa: E402
    contains_gt_tag,
    contains_test_identity,
    render_syntax_error_native,
)

gmp = pytest.importorskip("gt_mini_patch")

_BROKEN_PY = "def foo(\n    return 1\n"   # unclosed paren -> SyntaxError
_CLEAN_PY = "def foo():\n    return 1\n"


# --------------------------------------------------------------------------- #
# render_syntax_error_native — the native diagnostic (correct-or-quiet)
# --------------------------------------------------------------------------- #
def test_render_syntax_error_returns_native_diagnostic():
    diag = 'File "m.py", line 1\n    def foo(\n           ^\nSyntaxError: never closed'
    out = render_syntax_error_native({"verdict": "syntax_error", "diagnostic": diag})
    assert "SyntaxError" in out
    assert not contains_gt_tag(out)          # §0 native voice
    assert not contains_test_identity(out)   # leak invariant (source edit — no test)


def test_render_scrubs_gt_tag_defense_in_depth():
    # MUTATION: drop the _RE_GT_TAG.sub -> this leaks a tag -> assertion bites.
    out = render_syntax_error_native(
        {"verdict": "syntax_error", "diagnostic": "SyntaxError <gt-note>x</gt-note>"})
    assert out and not contains_gt_tag(out)


@pytest.mark.parametrize("verdict", ["ok", "unavailable", "", None])
def test_render_quiet_on_non_syntax_error(verdict):
    # MUTATION: relax the `verdict != "syntax_error"` guard -> speaks on ok -> bites.
    assert render_syntax_error_native({"verdict": verdict, "diagnostic": "x"}) == ""


def test_render_quiet_on_empty_diagnostic():
    assert render_syntax_error_native({"verdict": "syntax_error", "diagnostic": ""}) == ""


# --------------------------------------------------------------------------- #
# _edit_syntax_candidate — the producer (flag-gated, correct-or-quiet, leak-safe)
# --------------------------------------------------------------------------- #
@pytest.fixture
def _repo(tmp_path, monkeypatch):
    """A tmp repo whose _root() the producer reads; baseline off; executor=host."""
    monkeypatch.setattr(gmp, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(gmp, "_GT_BASELINE", False, raising=False)
    return tmp_path


def _write(repo: Path, rel: str, body: str) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return rel


def test_producer_fires_on_broken_source_edit(_repo, monkeypatch):
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    rel = _write(_repo, "pkg/mod.py", _BROKEN_PY)
    cand = gmp._edit_syntax_candidate(rel)
    assert cand is not None
    sev, kind, block, edit_bound = cand
    assert kind == "edit.syntax"
    assert edit_bound is True
    assert "SyntaxError" in block
    assert not contains_gt_tag(block)
    assert not contains_test_identity(block)


def test_producer_byte_identical_off(_repo, monkeypatch):
    # MUTATION: flip the `!= "1"` flag guard -> fires off -> this bites.
    monkeypatch.delenv("GT_EDIT_CHECK", raising=False)
    rel = _write(_repo, "pkg/mod.py", _BROKEN_PY)
    assert gmp._edit_syntax_candidate(rel) is None


def test_producer_quiet_on_clean_source(_repo, monkeypatch):
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    rel = _write(_repo, "pkg/mod.py", _CLEAN_PY)
    assert gmp._edit_syntax_candidate(rel) is None


def test_producer_leaklaw_skips_test_file(_repo, monkeypatch):
    # A broken TEST file the agent wrote must NOT surface (its path in the
    # diagnostic would trip the identity invariant). MUTATION: drop the
    # _is_post_search_testpath skip -> this fires -> bites.
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    rel = _write(_repo, "tests/test_mod.py", _BROKEN_PY)
    assert gmp._edit_syntax_candidate(rel) is None


def test_producer_quiet_under_baseline(_repo, monkeypatch):
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    monkeypatch.setattr(gmp, "_GT_BASELINE", True, raising=False)
    rel = _write(_repo, "pkg/mod.py", _BROKEN_PY)
    assert gmp._edit_syntax_candidate(rel) is None


# --------------------------------------------------------------------------- #
# phase exemption — an edit.syntax world-fact survives a no-policy phase
# --------------------------------------------------------------------------- #
def test_edit_syntax_phase_exempt():
    # A phase with no edit.syntax policy entry must NOT drop it (world-fact, like
    # verify.horizon.executed). MUTATION: remove "edit.syntax" from the exemption
    # tuple -> it phase-drops -> kept is empty -> bites.
    cand = (float(gmp._SEV_NUDGE_VERIFY), "edit.syntax", "SyntaxError: x", True)
    kept = gmp._filter_candidates_by_phase([cand], gmp.Phase.VIEW, gmp.Event.POST_EDIT)
    assert cand in kept
