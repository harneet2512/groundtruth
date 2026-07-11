"""Stage-1: the fact-to-decision registry (Wave 2 / verifyandobserve.md §1).

The registry is the SPINE of RL-adherent delivery: every model-facing fact class has a
static registration, and **unregistered facts must never render** (correct-or-quiet). This
suite pins that invariant deterministically, before any producer is wired to consult it.

TTD: written RED (module absent) -> implement -> GREEN. Biting mutation documented on
`test_registry_is_complete` (delete one registration => it bites).

Pins:
- (a) all 11 §1 fact classes registered, every field non-empty;
- (b) an unknown class is NOT registered and is NOT renderable (the core invariant);
- (c) `all_fact_classes()` is deterministic + sorted;
- (d) every registration's earliest_event / deliver_by is a valid event name;
- fidelity spot-checks pin the exact §1 producer / surface / renderer / dose values.
"""

from __future__ import annotations

import pytest

from groundtruth.runtime import fact_registry as fr

# The exact 11 model-facing fact classes of verifyandobserve.md §1 (the spine table).
EXPECTED_FACT_CLASSES: frozenset[str] = frozenset(
    {
        "obligations",
        "localization",
        "def_partition",
        "caller_contract",
        "syntax_result",
        "signature_delta",
        "covering_red",
        "submit_refusal",
        "cochange_prior",
        "newfile_precedent",
        "recovery",
    }
)

# The scalar (string) fields every registration must fill non-empty. freshness_deps is a
# tuple checked separately (non-empty sequence of non-empty strings).
_SCALAR_FIELDS = (
    "fact_class",
    "producer",
    "target_decision",
    "earliest_event",
    "deliver_by",
    "surface",
    "native_renderer",
    "max_dose",
    "receipt_predicate",
    "causal_eval",
)


# --------------------------------------------------------------------------- #
# (a) completeness — every §1 fact class registered; every field non-empty.
# --------------------------------------------------------------------------- #
def test_registry_is_complete():
    # MUTATION: delete any one entry from REGISTRY -> this equality bites (both the set
    # mismatch and the length check fail). Restore to go GREEN.
    assert set(fr.all_fact_classes()) == set(EXPECTED_FACT_CLASSES)
    assert len(fr.REGISTRY) == len(EXPECTED_FACT_CLASSES) == 11


def test_registry_key_matches_fact_class():
    # The dict key IS the fact_class (no drift between key and payload identity).
    for key, reg in fr.REGISTRY.items():
        assert reg.fact_class == key


def test_every_scalar_field_nonempty():
    for fc in fr.all_fact_classes():
        reg = fr.registration(fc)
        assert reg is not None
        for field in _SCALAR_FIELDS:
            val = getattr(reg, field)
            assert isinstance(val, str), f"{fc}.{field} must be str, got {type(val)}"
            assert val.strip(), f"{fc}.{field} is empty"


def test_freshness_deps_nonempty_tuple_of_strings():
    for fc in fr.all_fact_classes():
        reg = fr.registration(fc)
        assert isinstance(reg.freshness_deps, tuple), f"{fc}.freshness_deps not a tuple"
        assert reg.freshness_deps, f"{fc}.freshness_deps is empty"
        for dep in reg.freshness_deps:
            assert isinstance(dep, str) and dep.strip(), f"{fc} dep empty: {dep!r}"


# --------------------------------------------------------------------------- #
# (b) the CORE INVARIANT — unregistered facts must never render.
# --------------------------------------------------------------------------- #
def test_unregistered_is_not_registered_and_not_renderable():
    assert fr.is_registered("bogus_unregistered") is False
    assert fr.renderable("bogus_unregistered") is False
    assert fr.registration("bogus_unregistered") is None


def test_unregistered_empty_and_none_inputs_quiet():
    # Junk inputs are correct-or-quiet: never registered, never renderable, never raise.
    for junk in ("", "  ", "OBLIGATIONS", "obligation", "localisation"):
        assert fr.is_registered(junk) is False
        assert fr.renderable(junk) is False
        assert fr.registration(junk) is None


def test_every_registered_class_is_renderable():
    for fc in fr.all_fact_classes():
        assert fr.is_registered(fc) is True
        assert fr.renderable(fc) is True


def test_assert_registered_raises_for_unknown_returns_for_known():
    with pytest.raises(fr.UnregisteredFactError):
        fr.assert_registered("bogus_unregistered")
    reg = fr.assert_registered("localization")
    assert reg is fr.registration("localization")


# --------------------------------------------------------------------------- #
# (c) determinism — all_fact_classes() is stable + sorted.
# --------------------------------------------------------------------------- #
def test_all_fact_classes_sorted_and_stable():
    a = fr.all_fact_classes()
    b = fr.all_fact_classes()
    assert isinstance(a, tuple)
    assert a == b  # stable across calls
    assert list(a) == sorted(a)  # sorted
    assert len(set(a)) == len(a)  # no duplicates


# --------------------------------------------------------------------------- #
# (d) each registration's events are valid event names.
# --------------------------------------------------------------------------- #
def test_events_are_valid():
    for fc in fr.all_fact_classes():
        reg = fr.registration(fc)
        assert reg.earliest_event in fr.EVENTS, f"{fc} earliest_event invalid"
        assert reg.deliver_by in fr.EVENTS, f"{fc} deliver_by invalid"


def test_surfaces_and_renderers_are_valid():
    for fc in fr.all_fact_classes():
        reg = fr.registration(fc)
        assert reg.surface in fr.SURFACES, f"{fc} surface invalid"
        assert reg.native_renderer in fr.RENDERERS, f"{fc} renderer invalid"


# --------------------------------------------------------------------------- #
# fidelity spot-checks — the registry MUST match the §1 table verbatim.
# --------------------------------------------------------------------------- #
def test_section1_verbatim_values():
    obl = fr.registration("obligations")
    assert obl.producer == "spec"
    assert obl.target_decision == "initial plan"
    assert obl.surface == "brief"
    assert obl.native_renderer == "plan"
    assert obl.earliest_event == "task_start" and obl.deliver_by == "task_start"

    loc = fr.registration("localization")
    assert loc.producer == "v1r_brief"
    assert loc.target_decision == "which file to open"
    assert loc.native_renderer == "ranked-list"
    assert loc.max_dose == "small"
    assert loc.freshness_deps == ("nodes", "edges", "content_rev")

    sub = fr.registration("submit_refusal")
    assert sub.producer == "submit_gate"
    assert sub.surface == "submit"
    assert sub.native_renderer == "test-native"
    assert sub.freshness_deps == ("patch_rev", "graph_rev")

    rec = fr.registration("recovery")
    assert rec.producer == "governor"
    assert rec.surface == "steer"
    assert rec.native_renderer == "imperative"
    assert rec.earliest_event == "failure_obs"


def test_registration_is_frozen():
    reg = fr.registration("localization")
    with pytest.raises(Exception):
        reg.producer = "mutated"  # type: ignore[misc]


def test_receipt_predicate_and_causal_eval_present_and_distinct():
    # receipt_predicate is a per-fact name; causal_eval is a paired/counterfactual method.
    preds = {fr.registration(fc).receipt_predicate for fc in fr.all_fact_classes()}
    evals = {fr.registration(fc).causal_eval for fc in fr.all_fact_classes()}
    assert len(preds) == 11  # each fact class has its own receipt predicate name
    # every causal eval is a paired/counterfactual method (§0 gate item 12)
    for fc in fr.all_fact_classes():
        assert fr.registration(fc).causal_eval.startswith("paired_")
    assert len(evals) >= 1
