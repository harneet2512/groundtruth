r"""Per-class Lane-A retirement under the Gateway profile — SM-2b MIGRATION (2026-07-11).

The SM-2 LIPI bounce KEPT l3.contract/l3b.evidence as KEEP-legacy because the gateway had no
cross-language caller-contract producer (retiring them would be a §26.4 DELETION). SM-2b BUILDS
that producer (``gateway._produce_caller_contract`` — graph-based, delivers on Go/Rust/TS/JS),
so the migration can complete HONESTLY:

  * l3.cochange  -> KEEP-legacy (2026-07-22 correction): the Gateway ships no cochange
                    replacement, so retiring it was a §26.4 DELETION of the completeness signal.
                    Its noise-guarded Lane-A block delivers under both flags (like consensus.scope).
  * l3.contract  -> RETIRE, gated PER-EDIT on the replacement-delivers tripwire.
  * l3b.evidence -> RETIRE, gated PER-EDIT on the replacement-delivers tripwire.
  * consensus.scope -> KEEP-legacy (hedged orientation, no registered ``scope`` class).

The CARDINAL invariant (the SM-2 bounce, now structural): a REPLACEMENT-GATED class retires
ONLY on a turn where ``gateway._produce_caller_contract`` provably DELIVERS an equivalent
caller-break for THAT edit. On a language/case it cannot (bridges off, no sig-change, no
cross-file caller), the producer is silent -> the legacy KEEPS delivering (never a deletion).
"""
from __future__ import annotations

import ast
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import gt_mini_patch as g
from groundtruth.runtime import gateway as gw
from groundtruth.runtime.native_render import render_cochange_native


_SEAM = Path(__file__).parents[1] / "gt_mini_patch.py"

_INTERNAL: set = set()  # 2026-07-22: emptied — l3.cochange un-retired (no replacement -> KEEP-legacy)
_WITH_REPLACEMENT = {"l3.contract", "l3b.evidence"}
_RETIRABLE = _INTERNAL | _WITH_REPLACEMENT
_KEEP_LEGACY = {"consensus.scope_map", "post_search.localize", "concern.consensus", "l3.cochange"}


def _append_guards(kinds: set[str]) -> dict[str, str]:
    """The `if`-guard source for each `lane_a.append((<kind>, ...))` site in the seam."""
    tree = ast.parse(_SEAM.read_text(encoding="utf-8"))
    guards: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for child in node.body:
            if not isinstance(child, ast.Expr) or not isinstance(child.value, ast.Call):
                continue
            call = child.value
            if not isinstance(call.func, ast.Attribute) or call.func.attr != "append":
                continue
            if not call.args or not isinstance(call.args[0], ast.Tuple):
                continue
            first = call.args[0].elts[0]
            if isinstance(first, ast.Constant) and first.value in kinds:
                guards[first.value] = ast.unparse(node.test)
    return guards


# --------------------------------------------------------------------------- #
# SOURCE-SHAPE PINS — every retirable class is gated by the per-class predicate; the two
# replacement-gated classes pass the ``replacement_ready`` tripwire.
# --------------------------------------------------------------------------- #
def test_every_retirable_site_uses_the_predicate():
    guards = _append_guards(_RETIRABLE)
    for kind in _RETIRABLE:
        assert kind in guards, kind
        assert "_lane_a_retired_under_gateway" in guards[kind], (kind, guards[kind])


def test_replacement_gated_sites_thread_replacement_ready():
    """l3.contract + l3b.evidence retire ONLY behind the per-edit tripwire — the guard MUST
    thread ``replacement_ready`` (the §26.4 equivalence gate), not retire unconditionally."""
    guards = _append_guards(_WITH_REPLACEMENT)
    for kind in _WITH_REPLACEMENT:
        assert "replacement_ready" in guards[kind], (kind, guards[kind])
        assert "_cc_ready" in guards[kind], (kind, guards[kind])


def test_keep_legacy_sites_are_not_retired():
    guards = _append_guards({"consensus.scope_map", "post_search.localize"})
    for kind in ("consensus.scope_map", "post_search.localize"):
        assert "_lane_a_retired_under_gateway" not in guards.get(kind, ""), kind


# --------------------------------------------------------------------------- #
# SET + HELPER PINS
# --------------------------------------------------------------------------- #
def test_retired_sets_are_exactly_declared():
    assert g._GATEWAY_RETIRED_INTERNAL == frozenset(_INTERNAL)
    assert g._GATEWAY_RETIRED_WITH_REPLACEMENT == frozenset(_WITH_REPLACEMENT)
    assert g._GATEWAY_RETIRED_LANE_A == frozenset(_RETIRABLE)


def test_helper_cochange_is_keep_legacy_under_gateway(monkeypatch):
    """2026-07-22: l3.cochange un-retired. The Gateway ships no cochange replacement, so
    retiring it would be a §26.4 deletion -> it is KEEP-legacy: NEVER retired under the
    Gateway (delivers its Lane-A completeness block under both flags)."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    assert g._lane_a_retired_under_gateway("l3.cochange") is False
    assert g._lane_a_retired_under_gateway("l3.cochange", replacement_ready=False) is False
    assert g._lane_a_retired_under_gateway("l3.cochange", replacement_ready=True) is False


def test_helper_replacement_gated_needs_ready(monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    for kind in _WITH_REPLACEMENT:
        # replacement NOT ready -> KEEP legacy (the §26.4 no-deletion guarantee)
        assert g._lane_a_retired_under_gateway(kind, replacement_ready=False) is False, kind
        # replacement provably delivered -> retire
        assert g._lane_a_retired_under_gateway(kind, replacement_ready=True) is True, kind
    # a non-retirable class is never retired regardless of the tripwire
    assert g._lane_a_retired_under_gateway("consensus.scope_map", replacement_ready=True) is False


def test_helper_off_retires_nothing_byte_identical(monkeypatch):
    """GT_GATEWAY OFF -> NOTHING retired (even with replacement_ready=True) -> Lane-A delivers
    exactly as pre-Wave-1. This is the byte-identical-off guarantee."""
    monkeypatch.setenv("GT_GATEWAY", "0")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    for k in _RETIRABLE | _KEEP_LEGACY:
        assert g._lane_a_retired_under_gateway(k, replacement_ready=True) is False, k


def test_helper_baseline_retires_nothing(monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", True)
    for k in _RETIRABLE:
        assert g._lane_a_retired_under_gateway(k, replacement_ready=True) is False, k


# --------------------------------------------------------------------------- #
# MUTATION M2 — the helper's flag guard is load-bearing for byte-identical-off.
# --------------------------------------------------------------------------- #
def test_m2_helper_flag_guard_is_load_bearing(monkeypatch):
    """MUTATION M2: ignore ``_gt_gateway_on()`` in the helper. With GT_GATEWAY OFF a
    replacement-gated class would then retire -> the off path DROPS the legacy -> the
    byte-identical-off pin (`test_helper_off_retires_nothing_byte_identical`) BITES."""
    monkeypatch.setenv("GT_GATEWAY", "0")
    monkeypatch.setattr(g, "_GT_BASELINE", False)

    def _mutant(kind, *, replacement_ready=False):
        if kind in g._GATEWAY_RETIRED_INTERNAL:               # dropped the `if not _gt_gateway_on()`
            return True
        if kind in g._GATEWAY_RETIRED_WITH_REPLACEMENT:
            return bool(replacement_ready)
        return False

    assert _mutant("l3.contract", replacement_ready=True) is True   # mutant retires with flag OFF
    assert g._lane_a_retired_under_gateway("l3.contract", replacement_ready=True) is False  # real: safe


# --------------------------------------------------------------------------- #
# TRIPWIRE (direct) — the producer-delivers gate
# --------------------------------------------------------------------------- #
def _mk_caller_graph(tmp_path, sym, def_file, caller_file, lang="python"):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,"
        " file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,"
        " is_exported INTEGER, is_test INTEGER, language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,"
        " source_line INTEGER, source_file TEXT, resolution_method TEXT, confidence REAL, metadata TEXT);"
        "CREATE TABLE properties(id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT);")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(1,'Function',?,?,1,5,0,?)", (sym, def_file, lang))
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(2,'Function','caller',?,1,5,0,?)", (caller_file, lang))
    con.execute("INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
                "confidence) VALUES(1,2,1,'CALLS',3,'import',1.0)")
    con.commit()
    con.close()
    return db


def _tripwire_setup(tmp_path, monkeypatch, *, def_file, caller_file, before_sig, after_sig,
                    lang="python", gateway=True, bridges=True):
    sym = "get_user" if lang == "python" else "Handle"
    os.makedirs(str(tmp_path), exist_ok=True)   # tmp_path may be a caller-chosen subdir
    db = _mk_caller_graph(tmp_path, sym, def_file, caller_file, lang)
    dpath = tmp_path / def_file
    dpath.parent.mkdir(parents=True, exist_ok=True)
    dpath.write_text(after_sig + "\n\treturn None\n", encoding="utf-8")
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_GATEWAY", "1" if gateway else "0")
    if bridges:
        monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")
    else:
        monkeypatch.delenv("GT_GATEWAY_EDIT_BRIDGES", raising=False)
    action = {"command": "str_replace", "path": def_file,
              "old_str": before_sig, "new_str": after_sig}
    return action


def test_tripwire_true_on_python_and_go_signature_change(tmp_path, monkeypatch):
    py = _tripwire_setup(tmp_path / "py", monkeypatch, def_file="svc/users.py",
                         caller_file="app/main.py",
                         before_sig="def get_user(a):", after_sig="def get_user(a, b):")
    assert g._gt_gateway_caller_contract_ready(py, g._effective_cmd(py)) is True
    go = _tripwire_setup(tmp_path / "go", monkeypatch, def_file="handler.go",
                         caller_file="main.go", lang="go",
                         before_sig="func Handle(a int) error {",
                         after_sig="func Handle(a int, b string) error {")
    assert g._gt_gateway_caller_contract_ready(go, g._effective_cmd(go)) is True


def test_tripwire_false_when_gateway_off(tmp_path, monkeypatch):
    a = _tripwire_setup(tmp_path, monkeypatch, def_file="svc/users.py", caller_file="app/main.py",
                        before_sig="def get_user(a):", after_sig="def get_user(a, b):",
                        gateway=False)
    assert g._gt_gateway_caller_contract_ready(a, g._effective_cmd(a)) is False


def test_tripwire_false_when_bridges_off(tmp_path, monkeypatch):
    a = _tripwire_setup(tmp_path, monkeypatch, def_file="svc/users.py", caller_file="app/main.py",
                        before_sig="def get_user(a):", after_sig="def get_user(a, b):",
                        bridges=False)
    assert g._gt_gateway_caller_contract_ready(a, g._effective_cmd(a)) is False


def test_tripwire_false_without_cross_file_caller(tmp_path, monkeypatch):
    # def + edit reconstruct fine, but the caller lives in the SAME file -> not a cross-file break
    a = _tripwire_setup(tmp_path, monkeypatch, def_file="svc/users.py", caller_file="svc/users.py",
                        before_sig="def get_user(a):", after_sig="def get_user(a, b):")
    assert g._gt_gateway_caller_contract_ready(a, g._effective_cmd(a)) is False


# --------------------------------------------------------------------------- #
# FULL-SEAM real-bytes MIGRATION — the honest §26.4 replacement, end to end.
# --------------------------------------------------------------------------- #
def _mk_graph_with_caller(db):
    """foo (pkg/mod.py, sig `foo(a, b)`) with one deterministic (import, 1.0) cross-file
    caller bar (pkg/other.py) — enough for _graph_contract_block + _evidence to produce."""
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,"
        " file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,"
        " is_exported INTEGER, is_test INTEGER, language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,"
        " source_line INTEGER, source_file TEXT, resolution_method TEXT, confidence REAL, metadata TEXT);"
        "CREATE TABLE properties(id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT);")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,is_test,"
                "language) VALUES(1,'Function','foo','pkg/mod.py',1,5,'foo(a, b)',0,'python')")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,is_test,"
                "language) VALUES(2,'Function','bar','pkg/other.py',1,5,'bar()',0,'python')")
    con.execute("INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
                "confidence) VALUES(1,2,1,'CALLS',3,'import',1.0)")
    con.commit()
    con.close()


def _run_sig_change_edit(tmp_path, monkeypatch, *, gateway_on, bridges_on):
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "mod.py").write_text("def foo(a, b, c):\n    return a\n", encoding="utf-8")
    db = str(tmp_path / "graph.db")
    _mk_graph_with_caller(db)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_POST_SEARCH_ON", False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_GATEWAY", "1" if gateway_on else "0")
    # PIN THE ARBITER OFF (2026-07-28). Every assertion below expects BOTH `<gt-contract` and
    # `<gt-evidence` in ONE observation -- a two-dose observation, which is precisely what the
    # global arbiter exists to prevent. These tests are about the LEGACY inline path, so the
    # arbiter must be pinned, not inherited: this helper already pins GT_GATEWAY,
    # GT_GATEWAY_EDIT_BRIDGES, _POST_SEARCH_ON and _GT_BASELINE, and the arbiter belongs in that
    # list for the same reason. Omitting it was harmless only while the seam force-disabled the
    # arbiter without a batch-commit handshake; it is not harmless now.
    monkeypatch.delenv("GT_GLOBAL_ARBITER", raising=False)
    if bridges_on:
        monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")
    else:
        monkeypatch.delenv("GT_GATEWAY_EDIT_BRIDGES", raising=False)
    g._reset_oracle_state()
    action = {"command": "str_replace", "path": "pkg/mod.py",
              "old_str": "def foo(a, b):", "new_str": "def foo(a, b, c):"}
    out = {"output": "ok", "returncode": 0}
    g._augment_output(action, out)
    g._reset_oracle_state()
    return out["output"]


def test_migration_replacement_ready_retires_legacy_delivers_caller_break(tmp_path, monkeypatch):
    """GT_GATEWAY=1 + bridges on + a signature-changing edit with a cross-file caller: the
    replacement DELIVERS -> the legacy l3.contract / l3b.evidence are SUPPRESSED and the
    Gateway ships the caller_break instead. The honest migration, in real bytes."""
    obs = _run_sig_change_edit(tmp_path, monkeypatch, gateway_on=True, bridges_on=True)
    assert "caller_break" in obs                          # the replacement delivered
    assert "signature changed" in obs
    assert "<gt-contract" not in obs                      # legacy l3.contract retired
    assert "<gt-evidence" not in obs                      # legacy l3b.evidence retired


def test_migration_gateway_off_delivers_legacy_byte_identical(tmp_path, monkeypatch):
    """GT_GATEWAY=0 -> the legacy blocks deliver exactly as pre-Wave-1 (byte-identical-off)."""
    obs = _run_sig_change_edit(tmp_path, monkeypatch, gateway_on=False, bridges_on=False)
    assert "<gt-contract" in obs and "<gt-evidence" in obs
    assert "caller_break" not in obs


def test_migration_no_replacement_keeps_legacy(tmp_path, monkeypatch):
    """GT_GATEWAY=1 but bridges OFF -> the producer cannot reconstruct the change -> NO
    replacement -> the legacy KEEPS delivering (the §26.4 no-deletion guarantee, live)."""
    obs = _run_sig_change_edit(tmp_path, monkeypatch, gateway_on=True, bridges_on=False)
    assert "<gt-contract" in obs and "<gt-evidence" in obs
    assert "caller_break" not in obs


# --------------------------------------------------------------------------- #
# MUTATION M1 — the producer-delivers gate is load-bearing: a SILENT producer must not
# retire the legacy.
# --------------------------------------------------------------------------- #
def test_m1_silent_producer_keeps_legacy(tmp_path, monkeypatch):
    """MUTATION M1: force the Gateway caller_contract producer to emit NOTHING. The tripwire
    must then be False and the legacy l3.contract / l3b.evidence must STILL deliver (equivalence-
    before-retirement). If the retirement ignored the tripwire, the legacy would vanish here."""
    monkeypatch.setattr(gw, "_produce_caller_contract", lambda ev, st: [])
    obs = _run_sig_change_edit(tmp_path, monkeypatch, gateway_on=True, bridges_on=True)
    assert "<gt-contract" in obs and "<gt-evidence" in obs   # legacy survives a silent producer
    assert "caller_break" not in obs


# --------------------------------------------------------------------------- #
# cochange — the GATEWAY NATIVE PLANE ships nothing (no double-delivery); the completeness
# fact reaches the agent via its Lane-A block instead (un-retired 2026-07-22). This pins the
# no-native-form / no-double-delivery half only.
# --------------------------------------------------------------------------- #
def test_cochange_gateway_native_plane_ships_nothing(monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setattr(gw, "analyze_patch_delta", lambda *a, **k: SimpleNamespace(
        signature_mismatches=[], companion_surfaces=[],
        cochange_partners=[SimpleNamespace(file="a/x.py", count=3)]))
    ev = gw.ToolEvent(kind="edit", command="sed -i s/a/b/ a/x.py", output="",
                      edit_before_after={"a/x.py": (None, "after")}, action_index=1)
    st = gw.GatewayState(graph_db=None, repo_root="")
    assert gw.augment(ev, st) == []                       # internal: NO Gateway delivery
    assert render_cochange_native("a/x.py", 3) == ""      # no native form
