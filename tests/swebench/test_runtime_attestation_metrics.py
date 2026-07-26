"""Metrics integration for canonical provider-terminal proof metadata."""

from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_feature_metrics as metrics  # noqa: E402  # pyright: ignore[reportMissingImports]


def test_absent_canonical_journal_preserves_legacy_integrity_bytes(
    tmp_path,
) -> None:
    integrity = {
        "required_inputs_complete": True,
        "missing_required_inputs": [],
        "legacy": {"unchanged": True},
    }
    before = copy.deepcopy(integrity)

    metrics._apply_canonical_runtime_attestation(str(tmp_path), integrity)

    assert integrity == before


def test_corrupt_canonical_journal_taints_integrity_without_delivery_credit(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        metrics,
        "runtime_attestation_diagnostic",
        lambda _task_dir: {
            "schema": "gt.runtime_attestation.v1",
            "journal_present": True,
            "integrity_ok": False,
            "delivered_count": 0,
            "response_committed_count": 0,
            "records": [],
            "rejected": [
                {
                    "delivery_attempt_id": "delivery:call-1",
                    "reason": "DELIVERY_STATE_HASH_INVALID",
                }
            ],
            "explicit_acknowledgment": "UNMEASURED",
            "behavioral_influence": "UNMEASURED",
        },
    )
    integrity = {
        "required_inputs_complete": True,
        "missing_required_inputs": [],
    }

    metrics._apply_canonical_runtime_attestation(str(tmp_path), integrity)

    assert integrity["required_inputs_complete"] is False
    assert integrity["missing_required_inputs"] == [
        "canonical_runtime_attestation_integrity"
    ]
    diagnostic = integrity["canonical_runtime_attestation"]
    assert diagnostic["delivered_count"] == 0
    assert diagnostic["explicit_acknowledgment"] == "UNMEASURED"
    assert diagnostic["behavioral_influence"] == "UNMEASURED"
