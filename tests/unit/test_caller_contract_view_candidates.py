import sqlite3


from groundtruth.runtime.gateway import (
    GatewayState,
    ToolEvent,
    produce_raw,
)


def _mk_graph(path):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT,
            is_exported INTEGER DEFAULT 0, is_test INTEGER DEFAULT 0,
            language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL, metadata TEXT
        );
        """
    )
    # definition: helper() in pkg/mod.py at line 2 (viewed file)
    con.execute(
        "INSERT INTO nodes VALUES (1,'Function','helper','pkg.mod.helper',"
        "'pkg/mod.py',2,4,'def helper()','',1,0,'python',NULL)"
    )
    # caller: use_it() in pkg/other.py
    con.execute(
        "INSERT INTO nodes VALUES (2,'Function','use_it','pkg.other.use_it',"
        "'pkg/other.py',1,5,'def use_it()','',1,0,'python',NULL)"
    )
    # candidate-tier edge: name_match at 0.6 — below the FACT gate
    con.execute("INSERT INTO edges VALUES (1,2,1,'CALLS',3,'pkg/other.py','name_match',0.6,NULL)")
    con.commit()
    con.close()


def test_candidate_tier_caller_view_delivers_warning_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        "def outer():\n    def helper():\n        return 1\n", encoding="utf-8"
    )
    (pkg / "other.py").write_text("def use_it():\n    x = 1\n    helper()\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    _mk_graph(str(db))
    state = GatewayState(repo_root=str(tmp_path), graph_db=str(db))
    event = ToolEvent(
        kind="view",
        command="cat pkg/mod.py",
        viewed_files=("pkg/mod.py",),
        semantic_events=("file_view",),
        semantics_authoritative=True,
    )
    candidates = produce_raw(event, state)
    views = [c for c in candidates if c.evidence_type == "caller_contract_view"]
    assert views, "candidate-tier callers should deliver a WARNING envelope"
    assert str(views[0].tier).endswith("WARNING")
    rows = views[0].native_args.get("caller_rows") or ()
    assert ("pkg/other.py", 3, "use_it") in rows


def test_empty_graph_view_still_abstains(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT,
            qualified_name TEXT, file_path TEXT, start_line INTEGER,
            end_line INTEGER, signature TEXT, return_type TEXT,
            is_exported INTEGER DEFAULT 0, is_test INTEGER DEFAULT 0,
            language TEXT, parent_id INTEGER);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER,
            target_id INTEGER, type TEXT, source_line INTEGER,
            source_file TEXT, resolution_method TEXT, confidence REAL,
            metadata TEXT);
        """
    )
    con.execute(
        "INSERT INTO nodes VALUES (1,'Function','helper','pkg.mod.helper',"
        "'pkg/mod.py',1,2,'def helper()','',1,0,'python',NULL)"
    )
    con.commit()
    con.close()
    state = GatewayState(repo_root=str(tmp_path), graph_db=str(db))
    event = ToolEvent(
        kind="view",
        command="cat pkg/mod.py",
        viewed_files=("pkg/mod.py",),
        semantic_events=("file_view",),
        semantics_authoritative=True,
    )
    assert produce_raw(event, state) == []
