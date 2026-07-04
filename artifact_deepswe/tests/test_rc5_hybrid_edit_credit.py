"""RC5 — RED->GREEN proof for the HYBRID obligation edit-credit (edit_coverage_ratio).

The pre-RC5 credit was SINGLE-SOURCE lexical: an obligation symbol counted as
"edited" iff its name appeared as a token in the edit COMMAND string. That gave
the measured inversion:

  - UNDER-COUNT (solved task -> 0.0): the fix landed via apply_patch whose COMMAND
    text never spelled the symbol (the diff body / file state carry it). Old
    membership fails -> ratio 0.0. (abs / aiomonitor / awilix shape.)
  - OVER-COUNT (failed task -> 1.0): the command merely NAMED the symbol (sed
    search pattern / grep pipe / a different function in the same file), but the
    edit never landed AT the symbol's definition. Old membership succeeds -> 1.0.
    (actionlint shape.)

These fixtures mimic the two SHAPES on a tiny synthetic graph.db (no task IDs,
no gold labels, no benchmark metadata — repo/language-agnostic). Each asserts:
  * RED:  the OLD command-token-only credit returns the WRONG (inverted) ratio.
  * GREEN: the NEW >=3-signal hybrid (content-lexical + graph co-location +
           line-range overlap) returns the CORRECT ratio.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gt_mini_patch as g  # noqa: E402


# --- the OLD (pre-RC5) lexical-only credit, reproduced verbatim to prove RED ---
def _old_edit_coverage_ratio(obligation_syms, edited_tokens):
    syms = {s for s in (obligation_syms or ()) if s}
    if not syms:
        return None
    edited = {s for s in syms if s in (edited_tokens or ())}
    return len(edited) / len(syms)


def _build_graph(path, nodes):
    """nodes: list of (name, file_path, label, start_line, end_line)."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "label TEXT, name TEXT, file_path TEXT, start_line INTEGER, "
        "end_line INTEGER, is_test INTEGER DEFAULT 0)"
    )
    for name, fp, label, sl, el in nodes:
        con.execute(
            "INSERT INTO nodes (label, name, file_path, start_line, end_line) "
            "VALUES (?,?,?,?,?)",
            (label, name, fp, sl, el),
        )
    con.commit()
    con.close()


def _tmpdb(nodes):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _build_graph(path, nodes)
    return path


# ---------------------------------------------------------------------------
# (a) UNDER-COUNT shape: command text lacks the symbol token, but the patch
#     CONTENT body + the edited file's graph node + line-overlap all carry it.
#     OLD -> 0.0 (RED, false negative).  NEW -> 1.0 (GREEN).
# ---------------------------------------------------------------------------
def test_under_count_apply_patch_red_then_green():
    rel = "src/importer.py"
    # The graph node for the obligation symbol lives in the edited file at [40,55].
    db = _tmpdb([("set_fields", rel, "Function", 40, 55),
                 ("other", rel, "Function", 1, 30)])
    try:
        obligation = {"set_fields"}

        # apply_patch reads the diff from a SEPARATE staged file — the dominant
        # real shape.  This sub-test isolates the PURE STRUCTURAL credit path: the
        # staged file is ABSENT (a guaranteed-nonexistent path), so neither the
        # command nor the body names 'set_fields' and S2 (the graph node) is the
        # ONLY source of the symbol.  (The live `< file` read-back, where the body
        # DOES carry the symbol, is covered in test_rc5_patch_apply_edit_credit.py.)
        command = "apply_patch < /tmp/_rc5_absent_staged_diff_DO_NOT_CREATE.diff"
        old_cmd_tokens = {t for t in g._BLOCK_TOKEN_RE.findall(command)
                          if len(t) >= 3}
        assert "set_fields" not in old_cmd_tokens  # the command lacks the symbol

        # RED: OLD lexical-only credit (command tokens) wrongly returns 0.0 — the
        # symbol is in the patch CONTENT / file state, not the command text.
        red = _old_edit_coverage_ratio(obligation, old_cmd_tokens)
        assert red == 0.0, f"expected RED 0.0, got {red}"

        # NEW: the structural signal (the edited file's graph node carries
        # 'set_fields' at [40,55]) supplies the content the command lacked; the
        # diff hunk touched lines 42-45 (Signal 3 overlaps).  The staged file is
        # absent, so the body has no symbol — proves S2 is the source.
        content_toks = g._edit_body_tokens(command)  # {'apply_patch','tmp','diff',...}
        assert "set_fields" not in content_toks
        line_ranges = [(42, 45)]  # touched range recovered from the staged hunk

        # GREEN: hybrid credit (graph node co-location + line overlap) -> 1.0,
        # even though neither the command nor the body names the symbol.
        green = g.edit_coverage_ratio(
            obligation, old_cmd_tokens,
            content_toks=content_toks,
            edited_lines={rel: line_ranges},
            db_path=db, edited_files={rel},
        )
        assert green == 1.0, f"expected GREEN 1.0, got {green}"
    finally:
        os.unlink(db)


# ---------------------------------------------------------------------------
# (b) OVER-COUNT shape: command MENTIONS the symbol (sed search pattern) so the
#     command-token credit fires, but the written hunk is at a line range that
#     does NOT overlap the symbol's node span -> the edit site is NOT the symbol.
#     OLD -> 1.0 (RED, false positive).  NEW -> 0.0 (GREEN, rejected).
# ---------------------------------------------------------------------------
def test_over_count_name_mention_red_then_green():
    rel = "src/lexer.py"
    # 'parse' is defined at [10,30]; the edit will land at [80,82] (a different fn).
    db = _tmpdb([("parse", rel, "Function", 10, 30),
                 ("tokenize", rel, "Function", 70, 90)])
    try:
        obligation = {"parse"}

        # The command NAMES 'parse' (in a grep pipe / sed search), but rewrites a
        # hunk at lines 80-82 — the 'tokenize' region, NOT 'parse'.
        command = "grep -n parse src/lexer.py; sed -i '80,82s/old/new/' src/lexer.py"
        old_cmd_tokens = {t for t in g._BLOCK_TOKEN_RE.findall(command)
                          if len(t) >= 3}
        assert "parse" in old_cmd_tokens  # the command names it

        # RED: OLD lexical-only credit wrongly returns 1.0 (false positive).
        red = _old_edit_coverage_ratio(obligation, old_cmd_tokens)
        assert red == 1.0, f"expected RED 1.0, got {red}"

        # NEW evidence: content body tokens still contain 'parse' (it is named),
        # but the touched line range is 80-82 (Signal 3), NOT overlapping [10,30].
        content_toks = g._edit_body_tokens(command)
        assert "parse" in content_toks
        line_ranges = g._edited_line_ranges(command)
        assert line_ranges and line_ranges[0] == (80, 82)

        # GREEN: hybrid rejects — 'parse' node span [10,30] does not overlap the
        # edit site [80,82] -> credit 0.0.
        green = g.edit_coverage_ratio(
            obligation, old_cmd_tokens,
            content_toks=content_toks,
            edited_lines={rel: line_ranges},
            db_path=db, edited_files={rel},
        )
        assert green == 0.0, f"expected GREEN 0.0, got {green}"
    finally:
        os.unlink(db)


# ---------------------------------------------------------------------------
# (c) DORMANT / degrade: graph unreachable -> Signals 2,3 cannot run -> the rule
#     degrades to content-lexical-only (never crashes, never wrongly upgrades to
#     FACT).  And empty obligations -> None (DORMANT) unchanged.
# ---------------------------------------------------------------------------
def test_dormant_and_missing_graph_degrade():
    # DORMANT: no obligations -> None (the correct-or-quiet contract).
    assert g.edit_coverage_ratio(set(), {"anything"}) is None

    # Missing graph: db path is not a file -> _file_symbol_spans returns None for
    # every edited file -> structural signal could not run -> degrade to content-
    # lexical-only (Signal 1). The symbol IS in the content body -> credited
    # (UNCERTAIN), never a crash, never silently treated as structurally proven.
    rel = "src/x.py"
    ratio = g.edit_coverage_ratio(
        {"do_thing"}, {"do_thing"},
        content_toks={"do_thing"},
        edited_lines={},
        db_path="/no/such/graph.db", edited_files={rel},
    )
    assert ratio == 1.0  # degraded content-only credit, no crash

    # The credit decision itself reports UNCERTAIN under a missing graph.
    ok, uncertain = g._credit_obligation_symbol(
        "do_thing", {"do_thing"}, {rel: None}, {})
    assert ok is True and uncertain is True


# ---------------------------------------------------------------------------
# (d) PARITY: a clean solved case where command, content body, node, and line
#     range all AGREE -> 1.0 in BOTH old and new (no regression on the easy case).
# ---------------------------------------------------------------------------
def test_parity_clean_case_no_regression():
    rel = "src/mod.py"
    db = _tmpdb([("compute", rel, "Function", 5, 20)])
    try:
        obligation = {"compute"}
        # An inline sed whose command text spells 'compute' AND lands at line 8.
        command = "sed -i '8s/return 0/return compute(x)/' src/mod.py"
        old_cmd_tokens = {t for t in g._BLOCK_TOKEN_RE.findall(command)
                          if len(t) >= 3}
        # OLD: command names compute -> 1.0.
        assert _old_edit_coverage_ratio(obligation, old_cmd_tokens) == 1.0
        # NEW: content body has 'compute', node [5,20], edit line 8 overlaps -> 1.0.
        green = g.edit_coverage_ratio(
            obligation, old_cmd_tokens,
            content_toks=g._edit_body_tokens(command),
            edited_lines={rel: g._edited_line_ranges(command)},
            db_path=db, edited_files={rel},
        )
        assert green == 1.0
    finally:
        os.unlink(db)


if __name__ == "__main__":
    test_under_count_apply_patch_red_then_green()
    test_over_count_name_mention_red_then_green()
    test_dormant_and_missing_graph_degrade()
    test_parity_clean_case_no_regression()
    print("ALL RC5 HYBRID EDIT-CREDIT TESTS PASSED")
