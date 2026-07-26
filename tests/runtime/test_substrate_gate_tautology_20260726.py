"""CHARACTERIZATION of a known defect: the substrate release-gate cannot fail.

This test does NOT assert desired behaviour.  It pins CURRENT behaviour so the
defect is executable rather than only described in prose, and so that fixing it
is announced by a failing test rather than discovered by accident.

THE DEFECT (measured 2026-07-26, 10/10 features).
``reasoning_runtime`` gates evidence RELEASE on substrate availability::

    preferred_available = set(available_substrates) & contract.preferred_substrates
    if not preferred_available and not fallback_assured:
        -> HELD, reason = PREREQUISITES_PENDING

The seam supplies ``available_substrates`` from
``CanonicalRuntimeAttachment._available_substrates(records)``, whose docstring
claims *"Report only substrates evidenced by the computations that ran"*.  It is
in fact a STATIC per-feature table, and that table is identical to
``_FACT_FALLBACK_POLICIES``' preferred tuples.  So the intersection is non-empty
whenever a record exists: **the gate can never fail**, and
``PREREQUISITES_PENDING`` is unreachable through this path.  It never checks
that LSP ran, that a compiler ran, or that a test executed -- it derives the
answer from the same table it is checked against.

A safety gate that cannot fail is worse than no gate: it reads as prerequisite
verification in review and in the §9 invariant list while verifying nothing.

WHEN THIS TEST FAILS, THAT IS PROGRESS.  A real fix makes producers report the
substrates they actually consulted, at which case a feature whose prerequisite
did not run will report substrates that do NOT cover its contract, and
``test_substrate_gate_is_currently_tautological`` will go RED.  At that point:
delete this file, and add a test asserting the gate HOLDS evidence whose
prerequisites are genuinely absent.

Do NOT "fix" this test by loosening it.  Do NOT treat its passing as healthy.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr


_FACTS = (
    "caller_contract",
    "covering_red",
    "def_partition",
    "localization",
    "newfile_precedent",
    "obligations",
    "recovery",
    "signature_delta",
    "submit_refusal",
    "syntax_result",
)


def _declared(feature_id: str) -> tuple[str, ...]:
    """What the seam reports as 'available' for a single record of this class."""
    record = SimpleNamespace(feature_id=feature_id)
    return tuple(seam.CanonicalRuntimeAttachment._available_substrates([record]))


def _required(feature_id: str) -> tuple[str, ...]:
    return tuple(rr._FACT_FALLBACK_POLICIES[feature_id][0])


@pytest.mark.parametrize("feature_id", _FACTS)
def test_substrate_gate_is_currently_tautological(feature_id: str) -> None:
    """The declared substrates always satisfy the contract's prerequisites.

    RED here means the seam started reporting OBSERVED substrates -- see the
    module docstring, that is the desired end state, not a regression.
    """
    declared = set(_declared(feature_id))
    required = set(_required(feature_id))

    assert declared, f"{feature_id}: seam reported no substrates at all"
    assert declared & required, (
        f"{feature_id}: declared={sorted(declared)} no longer covers "
        f"required={sorted(required)} -- if substrate reporting became "
        f"observation-based, DELETE this file per its docstring"
    )


def test_no_fact_class_is_missing_from_either_table() -> None:
    """Both tables must stay in lockstep on the FACT roster.

    If one grows a class the other lacks, the tautology could silently become a
    partial gate for that class only -- a far more confusing state than either
    a real gate or a fully fake one.
    """
    policy_classes = set(rr._FACT_FALLBACK_POLICIES)

    assert set(_FACTS) <= policy_classes, sorted(set(_FACTS) - policy_classes)
    for feature_id in _FACTS:
        assert _declared(feature_id), f"{feature_id} absent from the seam table"


def test_unknown_feature_reports_no_substrates() -> None:
    """Correct-or-quiet: an unmapped class must not inherit someone else's
    prerequisites. This is the one genuinely sound behaviour in the table."""
    assert _declared("not_a_real_feature_class") == ()
