import sqlite3

from groundtruth.resolve import (
    _apply_lsp_resolution,
    _get_ambiguous_edges,
    _gt_index_binary,
    _lsp_character_for_callsite,
)


def _graph() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE nodes (
          id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, start_line INTEGER,
          end_line INTEGER, language TEXT, stable_id TEXT, node_type TEXT,
          source_revision TEXT, caller_symbol_id TEXT, line_start INTEGER,
          column_start INTEGER, callee_lexeme TEXT, candidate_state TEXT,
          selected_target_id TEXT, candidate_count_v2 INTEGER, callsite_id TEXT,
          label TEXT, qualified_name TEXT, signature TEXT, producer_build_id TEXT,
          target_symbol_id TEXT, pass_kind TEXT, pass_version TEXT,
          step_ordinal INTEGER, operation TEXT, input_fact_ids TEXT,
          boundary_id TEXT
          ,schema_version INTEGER, byte_start INTEGER, byte_end INTEGER
        );
        CREATE TABLE edges (
          id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
          source_file TEXT, source_line INTEGER, resolution_method TEXT,
          confidence REAL, trust_tier TEXT, stable_id TEXT UNIQUE,
          schema_version INTEGER, callsite_stable_id TEXT, target_symbol_id TEXT,
          ordinal INTEGER, viability TEXT, candidate_count INTEGER,
          evidence_type TEXT, verification_status TEXT, selection_rule_id TEXT,
          analysis_boundary TEXT, producer_build_id TEXT,
          producer_source_fingerprint TEXT, metadata TEXT, derivation_fact_ids TEXT,
          exclusion_fact_ids TEXT, derivation_contract TEXT,
          derivation_kind TEXT, evidence_set TEXT, pass_kind TEXT,
          pass_version TEXT, pass_status TEXT, sibling_count INTEGER
        );
        CREATE TABLE resolution_symbols (stable_id TEXT PRIMARY KEY, native_id TEXT);

        INSERT INTO nodes
          (id,name,file_path,start_line,end_line,language,stable_id,node_type,
           source_revision,caller_symbol_id,line_start,column_start,callee_lexeme,
           candidate_state,selected_target_id,candidate_count_v2,callsite_id,
           byte_start,byte_end) VALUES
          (1,'caller','src/app.py',1,20,'python','sym-caller','symbol','rev',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
          (2,'dispatch','src/old.py',5,8,'python','sym-old','symbol','rev',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
          (3,'dispatch','src/real.py',10,14,'python','sym-real','symbol','rev',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
          (10,'dispatch','src/app.py',7,7,'python','callsite-one','callsite','rev','sym-caller',7,4,'dispatch','ambiguous',NULL,1,NULL,100,115),
          (11,'dispatch','src/app.py',7,7,'python','callsite-other','callsite','rev','sym-caller',7,18,'dispatch','ambiguous',NULL,1,NULL,118,133);
        INSERT INTO resolution_symbols VALUES ('sym-caller','1'),('sym-old','2'),('sym-real','3');
        INSERT INTO edges VALUES
          (100,1,2,'CALLS','src/app.py',7,'name_match',0.5,'SPECULATIVE','legacy',1,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
          (101,10,2,'CANDIDATE_TARGET','src/app.py',NULL,'unique_name_fallback',NULL,'CANDIDATE','candidate-one',2,'callsite-one','sym-old',0,'viable',1,'resolver_candidate','source_supported',NULL,'rev','build','source',NULL,'["producer-fact"]','[]','v2','unique_name_fallback','closed','unique_name_fallback','1','completed',1),
          (102,11,3,'CANDIDATE_TARGET','src/app.py',NULL,'unique_name_fallback',NULL,'CANDIDATE','candidate-other',2,'callsite-other','sym-real',0,'viable',1,'resolver_candidate','source_supported',NULL,'rev','build','source',NULL,'[]','[]','v2','unique_name_fallback','closed','unique_name_fallback','1','completed',1),
          (103,1,2,'CALLS','src/app.py',7,'import_binding',1.0,'CERTIFIED','resolved',1,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);
        """
    )
    return conn


def _attached_targets(conn: sqlite3.Connection, callsite_id: str) -> list[int]:
    return [
        row[0]
        for row in conn.execute(
            """SELECT ce.target_id FROM nodes c
               JOIN edges ce ON ce.source_id=c.id AND ce.type='CANDIDATE_TARGET'
               WHERE c.stable_id=? AND ce.viability='viable'
               ORDER BY ce.ordinal""",
            (callsite_id,),
        )
    ]


def test_lsp_correction_updates_exact_primary_callsite_and_consumer() -> None:
    conn = _graph()
    # Current defect witness: changing only legacy CALLS leaves the primary consumer old.
    conn.execute("UPDATE edges SET target_id=3 WHERE id=100")
    assert _attached_targets(conn, "callsite-one") == [2]
    conn.execute("UPDATE edges SET target_id=2 WHERE id=100")

    eligible = _get_ambiguous_edges(conn, limit=10)
    assert [item["id"] for item in eligible] == [100]
    edge = dict(eligible[0])
    assert (edge["callsite_stable_id"], edge["callsite_column"]) == ("callsite-one", 4)
    edge["lsp_source_character"] = 13
    stats = {"verified": 0, "corrected": 0, "skipped": 0}
    outcome = _apply_lsp_resolution(
        conn,
        edge=edge,
        target_rel="src/real.py",
        target_line=11,
        target_name="dispatch",
        stats=stats,
        has_trust_tier=True,
        target_column=6,
        server_identity="pyright-langserver --stdio",
    )

    assert outcome == "corrected"
    assert conn.execute("SELECT target_id FROM edges WHERE id=100").fetchone()[0] == 3
    assert _attached_targets(conn, "callsite-one") == [3]
    assert _attached_targets(conn, "callsite-other") == [3]
    state = conn.execute(
        "SELECT candidate_state,selected_target_id,candidate_count_v2 FROM nodes WHERE id=10"
    ).fetchone()
    assert tuple(state) == ("selected", "sym-real", 1)
    selected = conn.execute(
        "SELECT target_id,resolution_method,callsite_stable_id,metadata FROM edges "
        "WHERE type='SELECTED_TARGET' AND source_id=10"
    ).fetchone()
    assert tuple(selected[:3]) == (3, "lsp", "callsite-one")
    assert '"source_character":13' in selected[3]
    assert '"definition_file":"src/real.py"' in selected[3]
    original = conn.execute(
        "SELECT viability,derivation_fact_ids,exclusion_fact_ids FROM edges WHERE id=101"
    ).fetchone()
    assert original[0] == "excluded"
    assert original[1] == '["producer-fact"]'
    assert "producer-fact" not in original[2]
    facts = conn.execute(
        "SELECT operation,signature FROM nodes WHERE node_type='derivation_fact' "
        "AND pass_kind='lsp_definition' ORDER BY operation"
    ).fetchall()
    assert [row[0] for row in facts] == ["exclude", "include"]
    assert all('"definition_character":6' in row[1] for row in facts)
    assert all('"server":"pyright-langserver --stdio"' in row[1] for row in facts)
    viable = conn.execute(
        "SELECT count(*) FROM edges WHERE type='CANDIDATE_TARGET' "
        "AND callsite_stable_id='callsite-one' AND viability='viable'"
    ).fetchone()[0]
    assert viable == 1


def test_primary_scope_abstains_when_legacy_row_has_no_exact_callsite() -> None:
    conn = _graph()
    # Make the second exact callsite a candidate for the same legacy endpoint.
    conn.execute("UPDATE edges SET target_id=2,target_symbol_id='sym-old' WHERE id=102")
    assert _get_ambiguous_edges(conn, limit=10) == []


def test_multiple_lsp_definitions_do_not_promote_first_result() -> None:
    conn = _graph()
    edge = dict(_get_ambiguous_edges(conn, limit=10)[0])
    stats = {"verified": 0, "corrected": 0, "skipped": 0}
    outcome = _apply_lsp_resolution(
        conn,
        edge=edge,
        target_rel="src/real.py",
        target_line=11,
        target_name="dispatch",
        stats=stats,
        has_trust_tier=True,
        definition_count=2,
    )
    assert outcome == "skipped"
    assert conn.execute("SELECT target_id FROM edges WHERE id=100").fetchone()[0] == 2
    assert _attached_targets(conn, "callsite-one") == [2]


def test_call_expression_byte_column_becomes_utf16_callee_character() -> None:
    line = '"🚀"; receiver.dispatch()'
    byte_column = len('"🚀"; '.encode("utf-8"))
    character, found = _lsp_character_for_callsite(
        line, "dispatch", byte_column, len("receiver.dispatch()".encode("utf-8"))
    )
    assert found
    assert character == len('"🚀"; receiver.'.encode("utf-16-le")) // 2
    assert _lsp_character_for_callsite("other(); dispatch()", "dispatch", 0, 7) == (
        -1,
        False,
    )


def test_closure_builder_uses_installer_binary_contract(monkeypatch) -> None:
    monkeypatch.setenv("GT_INDEX_BINARY", "/installed-agent/gt-index")
    monkeypatch.setenv("GT_INDEX_BIN", "/legacy/gt-index")
    assert _gt_index_binary() == "/installed-agent/gt-index"
