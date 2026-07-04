"""Invariant 8: Claim Ledger Truth

WORKING/VERIFIED claims must have runtime/test/replay/graph proof.
Claims with only code_audit proof are UNVERIFIED.

Ports logic from tests/topology/test_claim_checker.py for invariant suite.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from gt_check_claims import check_claims


def make_claim(claim_id: str, layer: str, doc_status: str = "WORKING",
               proof_type: str = "none") -> dict:
    return {
        "claim_id": claim_id, "layer": layer,
        "description": f"Test claim {claim_id}",
        "expected_trigger": "test", "expected_evidence": "test",
        "proof_type": proof_type, "test_path": None,
        "current_status": "UNVERIFIED", "doc_status": doc_status,
    }


def make_autopsy(layer_key: str, visible: bool) -> dict:
    return {
        "task_id": "test_task",
        "layers": {layer_key: {
            "expected": "yes", "generated": visible,
            "visible_in_output": visible,
            "status": "DELIVERED" if visible else "NOT_FIRED",
            "failure_class": "", "markers_found": ["m"] if visible else [],
            "events_from_log": 1 if visible else 0,
        }},
    }


class TestClaimTruthInvariant:
    """WORKING claims without proof must be flagged."""

    def test_working_without_proof_is_unsupported(self):
        claims = [make_claim("L1_BRIEF_DELIVERY", "L1")]
        result = check_claims(claims, [])
        assert len(result["unsupported"]) == 1

    def test_working_with_trajectory_proof_is_verified(self):
        claims = [make_claim("L1_BRIEF_DELIVERY", "L1")]
        autopsies = [make_autopsy("L1_BRIEF", visible=True)]
        result = check_claims(claims, autopsies)
        assert len(result["verified"]) == 1
        assert len(result["contradicted"]) == 0

    def test_working_contradicted_by_zero_visibility(self):
        claims = [make_claim("L3_POST_EDIT_DELIVERY", "L3")]
        autopsies = [make_autopsy("L3_POST_EDIT", visible=False)]
        result = check_claims(claims, autopsies)
        assert len(result["contradicted"]) == 1

    def test_code_audit_only_is_unsupported(self):
        claims = [make_claim("L0_SCHEMA", "L0", proof_type="code_audit")]
        result = check_claims(claims, [])
        assert len(result["unsupported"]) == 1
        assert result["unsupported"][0]["proof_type"] == "code_audit"

    def test_test_proof_is_verified(self):
        claims = [make_claim("L3_U_SHAPED", "L3", proof_type="test")]
        result = check_claims(claims, [])
        assert len(result["verified"]) == 1

    def test_open_bug_not_silently_passed(self):
        claims = [make_claim("L6_OPEN", "L6", doc_status="OPEN_BUG")]
        result = check_claims(claims, [])
        assert len(result["skipped"]) == 0
        assert len(result["unsupported"]) == 1
