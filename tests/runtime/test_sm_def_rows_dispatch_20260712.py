"""SM (2026-07-12) — wire the BUILT-but-unwired ``render_def_rows_native`` into the
gateway's native per-class dispatch (``_NATIVE_CLASS_RENDERERS``).

``render_def_rows_native`` (native_render.py) was BUILT + unit-tested (test_native_grammar)
but absent from the SM-2b native dispatch. Its fields ARE in the ``def_ref_partition``
envelope: the provenance IS the list of def sites ((file, line) each) and ``fact_id`` is the
queried symbol, so the SM-1 ``grep-native`` primitive is fed directly — no prose parsing.

RED-first: on the pre-fix tree — equivalently under the MUTATION that removes
``def_ref_partition`` from ``_NATIVE_CLASS_RENDERERS`` — a native standalone
``def_ref_partition`` (no injected mini renderer) renders GENERIC prose ("def: f:ln"),
NOT the ripgrep ROW grammar ("f:ln:sym"). The assertion is a TRUE value assertion
(the row-grammar substring present/absent), never a compile/attribute error.

Preserved: the TAGGED default (native=False) is byte-identical (the native dispatch is
consulted only under ``native=True``); in the seam production path the injected
``def_facts_renderer`` runs FIRST and wins, so production is byte-identical; leak=0
(render_def_rows_native drops test-file rows).

OUT OF SCOPE at the time of THIS wave (named seams). NOTE (SM-10, 2026-07-12): the
``native_args`` side-car later WIRED ``signature_mismatch`` (arity diagnostic) and
``companion_surface`` (render_registration_native, fed the structured siblings LIST the
producer owns) — a conscious, tested choice (test_sm10_signature_delta_native). NOTE (SM,
2026-07-12): the LAST two named seams — ``body_concept`` (grep-native rows, fed the
``_produce_body`` producer's native_args['rows']) and ``scope`` (the BUILT-but-dark
``render_scope_constraint_native``, WIRED not rebuilt; dormant until a registered scope
producer exists) — are now wired too (test_scope_body_concept_native), asserted below.
"""
from __future__ import annotations

from groundtruth.runtime.adapters import miniswe as ad
from groundtruth.runtime.evidence_envelope import VERIFIED, EvidenceEnvelope


def _env(**kw):
    base = dict(producer="def_ref_partition", fact_id="get_user", target="src/users.py",
                evidence_type="def_ref_partition",
                payload=("def: src/users.py:10", "fact-tier callers: 3"),
                provenance=(("src/users.py", 10), ("src/models.py", 42)),
                confidence=0.95, tier=VERIFIED)
    base.update(kw)
    return EvidenceEnvelope.build(**base)


def test_def_ref_partition_native_renders_ripgrep_rows_standalone():
    e = _env()
    # Standalone (no injected mini renderer) + native: the class must render its ripgrep
    # ROW grammar (path:line:sym), NOT the generic tag-free prose of the payload.
    out = ad.render_envelope(e, native=True, def_facts_renderer=None)
    assert "src/users.py:10:get_user" in out, out
    assert "src/models.py:42:get_user" in out, out
    assert "<gt-" not in out                       # native = tag-free
    assert "fact-tier callers" not in out          # native rows drop the supporting prose
    # the generic prose form ("def: src/users.py:10") is REPLACED by the row grammar.
    assert "def: src/users.py:10" not in out


def test_tagged_default_is_byte_identical():
    e = _env()
    # native=False never consults the native dispatch -> the <gt-fact> wrapper is unchanged.
    tagged = ad.render_envelope(e, native=False, def_facts_renderer=None)
    assert tagged.startswith('<gt-fact kind="def_ref_partition">')
    assert "def: src/users.py:10" in tagged        # generic tagged keeps the payload prose


def test_injected_mini_renderer_still_wins_in_production():
    e = _env()
    # In the seam production path the injected def_facts_renderer runs FIRST (DEF_FACTS_KINDS)
    # and wins, so the new native wiring is byte-identical there.
    out = ad.render_envelope(e, native=True, def_facts_renderer=lambda env: "MINI_BYTES\n")
    assert out == "MINI_BYTES\n"


def test_def_rows_drops_test_rows_leak0():
    # a def site in a TEST file must NEVER surface (leak law) — the row is dropped; the
    # remaining source row still renders.
    e = _env(provenance=(("tests/test_users.py", 5), ("src/users.py", 10)))
    out = ad.render_envelope(e, native=True, def_facts_renderer=None)
    assert "src/users.py:10:get_user" in out
    assert "test_users" not in out


def test_last_named_seams_now_wired():
    # SM-10 (2026-07-12): the ``native_args`` side-car WIRED signature_mismatch (arity
    # diagnostic) + companion_surface (registration warning, structured siblings) — both
    # covered by test_sm10_signature_delta_native with biting mutations.
    assert "signature_mismatch" in ad._NATIVE_CLASS_RENDERERS
    assert "companion_surface" in ad._NATIVE_CLASS_RENDERERS
    assert "def_ref_partition" in ad._NATIVE_CLASS_RENDERERS
    # SM (2026-07-12): the LAST two named seams are now WIRED — a conscious, tested choice
    # (test_scope_body_concept_native). body_concept is LIVE (the _produce_body producer
    # attaches native_args['rows']); scope wires the BUILT-but-dark render_scope_constraint_native
    # (dormant until a registered scope producer exists).
    assert "scope" in ad._NATIVE_CLASS_RENDERERS
    assert "body_concept" in ad._NATIVE_CLASS_RENDERERS
