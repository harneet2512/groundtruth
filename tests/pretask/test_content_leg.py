"""B1 content-BM25 leg (Fable hybrid): the retriever + sub-token splitter.

The leg fires only when the live-grep leg recalled nothing (mutually exclusive → no
double-count); these tests pin the two new units it depends on:
  - _content_fts_candidates: BM25 retrieval over symbol_content_fts by BODY content,
    test symbols excluded, graceful when the table is absent.
  - _split_camel_subtokens: parity with the Go indexer's stored sub-token splitting.
"""
import sqlite3

import pytest

from groundtruth.pretask.graph_localizer import (
    _content_fts_candidates,
    _split_camel_subtokens,
)


def _fts5_available() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.close()
        return True
    except sqlite3.OperationalError:
        return False


def _build(rows):
    """rows: list of (id, name, file_path, is_test, content). Returns a conn with a
    nodes table + a populated symbol_content_fts (rowid=id)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, is_test INTEGER)"
    )
    conn.execute("CREATE VIRTUAL TABLE symbol_content_fts USING fts5(content)")
    for nid, name, fp, is_test, content in rows:
        conn.execute("INSERT INTO nodes (id, name, file_path, is_test) VALUES (?,?,?,?)",
                     (nid, name, fp, is_test))
        if content is not None:
            conn.execute("INSERT INTO symbol_content_fts(rowid, content) VALUES (?, ?)",
                         (nid, content))
    conn.commit()
    return conn


def test_split_camel_subtokens():
    assert _split_camel_subtokens("HTTPServer") == ["HTTP", "Server"]
    assert _split_camel_subtokens("getUserById") == ["get", "User", "By", "Id"]
    assert _split_camel_subtokens("simple") == ["simple"]
    assert _split_camel_subtokens("") == []


@pytest.mark.skipif(not _fts5_available(), reason="Python sqlite3 built without FTS5")
def test_content_fts_candidates_retrieves_by_body():
    # alpha's BODY contains 'redis'/'handshake' (NOT its name); beta contains 'widget'.
    conn = _build([
        (1, "alpha", "svc.py", 0, "alpha redis tls handshake connect"),
        (2, "beta", "svc.py", 0, "beta unrelated widget paint"),
    ])
    got = _content_fts_candidates(conn, {"redis", "handshake"})
    ids = [r[0] for r in got]
    assert 1 in ids and 2 not in ids, f"body-content retrieval failed: {got}"
    # camelCase issue symbol must match the stored sub-tokens (query is split too)
    got2 = _content_fts_candidates(conn, {"connectRedisTLS"})
    assert 1 in [r[0] for r in got2], f"sub-token split query failed: {got2}"


@pytest.mark.skipif(not _fts5_available(), reason="Python sqlite3 built without FTS5")
def test_content_fts_candidates_excludes_test_symbols():
    # a TEST node whose body echoes the issue must never be returned (leak-safety;
    # the consumer join filters is_test even if a stale index row exists).
    conn = _build([
        (1, "test_redis_flow", "test_svc.py", 1, "test redis tls handshake assert"),
    ])
    got = _content_fts_candidates(conn, {"redis", "handshake"})
    assert got == [], f"a test symbol leaked into the content leg: {got}"


@pytest.mark.skipif(not _fts5_available(), reason="Python sqlite3 built without FTS5")
def test_content_fts_candidates_graceful_when_table_absent():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, is_test INTEGER)")
    conn.commit()
    assert _content_fts_candidates(conn, {"redis"}) == []
    assert _content_fts_candidates(conn, set()) == []


@pytest.mark.skipif(not _fts5_available(), reason="Python sqlite3 built without FTS5")
def test_localize_reaches_content_leg_on_repoless_no_anchor(tiny_graph_db):
    """WIRING regression (Fable efficacy gap, found via the smoke/efficacy probe): on the
    repo-less + no-anchor stratum-B path, localize() must REACH the content leg and surface
    the gold whose BODY carries the issue vocab — NOT early-abstain 'no_anchor_hit' before
    the leg fires (the content block was dead code behind the 2364 gate). RED before the
    gate fix (localize returned [] on this path); GREEN after. Isolation unit tests of
    _content_fts_candidates could not have caught this — it is a localize()-level gate."""
    import os
    import sqlite3

    from groundtruth.pretask.graph_localizer import localize, _normalize

    GOLD = "patroni/net.py"
    conn = sqlite3.connect(tiny_graph_db)
    # A generically-named function whose BODY (indexed content) carries the domain vocab;
    # give it an edge so it is a connected component (mirrors a real indexed symbol).
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,language,is_test) "
        "VALUES (99,'Function','handle_stream','patroni/net.py',1,9,'python',0)"
    )
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,source_line,source_file,resolution_method,confidence) "
        "VALUES (99,1,'CALLS',3,'patroni/net.py','name_match',0.9)"
    )
    conn.execute("CREATE VIRTUAL TABLE symbol_content_fts USING fts5(content)")
    conn.execute(
        "INSERT INTO symbol_content_fts(rowid,content) "
        "VALUES (99,'websocket upgrade handshake frame opcode payload masking')"
    )
    conn.commit()
    conn.close()

    # Behaviour vocab only — matches NO node name (no anchor); repo-less (no grep).
    issue = "websocket upgrade handshake fails: the opcode frame is rejected during masking"

    os.environ.pop("GT_CONTENT_LEG", None)
    off = localize(issue, tiny_graph_db, top_k=8, repo_root=None)
    assert not any(_normalize(c.file_path) == _normalize(GOLD) for c in off.candidates), (
        "control: without the content leg the stratum-B gold must NOT surface (early-abstain)"
    )

    os.environ["GT_CONTENT_LEG"] = "1"
    try:
        on = localize(issue, tiny_graph_db, top_k=8, repo_root=None)
    finally:
        os.environ.pop("GT_CONTENT_LEG", None)
    assert any(_normalize(c.file_path) == _normalize(GOLD) for c in on.candidates), (
        f"content leg did not surface the stratum-B gold from body vocab: "
        f"{[c.file_path for c in on.candidates]}"
    )
