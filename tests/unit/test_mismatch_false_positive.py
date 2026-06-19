"""Correct-or-quiet: the FALSE [MISMATCH] regression in evidence/mismatch.py.

Observed defect (generalized from the loguru run):
``_extract_removed_identifiers`` flags any identifier on a ``-`` diff line that
is absent from the diff's ``+`` lines, and ``_find_test_references`` then warns
whenever ``rid in content`` ANYWHERE in a test file. So a stdlib name the test
imports for ITS OWN use (``from datetime import timezone``) was reported as
"you removed ``timezone`` but tests still reference it" — a misdirecting false
positive that violates correct-or-quiet.

These tests pin BOTH directions:

  (a) NO over-suppression of the false positive: a diff that drops a stdlib name
      which is STILL PRESENT in the post-edit file (and only referenced by a
      test for the test's own use, on a line that does not touch the edited
      symbol) must produce NO [MISMATCH]. (the loguru case, generalized — no
      hardcoded 'timezone'/'datetime'.)

  (b) NEGATIVE CONTROL — no over-suppression of REAL mismatches: a diff that
      genuinely removes a parameter (gone from the post-edit file entirely) that
      a test still asserts on the edited symbol MUST still fire [MISMATCH].

Red-before-green: with the pre-fix reader, (a) yields a [MISMATCH] (the bug);
the fix makes (a) silent while (b) keeps firing.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from groundtruth.evidence.mismatch import detect_stale_references

# ---------------------------------------------------------------------------
# Schema helpers — build a Go-indexer-shape graph.db directly (the real schema
# production reads). Mirrors the convention in test_closure_staleness.py.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT,
    file_path TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    return_type TEXT,
    is_exported BOOLEAN DEFAULT 0,
    is_test BOOLEAN DEFAULT 0,
    language TEXT NOT NULL,
    parent_id INTEGER REFERENCES nodes(id)
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    source_line INTEGER,
    source_file TEXT,
    resolution_method TEXT,
    confidence REAL DEFAULT 0.0,
    metadata TEXT
);
"""


def _build_db(
    db_path: Path,
    *,
    edited_file: str,
    edited_func: str,
    test_file: str,
    test_func: str,
    test_line: int,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    # node 1 = the edited function (target of the test's CALLS edge)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, is_test, language) "
        "VALUES (1, 'Function', ?, ?, 0, 'python')",
        (edited_func, edited_file),
    )
    # node 2 = the test function that calls the edited function (is_test=1)
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, is_test, language) "
        "VALUES (2, 'Function', ?, ?, 1, 'python')",
        (test_func, test_file),
    )
    # VERIFIED CALLS edge test_func -> edited_func (conf 1.0, passes conf gate)
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, source_line, "
        "resolution_method, confidence) VALUES (2, 1, 'CALLS', ?, 'import', 1.0)",
        (test_line,),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# (a) THE LOGURU CASE, GENERALIZED — stdlib name still present → NO mismatch
# ---------------------------------------------------------------------------

def test_stdlib_name_still_present_emits_no_mismatch(tmp_path: Path) -> None:
    """A ``-`` diff line drops a stdlib member (``timezone``) but the post-edit
    file STILL imports/uses it; the test references it only for the test's own
    setup, on a line that does not touch the edited symbol. correct-or-quiet:
    NO [MISMATCH]."""
    repo = tmp_path
    edited_rel = "loguru/_recattrs.py"
    test_rel = "tests/test_recattrs.py"

    # Post-edit file: timezone is STILL present (imported + used) — the diff
    # only moved where it is referenced; it did NOT leave the file.
    (repo / "loguru").mkdir()
    (repo / "loguru" / "_recattrs.py").write_text(
        "from datetime import timezone\n"
        "\n"
        "def _make_record(elapsed):\n"
        "    tz = timezone.utc\n"
        "    return {'elapsed': elapsed, 'tz': tz}\n",
        encoding="utf-8",
    )

    # Test file: imports timezone for ITS OWN assertion setup; the line that
    # mentions timezone does NOT mention the edited func _make_record.
    (repo / "tests").mkdir()
    (repo / "tests" / "test_recattrs.py").write_text(
        "from datetime import timezone\n"
        "from loguru._recattrs import _make_record\n"
        "\n"
        "def test_make_record():\n"
        "    expected_tz = timezone.utc\n"
        "    rec = _make_record(0.5)\n"
        "    assert rec['elapsed'] == 0.5\n",
        encoding="utf-8",
    )

    db = repo / "graph.db"
    _build_db(
        db,
        edited_file=edited_rel,
        edited_func="_make_record",
        test_file=test_rel,
        test_func="test_make_record",
        test_line=5,
    )

    # The diff: a refactor hunk where a line referencing timezone was removed.
    # 'timezone' is on a '-' line and NOT echoed on any '+' line of this hunk —
    # exactly the shape that tripped the bug (the diff's own '+' filter does not
    # save it; the post-edit FILE still containing timezone is what must).
    diff_text = (
        "--- a/loguru/_recattrs.py\n"
        "+++ b/loguru/_recattrs.py\n"
        "@@ -3,4 +3,4 @@\n"
        " def _make_record(elapsed):\n"
        "-    legacy_zone = timezone\n"
        "+    pass\n"
        "     return {'elapsed': elapsed}\n"
    )

    warnings = detect_stale_references(
        db_path=str(db),
        file_path=edited_rel,
        func_name="_make_record",
        diff_text=diff_text,
        repo_root=str(repo),
    )

    mismatches = [w for w in warnings if "timezone" in w]
    assert not mismatches, (
        "FALSE [MISMATCH]: 'timezone' is a stdlib member still present in the "
        f"post-edit file and only used by the test for its own setup. "
        f"correct-or-quiet requires silence. Got: {warnings}"
    )


def test_module_level_stdlib_name_excluded(tmp_path: Path) -> None:
    """Defense (1): a top-level stdlib module name on a '-' line is never an
    agent-introduced removal worth warning on (computed via
    sys.stdlib_module_names, not a hardcoded list)."""
    # pick any real stdlib top-level module name dynamically
    stdlib_name = "subprocess"
    assert stdlib_name in sys.stdlib_module_names

    repo = tmp_path
    edited_rel = "pkg/runner.py"
    test_rel = "tests/test_runner.py"

    (repo / "pkg").mkdir()
    (repo / "pkg" / "runner.py").write_text(
        "def run():\n    return 1\n", encoding="utf-8"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_runner.py").write_text(
        f"import {stdlib_name}\n"
        "from pkg.runner import run\n"
        "\n"
        "def test_run():\n"
        "    assert run() == 1\n",
        encoding="utf-8",
    )

    db = repo / "graph.db"
    _build_db(
        db,
        edited_file=edited_rel,
        edited_func="run",
        test_file=test_rel,
        test_func="test_run",
        test_line=4,
    )

    diff_text = (
        "--- a/pkg/runner.py\n"
        "+++ b/pkg/runner.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def run():\n"
        f"-    {stdlib_name}.run(['x'])\n"
        "+    return 1\n"
    )

    warnings = detect_stale_references(
        db_path=str(db),
        file_path=edited_rel,
        func_name="run",
        diff_text=diff_text,
        repo_root=str(repo),
    )
    assert not any(stdlib_name in w for w in warnings), (
        f"stdlib module '{stdlib_name}' must never be flagged as a removed "
        f"identifier. Got: {warnings}"
    )


# ---------------------------------------------------------------------------
# (b) NEGATIVE CONTROL — a GENUINELY removed param a test asserts → MISMATCH
# ---------------------------------------------------------------------------

def test_genuinely_removed_param_still_fires_mismatch(tmp_path: Path) -> None:
    """No over-suppression: a parameter the agent truly removed (gone from the
    post-edit file entirely) that a test still passes/asserts on the edited
    symbol MUST still produce a [MISMATCH]."""
    repo = tmp_path
    edited_rel = "app/config.py"
    test_rel = "tests/test_config.py"

    # Post-edit file: 'old_url' is GONE — it does not appear anywhere.
    (repo / "app").mkdir()
    (repo / "app" / "config.py").write_text(
        "def set_url(new_url):\n"
        "    return new_url\n",
        encoding="utf-8",
    )

    # Test file: still calls/asserts set_url with the removed 'old_url' kwarg,
    # on a line referencing the edited func AND containing an assertion.
    (repo / "tests").mkdir()
    (repo / "tests" / "test_config.py").write_text(
        "from app.config import set_url\n"
        "\n"
        "def test_set_url():\n"
        "    assert set_url(old_url='x') == 'x'\n",
        encoding="utf-8",
    )

    db = repo / "graph.db"
    _build_db(
        db,
        edited_file=edited_rel,
        edited_func="set_url",
        test_file=test_rel,
        test_func="test_set_url",
        test_line=4,
    )

    # Diff genuinely removes the 'old_url' parameter (not echoed on any '+' line
    # and gone from the post-edit file).
    diff_text = (
        "--- a/app/config.py\n"
        "+++ b/app/config.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def set_url(old_url):\n"
        "-    return old_url\n"
        "+def set_url(new_url):\n"
        "+    return new_url\n"
    )

    warnings = detect_stale_references(
        db_path=str(db),
        file_path=edited_rel,
        func_name="set_url",
        diff_text=diff_text,
        repo_root=str(repo),
    )

    assert any("old_url" in w and "MISMATCH" in w for w in warnings), (
        "Negative control failed: a genuinely-removed param the test still "
        f"asserts on the edited symbol MUST fire [MISMATCH]. Got: {warnings}"
    )
