from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for entry in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from gt_complete_report import build_report, derived_inventory  # noqa: E402


CLAIM = {
    "primary_claim": "Whether valid GT changes agent behavior",
    "estimand": "live opportunity effect",
    "unit": "opportunity",
    "treatment": "deliver selected GT candidate",
    "comparator": "withhold selected GT candidate",
    "assignment": "randomized",
    "primary_outcome": "registered receipt",
    "consequence_horizon": "through the next decision boundary",
    "task_population": "locked held-out coding tasks",
    "pre_specified": True,
}
SYSTEM = {
    "model": "model",
    "provider": "provider",
    "scaffold": "mini-swe-agent",
    "tools": "fixed tool contract",
    "gt_profile": "2",
    "budget": "fixed",
    "retries": "fixed",
}


def test_inventory_is_holistic_and_direct_slice_is_derived() -> None:
    inventory = derived_inventory()
    assert inventory["family_counts"] == {
        "ACQ": 12,
        "CAP": 48,
        "FACT": 11,
        "PERF": 58,
    }
    assert inventory["total"] == 129
    assert inventory["direct_delivery_count"] == 17
    assert inventory["role_counts"]["byte_owner"] == 7
    assert inventory["role_counts"]["direct_fact"] == 10


def test_absent_live_records_cannot_be_promoted_by_report_builder() -> None:
    report = build_report(
        [],
        study_id="empty",
        scope="product",
        claim_contract=CLAIM,
        system_identity=SYSTEM,
        minimum_detectable_effect=0.2,
        held_out_test_set=True,
    )
    assert report["schema"] == "gt.complete.report.v1"
    assert report["level_1"]["inventory_total"] == 129
    assert report["level_1"]["ss_live_rows"] == 0
    assert report["level_1"]["status"] == "LEVEL1_UNMEASURED"
    assert report["level_2"]["status"] == "BEHAVIOR_UNMEASURED"
    assert report["verdict"]["final_verdict"] == "GT_OPERATION_UNPROVEN"
    assert len(report["feature_attribution"]) == 129
