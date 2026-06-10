#!/usr/bin/env python3
"""Red->green behavior tests for the LIPI squash-batch fixes in gt_intel.py.

One test (or test group) per assigned item. Each asserts the POST-FIX behavior and
would FAIL against the pre-fix code (the red->green contract noted per test):

  #7  get_callers — deterministic gate (name_match caller suppressed, like get_callees)
  #8  get_siblings — same-file containment (cross-file parent_id collision suppressed)
  #22 classify_caller_usage — call-line text/score from the ACTUAL call line (no off-by-window)
  #23 _format_import_for_language — Go/Java/C#/PHP import from package decl, else neutral (no dirname path)
  #24 SIBLING return-type vote — denominator = typed siblings, min-support floor >= 2
  #25 TYPE upgrade — driven by structured usage="destructure" enum (dead "destruct" grep fixed)
  #26 rank_and_select — negative-spec boost on WORD BOUNDARY (no "note"/"cannot" false boost)
  #27 generate_pretask_briefing — TEST link gated deterministic, like the top-caller (one gate)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import gt_intel  # noqa: E402


# ── synthetic graph helpers ──────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
    file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
    return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
    parent_id INTEGER
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
    source_line INTEGER, source_file TEXT, resolution_method TEXT,
    confidence REAL, metadata TEXT
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(_SCHEMA)
    return c


def _node(c, **kw):
    cols = ("id", "label", "name", "qualified_name", "file_path", "start_line",
            "end_line", "signature", "return_type", "is_exported", "is_test",
            "language", "parent_id")
    defaults = dict(label="Function", name="", qualified_name="", file_path="",
                    start_line=0, end_line=0, signature="", return_type="",
                    is_exported=0, is_test=0, language="python", parent_id=0)
    defaults.update(kw)
    c.execute(
        "INSERT INTO nodes (" + ",".join(cols) + ") VALUES (" + ",".join("?" * len(cols)) + ")",
        tuple(defaults[k] for k in cols),
    )


def _edge(c, src, tgt, method, conf=1.0, src_file="", line=0, etype="CALLS"):
    c.execute(
        "INSERT INTO edges (source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES (?,?,?,?,?,?,?)",
        (src, tgt, etype, line, src_file, method, conf),
    )


@pytest.fixture(autouse=True)
def _reset_gate():
    # The admissibility gate has module-level state; reset to the full default so a
    # prior test's narrowing never leaks. Tests that need the deterministic gate rely
    # on _deterministic_sql_in() intersecting the active set with the fact-set.
    gt_intel._active_resolutions = gt_intel.VERIFIED_RESOLUTIONS
    yield


# ── #7 get_callers deterministic gate ────────────────────────────────────────

def test_item7_get_callers_suppresses_name_match():
    """A name_match cross-file caller must NOT be returned (matches get_callees twin).
    Pre-fix (get_callers used _resolution_sql_in) this returned the name_match caller."""
    c = _conn()
    _node(c, id=1, name="target", file_path="app.py")
    _node(c, id=2, name="real_caller", file_path="a.py")    # import -> kept
    _node(c, id=3, name="phantom", file_path="b.py")        # name_match -> dropped
    _edge(c, 2, 1, "import", src_file="a.py")
    _edge(c, 3, 1, "name_match", src_file="b.py")
    c.commit()

    callers = gt_intel.get_callers(c, target_id=1, target_file="app.py")
    methods = {rm for (_n, _l, _f, rm) in callers}
    names = {n.name for (n, _l, _f, _rm) in callers}
    assert "name_match" not in methods, "name_match caller leaked (phantom dependent)"
    assert names == {"real_caller"}


# ── #8 get_siblings same-file containment ────────────────────────────────────

def test_item8_get_siblings_same_file_only():
    """Siblings must be constrained to the target's own file even when parent_id collides
    across files. Pre-fix (no file_path guard) the cross-file node leaked as a sibling."""
    c = _conn()
    # parent class node id=10 in app.py
    _node(c, id=10, label="Class", name="C", file_path="app.py")
    _node(c, id=1, label="Method", name="m_target", file_path="app.py", parent_id=10)
    _node(c, id=2, label="Method", name="m_same", file_path="app.py", parent_id=10)
    # cross-file node sharing the SAME parent_id (collision) — must be excluded
    _node(c, id=3, label="Method", name="m_other_file", file_path="other.py", parent_id=10)
    c.commit()

    sibs = gt_intel.get_siblings(c, target_id=1)
    names = {s.name for s in sibs}
    assert names == {"m_same"}, f"cross-file sibling leaked: {names}"


# ── #22 classify_caller_usage call-line text/score ───────────────────────────

def test_item22_call_text_is_the_actual_line_at_file_head(tmp_path):
    """When the call is on line 1, the spec text must be THAT line, not window line 2.
    Pre-fix (lines[min(1,...)] over a clamped window) returned the 2nd line at the head."""
    src = tmp_path / "caller.py"
    src.write_text("result = target(x)\nother = 5\nmore = 6\n")
    score, summary, call_text, usage = gt_intel.classify_caller_usage(
        str(tmp_path), "caller.py", call_line=1)
    assert "target(x)" in call_text, f"call_text off-by-window: {call_text!r}"
    assert "other = 5" not in call_text


def test_item22_score_from_call_line_not_neighbor(tmp_path):
    """An isinstance() on a NEIGHBOR line must not promote a plain-invoke call to score 3.
    Pre-fix the regexes scanned the whole 4-line window, so the neighbor leaked in."""
    src = tmp_path / "caller.py"
    # line 2 is a plain invoke; line 3 has isinstance (neighbor noise)
    src.write_text("x = 1\nfoo(bar)\nif isinstance(y, int):\n    pass\n")
    score, summary, call_text, usage = gt_intel.classify_caller_usage(
        str(tmp_path), "caller.py", call_line=2)
    assert "foo(bar)" in call_text
    assert usage == "invoke", f"neighbor isinstance leaked into score: usage={usage}"
    assert score == 1


def test_item22_destructure_on_call_line(tmp_path):
    src = tmp_path / "caller.py"
    src.write_text("a, b = target(x)\n")
    score, summary, call_text, usage = gt_intel.classify_caller_usage(
        str(tmp_path), "caller.py", call_line=1)
    assert usage == "destructure"
    assert score == 3


# ── #23 import path from package decl, else neutral ──────────────────────────

def test_item23_go_no_fabricated_dirname_path(tmp_path):
    """Go import must NOT be the on-disk dirname. With a package decl present we surface
    the real package; the literal `import "internal/foo"` fabrication must be gone."""
    pkgdir = tmp_path / "internal" / "foo"
    pkgdir.mkdir(parents=True)
    (pkgdir / "bar.go").write_text("package foo\n\nfunc Helper() {}\n")
    callee = gt_intel.GraphNode(
        id=1, label="Function", name="Helper", qualified_name="",
        file_path="internal/foo/bar.go", start_line=3, end_line=3, signature="",
        return_type="", is_exported=True, is_test=False, language="go", parent_id=0)
    stmt = gt_intel._format_import_for_language(str(tmp_path), callee, "go")
    assert 'import "internal/foo"' not in stmt, f"fabricated dirname import: {stmt!r}"
    assert "package foo" in stmt  # real decl surfaced


def test_item23_go_neutral_when_no_decl(tmp_path):
    """No readable package decl -> neutral (from path) form, never a guessed import."""
    callee = gt_intel.GraphNode(
        id=1, label="Function", name="Helper", qualified_name="",
        file_path="internal/foo/bar.go", start_line=3, end_line=3, signature="",
        return_type="", is_exported=True, is_test=False, language="go", parent_id=0)
    stmt = gt_intel._format_import_for_language(str(tmp_path), callee, "go")  # file absent
    assert stmt == "Helper (from internal/foo/bar.go)"


def test_item23_java_uses_package_decl(tmp_path):
    d = tmp_path / "src" / "main"
    d.mkdir(parents=True)
    (d / "Foo.java").write_text("package com.acme.real;\n\nclass Foo {}\n")
    callee = gt_intel.GraphNode(
        id=1, label="Class", name="Foo", qualified_name="",
        file_path="src/main/Foo.java", start_line=3, end_line=3, signature="",
        return_type="", is_exported=True, is_test=False, language="java", parent_id=0)
    stmt = gt_intel._format_import_for_language(str(tmp_path), callee, "java")
    assert stmt == "import com.acme.real.Foo;"  # decl, NOT src.main.Foo


# ── #24 SIBLING return-type vote denominator ─────────────────────────────────

def test_item24_typed_denominator_not_diluted(tmp_path):
    """3 typed siblings, all 'bool', plus 7 untyped -> 3/3 typed unanimity must upgrade.
    Pre-fix denominator was len(siblings)=10 -> 3/10 < 0.7 and the contract was suppressed."""
    c = _conn()
    _node(c, id=10, label="Class", name="C", file_path="app.py")
    _node(c, id=1, label="Method", name="m_target", file_path="app.py", parent_id=10,
          return_type="bool")
    # 3 typed siblings agreeing on bool (ids 2-4)
    for i, nm in enumerate(("s1", "s2", "s3"), start=2):
        _node(c, id=i, label="Method", name=nm, file_path="app.py", parent_id=10,
              return_type="bool", start_line=10 + i, end_line=20 + i)
    # 7 untyped siblings (return_type="") — ids 20-26, no collision with class id=10
    for i, nm in enumerate(("u1", "u2", "u3", "u4", "u5", "u6", "u7"), start=20):
        _node(c, id=i, label="Method", name=nm, file_path="app.py", parent_id=10,
              return_type="", start_line=30 + i, end_line=40 + i)
    c.commit()

    # Write a real source file so read_lines returns a body for the best sibling.
    (tmp_path / "app.py").write_text("\n".join(f"# line {i}" for i in range(60)))
    target = gt_intel.get_target_node(c, "app.py", "m_target")
    ev = gt_intel.compute_evidence(c, str(tmp_path), target)
    sib = [e for e in ev if e.family == "SIBLING"]
    assert sib, "no SIBLING evidence produced"
    assert sib[0].score == 3, f"typed-unanimity contract suppressed: score={sib[0].score}"
    assert "typed siblings agree" in sib[0].summary


def test_item24_min_support_floor(tmp_path):
    """A single typed sibling must NOT trivially hit 100% (floor requires >= 2 typed)."""
    c = _conn()
    _node(c, id=10, label="Class", name="C", file_path="app.py")
    _node(c, id=1, label="Method", name="m_target", file_path="app.py", parent_id=10)
    _node(c, id=2, label="Method", name="s_typed", file_path="app.py", parent_id=10,
          return_type="bool", start_line=12, end_line=20)
    _node(c, id=3, label="Method", name="s_untyped", file_path="app.py", parent_id=10,
          return_type="", start_line=22, end_line=30)
    c.commit()
    (tmp_path / "app.py").write_text("\n".join(f"# line {i}" for i in range(40)))
    target = gt_intel.get_target_node(c, "app.py", "m_target")
    ev = gt_intel.compute_evidence(c, str(tmp_path), target)
    sib = [e for e in ev if e.family == "SIBLING"]
    assert sib, "no SIBLING evidence produced"
    assert sib[0].score == 1, f"single typed sibling wrongly hit 100%: score={sib[0].score}"


# ── #25 TYPE upgrade driven by usage enum ────────────────────────────────────

def test_item25_type_upgrades_on_destructure_usage(tmp_path):
    """A CALLER that destructures the return value must upgrade TYPE to score 2.
    Pre-fix the consumer grepped 'destruct' in summary (never written) -> always score 1."""
    c = _conn()
    _node(c, id=1, label="Function", name="target", file_path="app.py",
          start_line=5, end_line=9, return_type="Tuple[int,int]")
    _node(c, id=2, label="Function", name="caller", file_path="caller.py",
          start_line=1, end_line=3)
    _edge(c, 2, 1, "import", src_file="caller.py", line=2)
    c.commit()
    (tmp_path / "app.py").write_text("\n".join(f"# {i}" for i in range(12)))
    (tmp_path / "caller.py").write_text("x = 1\na, b = target(z)\nreturn a\n")
    target = gt_intel.get_target_node(c, "app.py", "target")
    ev = gt_intel.compute_evidence(c, str(tmp_path), target)
    type_ev = [e for e in ev if e.family == "TYPE"]
    caller_ev = [e for e in ev if e.family == "CALLER"]
    assert caller_ev and caller_ev[0].usage == "destructure"
    assert type_ev and type_ev[0].score == 2, "TYPE upgrade dead — usage enum not consumed"


# ── #26 negative-spec word boundary ──────────────────────────────────────────

def test_item26_no_boost_on_substring_note():
    """A TEST summary containing 'notification'/'cannot' must NOT be boosted to 3.
    Pre-fix substring 'not' matched and over-ranked these nodes."""
    n = gt_intel.EvidenceNode(
        family="TEST", score=1, name="t", file="t.py", line=1,
        source_code="", summary="references notification handler; cannot reach")
    out = gt_intel.rank_and_select([n])
    assert out[0].score == 1, "substring 'not' inside notification/cannot still boosts"


def test_item26_boost_on_real_negative_spec():
    """A genuine 'raises ValueError' TEST summary must still be boosted to 3."""
    n = gt_intel.EvidenceNode(
        family="TEST", score=2, name="t", file="t.py", line=1,
        source_code="", summary="raises ValueError on bad input")
    out = gt_intel.rank_and_select([n])
    assert out[0].score == 3


# ── #27 briefing TEST link gated like the top-caller (one gate) ──────────────

def test_item27_briefing_test_link_deterministic(tmp_path):
    """The briefing TEST line must come from a deterministic edge, like the top-caller.
    Pre-fix the TEST sub-query used res_methods (name_match allowed) -> phantom test link."""
    c = _conn()
    _node(c, id=1, label="Function", name="do_thing", qualified_name="do_thing",
          file_path="app.py", start_line=5)
    # a name_match test edge ONLY (no deterministic) -> must NOT produce a TEST line
    _node(c, id=2, label="Function", name="test_phantom", file_path="t.py",
          start_line=1, is_test=1)
    _edge(c, 2, 1, "name_match", src_file="t.py")
    c.commit()
    out = gt_intel.generate_pretask_briefing(c, str(tmp_path), ["do_thing"], max_lines=8)
    assert "TEST:" not in out, f"phantom name_match test link leaked into briefing:\n{out}"
