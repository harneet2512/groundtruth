from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import feature_opportunity as opportunities  # noqa: E402
import gt_feature_inventory as inventory  # noqa: E402


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _action_sha(actions: list[dict]) -> str:
    keys = tuple(
        json.dumps(action, sort_keys=True, separators=(",", ":"), default=str)
        for action in actions
    )
    return _sha(json.dumps(keys, separators=(",", ":"), ensure_ascii=False))


def _row(parent: str, actions: list[dict]) -> dict:
    parent_sha = _sha(parent)
    action_sha = _action_sha(actions)
    observation_id = opportunities.policy_observation_id(
        0, parent_sha, action_sha
    )
    kind_sha = _sha("gateway.new_file_destination")
    dedup_sha = _sha("typed-dedup")
    return {
        "layer": "feature.opportunity",
        "event_type": "policy_observation",
        "file_path": "",
        "outcome": "eligible",
        "reason": "formatter_visible_candidate",
        "chars_delivered": 0,
        "iteration": 0,
        "observation_id": observation_id,
        "opportunity_id": opportunities.feature_opportunity_id(
            observation_id, 0, kind_sha, dedup_sha
        ),
        "parent_policy_sha256": parent_sha,
        "parent_policy_chars": len(parent),
        "action_batch_sha256": action_sha,
        "candidate_ordinal": 0,
        "candidate_kind_sha256": kind_sha,
        "candidate_dedup_sha256": dedup_sha,
        "delivery_eligible": True,
        "selected": True,
        "feature_refs": [
            {"category": "CAP", "feature_id": "GT_CHANGE_SURFACE", "role": "byte_owner"},
            {"category": "FACT", "feature_id": "newfile_precedent", "role": "fact"},
        ],
        "attribution_status": "BOUND",
        "attribution_reason": "typed_lineage",
    }


def test_bound_opportunity_requires_exact_hashes_and_parent_policy_join() -> None:
    parent = "I will inspect the package before choosing a destination."
    actions = [{"command": "inspect package"}]
    result = opportunities.collect_feature_opportunities(
        [_row(parent, actions)],
        [{"role": "assistant", "content": parent, "extra": {"actions": actions}}],
        inventory.canonical_feature_inventory(),
    )

    for feature in ("GT_CHANGE_SURFACE", "newfile_precedent"):
        evidence = result["features"][feature]
        assert evidence["status"] == "BOUND"
        assert evidence["eligible_opportunity"] is True
        assert evidence["delivery_eligible_count"] == 1
        assert evidence["selected_count"] == 1
        boundary = evidence["decision_boundary_evidence"][0]
        assert boundary["parent_message_index"] == 0
        assert boundary["parent_policy_joined"] is True
        assert boundary["decision_open"] is None
        assert boundary["precommit_status"] == "UNMEASURED:chronological_audit_required"
    assert result["integrity"]["publishable"] is True


def test_absent_mismatched_and_malformed_opportunities_fail_closed() -> None:
    inv = inventory.canonical_feature_inventory()
    absent = opportunities.collect_feature_opportunities([], [], inv)
    assert absent["features"]["newfile_precedent"] == {
        "status": "UNMEASURED",
        "reason": "no_bound_opportunity",
        "eligible_opportunity": None,
        "opportunity_count": 0,
        "delivery_eligible_count": 0,
        "selected_count": 0,
        "decision_boundary_evidence": [],
    }

    parent = "parent"
    actions = [{"command": "inspect"}]
    mismatch = _row(parent, actions)
    mismatch.update({
        "attribution_status": "UNMEASURED",
        "attribution_reason": "producer_registration_mismatch",
        "feature_refs": [],
    })
    mismatched = opportunities.collect_feature_opportunities(
        [mismatch],
        [{"role": "assistant", "content": parent, "extra": {"actions": actions}}],
        inv,
    )
    assert mismatched["integrity"]["publishable"] is True
    assert mismatched["integrity"]["unbound_rows"] == 1
    assert mismatched["features"]["newfile_precedent"]["status"] == "UNMEASURED"

    malformed = _row(parent, actions)
    malformed["opportunity_id"] = "0" * 64
    malformed["feature_refs"][1]["extra"] = "not exact structured lineage"
    invalid = opportunities.collect_feature_opportunities(
        [malformed],
        [{"role": "assistant", "content": parent, "extra": {"actions": actions}}],
        inv,
    )
    assert invalid["integrity"]["publishable"] is False
    assert invalid["features"]["newfile_precedent"]["status"] == "UNMEASURED"
    assert "invalid_bound_opportunity" in invalid["features"]["newfile_precedent"]["reason"]


@pytest.mark.parametrize(
    "field,invalid_value,expected_issue",
    [
        ("observation_id", "0" * 64, "observation_id_mismatch"),
        ("opportunity_id", "0" * 64, "opportunity_id_mismatch"),
        ("parent_policy_chars", 999, "parent_policy_chars_mismatch"),
    ],
)
def test_each_identity_and_parent_join_is_independently_cardinal(
    field: str, invalid_value: object, expected_issue: str,
) -> None:
    parent = "parent"
    actions = [{"command": "inspect"}]
    row = _row(parent, actions)
    row[field] = invalid_value

    result = opportunities.collect_feature_opportunities(
        [row],
        [{"role": "assistant", "content": parent, "extra": {"actions": actions}}],
        inventory.canonical_feature_inventory(),
    )

    evidence = result["features"]["newfile_precedent"]
    assert evidence["status"] == "UNMEASURED"
    assert expected_issue in evidence["reason"]


def test_parent_policy_hash_must_join_the_actual_trajectory_parent() -> None:
    actions = [{"command": "inspect"}]
    row = _row("recorded parent", actions)
    result = opportunities.collect_feature_opportunities(
        [row],
        [{"role": "assistant", "content": "different parent", "extra": {"actions": actions}}],
        inventory.canonical_feature_inventory(),
    )
    evidence = result["features"]["newfile_precedent"]
    assert evidence["status"] == "UNMEASURED"
    assert "parent_policy_join" in evidence["reason"]


def test_parent_message_index_preserves_non_dict_chronology() -> None:
    parent = "parent"
    actions = [{"command": "inspect"}]
    result = opportunities.collect_feature_opportunities(
        [_row(parent, actions)],
        ["decode-error-placeholder", {
            "role": "assistant", "content": parent, "extra": {"actions": actions},
        }],
        inventory.canonical_feature_inventory(),
    )
    boundary = result["features"]["newfile_precedent"]["decision_boundary_evidence"][0]
    assert boundary["parent_message_index"] == 1


def test_attaching_opportunity_evidence_cannot_promote_ss_live() -> None:
    row = {
        "family": "FACT",
        "status": "MEASURED",
        "ss_readiness": {
            "gates": {
                "delivered_byte_proven": False,
                "correct_info": None,
                "correct_rl_adhered_time": None,
                "acknowledged": None,
                "leak_zero": None,
                "dose_lte_one": None,
                "fair_probe": None,
            },
            "live_witness": False,
            "ss_live": False,
            "blockers": [
                "delivered_byte_proven", "correct_info", "correct_rl_adhered_time",
                "acknowledged", "leak_zero", "dose_lte_one", "fair_probe",
                "live_witness",
            ],
        },
    }
    evidence = {
        "status": "BOUND", "reason": "producer_matched_typed_lineage",
        "eligible_opportunity": True, "opportunity_count": 1,
        "delivery_eligible_count": 1, "selected_count": 1,
        "decision_boundary_evidence": [],
    }

    attached = opportunities.attach_opportunity_evidence(row, evidence)

    assert attached["opportunity_evidence"] == evidence
    assert attached["ss_readiness"] == row["ss_readiness"]
    assert attached["ss_readiness"]["ss_live"] is False
