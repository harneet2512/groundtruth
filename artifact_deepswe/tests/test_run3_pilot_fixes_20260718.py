"""Run-#3 pilot identity and receipt-path regressions.

The record layer was right to reject two candidate IDs.  The lane candidate
was built with a legacy ``kind/kind`` identity and then independently upgraded
to registered ``contract_map/caller_contract`` identity at delivery.  The
repair assigns registered identity before the opportunity is built, then
carries it through opportunity, delivery, and terminal control records.

Receipt persistence separately prefers the writable runtime output surface;
``GT_CERT_DIR`` is the read-only cert mount in the live container.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import gt_mini_patch as g
from groundtruth.runtime.evidence_envelope import build_observation_binding


def _caller_contract_candidate(monkeypatch):
    monkeypatch.setattr(g, "_ss_any_content_gate_on", lambda: False)
    out = {"output": "viewed source"}
    pool = []
    producer_text = (
        "\n[SIGNATURE] src/x.py::render(value: str) -> str\n"
        "[CALLERS] 1 verified caller; preserve this interface"
    )
    g._global_pool_add_lane_a(
        pool,
        out,
        "sed -n '1,120p' src/x.py",
        [("l3.contract", producer_text, "src/x.py")],
        krel="src/x.py",
        event="file_view",
        kkind="post_view",
    )
    assert len(pool) == 1
    candidate, _thunk = pool[0]
    assert candidate.kind == "l3.contract"
    assert candidate.lineage is not None
    assert candidate.lineage.runtime_producer_id == "contract_map"
    assert candidate.lineage.evidence_type == "caller_contract"
    delivery_extra = g._lane_delivery_extra(
        "l3.contract",
        producer_text,
        "src/x.py",
        "file_view",
        lineage=candidate.lineage,
    )
    assert delivery_extra is not None
    return candidate, producer_text, delivery_extra


def _binding(candidate):
    return build_observation_binding(
        batch_start_iteration=9,
        parent_policy_sha256="4" * 64,
        parent_policy_chars=64,
        action_batch_sha256="5" * 64,
        candidate_ordinal=0,
        candidate_kind=candidate.kind,
        candidate_id=candidate.dedup_key,
    )


def _record_terminal_control(
        monkeypatch, rows, *, binding, producer_text, delivery_extra):
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(dict(row)))
    monkeypatch.setattr(g, "_action_count", 12)
    g._terminal_lane_controls.clear()
    g._stage_terminal_lane_control(
        "l3.contract",
        producer_text,
        feature_id="GT_CONTRACT_NATIVE",
        decision_site="mini_seam.contract.native_render",
        decision="APPLIED",
        reason="native_contract_render_selected",
    )
    token = g._delivery_observation_context.set(binding)
    try:
        g._record_terminal_lane_controls(
            "l3.contract",
            producer_text,
            "\nGT:shipped-bytes",
            "src/x.py",
            delivery_extra=delivery_extra,
        )
    finally:
        g._delivery_observation_context.reset(token)


def test_caller_contract_identity_flows_opportunity_delivery_control(monkeypatch):
    candidate, producer_text, delivery_extra = _caller_contract_candidate(monkeypatch)
    binding = _binding(candidate)

    # Identity is selected before the opportunity. Canonicalization is
    # representation-only; it must not replace a conflicting identity.
    assert delivery_extra["candidate_id"] == candidate.dedup_key
    assert binding.candidate_id == candidate.dedup_key

    rows: list[dict] = []
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(dict(row)))
    monkeypatch.setattr(g, "_action_count", 12)
    state = {
        "pool": [(candidate, lambda: None)],
        "opportunity_bindings": {id(candidate): binding},
    }
    g._record_batch_opportunities(
        state,
        {id(candidate): SimpleNamespace(disposition="deliver")},
        candidate,
    )
    token = g._delivery_observation_context.set(binding)
    try:
        g._runtime_ledger_record(
            kind="l3.contract",
            outcome="delivered",
            chars=len("\nGT:shipped-bytes"),
            file_path="src/x.py",
            event="file_view",
            content="\nGT:shipped-bytes",
            extra=delivery_extra,
        )
    finally:
        g._delivery_observation_context.reset(token)
    _record_terminal_control(
        monkeypatch,
        rows,
        binding=binding,
        producer_text=producer_text,
        delivery_extra=delivery_extra,
    )

    opportunity = [r for r in rows if r.get("layer") == "feature.opportunity"]
    delivery = [
        r for r in rows
        if r.get("layer") == "l3.contract" and r.get("outcome") == "delivered"
    ]
    control = [r for r in rows if r.get("layer") == "control.participation"]
    assert len(opportunity) == len(delivery) == 1
    assert control
    assert all(r.get("participation_decision") != "ERROR" for r in control)
    joined = opportunity + delivery + control
    assert all(r.get("candidate_id") == binding.candidate_id for r in joined)
    assert all(
        r.get("observation_binding", {}).get("opportunity_id")
        == binding.opportunity_id
        for r in joined
    )


def test_unexplained_candidate_mismatch_remains_typed_error(monkeypatch):
    candidate, producer_text, delivery_extra = _caller_contract_candidate(monkeypatch)
    binding = _binding(candidate)
    conflicting_extra = dict(delivery_extra)
    conflicting_extra["candidate_id"] = "a5dd36de402caff4"
    assert conflicting_extra["candidate_id"] != binding.candidate_id

    rows: list[dict] = []
    _record_terminal_control(
        monkeypatch,
        rows,
        binding=binding,
        producer_text=producer_text,
        delivery_extra=conflicting_extra,
    )
    assert any(
        r.get("participation_decision") == "ERROR"
        and str(r.get("reason", "")).startswith("control_record_error:ValueError")
        for r in rows
    ), rows


def test_no_binding_context_keeps_canonical_lane_identity(monkeypatch):
    candidate, producer_text, delivery_extra = _caller_contract_candidate(monkeypatch)
    rows: list[dict] = []
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(dict(row)))
    monkeypatch.setattr(g, "_action_count", 12)
    g._terminal_lane_controls.clear()
    g._stage_terminal_lane_control(
        "l3.contract",
        producer_text,
        feature_id="GT_CONTRACT_NATIVE",
        decision_site="mini_seam.contract.native_render",
        decision="APPLIED",
        reason="native_contract_render_selected",
    )
    g._record_terminal_lane_controls(
        "l3.contract",
        producer_text,
        "\nGT:shipped-bytes",
        "src/x.py",
        delivery_extra=delivery_extra,
    )
    control = [r for r in rows if r.get("layer") == "control.participation"]
    assert control
    assert all(r.get("participation_decision") != "ERROR" for r in control)
    assert all(r.get("candidate_id") == candidate.dedup_key for r in control)


def test_randomized_winner_assignment_precedes_delivery_outcome(monkeypatch):
    source, _producer_text, _delivery_extra = _caller_contract_candidate(monkeypatch)
    winner = SimpleNamespace(
        kind=source.kind,
        plane=g._GA_PLANE_GATEWAY,
        dedup_key=source.dedup_key,
        symbol="src/x.py",
        lineage=source.lineage,
    )
    binding = _binding(winner)
    rows: list[dict] = []
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_ss_shadow_on", lambda: True)
    monkeypatch.setattr(g, "_ss_shadow_rate", lambda: "0.25")
    monkeypatch.setattr(
        g, "_ledger_line_direct",
        lambda row: rows.append(dict(row)) or True,
    )
    plan = g._BatchDeliveryPlan(
        winner,
        lambda: g._ledger_line_direct({
            "layer": "test.delivery",
            "outcome": "delivered",
            "chars_delivered": 1,
        }),
        {"output": "base"},
        "\nGT:shipped-bytes",
        False,
        "deliver",
        {"shipped_suffix": "\nGT:shipped-bytes"},
    )
    state = {
        "contexts": {
            id(winner): {"producer_iteration": 7, "krel": "src/x.py"}
        },
        "prepared": {id(winner): {}},
        "rollbacks": {},
        "opportunity_bindings": {id(winner): binding},
    }
    result = SimpleNamespace(losers=[], repair_support=False)
    try:
        g._commit_batch_arbitration(
            state, result, winner, {id(winner): plan})
    finally:
        g._EPISODE.delivered_dedup.discard(winner.dedup_key)

    assignment = next(
        row for row in rows if row.get("layer") == "shadow.assignment")
    assignment_index = rows.index(assignment)
    delivery_index = next(
        index for index, row in enumerate(rows)
        if row.get("layer") == "test.delivery")
    assert assignment_index < delivery_index
    assert assignment["event_type"] == "policy_observation"
    assert assignment["outcome"] == "assigned"
    assert assignment["reason"] == "pre_outcome_randomization"
    assert assignment["assignment"] == "DELIVER"
    assert assignment["assignment_probability"] == 0.75
    assert assignment["holdout_rate"] == 0.25
    assert assignment["fact_class"] == "caller_contract"
    assert assignment["candidate_id"] == binding.candidate_id
    assert (
        assignment["observation_binding"]["opportunity_id"]
        == binding.opportunity_id
    )
    assert assignment["candidate_chars"] == len("\nGT:shipped-bytes")
    assert "payload" not in assignment and "content" not in assignment


def test_randomized_holdout_assignment_precedes_holdout_outcome(monkeypatch):
    source, _producer_text, _delivery_extra = _caller_contract_candidate(monkeypatch)
    winner = SimpleNamespace(
        kind=source.kind,
        plane=g._GA_PLANE_GATEWAY,
        dedup_key=source.dedup_key,
        symbol="src/x.py",
        lineage=source.lineage,
    )
    binding = _binding(winner)
    rows: list[dict] = []
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_ss_shadow_on", lambda: True)
    monkeypatch.setattr(g, "_ss_shadow_rate", lambda: "0.25")
    monkeypatch.setattr(
        g, "_ledger_line_direct",
        lambda row: rows.append(dict(row)) or True,
    )
    plan = g._BatchDeliveryPlan(
        winner,
        lambda: None,
        {"output": "base"},
        "",
        False,
        "holdout",
        {
            "payload": "GT:shipped-bytes",
            "shipped_suffix": "\nGT:shipped-bytes",
            "shadow_key": "content-key",
        },
    )
    state = {
        "contexts": {
            id(winner): {"producer_iteration": 7, "krel": "src/x.py"}
        },
        "prepared": {id(winner): {}},
        "rollbacks": {},
        "opportunity_bindings": {id(winner): binding},
    }
    result = SimpleNamespace(losers=[], repair_support=False)
    try:
        g._commit_batch_arbitration(
            state, result, winner, {id(winner): plan})
    finally:
        g._EPISODE.delivered_dedup.discard(winner.dedup_key)

    assignment_index = next(
        index for index, row in enumerate(rows)
        if row.get("layer") == "shadow.assignment")
    holdout_index = next(
        index for index, row in enumerate(rows)
        if row.get("outcome") == "shadow_holdout")
    assignment = rows[assignment_index]
    assert assignment_index < holdout_index
    assert assignment["assignment"] == "HOLDOUT"
    assert assignment["assignment_probability"] == 0.75
    assert assignment["candidate_id"] == binding.candidate_id
    assert "payload" not in assignment and "content" not in assignment


def test_safety_class_never_enters_randomized_assignment_cohort(monkeypatch):
    binding = build_observation_binding(
        batch_start_iteration=9,
        parent_policy_sha256="4" * 64,
        parent_policy_chars=64,
        action_batch_sha256="5" * 64,
        candidate_ordinal=0,
        candidate_kind="edit.syntax",
        candidate_id="b" * 16,
    )
    rows: list[dict] = []
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_ss_shadow_on", lambda: True)
    monkeypatch.setattr(g, "_ss_shadow_rate", lambda: "1")
    monkeypatch.setattr(
        g, "_ledger_line_direct",
        lambda row: rows.append(dict(row)) or True,
    )
    token = g._delivery_observation_context.set(binding)
    try:
        assert g._record_shadow_assignment(
            "edit.syntax",
            "syntax verdict",
            "DELIVER",
            fact_class="syntax_result",
            candidate_id=binding.candidate_id,
        ) is False
    finally:
        g._delivery_observation_context.reset(token)
    assert not [row for row in rows if row.get("layer") == "shadow.assignment"]


def test_untyped_candidate_never_enters_randomized_assignment_cohort(monkeypatch):
    binding = build_observation_binding(
        batch_start_iteration=9,
        parent_policy_sha256="4" * 64,
        parent_policy_chars=64,
        action_batch_sha256="5" * 64,
        candidate_ordinal=0,
        candidate_kind="post_search.localize",
        candidate_id="b" * 16,
    )
    rows: list[dict] = []
    monkeypatch.setattr(g, "_ss_shadow_on", lambda: True)
    monkeypatch.setattr(g, "_ss_shadow_rate", lambda: "0.5")
    monkeypatch.setattr(
        g, "_ledger_line_direct",
        lambda row: rows.append(dict(row)) or True,
    )
    token = g._delivery_observation_context.set(binding)
    try:
        assert not g._record_shadow_assignment(
            "post_search.localize", "candidate", "DELIVER"
        )
    finally:
        g._delivery_observation_context.reset(token)
    assert not [row for row in rows if row.get("layer") == "shadow.assignment"]


def test_receipts_sidecar_prefers_writable_out_surface(monkeypatch):
    monkeypatch.setenv("GT_C_OUT", "/gt_out")
    monkeypatch.setenv("GT_RUNTIME_LEDGER", "/gt_out/gt_runtime_ledger.jsonl")
    monkeypatch.setenv("GT_CERT_DIR", "/gt_artifacts")
    assert g._receipts_sidecar_path() == os.path.join("/gt_out", "gt_receipts.jsonl")

    monkeypatch.delenv("GT_C_OUT")
    assert g._receipts_sidecar_path() == os.path.join("/gt_out", "gt_receipts.jsonl")

    monkeypatch.delenv("GT_RUNTIME_LEDGER")
    assert g._receipts_sidecar_path() == os.path.join(
        "/gt_artifacts", "gt_receipts.jsonl"
    )

    monkeypatch.delenv("GT_CERT_DIR")
    assert g._receipts_sidecar_path() == ""
