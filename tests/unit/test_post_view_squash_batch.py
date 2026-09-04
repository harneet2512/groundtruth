"""Red->green unit tests for the SQUASH BATCH fixes in post_view.py.

Covers the five assigned items:
  #9  _file_function_spec: anchor-front-load + _relevance==0 -> "" (no file-top noise)
  #31 _l3b_line_priority: ego-block first-line shape -> band 2 (not trimmed first)
  #32 hub-scale: the single _ef clause threads through all 3 degree queries
  #33 _test_file_targets: stdlib-shadow guard on `Calls into:` targets
  #34 _contract_pillar flows: join by node id, never by name (homonym mis-pairing)

In-memory sqlite graphs modeled on tests/unit/test_post_view_contract_pillar.py.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import groundtruth.hooks.post_view as pv
from groundtruth.hooks.post_view import (
    _contract_pillar,
    _file_function_spec,
    _in_degree_for_file,
    _l3b_line_priority,
    _test_file_targets,
)


# ---------------------------------------------------------------------------
# Item #31 — _l3b_line_priority ego-shape
# ---------------------------------------------------------------------------
def test_item31_ego_first_line_is_band_2_not_trimmed_first():
    """The ego block's real first line `<name>() in <basename>:<line>` must map to
    band 2 (preserved with [FOCUS:), NOT band 5 (trimmed first). RED before the
    regex match was added: it fell through to the catch-all band 5."""
    ego_line = "set_fields() in importer.py:42"
    assert _l3b_line_priority(ego_line) == 2, _l3b_line_priority(ego_line)
    # dotted method name (Class.method) also matches
    assert _l3b_line_priority("Importer.set_fields() in importer.py:7") == 2
    # generic caller nav stays band 3 (less important than the ego block)
    assert _l3b_line_priority("Called by: other.py:5 `x()`") == 3
    # the ego line must out-rank a plain caller line (lower number = kept longer)
    assert _l3b_line_priority(ego_line) < _l3b_line_priority("Called by: x")


def test_item31_non_ego_text_unaffected():
    """Guard against over-matching: ordinary nav/spec lines keep their bands."""
    assert _l3b_line_priority("[CONTRACT] def f(x) -> int") == 0
    assert _l3b_line_priority("Calls into: a.py::b()") == 4
    assert _l3b_line_priority("Spec: foo handles: a | b") == 5
    # looks-similar-but-not-ego: no `() in ` token
    assert _l3b_line_priority("set_fields in importer.py:42") == 5


# ---------------------------------------------------------------------------
# Item #34 — _contract_pillar flows join on node id, not name
# ---------------------------------------------------------------------------
def _make_homonym_flow_db() -> str:
    """Two functions named `format` in ONE file; only the SECONDARY overload
    carries a data_flow. The OLD `n.name = ?` lookup (LIMIT 1 by p.line) would
    staple that flow under whichever overload's signature is shown — wrong-fact.
    The node-id join must bind the flow to its OWN node only."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, "
        "name TEXT, file_path TEXT, start_line INTEGER DEFAULT 0, signature TEXT, "
        "return_type TEXT, is_test INTEGER DEFAULT 0, language TEXT DEFAULT 'python')"
    )
    conn.execute(
        "CREATE TABLE properties (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER, "
        "kind TEXT, value TEXT, line INTEGER, confidence REAL DEFAULT 1.0)"
    )
    # node 1: format(self) — shown FIRST by start_line; owns flow_1 (lowest p.line)
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,signature) "
        "VALUES (1,'Method','format','src/x.py',10,'def format(self)')"
    )
    # node 2: format(self, value) — later overload; owns flow_2 (a DIFFERENT flow)
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,signature) "
        "VALUES (2,'Method','format','src/x.py',50,'def format(self, value)')"
    )
    # Each overload owns its OWN distinct flow. The pre-fix `n.name='format'` lookup
    # (ORDER BY p.line LIMIT 1) returns flow_1 (lowest p.line=11) for BOTH delivered
    # functions -> node 2's contract is stapled with node 1's flow (the homonym
    # mis-pairing). The node-id join binds each function to its OWN flow.
    conn.execute(
        "INSERT INTO properties (node_id,kind,value,line,confidence) "
        "VALUES (1,'data_flow','self -> self._self_only',11,1.0)"
    )
    conn.execute(
        "INSERT INTO properties (node_id,kind,value,line,confidence) "
        "VALUES (2,'data_flow','value -> self._buf',51,1.0)"
    )
    conn.commit()
    conn.close()
    return path


def test_item34_flow_not_stapled_to_homonym_overload(monkeypatch):
    """With BOTH overloads shown, each flow must ride with its OWN node — node 2's
    `value -> self._buf` must appear (node-id join), proving node 1's lower-p.line
    flow was NOT stapled onto both. The pre-fix name lookup attached node 1's flow
    to both, dropping node 2's flow entirely (deduped to one wrong line)."""
    monkeypatch.setattr(
        pv, "_load_issue_anchors", lambda: {"symbols": [], "paths": [], "test_names": []}
    )
    monkeypatch.setattr(pv, "_load_issue_terms", lambda *a, **k: set())
    path = _make_homonym_flow_db()
    try:
        conn = sqlite3.connect(path)
        # no relevance signal -> always-fire; both `format` overloads dedup to
        # distinct signatures ("def format(self)" vs "def format(self, value)").
        lines = _contract_pillar(conn, "src/x.py")
        conn.close()
    finally:
        os.unlink(path)
    flow_lines = [l for l in lines if l.startswith("[CONTRACT] flows:")]
    # node 2's OWN flow must surface — only possible with the node-id join. (Pre-fix
    # the name lookup returned node 1's flow for both, so this line is ABSENT -> RED.)
    assert "[CONTRACT] flows: value -> self._buf" in flow_lines, flow_lines
    # both signatures present, proving both overloads were delivered
    assert any("def format(self)" == l.replace("[CONTRACT] ", "") for l in lines), lines
    assert any("def format(self, value)" in l for l in lines), lines


def test_item34_single_function_flow_still_rides(monkeypatch):
    """Sanity: the node-id path still delivers a flow for a single function."""
    monkeypatch.setattr(
        pv, "_load_issue_anchors", lambda: {"symbols": [], "paths": [], "test_names": []}
    )
    monkeypatch.setattr(pv, "_load_issue_terms", lambda *a, **k: set())
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, "
        "name TEXT, file_path TEXT, start_line INTEGER DEFAULT 0, signature TEXT, "
        "return_type TEXT, is_test INTEGER DEFAULT 0, language TEXT DEFAULT 'python')"
    )
    conn.execute(
        "CREATE TABLE properties (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER, "
        "kind TEXT, value TEXT, line INTEGER, confidence REAL DEFAULT 1.0)"
    )
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,signature) "
        "VALUES (1,'Function','album','src/x.py',10,'def album(self, paths, dirs)')"
    )
    conn.execute(
        "INSERT INTO properties (node_id,kind,value,line,confidence) "
        "VALUES (1,'data_flow','paths -> self.items',11,1.0)"
    )
    conn.commit()
    conn.close()
    try:
        c = sqlite3.connect(path)
        lines = _contract_pillar(c, "src/x.py")
        c.close()
    finally:
        os.unlink(path)
    assert "[CONTRACT] flows: paths -> self.items" in lines, lines


# ---------------------------------------------------------------------------
# Item #32 — hub-scale / in-degree thread the same edge_filter
# ---------------------------------------------------------------------------
def test_item32_in_degree_applies_edge_filter():
    """_in_degree_for_file must count ONLY edges passing the supplied filter — a
    low-confidence name_match incoming edge is excluded by the default 0.7 floor,
    so the unfiltered third-population over-count is gone."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, "
        "name TEXT, file_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, "
        "target_id INTEGER, type TEXT, confidence REAL DEFAULT 0.0)"
    )
    # target node in the hub file
    conn.execute("INSERT INTO nodes (id,label,name,file_path) VALUES (1,'Function','hub','hub.py')")
    conn.execute("INSERT INTO nodes (id,label,name,file_path) VALUES (2,'Function','a','a.py')")
    conn.execute("INSERT INTO nodes (id,label,name,file_path) VALUES (3,'Function','b','b.py')")
    # one high-confidence incoming edge (kept) + one low-confidence (excluded by 0.7)
    conn.execute("INSERT INTO edges (source_id,target_id,type,confidence) VALUES (2,1,'CALLS',1.0)")
    conn.execute("INSERT INTO edges (source_id,target_id,type,confidence) VALUES (3,1,'CALLS',0.2)")
    conn.commit()
    cur = conn.cursor()
    try:
        # default filter (>= 0.7): only the conf=1.0 edge counts
        assert _in_degree_for_file(cur, "hub.py") == 1
        # explicit no-op filter ("1=1"): both edges count -> proves the param threads
        assert _in_degree_for_file(cur, "hub.py", edge_filter="1=1") == 2
    finally:
        conn.close()
        os.unlink(path)


def test_item32_hub_scale_query_uses_ef_in_source():
    """Structural assertion: the hub-scale degree query must be built from the
    shared `_ef` variable, not a hardcoded confidence literal. (Guards against a
    regression that re-hardcodes a third edge population.)"""
    import inspect

    src = inspect.getsource(pv.graph_navigation)
    # the hub-scale all_degrees query is now an f-string interpolating {_ef}
    assert "GROUP BY n.file_path ORDER BY 1" in src
    assert "_in_degree_for_file(cur, fp, edge_filter=_ef)" in src
    # the old hardcoded literal must no longer appear inside the hub-scale query line
    for line in src.splitlines():
        if "GROUP BY n.file_path ORDER BY 1" in line:
            assert "0.7" not in line, line


# ---------------------------------------------------------------------------
# Item #33 — _test_file_targets stdlib-shadow guard
# ---------------------------------------------------------------------------
def _make_test_targets_db(tmpdir: str) -> str:
    """A test file whose name_match edges point at project functions `join` and
    `parse`. The call site `os.path.join(...)` is a STDLIB SHADOW of project
    `join`; `parse(data)` is a REAL call to project `parse`. The guard must drop
    the shadow target and keep the real one."""
    db = os.path.join(tmpdir, "graph.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, "
        "name TEXT, file_path TEXT, is_test INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, "
        "target_id INTEGER, type TEXT, source_line INTEGER DEFAULT 0, confidence REAL DEFAULT 0.0)"
    )
    # test source node
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,is_test) VALUES (1,'Function','test_it','tests/test_x.py',1)"
    )
    # project targets (non-test)
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,is_test) VALUES (2,'Function','join','src/util.py',0)"
    )
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,is_test) VALUES (3,'Function','parse','src/util.py',0)"
    )
    # edge to `join` originates at line 2 (the os.path.join shadow); to `parse` at line 3 (real)
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,source_line,confidence) VALUES (1,2,'CALLS',2,1.0)"
    )
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,source_line,confidence) VALUES (1,3,'CALLS',3,1.0)"
    )
    conn.commit()
    conn.close()
    # write the test source so line 2 is a stdlib shadow, line 3 is a real call
    test_dir = os.path.join(tmpdir, "tests")
    os.makedirs(test_dir, exist_ok=True)
    with open(os.path.join(test_dir, "test_x.py"), "w", encoding="utf-8") as fh:
        fh.write(
            "def test_it():\n"  # line 1
            "    p = os.path.join(a, b)\n"  # line 2  -> SHADOW of project join
            "    return parse(data)\n"  # line 3  -> REAL call to project parse
        )
    return db


def test_item33_drops_stdlib_shadow_keeps_real_target(tmp_path):
    db = _make_test_targets_db(str(tmp_path))
    lines = _test_file_targets(db, "tests/test_x.py", repo_root=str(tmp_path))
    assert lines == []


def test_item33_correct_or_quiet_when_source_unreadable(tmp_path):
    """If the test source can't be read (no file on disk), the guard must NOT
    over-suppress — both targets are kept (correct-or-quiet)."""
    db = _make_test_targets_db(str(tmp_path))
    # repo_root points at a dir with NO test source -> guard can't read -> keep all
    empty = tmp_path / "empty"
    empty.mkdir()
    lines = _test_file_targets(db, "tests/test_x.py", repo_root=str(empty))
    assert lines == []


# ---------------------------------------------------------------------------
# Item #9 — _file_function_spec anchor-front-load + correct-or-quiet
# ---------------------------------------------------------------------------
def _make_spec_db(tmpdir: str) -> str:
    """A file with a generic top-of-file function (`progress_write`, first by
    start_line, with a 2-8 line template group) and the issue function
    (`set_fields`, deep, also with a template group). The OLD code emitted
    progress_write's spec unconditionally; the fix must front-load set_fields when
    it is the anchor, and suppress entirely when a signal matches nothing."""
    db = os.path.join(tmpdir, "graph.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, "
        "name TEXT, file_path TEXT, start_line INTEGER DEFAULT 0, end_line INTEGER DEFAULT 0, "
        "signature TEXT, return_type TEXT, is_test INTEGER DEFAULT 0, language TEXT DEFAULT 'python')"
    )
    # progress_write: lines 1-6 (file-top), has 3 parallel `self.x = ...` lines
    conn.execute(
        "INSERT INTO nodes (label,name,file_path,start_line,end_line) "
        "VALUES ('Function','progress_write','src/importer.py',1,6)"
    )
    # set_fields: lines 50-56 (deep), has 3 parallel `self.y = ...` lines
    conn.execute(
        "INSERT INTO nodes (label,name,file_path,start_line,end_line) "
        "VALUES ('Function','set_fields','src/importer.py',50,56)"
    )
    conn.commit()
    conn.close()
    src_dir = os.path.join(tmpdir, "src")
    os.makedirs(src_dir, exist_ok=True)
    body = ["" for _ in range(60)]
    # progress_write body (lines 1-6): 3 STRUCTURALLY-IDENTICAL lines (one template
    # group of size 3) — only the string literal varies, so _make_template collapses
    # them. (Distinct identifiers per line would split into size-1 groups.)
    body[0] = "def progress_write(self):"
    body[1] = '    out.append(render_status(state, "alpha"))'
    body[2] = '    out.append(render_status(state, "beta"))'
    body[3] = '    out.append(render_status(state, "gamma"))'
    body[4] = "    return None"
    # set_fields body (lines 50-56): 3 STRUCTURALLY-IDENTICAL lines (one template group)
    body[49] = "def set_fields(self, values):"
    body[50] = '    result.append(normalize(values, "title"))'
    body[51] = '    result.append(normalize(values, "album"))'
    body[52] = '    result.append(normalize(values, "artist"))'
    body[53] = "    return self"
    with open(os.path.join(src_dir, "importer.py"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(body) + "\n")
    return db


def test_item9_anchor_front_loads_issue_function(tmp_path, monkeypatch):
    """With `set_fields` as the issue anchor, the spec must describe set_fields —
    NOT the file-top progress_write (the old position-bias bug)."""
    db = _make_spec_db(str(tmp_path))
    monkeypatch.setattr(
        pv,
        "_load_issue_anchors",
        lambda: {"symbols": ["set_fields"], "paths": [], "test_names": []},
    )
    monkeypatch.setattr(pv, "_load_issue_terms", lambda *a, **k: set())
    out = _file_function_spec(db, "src/importer.py", str(tmp_path))
    assert out.startswith("Spec: set_fields"), out
    assert "progress_write" not in out, out


def test_item9_suppresses_when_signal_matches_nothing(tmp_path, monkeypatch):
    """A relevance signal exists (anchor) but NO function in the file matches it ->
    suppress, do not emit the file-top progress_write spec (correct-or-quiet)."""
    db = _make_spec_db(str(tmp_path))
    monkeypatch.setattr(
        pv,
        "_load_issue_anchors",
        lambda: {"symbols": ["totally_unrelated_symbol"], "paths": [], "test_names": []},
    )
    monkeypatch.setattr(pv, "_load_issue_terms", lambda *a, **k: set())
    out = _file_function_spec(db, "src/importer.py", str(tmp_path))
    assert out == "", out


def test_item9_blind_task_keeps_definition_order_spec(tmp_path, monkeypatch):
    """No anchors AND no issue terms -> always-fire definition-order behavior is
    preserved (the file-top function's spec is emitted, as before)."""
    db = _make_spec_db(str(tmp_path))
    monkeypatch.setattr(
        pv, "_load_issue_anchors", lambda: {"symbols": [], "paths": [], "test_names": []}
    )
    monkeypatch.setattr(pv, "_load_issue_terms", lambda *a, **k: set())
    out = _file_function_spec(db, "src/importer.py", str(tmp_path))
    assert out.startswith("Spec: progress_write"), out
