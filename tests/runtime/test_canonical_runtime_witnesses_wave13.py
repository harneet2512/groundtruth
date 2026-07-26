from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from groundtruth.runtime import evidence_envelope as ee
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.canonical_producers import (
    ProducerContext,
    SubmitEvidenceOwner,
    produce_covering_red,
    produce_recovery,
    produce_submit_refusal,
    produce_syntax_result,
)
from groundtruth.runtime.covering_runner import CoveringAttribution
from groundtruth.runtime.hypothesis_ledger import (
    Advisory,
    D_HYPOTHESIS_FALSIFIED,
    T_EDIT_CONTRADICTED_CONTRACT,
)
from groundtruth.runtime.submit_gate import GateVerdict


REVISION = rr.RevisionVector(
    repository_content="repo-content-13",
    graph="graph-13",
    lsp="lsp-13",
    runtime_evidence="runtime-13",
)
EVENT_HASH = "a" * 64


def _runtime_context(
    witness: ee.CanonicalRuntimeWitness | None = None,
) -> ProducerContext:
    return ProducerContext(
        subject="src/auth/session.py::refresh_session",
        provenance=(),
        revision=REVISION,
        decision_id="decision-13",
        causal_neighborhood=("path:auth-session",),
        runtime_witnesses=(
            witness
            or ee.CanonicalRuntimeWitness.canonical_event(
                event_id="ev-internal-validation-13",
                content_sha256=EVENT_HASH,
            ),
        ),
    )


def _syntax_result() -> dict[str, object]:
    return {
        "verdict": "syntax_error",
        "diagnostic": (
            'File "src/auth/session.py", line 41\n'
            "    if token:\n"
            "             ^\n"
            "SyntaxError: expected expression"
        ),
        "language": ".py",
        "reason": "parse_error",
        "checker": ["ast.parse"],
    }


def test_runtime_event_witness_replaces_fabricated_source_line() -> None:
    envelope = produce_syntax_result(
        context=_runtime_context(),
        result=_syntax_result(),
    )

    assert envelope is not None
    assert envelope.provenance == ()
    assert envelope.runtime_witnesses == (
        ee.CanonicalRuntimeWitness.canonical_event(
            event_id="ev-internal-validation-13",
            content_sha256=EVENT_HASH,
        ),
    )
    assert ee.validate(envelope) == []


def test_runtime_event_witness_converts_only_against_exact_committed_event_hash() -> None:
    envelope = produce_syntax_result(
        context=_runtime_context(),
        result=_syntax_result(),
    )
    assert envelope is not None

    assert rr.canonical_evidence_from_envelope(envelope) is None
    assert rr.canonical_evidence_from_envelope(
        envelope,
        committed_event_hashes={
            "ev-internal-validation-13": "b" * 64,
        },
    ) is None

    record = rr.canonical_evidence_from_envelope(
        envelope,
        committed_event_hashes={
            "ev-internal-validation-13": EVENT_HASH,
        },
    )
    assert record is not None
    assert record.provenance == (
        "event:ev-internal-validation-13:sha256:" + EVENT_HASH,
    )


def test_exact_computation_witness_converts_without_manufactured_source_line() -> None:
    verdict = GateVerdict(
        allow=False,
        reason="covering_test_failed",
        detail="a covering check remains red",
        record={"block": "covering_test_failed"},
    )
    identity = {
        "gate_record": '{"block":"covering_test_failed"}',
        "patch_revision": REVISION.repository_content,
        "reason": verdict.reason,
    }
    witness = ee.CanonicalRuntimeWitness.deterministic_computation(
        computation_id="submit_gate:gate_verdict",
        content_sha256=hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
    )
    envelope = produce_submit_refusal(
        context=_runtime_context(witness),
        verdict=verdict,
        output_owner=SubmitEvidenceOwner.REFUSAL,
    )
    assert envelope is not None

    record = rr.canonical_evidence_from_envelope(envelope)
    assert record is not None
    assert record.provenance == (
        f"computation:submit_gate:gate_verdict:sha256:{witness.content_sha256}",
    )


def test_runtime_witness_is_identity_render_and_serialization_neutral() -> None:
    envelope = produce_syntax_result(
        context=_runtime_context(),
        result=_syntax_result(),
    )
    assert envelope is not None

    without_sidecar = replace(envelope, runtime_witnesses=())
    assert envelope == without_sidecar
    assert hash(envelope) == hash(without_sidecar)
    assert envelope.dedup_key == without_sidecar.dedup_key
    assert ee.render_bytes(envelope) == ee.render_bytes(without_sidecar)
    assert ee.to_dict(envelope) == ee.to_dict(without_sidecar)
    assert "runtime_witnesses" not in ee.to_dict(envelope)
    assert ee.validate(without_sidecar) == [
        "tier: VERIFIED requires nonempty provenance or a canonical runtime witness"
    ]


def test_runtime_witnesses_fail_closed_on_missing_or_malformed_proof() -> None:
    with pytest.raises(ValueError, match="source provenance or canonical runtime"):
        ProducerContext(
            subject="src/auth/session.py::refresh_session",
            provenance=(),
            revision=REVISION,
            decision_id="decision-13",
            causal_neighborhood=("path:auth-session",),
        )

    with pytest.raises(ValueError, match="content_sha256"):
        ee.CanonicalRuntimeWitness.canonical_event(
            event_id="ev-13",
            content_sha256="not-a-sha256",
        )

    with pytest.raises(ValueError, match="source line"):
        ee.CanonicalRuntimeWitness.diagnostic_location(
            checker_id="ast.parse",
            diagnostic_sha256="c" * 64,
            source_path="src/auth/session.py",
            source_line=0,
        )


def test_diagnostic_and_gate_computation_witnesses_are_exact() -> None:
    syntax = _syntax_result()
    diagnostic_text = syntax["diagnostic"]
    assert isinstance(diagnostic_text, str)
    diagnostic = ee.CanonicalRuntimeWitness.diagnostic_location(
        checker_id="ast.parse",
        diagnostic_sha256=hashlib.sha256(
            diagnostic_text.encode("utf-8")
        ).hexdigest(),
        source_path="src/auth/session.py",
        source_line=41,
        source_column=13,
    )
    assert ee.runtime_witness_violations(diagnostic) == []
    syntax_envelope = produce_syntax_result(
        context=_runtime_context(diagnostic),
        result=syntax,
    )
    assert syntax_envelope is not None
    assert syntax_envelope.runtime_witnesses == (diagnostic,)

    verdict = GateVerdict(
        allow=False,
        reason="covering_test_failed",
        detail="a covering check remains red",
        record={"block": "covering_test_failed"},
    )
    identity = {
        "gate_record": '{"block":"covering_test_failed"}',
        "patch_revision": REVISION.repository_content,
        "reason": verdict.reason,
    }
    gate = ee.CanonicalRuntimeWitness.deterministic_computation(
        computation_id="submit_gate:gate_verdict",
        content_sha256=hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
    )
    envelope = produce_submit_refusal(
        context=_runtime_context(gate),
        verdict=verdict,
        output_owner=SubmitEvidenceOwner.REFUSAL,
    )
    assert envelope is not None
    assert envelope.runtime_witnesses == (gate,)
    assert ee.validate(envelope) == []

    wrong_gate = replace(gate, content_sha256="b" * 64)
    assert produce_submit_refusal(
        context=_runtime_context(wrong_gate),
        verdict=verdict,
        output_owner=SubmitEvidenceOwner.REFUSAL,
    ) is None


def test_runtime_witness_cannot_rescue_crossed_source_or_diagnostic_location() -> None:
    crossed = ProducerContext(
        subject="src/auth/session.py::refresh_session",
        provenance=(("src/auth/other.py", 12),),
        revision=REVISION,
        decision_id="decision-13",
        causal_neighborhood=("path:auth-session",),
        runtime_witnesses=(
            ee.CanonicalRuntimeWitness.canonical_event(
                event_id="ev-internal-validation-13",
                content_sha256=EVENT_HASH,
            ),
        ),
    )
    assert produce_syntax_result(
        context=crossed,
        result=_syntax_result(),
    ) is None

    with pytest.raises(ValueError, match="locate the producer subject"):
        _runtime_context(
            ee.CanonicalRuntimeWitness.diagnostic_location(
                checker_id="ast.parse",
                diagnostic_sha256="c" * 64,
                source_path="src/auth/other.py",
                source_line=12,
            )
        )


def test_covering_and_recovery_accept_exact_runtime_event_witness() -> None:
    context = _runtime_context()
    covering = produce_covering_red(
        context=context,
        result={
            "executed": True,
            "verdict": "fail",
            "reason": "test_failure",
            "files": ["tests/auth/test_session.py"],
            "ran": ["tests/auth/test_session.py"],
            "command": ["pytest", "-q", "tests/auth/test_session.py"],
            "stdout_tail": "1 failed",
            "stderr_tail": "",
            "exit_code": 1,
        },
        attribution=CoveringAttribution(
            attributed=True,
            method="unresolved_covering",
            current_verdict="fail",
            base_verdict="fail",
            implicated_edited_paths=("src/auth/session.py",),
            covering_files=("tests/auth/test_session.py",),
        ),
    )
    recovery = produce_recovery(
        context=context,
        advisory=Advisory(
            transition=T_EDIT_CONTRADICTED_CONTRACT,
            disposition=D_HYPOTHESIS_FALSIFIED,
            tier=ee.WARNING,
            blocking_eligibility=ee.ADVISORY,
            statement="the same failure recurred after the current edit.",
            evidence_ids=("event:ev-internal-validation-13",),
        ),
    )

    assert covering is not None
    assert recovery is not None
    assert covering.provenance == recovery.provenance == ()
    assert covering.runtime_witnesses == recovery.runtime_witnesses
    assert ee.validate(covering) == ee.validate(recovery) == []
