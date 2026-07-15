from __future__ import annotations

import hashlib
from pathlib import Path

import gt_mini_patch as g


def _capture(monkeypatch) -> list[dict]:
    rows: list[dict] = []
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(dict(row)))
    g._terminal_lane_controls.clear()
    return rows


def _exact(kind: str, text: str, event=g.Event.TEST_RESULT) -> dict:
    return g._lane_delivery_extra(kind, text, "src/mod.py", event)


def test_terminal_lane_controls_bind_only_committed_exact_suffix(monkeypatch) -> None:
    rows = _capture(monkeypatch)
    produced = "break the repeated loop"
    shipped = "\n" + produced
    g._stage_terminal_lane_control(
        "detect.loop", produced,
        feature_id="GT_NUDGE_NATIVE",
        decision_site="mini_seam.nudge.native_render",
        decision="APPLIED",
        reason="native_contract_render",
    )

    # Producing/staging is not terminal evidence.
    assert rows == []
    g._record_terminal_lane_controls(
        "detect.loop", produced, shipped, "src/mod.py",
        delivery_extra=_exact("detect.loop", produced),
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["control_ref"]["feature_id"] == "GT_NUDGE_NATIVE"
    assert row["fact_class"] == "recovery"
    assert row["candidate_id"]
    assert row["candidate_chars"] == len(shipped)
    assert row["candidate_sha256_16"] == hashlib.sha256(shipped.encode()).hexdigest()[:16]


def test_terminal_lane_controls_do_not_cross_kind_or_uncommitted_candidate(monkeypatch) -> None:
    rows = _capture(monkeypatch)
    g._stage_terminal_lane_control(
        "l3b.evidence", "same bytes",
        feature_id="GT_EVIDENCE_NATIVE",
        decision_site="mini_seam.evidence.native_render",
        decision="APPLIED",
        reason="native_evidence_render",
    )

    g._record_terminal_lane_controls(
        "l3.contract", "same bytes", "same bytes", "src/mod.py",
    )
    assert rows == []


def test_terminal_lane_controls_emit_multiple_real_decisions_once(monkeypatch) -> None:
    rows = _capture(monkeypatch)
    produced = "recovery bytes"
    for feature, site, decision in (
        ("GT_NUDGE_NATIVE", "mini_seam.nudge.native_render", "NO_EFFECT"),
        ("GT_STEER_NATIVE", "mini_seam.steer.native_render", "APPLIED"),
    ):
        g._stage_terminal_lane_control(
            "detect.loop", produced,
            feature_id=feature, decision_site=site, decision=decision,
            reason="terminal_decision_reached",
        )

    g._record_terminal_lane_controls(
        "detect.loop", produced, produced, "src/mod.py",
        delivery_extra=_exact("detect.loop", produced),
    )
    g._record_terminal_lane_controls(
        "detect.loop", produced, produced, "src/mod.py",
        delivery_extra=_exact("detect.loop", produced),
    )

    assert [(r["control_ref"]["feature_id"], r["participation_decision"])
            for r in rows] == [
        ("GT_NUDGE_NATIVE", "NO_EFFECT"),
        ("GT_STEER_NATIVE", "APPLIED"),
    ]


def test_terminal_lane_control_staging_is_byte_neutral(monkeypatch) -> None:
    _capture(monkeypatch)
    produced = "unchanged model bytes"
    before = produced.encode()
    g._stage_terminal_lane_control(
        "post_search.localize", produced,
        feature_id="GT_POST_SEARCH_NATIVE",
        decision_site="mini_seam.post_search.native_render",
        decision="APPLIED",
        reason="native_search_render",
    )
    assert produced.encode() == before


def test_terminal_lane_control_loser_is_dropped_at_observation_boundary(
    monkeypatch,
) -> None:
    rows = _capture(monkeypatch)
    produced = "losing candidate"
    g._stage_terminal_lane_control(
        "l3.contract", produced,
        feature_id="GT_CONTRACT_NATIVE",
        decision_site="mini_seam.contract.native_render",
        decision="APPLIED",
        reason="native_contract_render",
    )

    g._discard_terminal_lane_controls({g._action_count})
    g._record_terminal_lane_controls(
        "l3.contract", produced, produced, "src/mod.py",
    )

    assert rows == []
    assert g._terminal_lane_controls == {}


def test_all_eight_native_lane_controls_stage_only_at_real_decisions(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def capture(kind: str, _text: str, **fields) -> None:
        calls.append((kind, fields["feature_id"]))

    monkeypatch.setattr(g, "_stage_terminal_lane_control", capture)
    g._stage_contract_terminal_controls(
        "contract", native=True, mode_on=True, mode_applied=False,
        bilateral_reached=True, bilateral_applied=False)

    monkeypatch.setenv("GT_POST_SEARCH_NATIVE", "1")
    g._fmt_def_facts(
        "symbol",
        {"def_sites": [("src/mod.py", 8)], "callers_render": "", "test_ref_count": 0},
        "repo",
    )

    monkeypatch.setenv("GT_EVIDENCE_NATIVE", "1")
    monkeypatch.setattr(g, "_classify", lambda _cmd: ("post_view", "src/mod.py"))
    monkeypatch.setattr(g, "_root", lambda: "repo")
    monkeypatch.setattr(g, "_to_repo_rel", lambda path, _root: path)
    monkeypatch.setattr(g, "_evidence_body", lambda *_args: "tagged evidence")
    monkeypatch.setattr(g, "_evidence_native", lambda _lines: "native evidence")
    monkeypatch.setattr(g, "_budget_trim", lambda text: text)
    monkeypatch.setattr(g, "_inseam_eligible", lambda *_args: None)
    monkeypatch.setattr(g, "_inseam_stamp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(g, "_arm_lane_a_rearm", lambda *_args: None)
    monkeypatch.setattr(g, "_seen", set())
    assert g._evidence("view src/mod.py")

    monkeypatch.setenv("GT_SCOPE_NATIVE", "1")
    monkeypatch.setattr(g, "_consensus_scope", {"src/mod.py"})
    monkeypatch.setattr(
        g, "_query_scope", lambda _rel: ["src/mod.py", "src/peer.py"])
    monkeypatch.setattr(g, "_consensus_scope_native", lambda _neigh: "scope")
    assert g._consensus_scope_block("src/mod.py") == "scope"

    monkeypatch.setenv("GT_NUDGE_NATIVE", "1")
    g._nudge_native(
        "\n<gt-nudge>\nGT: change course\n</gt-nudge>", kind="detect.loop")
    g._steer_native(
        "\n<gt-nudge>\nGT: verify now\n</gt-nudge>", kind="recovery")

    assert {feature for _kind, feature in calls} == {
        "GT_CONTRACT_BILATERAL",
        "GT_CONTRACT_MODE",
        "GT_CONTRACT_NATIVE",
        "GT_EVIDENCE_NATIVE",
        "GT_NUDGE_NATIVE",
        "GT_POST_SEARCH_NATIVE",
        "GT_SCOPE_NATIVE",
        "GT_STEER_NATIVE",
    }


def _lane_filter_harness(monkeypatch) -> list[dict]:
    rows = _capture(monkeypatch)
    monkeypatch.setenv("GT_SS_PROVENANCE", "1")
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setattr(g, "_root", lambda: "repo")
    monkeypatch.setattr(g, "_record_hook_fire", lambda *_args: None)
    monkeypatch.setattr(g, "_payload_leaks_test_identity", lambda _text: False)
    monkeypatch.setattr(g, "_ss_screen_delivery", lambda *_args, **_kwargs: (False, ""))
    monkeypatch.setattr(g, "_oracle_content_hash", lambda text: "state:" + text)
    monkeypatch.setattr(g, "_oracle_delivered_hashes", set())
    monkeypatch.setattr(g, "_lane_fits_budget", lambda _text: True)
    monkeypatch.setattr(g, "_ss_shadow_withheld", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(g, "_ledger_note_delivery", lambda *_args: None)
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **_kwargs: None)
    monkeypatch.setattr(g, "_record_lane_provenance_control", lambda *_args: None)
    monkeypatch.setattr(g, "_ss_record_delivered", lambda *_args: None)

    monkeypatch.setattr(g, "_seal_lane_delivery", lambda *_args, **_kwargs: None)
    return rows


def test_lane_provenance_rewrite_matches_original_stage_to_final_bytes(
    monkeypatch,
) -> None:
    rows = _lane_filter_harness(monkeypatch)
    original = "src/mod.py:8: caller\n/tmp/scratch.py:2: noise"
    final = "src/mod.py:8: caller"
    monkeypatch.setattr(g, "_ss_provenance_filter", lambda *_args: final)
    g._stage_terminal_lane_control(
        "obligation.unexercised", original,
        feature_id="GT_NUDGE_NATIVE",
        decision_site="mini_seam.nudge.native_render",
        decision="APPLIED",
        reason="native_contract_render",
    )

    out: dict = {}
    g._lane_a_deliver(
        out, "edit", [("obligation.unexercised", original, "src/mod.py")],
        krel="src/mod.py", event=None)

    assert out["output"] == final
    native = [row for row in rows
              if row.get("control_ref", {}).get("feature_id") == "GT_NUDGE_NATIVE"]
    assert len(native) == 1
    assert native[0]["candidate_chars"] == len(final)
    assert native[0]["candidate_sha256_16"] == hashlib.sha256(
        final.encode()).hexdigest()[:16]


def test_lane_provenance_zero_content_suppression_cannot_attribute(
    monkeypatch,
) -> None:
    rows = _lane_filter_harness(monkeypatch)
    original = "/tmp/scratch.py:2: noise"
    monkeypatch.setattr(g, "_ss_provenance_filter", lambda *_args: "")
    g._stage_terminal_lane_control(
        "obligation.unexercised", original,
        feature_id="GT_NUDGE_NATIVE",
        decision_site="mini_seam.nudge.native_render",
        decision="APPLIED",
        reason="native_contract_render",
    )

    out: dict = {}
    g._lane_a_deliver(
        out, "edit", [("obligation.unexercised", original, "src/mod.py")],
        krel="src/mod.py", event=None)

    assert (out.get("output") or "") == ""
    assert not any(row.get("control_ref", {}).get("feature_id") == "GT_NUDGE_NATIVE"
                   for row in rows)


def test_terminal_control_requires_exact_delivery_lineage(monkeypatch) -> None:
    rows = _capture(monkeypatch)
    produced = "break the repeated loop"
    g._stage_terminal_lane_control(
        "detect.loop", produced,
        feature_id="GT_NUDGE_NATIVE",
        decision_site="mini_seam.nudge.native_render",
        decision="APPLIED",
        reason="native_nudge_render",
    )
    exact = g._lane_delivery_extra(
        "detect.loop", produced, "src/mod.py", g.Event.TEST_RESULT)

    g._record_terminal_lane_controls(
        "detect.loop", produced, produced, "src/mod.py",
        delivery_extra=exact,
    )

    assert len(rows) == 1
    assert rows[0]["fact_class"] == "recovery"
    assert rows[0]["candidate_id"] == exact["candidate_id"]


def test_terminal_control_cannot_infer_authority_from_arbiter_class(
    monkeypatch,
) -> None:
    rows = _capture(monkeypatch)
    produced = "src/mod.py:8: caller"
    g._stage_terminal_lane_control(
        "l3.contract", produced,
        feature_id="GT_CONTRACT_NATIVE",
        decision_site="mini_seam.contract.native_render",
        decision="APPLIED",
        reason="native_contract_render",
    )

    g._record_terminal_lane_controls(
        "l3.contract", produced, produced, "src/mod.py",
        delivery_extra=g._lane_delivery_extra(
            "l3.contract", produced, "src/mod.py", g.Event.POST_EDIT),
    )

    assert rows == []


def test_steer_terminal_commit_precedes_delivery_and_collector_joins(
    monkeypatch,
) -> None:
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "swebench"
    monkeypatch.syspath_prepend(str(scripts))
    import gt_feature_metrics as metrics

    rows = _capture(monkeypatch)
    monkeypatch.setenv("GT_STEER_NATIVE", "1")
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setattr(g, "_ledger_note_delivery", lambda *_args: None)
    monkeypatch.setattr(g, "_record_hook_fire", lambda *_args: None)
    monkeypatch.setattr(g, "_ss_record_delivered", lambda *_args: None)
    monkeypatch.setattr(g, "_persist_receipt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        g, "_persist_lane_producer_attestation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(g, "_gt_gateway_chain_head", "")
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [])
    g._EPISODE.delivered_dedup.clear()

    out = {"output": "native tool output"}
    produced = g._steer_native(
        "break the repeated loop", kind="detect.loop")
    g._commit_prepared_steer(
        out, "pytest", "detect.loop", "",
        {"payload": produced, "shipped_suffix": "\n" + produced},
        krel="src/mod.py", kf="", event=g.Event.TEST_RESULT,
        steer_base="native tool output",
    )

    shipped = "\nbreak the repeated loop"
    control_index = next(
        index for index, row in enumerate(rows)
        if row.get("control_ref", {}).get("feature_id") == "GT_STEER_NATIVE")
    delivery_index = next(
        index for index, row in enumerate(rows)
        if row.get("layer") == "detect.loop" and row.get("outcome") == "delivered")
    assert control_index < delivery_index
    control = rows[control_index]
    delivery = rows[delivery_index]
    sealed = g._gt_gateway_deliveries[-1]
    assert control["candidate_id"] == delivery["candidate_id"] == sealed.dedup_key
    assert control["candidate_chars"] == delivery["chars_delivered"] == len(shipped)
    assert control["candidate_sha256_16"] == delivery["content_sha256_16"]

    consumption = {"entries": [{
        "source": "trajectory", "joined": True, "join_method": "seal",
        "content_sha256_16": delivery["content_sha256_16"],
        "ledger_chars": len(shipped), "ledger_layer": "detect.loop",
        "msg_index": 0, "receipt": 1,
    }]}
    evidence = metrics._control_participation_evidence(
        rows, [{"role": "tool", "content": out["output"]}], consumption)
    joins = evidence["joins"]["GT_STEER_NATIVE"]
    assert len(joins) == 1
    assert joins[0]["delivery_row_index"] == delivery_index
    assert joins[0]["observation_joined"] is True


def test_lane_envelope_uses_covering_lineage_not_arbiter_class(
    monkeypatch,
) -> None:
    rows = _capture(monkeypatch)
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setattr(
        g, "_last_verify_executed_identity",
        ("covering_runner", "covering_red", "test_result"))
    monkeypatch.setattr(g, "_persist_receipt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        g, "_persist_lane_producer_attestation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(g, "_gt_gateway_chain_head", "")
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [])
    g._EPISODE.delivered_dedup.clear()
    text = "covering RED"
    extra = g._lane_delivery_extra(
        "verify.horizon.executed", text, "src/mod.py",
        g.Event.REVIEW_TRANSITION)

    g._seal_lane_delivery(
        "verify.horizon.executed", text, "src/mod.py",
        delivery_extra=extra)

    lane = next(row for row in rows
                if row.get("control_ref", {}).get("feature_id") == "GT_LANE_ENVELOPE")
    assert lane["fact_class"] == "covering_red"
    assert lane["candidate_id"] == extra["candidate_id"]
    assert all(row.get("fact_class") != "executed_world_fact" for row in rows)
