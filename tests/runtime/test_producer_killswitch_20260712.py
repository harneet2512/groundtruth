"""Gateway producer sub-flag KILL-SWITCHES — ``GT_CHANGE_SURFACE`` (W-A) / ``GT_PATCH_DELTA``
(W-C), gateway.py:16 (2026-07-12).

gateway.py:16's docstring long claimed "Producer sub-flags honored (``GT_CHANGE_SURFACE`` for
W-A, ``GT_PATCH_DELTA`` for W-C)" while neither name appeared anywhere else in gateway.py — the
two ``augment()`` call sites (``_produce_patch_delta`` on ``KIND_EDIT``, ``_produce_change_surface``
on ``ZERO_ABSENT``) dispatched unconditionally: a dead control claim. (The SAME two env var names
WERE already read one call further down, inside the producers' own downstream engines --
``change_surface._flag_enabled`` / ``patch_delta._flag_on`` -- but with the OPPOSITE, default-OFF
ENABLEMENT polarity; that pre-existing engine-level gate is untouched by this fix and is
independently pinned by ``tests/pretask/test_change_surface.py`` + ``tests/runtime/test_patch_delta.py``.)

This suite pins the NEW gateway.py:16 call-site KILL-SWITCH (default-ON; the literal string
``"0"`` disables) added at ``augment``'s two producer-dispatch lines. Both downstream engines are
MOCKED (``gw.detect_change_surface`` / ``gw.analyze_patch_delta``) so ONLY the Gateway's own new
gate is under test, isolated from the engines' independent (and differently-polarized) flag:

  * default/unset -> both producers fire exactly as they did before the gate existed.
  * "0" -> that producer's class is absent; the OTHER producer, fired on its own event, is
    unaffected (independence -- disabling one flag never disables the sibling).
  * "1" / garbage -> fires (only the literal string "0" disables).
  * MUTATION: monkeypatching the new gate helper back to an unconditional ``True`` (the pre-fix
    call-site shape) reddens the "0 disables" pin -- proving the real gate is load-bearing.

RED-first receipt (captured manually before this file existed, via ``git stash`` on
``gateway.py`` to restore the pre-fix call sites): with the gate absent, the disable tests below
(section 2) FAIL -- ``_fire_zero_absent``/``_fire_edit`` still yield a ``new_file_destination`` /
``signature_mismatch`` envelope even though the flag is ``"0"``, because the old call sites never
looked at ``os.environ`` before dispatching. Section 5's in-suite mutation reproduces the same
red by monkeypatching the gate helper back to ``True``.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

import groundtruth.runtime.gateway as gw
from groundtruth.pretask.change_surface import ChangeSurfaceResult, NewFileDestination


# --------------------------------------------------------------------------- #
# fixtures (mirrors tests/runtime/test_gateway.py's synthetic graph + repo tree)
# --------------------------------------------------------------------------- #
def _mk_graph(tmp_path, nodes, edges):
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
    )
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("end_line", 5),
             n.get("is_test", 0), n.get("language", "python")))
    for e in edges:
        con.execute(
            "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
            "confidence,metadata) VALUES(?,?,?,?,?,?,?,?)",
            (e["id"], e["source_id"], e["target_id"], e.get("type", "CALLS"),
             e.get("source_line", 1), e.get("resolution_method", "import"),
             e.get("confidence", 1.0), e.get("metadata")))
    con.commit()
    con.close()
    return db


def _state(tmp_path, db, **kw):
    return gw.GatewayState(graph_db=db, repo_root=str(tmp_path), **kw)


def _ev(kind, command="", output="", **kw):
    return gw.ToolEvent(kind=kind, command=command, output=output, **kw)


@pytest.fixture(autouse=True)
def _gateway_on(monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    yield


# --------------------------------------------------------------------------- #
# W-A change_surface fixture — byte-for-byte the graph/repo-tree from
# test_gateway.py::test_zero_absent_repeat_fires_change_surface (proven to classify
# ZERO_ABSENT on the 2nd probe); the engine is MOCKED so only the new gate is exercised.
# --------------------------------------------------------------------------- #
def _change_surface_zero_absent_state(tmp_path):
    (tmp_path / "providers").mkdir()
    (tmp_path / "providers" / "aws.py").write_text("class AwsProvider:\n    pass\n")
    (tmp_path / "providers" / "gcp.py").write_text("class GcpProvider:\n    pass\n")
    (tmp_path / "providers" / "__init__.py").write_text(
        "from .aws import AwsProvider\nfrom .gcp import GcpProvider\n"
        "REGISTRY = {'aws': AwsProvider, 'gcp': GcpProvider}\n")
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "AwsProvider", "label": "Class",
         "file_path": "providers/aws.py", "start_line": 1},
        {"id": 2, "name": "GcpProvider", "label": "Class",
         "file_path": "providers/gcp.py", "start_line": 1},
    ], [])
    issue = "Add an azure provider analogous to the existing aws and gcp providers."
    return _state(tmp_path, db, issue_text=issue)


def _fire_zero_absent(st):
    """Two probes (silent, then ZERO_ABSENT) of the SAME missing symbol; returns the
    second call's result — the one where the change_surface producer may speak."""
    ev1 = _ev("search", "grep -rn azure .", "", action_index=3)
    ev2 = _ev("search", "grep -rn azure .", "", action_index=7)
    first = gw.augment(ev1, st)
    assert first == []
    assert gw.classify_outcome(ev2, st) == gw.ZERO_ABSENT
    return gw.augment(ev2, st)


def _fake_change_surface_result():
    return ChangeSurfaceResult(
        entities=["azure"],
        destinations=[NewFileDestination(
            entity="azure", suggested_path="providers/azure.py",
            directory="providers", template_file="",
            registration_file="providers/__init__.py",
            sibling_files=["providers/aws.py"], issue_span="azure provider",
            evidence=["nearest sibling: providers/aws.py"],
        )],
        abstained=False,
    )


# --------------------------------------------------------------------------- #
# W-C patch_delta fixture — mirrors test_gateway_wave2_supermode_20260711.py's
# ``_sig_mismatch_result`` (engine MOCKED so only the new gate is under test).
# --------------------------------------------------------------------------- #
def _fake_patch_delta_result():
    sm = SimpleNamespace(
        caller="bar", caller_file="pkg/mod.py", caller_line=12, symbol="foo",
        positional_args=2, new_min_params=1, new_max_params=1,
        call_site_text="foo(a, b)", confidence=0.8)
    return SimpleNamespace(
        signature_mismatches=[sm], companion_surfaces=[], cochange_partners=[])


def _patch_delta_edit_state(tmp_path):
    db = _mk_graph(tmp_path, [], [])
    return _state(tmp_path, db)


def _fire_edit(st):
    ev = _ev("edit", "sed -i s/a/b/ pkg/mod.py", "",
             edit_before_after={"pkg/mod.py": (None, "after")}, action_index=1)
    return gw.augment(ev, st)


# =========================================================================== #
# 1. DEFAULT/UNSET — both producers fire exactly as before this gate existed.
# =========================================================================== #
def test_change_surface_fires_when_flag_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("GT_CHANGE_SURFACE", raising=False)
    monkeypatch.setattr(gw, "detect_change_surface",
                        lambda *a, **k: _fake_change_surface_result())
    st = _change_surface_zero_absent_state(tmp_path)
    second = _fire_zero_absent(st)
    assert any(a.evidence_type == "new_file_destination" for a in second)


def test_patch_delta_fires_when_flag_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("GT_PATCH_DELTA", raising=False)
    monkeypatch.setattr(gw, "analyze_patch_delta",
                        lambda *a, **k: _fake_patch_delta_result())
    st = _patch_delta_edit_state(tmp_path)
    out = _fire_edit(st)
    assert any(a.evidence_type == "signature_mismatch" for a in out)


# =========================================================================== #
# 2. "0" DISABLES — the flagged producer's class is absent.
# =========================================================================== #
def test_change_surface_disabled_by_literal_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "0")
    monkeypatch.setattr(gw, "detect_change_surface",
                        lambda *a, **k: _fake_change_surface_result())
    st = _change_surface_zero_absent_state(tmp_path)
    second = _fire_zero_absent(st)
    assert not any(a.evidence_type == "new_file_destination" for a in second)


def test_patch_delta_disabled_by_literal_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_PATCH_DELTA", "0")
    monkeypatch.setattr(gw, "analyze_patch_delta",
                        lambda *a, **k: _fake_patch_delta_result())
    st = _patch_delta_edit_state(tmp_path)
    out = _fire_edit(st)
    assert not any(a.evidence_type == "signature_mismatch" for a in out)


# =========================================================================== #
# 3. INDEPENDENCE — disabling one flag never disables the sibling producer.
# =========================================================================== #
def test_change_surface_off_leaves_patch_delta_unaffected(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "0")
    monkeypatch.delenv("GT_PATCH_DELTA", raising=False)
    monkeypatch.setattr(gw, "analyze_patch_delta",
                        lambda *a, **k: _fake_patch_delta_result())
    st = _patch_delta_edit_state(tmp_path)
    out = _fire_edit(st)
    assert any(a.evidence_type == "signature_mismatch" for a in out)


def test_patch_delta_off_leaves_change_surface_unaffected(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_PATCH_DELTA", "0")
    monkeypatch.delenv("GT_CHANGE_SURFACE", raising=False)
    monkeypatch.setattr(gw, "detect_change_surface",
                        lambda *a, **k: _fake_change_surface_result())
    st = _change_surface_zero_absent_state(tmp_path)
    second = _fire_zero_absent(st)
    assert any(a.evidence_type == "new_file_destination" for a in second)


# =========================================================================== #
# 4. "1" / garbage — only the literal string "0" disables.
# =========================================================================== #
@pytest.mark.parametrize("value", ["1", "garbage", "true", "TRUE", "yes"])
def test_change_surface_fires_on_non_literal_zero(tmp_path, monkeypatch, value):
    monkeypatch.setenv("GT_CHANGE_SURFACE", value)
    monkeypatch.setattr(gw, "detect_change_surface",
                        lambda *a, **k: _fake_change_surface_result())
    st = _change_surface_zero_absent_state(tmp_path)
    second = _fire_zero_absent(st)
    assert any(a.evidence_type == "new_file_destination" for a in second)


@pytest.mark.parametrize("value", ["1", "garbage", "true", "TRUE", "yes"])
def test_patch_delta_fires_on_non_literal_zero(tmp_path, monkeypatch, value):
    monkeypatch.setenv("GT_PATCH_DELTA", value)
    monkeypatch.setattr(gw, "analyze_patch_delta",
                        lambda *a, **k: _fake_patch_delta_result())
    st = _patch_delta_edit_state(tmp_path)
    out = _fire_edit(st)
    assert any(a.evidence_type == "signature_mismatch" for a in out)


# =========================================================================== #
# 5. MUTATION — invert the new gate (monkeypatch it back to the pre-fix shape:
# unconditionally True) and show the "0 disables" pin reddens, proving the real
# _change_surface_producer_on / _patch_delta_producer_on gate is load-bearing.
# =========================================================================== #
def test_mutation_reverting_change_surface_gate_reddens_disable_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "0")
    monkeypatch.setattr(gw, "detect_change_surface",
                        lambda *a, **k: _fake_change_surface_result())
    # MUTATION: reproduce the pre-fix call site — no gate at all.
    monkeypatch.setattr(gw, "_change_surface_producer_on", lambda: True)
    st = _change_surface_zero_absent_state(tmp_path)
    second = _fire_zero_absent(st)
    # the mutant fires DESPITE "0" — exactly the bug this fix closes.
    assert any(a.evidence_type == "new_file_destination" for a in second)


def test_mutation_reverting_patch_delta_gate_reddens_disable_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_PATCH_DELTA", "0")
    monkeypatch.setattr(gw, "analyze_patch_delta",
                        lambda *a, **k: _fake_patch_delta_result())
    monkeypatch.setattr(gw, "_patch_delta_producer_on", lambda: True)
    st = _patch_delta_edit_state(tmp_path)
    out = _fire_edit(st)
    assert any(a.evidence_type == "signature_mismatch" for a in out)
