"""One candidate's identity must not outlive its own delivery.

MEASURED 2026-07-28, and the cause of four live regressions.

``_ensure_observation_binding`` PUBLISHES the binding it derives onto the
``_delivery_observation_context`` ContextVar.  That publish is required: the function's
own docstring records that ``_runtime_ledger_record`` reads the var directly, so a
DELIVERED ledger row carries ``observation_binding`` only when it is set -- "both halves
or neither", the whole point of Wave 1 Step 3.

The defect was that the publish had NO token and NO reset, while every other publisher in
``gt_mini_patch.py`` is token-bounded (``17842``/``17917``, ``18010``/``18022``,
``22253``/``22284``).  That asymmetry WAS the bug.

WHY IT IS PRODUCTION HARM, not a test artifact.  The published value is CANDIDATE-scoped
(``candidate_id`` + ``opportunity_id``), but two consumers read the bare var as though it
were OBSERVATION-scoped, with no candidate to check it against:

  * ``_runtime_ledger_record`` does ``_row["candidate_id"] = binding["candidate_id"]`` --
    it OVERWRITES the join key every downstream reader depends on. Two unrelated
    ``ss.coherence`` proof rows were measured carrying one candidate's ``opportunity_id``.
  * ``_control_participation_record`` raises ``control candidate_id disagrees with
    observation binding``, degrading the row to ``outcome:"measurement_failed"`` /
    participation ``ERROR``. ``gateway._record_control`` never passes
    ``allow_candidate_mismatch``, so every gateway control decision takes that strict path.

Inside a multi-dose observation -- a real state, "6 of 20" on run 30390877219 with the
arbiter off -- seal #1's identity was therefore stamped onto later seals and onto control
rows belonging to DIFFERENT candidates.

THE SCOPE IS "ONE DELIVERY".  Not one seal: the CALLER writes the DELIVERED ledger row
AFTER the seal returns and legitimately needs the binding alive for it, so clearing on
seal-exit would silently undo Step 3.  Not one observation: that is the leaky status quo.
The correct unit is the function performing a single delivery, which contains both the
seal call and the ``_runtime_ledger_record`` that follows it.

Two consumer-side repairs were tried FIRST and both were wrong; this file exists partly so
neither is re-attempted:

  * making ``_control_participation_record`` treat an AMBIENT mismatch as "not mine" broke
    three CLASS-6 tests that deliberately pin that mismatch AS a typed error;
  * making the ledger stamp use ``setdefault`` cannot work, because no caller passes
    ``candidate_id`` into that writer -- the binding is its SOLE source, so requiring prior
    agreement would strip DELIVERED rows of their binding entirely.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime.evidence_envelope import build_observation_binding  # noqa: E402


def _binding(candidate_id: str, ordinal: int = 0):
    return build_observation_binding(
        batch_start_iteration=0,
        parent_policy_sha256="a" * 64,
        parent_policy_chars=5,
        action_batch_sha256="b" * 64,
        candidate_ordinal=ordinal,
        candidate_kind="recovery",
        candidate_id=candidate_id,
    )


@pytest.fixture(autouse=True)
def _clean_context():
    """These assertions are about a leak, so they must not inherit one."""
    g._delivery_observation_context.set(None)
    yield
    g._delivery_observation_context.set(None)


def test_a_publish_inside_a_delivery_does_not_survive_it():
    """THE BUG. A delivery that publishes must leave the var as it found it."""
    @g._delivery_scoped_binding
    def _one_delivery():
        g._delivery_observation_context.set(_binding("CANDIDATE-A"))
        # ...the DELIVERED ledger row is written HERE, inside the scope, and must see it.
        assert g._current_observation_binding() is not None
        return "delivered"

    assert g._current_observation_binding() is None
    assert _one_delivery() == "delivered"
    assert g._current_observation_binding() is None, (
        "candidate A's identity outlived its delivery and is now ambient for every "
        "later seal, control row and ledger row in this observation"
    )


def test_two_deliveries_in_one_observation_do_not_cross_contaminate():
    """The production shape: a multi-dose observation with the arbiter off."""
    seen: list = []

    @g._delivery_scoped_binding
    def _deliver(candidate_id):
        g._delivery_observation_context.set(_binding(candidate_id))
        seen.append(g._current_observation_binding().candidate_id)

    _deliver("CANDIDATE-A")
    inherited = g._current_observation_binding()
    _deliver("CANDIDATE-B")

    assert inherited is None, "delivery B began with A's identity already published"
    assert len(set(seen)) == 2, seen


def test_an_outer_publish_is_RESTORED_not_destroyed():
    """NEAR-NEGATIVE, and the reason this is snapshot-and-restore rather than clear-on-exit.

    The batch path publishes a binding around a whole commit (``17842``/``17917``) and
    nested delivery work must not blow it away -- only its OWN publish is discarded.
    """
    outer = _binding("OUTER-CANDIDATE")
    g._delivery_observation_context.set(outer)

    @g._delivery_scoped_binding
    def _inner():
        g._delivery_observation_context.set(_binding("INNER-CANDIDATE"))

    _inner()
    assert g._current_observation_binding() is outer


def test_the_scope_holds_when_the_delivery_RAISES():
    """A fault must not leak an identity either -- try/finally, not a trailing clear."""
    @g._delivery_scoped_binding
    def _boom():
        g._delivery_observation_context.set(_binding("CANDIDATE-A"))
        raise RuntimeError("seal fault")

    with pytest.raises(RuntimeError, match="seal fault"):
        _boom()
    assert g._current_observation_binding() is None


def test_every_delivery_unit_that_can_publish_is_actually_decorated():
    """ANTI-DRIFT. The fix is 8 decorations; a 9th publisher added later must not be missed.

    Checked on the real module objects rather than by reading source, so a decoration that
    is present but shadowed by a later rebinding still fails.
    """
    for name in (
        "_lane_a_deliver", "_commit_prepared_lane", "_gt_gateway_pool_envelope",
        "_deliver_gate_winner", "_commit_prepared_steer", "_record_shadow_assignment",
    ):
        fn = getattr(g, name)
        assert getattr(fn, "__wrapped__", None) is not None, (
            f"{name} performs a delivery but is not @_delivery_scoped_binding -- any "
            f"binding it publishes will leak to the rest of the observation"
        )
