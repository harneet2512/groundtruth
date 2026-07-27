"""A DECISION_INCOMPLETE row must name the role it could not fill.

WHAT WENT WRONG.  The compilation ledger row recorded `held_evidence` and
`suppressed_decisions` and nothing else. `held_evidence` is
`len(plan.held_evidence_ids)`, and on the LIVE path that is the evidence the oracle
DELIBERATELY DEFERRED -- not the size of the pool it considered. (Only the DISABLED plan
path sets it to the whole store.)

So `held_evidence == 0` means "the oracle withheld nothing". It does NOT mean "there was no
evidence". Those are opposite diagnoses with opposite fixes -- an empty pool is an upstream
producer bug, a full pool that still fails decision-completeness is an anchor/role bug -- and
the row could not tell them apart. On run 30246661710 all 204 compile attempts came back
DECISION_INCOMPLETE and the field was 0 on ~all of them; that was read as "no evidence
existed", which the field cannot support.

WHAT ACTUALLY ANSWERS IT.  `OracleDecision.unresolved_roles` names the required role the
coalition could not fill, and the coalition/pool sizes say whether anything was available to
fill it. None of that was ledgered, which is why the run's own artifacts cannot explain its
own silence.

This is deliberately telemetry-only: `chars=0`, `outcome=suppressed_internal_only`, never
model-facing, and it changes no delivery decision. The bar it protects is the one the seam
comment already states -- "a SILENT non-delivery is indistinguishable downstream from 'GT had
nothing to say'" -- which is exactly the ambiguity that kept 0/17 unfalsifiable.

DO NOT "fix" a failure here by widening `held_evidence`'s meaning or by deriving a pool size
from it. Read the fields that exist.
"""

from __future__ import annotations

import inspect

from artifact_deepswe import gt_mini_patch as seam


def _compilation_ledger_source() -> str:
    """The exact block that writes the canonical_runtime.compilation row.

    Sliced STRUCTURALLY -- from the enclosing `try:` to the `except` that closes it --
    rather than by a magic character count. A fixed window silently stops covering the
    block the moment the block grows, which turns these assertions into false failures (or,
    worse, false passes if the window slid the other way).
    """
    src = inspect.getsource(seam)
    idx = src.index('kind="canonical_runtime.compilation"')
    start = src.rindex("try:", 0, idx)
    end = src.index("except Exception", idx)
    return src[start:end]


def test_row_names_the_unresolved_role():
    """The whole point: WHICH required role went unfilled."""
    block = _compilation_ledger_source()
    assert "unresolved_roles" in block, (
        "a DECISION_INCOMPLETE row that does not name the unfilled role cannot explain the "
        "oracle's silence -- which is the ambiguity this row exists to remove"
    )


def test_row_records_pool_size_separately_from_deferred_count():
    """`held_evidence` counts DEFERRED items. Distinguishing 'no evidence existed' from
    'evidence existed and still did not compose' needs the POOL, and they must be separate
    fields -- one cannot be derived from the other."""
    block = _compilation_ledger_source()
    assert "evidence_store" in block, (
        "no pool-size field: an empty pool (upstream producer bug) is still "
        "indistinguishable from a full pool that failed decision-completeness (anchor bug)"
    )
    assert "held_evidence" in block, "the deferred count must stay -- it is a real signal"


def test_row_records_what_the_coalition_managed_to_gather():
    """Coalition size closes the loop: 0 with a non-empty pool means nothing was eligible;
    >0 means it gathered members and still missed a required role."""
    assert "coalition_size" in _compilation_ledger_source()


def test_telemetry_only_never_model_facing():
    """ANTI-REGRESSION. This row must never ship bytes or change a delivery decision."""
    block = _compilation_ledger_source()
    assert "chars=0" in block
    assert 'outcome="suppressed_internal_only"' in block


def test_the_writer_is_still_fully_self_guarded():
    """Telemetry must never break the agent: the whole block stays inside try/except.

    The slice ENDS at the `except`, so assert the guard from the full source instead of
    from the slice -- otherwise this test can only ever fail.
    """
    src = inspect.getsource(seam)
    idx = src.index('kind="canonical_runtime.compilation"')
    tail = src[idx : idx + 6000]
    assert "except Exception" in tail, "the telemetry write is no longer guarded"
    assert "telemetry never blocks" in tail


def test_unresolved_roles_is_a_real_field_on_the_oracle_decision():
    """POSITIVE CONTROL. If this attribute does not exist, the row above would silently
    record nothing and the test suite would still be green -- the exact failure mode that
    produced the retracted classification."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from groundtruth.runtime.reasoning_runtime import OracleDecision

    assert "unresolved_roles" in OracleDecision.__dataclass_fields__, (
        "OracleDecision has no unresolved_roles field -- the ledger change would be "
        "dead-by-construction"
    )
