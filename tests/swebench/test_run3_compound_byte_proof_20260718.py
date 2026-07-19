"""Run-3 R1 regressions: compound brief bytes require one exact block identity."""
from __future__ import annotations

import hashlib
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gt_feature_metrics as metrics  # noqa: E402
from chronology_extract import extract_block_chronologies  # noqa: E402
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    build_lineage,
    lineage_ledger_extra,
    lineage_to_dict,
)


def _seal(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _lineage(evidence_type: str, producer: str) -> dict:
    lineage = build_lineage(
        runtime_producer_id=producer,
        evidence_type=evidence_type,
        actual_event="task_start",
    )
    assert lineage is not None
    return lineage_to_dict(lineage)


def _block(
    payload: str,
    *,
    start: int,
    block_id: str = "obligations:0",
    evidence_type: str = "obligations",
    producer: str = "spec",
    fact_class: str = "obligations",
) -> dict:
    return {
        "block_id": block_id,
        "label": "obligations",
        "candidate_id": f"candidate:{block_id}",
        "char_span": [start, start + len(payload)],
        "chars_delivered": len(payload),
        "content_sha256_16": _seal(payload),
        "declared_fact_class": fact_class,
        "lineage_status": "REGISTERED",
        "lineage": _lineage(evidence_type, producer),
    }


def _fixture(
    brief: str,
    blocks: list[dict],
) -> tuple[dict, list[dict]]:
    return (
        {"messages": [{"role": "user", "content": brief}]},
        [{
            "layer": "brief.task",
            "event_type": "task_start",
            "file_path": "",
            "outcome": "delivered",
            "reason": "step0_brief_prepend",
            "chars_delivered": len(brief),
            "iteration": 0,
            "content_sha256_16": _seal(brief),
            "compound_delivery": True,
            "compound_lineage_schema": "gt.compound_feature_lineage.v1",
            "block_lineage": blocks,
        }],
    )


def test_compound_byte_proof_binds_declared_span_seal_id_parent_and_lineage() -> None:
    payload = "OBLIGATIONS:\n- preserve the public contract\n"
    trajectory, rows = _fixture(payload, [_block(payload, start=0)])

    chronologies = extract_block_chronologies(trajectory, rows)

    assert len(chronologies) == 1
    chron = chronologies[0]
    assert chron.block_id == "obligations:0"
    assert chron.block_candidate_id == "candidate:obligations:0"
    assert chron.block_char_span == (0, len(payload))
    assert chron.block_chars_delivered == len(payload)
    assert chron.delivery_seal == _seal(payload)
    assert chron.block_lineage_validated is True
    assert chron.parent_physical_id
    assert chron.physical_id == (
        f"{chron.parent_physical_id}:block:0:{len(payload)}:obligations:0"
    )
    assert metrics._block_delivery_byte_proofs(rows, chronologies) == {
        "obligations"
    }


def test_duplicate_bytes_use_declared_span_not_first_equal_window() -> None:
    # BITING MUTATION R1-A: restoring ``spans[0]`` makes this chronology claim offset 0.
    payload = "OBLIGATIONS:\n- preserve behavior\n"
    brief = payload + payload
    trajectory, rows = _fixture(
        brief, [_block(payload, start=len(payload))]
    )

    chron = extract_block_chronologies(trajectory, rows)[0]

    assert chron.block_char_span == (len(payload), len(brief))
    assert f":block:{len(payload)}:{len(brief)}:" in str(chron.physical_id)


def test_forged_span_and_duplicate_block_identity_fail_closed() -> None:
    # BITING MUTATION R1-B: ignoring declared char_span would seat the forged first case.
    payload = "OBLIGATIONS:\n- preserve behavior\n"
    trajectory, rows = _fixture(payload, [_block(payload, start=0)])
    rows[0]["block_lineage"][0]["char_span"] = [1, len(payload)]
    assert extract_block_chronologies(trajectory, rows) == []

    trajectory, rows = _fixture(
        payload,
        [
            _block(payload, start=0, block_id="duplicate"),
            _block(payload, start=0, block_id="duplicate"),
        ],
    )
    assert extract_block_chronologies(trajectory, rows) == []

    trajectory, rows = _fixture(
        payload,
        [
            _block(payload, start=0, block_id="first"),
            _block(payload, start=0, block_id="second"),
        ],
    )
    assert extract_block_chronologies(trajectory, rows) == []


def test_byte_proof_consumer_rechecks_chronology_identity() -> None:
    payload = "OBLIGATIONS:\n- preserve behavior\n"
    trajectory, rows = _fixture(payload, [_block(payload, start=0)])
    chron = extract_block_chronologies(trajectory, rows)[0]

    for forged in (
        replace(chron, block_char_span=(1, len(payload))),
        replace(chron, block_chars_delivered=len(payload) - 1),
        replace(chron, delivery_seal="f" * 16),
        replace(chron, parent_physical_id="m999:0:1"),
        replace(chron, block_id="other"),
        replace(chron, block_candidate_id="forged-candidate"),
        replace(chron, block_lineage_validated=False),
    ):
        assert metrics._block_delivery_byte_proofs(rows, [forged]) == frozenset()


def test_registered_block_with_forged_typed_lineage_fails_closed() -> None:
    payload = "OBLIGATIONS:\n- preserve behavior\n"
    trajectory, rows = _fixture(payload, [_block(payload, start=0)])
    # A REAL block chronology from the VALID rows — the consumer's actual join input (not []).
    chronologies = extract_block_chronologies(trajectory, rows)
    assert metrics._block_delivery_byte_proofs(rows, chronologies) == {"obligations"}

    # Forge the block's TYPED LINEAGE to a producer the registry does NOT authorize for
    # obligations (``spec``), then re-run the CONSUMER with the SAME real chronology. The forged
    # block therefore flows THROUGH a real chronology; it is the consumer's OWN lineage
    # re-validation (``validate_block_lineage`` in ``_block_delivery_byte_proofs``) that must
    # reject it — never merely a vacuous empty-chronology pass.
    forged = deepcopy(rows[0]["block_lineage"][0]["lineage"])
    forged["registered_producer_id"] = "v1r_brief"
    rows[0]["block_lineage"][0]["lineage"] = forged
    assert metrics._block_delivery_byte_proofs(rows, chronologies) == frozenset()
    # defense in depth: a FRESH extraction over the forged rows also rejects the forgery.
    assert extract_block_chronologies(trajectory, rows) == []
    # MUTATION[drop the `typed = validate_block_lineage(block)` recheck / its `typed is None` guard
    # in _block_delivery_byte_proofs] -> the forged block byte-proves "obligations" again against the
    # valid chronology -> this test goes RED.


def test_mutated_block_candidate_identity_cannot_reuse_old_chronology() -> None:
    payload = "OBLIGATIONS:\n- preserve behavior\n"
    trajectory, rows = _fixture(payload, [_block(payload, start=0)])
    chronologies = extract_block_chronologies(trajectory, rows)
    assert metrics._block_delivery_byte_proofs(rows, chronologies) == {
        "obligations"
    }

    rows[0]["block_lineage"][0]["candidate_id"] = "forged-candidate"
    assert metrics._block_delivery_byte_proofs(rows, chronologies) == frozenset()


def test_nested_evidence_type_controls_timing_identity_not_declared_class() -> None:
    payload = "LOCALIZATION:\n1. src/example.py\n"
    trajectory, rows = _fixture(
        payload,
        [_block(
            payload,
            start=0,
            block_id="localization:0",
            evidence_type="brief_localization",
            producer="v1r_brief",
            fact_class="localization",
        )],
    )

    chron = extract_block_chronologies(trajectory, rows)[0]

    assert chron.evidence_type == "brief_localization"
    assert chron.fact_class == "localization"
    assert metrics._block_delivery_byte_proofs(rows, [chron]) == {"localization"}


def test_single_fact_byte_proof_path_is_unchanged() -> None:
    payload = "OBLIGATIONS:\n- preserve behavior\n"
    lineage = build_lineage(
        runtime_producer_id="spec",
        evidence_type="obligations",
        actual_event="task_start",
    )
    assert lineage is not None
    row = {
        "layer": "obligations",
        "outcome": "delivered",
        "chars_delivered": len(payload),
        "content_sha256_16": _seal(payload),
        **lineage_ledger_extra(lineage),
    }
    consumption = {
        "entries": [{
            "source": "trajectory",
            "joined": True,
            "join_method": "seal",
            "content_sha256_16": _seal(payload),
            "ledger_chars": len(payload),
            "ledger_layer": "obligations",
        }]
    }

    assert metrics._fact_delivery_byte_proven(
        "obligations", [row], consumption
    ) is True
