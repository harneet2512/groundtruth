"""Adversarial contracts for issue relevance versus structural edge truth.

The fixtures are language/repository/task neutral.  They pin the boundary that a
deterministic graph edge proves an edge, not that either endpoint is an edit target.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask.anchors import extract_issue_anchors
from groundtruth.pretask.graph_localizer import Candidate, LocalizerResult, Witness
from groundtruth.pretask.v1r_brief import (
    FileEntry,
    _exact_issue_named_files,
    _localization_header,
    _localization_header_for_entries,
    _model_visible_localization_entries,
    render_brief,
)


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
    is_exported INTEGER DEFAULT 0,
    is_test INTEGER DEFAULT 0,
    language TEXT,
    parent_id INTEGER
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER,
    target_id INTEGER,
    type TEXT,
    source_line INTEGER,
    source_file TEXT,
    resolution_method TEXT,
    confidence REAL,
    metadata TEXT
);
"""


def _graph(path: Path, rows: list[tuple[str, str, str]]) -> str:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes(label,name,file_path,is_test,language) "
        "VALUES ('Function',?,?,0,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return str(path)


def _candidate(path: str, *, anchor: str = "trusted_symbol") -> Candidate:
    witness = Witness(
        file_path=path,
        anchor=anchor,
        edge_type="CALLS",
        direction="calls_anchor",
        verified=True,
        confidence=1.0,
        hop=1,
        src_symbol="candidate_local",
        dst_symbol=anchor,
        resolution_method="import",
    )
    return Candidate(
        file_path=path,
        score=1.0,
        witnesses=[witness],
        lex_hits=0,
        degree=0,
        confidence=1.0,
    )


def test_markdown_headings_and_unique_body_prose_are_queries_not_trusted_anchors(
    tmp_path: Path,
) -> None:
    db = _graph(
        tmp_path / "graph.db",
        [
            ("Error", "src/error.ts", "typescript"),
            ("copy", "src/copy.go", "go"),
            ("show_versions", "src/version.rs", "rust"),
        ],
    )
    issue = "A request fails during normal use\n\n## Error\ncopy the value\n## show_versions"

    anchors = extract_issue_anchors(issue, db)

    assert anchors.symbols == set()
    assert anchors.title_symbols == set()
    assert anchors.symbol_provenance == {
        "Error": "PROSE_QUERY",
        "copy": "PROSE_QUERY",
        "show_versions": "PROSE_QUERY",
    }


def test_only_explicit_code_or_code_like_first_line_can_be_trusted_symbol(
    tmp_path: Path,
) -> None:
    db = _graph(
        tmp_path / "graph.db",
        [
            ("parse_node", "parser/core.py", "python"),
            ("uniqueBodyName", "parser/noise.java", "java"),
            ("quoted_handler", "web/handler.ts", "typescript"),
        ],
    )
    issue = (
        "parse_node rejects a valid document\n\n"
        "uniqueBodyName appears in explanatory prose.\n"
        "The reporter explicitly identifies `quoted_handler`."
    )

    anchors = extract_issue_anchors(issue, db)

    assert anchors.symbols == {"parse_node", "quoted_handler"}
    assert anchors.symbol_provenance["parse_node"] == "TITLE_SYMBOL"
    assert anchors.symbol_provenance["quoted_handler"] == "CODE_SYMBOL"
    assert anchors.symbol_provenance["uniqueBodyName"] == "PROSE_QUERY"


@pytest.mark.parametrize(
    "path",
    ["types/api.pyi", "cmd/main.go", "engine/lib.rs", "ui/widget.ts"],
)
def test_explicit_cross_language_paths_have_permitted_provenance(
    tmp_path: Path,
    path: str,
) -> None:
    db = _graph(tmp_path / "graph.db", [("entry", path, path.rsplit(".", 1)[-1])])
    anchors = extract_issue_anchors(f"The defect is in `{path}`.", db)

    assert path in anchors.paths
    assert anchors.path_provenance[path] == "EXPLICIT_PATH"


def test_traceback_only_path_is_weak_provenance_not_an_exact_candidate(
    tmp_path: Path,
) -> None:
    db = _graph(tmp_path / "graph.db", [("parse", "src/parser.py", "python")])
    issue = 'Traceback (most recent call last):\n  File "src/parser.py", line 8, in parse'
    anchors = extract_issue_anchors(issue, db)

    assert anchors.path_provenance["src/parser.py"] == "TRACEBACK_PATH"
    assert _exact_issue_named_files(issue, db, anchors) == {}


def test_short_explicit_path_is_guaranteed_without_stem_shape_heuristics(
    tmp_path: Path,
) -> None:
    db = _graph(tmp_path / "graph.db", [("run", "x.go", "go")])
    issue = "The defect is in `x.go`."
    anchors = extract_issue_anchors(issue, db)

    assert _exact_issue_named_files(issue, db, anchors) == {"x.go": ["x"]}


def test_verified_edge_is_not_itself_verified_candidate_relevance() -> None:
    candidate = _candidate("src/unrelated.py", anchor="body_prose_name")

    assert candidate.edge_verified is True
    assert candidate.relevance_grade != "VERIFIED"


def test_header_never_appends_an_arbitrary_resolved_edge_tail(monkeypatch) -> None:
    candidate = _candidate("src/candidate.py")
    loc = LocalizerResult(
        [candidate],
        ["trusted_symbol"],
        1.0,
        False,
        "contention",
        agreement_by_file={"src/candidate.py": 1},
    )
    monkeypatch.setattr(
        "groundtruth.pretask.v1r_brief._resolved_witness_tail",
        lambda *_args, **_kwargs: "resolved caller: unrelated() in src/other.py:9",
    )

    header, _primary = _localization_header(loc, "", "trusted_symbol fails")

    assert "src/candidate.py" in header
    assert "src/other.py" not in header
    assert "resolved caller:" not in header


def test_candidate_local_trusted_witness_survives_in_file_entry() -> None:
    brief = render_brief([
        FileEntry(
            "src/candidate.py",
            1.0,
            witness="candidate_local() calls trusted_symbol() [CALLS]",
            witness_verified=True,
            relevance_grade="VERIFIED",
        )
    ])

    assert "Witness: candidate_local() calls trusted_symbol() [CALLS]" in brief


def test_visible_candidate_membership_uses_numbered_candidate_lines_only() -> None:
    entries = [FileEntry("src/a.py", 1.0), FileEntry("src/b.py", 0.5)]
    brief = """<gt-localization confidence="medium">
Candidate edit targets:
  1. src/a.py
     resolved call: -> helper() in src/b.py:7
</gt-localization>"""

    visible = _model_visible_localization_entries(brief, entries)

    assert [entry.path for entry in visible] == ["src/a.py"]


def test_high_header_population_is_intersected_with_terminal_entries(monkeypatch) -> None:
    candidates = [_candidate("src/localizer_only.py"), _candidate("src/terminal.py")]
    loc = LocalizerResult(candidates, ["trusted_symbol"], 1.0, True, "fixture")
    entries = [FileEntry("src/terminal.py", 1.0)]

    def fake_header(view, _graph_db, _issue_text):
        path = view.candidates[0].file_path
        return f'<gt-localization confidence="high">\nEdit target: {path} :: f\n</gt-localization>', path

    monkeypatch.setattr("groundtruth.pretask.v1r_brief._localization_header", fake_header)

    header, primary, tier = _localization_header_for_entries(loc, "", "", entries)

    assert tier == "high"
    assert primary == "src/terminal.py"
    assert "src/localizer_only.py" not in header
