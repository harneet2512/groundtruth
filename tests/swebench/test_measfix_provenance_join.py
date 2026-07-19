"""RED-first regression for the CONTROL_PRECEDES_DELIVERY join drop.

Defect (SS-REFEREE control terminal): ``_control_participation_evidence`` searched
for a mediator's downstream delivery FORWARD-only (``range(index + 1, len(rows))``).
The provenance-seal mediator (``decision_site=mini_seam.delivery.provenance_seal``)
flushes the sealed DELIVERED row into the ledger immediately BEFORE it appends its own
control-decision record for the SAME atomic observation (identical ``observation_binding``,
iteration, and timestamp). Ledger WRITE ORDER therefore placed the delivery at a LOWER
index than the control record, and the forward-only slice silently dropped a delivery
that WAS present in the ledger — leaving ``runtime_member_control_receipt`` /
``mediated_fact_ids`` / ``mediation_correct`` at ``None`` for GT_SS_PROVENANCE across
27/35 run-4 tasks (49 rows).

RED proof (pre-fix code, on the exact real-shaped rows below): the control record sits at
index 1, the delivery at index 0, ``range(2, 2)`` is empty → ``joins == []`` and
``correctness is None``. The fix scans forward first (byte-identical for every already-
binding case) then the earlier rows, so the delivery written just behind its control
record binds — backed by the EXACT identity join (candidate_id + seal + chars +
fact_class + producer registration + FULL observation_binding equality), which is unique
per observation and cannot bind a spurious delivery. The bar is not weakened: a mediator
still needs a real, exactly-identified delivered row to earn a receipt.

Row shapes are faithfully reconstructed from
``D:/gt_runs/run4_smoke_29694879462/art/ll-full-conan-io__conan-17123``
(GT_SS_PROVENANCE obligations record at ledger index 90 + its delivered
``obligation_unexercised`` row at index 89). An optional check binds the same evidence
straight off the real artifact when the run-4 tree is present.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import gt_feature_metrics as metrics  # noqa: E402  # pyright: ignore[reportMissingImports]

_RUN4_TASK_DIR = Path(
    "D:/gt_runs/run4_smoke_29694879462/art/ll-full-conan-io__conan-17123"
)


def _observation_binding() -> dict:
    return {
        "schema": "gt.observation_binding.v1",
        "observation_id": (
            "83dfcadddf0005598dfc2c9c38bcb96ffcb600815c909af7b28517105877c6ee"
        ),
        "opportunity_id": (
            "f02eeac92c7ce8c84eff8823393a4d2bce01641fcf684b83fdd767a097126994"
        ),
        "batch_start_iteration": 11,
        "parent_policy_sha256": (
            "0327a602189258c4487dca180b0bbbda9446485abb680dda68eced7afb12c295"
        ),
        "parent_policy_chars": 84,
        "action_batch_sha256": (
            "96759419637a370391fbb86e4146b26ccbf2c53837e04a15caeb0300fee26deb"
        ),
        "candidate_ordinal": 0,
        "candidate_kind_sha256": (
            "03b66a19946c43b1a1ec08d281126cc60108ac70bd2b5d9ac8ef23f1f903fb74"
        ),
        "candidate_dedup_sha256": (
            "c074643fb87efee4c963cb672d73f27274dad3d0f31588e6c0efa83bd97d133d"
        ),
        "candidate_id": "dcaf66bd0c75f8c0",
    }


def _delivered_obligations_row() -> dict:
    return {
        "layer": "obligation.unexercised",
        "event_type": "test_result",
        "file_path": "",
        "outcome": "delivered",
        "reason": "",
        "chars_delivered": 305,
        "iteration": 12,
        "content_sha256_16": "fc25538ba77bbf16",
        "seal_scope": "block",
        "candidate_id": "dcaf66bd0c75f8c0",
        "lineage_schema": "gt.feature_lineage.v1",
        "runtime_producer_id": "spec",
        "registered_producer_id": "spec",
        "producer_registration_match": True,
        "evidence_type": "obligation_unexercised",
        "fact_class": "obligations",
        "feature_ids": [
            {"category": "FACT", "feature_id": "obligations", "role": "fact"}
        ],
        "required_event": "test_result",
        "actual_event": "test_result",
        "observation_binding": _observation_binding(),
        "timestamp_ms": 1784480221802,
    }


def _provenance_control_row() -> dict:
    return {
        "layer": "control.participation",
        "event_type": "control_decision",
        "file_path": "",
        "outcome": "evaluated",
        "reason": "typed_control_participation",
        "chars_delivered": 0,
        "iteration": 12,
        "participation_decision": "NO_EFFECT",
        "decision_reason": "candidate_already_clean",
        "schema": "gt.control_participation.v1",
        "control_ref": {
            "category": "CAP",
            "feature_id": "GT_SS_PROVENANCE",
            "role": "mediator",
        },
        "decision_site": "mini_seam.delivery.provenance_seal",
        "candidate_chars": 305,
        "candidate_sha256_16": "fc25538ba77bbf16",
        "fact_class": "obligations",
        "candidate_id": "dcaf66bd0c75f8c0",
        "temporal_relation": "CONTROL_PRECEDES_DELIVERY",
        "related_delivery_iteration": None,
        "observation_binding": _observation_binding(),
        "timestamp_ms": 1784480221802,
    }


def _evidence(rows: list[dict]) -> dict:
    return metrics._control_participation_evidence(rows, [], {"entries": []}, None)


def test_delivery_written_before_control_row_binds() -> None:
    """The provenance seal's delivery precedes its control record in write order.

    RED on the pre-fix forward-only search (control at index 1, ``range(2, 2)`` empty
    → zero joins, correctness ``None``). GREEN after the fix: the exact-identity
    delivery behind the control record binds and the NO_EFFECT declaration grades True.
    """
    rows = [_delivered_obligations_row(), _provenance_control_row()]

    ev = _evidence(rows)

    joins = ev["joins"].get("GT_SS_PROVENANCE", [])
    assert len(joins) == 1, (
        "forward-only search dropped the delivery written behind the control record"
    )
    assert joins[0]["fact_class"] == "obligations"
    assert ev["correctness"].get("GT_SS_PROVENANCE") is True
    assert ev["valid"] is True


def test_infra_control_gates_bind_from_backward_delivery() -> None:
    """The dropped join is exactly what left the SS-REFEREE terminal gates at None."""
    rows = [_delivered_obligations_row(), _provenance_control_row()]
    ev = _evidence(rows)

    readiness = metrics._infra_control_readiness(
        "GT_SS_PROVENANCE",
        (),  # kernel mediator: scope derives from fact_lifecycles below
        {"obligations": metrics.new_lifecycle("fixture")},
        ledger_artifact="gt_runtime_ledger_fixture.jsonl",
        control_evidence=ev,
    )
    gates = readiness["gates"]
    assert gates["runtime_member_control_receipt"] is True
    assert gates["mediated_fact_ids"] is True
    assert gates["mediation_correct"] is True
    assert readiness["mediation"]["runtime_linked_fact_ids"] == ["obligations"]


def test_forward_ordered_delivery_still_binds() -> None:
    """Regression guard: the ordinary case (delivery after the control) is unchanged."""
    rows = [_provenance_control_row(), _delivered_obligations_row()]
    ev = _evidence(rows)
    assert len(ev["joins"].get("GT_SS_PROVENANCE", [])) == 1
    assert ev["correctness"].get("GT_SS_PROVENANCE") is True


def test_no_delivery_present_stays_unbound() -> None:
    """The bar is not weakened: no exact-identity delivery anywhere → no join, None."""
    rows = [_provenance_control_row()]  # control record, but no delivered row at all
    ev = _evidence(rows)
    assert ev["joins"].get("GT_SS_PROVENANCE", []) == []
    assert ev["correctness"].get("GT_SS_PROVENANCE") is None


def test_mismatched_seal_delivery_does_not_bind() -> None:
    """A backward delivery whose bytes differ must NOT bind (exact identity required)."""
    delivery = _delivered_obligations_row()
    delivery["content_sha256_16"] = "ffffffffffffffff"  # different bytes
    rows = [delivery, _provenance_control_row()]
    ev = _evidence(rows)
    assert ev["joins"].get("GT_SS_PROVENANCE", []) == []
    assert ev["correctness"].get("GT_SS_PROVENANCE") is None


@pytest.mark.skipif(
    not (_RUN4_TASK_DIR / "gt_runtime_ledger_conan-io__conan-17123.jsonl").exists(),
    reason="run-4 artifact tree not present on this host",
)
def test_real_run4_provenance_binds() -> None:
    """The same drop, straight off the real run-4 conan-17123 ledger + brief."""
    ledger = _RUN4_TASK_DIR / "gt_runtime_ledger_conan-io__conan-17123.jsonl"
    traj = _RUN4_TASK_DIR / "mini-swe-agent.trajectory.json"
    brief = _RUN4_TASK_DIR / "brief_result.json"
    rows = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    trajectory = json.loads(traj.read_text(encoding="utf-8"))
    brief_payload = json.loads(brief.read_text(encoding="utf-8"))
    _, cons = metrics._consumption_by_fact_class(trajectory, str(ledger))
    ev = metrics._control_participation_evidence(
        rows, trajectory.get("messages", []) or [], cons, brief_payload
    )
    joins = ev["joins"].get("GT_SS_PROVENANCE", [])
    assert len(joins) >= 1
    assert "obligations" in {j.get("fact_class") for j in joins}
    assert ev["correctness"].get("GT_SS_PROVENANCE") is True
