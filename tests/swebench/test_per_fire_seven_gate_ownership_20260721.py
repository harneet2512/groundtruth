"""RED contract — CODEX_129_BUGLIST class-level seven-gate laundering (FACT + CAP byte-owner).

The documented bar is ALL SEVEN SS gates on ONE individual physical fire. The grader
assembled the class terminal from INDEPENDENT class-level reductions (byte-proof returns on
ANY qualifying class row; timing/ack/probe/truth reduced separately by class; leak/dose the
only per-fire object). Two fires can therefore each supply a DIFFERENT subset of the seven
gates so the class UNION passes although NO single fire owns all seven — cross-fire laundering.

This suite pins the fix at both seams:

  * ``feature_fire_gate_ownership`` builds a seven-gate record keyed by the physical delivery
    (candidate_id + candidate_sha256_16 + observation) and reports ``owned`` = True ONLY when
    EVERY fire owns all seven. Two fires each missing a different gate => ``owned`` False even
    though the per-gate UNION across fires is all-True (the laundering the class did).

  * ``classify_delivery_feature`` (the classifier the run aggregator consumes) must NOT return
    a PASS bucket (CAUSAL_P5 / ACKNOWLEDGED) for a laundered class: class-union gates all True
    but ``fire_gate_ownership.owned`` False => SEALED_DELIVERED_UNGRADED (fail-closed). A single
    fully-owned fire still reaches CAUSAL_P5 (no over-tightening); the legacy no-record path is
    preserved (the classifier's own unit contract). A concrete failed fire is classified by its
    worst failed gate instead of being hidden behind a generic class-union terminal.

Biting mutations: laundered => downgrade; single-owned fire => still passes; legacy absence =>
unchanged; a fire missing leak/dose/time/ack each individually => not owned.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_feature_metrics as metrics  # noqa: E402
import ss_live_diagnosis as diagnosis  # noqa: E402
from groundtruth.runtime.fact_registry import (  # noqa: E402
    registration_for,
    required_event,
)
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    build_lineage,
    lineage_ledger_extra,
)

FEATURE = "caller_contract"


def _metric(value, status: str = "MEASURED") -> dict:
    return {"value": value, "status": status}


def _delivered(*, causal: bool = False) -> dict:
    registration = registration_for(FEATURE)
    assert registration is not None
    lineage = build_lineage(
        runtime_producer_id=registration.producer,
        evidence_type=FEATURE,
        actual_event=required_event(FEATURE) or registration.deliver_by,
    )
    assert lineage is not None
    return {
        "outcome": "delivered", "content_sha256_16": "a" * 16,
        "chars_delivered": 12, **lineage_ledger_extra(lineage),
    }


def _entry(receipt: int, *, causal: bool = False) -> dict:
    return {
        "source": "trajectory", "joined": True, "join_method": "seal",
        "receipt": receipt,
        "feature_lineage": {
            "schema": "gt.feature_lineage.v1",
            "features": [{"category": "FACT", "feature_id": FEATURE, "role": "fact"}],
            "causal_contribution_proven": causal,
        },
    }


def _fire(pid: str, *, leak: bool, dose: bool) -> dict:
    return {
        "host_physical_id": pid,
        "candidate_id": f"cand:{pid}",
        "candidate_sha256_16": pid[:16].ljust(16, "0"),
        "observation": f"observation_id:obs:{pid}",
        "leak_zero": leak,
        "dose_lte_one": dose,
    }


# --------------------------------------------------------------------------- #
# Builder — cross-fire laundering must not survive.
# --------------------------------------------------------------------------- #
def test_two_fires_each_missing_a_gate_are_not_owned() -> None:
    # Fire A: on-time but NOT acknowledged.  Fire B: acknowledged but LATE.
    # Both byte-proven (delivered), leak-clean, dose-ok.  Class correct_info / fair_probe True.
    fires = [_fire("pA", leak=True, dose=True), _fire("pB", leak=True, dose=True)]
    delivered_classes_by_fire = {"pA": {FEATURE}, "pB": {FEATURE}}
    time_by_physical_class = {("pA", FEATURE): True, ("pB", FEATURE): False}
    ack_by_physical_class = {("pA", FEATURE): False, ("pB", FEATURE): True}

    record = metrics.feature_fire_gate_ownership(
        fires=fires,
        delivered_classes_by_fire=delivered_classes_by_fire,
        time_by_physical_class=time_by_physical_class,
        ack_by_physical_class=ack_by_physical_class,
        correct_info_by_physical_class={("pA", FEATURE): True, ("pB", FEATURE): True},
        fair_probe_by_physical_class={("pA", FEATURE): True, ("pB", FEATURE): True},
        fact_classes=[FEATURE],
    )[FEATURE]

    assert record["schema"] == metrics.FEATURE_FIRE_GATE_SCHEMA
    assert record["fire_count"] == 2
    # Biting: the per-gate UNION across the two fires is all-True (the class laundering) ...
    union = {
        name: any(f["gates"][name] is True for f in record["fires"])
        for name in metrics._SS_GATE_NAMES
    }
    assert all(union.values()), union
    # ... yet NO single fire owns all seven, so the class is NOT owned.
    assert record["owned"] is False
    assert all(f["owns_all_seven"] is False for f in record["fires"])
    # The record is keyed by physical delivery identity.
    keys = {(f["candidate_id"], f["candidate_sha256_16"], f["observation"]) for f in record["fires"]}
    assert len(keys) == 2


def test_one_good_fire_cannot_launder_one_bad_fire() -> None:
    fires = [_fire("pGood", leak=True, dose=True), _fire("pBad", leak=False, dose=True)]
    record = metrics.feature_fire_gate_ownership(
        fires=fires,
        delivered_classes_by_fire={"pGood": {FEATURE}, "pBad": {FEATURE}},
        time_by_physical_class={("pGood", FEATURE): True, ("pBad", FEATURE): True},
        ack_by_physical_class={("pGood", FEATURE): True, ("pBad", FEATURE): True},
        correct_info_by_physical_class={
            ("pGood", FEATURE): True, ("pBad", FEATURE): True,
        },
        fair_probe_by_physical_class={
            ("pGood", FEATURE): True, ("pBad", FEATURE): True,
        },
        fact_classes=[FEATURE],
    )[FEATURE]
    # MUTATION 1: ``any(owns_all_seven)`` makes this True and launders pBad.
    assert record["owned"] is False
    owned_fires = [f for f in record["fires"] if f["owns_all_seven"]]
    assert [f["host_physical_id"] for f in owned_fires] == ["pGood"]


def test_all_fires_fully_gated_is_owned() -> None:
    fires = [_fire("p1", leak=True, dose=True), _fire("p2", leak=True, dose=True)]
    keys = {("p1", FEATURE): True, ("p2", FEATURE): True}
    record = metrics.feature_fire_gate_ownership(
        fires=fires,
        delivered_classes_by_fire={"p1": {FEATURE}, "p2": {FEATURE}},
        time_by_physical_class=keys,
        ack_by_physical_class=keys,
        correct_info_by_physical_class=keys,
        fair_probe_by_physical_class=keys,
        fact_classes=[FEATURE],
    )[FEATURE]
    assert record["owned"] is True


def test_no_bound_fire_is_unowned_none() -> None:
    record = metrics.feature_fire_gate_ownership(
        fires=[],
        delivered_classes_by_fire={},
        time_by_physical_class={},
        ack_by_physical_class={},
        correct_info_by_physical_class={},
        fair_probe_by_physical_class={},
        fact_classes=[FEATURE],
    )[FEATURE]
    assert record["owned"] is None
    assert record["fire_count"] == 0


def test_each_physical_gate_individually_blocks_ownership() -> None:
    base = dict(
        delivered_classes_by_fire={"p1": {FEATURE}},
        correct_info_by_physical_class={("p1", FEATURE): True},
        fair_probe_by_physical_class={("p1", FEATURE): True},
        fact_classes=[FEATURE],
    )
    # leak fail
    assert metrics.feature_fire_gate_ownership(
        fires=[_fire("p1", leak=False, dose=True)],
        time_by_physical_class={("p1", FEATURE): True},
        ack_by_physical_class={("p1", FEATURE): True}, **base,
    )[FEATURE]["owned"] is False
    # dose fail
    assert metrics.feature_fire_gate_ownership(
        fires=[_fire("p1", leak=True, dose=False)],
        time_by_physical_class={("p1", FEATURE): True},
        ack_by_physical_class={("p1", FEATURE): True}, **base,
    )[FEATURE]["owned"] is False
    # time unmeasured
    assert metrics.feature_fire_gate_ownership(
        fires=[_fire("p1", leak=True, dose=True)],
        time_by_physical_class={},
        ack_by_physical_class={("p1", FEATURE): True}, **base,
    )[FEATURE]["owned"] is False
    # ack unmeasured
    assert metrics.feature_fire_gate_ownership(
        fires=[_fire("p1", leak=True, dose=True)],
        time_by_physical_class={("p1", FEATURE): True},
        ack_by_physical_class={}, **base,
    )[FEATURE]["owned"] is False


def test_truth_and_probe_are_joined_to_the_same_physical_fire() -> None:
    """Class-level truth/causality must not fill a different fire's missing gates."""
    fires = [_fire("proved", leak=True, dose=True), _fire("unproved", leak=True, dose=True)]
    record = metrics.feature_fire_gate_ownership(
        fires=fires,
        delivered_classes_by_fire={"proved": {FEATURE}, "unproved": {FEATURE}},
        time_by_physical_class={("proved", FEATURE): True, ("unproved", FEATURE): True},
        ack_by_physical_class={("proved", FEATURE): True, ("unproved", FEATURE): True},
        correct_info_by_physical_class={("proved", FEATURE): True},
        fair_probe_by_physical_class={("proved", FEATURE): True},
        fact_classes=[FEATURE],
    )[FEATURE]
    by_id = {fire["host_physical_id"]: fire for fire in record["fires"]}
    # MUTATION 2: copying the class True onto every fire turns both missing gates True.
    assert by_id["unproved"]["gates"]["correct_info"] is None
    assert by_id["unproved"]["gates"]["fair_probe"] is None
    assert record["owned"] is False


def test_cap_owner_restriction_keeps_only_exact_owned_physical_fire() -> None:
    base = {
        "schema": metrics.FEATURE_FIRE_GATE_SCHEMA,
        "fact_class": "syntax_result",
        "fire_count": 2,
        "owned": False,
        "fires": [
            {**_fire("owned", leak=True, dose=True), "gates": dict(_ALL_TRUE_GATES),
             "owns_all_seven": True},
            {**_fire("other", leak=True, dose=True), "gates": dict(_ALL_TRUE_GATES),
             "owns_all_seven": True},
        ],
    }
    restricted = metrics.restrict_fire_gate_ownership(
        base, {"owned"}, feature_id="GT_EDIT_CHECK",
    )
    assert restricted["feature_id"] == "GT_EDIT_CHECK"
    assert restricted["fire_count"] == 1
    assert [fire["host_physical_id"] for fire in restricted["fires"]] == ["owned"]


def test_collect_task_keeps_row_truth_probe_and_cap_ownership_fire_local(
    tmp_path, monkeypatch,
) -> None:
    """Adversarial integration: every class UNION is True, but individual fires are not."""
    task = "synthetic__per-fire"
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(
        '{"messages": [{"role": "user", "content": "fixture"}]}', encoding="utf-8",
    )
    rows = [
        {"outcome": "delivered", "layer": "gateway.caller_break",
         "content_sha256_16": "1" * 16, "chars_delivered": 10},
        {"outcome": "delivered", "layer": "gateway.caller_break",
         "content_sha256_16": "2" * 16, "chars_delivered": 10},
        {"outcome": "delivered", "layer": "edit.syntax", "profile_member": "GT_EDIT_CHECK",
         "content_sha256_16": "3" * 16, "chars_delivered": 10},
        {"outcome": "delivered", "layer": "edit.syntax",
         "content_sha256_16": "4" * 16, "chars_delivered": 10},
    ]
    (tmp_path / f"gt_runtime_ledger_{task}.jsonl").write_text(
        "".join(__import__("json").dumps(row) + "\n" for row in rows), encoding="utf-8",
    )
    fires = [
        {"runtime_ledger_index": index, "physical_id": f"p{index}",
         "candidate_id": f"candidate:{index}", "candidate_sha256_16": str(index + 1) * 16,
         "observation_group": f"observation_id:obs:{index}", "leak_zero": True,
         "dose_lte_one": True, "dose": 1, "leak_hits": []}
        for index in range(4)
    ]
    entries = [
        {"source": "trajectory", "joined": True, "join_method": "seal",
         "runtime_ledger_index": index, "physical_id": f"p{index}",
         "content_sha256_16": str(index + 1) * 16, "ledger_chars": 10,
         "ledger_layer": rows[index]["layer"], "receipt": 1, "msg_index": index}
        for index in range(4)
    ]
    monkeypatch.setattr(metrics, "_consumption_by_fact_class", lambda *_: (
        {}, {"schema": "gt.consumption_ledger.v2", "entries": entries,
             "visible_audit_complete": True, "physical_identity_conflict_ids": []},
    ))
    monkeypatch.setattr(metrics, "per_fire_gate_grades", lambda *_: {
        "schema": metrics.PER_FIRE_GATE_SCHEMA, "bound_fire_count": 4,
        "leaking_fire_count": 0, "dose_violation_count": 0, "max_dose": 1,
        "leak_hits": [], "fires": fires,
    })

    def fake_truth(_task_dir, _rows, lifecycles, _artifact):
        for fc in ("caller_contract", "syntax_result"):
            lifecycles[fc]["truth_valid"] = _metric(True)
            lifecycles[fc]["authority_valid"] = _metric(True)
        # Only p0 and the two syntax rows have exact truth. p1 must stay unproved even
        # though the caller_contract CLASS aggregate above is True.
        return {"joined_fact_classes": {
            "caller_contract": {"per_delivery": [
                {"ledger_row_index": 0, "truth": True, "authority": True,
                 "freshness": True},
            ]},
            "syntax_result": {"per_delivery": [
                {"ledger_row_index": 2, "truth": True, "authority": True,
                 "freshness": True},
                {"ledger_row_index": 3, "truth": True, "authority": True,
                 "freshness": True},
            ]},
        }}

    monkeypatch.setattr(metrics, "_apply_attestation_truth", fake_truth)
    deliveries = [
        {"ledger_row_index": index,
         "fact_class": "caller_contract" if index < 2 else "syntax_result",
         "physical_id": f"p{index}", "parent_physical_id": None,
         "timing_verdict": "ON_TIME"}
        for index in range(4)
    ]
    monkeypatch.setattr(metrics, "adjudicate_deliveries", lambda *_: {
        "deliveries": deliveries, "per_fact_class": {},
    })
    monkeypatch.setattr(metrics, "timing_by_fact_class", lambda *_: {
        "caller_contract": True, "syntax_result": True,
    })
    chronologies = {
        index: SimpleNamespace(
            ledger_row_index=index,
            fact_class="caller_contract" if index < 2 else "syntax_result",
            physical_id=f"p{index}", parent_physical_id=None,
        )
        for index in range(4)
    }
    monkeypatch.setattr(metrics, "extract_chronologies", lambda *_: chronologies)
    monkeypatch.setattr(metrics, "extract_block_chronologies", lambda *_: [])
    monkeypatch.setattr(metrics, "acknowledgment_by_fact_class", lambda *_args, **_kwargs: {
        "caller_contract": True, "syntax_result": True,
    })
    monkeypatch.setattr(metrics, "_receipt_corroborated_acknowledgment",
                        lambda *_args, **_kwargs: (
                            {"caller_contract": True, "syntax_result": True},
                            {"integrity_ok": True},
                        ))
    monkeypatch.setattr(metrics, "acknowledgment_for_row",
                        lambda ec, *_: False if ec.ledger_row_index in {1, 2} else True)
    monkeypatch.setattr(metrics, "load_attestations",
                        lambda *_: SimpleNamespace(attestations=()))
    fair_join = {
        "per_fact_class": {
            "caller_contract": {"fair_probe": True},
            "syntax_result": {"fair_probe": True},
        },
        "per_delivery": [
            {"ledger_row_index": 0, "fact_class": "caller_contract", "fair_probe": True},
            {"ledger_row_index": 2, "fact_class": "syntax_result", "fair_probe": True},
            {"ledger_row_index": 3, "fact_class": "syntax_result", "fair_probe": True},
        ],
    }
    monkeypatch.setattr(metrics, "join_fair_probes", lambda *_args, **_kwargs: fair_join)
    monkeypatch.setattr(metrics, "fair_probe_bool_by_fact_class", lambda *_: {
        "caller_contract": True, "syntax_result": True,
    })
    monkeypatch.setattr(metrics, "fair_probe_bool_by_delivery_row", lambda *_: {
        (0, "caller_contract"): True,
        (2, "syntax_result"): True,
        (3, "syntax_result"): True,
    })

    record = metrics.collect_task(task, str(tmp_path), profile="2")
    caller = record["ss_features"]["caller_contract"]["ss_readiness"][
        "fire_gate_ownership"
    ]
    assert caller["fire_count"] == 2
    assert caller["owned"] is False
    caller_fires = {fire["host_physical_id"]: fire for fire in caller["fires"]}
    assert caller_fires["p1"]["gates"]["correct_info"] is None
    assert caller_fires["p1"]["gates"]["fair_probe"] is None

    cap = record["ss_features"]["GT_EDIT_CHECK"]["ss_readiness"][
        "fire_gate_ownership"
    ]
    # MUTATION 3: class-only CAP inheritance includes p3 and lets it cover p2's failed ack.
    assert cap["fire_count"] == 1
    assert [fire["host_physical_id"] for fire in cap["fires"]] == ["p2"]
    assert cap["owned"] is False


# --------------------------------------------------------------------------- #
# Classifier — the laundered class must never terminal a PASS bucket.
# --------------------------------------------------------------------------- #
def _lifecycle() -> dict:
    return {
        "eligible": _metric(True), "produced": _metric(True),
        "delivered": _metric(True), "truth_valid": _metric(True),
        "authority_valid": _metric(True), "expired_late": _metric(False),
        "stale": _metric(False), "receipt_level": _metric(3),
    }


_ALL_TRUE_GATES = {
    "delivered_byte_proven": True, "correct_info": True,
    "correct_rl_adhered_time": True, "acknowledged": True,
    "leak_zero": True, "dose_lte_one": True, "fair_probe": True,
}


def _ownership(owned) -> dict:
    return {
        "schema": metrics.FEATURE_FIRE_GATE_SCHEMA, "fact_class": FEATURE,
        "fire_count": 2 if owned is not None else 0, "owned": owned, "fires": [],
    }


def _ownership_with_fire(**gate_overrides) -> dict:
    gates = dict(_ALL_TRUE_GATES, **gate_overrides)
    return {
        "schema": metrics.FEATURE_FIRE_GATE_SCHEMA,
        "fact_class": FEATURE,
        "fire_count": 1,
        "owned": all(value is True for value in gates.values()),
        "fires": [{
            **_fire("p1", leak=gates["leak_zero"], dose=gates["dose_lte_one"]),
            "gates": gates,
            "owns_all_seven": all(value is True for value in gates.values()),
        }],
    }


def test_classifier_rejects_cross_fire_laundering() -> None:
    # Class-union gates all pass, but the per-fire record proves no single fire owns all
    # seven.  The classifier must NOT credit CAUSAL_P5/ACKNOWLEDGED; it fails closed.
    laundered = {"gates": dict(_ALL_TRUE_GATES), "fire_gate_ownership": _ownership(False)}
    assert diagnosis.classify_delivery_feature(
        FEATURE, "FACT", _lifecycle(), laundered,
        [_delivered(causal=True)], [_entry(3, causal=True)],
    ) == "SEALED_DELIVERED_UNGRADED"

    # Same class-union, but ack True with no causal proof and no fire ownership -> still
    # must not reach ACKNOWLEDGED (the ack could belong to a different fire).
    laundered_ack = {"gates": dict(_ALL_TRUE_GATES), "fire_gate_ownership": _ownership(False)}
    assert diagnosis.classify_delivery_feature(
        FEATURE, "FACT", _lifecycle(), laundered_ack,
        [_delivered()], [_entry(3)],
    ) == "SEALED_DELIVERED_UNGRADED"


def test_classifier_credits_single_owned_fire() -> None:
    owned = {"gates": dict(_ALL_TRUE_GATES), "fire_gate_ownership": _ownership(True)}
    assert diagnosis.classify_delivery_feature(
        FEATURE, "FACT", _lifecycle(), owned,
        [_delivered(causal=True)], [_entry(3, causal=True)],
    ) == "CAUSAL_P5"
    assert diagnosis.classify_delivery_feature(
        FEATURE, "FACT", _lifecycle(), owned,
        [_delivered()], [_entry(3)],
    ) == "ACKNOWLEDGED"


def test_classifier_legacy_no_record_path_preserved() -> None:
    # No fire_gate_ownership key (direct unit callers) -> legacy class-union behavior.
    legacy = {"gates": dict(_ALL_TRUE_GATES)}
    assert diagnosis.classify_delivery_feature(
        FEATURE, "FACT", _lifecycle(), legacy,
        [_delivered(causal=True)], [_entry(3, causal=True)],
    ) == "CAUSAL_P5"


def test_classifier_hard_gate_failure_still_dominates_ownership() -> None:
    # A class-level MEASURED gate failure (leak) must still win regardless of ownership.
    leaked = {
        "gates": dict(_ALL_TRUE_GATES, leak_zero=False),
        "fire_gate_ownership": _ownership(True),
    }
    assert diagnosis.classify_delivery_feature(
        FEATURE, "FACT", _lifecycle(), leaked,
        [_delivered(causal=True)], [_entry(3, causal=True)],
    ) == "LEAKED"


def test_classifier_names_worst_individual_fire_failure() -> None:
    # The class UNION is clean; only the exact fire exposes the failure. The terminal must
    # preserve the concrete reason instead of flattening every fire-local failure to UNGRADED.
    per_fire_leak = {
        "gates": dict(_ALL_TRUE_GATES),
        "fire_gate_ownership": _ownership_with_fire(leak_zero=False),
    }
    assert diagnosis.classify_delivery_feature(
        FEATURE, "FACT", _lifecycle(), per_fire_leak,
        [_delivered(causal=True)], [_entry(3, causal=True)],
    ) == "LEAKED"

    per_fire_ignored = {
        "gates": dict(_ALL_TRUE_GATES),
        "fire_gate_ownership": _ownership_with_fire(acknowledged=False),
    }
    assert diagnosis.classify_delivery_feature(
        FEATURE, "FACT", _lifecycle(), per_fire_ignored,
        [_delivered(causal=True)], [_entry(3, causal=True)],
    ) == "NOVEL_IGNORED"
