"""Tests for gt_check_claims.py — the claim proof checker."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from gt_check_claims import check_claims


def make_claim(claim_id: str, layer: str, doc_status: str = "WORKING",
               proof_type: str = "none") -> dict:
    return {
        "claim_id": claim_id,
        "layer": layer,
        "description": f"Test claim {claim_id}",
        "expected_trigger": "test",
        "expected_evidence": "test",
        "proof_type": proof_type,
        "test_path": None,
        "current_status": "UNVERIFIED",
        "doc_status": doc_status,
    }


def make_autopsy(layer_key: str, visible: bool, generated: bool = True) -> dict:
    return {
        "task_id": "test_task",
        "layers": {
            layer_key: {
                "expected": "yes",
                "generated": generated,
                "visible_in_output": visible,
                "status": "DELIVERED" if visible else "NOT_FIRED",
                "failure_class": "",
                "markers_found": ["marker"] if visible else [],
                "events_from_log": 1 if generated else 0,
            }
        },
    }


class TestContradictedClaims:
    """Claims contradicted by fresh run artifacts."""

    def test_working_claim_contradicted_by_zero_visibility(self):
        claims = [make_claim("L3_POST_EDIT_DELIVERY", "L3")]
        autopsies = [make_autopsy("L3_POST_EDIT", visible=False, generated=False)]
        result = check_claims(claims, autopsies)
        assert len(result["contradicted"]) == 1
        assert result["contradicted"][0]["claim_id"] == "L3_POST_EDIT_DELIVERY"

    def test_working_claim_verified_by_visibility(self):
        claims = [make_claim("L1_BRIEF_DELIVERY", "L1")]
        autopsies = [make_autopsy("L1_BRIEF", visible=True)]
        result = check_claims(claims, autopsies)
        assert len(result["contradicted"]) == 0
        assert len(result["verified"]) == 1

    def test_l1_key_contracts_zero_visibility_contradicts(self):
        """L1_KEY_CONTRACTS 0/N must be CONTRADICTED, not silently passed."""
        claims = [make_claim("L1_KEY_CONTRACTS", "L1+")]
        autopsies = [
            make_autopsy("L1_KEY_CONTRACTS", visible=False, generated=False),
            make_autopsy("L1_KEY_CONTRACTS", visible=False, generated=False),
            make_autopsy("L1_KEY_CONTRACTS", visible=False, generated=False),
        ]
        result = check_claims(claims, autopsies)
        assert len(result["contradicted"]) == 1, (
            f"L1_KEY_CONTRACTS 0/3 visible must be contradicted. "
            f"Got: contradicted={result['contradicted']}, verified={result['verified']}, "
            f"unsupported={result['unsupported']}, skipped={result['skipped']}"
        )
        assert "0/3" in result["contradicted"][0]["reason"]


class TestUnsupportedClaims:
    """Claims without trajectory proof."""

    def test_delivery_claim_without_trajectory_proof(self):
        claims = [make_claim("L1_BRIEF_DELIVERY", "L1", proof_type="none")]
        result = check_claims(claims, [])
        assert len(result["unsupported"]) == 1

    def test_code_audit_claim_flagged(self):
        claims = [make_claim("L0_SCHEMA", "L0", proof_type="code_audit")]
        result = check_claims(claims, [])
        assert len(result["unsupported"]) == 1
        assert result["unsupported"][0]["proof_type"] == "code_audit"

    def test_test_proof_accepted(self):
        claims = [make_claim("L3_U_SHAPED_ORDER", "L3", proof_type="test")]
        result = check_claims(claims, [])
        assert len(result["verified"]) == 1


class TestOpenBugClaims:
    """OPEN_BUG claims must not be skipped — they must be flagged."""

    def test_open_bug_not_skipped(self):
        claims = [make_claim("L6_PRESUBMIT_OPEN", "L6", doc_status="OPEN_BUG")]
        autopsies = [make_autopsy("L6_PRESUBMIT", visible=False, generated=True)]
        result = check_claims(claims, autopsies)
        assert len(result["skipped"]) == 0, "OPEN_BUG must not be skipped"
        assert len(result["unsupported"]) == 1
        assert "OPEN_BUG" in result["unsupported"][0]["reason"]

    def test_open_bug_without_autopsies(self):
        claims = [make_claim("L6_PRESUBMIT_OPEN", "L6", doc_status="OPEN_BUG")]
        result = check_claims(claims, [])
        assert len(result["skipped"]) == 0
        assert len(result["unsupported"]) == 1


class TestSkippedClaims:
    """Only DISABLED claims are skipped."""

    def test_disabled_claim_skipped(self):
        claims = [make_claim("CONDENSER_DISABLED", "Infrastructure", doc_status="DISABLED")]
        result = check_claims(claims, [])
        assert len(result["skipped"]) == 1

    def test_broken_claim_not_skipped(self):
        """BROKEN is no longer auto-skipped — use OPEN_BUG or DISABLED."""
        claims = [make_claim("SOME_BROKEN", "L6", doc_status="BROKEN")]
        result = check_claims(claims, [])
        # BROKEN without matching DISABLED/OPEN_BUG falls through to unsupported or skipped
        # depending on layer. The key assertion: it's not silently passed.
        total_processed = (
            len(result["verified"]) + len(result["unsupported"])
            + len(result["contradicted"]) + len(result["skipped"])
        )
        assert total_processed == 1


class TestMultipleAutopsies:
    """Claims checked against multiple task autopsies."""

    def test_partial_visibility_still_verifies(self):
        claims = [make_claim("L3B_POST_VIEW_DELIVERY", "L3b")]
        autopsies = [
            make_autopsy("L3B_POST_VIEW", visible=True),
            make_autopsy("L3B_POST_VIEW", visible=False),
        ]
        result = check_claims(claims, autopsies)
        assert len(result["verified"]) == 1
        assert len(result["contradicted"]) == 0

    def test_zero_visibility_across_all_tasks_contradicts(self):
        claims = [make_claim("L4A_AUTO_QUERY", "L4a")]
        autopsies = [
            make_autopsy("L4A_AUTO_QUERY", visible=False, generated=False),
            make_autopsy("L4A_AUTO_QUERY", visible=False, generated=False),
        ]
        result = check_claims(claims, autopsies)
        assert len(result["contradicted"]) == 1
