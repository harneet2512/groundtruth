from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

from groundtruth.hooks.post_edit import generate_improved_evidence


def _make_graph(tmp_path: Path) -> tuple[str, str]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text(
        textwrap.dedent(
            """
            def target(x):
                if x:
                    return x
                return None
            """
        )
    )
    db = tmp_path / "graph.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
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
            source_id INTEGER NOT NULL REFERENCES nodes(id),
            target_id INTEGER NOT NULL REFERENCES nodes(id),
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 1.0,
            metadata TEXT
        );
        INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, signature, language)
        VALUES (1, 'Function', 'target', 'src.app.target', 'src/app.py', 1, 5, 'target(x)', 'python');
        """
    )
    con.close()
    return str(db), str(repo)


def test_failing_mismatch_module_is_logged(monkeypatch, capsys, tmp_path):
    import groundtruth.evidence.mismatch as mismatch

    def fail(*args, **kwargs):
        raise RuntimeError("mismatch unavailable")

    monkeypatch.setattr(mismatch, "detect_stale_references", fail)
    db, repo = _make_graph(tmp_path)

    generate_improved_evidence("src/app.py", ["target"], db, repo)

    # GT_META moved stdout -> stderr (commit a8c870c2, post_edit.py:2732)
    captured = capsys.readouterr()
    assert "mismatch_error: RuntimeError: mismatch unavailable" in captured.err
    assert "mismatch_error" not in captured.out  # must NOT leak into agent stdout


def test_failing_format_module_is_logged(monkeypatch, capsys, tmp_path):
    import groundtruth.evidence.format_contract as format_contract

    def fail(*args, **kwargs):
        raise RuntimeError("format unavailable")

    monkeypatch.setattr(format_contract, "mine_return_shape", fail)
    db, repo = _make_graph(tmp_path)

    generate_improved_evidence("src/app.py", ["target"], db, repo)

    # GT_META moved stdout -> stderr (commit a8c870c2, post_edit.py:2740)
    captured = capsys.readouterr()
    assert "format_contract_error: RuntimeError: format unavailable" in captured.err
    assert "format_contract_error" not in captured.out  # must NOT leak into agent stdout


def test_failing_issue_grounding_module_is_logged(monkeypatch, capsys, tmp_path):
    import groundtruth.evidence.issue_grounding as issue_grounding

    def fail(*args, **kwargs):
        raise RuntimeError("anchors unavailable")

    monkeypatch.setattr(issue_grounding, "load_issue_anchors", fail)
    db, repo = _make_graph(tmp_path)

    generate_improved_evidence("src/app.py", ["target"], db, repo)

    # GT_META moved stdout -> stderr (commit a8c870c2, post_edit.py:2755)
    captured = capsys.readouterr()
    assert "issue_grounding_error: RuntimeError: anchors unavailable" in captured.err
    assert "issue_grounding_error" not in captured.out  # must NOT leak into agent stdout
