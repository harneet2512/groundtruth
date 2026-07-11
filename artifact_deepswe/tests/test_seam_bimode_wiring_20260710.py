"""TTD proof for the five seam findings B-14 / B-19 / B-20 / B-3 / B-4
(gt_mini_patch.py, 2026-07-10).

Each finding gets a RED->GREEN pin + a documented biting mutation (the mutation
is applied to the SOURCE by the session and the matching test is re-run to show it
turns RED; here every assertion is GREEN with the fix in place). All new behaviour is
flag-gated and byte-identical when the flag is off — the *_off tests pin that.

Hermetic: synthetic graph.db, no ONNX / no repo checkout / no network. Every env flip
is via monkeypatch (never os.environ at module scope — a leaked GT_* flag manufactures
false regressions in later seam-test files).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import gt_mini_patch as g
from groundtruth.runtime import gateway as gw
from groundtruth.runtime.adapters import miniswe as ad
from groundtruth.runtime.adapters.miniswe import seal_delivery
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope, from_dict


# --------------------------------------------------------------------------- #
# graph builder (nodes + edges + properties)
# --------------------------------------------------------------------------- #
def _mk_graph(tmp_path, nodes, edges=(), props=()):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);"
        "CREATE TABLE properties(id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT,"
        " value TEXT, line INTEGER, confidence REAL);")
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,"
            "is_test,language) VALUES(?,?,?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("end_line", 5), n.get("signature", ""),
             n.get("is_test", 0), n.get("language", "python")))
    for e in edges:
        con.execute(
            "INSERT INTO edges(source_id,target_id,type,resolution_method,confidence)"
            " VALUES(?,?,?,?,?)",
            (e["source_id"], e["target_id"], e.get("type", "CALLS"),
             e.get("resolution_method", "import"), e.get("confidence", 1.0)))
    for p in props:
        con.execute(
            "INSERT INTO properties(node_id,kind,value,line,confidence)"
            " VALUES(?,?,?,?,?)",
            (p["node_id"], p["kind"], p["value"], p.get("line", 1),
             p.get("confidence", 0.8)))
    con.commit()
    con.close()
    return db


@pytest.fixture
def contract_graph(tmp_path, monkeypatch):
    """`mod.py::get_user(user_id)` with ONE deterministic non-test caller
    `app.py::handler` that boolean-checks the result (caller_usage property)."""
    db = _mk_graph(
        tmp_path,
        nodes=[
            {"id": 1, "name": "get_user", "file_path": "mod.py",
             "signature": "get_user(user_id)"},
            {"id": 2, "name": "handler", "file_path": "app.py",
             "signature": "handler()"},
        ],
        edges=[{"source_id": 2, "target_id": 1, "resolution_method": "import",
                "confidence": 1.0}],
        props=[{"node_id": 2, "kind": "caller_usage",
                "value": "boolean_check:get_user|if get_user(x):"}],
    )
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._reset_oracle_state()
    yield db
    g._reset_oracle_state()


def _contract(rel="mod.py", **kw):
    g._contract_seen.discard(rel)  # the latch is set only in _lane_a_deliver; keep re-armed
    return g._graph_contract_block(rel, **kw)


# =========================================================================== #
# B-19 — mode-conditioned 'preserve'
# =========================================================================== #
def test_b19_off_is_current_preserve(contract_graph, monkeypatch):
    """Flag OFF: byte-identical to the pre-B-19 contract — 'preserve this interface'
    on the caller count regardless of the edit action."""
    monkeypatch.delenv("GT_CONTRACT_MODE", raising=False)
    add = {"command": "create", "path": "mod.py", "file_text": "x = 1"}
    out = _contract(action=add, cmd="")
    assert "preserve this interface" in out
    assert "signature changing" not in out


def test_b19_modify_in_place_preserves(contract_graph, monkeypatch):
    monkeypatch.setenv("GT_CONTRACT_MODE", "1")
    body = {"command": "str_replace", "path": "mod.py",
            "old_str": "x = 1", "new_str": "x = 2"}
    out = _contract(action=body, cmd="")
    assert "preserve this interface" in out  # unchanged interface -> keep the preserve


def test_b19_add_new_file_suppresses_preserve(contract_graph, monkeypatch):
    monkeypatch.setenv("GT_CONTRACT_MODE", "1")
    add = {"command": "create", "path": "mod.py", "file_text": "y = 3\n"}
    out = _contract(action=add, cmd="")
    assert "[CALLERS] get_user:" in out
    assert "preserve this interface" not in out  # ADD -> no interface to preserve


def test_b19_signature_change_emits_delta_not_preserve(contract_graph, monkeypatch):
    monkeypatch.setenv("GT_CONTRACT_MODE", "1")
    sig = {"command": "str_replace", "path": "mod.py",
           "old_str": "def get_user(user_id):",
           "new_str": "def get_user(user_id, tenant):"}
    out = _contract(action=sig, cmd="")
    assert "signature changing (user_id -> user_id, tenant)" in out
    assert "preserve this interface" not in out
    assert "update the call sites" in out


def test_b19_annotation_only_is_not_a_change(contract_graph, monkeypatch):
    """A param annotation/whitespace-only edit is NOT a signature change (fail-safe
    to PRESERVE) — the comparison is on parameter identifiers, not raw text."""
    monkeypatch.setenv("GT_CONTRACT_MODE", "1")
    sig = {"command": "str_replace", "path": "mod.py",
           "old_str": "def get_user(user_id):",
           "new_str": "def get_user(user_id: int):"}
    out = _contract(action=sig, cmd="")
    assert "signature changing" not in out
    assert "preserve this interface" in out


# =========================================================================== #
# B-20 — bilateral caller-consumption
# =========================================================================== #
def test_b20_off_no_consumed_line(contract_graph, monkeypatch):
    monkeypatch.delenv("GT_CONTRACT_BILATERAL", raising=False)
    out = _contract()
    assert "[CONSUMED]" not in out


def test_b20_on_composes_consumption_contract(contract_graph, monkeypatch):
    monkeypatch.setenv("GT_CONTRACT_BILATERAL", "1")
    out = _contract()
    assert "[CONSUMED] callers of get_user():" in out
    assert "None/boolean-checked x1" in out
    assert "preserve these consumption contracts" in out


def test_b20_ignores_usage_of_a_different_callee(tmp_path, monkeypatch):
    """The caller_usage callee must MATCH the target — a caller's usage of some OTHER
    symbol never counts toward this symbol's bilateral contract."""
    db = _mk_graph(
        tmp_path,
        nodes=[
            {"id": 1, "name": "get_user", "file_path": "mod.py",
             "signature": "get_user(user_id)"},
            {"id": 2, "name": "handler", "file_path": "app.py", "signature": "handler()"},
        ],
        edges=[{"source_id": 2, "target_id": 1}],
        props=[{"node_id": 2, "kind": "caller_usage",
                "value": "iterated:something_else|for r in something_else():"}],
    )
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setenv("GT_CONTRACT_BILATERAL", "1")
    g._reset_oracle_state()
    out = _contract()
    assert "[CONSUMED]" not in out  # no usage OF get_user -> nothing to compose


# =========================================================================== #
# B-3 — dark bridges (changed_files + edit_before_after)
# =========================================================================== #
def test_b3_edit_bridges_reconstructs_before(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")
    (tmp_path / "mod.py").write_text("def get_user(a, b):\n    return a\n", encoding="utf-8")
    action = {"command": "str_replace", "path": "mod.py",
              "old_str": "def get_user(a):", "new_str": "def get_user(a, b):"}
    changed, eba = g._gateway_edit_bridges(action, "")
    assert changed == ("mod.py",)
    assert eba and "mod.py" in eba
    before, after = eba["mod.py"]
    assert after == "def get_user(a, b):\n    return a\n"
    assert before == "def get_user(a):\n    return a\n"  # reverse-applied str_replace


def test_b3_edit_bridges_off_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.delenv("GT_GATEWAY_EDIT_BRIDGES", raising=False)
    (tmp_path / "mod.py").write_text("def get_user(a, b): pass\n", encoding="utf-8")
    action = {"command": "str_replace", "path": "mod.py",
              "old_str": "def get_user(a):", "new_str": "def get_user(a, b):"}
    assert g._gateway_edit_bridges(action, "") == ((), None)


def test_b3_create_before_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")
    (tmp_path / "new.py").write_text("def f(): pass\n", encoding="utf-8")
    action = {"command": "create", "path": "new.py", "file_text": "def f(): pass\n"}
    changed, eba = g._gateway_edit_bridges(action, "")
    assert changed == ("new.py",)
    assert eba["new.py"][0] is None  # a create has no prior content to diff


def test_b3_seam_threads_bridges_into_normalize_event(tmp_path, monkeypatch):
    """The seam passes changed_files + edit_before_after INTO normalize_event when the
    bridge flag is on — the previously-4-arg call that left patch_delta unreachable."""
    (tmp_path / "mod.py").write_text("def get_user(a, b):\n    return a\n", encoding="utf-8")
    _mk_graph(tmp_path, nodes=[{"id": 1, "name": "get_user", "file_path": "mod.py",
                                "signature": "get_user(a, b)"}])
    db = str(tmp_path / "graph.db")
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")
    g._reset_oracle_state()

    captured: dict = {}
    real = ad.normalize_event

    def _cap(command, output, rc, ai, **kw):
        captured["called"] = True
        captured.update(kw)
        return real(command, output, rc, ai, **kw)

    monkeypatch.setattr(ad, "normalize_event", _cap)
    action = {"command": "str_replace", "path": "mod.py",
              "old_str": "def get_user(a):", "new_str": "def get_user(a, b):"}
    out = {"output": "edited mod.py", "returncode": 0}
    g._gt_gateway_deliver(action, out, g._effective_cmd(action), out["output"])

    assert captured.get("called")
    assert captured.get("changed_files") == ("mod.py",)
    eba = captured.get("edit_before_after")
    assert eba and eba.get("mod.py") and eba["mod.py"][0] == "def get_user(a):\n    return a\n"


def test_b3_seam_off_is_4arg_equivalent(tmp_path, monkeypatch):
    _mk_graph(tmp_path, nodes=[{"id": 1, "name": "run", "file_path": "mod.py"}])
    db = str(tmp_path / "graph.db")
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.delenv("GT_GATEWAY_EDIT_BRIDGES", raising=False)
    g._reset_oracle_state()

    captured: dict = {}
    real = ad.normalize_event

    def _cap(command, output, rc, ai, **kw):
        captured.update(kw)
        return real(command, output, rc, ai, **kw)

    monkeypatch.setattr(ad, "normalize_event", _cap)
    action = {"command": "str_replace", "path": "mod.py",
              "old_str": "a", "new_str": "b"}
    g._gt_gateway_deliver(action, {"output": "x", "returncode": 0},
                          g._effective_cmd(action), "x")
    # off -> the bridges are the empty defaults (identical to the pre-B-3 4-arg event)
    assert captured.get("changed_files") == ()
    assert captured.get("edit_before_after") is None


def test_b3_covering_normalize_passthrough_documented():
    """DOCUMENTED plumbing: covering FLOWS through normalize_event (the bridge exists).
    The mini seam runs covering PROACTIVELY at edit-time via the lane
    (_executed_covering_emission) and does not inject a fresh test-turn verdict, so this
    leg is deliberately not wired live (see the report) — but the 4->kwargs plumbing is
    intact and testable."""
    ev = ad.normalize_event(
        "go test ./...", "FAIL", 1, 3,
        covering=gw.CoveringResult(target="x.go", verdict="fail", body_lines=["boom"]))
    assert ev.covering is not None and ev.covering.target == "x.go"


# =========================================================================== #
# B-4 — real issue text into GatewayState
# =========================================================================== #
def test_b4_issue_text_from_issue_file(tmp_path, monkeypatch):
    issue = tmp_path / "issue.txt"
    issue.write_text("Redis TLS handshake fails on reconnect.\n", encoding="utf-8")
    monkeypatch.setenv("GT_ISSUE_FILE", str(issue))
    g._reset_oracle_state()  # clears the per-task cache
    assert "Redis TLS handshake" in g._issue_text()


def test_b4_issue_text_from_cert_dir(tmp_path, monkeypatch):
    (tmp_path / "issue.txt").write_text("Calendar recurrence off by one.\n", encoding="utf-8")
    monkeypatch.delenv("GT_ISSUE_FILE", raising=False)
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    g._reset_oracle_state()
    assert "Calendar recurrence" in g._issue_text()


def test_b4_issue_text_absent_is_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("GT_ISSUE_FILE", raising=False)
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))  # dir has NO issue.txt
    g._reset_oracle_state()
    assert g._issue_text() == ""  # correct-or-quiet: change_surface abstains


def test_b4_seam_threads_issue_text_into_gateway_state(tmp_path, monkeypatch):
    """The gateway builds GatewayState with the REAL issue text, not ''."""
    (tmp_path / "issue.txt").write_text("Add a --json flag to the export command.\n",
                                        encoding="utf-8")
    _mk_graph(tmp_path, nodes=[{"id": 1, "name": "run", "file_path": "mod.py"}])
    db = str(tmp_path / "graph.db")
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    g._reset_oracle_state()

    seen: dict = {}
    real = gw.GatewayState

    def _cap(*a, **kw):
        seen["issue_text"] = kw.get("issue_text")
        return real(*a, **kw)

    monkeypatch.setattr(gw, "GatewayState", _cap)
    g._gt_gateway_deliver({"command": "ls -la"}, {"output": "x", "returncode": 0},
                          "ls -la", "x")
    assert seen.get("issue_text") and "export command" in seen["issue_text"]


# =========================================================================== #
# B-14 — durable receipt persistence
# =========================================================================== #
def _sealed(target="a/x.py", ep="ep", ev="7"):
    env = EvidenceEnvelope.build(producer="lane", fact_id="", target=target,
                                 evidence_type="lane", payload=("hi",))
    sealed, _ = seal_delivery(env, episode_id=ep, event_id=ev, parent_hash="",
                              rendered_bytes=b"hi", renderer_id="lane")
    return sealed


def test_b14_persist_roundtrips_after_process_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    sealed = _sealed()
    g._persist_receipt(sealed, kind="lane", transition="delivered")

    path = tmp_path / "gt_receipts.jsonl"
    assert path.is_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])  # a FRESH read, as a post-run auditor would do
    assert rec["kind"] == "lane" and rec["transition"] == "delivered"
    rt = from_dict(rec["envelope"])
    assert rt.dedup_key == sealed.dedup_key
    assert rt.episode_id == "ep" and rt.event_id == "7"
    assert rt.receipt_state == sealed.receipt_state == "delivered"


def test_b14_no_cert_dir_graceful_no_op(tmp_path, monkeypatch):
    monkeypatch.delenv("GT_CERT_DIR", raising=False)
    assert g._receipts_sidecar_path() == ""
    g._persist_receipt(_sealed(), kind="lane", transition="delivered")  # no crash
    assert not (tmp_path / "gt_receipts.jsonl").exists()


def test_b14_seam_persists_gateway_delivery(tmp_path, monkeypatch):
    """A real gateway delivery writes a durable 'delivered' receipt line."""
    _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ])
    db = str(tmp_path / "graph.db")
    cert = tmp_path / "cert"
    cert.mkdir()
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_POST_SEARCH_ON", False)
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(cert))
    g._reset_oracle_state()

    out = {"output": "a/x.py:10: run\nb/y.py:20: run", "returncode": 0}
    g._gt_gateway_deliver({"command": "grep -rn run ."}, out, "grep -rn run .",
                          out["output"])
    assert g._gt_gateway_deliveries  # a delivery was sealed
    path = cert / "gt_receipts.jsonl"
    assert path.is_file()
    recs = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    delivered = [r for r in recs if r["kind"] == "gateway" and r["transition"] == "delivered"]
    assert delivered
    assert delivered[-1]["envelope"]["dedup_key"] == g._gt_gateway_deliveries[-1].dedup_key
