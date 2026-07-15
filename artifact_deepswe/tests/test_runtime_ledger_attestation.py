"""Pins the seam writer-failure signal consumed by terminal attestation."""
from __future__ import annotations

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
