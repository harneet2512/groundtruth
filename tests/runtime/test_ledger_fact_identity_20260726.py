"""The delivery row must say WHICH feature spoke and WHEN it was contracted to.

Without this, "did feature N deliver at its contracted time?" is unanswerable from the
artifact -- measured on run 30225435976: 95 delivered payloads, `fact_class` null on 80, and
no boundary field at all. Three fields each LOOK like the boundary (`commitment_boundary`,
`surface`, the ledger's free-form `event_type`) and none of them is, which nearly produced a
false "0/95 on contracted boundary" verdict.

The layer->fact map is hand-authored because `fact_registry` is keyed by EVIDENCE TYPE and
does not resolve producer LAYER names. These tests are what stop it drifting.
"""

from __future__ import annotations

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import fact_registry as fr
from groundtruth.runtime.reasoning_runtime import _FACT_DECISION_CONTRACTS


def test_every_mapped_value_is_a_real_registered_fact_class():
    """A typo'd or renamed fact class must fail here, not silently stamp garbage."""
    for layer, fact in seam._LAYER_TO_FACT_CLASS.items():
        assert fact in _FACT_DECISION_CONTRACTS, (
            f"{layer} -> {fact!r} is not one of the 10 registered FACT classes"
        )


def test_every_mapped_fact_resolves_a_contracted_boundary():
    for layer, fact in seam._LAYER_TO_FACT_CLASS.items():
        _f, boundary = seam._fact_identity_for_layer(layer)
        assert _f == fact
        assert boundary, f"{layer} -> {fact} resolves no deliver_by"
        assert boundary == fr.registration_for(fact).deliver_by


def test_unmapped_layer_stamps_nothing_rather_than_guessing():
    """Correct-or-quiet: absent identity must mean 'not a DIRECT feature', never a wrong one."""
    assert seam._fact_identity_for_layer("l3b.evidence") == (None, None)
    assert seam._fact_identity_for_layer("") == (None, None)
    assert seam._fact_identity_for_layer("totally.unknown.layer") == (None, None)


@pytest.mark.parametrize(
    "layer,fact,boundary",
    [
        ("l3.contract", "caller_contract", "file_view"),
        ("edit.syntax", "syntax_result", "edit_result"),
        ("patch_delta", "signature_delta", "edit_result"),
        ("submit_gate", "submit_refusal", "submit"),
        ("gateway.trace_frame", "localization", "search_result"),
        ("change_surface", "newfile_precedent", "failed_search"),
        ("verify.horizon.executed", "covering_red", "test_result"),
    ],
)
def test_known_producer_bindings(layer, fact, boundary):
    """The bindings from reading each producer (CLAUDE.md §6.1)."""
    assert seam._fact_identity_for_layer(layer) == (fact, boundary)


def test_the_stamp_is_wired_into_the_durable_row():
    """Source guard: the resolver must actually be called where the row is built."""
    import inspect

    src = inspect.getsource(seam._runtime_ledger_record)
    assert "_fact_identity_for_layer(kind)" in src
    assert 'setdefault("fact_class"' in src
    assert 'setdefault("contracted_boundary"' in src


def test_stamp_never_clobbers_a_caller_supplied_fact_class():
    """`setdefault`, not assignment -- the SS-8 side-car already carries fact_class."""
    import inspect

    src = inspect.getsource(seam._runtime_ledger_record)
    assert '_row["fact_class"] =' not in src


def test_all_ten_fact_classes_are_reachable_from_some_layer():
    """If a FACT has no producing layer, its deliveries can never be attributed."""
    mapped = set(seam._LAYER_TO_FACT_CLASS.values())
    missing = sorted(set(_FACT_DECISION_CONTRACTS) - mapped)
    assert not missing, f"FACT classes no layer maps to: {missing}"


def test_completion_cert_maps_to_submit_refusal():
    """The SS-2 submit-RED referee emits its refusal on `completion_cert`, not `submit_gate`.

    CLAUDE.md §6.1 defines GT_CERT_DELIVERY as `completion_cert`+`submit_gate`. Omitting
    `completion_cert` made submit_refusal / GT_SS_SUBMIT_RED / GT_CERT_DELIVERY read
    TRIGGER-ABSENT on a run where the referee demonstrably BOUNCED a submit (run
    30229092179, actionlint task, iteration 136: outcome=bounce_once, reason=ss_submit_red).

    A missing map entry understates a feature as never-triggered — the worst direction to be
    wrong in, because it reads as correct-quiet rather than as a defect.
    """
    assert seam._fact_identity_for_layer("completion_cert") == ("submit_refusal", "submit")


def test_both_submit_boundary_layers_resolve_to_the_same_fact():
    """`submit_gate` and `completion_cert` are two producers of one contracted boundary."""
    gate = seam._fact_identity_for_layer("submit_gate")
    cert = seam._fact_identity_for_layer("completion_cert")
    assert gate == cert == ("submit_refusal", "submit")
