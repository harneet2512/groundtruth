"""caller_contract_view must render a TRUTHFUL view contract, never a change-claim.

RED-FIRST (2026-07-29). Baseline defect, pinned verbatim before the fix:
``_NATIVE_CLASS_RENDERERS`` routed ``caller_contract_view`` to
``_render_caller_break``, so a pure source VIEW (an UNEDITED file the agent
merely read) rendered as

    src/api.py: error: get_user() signature changed; 2 caller(s) in 2 file(s)
    must update the call sites

— a fabricated post-edit fact at VERIFIED tier (nothing changed), and on a
multi-symbol view the placeholder ``fact_id`` even became the "symbol":

    src/api.py: error: viewed_file_contract() signature changed; ...

The fix routes the view class to a view-truthful renderer built from EXISTING
primitives: the producer's own payload head ("<sym>() has N production
caller(s) across M file(s)") + ``render_note_rows_native`` over the
``native_args['caller_rows']`` the producer already attaches (relationship-
agnostic compiler ``note:`` rows — documented as never overclaiming
"signature") + the typed ``render_caller_usage_native`` lines. The break
diagnostic stays EXCLUSIVELY on ``caller_break`` (a real post-edit fact).

MUTATION TARGETS:
  * re-route "caller_contract_view" to ``_render_caller_break`` -> the
    no-change-claim tests bite on "signature changed".
  * route "caller_break" to the view renderer -> the break test bites.
"""

from __future__ import annotations

import dataclasses

from groundtruth.runtime.adapters.miniswe import render_envelope
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope


def _view_envelope(
    fact_id: str,
    payload: tuple,
    native_args: "dict | None",
) -> EvidenceEnvelope:
    env = EvidenceEnvelope.build(
        producer="caller_contract",
        fact_id=fact_id,
        target="src/api.py",
        evidence_type="caller_contract_view",
        payload=payload,
        provenance=(("src/caller.py", 12), ("src/other.py", 40)),
        confidence=0.95,
        tier="VERIFIED",
        graph_revision="graph-9",
        preferred_event="view",
    )
    if native_args is not None:
        env = dataclasses.replace(env, native_args=native_args)
    return env


def _single_symbol_view() -> EvidenceEnvelope:
    return _view_envelope(
        "get_user",
        ("get_user() has 2 production caller(s) across 2 file(s)",),
        {
            "caller_rows": (
                ("src/caller.py", 12, "use"),
                ("src/other.py", 40, "load"),
            ),
            "caller_usage_rows": (
                ("src/caller.py", 12, "get_user", "iterated"),
            ),
        },
    )


def test_view_contract_never_claims_a_signature_changed() -> None:
    """A pure VIEW delivery must not ship a post-edit change-claim (the file was
    never edited). This is the wrong-info-at-VERIFIED-tier pin."""
    rendered = render_envelope(_single_symbol_view(), native=True)

    assert "signature changed" not in rendered, rendered
    assert ": error:" not in rendered, rendered
    assert "must update the call sites" not in rendered, rendered


def test_view_contract_renders_truthful_contract_form() -> None:
    """The truthful view form: the producer's own contract head + the
    relationship-agnostic note rows + the typed usage note."""
    rendered = render_envelope(_single_symbol_view(), native=True)

    assert rendered == (
        "get_user() has 2 production caller(s) across 2 file(s)\n"
        "src/caller.py:12: note: use - verify your change is consistent here\n"
        "src/other.py:40: note: load - verify your change is consistent here\n"
        "src/caller.py:12: note: get_user() result is iterated "
        "(expects an iterable)\n"
    )
    assert "<gt-" not in rendered


def test_multi_symbol_view_never_fabricates_a_symbol() -> None:
    """The multi-symbol placeholder fact_id must never surface as a callable
    symbol in the model-facing bytes (baseline rendered
    ``viewed_file_contract() signature changed``)."""
    env = _view_envelope(
        "viewed_file_contract",
        (
            "get_user() has 1 production caller(s) across 1 file(s)",
            "save_user() has 1 production caller(s) across 1 file(s)",
        ),
        {
            "caller_rows": (
                ("src/caller.py", 12, "use"),
                ("src/other.py", 40, "load"),
            ),
            "caller_usage_rows": (),
        },
    )
    rendered = render_envelope(env, native=True)

    assert "viewed_file_contract()" not in rendered, rendered
    assert "signature changed" not in rendered, rendered
    assert "get_user() has 1 production caller(s)" in rendered
    assert "save_user() has 1 production caller(s)" in rendered


def test_view_contract_without_native_rows_falls_back_generic_truthfully() -> None:
    """No ``native_args`` caller rows -> the view renderer abstains ("") ->
    ``_render_generic`` ships the producer's own truthful payload, still with
    no change-claim (correct-or-quiet, never a fabricated diagnostic)."""
    env = _view_envelope(
        "get_user",
        ("get_user() has 2 production caller(s) across 2 file(s)",),
        {},
    )
    rendered = render_envelope(env, native=True)

    assert "signature changed" not in rendered, rendered
    assert "get_user() has 2 production caller(s)" in rendered


def test_caller_break_keeps_the_break_diagnostic() -> None:
    """MUTATION CHECK: the post-edit ``caller_break`` class (a REAL signature
    change) must keep the native contract-break diagnostic — the fix narrows
    the ROUTING, not the break renderer."""
    env = EvidenceEnvelope.build(
        producer="caller_contract",
        fact_id="get_user",
        target="src/api.py",
        evidence_type="caller_break",
        payload=("legacy payload is not authoritative",),
        provenance=(("src/caller.py", 12),),
        confidence=0.95,
        tier="WARNING",
        graph_revision="graph-9",
        preferred_event="edit_result",
    )
    env = dataclasses.replace(env, native_args={
        "before_parameters": ("uid",),
        "after_parameters": ("uid", "name"),
        "caller_rows": (("src/caller.py", 12, "use"),),
        "caller_usage_rows": (),
    })
    rendered = render_envelope(env, native=True)

    assert rendered.startswith(
        "src/api.py: error: get_user() signature changed (uid -> uid, name); "
        "1 caller(s) in 1 file(s) must update the call sites"
    ), rendered
