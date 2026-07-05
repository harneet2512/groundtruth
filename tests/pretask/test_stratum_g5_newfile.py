"""G5 pins: the stratum classifier over-called 'D' (new-file) on prose/error tokens.

Two halves of the fix:
  (anchors) unresolved_code_symbols admits only IDENTIFIER-shaped tokens, so error
    prose inside a fenced block ("cannot", "mismatch", "character") no longer looks
    like the API-to-be-built.
  (stratum) when RESOLVED anchors exist, routing to D additionally requires positive
    new-file evidence (a feature-add verb), so a few stray unresolved tokens cannot
    steal an existing-file bug into D.

Pure sqlite fixture; no task IDs, no gold labels.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.pretask.anchors import IssueAnchors, extract_issue_anchors
from groundtruth.pretask.stratum import classify_stratum


def _mk_graph(db_path: Path, names: list[str]) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, "
        "name TEXT, qualified_name TEXT, file_path TEXT, start_line INTEGER, "
        "end_line INTEGER, signature TEXT, is_test BOOLEAN DEFAULT 0, "
        "language TEXT DEFAULT 'python', parent_id INTEGER)"
    )
    for i, n in enumerate(names):
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, start_line, end_line) "
            "VALUES ('Function', ?, ?, 1, 2)",
            (n, f"src/mod{i}.py"),
        )
    conn.commit()
    conn.close()
    return str(db_path)


# --- anchors half ----------------------------------------------------------
def test_g5_prose_tokens_in_fence_not_admitted_as_unresolved(tmp_path) -> None:
    db = _mk_graph(tmp_path / "g.db", ["parse_config", "load_state"])
    issue = (
        "parse_config drops keys after load_state runs.\n\n"
        "```\n"
        "Error: cannot decode value, character mismatch at offset 3\n"
        "```\n"
    )
    a = extract_issue_anchors(issue, db)
    for prose in ("cannot", "mismatch", "character", "decode", "offset"):
        assert prose not in a.unresolved_code_symbols, (
            f"prose token {prose!r} leaked into unresolved tier: "
            f"{sorted(a.unresolved_code_symbols)}")


def test_g5_identifier_shaped_unresolved_still_admitted(tmp_path) -> None:
    db = _mk_graph(tmp_path / "g2.db", ["BeginRepl"])
    issue = (
        "Add `require_cache_info()` and `ABS_MODULE_PATH` support.\n"
        "Preserve `BeginRepl`.\n"
    )
    a = extract_issue_anchors(issue, db)
    assert "require_cache_info" in a.unresolved_code_symbols
    assert "ABS_MODULE_PATH" in a.unresolved_code_symbols


# --- stratum half ----------------------------------------------------------
def test_g5_five_resolved_plus_prose_is_not_stratum_D(tmp_path) -> None:
    """The named finding case: 5 resolved anchors + error prose -> A/B, not D."""
    names = ["parse_config", "load_state", "flush_cache", "reset_index", "Encoder"]
    db = _mk_graph(tmp_path / "g3.db", names)
    issue = (
        "Encoder.parse_config drops entries when load_state and flush_cache race; "
        "reset_index does not recover.\n\n"
        "```\n"
        "Traceback stripped — cannot decode, character mismatch, value error\n"
        "```\n"
    )
    # (no fenced 'Traceback (most recent call last):' marker, so C does not fire)
    r = classify_stratum(issue, db)
    assert r.label in ("A", "B"), f"expected A/B, got {r.label}: {r.evidence}"
    assert r.label != "D"


def test_g5_verb_gate_when_resolved_anchors_present() -> None:
    """With resolved anchors AND unresolved dominance, D fires ONLY with a
    feature-add verb (positive new-file evidence)."""
    anchors = IssueAnchors(
        symbols={"Foo"},
        unresolved_code_symbols={"bar_baz", "qux_quux"},
    )
    # feature verb in title -> D
    d = classify_stratum("Add support for bar_baz and qux_quux on Foo.",
                         issue_anchors=anchors)
    assert d.label == "D"
    # same anchors, NO feature verb -> must NOT be D (existing-file bug)
    a = classify_stratum("Foo mishandles bar_baz and qux_quux under load.",
                        issue_anchors=anchors)
    assert a.label != "D"
    assert a.label == "A"  # resolved anchor present
