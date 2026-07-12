"""SM-9a multi-repo READ-side consumer — repo scoping on the Python read path.

SM-9a (Go, task #52) folds multiple repos into one graph.db via a ``repos`` table +
nullable ``repo_id`` partition columns. Before this consumer, the Python readers
(GraphStore name-match, the L1 localizer seed producers) queried ``nodes`` unscoped,
so on a multi-repo db a same-named symbol from the WRONG repository was returned as a
candidate — a cross-repo false positive.

RED-first proof:
  * primitive: single-repo no-op ("",()), multi+resolved scoped, multi+unresolved
    fail-closed (" AND 1=0"), legacy (no repos table) no-op, exact/ambiguous match.
  * GraphStore.find_symbol_by_name: two-repo db, same-named symbol in both -> unscoped
    returns BOTH; scoped to the active repo returns ONLY the active repo's node.
  * localizer seed producers (_seed_node_rows / _path_to_seeds): unscoped seed BOTH
    repos' nodes; scoped seed only the active repo's.
  * localize(): multi-repo + unresolved repo_root fails closed (empty, correct-or-quiet);
    resolved repo_root excludes the other repo's files from the candidate set.
  * BACK-COMPAT PINS: on a single-repo db every reader is byte-identical with/without the
    active_repo_root, and a legacy db (no repos table) is a no-op.
  * MUTATION: neutering node_filter to ("",()) re-leaks the wrong-repo candidate.

Fixtures are SYNTHETIC and generic (repos "alpha"/"beta", symbol "shared_helper") — no
task/repo/language constants; the property under test is the repo partition, not a repo.
"""

from __future__ import annotations

import sqlite3

import pytest

from groundtruth.index import repo_scope
from groundtruth.index.repo_scope import RepoScope, for_read


# --------------------------------------------------------------------------- #
# Fixture builders — a graph.db matching the SM-9a schema subset the readers touch
# --------------------------------------------------------------------------- #

_NODES_DDL = """
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
    parent_id INTEGER,
    {repo_id_col}
);
"""

_EDGES_DDL = """
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    source_line INTEGER,
    source_file TEXT,
    resolution_method TEXT,
    confidence REAL DEFAULT 0.0,
    metadata TEXT,
    trust_tier TEXT DEFAULT 'SPECULATIVE',
    candidate_count INTEGER DEFAULT 1,
    evidence_type TEXT,
    verification_status TEXT DEFAULT 'unverified',
    {repo_id_col}
);
"""

_REPOS_DDL = """
CREATE TABLE repos (id INTEGER PRIMARY KEY, root TEXT, "commit" TEXT);
"""

ROOT_ALPHA = "/synthetic/alpha"
ROOT_BETA = "/synthetic/beta"


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _add_node(conn, *, name, file_path, repo_id, label="Function", is_test=0, parent_id=None):
    cur = conn.execute(
        "INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line, "
        "signature, return_type, is_exported, is_test, language, parent_id, repo_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (label, name, name, file_path, 1, 5, name + "()", None, 1, is_test, "python",
         parent_id, repo_id),
    )
    return cur.lastrowid


def _add_edge(conn, *, src, dst, repo_id, source_file):
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
        "resolution_method, confidence, trust_tier, repo_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (src, dst, "CALLS", 3, source_file, "import", 1.0, "CERTIFIED", repo_id),
    )


def build_multi_repo_db(path: str) -> dict:
    """Two repos, each with a same-named ``shared_helper`` and a same-named
    ``process_order`` that CALLS a DISTINCT downstream file. The colliding names are
    the cross-repo false-positive; the distinct downstream files let localize() show
    which repo's subgraph was traversed."""
    conn = _connect(path)
    conn.executescript(
        _NODES_DDL.format(repo_id_col="repo_id INTEGER") +
        _EDGES_DDL.format(repo_id_col="repo_id INTEGER") +
        _REPOS_DDL
    )
    conn.execute('INSERT INTO repos (id, root, "commit") VALUES (?,?,?)', (0, ROOT_ALPHA, "aaa"))
    conn.execute('INSERT INTO repos (id, root, "commit") VALUES (?,?,?)', (1, ROOT_BETA, "bbb"))

    ids = {}
    # alpha (repo 0)
    ids["a_helper"] = _add_node(conn, name="shared_helper", file_path="alpha/util.py", repo_id=0)
    ids["a_proc"] = _add_node(conn, name="process_order", file_path="alpha/orders.py", repo_id=0)
    ids["a_down"] = _add_node(conn, name="alpha_downstream", file_path="alpha/downstream.py", repo_id=0)
    _add_edge(conn, src=ids["a_proc"], dst=ids["a_down"], repo_id=0, source_file="alpha/orders.py")
    # beta (repo 1)
    ids["b_helper"] = _add_node(conn, name="shared_helper", file_path="beta/util.py", repo_id=1)
    ids["b_proc"] = _add_node(conn, name="process_order", file_path="beta/orders.py", repo_id=1)
    ids["b_down"] = _add_node(conn, name="beta_downstream", file_path="beta/downstream.py", repo_id=1)
    _add_edge(conn, src=ids["b_proc"], dst=ids["b_down"], repo_id=1, source_file="beta/orders.py")

    conn.commit()
    conn.close()
    return ids


def build_single_repo_db(path: str) -> dict:
    """A current single-root index: repo_id column PRESENT but NULL, repos table EMPTY."""
    conn = _connect(path)
    conn.executescript(
        _NODES_DDL.format(repo_id_col="repo_id INTEGER") +
        _EDGES_DDL.format(repo_id_col="repo_id INTEGER") +
        _REPOS_DDL
    )
    ids = {}
    ids["helper"] = _add_node(conn, name="shared_helper", file_path="pkg/util.py", repo_id=None)
    ids["proc"] = _add_node(conn, name="process_order", file_path="pkg/orders.py", repo_id=None)
    ids["down"] = _add_node(conn, name="only_downstream", file_path="pkg/downstream.py", repo_id=None)
    _add_edge(conn, src=ids["proc"], dst=ids["down"], repo_id=None, source_file="pkg/orders.py")
    conn.commit()
    conn.close()
    return ids


def build_legacy_db(path: str) -> None:
    """A pre-SM-9a db: NO repos table, NO repo_id column."""
    conn = _connect(path)
    conn.executescript(
        _NODES_DDL.format(repo_id_col="dummy_col INTEGER").replace(",\n    dummy_col INTEGER", "") +
        _EDGES_DDL.format(repo_id_col="dummy_col INTEGER").replace(",\n    dummy_col INTEGER", "")
    )
    _add_node_legacy(conn, name="shared_helper", file_path="pkg/util.py")
    conn.commit()
    conn.close()


def _add_node_legacy(conn, *, name, file_path):
    conn.execute(
        "INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line, "
        "signature, is_exported, is_test, language) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("Function", name, name, file_path, 1, 5, name + "()", 1, 0, "python"),
    )


# --------------------------------------------------------------------------- #
# 1) PRIMITIVE — detection / resolution / fail-closed
# --------------------------------------------------------------------------- #

def test_primitive_single_repo_is_noop(tmp_path):
    build_single_repo_db(str(tmp_path / "g.db"))
    conn = sqlite3.connect(str(tmp_path / "g.db"))
    scope = for_read(conn, ROOT_BETA)
    assert scope.is_multi_repo is False
    assert scope.resolved is True  # nothing to scope
    assert scope.node_filter() == ("", ())
    assert scope.node_filter("n") == ("", ())


def test_primitive_legacy_no_repos_table_is_noop(tmp_path):
    build_legacy_db(str(tmp_path / "g.db"))
    conn = sqlite3.connect(str(tmp_path / "g.db"))
    scope = for_read(conn, ROOT_BETA)
    assert scope.is_multi_repo is False
    assert scope.node_filter() == ("", ())


def test_primitive_multi_resolved_scopes(tmp_path):
    build_multi_repo_db(str(tmp_path / "g.db"))
    conn = sqlite3.connect(str(tmp_path / "g.db"))
    scope = for_read(conn, ROOT_BETA)
    assert scope.is_multi_repo is True
    assert scope.active_repo_id == 1
    assert scope.resolved is True
    assert scope.node_filter() == (" AND repo_id = ?", (1,))
    assert scope.node_filter("n") == (" AND n.repo_id = ?", (1,))


def test_primitive_multi_unresolved_fails_closed(tmp_path):
    build_multi_repo_db(str(tmp_path / "g.db"))
    conn = sqlite3.connect(str(tmp_path / "g.db"))
    # repo_root that matches no stored root -> unresolved -> fail-closed.
    scope = for_read(conn, "/synthetic/ghost")
    assert scope.is_multi_repo is True
    assert scope.active_repo_id is None
    assert scope.resolved is False
    assert scope.node_filter() == (" AND 1=0", ())


def test_primitive_multi_empty_repo_root_fails_closed(tmp_path):
    build_multi_repo_db(str(tmp_path / "g.db"))
    conn = sqlite3.connect(str(tmp_path / "g.db"))
    scope = for_read(conn, "")
    assert scope.is_multi_repo is True
    assert scope.active_repo_id is None
    assert scope.node_filter() == (" AND 1=0", ())


def test_primitive_root_normalization_trailing_slash_and_sep(tmp_path):
    build_multi_repo_db(str(tmp_path / "g.db"))
    conn = sqlite3.connect(str(tmp_path / "g.db"))
    # Trailing slash + backslash separators normalize to the stored root.
    assert for_read(conn, ROOT_ALPHA + "/").active_repo_id == 0
    assert for_read(conn, "\\synthetic\\beta").active_repo_id == 1


# --------------------------------------------------------------------------- #
# 2) GraphStore name-match reader — the cross-repo false positive
# --------------------------------------------------------------------------- #

def _open_store(path, active_repo_root=None):
    from groundtruth.index.graph_store import GraphStore
    store = GraphStore(path, active_repo_root=active_repo_root)
    res = store.initialize()
    assert not hasattr(res, "error") or res.is_ok() if hasattr(res, "is_ok") else True
    return store


def test_multi_repo_fixture_has_colliding_symbol(tmp_path):
    """The hazard is real IN THE DB: a bare name query returns BOTH repos' node — this
    is what the consumer must stop delivering as fact (proven via raw SQL, no consumer)."""
    p = str(tmp_path / "g.db")
    build_multi_repo_db(p)
    conn = sqlite3.connect(p)
    files = {r[0] for r in conn.execute(
        "SELECT file_path FROM nodes WHERE name = ?", ("shared_helper",)).fetchall()}
    assert files == {"alpha/util.py", "beta/util.py"}


def test_graphstore_no_active_root_multi_fails_closed(tmp_path):
    """A multi-repo db with NO active_repo_root cannot resolve an active repo -> the
    consumer fails closed (empty), NOT leaking both repos' candidates as before."""
    p = str(tmp_path / "g.db")
    build_multi_repo_db(p)
    from groundtruth.index.graph_store import GraphStore
    store = GraphStore(p)  # no active repo specified
    store.initialize()
    res = store.find_symbol_by_name("shared_helper")
    assert res.value == [], "unresolved multi-repo must fail closed, not leak both repos"


def test_graphstore_scoped_multi_returns_only_active_repo(tmp_path):
    p = str(tmp_path / "g.db")
    build_multi_repo_db(p)
    from groundtruth.index.graph_store import GraphStore
    store = GraphStore(p, active_repo_root=ROOT_BETA)
    store.initialize()
    res = store.find_symbol_by_name("shared_helper")
    files = {s.file_path for s in res.value}
    assert files == {"beta/util.py"}, f"cross-repo leak: {files}"


def test_graphstore_scoped_unresolved_fails_closed(tmp_path):
    p = str(tmp_path / "g.db")
    build_multi_repo_db(p)
    from groundtruth.index.graph_store import GraphStore
    store = GraphStore(p, active_repo_root="/synthetic/ghost")
    store.initialize()
    res = store.find_symbol_by_name("shared_helper")
    assert res.value == [], "unresolved multi-repo must fail closed (empty), not leak"


def test_graphstore_get_all_symbol_names_scoped(tmp_path):
    p = str(tmp_path / "g.db")
    build_multi_repo_db(p)
    from groundtruth.index.graph_store import GraphStore
    store = GraphStore(p, active_repo_root=ROOT_ALPHA)
    store.initialize()
    names = set(store.get_all_symbol_names().value)
    assert "alpha_downstream" in names
    assert "beta_downstream" not in names


# --------------------------------------------------------------------------- #
# 3) GraphStore BACK-COMPAT — single-repo byte-identical with/without the feature
# --------------------------------------------------------------------------- #

def test_graphstore_single_repo_byte_identical(tmp_path):
    p = str(tmp_path / "g.db")
    build_single_repo_db(p)
    from groundtruth.index.graph_store import GraphStore

    plain = GraphStore(p)
    plain.initialize()
    scoped = GraphStore(p, active_repo_root=ROOT_BETA)  # active root irrelevant on single-repo
    scoped.initialize()

    for name in ("shared_helper", "process_order", "only_downstream"):
        a = {(s.id, s.file_path) for s in plain.find_symbol_by_name(name).value}
        b = {(s.id, s.file_path) for s in scoped.find_symbol_by_name(name).value}
        assert a == b, f"single-repo path changed for {name}: {a} vs {b}"
    assert set(plain.get_all_symbol_names().value) == set(scoped.get_all_symbol_names().value)
    assert set(plain.get_all_files().value) == set(scoped.get_all_files().value)


# --------------------------------------------------------------------------- #
# 4) GraphStore MUTATION — neuter node_filter -> the leak returns
# --------------------------------------------------------------------------- #

def test_graphstore_mutation_neutered_filter_releaks(tmp_path, monkeypatch):
    p = str(tmp_path / "g.db")
    build_multi_repo_db(p)
    from groundtruth.index.graph_store import GraphStore

    # Neuter the scope so node_filter is a no-op even on a resolved multi-repo db.
    monkeypatch.setattr(RepoScope, "node_filter", lambda self, alias="": ("", ()))
    store = GraphStore(p, active_repo_root=ROOT_BETA)
    store.initialize()
    files = {s.file_path for s in store.find_symbol_by_name("shared_helper").value}
    assert files == {"alpha/util.py", "beta/util.py"}, (
        "MUTATION SHOULD BITE: with node_filter neutered the wrong-repo candidate must return"
    )


# --------------------------------------------------------------------------- #
# 5) Localizer seed producers — scoped seeding
# --------------------------------------------------------------------------- #

def test_seed_node_rows_scoped(tmp_path):
    from groundtruth.pretask.graph_localizer import _seed_node_rows
    from groundtruth.pretask.curation_map import _open_ro

    p = str(tmp_path / "g.db")
    build_multi_repo_db(p)
    conn = _open_ro(p)

    unscoped = _seed_node_rows(conn, {"process_order"})
    files_unscoped = {fp for _, _, fp in unscoped}
    assert files_unscoped == {"alpha/orders.py", "beta/orders.py"}

    scope = for_read(conn, ROOT_BETA)
    scoped = _seed_node_rows(conn, {"process_order"}, scope=scope)
    files_scoped = {fp for _, _, fp in scoped}
    assert files_scoped == {"beta/orders.py"}, f"cross-repo seed leak: {files_scoped}"


def test_path_to_seeds_scoped(tmp_path):
    from groundtruth.pretask.graph_localizer import _path_to_seeds
    from groundtruth.pretask.curation_map import _open_ro

    p = str(tmp_path / "g.db")
    build_multi_repo_db(p)
    conn = _open_ro(p)

    # "orders" is a path token present in both repos' orders.py.
    unscoped = _path_to_seeds(conn, {"orders"}, set(), limit=10)
    assert {fp for _, _, fp in unscoped} == {"alpha/orders.py", "beta/orders.py"}

    scope = for_read(conn, ROOT_ALPHA)
    scoped = _path_to_seeds(conn, {"orders"}, set(), limit=10, scope=scope)
    assert {fp for _, _, fp in scoped} == {"alpha/orders.py"}


def test_seed_node_rows_single_repo_byte_identical(tmp_path):
    from groundtruth.pretask.graph_localizer import _seed_node_rows
    from groundtruth.pretask.curation_map import _open_ro

    p = str(tmp_path / "g.db")
    build_single_repo_db(p)
    conn = _open_ro(p)
    scope = for_read(conn, ROOT_BETA)  # single-repo -> no-op
    a = _seed_node_rows(conn, {"process_order"})
    b = _seed_node_rows(conn, {"process_order"}, scope=scope)
    assert a == b


# --------------------------------------------------------------------------- #
# 6) localize() — fail-closed + resolved exclusion
# --------------------------------------------------------------------------- #

def test_localize_multi_unresolved_fails_closed(tmp_path):
    from groundtruth.pretask.graph_localizer import localize

    p = str(tmp_path / "g.db")
    build_multi_repo_db(p)
    res = localize("process_order returns the wrong total", p, repo_root="/synthetic/ghost")
    assert res.candidates == [], "unresolved multi-repo localize must be empty (correct-or-quiet)"
    assert res.gate_reason == "multi_repo_unresolved"


def test_localize_multi_resolved_excludes_other_repo(tmp_path):
    from groundtruth.pretask.graph_localizer import localize

    p = str(tmp_path / "g.db")
    build_multi_repo_db(p)
    res = localize("process_order returns the wrong total", p, repo_root=ROOT_BETA)
    files = {c.file_path for c in res.candidates}
    assert not any(f.startswith("alpha/") for f in files), f"cross-repo candidate leak: {files}"
    # sanity: the active repo's own files are reachable
    assert any(f.startswith("beta/") for f in files)
