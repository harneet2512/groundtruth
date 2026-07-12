r"""SM-2b (2026-07-11) — the CROSS-LANGUAGE gateway ``caller_contract`` producer.

The SM-2 LIPI bounce proved the gateway lacked a general caller-contract producer, so the
Wave-1 retirement of l3.contract/l3b.evidence was a DELETION (§26.4 forbids retiring a class
before its equivalence is proven). This wave BUILDS that producer — graph-based (tree-sitter
CALLS edges), so it delivers the caller-break on a Go/Rust/TS/JS edit that
``patch_delta.analyze_patch_delta`` (ast-only, ``_SCAN_EXTS={.py,.pyi}``) CANNOT.

Pins (each with a proven-biting mutation, recorded inline):
  (1) registry wiring — ``caller_break`` aliases to ``caller_contract`` and delivers at the
      ``edit_result`` boundary (override) so GT_REGISTRY_ENFORCE keeps it on-time;
  (2) the producer delivers on a PYTHON signature-changing edit with a cross-file caller;
  (3) the EQUIVALENCE proof — the SAME on a GO edit, the exact case patch_delta cannot (M3);
  (4) correct-or-quiet — no sig change / no cross-file caller / no bridges -> [];
  (5) leak law — a caller in a TEST file never surfaces (count + provenance);
  (6) native render — render_envelope dispatches caller_break to the SM-1 contract diagnostic.

Windows: run with ``PYTHONIOENCODING=utf-8`` (the payload carries an em-dash).
"""
from __future__ import annotations

import sqlite3

import pytest

from groundtruth.runtime import fact_registry as fr
from groundtruth.runtime import gateway as gw
from groundtruth.runtime.adapters import miniswe as ad


# --------------------------------------------------------------------------- #
# fixtures — a synthetic graph.db + a ToolEvent factory
# --------------------------------------------------------------------------- #
def _mk_graph(tmp_path, nodes, edges, name="graph.db"):
    db = str(tmp_path / name)
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);")
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("end_line", 5), n.get("is_test", 0),
             n.get("language", "python")))
    for e in edges:
        con.execute(
            "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
            "confidence) VALUES(?,?,?,'CALLS',?,?,?)",
            (e["id"], e["source_id"], e["target_id"], e.get("source_line", 3),
             e["resolution_method"], e["confidence"]))
    con.commit()
    con.close()
    return db


def _edit_ev(rel, before, after, *, action_index=1):
    return gw.ToolEvent(kind="edit", command=f"str_replace {rel}", output="",
                        changed_files=(rel,), action_index=action_index,
                        edit_before_after={rel: (before, after)})


# a caller graph: `foo`/`Handle` defined in the edited file + one deterministic (import,
# conf=1.0) CROSS-FILE, non-test caller -> the producer's FACT-tier caller gate is met.
def _py_caller_graph(tmp_path):
    return _mk_graph(tmp_path, [
        {"id": 1, "name": "get_user", "file_path": "svc/users.py"},
        {"id": 2, "name": "caller", "file_path": "app/main.py"},
    ], [{"id": 1, "source_id": 2, "target_id": 1, "resolution_method": "import",
         "confidence": 1.0}])


def _go_caller_graph(tmp_path):
    return _mk_graph(tmp_path, [
        {"id": 1, "name": "Handle", "file_path": "handler.go", "language": "go"},
        {"id": 2, "name": "main", "file_path": "main.go", "language": "go"},
    ], [{"id": 1, "source_id": 2, "target_id": 1, "resolution_method": "import",
         "confidence": 1.0}])


# =========================================================================== #
# (1) registry wiring — caller_break -> caller_contract @ edit_result
# =========================================================================== #
def test_caller_break_registers_as_caller_contract_at_edit_boundary():
    assert fr.registration_for("caller_break").fact_class == "caller_contract"
    assert fr.renderable("caller_break") is True
    assert fr.required_renderer("caller_break") == "contract"
    # the load-bearing TIMING override: aliased to caller_contract (file_view, PRE-EDIT) but
    # delivered at edit_result so GT_REGISTRY_ENFORCE does not EXPIRE the edit-boundary break.
    assert fr.required_event("caller_break") == "edit_result"
    assert fr.is_reactive("caller_break") is False


def test_registry_enforce_keeps_caller_break_on_time(tmp_path, monkeypatch):
    """Under GT_REGISTRY_ENFORCE the caller_break must DELIVER at the edit event (its declared
    boundary), NOT expire. MUTATION (recorded): drop the ``caller_break -> edit_result``
    override in fact_registry._EVIDENCE_TYPE_DELIVER_BY -> the class falls back to
    caller_contract's file_view boundary -> at the edit event route_delivery == EXPIRED_LATE
    -> `assert route == ROUTE_DELIVER` BITES (verified below via a boundary-mismatch probe)."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    db = _py_caller_graph(tmp_path)
    ev = _edit_ev("svc/users.py", "def get_user(a):\n    return a\n",
                  "def get_user(a, b):\n    return a\n")
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    envs = gw._produce_caller_contract(ev, st)
    assert envs, "producer must fire"
    assert gw.route_delivery(envs[0], ev, st) == gw.ROUTE_DELIVER
    # the boundary-mismatch probe that the missing override would trip: a file_view-boundary
    # class delivered on the edit event EXPIRES (this is exactly what dropping the override does).
    file_view_env = gw._mk_add(st, ev, fact_kind="caller_contract", target="svc/users.py",
                               body_lines=["x"], evidence=[], tier=gw.WARNING,
                               producer="contract_map", symbol="get_user")
    assert gw.route_delivery(file_view_env, ev, st) == gw.ROUTE_EXPIRED_LATE


# =========================================================================== #
# (2) PYTHON delivery
# =========================================================================== #
def test_python_signature_change_delivers_caller_break(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    db = _py_caller_graph(tmp_path)
    ev = _edit_ev("svc/users.py", "def get_user(a):\n    return a\n",
                  "def get_user(a, b):\n    return a + b\n")
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    envs = gw._produce_caller_contract(ev, st)
    assert len(envs) == 1
    e = envs[0]
    assert e.evidence_type == "caller_break" and e.fact_id == "get_user"
    assert e.target == "svc/users.py"
    assert e.provenance == (("app/main.py", 3),)     # the FACT-tier cross-file caller site
    assert "1 caller(s) in 1 file(s); update the call sites" in "\n".join(e.payload)
    # it surfaces through the full augment path too (arbitrated dose)
    adds = gw.augment(ev, gw.GatewayState(graph_db=db, repo_root=str(tmp_path)))
    assert any(a.evidence_type == "caller_break" for a in adds)


# =========================================================================== #
# (3) GO EQUIVALENCE + M3 — the caller-break patch_delta cannot produce
# =========================================================================== #
def test_go_signature_change_delivers_caller_break_that_patch_delta_cannot(tmp_path, monkeypatch):
    """The SM-2 LIPI's exact failure: a Go function signature change breaks a cross-file caller.
    patch_delta (ast-only, .py/.pyi) produces NOTHING; the graph-based caller_contract producer
    DELIVERS the caller-break. This is the equivalence gap the whole wave exists to close."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    db = _go_caller_graph(tmp_path)
    ev = _edit_ev("handler.go", "func Handle(a int) error {\n\treturn nil\n}\n",
                  "func Handle(a int, b string) error {\n\treturn nil\n}\n")
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    # the graph-based producer DELIVERS on Go
    envs = gw._produce_caller_contract(ev, st)
    assert len(envs) == 1 and envs[0].evidence_type == "caller_break"
    assert envs[0].fact_id == "Handle"
    assert "1 caller(s) in 1 file(s)" in "\n".join(envs[0].payload)
    # patch_delta (the ONLY prior edit-caller producer) is ast-only -> NOTHING on Go
    assert gw._produce_patch_delta(ev, st) == [], (
        "patch_delta must be silent on a .go edit — that is the gap caller_contract fills")


def test_m3_ast_only_regression_bites_on_go(tmp_path, monkeypatch):
    """MUTATION M3: regress the producer to AST-ONLY (skip a non-.py/.pyi edited file). The Go
    caller-break then vanishes -> the equivalence pin above BITES. Proven inline: the mutant
    filters by extension and returns [] for the Go edit while the real producer delivers."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    db = _go_caller_graph(tmp_path)
    ev = _edit_ev("handler.go", "func Handle(a int) error {}\n",
                  "func Handle(a int, b string) error {}\n")
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))

    def _ast_only_mutant(event, state):
        eba = event.edit_before_after or {}
        for cf in eba:
            if not (cf.endswith(".py") or cf.endswith(".pyi")):
                return []                    # the ast-only blindness (patch_delta._SCAN_EXTS)
        return gw._produce_caller_contract(event, state)

    assert _ast_only_mutant(ev, st) == []            # mutant: Go caller-break lost
    assert gw._produce_caller_contract(ev, st)       # real producer: delivered (bites)


# =========================================================================== #
# (4) correct-or-quiet
# =========================================================================== #
def test_no_signature_change_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    db = _py_caller_graph(tmp_path)
    # only the BODY changed (params identical) -> not a caller-break
    ev = _edit_ev("svc/users.py", "def get_user(a):\n    return a\n",
                  "def get_user(a):\n    return a + 0\n")
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    assert gw._produce_caller_contract(ev, st) == []


def test_no_cross_file_caller_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    # get_user changed, but NO caller edge -> no caller-break (the agent has nothing to update)
    db = _mk_graph(tmp_path, [{"id": 1, "name": "get_user", "file_path": "svc/users.py"}], [])
    ev = _edit_ev("svc/users.py", "def get_user(a):\n    return a\n",
                  "def get_user(a, b):\n    return a\n")
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    assert gw._produce_caller_contract(ev, st) == []


def test_new_file_add_is_quiet(tmp_path, monkeypatch):
    """A create (before=None) has no prior contract to break -> quiet."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    db = _py_caller_graph(tmp_path)
    ev = gw.ToolEvent(kind="edit", command="create svc/users.py",
                      changed_files=("svc/users.py",),
                      edit_before_after={"svc/users.py": (None, "def get_user(a, b):\n    return a\n")})
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    assert gw._produce_caller_contract(ev, st) == []


def test_no_bridges_no_delivery(tmp_path, monkeypatch):
    """Without edit_before_after (bridges off) the producer cannot detect a change -> []."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    db = _py_caller_graph(tmp_path)
    ev = gw.ToolEvent(kind="edit", command="sed -i s/a/b/ svc/users.py",
                      changed_files=("svc/users.py",), edit_before_after=None)
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    assert gw._produce_caller_contract(ev, st) == []


# =========================================================================== #
# (5) LEAK LAW — a test-file caller never surfaces (count OR provenance)
# =========================================================================== #
def test_leak_law_test_caller_excluded_from_break(tmp_path, monkeypatch):
    """A caller that lives in a TEST file must NOT be counted or cited. MUTATION (recorded):
    drop the `_is_leaky(cf)` skip in _fact_callers_of_symbol_in_file -> the test caller inflates
    the count and leaks ``tests/test_users.py`` into provenance -> both asserts below BITE."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "get_user", "file_path": "svc/users.py"},
        {"id": 2, "name": "real_caller", "file_path": "app/main.py"},
        {"id": 3, "name": "test_it", "file_path": "tests/test_users.py", "is_test": 1},
    ], [
        {"id": 1, "source_id": 2, "target_id": 1, "resolution_method": "import", "confidence": 1.0},
        {"id": 2, "source_id": 3, "target_id": 1, "resolution_method": "import", "confidence": 1.0},
    ])
    ev = _edit_ev("svc/users.py", "def get_user(a):\n    return a\n",
                  "def get_user(a, b):\n    return a\n")
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    envs = gw._produce_caller_contract(ev, st)
    assert len(envs) == 1
    e = envs[0]
    body = "\n".join(e.payload)
    assert "1 caller(s) in 1 file(s)" in body                 # the test caller is NOT counted
    prov_files = {f for f, _ in e.provenance}
    assert prov_files == {"app/main.py"}                      # test path never cited
    from groundtruth.runtime.native_render import contains_test_identity
    assert contains_test_identity(body) is False


# =========================================================================== #
# (6) native render dispatch (Finding 2) — caller_break -> the contract diagnostic
# =========================================================================== #
def test_render_envelope_native_dispatch_for_caller_break(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    db = _go_caller_graph(tmp_path)
    ev = _edit_ev("handler.go", "func Handle(a int) error {}\n",
                  "func Handle(a int, b string) error {}\n")
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    e = gw._produce_caller_contract(ev, st)[0]
    # TAGGED: the <gt-fact> wrapper over the payload (unchanged default form)
    tagged = ad.render_envelope(e, native=False)
    assert tagged.startswith('<gt-fact kind="caller_break">') and "</gt-fact>" in tagged
    # NATIVE: the SM-1 compiler CONTRACT diagnostic (path: error: ...), NOT tagged English
    native = ad.render_envelope(e, native=True)
    assert "<gt-" not in native
    assert native == ("handler.go: error: Handle() signature changed; "
                      "1 caller(s) in 1 file(s) must update the call sites\n")
    from groundtruth.runtime.native_render import contains_test_identity
    assert contains_test_identity(native) is False


def test_caller_break_wins_edit_turn_arbitration():
    """On an edit turn the caller_break out-ranks trace/def facts (so it is the dose the seam
    ships) but yields to the narrower signature_mismatch (an arity diagnostic is more specific)."""
    assert ad._EVIDENCE_TYPE_RANK["caller_break"] > ad._EVIDENCE_TYPE_RANK["trace_frame"]
    assert ad._EVIDENCE_TYPE_RANK["caller_break"] < ad._EVIDENCE_TYPE_RANK["signature_mismatch"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
