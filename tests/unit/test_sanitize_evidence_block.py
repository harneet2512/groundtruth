"""TDD for Cluster-1 boundary oracle / semantic Safe Renderer (B1/B2/B3/B3b).

Defects VERIFIED in agent-facing output.jsonl at HEAD 815f1455 (canary 26669671701):
  B3  semantic-nonsense exception  — beets   `Contract: raises raise,exc_info[1].with_traceback`
  B3b empty contract field         — haystack `[GT KEY CONTRACTS]\n  Preserve: guard_clause: `
  B1  prepend glue / marker cut    — haystack `…text wit# SPDX…`; beets `…item") | [CATCHEHere's`
  B2  no central sanitizer         — append/prepend raw concat / raw `text[:600]` slice

The existing `clip_balanced`/`is_well_formed_clause` are STRUCTURAL only — they pass
`raises raise,exc_info[1].with_traceback` (brackets balanced) and an empty guard. C1 adds
SEMANTIC validators + a boundary block sanitizer. Oracles here are INDEPENDENT of the
implementation so they validate real output, not themselves. Language-agnostic by design
(GT is multi-language) — no Python `ast`.
"""
from __future__ import annotations

import re

import pytest

from groundtruth.runtime.sanitizer import (  # noqa: F401 — import IS the first red signal
    valid_exception_spec,
    valid_guard_clause,
    valid_return_shape,
    sanitize_evidence_block,
    join_without_glue,
)

# ---- the 4 REAL bad fixtures (VRA) ----
BEETS_NONSENSE_CONTRACT = "   Contract: raises raise,exc_info[1].with_traceback"
HAYSTACK_EMPTY_FIELD = "[GT KEY CONTRACTS]\n  Preserve: guard_clause: "
HAYSTACK_GLUE_GT = "1. document_splitter.py run() -> docs[0].content == \"This is a text wit"
HAYSTACK_GLUE_FILE = "# SPDX-FileCopyrightText: 2022-present deepset GmbH"
BEETS_CATCHE_GT = '[RAISES] WHEN not items: raise ValueError("need at least one item") | [CATCHES] except Exception'
BEETS_FILE_BANNER = "Here's the result of running `cat -n` on /workspace/beetbox__beets-5495/beets/importer.py"

# ---- valid evidence that MUST survive unchanged (negative controls, VRA) ----
BEANCOUNT_VALID_CONTRACT = "1. beancount/plugins/leafonly.py\n   Contract: returns value|entries, errors"
HAYSTACK_VALID_CONTRACT = (
    "   Contract: raises TypeError,ValueError | preserve raise: not isinstance(documents, list) "
    "or (documents and not isinstance(documents[0], Document)) -> raise TypeError | "
    "returns collection|{\"documents\": split_docs}"
)
BEETS_VALID_PRESERVE = "  Preserve: exception_handler: except UnreadableFileError as exc -> re-raises"


# ===== independent oracles =====
def _has_glue_junction(s: str) -> bool:
    """True if GT marker/word content is fused directly onto a file-read banner with no newline."""
    return bool(re.search(r"\S(?:Here's the result of running|# SPDX-FileCopyrightText)", s))


def _has_truncated_marker(s: str) -> bool:
    """True if a `[MARKER` opener appears with no closing `]` before line end / non-marker char."""
    return bool(re.search(r"\[[A-Z][A-Z _]{2,}(?:\n|$|[^A-Z\]_ ])", s)) and not re.search(r"\[CATCHES\]", s)


def _balanced(s: str) -> bool:
    in_str, esc, depth = "", False, 0
    for ch in s:
        if esc:
            esc = False; continue
        if in_str:
            if ch == "\\": esc = True
            elif ch == in_str: in_str = ""
            continue
        if ch in "\"'": in_str = ch
        elif ch in "([{": depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0: return False
    return not in_str and depth == 0


# ===== negative controls: prove the test detects the bug class =====
def test_negative_control_old_raw_slice_glues():
    """The OLD `text[:600]` + raw concat fuses GT onto the file with no newline."""
    glued = (HAYSTACK_GLUE_GT)[:len(HAYSTACK_GLUE_GT)] + HAYSTACK_GLUE_FILE  # raw concat
    assert _has_glue_junction(glued), "fixture must reproduce the glue the new code must prevent"


def test_negative_control_structural_gate_passes_nonsense():
    """clip_balanced/is_well_formed_clause (structural) WOULD pass the nonsense — that's why we need semantic."""
    from groundtruth.runtime.sanitizer import is_well_formed_clause
    assert is_well_formed_clause("raise,exc_info[1].with_traceback"), "structural gate is blind here (by design)"


# ===== B3: exception spec validator =====
def test_exception_spec_rejects_real_nonsense():
    assert not valid_exception_spec("raise,exc_info[1].with_traceback")  # beets VRA


def test_bare_raises_returns_suppressed():
    """Gap-1 (found by the generalized A-E run): a bare value-less `raises`/
    `returns` segment must be suppressed, not kept."""
    assert "raises" not in sanitize_evidence_block("Contract: raises")
    assert "returns" not in sanitize_evidence_block("Contract: returns")
    assert sanitize_evidence_block("Contract: raises") == ""  # whole line gone (sole segment)
    # valid still kept (negative control)
    assert "raises TypeError" in sanitize_evidence_block("Contract: raises TypeError")
    # mixed: bare raises dropped, valid returns kept
    out = sanitize_evidence_block("Contract: raises | returns value|entries")
    assert "returns value|entries" in out and "raises" not in out


@pytest.mark.parametrize("good", ["TypeError", "TypeError,ValueError", "ReadError,WriteError",
                                  "ConanException", "conan.errors.ConanException"])
def test_exception_spec_accepts_valid(good):
    assert valid_exception_spec(good)


@pytest.mark.parametrize("bad", [
    "", "  ", "raise", "return None", "e.args[0]", "Foo(", "exc_info[1].with_traceback",
    "TypeError,", ",ValueError", "raise ValueError", "throw Err", "1Error",
])
def test_exception_spec_rejects_generalized_adversarial(bad):
    assert not valid_exception_spec(bad)


# ===== B3b: guard clause validator =====
def test_guard_rejects_empty():
    assert not valid_guard_clause("")        # haystack VRA
    assert not valid_guard_clause("   ")


@pytest.mark.parametrize("good", [
    "not isinstance(documents, list)",
    "documents and not isinstance(documents[0], Document)",
    "not conanfile.package_folder",
])
def test_guard_accepts_valid(good):
    assert valid_guard_clause(good)


@pytest.mark.parametrize("bad", ["", "(documents and not", 'raise TypeError("open', "x or", "and"])
def test_guard_rejects_generalized_adversarial(bad):
    assert not valid_guard_clause(bad)


# ===== return shape validator =====
@pytest.mark.parametrize("good", ["value|entries, errors", 'collection|{"documents": split_docs}',
                                  "Optional[User]"])
def test_return_accepts_valid(good):
    assert valid_return_shape(good)


@pytest.mark.parametrize("bad", ["", "value|", "value|(documents and", 'x|"open'])
def test_return_rejects_invalid(bad):
    assert not valid_return_shape(bad)


# ===== B3 at the brief boundary: suppress the nonsense Contract line =====
def test_brief_suppresses_nonsense_contract_line():
    out = sanitize_evidence_block("<gt-task-brief>\n" + BEETS_NONSENSE_CONTRACT + "\n</gt-task-brief>")
    assert "raise,exc_info" not in out, "nonsense raises segment must be suppressed"
    assert "<gt-task-brief>" in out and "</gt-task-brief>" in out, "structure preserved"


# ===== B3b: suppress empty contract field + orphaned header =====
def test_brief_suppresses_empty_guard_field():
    out = sanitize_evidence_block(HAYSTACK_EMPTY_FIELD)
    assert "guard_clause:" not in out, "empty guard_clause field must be suppressed"
    assert "[GT KEY CONTRACTS]" not in out, "orphaned header (no valid Preserve left) must be dropped"


# ===== negative controls: valid contracts UNCHANGED =====
def test_valid_beancount_contract_unchanged():
    out = sanitize_evidence_block(BEANCOUNT_VALID_CONTRACT)
    assert "Contract: returns value|entries, errors" in out


def test_valid_haystack_contract_unchanged():
    out = sanitize_evidence_block(HAYSTACK_VALID_CONTRACT)
    assert "raises TypeError,ValueError" in out
    assert 'returns collection|{"documents": split_docs}' in out


def test_valid_preserve_line_unchanged():
    out = sanitize_evidence_block(BEETS_VALID_PRESERVE)
    assert "except UnreadableFileError as exc -> re-raises" in out


# ===== B1: glue prevention at the junction =====
def test_join_without_glue_inserts_newline():
    out = join_without_glue("...text wit", "# SPDX-FileCopyrightText")
    assert not _has_glue_junction(out), f"still glued: {out!r}"
    assert "\n" in out


def test_join_without_glue_idempotent_when_boundary_present():
    assert join_without_glue("a\n", "b") == "a\nb"
    assert join_without_glue("a", "\nb") == "a\nb"


@pytest.mark.parametrize("gt,filec", [
    # the REAL haystack markerless glues, confirmed from raw output.jsonl bytes:
    ('[TEST] DocumentSplitter(split_length=250, split_ove', "Here's the result of running `cat -n`"),
    ('[TEST] assert docs[0].content == "This is a text wit', "# SPDX-FileCopyrightText: 2022-present"),
])
def test_markerless_glue_prevented_at_boundary(gt, filec):
    """Markerless glue (`split_oveHere's`, `text wit# SPDX`) is the MAJORITY of
    real B1 glue and has no general semantic signature — it is PREVENTED at the
    boundary/join layer (a newline guarantee), NOT detected by a content rule."""
    joined = join_without_glue(gt, filec)
    assert (gt + filec) not in joined, "must not be raw-concatenated (glued)"
    assert joined == gt + "\n" + filec, "boundary must insert exactly one newline"


def test_prepend_path_no_glue_no_cut():
    """Simulate prepend: sanitize the GT block, join to file content — no glue, no marker cut."""
    block = sanitize_evidence_block(BEETS_CATCHE_GT, max_chars=600)
    joined = join_without_glue(block, BEETS_FILE_BANNER)
    assert not _has_glue_junction(joined), f"glued: {joined!r}"
    assert "[CATCHE" not in joined.replace("[CATCHES]", ""), "no truncated [CATCHE marker"


# ===== B2 boundary: safe line-boundary cap, explicit ellipsis, never raw [:N] =====
def test_cap_is_line_boundary_with_ellipsis():
    block = "\n".join([f"line {i} aaaaaaaaaa" for i in range(20)])  # ~360 chars
    out = sanitize_evidence_block(block, max_chars=120)
    assert len(out) <= 140
    assert "…" in out, "explicit ellipsis on truncation"
    # never cut mid-line: every retained non-ellipsis line is a complete original line
    for ln in out.split("\n"):
        if ln and ln != "…":
            assert ln in block.split("\n"), f"mid-line cut: {ln!r}"


def test_empty_in_empty_out():
    assert sanitize_evidence_block("") == ""
    assert sanitize_evidence_block("   \n  ") == ""


def test_hidden_diagnostics_dropped():
    out = sanitize_evidence_block("[GT_META] internal\n[CONTRACT] def f()")
    assert "[GT_META]" not in out
    assert "[CONTRACT] def f()" in out
