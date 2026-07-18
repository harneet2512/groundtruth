"""RED contract — Cluster-2c defects 7/8/9.

Defect 7: Gate-5 leak_zero graded PER FIRE (per PHYSICAL_DELIVERY_BOUND span), reusing
          scan_test_identity_leaks + the live_evidence _LEAK_RE class.
Defect 8: Gate-6 dose_lte_one graded PER FIRE (unique physical deliveries homed to the
          same policy observation; observation_id when present, else the legacy owner
          grouping; a FACT row + its CAP byte-owner sharing ONE span = ONE dose).
Defect 9: a CAP byte-owner inherits its FACT gates ONLY through its authorized mechanism;
          an unauthorized claim is a NAMED ownership rejection, never a silent non-inherit.

Each defect carries >= 2 biting mutations (a leaking span must FAIL, a double dose must
FAIL, a shared span must PASS, an unauthorized CAP claim must be NAMED).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_feature_metrics as metrics  # noqa: E402


def _fire(idx, phys, text, *, obs_id=None, msg_index=5):
    entry = {
        "source": "trajectory",
        "joined": True,
        "join_method": "seal",
        "runtime_ledger_index": idx,
        "physical_id": phys,
        "msg_index": msg_index,
        "rendered_text": text,
        "ledger_chars": len(text),
        "chars": len(text),
        "content_sha256_16": "a" * 16,
    }
    if obs_id is not None:
        entry["observation_binding"] = {"observation_id": obs_id}
    return entry


# --------------------------------------------------------------------------- #
# Defect 7 — per-fire leak scan.
# --------------------------------------------------------------------------- #
def test_leaking_fire_fails_its_own_leak_zero() -> None:
    # A bare pytest name + an assertion — caught by scan_test_identity_leaks AND _LEAK_RE.
    led = {"entries": [_fire(0, "m5:0:40", "run test_widget_state then assert value == 1")]}
    graded = metrics.per_fire_gate_grades(led, {})
    assert graded["bound_fire_count"] == 1
    assert graded["leaking_fire_count"] == 1
    # Biting: a leaking span must NOT pass its own gate row.
    assert graded["fires"][0]["leak_zero"] is False
    assert "test_widget_state" in graded["leak_hits"]
    assert "assert" in graded["leak_hits"]


def test_f2p_marker_span_leaks() -> None:
    led = {"entries": [_fire(0, "m5:0:40", "FAIL_TO_PASS is not a real block")]}
    graded = metrics.per_fire_gate_grades(led, {})
    assert graded["leaking_fire_count"] == 1
    assert "FAIL_TO_PASS" in graded["leak_hits"]


def test_clean_fire_passes_leak_zero() -> None:
    # No structural test identity / assertion -> byte-identical clean pass.
    led = {"entries": [_fire(0, "m5:0:30", "[CALLERS] widget in view.py")]}
    graded = metrics.per_fire_gate_grades(led, {})
    assert graded["leaking_fire_count"] == 0
    assert graded["fires"][0]["leak_zero"] is True
    assert graded["leak_hits"] == []


# --------------------------------------------------------------------------- #
# Defect 8 — per-observation dose.
# --------------------------------------------------------------------------- #
def test_double_dose_same_observation_fails() -> None:
    obs = "b" * 64
    led = {"entries": [
        _fire(0, "m5:0:10", "fact A", obs_id=obs),
        _fire(1, "m5:11:20", "cap B", obs_id=obs),
    ]}
    graded = metrics.per_fire_gate_grades(led, {})
    # Biting: two distinct physical spans on ONE observation is a double dose.
    assert graded["dose_violation_count"] == 2
    assert graded["max_dose"] == 2
    assert all(f["dose_lte_one"] is False for f in graded["fires"])


def test_fact_and_cap_sharing_one_span_is_one_dose() -> None:
    obs = "c" * 64
    # SAME physical_id (a FACT row and its CAP byte-owner over one physical span).
    led = {"entries": [
        _fire(0, "m5:0:10", "shared", obs_id=obs),
        _fire(1, "m5:0:10", "shared", obs_id=obs),
    ]}
    graded = metrics.per_fire_gate_grades(led, {})
    # Biting: a shared span must count as ONE dose and PASS.
    assert graded["dose_violation_count"] == 0
    assert graded["max_dose"] == 1
    assert all(f["dose_lte_one"] is True for f in graded["fires"])


def test_legacy_owner_fallback_groups_by_model_observation() -> None:
    # No observation_id -> group by the _model_observation_owners owner of msg_index.
    led = {"entries": [
        _fire(0, "m4:0:10", "A", msg_index=4),
        _fire(1, "m4:11:20", "B", msg_index=4),
    ]}
    owners = {4: 9}  # both messages observed by the same policy call
    graded = metrics.per_fire_gate_grades(led, owners)
    assert graded["dose_violation_count"] == 2
    # Distinct owners -> no shared observation -> no violation.
    graded_split = metrics.per_fire_gate_grades(led, {4: 4})
    assert graded_split["dose_violation_count"] == 2  # still same owner (both msg 4)
    led2 = {"entries": [
        _fire(0, "m4:0:10", "A", msg_index=4),
        _fire(1, "m8:0:10", "B", msg_index=8),
    ]}
    graded2 = metrics.per_fire_gate_grades(led2, {4: 4, 8: 8})
    assert graded2["dose_violation_count"] == 0


def test_broken_binding_is_not_a_fire() -> None:
    # A ledger_only / broken row never becomes a bound fire.
    led = {"entries": [{
        "source": "ledger_only", "runtime_ledger_index": 0,
        "physical_join_reason": "delivery_unjoined",
        "content_sha256_16": "a" * 16, "ledger_chars": 5,
    }]}
    graded = metrics.per_fire_gate_grades(led, {})
    assert graded["bound_fire_count"] == 0
    assert graded["dose_violation_count"] == 0


# --------------------------------------------------------------------------- #
# Defect 9 — CAP byte-owner ownership.
# --------------------------------------------------------------------------- #
def test_authorized_exact_profile_owner_not_rejected() -> None:
    rows = [{"outcome": "delivered", "profile_member": "GT_EDIT_CHECK", "layer": "edit.syntax"}]
    audit = metrics.byte_owner_ownership_audit(rows)
    assert audit["valid"] is True
    assert audit["rejection_count"] == 0


def test_authorized_typed_owner_not_rejected() -> None:
    rows = [{
        "outcome": "delivered",
        "lineage_schema": "gt.feature_lineage.v1",
        "producer_registration_match": True,
        "feature_ids": [
            {"category": "CAP", "feature_id": "GT_CHANGE_SURFACE", "role": "byte_owner"},
            {"category": "FACT", "feature_id": "newfile_precedent", "role": "fact"},
        ],
        "evidence_type": "new_file_destination",
        "runtime_producer_id": "change_surface",
        "registered_producer_id": "change_surface",
        "fact_class": "newfile_precedent",
    }]
    assert metrics.byte_owner_ownership_audit(rows)["valid"] is True


def test_reclassified_coherence_mediator_stamp_is_not_a_claim() -> None:
    # P4: GT_SS_COHERENCE_V2 keeps its detect.coherence byte stamp as a lane mediator
    # stamp — it is NOT a byte-owner inheritance claim and must never be rejected.
    rows = [{"outcome": "delivered", "profile_member": "GT_SS_COHERENCE_V2",
             "layer": "detect.coherence"}]
    assert metrics.byte_owner_ownership_audit(rows)["valid"] is True


def test_profile_stamp_for_wrong_layer_is_named_rejection() -> None:
    rows = [{"outcome": "delivered", "profile_member": "GT_EDIT_CHECK", "layer": "recovery"}]
    audit = metrics.byte_owner_ownership_audit(rows)
    # Biting: an unauthorized profile stamp must be NAMED, never a silent non-inherit.
    assert audit["valid"] is False
    assert audit["rejections"][0]["reason"] == "profile_layer_mismatch"
    assert audit["rejections"][0]["member"] == "GT_EDIT_CHECK"


def test_typed_cap_ref_for_profile_owner_is_wrong_mechanism() -> None:
    rows = [{"outcome": "delivered", "feature_ids": [
        {"category": "CAP", "feature_id": "GT_EDIT_CHECK", "role": "byte_owner"},
    ]}]
    audit = metrics.byte_owner_ownership_audit(rows)
    assert audit["valid"] is False
    assert audit["rejections"][0]["reason"] == "cap_ref_for_non_typed_owner"


def test_typed_cap_ref_binding_mismatch_is_named() -> None:
    rows = [{
        "outcome": "delivered",
        "lineage_schema": "gt.feature_lineage.v1",
        "producer_registration_match": True,
        "feature_ids": [
            {"category": "CAP", "feature_id": "GT_CHANGE_SURFACE", "role": "byte_owner"},
        ],
        "evidence_type": "bogus_evidence",
        "runtime_producer_id": "not_change_surface",
        "registered_producer_id": "x",
        "fact_class": "y",
    }]
    audit = metrics.byte_owner_ownership_audit(rows)
    assert audit["valid"] is False
    assert audit["rejections"][0]["reason"] == "typed_binding_mismatch"


# --------------------------------------------------------------------------- #
# Wiring — collect_task surfaces the new integrity projections (fail-closed).
# --------------------------------------------------------------------------- #
def test_collect_task_surfaces_per_fire_and_ownership(tmp_path) -> None:
    task_dir = tmp_path / "wire"
    task_dir.mkdir()
    record = metrics.collect_task("synthetic__wire", str(task_dir), profile="2")
    integ = record["ss_integrity"]
    assert integ["per_fire_gate"]["schema"] == metrics.PER_FIRE_GATE_SCHEMA
    assert integ["byte_owner_ownership"]["schema"] == metrics.BYTE_OWNER_OWNERSHIP_SCHEMA
    # No fires / no rows -> both clean, no forced fail-closed from these two paths.
    assert integ["per_fire_gate"]["bound_fire_count"] == 0
    assert integ["byte_owner_ownership"]["valid"] is True
    # Legacy ``integrity`` projection stays byte-stable (rollups live in ss_integrity).
    assert "per_fire_bound_count" not in record["integrity"]
