from __future__ import annotations

import sqlite3

import pytest

import gt_mini_patch as g


def _graph(tmp_path):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,
          qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,
          signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,
          language TEXT, parent_id INTEGER);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
          type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,
          confidence REAL, metadata TEXT);
        CREATE VIRTUAL TABLE symbol_content_fts USING fts5(content,
          tokenize="unicode61 tokenchars '_'");
        """
    )
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language) "
        "VALUES(1,'Function','parse_widget','app/widget.py',5,9,0,'python')"
    )
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language) "
        "VALUES(2,'Function','get_user_id','app/models.py',10,20,0,'python')"
    )
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language) "
        "VALUES(3,'Function','connect_tls','app/net.py',30,50,0,'python')"
    )
    con.execute(
        "INSERT INTO symbol_content_fts(rowid,content) "
        "VALUES(3,'connect_tls tls handshake socket')"
    )
    con.commit()
    con.close()
    return db


@pytest.fixture
def post_search(tmp_path, monkeypatch):
    db = _graph(tmp_path)
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_action_count", 1)
    g._search_seen.clear()
    yield
    g._search_seen.clear()


@pytest.mark.parametrize(
    ("cmd", "out", "producer", "evidence_type"),
    [
        ("grep -rn parse_widget .", "app/widget.py:5:def parse_widget():\n",
         "post_search", "def_partition"),
        ("grep -rn getUserId .", "", "name_fold", "name_fold"),
        ("grep -rn parse_widget .", "tests/test_widget.py:4:parse_widget()\n",
         "wrong_surface", "wrong_surface"),
        ("grep -rn handshake .", "", "body_concept", "body_concept"),
    ],
)
def test_each_lattice_branch_owns_exact_registered_identity(
    post_search, cmd, out, producer, evidence_type,
):
    decision = g._search_localize_decision(cmd, out)

    assert decision.text
    assert decision.runtime_producer_id == producer
    assert decision.evidence_type == evidence_type
    assert decision.actual_event == "search_result"
    lineage = decision.lineage()
    assert lineage is not None
    assert lineage.producer_registration_match is True
    assert lineage.fact_class == "def_partition"


def test_honest_negative_is_explicitly_unlineaged(post_search):
    cmd = "grep -rn absent_widget ."
    assert g._search_localize_decision(cmd, "").text == ""
    g._action_count = 2
    decision = g._search_localize_decision(cmd, "")

    assert decision.text
    assert decision.runtime_producer_id == ""
    assert decision.evidence_type == ""
    assert decision.actual_event == ""
    assert decision.lineage() is None


def test_public_string_api_and_model_bytes_are_unchanged(post_search):
    cmd = "grep -rn getUserId ."
    decision = g._search_localize_decision(cmd, "")
    expected = decision.text.encode("utf-8")
    g._search_seen.clear()

    assert g._search_localize_block(cmd, "").encode("utf-8") == expected


def test_branch_lineage_survives_lane_candidate_and_final_delivery(post_search, monkeypatch):
    decision = g._search_localize_decision(
        "grep -rn getUserId .", "")
    captured = []
    monkeypatch.setattr(g, "_ss_any_content_gate_on", lambda: False)
    monkeypatch.setattr(
        g, "_append_batch_candidate",
        lambda pool, candidate, thunk, *args, **kwargs:
            pool.append((candidate, thunk)),
    )
    pool = []
    g._global_pool_add_lane_a(
        pool, {"output": ""}, "grep -rn getUserId .",
        [("post_search.localize", decision.text, "app/models.py", decision)],
        krel="", event=None, kkind="post_search",
    )

    candidate = pool[0][0]
    assert candidate.lineage == decision.lineage()
    assert candidate.lineage.runtime_producer_id == "name_fold"

    monkeypatch.setattr(
        g, "_runtime_ledger_record",
        lambda **fields: captured.append(fields) or True,
    )
    monkeypatch.setattr(g, "_record_hook_fire", lambda *_args: None)
    monkeypatch.setattr(g, "_payload_leaks_test_identity", lambda _text: False)
    monkeypatch.setattr(g, "_ss_screen_delivery", lambda *_args, **_kwargs: (False, ""))
    monkeypatch.setattr(g, "_oracle_content_hash", lambda _text: "state-hash")
    monkeypatch.setattr(g, "_oracle_delivered_hashes", set())
    monkeypatch.setattr(g, "_ss_shadow_withheld", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(g, "_ledger_note_delivery", lambda *_args: None)
    monkeypatch.setattr(g, "_seal_lane_delivery", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(g, "_record_terminal_lane_controls", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(g, "_ss_record_delivered", lambda *_args, **_kwargs: None)
    out = {"output": ""}
    g._lane_a_deliver(
        out, "grep -rn getUserId .",
        [("post_search.localize", decision.text, "app/models.py", decision)],
        krel="", event=None,
    )

    delivered = next(row for row in captured if row["outcome"] == "delivered")
    assert delivered["extra"]["runtime_producer_id"] == "name_fold"
    assert delivered["extra"]["evidence_type"] == "name_fold"
    assert delivered["extra"]["actual_event"] == "search_result"


def test_final_sealed_envelope_keeps_branch_producer_identity(post_search, monkeypatch):
    decision = g._search_localize_decision("grep -rn getUserId .", "")
    lineage = decision.lineage()
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setattr(g, "_persist_receipt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        g, "_persist_lane_producer_attestation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(g, "_gt_gateway_chain_head", "")
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [])
    g._EPISODE.delivered_dedup.clear()
    extra = g._lane_delivery_extra(
        "post_search.localize", decision.text, "app/models.py",
        "post_search", lineage=lineage,
    )

    g._seal_lane_delivery(
        "post_search.localize", decision.text, "app/models.py",
        delivery_extra=extra, lineage=lineage,
    )

    sealed = g._gt_gateway_deliveries[-1]
    assert sealed.producer == "name_fold"
    assert sealed.evidence_type == "name_fold"
    assert sealed.lineage == lineage
    assert sealed.dedup_key == extra["candidate_id"]


def test_native_control_follows_spliced_branch_bytes(post_search, monkeypatch):
    monkeypatch.setenv("GT_POST_SEARCH_NATIVE", "1")
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    g._terminal_lane_controls.clear()
    decision = g._search_localize_decision("grep -rn getUserId .", "")

    old_keys = [key for key in g._terminal_lane_controls if key[1] == "post_search.localize"]
    assert old_keys == [g._terminal_lane_control_key(
        "post_search.localize", decision.text)]
