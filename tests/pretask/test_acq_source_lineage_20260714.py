"""Candidate-local ACQ lineage is additive, typed, and fail-closed."""

from __future__ import annotations

import hashlib
import sqlite3

from groundtruth.pretask.v1r_brief import (
    _candidate_acquisition_sources,
    _terminal_acquisition_components,
)


def _db(path, *, root: str, file_path: str, digest: str, repo_id: int | None) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE file_hashes (file_path TEXT, content_hash TEXT);"
        "CREATE TABLE nodes (file_path TEXT, repo_id INTEGER);"
        "CREATE TABLE repos (id INTEGER PRIMARY KEY, root TEXT);"
    )
    conn.execute("INSERT INTO file_hashes VALUES (?, ?)", (file_path, digest))
    conn.execute("INSERT INTO nodes VALUES (?, ?)", (file_path, repo_id))
    if repo_id is not None:
        conn.execute("INSERT INTO repos VALUES (?, ?)", (repo_id, root))
    conn.commit()
    conn.close()


def test_candidate_sources_bind_methods_revision_and_active_partition(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "loader.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"def load():\n    return 1\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    graph = tmp_path / "graph.db"
    _db(
        str(graph), root=str(repo), file_path="pkg/loader.py",
        digest=digest, repo_id=7,
    )

    sources = _candidate_acquisition_sources(
        str(graph), str(repo), "pkg/loader.py", {"import", "type_flow", "lsp"}
    )

    assert sources["resolution_honesty"]["all_verified"] is True
    assert sources["type_intelligence"]["methods"] == ["type_flow"]
    assert sources["LSP"]["methods"] == ["lsp"]
    assert sources["freshness_basis"]["indexed_sha256"] == digest
    assert sources["repo_scope"]["active_repo_id"] == 7
    assert "determinism" not in sources


def test_candidate_sources_abstain_on_stale_bytes_unresolved_scope_and_guess(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "loader.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"current bytes\n")
    graph = tmp_path / "graph.db"
    _db(
        str(graph), root=str(tmp_path / "different-root"),
        file_path="pkg/loader.py", digest="0" * 64, repo_id=7,
    )

    sources = _candidate_acquisition_sources(
        str(graph), str(repo), "pkg/loader.py", {"name_match"}
    )

    assert sources == {}


def test_candidate_sources_prove_single_repo_noop_only_with_current_candidate_bytes(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "loader.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"current bytes\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    graph = tmp_path / "graph.db"
    _db(
        str(graph), root=str(repo), file_path="pkg/loader.py",
        digest=digest, repo_id=None,
    )

    sources = _candidate_acquisition_sources(
        str(graph), str(repo), "pkg/loader.py", set()
    )

    assert sources["repo_scope"] == {
        "kind": "repo_partition",
        "is_multi_repo": False,
        "resolved": True,
        "scope_mode": "single_repo_noop",
        "active_repo_id": None,
        "candidate_repo_id": None,
        "candidate_path": "pkg/loader.py",
    }


def test_body_component_requires_real_terminal_body_participation():
    base = {"sem": 0.7, "lex": 0.2}

    participated = _terminal_acquisition_components(
        base, "./pkg/loader.py", body_paths={"pkg/loader.py"},
    )
    absent = _terminal_acquisition_components(
        base, "pkg/other.py", body_paths={"pkg/loader.py"},
    )

    assert participated == {"sem": 0.7, "lex": 0.2, "body": 1.0}
    assert absent == base
    assert "body" not in base
