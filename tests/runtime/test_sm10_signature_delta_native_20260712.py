r"""SM-10 (2026-07-12) — the ``native_args`` envelope side-car -> native class diagnostics.

The DARK->LIVE promotion the SM-10 audit named #1 ROI: ``render_signature_delta_native``
(native_render.py:486) was BUILT but could never fire from the gateway, because the
``EvidenceEnvelope`` carried only a TEXT payload — the renderer's structured arity fields
(positional_args / new_min_params / new_max_params / caller_file / caller_line / symbol)
had nowhere to ride. So ``signature_mismatch`` fell back to ``_render_generic`` prose even
under ``GT_GATEWAY_NATIVE``. This wave adds ONE optional ``native_args`` side-car field and
wires the ``signature_mismatch`` (mypy/gopls arity) and ``companion_surface``
(ruff/eslint registration) classes onto their SM-1 renderers.

Pins (each with a proven-biting mutation, recorded inline):
  (1) the gateway producer ATTACHES native_args with the exact structured fields;
  (2) NATIVE render == the mypy/gopls arity diagnostic (not tag-free prose) — MUTATION:
      unregister ``signature_mismatch`` -> the true-value assertion BITES;
  (3) companion_surface renders the linter registration warning from the structured
      ``siblings`` LIST the producer owns — MUTATION: unregister -> BITES;
  (4) leak law — the native diagnostic carries ZERO test identity;
  (5) BYTE-IDENTICAL when OFF — native=False is the unchanged tagged form, and a
      native_args-less envelope renders generic under native=True (the wiring is a pure
      no-op absent the side-car);
  (6) IDENTITY-NEUTRAL — native_args is out of __eq__/__hash__/FIELD_ORDER/to_dict, so
      every existing envelope round-trips + hashes identically.

Windows: run with PYTHONIOENCODING=utf-8.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.runtime import gateway as gw
from groundtruth.runtime.adapters import miniswe as ad
from groundtruth.runtime.evidence_envelope import (
    VERIFIED,
    EvidenceEnvelope,
    from_dict,
    to_dict,
)
from groundtruth.runtime.native_render import contains_test_identity


# --------------------------------------------------------------------------- #
# fixtures — a real sqlite graph + a real repo tree (never a mock)
# --------------------------------------------------------------------------- #
def _make_graph(path, nodes, edges) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, "
        "signature TEXT, return_type TEXT, is_exported INT, is_test INT, "
        "language TEXT, parent_id INT)")
    con.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, "
        "confidence REAL, metadata TEXT)")
    for nid, name, fpath, is_test in nodes:
        con.execute(
            "INSERT INTO nodes (id, label, name, file_path, is_test, language) "
            "VALUES (?,?,?,?,?,?)", (nid, "Function", name, fpath, is_test, "python"))
    for src, tgt, method, conf, sfile, sline in edges:
        con.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, "
            "confidence, source_file, source_line) VALUES (?,?,'CALLS',?,?,?,?)",
            (src, tgt, method, conf, sfile, sline))
    con.commit()
    con.close()


def _edit_ev(rel, before, after):
    return gw.ToolEvent(kind="edit", command=f"str_replace {rel}", output="",
                        changed_files=(rel,), action_index=1,
                        edit_before_after={rel: (before, after)})


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_PATCH_DELTA", "1")


def _sig_mismatch_env(tmp_path: Path) -> EvidenceEnvelope:
    """A firing signature_mismatch: get_user gains a required positional param + a
    FACT-edge caller `use()` that still passes the OLD arity (1) -> arity break."""
    repo = tmp_path
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "caller.py").write_text(
        "def use():\n    return get_user(5)\n", encoding="utf-8")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "import", 0.9, "src/caller.py", 2)])
    ev = _edit_ev("src/api.py", "def get_user(uid):\n    return uid\n",
                  "def get_user(uid, name):\n    return uid\n")
    st = gw.GatewayState(graph_db=str(db), repo_root=str(repo))
    envs = [e for e in gw._produce_patch_delta(ev, st)
            if e.evidence_type == "signature_mismatch"]
    assert len(envs) == 1, envs
    return envs[0]


def _companion_env(tmp_path: Path) -> EvidenceEnvelope:
    """A firing companion_surface: registry.py references aws+gcp (>=2 siblings) but not
    the newly-added azure -> a registration gap carrying the structured sibling list."""
    repo = tmp_path
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "registry.py").write_text(
        'PROVIDERS = {\n    "aws": aws,\n    "gcp": gcp,\n}\n', encoding="utf-8")
    before = "def aws():\n    pass\n\n\ndef gcp():\n    pass\n"
    after = before + "\n\ndef azure():\n    pass\n"
    ev = _edit_ev("src/providers.py", before, after)
    st = gw.GatewayState(graph_db=str(repo / "nope.db"), repo_root=str(repo))
    envs = [e for e in gw._produce_patch_delta(ev, st)
            if e.evidence_type == "companion_surface"]
    assert len(envs) == 1, envs
    return envs[0]


# =========================================================================== #
# (1) the producer attaches the STRUCTURED native_args
# =========================================================================== #
def test_signature_producer_attaches_native_args(tmp_path):
    e = _sig_mismatch_env(tmp_path)
    assert e.native_args == {
        "caller_file": "src/caller.py",
        "caller_line": 2,
        "symbol": "get_user",
        "positional_args": 1,
        "new_min_params": 2,
        "new_max_params": 2,
    }


# =========================================================================== #
# (2) NATIVE render == the mypy/gopls arity diagnostic (the load-bearing pin)
# =========================================================================== #
def test_signature_mismatch_renders_native_arity_diagnostic(tmp_path):
    e = _sig_mismatch_env(tmp_path)
    # TAGGED (default / GT_GATEWAY_NATIVE off): the <gt-fact> wrapper over the prose payload.
    tagged = ad.render_envelope(e, native=False)
    assert tagged.startswith('<gt-fact kind="signature_mismatch">')
    assert "</gt-fact>" in tagged
    assert "call passes 1 positional arg(s)" in tagged   # the generic prose, unchanged
    assert "error:" not in tagged                          # NOT the compiler grammar
    # NATIVE: the SM-1 mypy/gopls arity DIAGNOSTIC — the RL-trained fix-on channel.
    native = ad.render_envelope(e, native=True)
    assert native == (
        "src/caller.py:2: error: get_user() takes 2 positional argument(s) but 1 given\n")
    assert "<gt-" not in native


def test_mutation_unregister_signature_mismatch_reddens(tmp_path, monkeypatch):
    """MUTATION: remove ``signature_mismatch`` from _NATIVE_CLASS_RENDERERS. _render_native_class
    then returns "" -> render_envelope falls back to _render_generic's tag-free prose, so the
    (2) true-value assertion (native == the arity diagnostic) BITES. Proven inline: the mutant
    output no longer carries the ``error:`` diagnostic grammar."""
    e = _sig_mismatch_env(tmp_path)
    real = ad.render_envelope(e, native=True)
    assert "error:" in real                                # green before the mutation

    mutant_registry = dict(ad._NATIVE_CLASS_RENDERERS)
    mutant_registry.pop("signature_mismatch")
    monkeypatch.setattr(ad, "_NATIVE_CLASS_RENDERERS", mutant_registry)
    mutated = ad.render_envelope(e, native=True)
    assert mutated != real                                 # the registration is load-bearing
    assert "src/caller.py:2: error:" not in mutated        # the diagnostic is gone (bites)
    assert "call passes 1 positional arg(s)" in mutated    # degrades to generic prose


# =========================================================================== #
# (3) companion_surface -> the linter registration warning (structured siblings)
# =========================================================================== #
def test_companion_producer_attaches_native_args(tmp_path):
    e = _companion_env(tmp_path)
    a = e.native_args
    assert a["file"] == "src/registry.py"
    assert a["symbol"] == "azure"
    assert a["siblings"] == ["aws", "gcp"]                  # the STRUCTURED list, not prose
    assert a["line"] in (2, 3)                              # a referencing-line surface loc


def test_companion_surface_renders_native_registration_warning(tmp_path):
    e = _companion_env(tmp_path)
    tagged = ad.render_envelope(e, native=False)
    assert tagged.startswith('<gt-fact kind="companion_surface">')
    assert "warning:" not in tagged                         # tagged prose, unchanged
    native = ad.render_envelope(e, native=True)
    line = e.native_args["line"]
    assert native == (
        f"src/registry.py:{line}: warning: registers aws, gcp but not 'azure'\n")
    assert "<gt-" not in native


def test_mutation_unregister_companion_reddens(tmp_path, monkeypatch):
    """MUTATION: remove ``companion_surface`` from the registry -> the native render loses the
    linter grammar and degrades to generic prose (bites the (3) pin)."""
    e = _companion_env(tmp_path)
    assert "warning: registers" in ad.render_envelope(e, native=True)
    mutant = dict(ad._NATIVE_CLASS_RENDERERS)
    mutant.pop("companion_surface")
    monkeypatch.setattr(ad, "_NATIVE_CLASS_RENDERERS", mutant)
    assert "warning: registers" not in ad.render_envelope(e, native=True)


# =========================================================================== #
# (4) LEAK LAW — the native diagnostics carry zero test identity
# =========================================================================== #
def test_native_diagnostics_leak_zero(tmp_path):
    assert contains_test_identity(
        ad.render_envelope(_sig_mismatch_env(tmp_path), native=True)) is False
    assert contains_test_identity(
        ad.render_envelope(_companion_env(tmp_path), native=True)) is False


# =========================================================================== #
# (5) BYTE-IDENTICAL when the side-car is absent — the wiring is a pure no-op
# =========================================================================== #
def test_native_args_absent_renders_generic_under_native():
    """A signature_mismatch envelope with NO native_args (any non-gateway construction)
    renders exactly as before: the adapter abstains ("") -> _render_generic native voice.
    This is the byte-identity guarantee for every envelope not carrying the side-car."""
    e = EvidenceEnvelope.build(
        producer="p", fact_id="get_user", target="src/caller.py",
        evidence_type="signature_mismatch",
        payload=("get_user() now takes 2-2",), provenance=(("src/caller.py", 2),),
        confidence=0.6, tier=VERIFIED)
    assert e.native_args is None
    native = ad.render_envelope(e, native=True)
    assert "error:" not in native                          # no diagnostic without the fields
    assert "get_user() now takes 2-2" in native            # the generic native prose
    assert "<gt-" not in native


# =========================================================================== #
# (6) IDENTITY-NEUTRAL — native_args is out of eq/hash/serialization
# =========================================================================== #
def _clean(**kw) -> EvidenceEnvelope:
    base = dict(producer="p", fact_id="get_user", target="src/x.py",
                evidence_type="signature_mismatch", payload=("x",),
                provenance=(("src/x.py", 1),), confidence=0.6, tier=VERIFIED)
    base.update(kw)
    return EvidenceEnvelope.build(**base)


def test_native_args_neutral_to_eq_and_hash():
    plain = _clean()
    withargs = _clean(native_args={"symbol": "get_user", "caller_line": 2})
    # differing ONLY in native_args -> still ==, same hash, same dedup_key (identity-neutral)
    assert withargs == plain
    assert hash(withargs) == hash(plain)
    assert withargs.dedup_key == plain.dedup_key


def test_native_args_out_of_serialization_and_field_order():
    withargs = _clean(native_args={"symbol": "get_user"})
    assert "native_args" not in EvidenceEnvelope.FIELD_ORDER
    d = to_dict(withargs)
    assert "native_args" not in d
    assert list(d.keys()) == list(EvidenceEnvelope.FIELD_ORDER)  # to_dict contract intact
    # round-trip: __eq__ ignores the side-car, so from_dict(to_dict(env)) == env holds even
    # though native_args is not serialized (a render side-car, not durable state).
    assert from_dict(d) == withargs


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
