"""RED-first: a derivable SELF_LOCALIZED gate must NOT be dropped to None by a co-present
mechanism-only / enrichment verdict.

DEFECT (fair_probe_result.join_fair_probes gate rollup). ``per_fact_class[fc]["fair_probe"]`` was
derived as ``_verdict_to_bool(verdict)`` over the SINGLE reported ``verdict`` headline, and that
headline is picked by ``_VERDICT_RANK`` where the gate-``None`` verdicts ``CAUSAL_FORK`` (rank 5)
and ``CAUSAL_PAIRED`` (rank 3) out-rank ``SELF_LOCALIZED`` (rank 2). So when a SAFETY-excluded fact
class (``submit_refusal`` / ``syntax_result``) had BOTH a delivery whose chronology proves
``SELF_LOCALIZED`` (native acquisition strictly before delivery -> the honest non-causal gate value
``False``) AND a mechanism-only ``CAUSAL_FORK`` (a pure sealed-checkpoint re-derivation, gate-None
by B5), the merged headline was ``CAUSAL_FORK`` and the gate fell through to ``None`` — a DERIVABLE
``SELF_LOCALIZED`` verdict silently dropped. The join carried the evidence; the binder dropped it.

FIX. The gate is rolled up SEPARATELY (``_merge_gate``) by strength of PROOF — ``CAUSAL`` (True) >
``SELF_LOCALIZED`` (False) > None — so a non-gating verdict (which contributes only None) can never
mask a co-present derivable behavioural gate. This never mints a True without a ``CAUSAL``
adjudication (no bar weakening); it only stops a derivable verdict being dropped to None.

Fixtures are faithful run-4 row shapes: ``submit_refusal`` is a run-4 fact class; the self-localized
chronology (native < delivery) is the exact shape run-4 emits for a self-acquired delivery (see e.g.
conan-io__conan-17092 obligations deliv=147/native=52); the sealed fork checkpoint/citation/producer
inputs are the exact B4 shapes ``_safety_fork_probes`` consumes. The masking scenario itself does not
appear in the run-4 SMOKE artifacts only because that run carried no safety-fork input
(fork-config-off) — this reconstruction supplies that one caller input and holds every row shape
faithful, so it isolates the pure gate-rollup defect.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from groundtruth.runtime.chronological_adjudication import (  # noqa: E402
    SELF_LOCALIZED,
    UNMEASURED,
    Chronology,
    adjudicate,
)
from chronology_extract import ExtractedChronology  # noqa: E402
from fair_probe_result import (  # noqa: E402
    CAUSAL_FORK,
    _merge_gate,
    fair_probe_bool_by_fact_class,
    join_fair_probes,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _self_localized_submit_refusal_chronology() -> ExtractedChronology:
    """A delivered ``submit_refusal`` row whose chronology proves the agent self-acquired the fact
    BEFORE GT delivered it (native_acquisition_index < delivery_index) -> adjudicate SELF_LOCALIZED
    -> the honest behavioural gate value is False."""
    ec = ExtractedChronology(
        ledger_row_index=0,
        evidence_type="submit_refusal",
        fact_class="submit_refusal",
        delivery_seal="a" * 16,
        actual_event="submit_result",
        chronology=Chronology(
            decision_open_index=1,
            delivery_index=6,
            decision_commit_index=8,
            native_acquisition_index=2,  # strictly before delivery_index=6 -> SELF_LOCALIZED
            acknowledgment_index=None,
            action_index=None,
        ),
        timing_verdict=UNMEASURED,
        unmeasured_reason=None,
    )
    # guard: the chronology alone is a derivable SELF_LOCALIZED (the fixture's premise).
    assert (
        adjudicate(
            evidence_type=ec.evidence_type,
            actual_event=ec.actual_event,
            delivery_seal=ec.delivery_seal,
            chronology=ec.chronology,
            matched_probe=None,
        ).fair_probe_verdict
        == SELF_LOCALIZED
    )
    return ec


def _submit_refusal_fork() -> dict:
    """A valid B4 safety-fork for ``submit_refusal``: the GT covering candidate is present in the
    treatment arm (covering fails -> BLOCK) and withheld in the control arm (-> ALLOW), so the pure
    re-derivation over the sealed checkpoint mints CAUSAL_FORK (mechanism evidence, gate-None)."""
    obs = 3
    prefix_hash = "p" * 64
    repo_state_hash = "r" * 64
    checkpoint_id = hashlib.sha256(
        _canonical([obs, prefix_hash, repo_state_hash])
    ).hexdigest()
    return {
        "fact_class": "submit_refusal",
        "delivery_seal": "b" * 16,
        "checkpoint": {
            "observation_id": obs,
            "prefix_hash": prefix_hash,
            "repo_state_hash": repo_state_hash,
            "checkpoint_id": checkpoint_id,
        },
        "citation": {"observation_id": obs, "char_span": [0, 10]},
        "producer_inputs": {
            "treatment": {"covering": {"verdict": "fail"}},  # GT covering present -> BLOCK
            "control": {"covering": None},  # GT covering withheld -> ALLOW
        },
    }


def test_causal_fork_does_not_mask_derivable_self_localized_gate() -> None:
    """The join must surface the derivable SELF_LOCALIZED gate (False) even when a mechanism-only
    CAUSAL_FORK is the reported headline verdict for the same class. RED before the gate-rollup
    fix (gate was None); GREEN after."""
    chron = {0: _self_localized_submit_refusal_chronology()}
    join = join_fair_probes(
        {"messages": []},
        [],
        chronologies=chron,
        safety_forks=[_submit_refusal_fork()],
    )
    info = join["per_fact_class"]["submit_refusal"]
    # the fork is still REPORTED as the headline verdict (reporting precedence unchanged) ...
    assert info["verdict"] == CAUSAL_FORK
    assert info["causal_fork_probes"] == 1
    # ... but the behavioural GATE is the honest, derivable SELF_LOCALIZED value, NOT dropped to
    # None by the gate-None fork.
    assert info["fair_probe"] is False
    assert fair_probe_bool_by_fact_class(join)["submit_refusal"] is False


def test_fork_alone_stays_none_gate_no_fabricated_true() -> None:
    """A safety fork with NO co-present self-localized/causal delivery stays gate None (mechanism
    evidence never sets the behavioural gate). Guards against the fix fabricating a value."""
    join = join_fair_probes(
        {"messages": []},
        [],
        chronologies={},
        safety_forks=[_submit_refusal_fork()],
    )
    info = join["per_fact_class"]["submit_refusal"]
    assert info["verdict"] == CAUSAL_FORK
    assert info["fair_probe"] is None
    assert fair_probe_bool_by_fact_class(join)["submit_refusal"] is None


def test_self_localized_alone_binds_false() -> None:
    """A self-localized delivery with no probe binds the gate to the honest False (regression lock
    on the already-correct path)."""
    join = join_fair_probes(
        {"messages": []},
        [],
        chronologies={0: _self_localized_submit_refusal_chronology()},
    )
    info = join["per_fact_class"]["submit_refusal"]
    assert info["verdict"] == SELF_LOCALIZED
    assert info["fair_probe"] is False


def test_merge_gate_precedence() -> None:
    """The gate rollup is by strength of proof: True > False > None; a None contribution (a
    mechanism-only / enrichment verdict) never masks a concrete bool."""
    assert _merge_gate(None, None) is None
    assert _merge_gate(None, False) is False
    assert _merge_gate(False, None) is False  # <- the masking guard: None never erases False
    assert _merge_gate(None, True) is True
    assert _merge_gate(True, None) is True
    assert _merge_gate(False, True) is True
    assert _merge_gate(True, False) is True
