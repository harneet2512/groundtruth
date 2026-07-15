from __future__ import annotations

import hashlib

import gt_mini_patch as g


def _capture(monkeypatch) -> list[dict]:
    rows: list[dict] = []
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(dict(row)))
    g._terminal_lane_controls.clear()
    return rows


def test_terminal_lane_controls_bind_only_committed_exact_suffix(monkeypatch) -> None:
    rows = _capture(monkeypatch)
    produced = "src/mod.py:8: caller"
    shipped = "\n" + produced
    g._stage_terminal_lane_control(
        "l3.contract", produced,
        feature_id="GT_CONTRACT_NATIVE",
        decision_site="mini_seam.contract.native_render",
        decision="APPLIED",
        reason="native_contract_render",
    )

    # Producing/staging is not terminal evidence.
    assert rows == []
    g._record_terminal_lane_controls(
        "l3.contract", produced, shipped, "src/mod.py",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["control_ref"]["feature_id"] == "GT_CONTRACT_NATIVE"
    assert row["fact_class"] == "caller_contract"
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
    produced = "contract bytes"
    for feature, site, decision in (
        ("GT_CONTRACT_MODE", "mini_seam.contract.mode_selection", "NO_EFFECT"),
        ("GT_CONTRACT_BILATERAL", "mini_seam.contract.bilateral_selection", "APPLIED"),
    ):
        g._stage_terminal_lane_control(
            "l3.contract", produced,
            feature_id=feature, decision_site=site, decision=decision,
            reason="terminal_decision_reached",
        )

    g._record_terminal_lane_controls(
        "l3.contract", produced, produced, "src/mod.py",
    )
    g._record_terminal_lane_controls(
        "l3.contract", produced, produced, "src/mod.py",
    )

    assert [(r["control_ref"]["feature_id"], r["participation_decision"])
            for r in rows] == [
        ("GT_CONTRACT_MODE", "NO_EFFECT"),
        ("GT_CONTRACT_BILATERAL", "APPLIED"),
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
