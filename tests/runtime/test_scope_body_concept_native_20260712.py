r"""SM (2026-07-12) — the LAST two named per-class native renderers: scope + body_concept.

The SM-2b native dispatch (``adapters.miniswe._NATIVE_CLASS_RENDERERS``) had real renderers for
trace_frame / caller_break / def_ref_partition / signature_mismatch / companion_surface /
localization; ``scope`` and ``body_concept`` were the last named seams. This wave wires both:

  * body_concept — LIVE. ``gateway._produce_body`` attaches ``native_args['rows']`` (the
    concept-hit function bodies as (path,line,sym) triples), so the adapter renders the
    grep-native ROW block DIRECTLY via ``render_body_concept_native`` (grep-native, hedge-free,
    leak=0). This is a def_partition-family answer that rides the agent's OWN grep channel.
  * scope — WIRED, NOT REBUILT. ``render_scope_constraint_native`` ALREADY EXISTED (native_render)
    but was DARK; it is wired into the adapter dispatch as-is. DORMANT in production: no live
    gateway producer emits a ``scope`` evidence_type today (``scope`` is not a registered class;
    the only scope surface is the seam's Lane-A ``_consensus_scope_block`` <gt-scope> tag, not
    routed through this adapter), so the entry is unit-tested but inert on a live run until a
    registered scope producer exists — mirroring ``localization``'s inert-until-its-producer state.

Pins (deterministic; no ONNX / network / task IDs):
  (A) each renderer's native grammar + hedge-free + leak=0 (identity firewall inherited verbatim);
  (B) adapter dispatch under native=True; RED via the load-bearing dispatch MUTATION (remove the
      entry -> generic fallback, NOT the native form);
  (C) BYTE-IDENTICAL-OFF: native=False -> the <gt-fact> tagged form is unchanged; a native_args-less
      body_concept -> "" -> generic fallback;
  (D) LEAK MUTATIONS: neuter ``_is_test_path`` -> a test row / test target survives (redness proves
      the firewall is load-bearing);
  (E) the ``_produce_body`` producer attaches native_args end-to-end.

Windows: run with ``PYTHONIOENCODING=utf-8`` (payloads may carry an em-dash).
"""
from __future__ import annotations

import sqlite3

import pytest

import groundtruth.runtime.gateway as gw
import groundtruth.runtime.native_render as nr
from groundtruth.runtime.adapters import miniswe as ad
from groundtruth.runtime.evidence_envelope import INFO, EvidenceEnvelope


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _body_env(rows, *, target="svc/x.py", sym="handle") -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="body_concept", fact_id=sym, target=target, evidence_type="body_concept",
        payload=(f'{len(rows)} function bodies mention "{sym}" (no name or path match)',),
        provenance=tuple((p, ln) for p, ln, _s in rows),
        confidence=0.4, tier=INFO, native_args={"rows": list(rows)})


def _scope_env(target="src/pkg/config.py") -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="scope", fact_id=target, target=target, evidence_type="scope",
        payload=("in scope",), provenance=(), confidence=0.4, tier=INFO)


def _mk_graph(tmp_path, nodes):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,"
        " file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,"
        " is_exported INTEGER, is_test INTEGER, language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);")
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,is_test,language)"
            " VALUES(?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("is_test", 0), "python"))
    con.commit()
    con.close()
    return db


# =========================================================================== #
# 1. RENDERERS — native grammar, hedge-free, leak=0
# =========================================================================== #
def test_body_concept_native_is_grep_native_rows():
    out = nr.render_body_concept_native(
        [("svc/x.py", 10, "handle"), ("svc/y.py", 20, "process")])
    assert out == "svc/x.py:10:handle\nsvc/y.py:20:process"
    assert "<gt-" not in out.lower()
    assert "mention" not in out.lower()                       # the prose narration is dropped
    assert nr.contains_test_identity(out) is False


def test_body_concept_native_drops_test_row_and_mutation_reddens(monkeypatch):
    row = [("tests/test_helpers.py", 3, "test_foo")]
    assert nr.render_body_concept_native(row) == ""           # REAL: test-file row dropped
    # MUTATION: neuter the per-row test-path DROP -> the row survives (non-empty). Proves the
    # inherited render_def_rows_native `_is_test_path` firewall is load-bearing here too.
    monkeypatch.setattr(nr, "_is_test_path", lambda *a, **k: False)
    assert nr.render_body_concept_native(row) != ""


def test_scope_native_is_a_compiler_note_constraint():
    out = nr.render_scope_constraint_native("src/pkg/config.py")
    assert out == "src/pkg/config.py: note: your change must also update this file"
    assert "<gt-" not in out.lower()
    assert nr.contains_test_identity(out) is False
    # no named decision (no file) -> INTERNAL only, no narration.
    assert nr.render_scope_constraint_native("") == ""


def test_scope_native_firewalls_a_test_target_and_mutation_reddens(monkeypatch):
    assert nr.render_scope_constraint_native("tests/test_config.py") == ""     # REAL: firewalled
    # MUTATION: neuter the whole-file test-path guard -> the test target leaks into a note.
    monkeypatch.setattr(nr, "_is_test_path", lambda *a, **k: False)
    assert nr.render_scope_constraint_native("tests/test_config.py") != ""


# =========================================================================== #
# 2. ADAPTER DISPATCH — native=True routes to the wired renderer (RED via mutation)
# =========================================================================== #
def test_body_concept_dispatch_renders_rows_under_native():
    env = _body_env([("svc/x.py", 10, "handle"), ("svc/y.py", 20, "process")])
    out = ad.render_envelope(env, native=True)
    assert "svc/x.py:10:handle" in out and "svc/y.py:20:process" in out
    assert "<gt-" not in out.lower()
    assert "mention" not in out.lower()


def test_scope_dispatch_renders_note_under_native():
    out = ad.render_envelope(_scope_env("src/pkg/config.py"), native=True)
    assert "src/pkg/config.py: note: your change must also update this file" in out
    assert "<gt-" not in out.lower()


def test_body_concept_dispatch_is_load_bearing(monkeypatch):
    """MUTATION: remove the body_concept native dispatch entry -> the envelope falls back to
    _render_generic (the prose narration), NOT the grep rows. Proves the entry matters (this is
    the RED state before this wave wired it)."""
    env = _body_env([("svc/x.py", 10, "handle")])
    assert "svc/x.py:10:handle" in ad.render_envelope(env, native=True)         # REAL: rows
    mutant = dict(ad._NATIVE_CLASS_RENDERERS)
    mutant.pop("body_concept")
    monkeypatch.setattr(ad, "_NATIVE_CLASS_RENDERERS", mutant)
    assert ad._render_native_class(env) == ""                                   # no native form
    fallback = ad.render_envelope(env, native=True)
    assert "svc/x.py:10:handle" not in fallback and "mention" in fallback       # generic prose


def test_scope_dispatch_is_load_bearing(monkeypatch):
    env = _scope_env("src/pkg/config.py")
    assert "note:" in ad.render_envelope(env, native=True)                      # REAL: note
    mutant = dict(ad._NATIVE_CLASS_RENDERERS)
    mutant.pop("scope")
    monkeypatch.setattr(ad, "_NATIVE_CLASS_RENDERERS", mutant)
    assert ad._render_native_class(env) == ""                                   # no native form


# =========================================================================== #
# 3. BYTE-IDENTICAL-OFF — native=False keeps the <gt-fact> tagged form
# =========================================================================== #
def test_tagged_default_is_byte_identical_body_concept():
    env = _body_env([("svc/x.py", 10, "handle")])
    tagged = ad.render_envelope(env, native=False)
    assert tagged.startswith('<gt-fact kind="body_concept">')
    assert "mention" in tagged                                                  # payload prose kept


def test_tagged_default_is_byte_identical_scope():
    tagged = ad.render_envelope(_scope_env("src/pkg/config.py"), native=False)
    assert tagged.startswith('<gt-fact kind="scope">')


def test_body_concept_without_native_args_falls_back_generic():
    """A body_concept envelope carrying NO native_args (non-gateway construction) renders "" from
    the native dispatch -> _render_generic world-fact voice. Byte-identical to pre-wire native."""
    env = EvidenceEnvelope.build(
        producer="body_concept", fact_id="handle", target="svc/x.py",
        evidence_type="body_concept", payload=("2 function bodies mention x",),
        provenance=(("svc/x.py", 10),), confidence=0.4, tier=INFO)
    assert env.native_args is None
    assert ad._render_body_concept(env) == ""                                   # no rows -> quiet
    out = ad.render_envelope(env, native=True)                                  # generic native
    assert "<gt-" not in out.lower() and "mention" in out


def test_body_concept_dispatch_leak_zero_on_test_row():
    env = _body_env([("tests/test_x.py", 3, "test_foo"), ("svc/x.py", 10, "handle")],
                    target="svc/x.py")
    out = ad.render_envelope(env, native=True)
    assert "svc/x.py:10:handle" in out
    assert "test_x" not in out
    assert nr.contains_test_identity(out) is False


# =========================================================================== #
# 4. PRODUCER — _produce_body attaches native_args end-to-end
# =========================================================================== #
def test_produce_body_attaches_native_args(tmp_path, monkeypatch):
    db = _mk_graph(tmp_path, [{"id": 1, "name": "handle", "file_path": "svc/x.py"}])
    # Cluster-2b: _body_rows now yields a 5th element (the node id) for the def-partition
    # sidecar; the stub must match the real contract (leading 4 elements unchanged).
    monkeypatch.setattr(gw, "_body_rows",
                        lambda con, sym, root: [("svc/x.py", 10, "handle", "Function", 1),
                                                ("svc/y.py", 20, "process", "Method", 2)])
    ev = gw.ToolEvent(kind="search", command="grep -rn handle .", output="", action_index=0)
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    envs = gw._produce_body(ev, st)
    assert len(envs) == 1
    e = envs[0]
    assert e.evidence_type == "body_concept"
    assert e.native_args == {"rows": [("svc/x.py", 10, "handle"),
                                      ("svc/y.py", 20, "process")]}
    rendered = ad.render_envelope(e, native=True)
    assert "svc/x.py:10:handle" in rendered and "svc/y.py:20:process" in rendered
    assert "<gt-" not in rendered.lower()
    assert nr.contains_test_identity(rendered) is False


def test_produce_body_native_args_neutral_to_identity(tmp_path, monkeypatch):
    """native_args is compare=False / unserialized: attaching it must not move the dedup key vs
    the same fact built without it (identity/byte-chain unchanged)."""
    db = _mk_graph(tmp_path, [{"id": 1, "name": "handle", "file_path": "svc/x.py"}])
    monkeypatch.setattr(gw, "_body_rows",
                        lambda con, sym, root: [("svc/x.py", 10, "handle", "Function", 1)])
    ev = gw.ToolEvent(kind="search", command="grep -rn handle .", output="", action_index=0)
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    e = gw._produce_body(ev, st)[0]
    twin = EvidenceEnvelope.build(
        producer=e.producer, fact_id=e.fact_id, target=e.target,
        evidence_type=e.evidence_type, payload=e.payload, provenance=e.provenance,
        confidence=e.confidence, tier=e.tier, graph_revision=e.graph_revision,
        valid_until=e.valid_until, preferred_event=e.preferred_event)
    assert e.dedup_key == twin.dedup_key                                        # identity unmoved


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
