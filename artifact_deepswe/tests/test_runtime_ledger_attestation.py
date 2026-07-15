"""Pins the seam writer-failure signal consumed by terminal attestation."""
from __future__ import annotations

import json

from artifact_deepswe import ledger_attestation
from artifact_deepswe import gt_mini_patch as seam


def test_direct_ledger_writer_counts_swallowed_io_failure(monkeypatch) -> None:
    before = seam.ledger_write_failures()
    monkeypatch.setattr(seam, "_LEDGER_WRITE_FAILURES", before)

    def fail_open(*_args, **_kwargs):
        raise OSError("fixture sink unavailable")

    monkeypatch.setattr(seam, "open", fail_open, raising=False)
    seam._ledger_line_direct({
        "layer": "fixture", "event_type": "fixture", "file_path": "",
        "outcome": "eligible", "reason": "fixture", "chars_delivered": 0,
        "iteration": 0,
    })

    assert seam.ledger_write_failures() == before + 1


def test_harvested_task_name_requires_a_regenerated_attestation(tmp_path) -> None:
    source = tmp_path / "gt_runtime_ledger.jsonl"
    source.write_text(
        json.dumps({
            "layer": "fixture", "event_type": "fixture", "file_path": "",
            "outcome": "eligible", "reason": "fixture",
            "chars_delivered": 0, "iteration": 0,
        }) + "\n",
        encoding="utf-8",
    )
    source_attestation = tmp_path / "gt_runtime_ledger_attestation.json"
    ledger_attestation.write_attestation(
        source, source_attestation, write_failures=0,
    )
    assert ledger_attestation.validate_attestation(source, source_attestation)

    harvested = tmp_path / "gt_runtime_ledger_task.jsonl"
    harvested.write_bytes(source.read_bytes())
    copied_attestation = tmp_path / "copied_attestation.json"
    copied_attestation.write_bytes(source_attestation.read_bytes())
    assert not ledger_attestation.validate_attestation(
        harvested, copied_attestation,
    )

    harvested_attestation = tmp_path / "gt_runtime_ledger_attestation_task.json"
    source_document = json.loads(source_attestation.read_text(encoding="utf-8"))
    ledger_attestation.write_attestation(
        harvested,
        harvested_attestation,
        write_failures=source_document["write_failures"],
    )
    assert ledger_attestation.validate_attestation(
        harvested, harvested_attestation,
    )
