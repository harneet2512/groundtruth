"""RED-first contracts for canonical live-brief acquisition order.

These fixtures contain no benchmark IDs, gold paths, or task-specific constants.  They
exercise the active acquisition cuts whose output can become model-visible: anchor-set
chunking, rg-less file recall, per-file node caps, and semantic encode budgets.
Equivalent repositories must produce the same ordered acquisition result regardless of
process hash seed, filesystem creation order, or graph insertion order.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np

from groundtruth.pretask import anchor_select
from groundtruth.pretask import graph_localizer as gl


_NODE_SCHEMA = """
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


def _new_graph(path: Path, rows: list[tuple[str, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_NODE_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (label,name,file_path,signature,is_test,language) "
        "VALUES ('Function',?,?, '()',0,'python')",
        rows,
    )
    conn.commit()
    conn.close()


def _run_seed_process(db: Path, seed: str) -> subprocess.CompletedProcess[str]:
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    program = f"""
import json, sqlite3, sys
sys.path.insert(0, {source_root!r})
from groundtruth.pretask.graph_localizer import _seed_node_rows
conn = sqlite3.connect({str(db)!r})
anchors = {{f'symbol_{{i:04d}}' for i in range(401)}}
print(json.dumps(_seed_node_rows(conn, anchors), separators=(',', ':')))
conn.close()
"""
    env = {
        **os.environ,
        "PYTHONHASHSEED": seed,
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_seed_rows_are_byte_identical_across_python_hash_seeds(tmp_path: Path) -> None:
    """A >400 anchor set crosses the SQL chunk cut without making set order observable."""
    db = tmp_path / "graph.db"
    _new_graph(
        db,
        [(f"symbol_{i:04d}", f"pkg/mod_{i:04d}.py") for i in range(401)],
    )

    first = _run_seed_process(db, "1")
    second = _run_seed_process(db, "777")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_rows = json.loads(first.stdout)
    second_rows = json.loads(second.stdout)
    mismatch = next(
        (index for index, pair in enumerate(zip(first_rows, second_rows)) if pair[0] != pair[1]),
        None,
    )
    detail = {
        "first_sha256": hashlib.sha256(first.stdout.encode()).hexdigest(),
        "second_sha256": hashlib.sha256(second.stdout.encode()).hexdigest(),
        "first_mismatch_index": mismatch,
        "first_row": first_rows[mismatch] if mismatch is not None else None,
        "second_row": second_rows[mismatch] if mismatch is not None else None,
        "first_tail": first_rows[-2:],
        "second_tail": second_rows[-2:],
    }
    assert first.stdout == second.stdout, json.dumps(detail, sort_keys=True)
    assert len(json.loads(first.stdout)) == 401  # non-vacuous


def _grep_fixture(root: Path, *, reverse: bool) -> tuple[Path, sqlite3.Connection]:
    repo = root / "repo"
    package = repo / "pkg"
    package.mkdir(parents=True)
    file_names = ["a.py", "b.py"]
    for name in reversed(file_names) if reverse else file_names:
        (package / name).write_text("def entry():\n    return 'needle'\n", encoding="utf-8")

    rows: list[tuple[str, str]] = []
    for name in file_names:
        nodes = [(f"{name[0]}_node_{i}", f"pkg/{name}") for i in range(7)]
        rows.extend(reversed(nodes) if reverse else nodes)
    db = root / "graph.db"
    _new_graph(db, rows)
    return repo, sqlite3.connect(db)


def test_no_rg_tied_file_and_node_cuts_ignore_physical_order(
    tmp_path: Path, monkeypatch,
) -> None:
    """Equal file scores and >5 nodes have one canonical result across indexings."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    repo_a, conn_a = _grep_fixture(tmp_path / "forward", reverse=False)
    repo_b, conn_b = _grep_fixture(tmp_path / "reverse", reverse=True)
    try:
        forward = gl._grep_to_seeds({"needle"}, str(repo_a), conn_a, max_seeds=1)
        reverse = gl._grep_to_seeds({"needle"}, str(repo_b), conn_b, max_seeds=1)
    finally:
        conn_a.close()
        conn_b.close()

    assert [(name, path) for _nid, name, path in forward] == [
        (name, path) for _nid, name, path in reverse
    ]
    assert [(name, path) for _nid, name, path in forward] == [
        (f"a_node_{i}", "pkg/a.py") for i in range(5)
    ]


class _EqualEmbedder:
    """Every passage and query has the same unit vector: all semantic scores tie."""

    model_name = "fixture/equal"
    dim = 8

    def encode(self, texts, **_kwargs):
        texts = list(texts)
        return np.ones((len(texts), self.dim), dtype=np.float32) / np.sqrt(self.dim)

    def embed(self, _text, is_query=False):
        del is_query
        return (np.ones(self.dim, dtype=np.float32) / np.sqrt(self.dim)).tolist()

    def embed_batch(self, texts, is_query=False):
        del is_query
        return [self.embed(text) for text in texts]


def _semantic_fixture(root: Path, paths: list[str]) -> Path:
    root.mkdir(parents=True)
    db = root / "graph.db"
    _new_graph(
        db,
        [(f"shared_subject_{Path(path).stem}", path) for path in paths],
    )
    return db


def _scored_semantic_paths(db: Path, root: Path, monkeypatch) -> list[str]:
    anchor_select._EMBED_CACHE.clear()
    anchor_select._SYMVEC_CACHE.clear()
    monkeypatch.setenv("GT_SEM_PASSAGE_BUDGET", "1")
    scores = anchor_select.semantic_top_k(
        "shared subject behavior",
        str(root),
        str(db),
        _EqualEmbedder(),
        k_sem_top=2,
    )
    return list(scores)


def test_semantic_budget_and_top_k_ties_ignore_db_insertion_order(
    tmp_path: Path, monkeypatch,
) -> None:
    """A tight budget and equal cosine cannot select a different file by DB order."""
    paths = ["pkg/a.py", "pkg/b.py"]
    db_a = _semantic_fixture(tmp_path / "forward", paths)
    db_b = _semantic_fixture(tmp_path / "reverse", list(reversed(paths)))

    forward = _scored_semantic_paths(db_a, db_a.parent, monkeypatch)
    reverse = _scored_semantic_paths(db_b, db_b.parent, monkeypatch)

    assert forward == reverse == ["pkg/a.py"]


def test_terminal_evidence_rrf_ties_have_a_canonical_path_order() -> None:
    """Equal evidence must not preserve an arbitrary producer/DB insertion order."""
    records = [
        {"path": "pkg/b.py", "components": {"lex": 1.0}},
        {"path": "pkg/a.py", "components": {"lex": 1.0}},
    ]
    reverse = list(reversed(records))

    first = [r["path"] for r in gl_v1r_apply(records)]
    second = [r["path"] for r in gl_v1r_apply(reverse)]

    assert first == second == ["pkg/a.py", "pkg/b.py"]


def test_terminal_evidence_rrf_ties_preserve_localizer_rank_before_path() -> None:
    """Canonical path must not overrule an existing relevance-bearing localizer rank."""
    records = [
        {"path": "pkg/z.py", "components": {"lex": 1.0}},
        {"path": "pkg/a.py", "components": {"lex": 1.0}},
    ]

    ranked = gl_v1r_apply(records, loc_rank_by_file={"pkg/z.py": 0, "pkg/a.py": 1})

    assert [record["path"] for record in ranked] == ["pkg/z.py", "pkg/a.py"]


def test_total_order_refinement_preserves_unique_relevance_bytes() -> None:
    """A real relevance difference wins before the new canonical tie-break."""
    records = [
        {"path": "pkg/z.py", "components": {"lex": 2.0}},
        {"path": "pkg/a.py", "components": {"lex": 1.0}},
    ]

    rendered = json.dumps(gl_v1r_apply(records), separators=(",", ":"), sort_keys=True)

    assert rendered == (
        '[{"components":{"lex":2.0},"entered_via":"evidence:lexical",'
        '"path":"pkg/z.py"},{"components":{"lex":1.0},'
        '"entered_via":"evidence:lexical","path":"pkg/a.py"}]'
    )


def gl_v1r_apply(
    records: list[dict], *, loc_rank_by_file: dict[str, int] | None = None,
) -> list[dict]:
    """Import at call time so this test names the actual terminal brief cut."""
    from groundtruth.pretask.v1r_brief import _apply_evidence_rrf

    return _apply_evidence_rrf(
        [dict(record) for record in records],
        loc_rank_by_file=loc_rank_by_file,
    )


def _path_seed_fixture(root: Path, *, reverse: bool) -> sqlite3.Connection:
    db = root / "graph.db"
    root.mkdir(parents=True)
    rows: list[tuple[str, str]] = []
    for path in ("pkg/needle/a.py", "pkg/needle/b.py"):
        rows.extend((f"{Path(path).stem}_{index}", path) for index in range(7))
    _new_graph(db, list(reversed(rows)) if reverse else rows)
    return sqlite3.connect(db)


def test_path_seed_limits_ignore_graph_insertion_order(tmp_path: Path) -> None:
    """The five-file cut keeps one canonical node per path across rebuilds."""
    forward_conn = _path_seed_fixture(tmp_path / "forward", reverse=False)
    reverse_conn = _path_seed_fixture(tmp_path / "reverse", reverse=True)
    try:
        forward = gl._path_to_seeds(forward_conn, {"needle"}, set(), limit=10)
        reverse = gl._path_to_seeds(reverse_conn, {"needle"}, set(), limit=10)
    finally:
        forward_conn.close()
        reverse_conn.close()

    def project(rows):
        return [(name, path) for _node_id, name, path in rows]

    assert project(forward) == project(reverse) == [
        ("a_0", "pkg/needle/a.py"),
        ("b_0", "pkg/needle/b.py"),
    ]


def test_compound_path_seed_cap_uses_canonical_path_on_equal_overlap(
    tmp_path: Path,
) -> None:
    rows = [("a_node", "pkg/alpha_beta/a.py"), ("b_node", "pkg/alpha_beta/b.py")]
    forward_db = tmp_path / "compound_forward.db"
    reverse_db = tmp_path / "compound_reverse.db"
    _new_graph(forward_db, rows)
    _new_graph(reverse_db, list(reversed(rows)))
    forward_conn = sqlite3.connect(forward_db)
    reverse_conn = sqlite3.connect(reverse_db)
    try:
        forward = gl._path_to_seeds(
            forward_conn, {"alpha", "beta"}, set(), limit=1
        )
        reverse = gl._path_to_seeds(
            reverse_conn, {"alpha", "beta"}, set(), limit=1
        )
    finally:
        forward_conn.close()
        reverse_conn.close()

    def project(rows):
        return [(name, path) for _node_id, name, path in rows]

    assert project(forward) == project(reverse) == [
        ("a_node", "pkg/alpha_beta/a.py")
    ]


def _over_cap_graph(root: Path, *, reverse: bool, count: int = 81) -> Path:
    rows = [(f"symbol_{index:03d}", "pkg/module.py") for index in range(count)]
    root.mkdir(parents=True)
    db = root / "graph.db"
    _new_graph(db, list(reversed(rows)) if reverse else rows)
    return db


class _TextIdentityEmbedder:
    model_name = "fixture/text-identity"
    dim = 4

    def encode(self, texts, **_kwargs):
        vectors = []
        for text in texts:
            digest = hashlib.sha256(str(text).encode()).digest()
            vector = np.asarray([byte + 1 for byte in digest[: self.dim]], dtype=np.float32)
            vectors.append(vector / np.linalg.norm(vector))
        return np.vstack(vectors)

    def embed(self, text, is_query=False):
        del is_query
        return self.encode([text])[0].tolist()

    def embed_batch(self, texts, is_query=False):
        del is_query
        return self.encode(texts).tolist()


def test_anchor_sixty_passage_cap_uses_source_identity_not_node_id(
    tmp_path: Path, monkeypatch,
) -> None:
    forward_db = _over_cap_graph(tmp_path / "forward", reverse=False)
    reverse_db = _over_cap_graph(tmp_path / "reverse", reverse=True)
    monkeypatch.setenv("GT_SEM_PASSAGE_BUDGET", "1000")
    model = _TextIdentityEmbedder()

    anchor_select._EMBED_CACHE.clear()
    anchor_select._SYMVEC_CACHE.clear()
    _, forward = anchor_select._get_file_embeddings(
        str(forward_db), str(forward_db.parent), model, "symbol behavior"
    )
    anchor_select._EMBED_CACHE.clear()
    anchor_select._SYMVEC_CACHE.clear()
    _, reverse = anchor_select._get_file_embeddings(
        str(reverse_db), str(reverse_db.parent), model, "symbol behavior"
    )

    np.testing.assert_array_equal(forward["pkg/module.py"], reverse["pkg/module.py"])
    assert forward["pkg/module.py"].shape[0] == 60


def test_localizer_eighty_passage_cap_uses_source_identity_not_node_id(
    tmp_path: Path,
) -> None:
    forward_db = _over_cap_graph(tmp_path / "forward", reverse=False)
    reverse_db = _over_cap_graph(tmp_path / "reverse", reverse=True)

    forward, forward_names = gl._assemble_symbol_passages(
        str(forward_db), {"pkg/module.py"}, False, True
    )
    reverse, reverse_names = gl._assemble_symbol_passages(
        str(reverse_db), {"pkg/module.py"}, False, True
    )

    assert forward == reverse
    assert forward_names == reverse_names
    assert forward_names["pkg/module.py"] == [f"symbol_{index:03d}" for index in range(80)]


def test_symbol_anchor_top_k_ties_ignore_graph_insertion_order(tmp_path: Path) -> None:
    rows = [("AlphaFeature", "pkg/a.py"), ("BetaFeature", "pkg/b.py")]
    forward_db = tmp_path / "forward.db"
    reverse_db = tmp_path / "reverse.db"
    _new_graph(forward_db, rows)
    _new_graph(reverse_db, list(reversed(rows)))

    forward = anchor_select._symbol_anchors(
        "AlphaFeature and BetaFeature", str(forward_db), k_anchor=1
    )
    reverse = anchor_select._symbol_anchors(
        "AlphaFeature and BetaFeature", str(reverse_db), k_anchor=1
    )

    assert list(forward) == list(reverse) == ["pkg/a.py"]


def _fts_fixture(path: Path, *, reverse: bool) -> sqlite3.Connection:
    rows = [("target", "pkg/a.py"), ("target", "pkg/b.py")]
    _new_graph(path, list(reversed(rows)) if reverse else rows)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE VIRTUAL TABLE nodes_fts USING "
        "fts5(name, qualified_name, signature, file_path)"
    )
    for node_id, name, file_path in conn.execute(
        "SELECT id, name, file_path FROM nodes"
    ).fetchall():
        conn.execute(
            "INSERT INTO nodes_fts(rowid,name,qualified_name,signature,file_path) "
            "VALUES (?,?,?,?,?)",
            (node_id, name, name, "()", file_path),
        )
    conn.commit()
    return conn


def test_fts_score_ties_use_canonical_path_before_limit(tmp_path: Path) -> None:
    forward_conn = _fts_fixture(tmp_path / "forward.db", reverse=False)
    reverse_conn = _fts_fixture(tmp_path / "reverse.db", reverse=True)
    try:
        forward = gl._fts5_candidates(forward_conn, {"target"}, limit=1)
        reverse = gl._fts5_candidates(reverse_conn, {"target"}, limit=1)
    finally:
        forward_conn.close()
        reverse_conn.close()

    def project(rows):
        return [(name, path) for _id, name, path, _score in rows]

    assert project(forward) == project(reverse) == [("target", "pkg/a.py")]


def test_localizer_serializes_anchor_set_canonically(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "graph.db"
    _new_graph(db, [("alpha", "pkg/a.py"), ("beta", "pkg/b.py")])
    monkeypatch.setattr(gl, "_get_embedder", lambda: None)
    anchors = gl.IssueAnchors(symbols={"beta", "alpha"})

    result = gl.localize(
        "alpha beta", str(db), issue_anchors=anchors, repo_root="", top_k=2
    )

    assert result.anchor_symbols == ["alpha", "beta"]
