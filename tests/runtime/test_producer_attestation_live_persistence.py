from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from groundtruth.runtime.evidence_envelope import EvidenceEnvelope
from groundtruth.runtime.lane_attestation import lane_delivery_candidate_id
from groundtruth.runtime.producer_inputs import (
    PRODUCER_INPUTS_SCHEMA,
    CallerEvidenceRow,
    ProducerInputs,
    SignatureChange,
    SourceState,
)
from groundtruth.runtime.syntax_observation import build_syntax_observation


ARTIFACT_DIR = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
if ARTIFACT_DIR not in sys.path:
    sys.path.insert(0, ARTIFACT_DIR)
import gt_mini_patch as gmp  # noqa: E402


def _stored_attestation(root: Path) -> dict:
    bundles = list((root / "producer_attestations" / "index").iterdir())
    assert len(bundles) == 1
    return json.loads((bundles[0] / "attestation.json").read_text(encoding="utf-8"))


def test_exact_delivered_syntax_winner_persists_pass_attestation(
    tmp_path, monkeypatch,
) -> None:
    source_path = tmp_path / "src/mod.py"
    source_path.parent.mkdir(parents=True)
    source = b"def f(\n"
    source_path.write_bytes(source)
    block = 'File "src/mod.py", line 1\n    def f(\n         ^\nSyntaxError: never closed'
    shipped = "\n" + block
    observation = build_syntax_observation(
        file_path="src/mod.py",
        source_bytes=source,
        check_result={
            "verdict": "syntax_error", "diagnostic": block, "language": ".py",
            "reason": "parse_error", "checker": ["ast.parse"],
        },
        actual_event="edit_result",
        rendered_block=block,
    )
    monkeypatch.setenv("GT_C_OUT", str(tmp_path))
    monkeypatch.setattr(gmp, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(gmp, "_last_edit_syntax_observation", observation)
    candidate_id = lane_delivery_candidate_id("edit.syntax", "src/mod.py", shipped)
    seal = hashlib.sha256(shipped.encode()).hexdigest()[:16]

    gmp._persist_lane_producer_attestation(
        "edit.syntax", "src/mod.py", block, shipped, candidate_id, seal
    )

    stored = _stored_attestation(tmp_path)
    assert stored["candidate_id"] == candidate_id
    assert stored["delivery_seal"] == seal
    assert stored["truth_verdict"] == "PASS"
    assert stored["freshness_verdict"] == "PASS"


def _state(file: str, token: str) -> SourceState:
    return SourceState(file, token * 64, f"source:{token * 64}")


def test_exact_delivered_gateway_winner_persists_pass_attestation(
    tmp_path, monkeypatch,
) -> None:
    envelope = EvidenceEnvelope.build(
        producer="caller_contract", fact_id="f", target="src/api.py",
        evidence_type="caller_break", payload=("caller contract",),
        provenance=(("src/caller.py", 2),), graph_revision="graph-1",
    )
    inputs = ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA,
        evidence_type="caller_break",
        candidate_id=envelope.dedup_key,
        before_state=_state("src/api.py", "a"),
        after_state=_state("src/api.py", "b"),
        caller_rows=(CallerEvidenceRow(
            identity="use", file="src/caller.py", line=2, confidence=0.95,
            resolution_method="import", source_state=_state("src/caller.py", "c"),
            edge_id=7, definition_id=3,
        ),),
        graph_revision="graph-1",
        signature_changes=(SignatureChange(
            symbol="f", edited_file="src/api.py",
            before_parameters=("x",), after_parameters=("x", "y"),
            old_min_params=None, old_max_params=None,
            new_min_params=None, new_max_params=None, positional_args=None,
        ),),
    )
    envelope = replace(envelope, producer_inputs=inputs)
    shipped = "\ncaller contract"
    seal = hashlib.sha256(shipped.encode()).hexdigest()
    monkeypatch.setenv("GT_C_OUT", str(tmp_path))

    gmp._persist_gateway_producer_attestation(
        envelope, shipped, SimpleNamespace(rendered_bytes_hash=seal)
    )

    stored = _stored_attestation(tmp_path)
    assert stored["candidate_id"] == envelope.dedup_key
    assert stored["delivery_seal"] == seal[:16]
    assert stored["truth_verdict"] == "PASS"
    assert stored["freshness_verdict"] == "PASS"
