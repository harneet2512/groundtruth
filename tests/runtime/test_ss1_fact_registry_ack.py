"""SS-1 — fact_registry.ack_expected metadata + localization defer-boundary (Stage-1).

Item 3 of the SS-1 build:
  * add an ``ack_expected`` boolean field to every registration (METADATA ONLY — consumed by
    the ack-metrics telemetry, GT_SS_ACK_METRICS; the delivery kernel never branches on it, so
    it changes no delivered byte);
  * ensure the localization class's declared boundaries make ordinal-DEFER expressible
    (earliest_event == deliver_by == a single fixed EVENTS boundary, so a localization fact
    produced at a LATER ordinal — view/edit — is expressibly late and defers to the next
    search/review boundary).
"""
from __future__ import annotations

import pytest

from groundtruth.runtime import fact_registry as fr


# --------------------------------------------------------------------------- #
# ack_expected — the new metadata field.
# --------------------------------------------------------------------------- #
def test_ack_expected_metadata_present():
    # every registration carries a strict-bool ack_expected; every §1 class declares a
    # receipt_predicate, so an ack IS expected -> all True.
    for fc in fr.all_fact_classes():
        reg = fr.registration(fc)
        assert isinstance(reg.ack_expected, bool), fc
        assert reg.ack_expected is True, fc
        assert reg.receipt_predicate  # the ack has a named non-reacquisition receipt


def test_ack_expected_accessor_resolves_via_alias():
    # the public accessor resolves a canonical key AND a gateway evidence_type alias.
    assert fr.ack_expected("localization") is True
    assert fr.ack_expected("def_ref_partition") is True   # alias -> def_partition
    assert fr.ack_expected("covering_verdict") is True     # alias -> covering_red
    # an unregistered type has no fact whose ack could be expected.
    assert fr.ack_expected("totally_unknown_type") is False
    assert fr.ack_expected("") is False


def test_ack_expected_is_metadata_only_not_a_scalar_string_field():
    # it must NOT be swept into the non-empty-string scalar guard (it is a bool, not a §1 text
    # column). The import-time self-check enforces bool-ness (see the mutation note below).
    reg = fr.registration("recovery")
    assert reg.ack_expected in (True, False)


def test_self_check_bool_guard_bites(monkeypatch):
    # MUTATION SENTINEL: ack_expected must be a strict bool. Poison one registration's field
    # with a non-bool and re-run the import-time self-check -> it must FAIL LOUD.
    import dataclasses
    bad = dataclasses.replace(fr.registration("obligations"), ack_expected="yes")  # type: ignore[arg-type]
    monkeypatch.setitem(fr.REGISTRY, "obligations", bad)
    with pytest.raises(ValueError):
        fr._self_check()


# --------------------------------------------------------------------------- #
# localization boundary — makes ordinal-DEFER expressible.
# --------------------------------------------------------------------------- #
def test_localization_boundary_supports_ordinal_defer():
    loc = fr.registration("localization")
    # a SINGLE fixed boundary (earliest == deliver_by) so a later-ordinal production is
    # expressibly LATE -> the arbiter/seam can DEFER it to the next occurrence of this boundary.
    assert loc.earliest_event == loc.deliver_by
    assert loc.earliest_event in fr.EVENTS
    # the re-slotted decision boundary is search_result (ordinal ~1) — where "which file to
    # open" is decided; a view/edit-produced localization fact is past it and defers.
    assert loc.deliver_by == fr.EVENT_SEARCH_RESULT
    assert fr.required_event("localization") == fr.EVENT_SEARCH_RESULT


def test_localization_earliest_and_required_event_agree_for_defer():
    # earliest_event_for and required_event agree (single fixed boundary) -> the defer target is
    # unambiguous. MUTATION[split earliest != deliver_by] would make the defer window ambiguous.
    assert fr.earliest_event_for("localization") == fr.required_event("localization")
