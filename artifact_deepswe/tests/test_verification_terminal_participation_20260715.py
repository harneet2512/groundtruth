from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import gt_mini_patch as g
from groundtruth.runtime import covering_runner, native_render, verification_plan


@dataclass
class _Result:
    kind: str
    verdict: str
    detail: dict
    attribution_satisfied: bool = False
    covered_entities: tuple[str, ...] = ()


@dataclass
class _Green:
    status: str


def _capture(monkeypatch) -> list[dict]:
    rows: list[dict] = []
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setenv("GT_LANE_ENVELOPE", "1")
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_action_count", 7)
    monkeypatch.setattr(g, "_last_test_step", None)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(dict(row)))
    monkeypatch.setattr(g, "_persist_receipt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        g, "_persist_lane_producer_attestation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(g, "_gt_gateway_chain_head", "")
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [])
    monkeypatch.setattr(g, "_ledger_note_delivery", lambda *_args: None)
    monkeypatch.setattr(g, "_record_hook_fire", lambda *_args: None)
    monkeypatch.setattr(g, "_ss_record_delivered", lambda *_args: None)
    g._terminal_lane_controls.clear()
    g._EPISODE.delivered_dedup.clear()
    return rows


def _covering_red(tmp_path: Path, monkeypatch) -> str:
    test_file = tmp_path / "covering.py"
    test_file.write_text("pass\n", encoding="utf-8")
    edited = tmp_path / "src" / "widget.py"
    edited.parent.mkdir()
    edited.write_text("def widget():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_build_verification_executor", lambda: None)
    monkeypatch.setattr(
        covering_runner,
        "run_covering_tests",
        lambda *_args, **_kwargs: {"verdict": "fail", "ran": ["covering.py"]},
    )
    monkeypatch.setattr(
        covering_runner,
        "attribute_covering_red",
        lambda *_args, **_kwargs: covering_runner.CoveringAttribution(
            attributed=True,
            method="differential",
            current_verdict="fail",
            base_verdict="pass",
            implicated_edited_paths=("src/widget.py",),
            covering_files=("covering.py",),
        ),
    )
    monkeypatch.setattr(
        native_render,
        "render_covering_failure_native",
        lambda *_args, **_kwargs: "covering failure",
    )
    block = g._executed_covering_emission(
        [{"file": "covering.py"}], {"src/widget.py"}, {"widget"})
    assert block == "covering failure"
    return block


def _plan(monkeypatch, results: list[_Result]) -> None:
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    monkeypatch.setattr(g, "_root", lambda: "/repo")
    monkeypatch.setattr(g, "_db_path", lambda: "/repo/graph.db")
    monkeypatch.setattr(g, "_obligation_symbol_set", lambda: set())
    monkeypatch.setattr(g, "_build_verification_executor", lambda: None)
    monkeypatch.setattr(g, "_build_edit_check_executor", lambda: None)
    monkeypatch.setattr(
        verification_plan,
        "build_verification_plan",
        lambda *_args, **_kwargs: verification_plan.VerificationPlan(
            patch_revision="p",
            graph_revision="g",
            changed_entities=("widget",),
            obligations=(),
            checks=(),
            edited_files=("src/widget.py",),
        ),
    )
    monkeypatch.setattr(
        verification_plan, "run_plan", lambda *_args, **_kwargs: results)
    monkeypatch.setattr(
        verification_plan,
        "green",
        lambda result, _plan: _Green(
            "red" if result.verdict in {"fail", "syntax_error"} else "green"),
    )


def test_covering_execute_stages_only_exact_deliverable_red(
    tmp_path: Path, monkeypatch,
) -> None:
    _capture(monkeypatch)
    block = _covering_red(tmp_path, monkeypatch)

    staged = g._terminal_lane_controls[
        g._terminal_lane_control_key("verify.horizon.executed", block)]
    assert staged == [(
        "GT_VERIFY_EXECUTE",
        "mini_seam.verification.execution",
        "APPLIED",
        "attributed_covering_red_selected",
    )]
    assert g._last_verify_executed_identity == (
        "covering_runner", "covering_red", "test_result")


def test_plan_syntax_and_unit_bind_to_their_real_producers(monkeypatch) -> None:
    _capture(monkeypatch)
    monkeypatch.setattr(g, "_last_covering_candidate_input", object())
    syntax = {"verdict": "syntax_error", "file": "src/widget.py"}
    _plan(monkeypatch, [_Result("syntax", "syntax_error", {"per_file": [syntax]})])
    monkeypatch.setattr(
        native_render, "render_syntax_error_native", lambda _result: "syntax failure")

    block = g._verification_plan_emission({"src/widget.py"}, {"widget"})

    assert block == "syntax failure"
    assert g._last_verify_executed_identity == (
        "edit_check", "syntax_result", "edit_result")
    assert g._last_covering_candidate_input is None
    assert g._terminal_lane_controls[
        g._terminal_lane_control_key("verify.horizon.executed", block)] == [
        (
            "GT_VERIFY_EXECUTE",
            "mini_seam.verification.execution",
            "APPLIED",
            "verification_plan_syntax_red_selected",
        ),
        (
            "GT_VERIFICATION_PLAN",
            "mini_seam.verification.plan_selection",
            "APPLIED",
            "progressive_syntax_red_selected",
        ),
    ]

    g._terminal_lane_controls.clear()
    monkeypatch.setattr(g, "_last_covering_candidate_input", object())
    unit = {"verdict": "fail", "ran": ["covering.py"]}
    _plan(monkeypatch, [
        _Result("unit", "fail", unit, True, ("widget",)),
    ])
    monkeypatch.setattr(
        native_render,
        "render_covering_failure_native",
        lambda *_args, **_kwargs: "unit failure",
    )
    block = g._verification_plan_emission({"src/widget.py"}, {"widget"})

    assert block == "unit failure"
    assert g._last_verify_executed_identity == (
        "covering_runner", "covering_red", "test_result")
    assert g._last_covering_candidate_input is None
    assert {item[0] for item in g._terminal_lane_controls[
        g._terminal_lane_control_key("verify.horizon.executed", block)]} == {
        "GT_VERIFY_EXECUTE", "GT_VERIFICATION_PLAN"}


def test_verification_controls_do_not_stage_green_unattributed_or_leaky(
    monkeypatch,
) -> None:
    _capture(monkeypatch)
    _plan(monkeypatch, [_Result("syntax", "ok", {"per_file": []})])
    assert g._verification_plan_emission({"src/widget.py"}, {"widget"}) is None
    assert g._terminal_lane_controls == {}

    _plan(monkeypatch, [_Result("unit", "fail", {"verdict": "fail"}, False)])
    assert g._verification_plan_emission({"src/widget.py"}, {"widget"}) is None
    assert g._terminal_lane_controls == {}

    syntax = {"verdict": "syntax_error", "file": "src/widget.py"}
    _plan(monkeypatch, [_Result("syntax", "syntax_error", {"per_file": [syntax]})])
    monkeypatch.setattr(
        native_render, "render_syntax_error_native", lambda _result: "<gt-leak>")
    assert g._verification_plan_emission({"src/widget.py"}, {"widget"}) is None
    assert g._terminal_lane_controls == {}


def test_verification_control_flags_off_are_byte_and_stage_neutral(
    monkeypatch,
) -> None:
    _capture(monkeypatch)
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    monkeypatch.setattr(
        g,
        "_stage_verification_terminal_controls",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("flag-off path staged a control")),
    )

    assert g._executed_covering_emission([], set(), set()) is None
    assert g._verification_plan_emission({"src/widget.py"}, {"widget"}) is None
    assert g._terminal_lane_controls == {}


def test_covering_control_commits_exact_final_suffix_and_collector_join(
    tmp_path: Path, monkeypatch,
) -> None:
    rows = _capture(monkeypatch)
    block = _covering_red(tmp_path, monkeypatch)
    out = {"output": "tool observation"}
    shipped = "\n" + block
    g._commit_prepared_steer(
        out,
        "pytest",
        "verify.horizon.executed",
        "",
        {"payload": block, "shipped_suffix": shipped},
        krel="src/widget.py",
        kf="",
        event=g.Event.TEST_RESULT,
        steer_base="tool observation",
    )

    control = next(
        row for row in rows
        if row.get("control_ref", {}).get("feature_id") == "GT_VERIFY_EXECUTE")
    delivery = next(
        row for row in rows
        if row.get("layer") == "verify.horizon.executed"
        and row.get("outcome") == "delivered")
    assert control["fact_class"] == "covering_red"
    assert control["candidate_id"] == delivery["candidate_id"]
    assert control["candidate_chars"] == len(shipped)
    assert control["candidate_sha256_16"] == hashlib.sha256(
        shipped.encode()).hexdigest()[:16]

    scripts = Path(__file__).resolve().parents[2] / "scripts" / "swebench"
    monkeypatch.syspath_prepend(str(scripts))
    import gt_feature_metrics as metrics

    evidence = metrics._control_participation_evidence(
        rows,
        [{"role": "tool", "content": out["output"]}],
        {"entries": [{
            "source": "trajectory",
            "joined": True,
            "join_method": "seal",
            "content_sha256_16": delivery["content_sha256_16"],
            "ledger_chars": len(shipped),
            "ledger_layer": "verify.horizon.executed",
            "msg_index": 0,
            "receipt": 1,
        }]},
    )
    joins = evidence["joins"]["GT_VERIFY_EXECUTE"]
    assert len(joins) == 1
    assert joins[0]["observation_joined"] is True


def test_plan_syntax_commits_both_controls_to_exact_delivery(monkeypatch) -> None:
    rows = _capture(monkeypatch)
    syntax = {"verdict": "syntax_error", "file": "src/widget.py"}
    _plan(monkeypatch, [_Result("syntax", "syntax_error", {"per_file": [syntax]})])
    monkeypatch.setattr(
        native_render, "render_syntax_error_native", lambda _result: "syntax failure")
    block = g._verification_plan_emission({"src/widget.py"}, {"widget"})
    assert block == "syntax failure"

    out = {"output": "edit observation"}
    shipped = "\n" + block
    g._commit_prepared_steer(
        out,
        "edit",
        "verify.horizon.executed",
        "",
        {"payload": block, "shipped_suffix": shipped},
        krel="src/widget.py",
        kf="",
        event=g.Event.POST_EDIT,
        steer_base="edit observation",
    )

    delivery = next(
        row for row in rows
        if row.get("layer") == "verify.horizon.executed"
        and row.get("outcome") == "delivered")
    controls = {
        row["control_ref"]["feature_id"]: row
        for row in rows
        if row.get("control_ref", {}).get("feature_id") in {
            "GT_VERIFY_EXECUTE", "GT_VERIFICATION_PLAN"
        }
    }
    assert set(controls) == {"GT_VERIFY_EXECUTE", "GT_VERIFICATION_PLAN"}
    for control in controls.values():
        assert control["fact_class"] == "syntax_result"
        assert control["candidate_id"] == delivery["candidate_id"]
        assert control["candidate_chars"] == len(shipped)
        assert control["candidate_sha256_16"] == delivery["content_sha256_16"]


def test_plan_output_cannot_persist_stale_direct_covering_truth(monkeypatch) -> None:
    _capture(monkeypatch)
    stale = object()
    monkeypatch.setattr(g, "_last_covering_candidate_input", stale)
    syntax = {"verdict": "syntax_error", "file": "src/widget.py"}
    _plan(monkeypatch, [_Result("syntax", "syntax_error", {"per_file": [syntax]})])
    monkeypatch.setattr(
        native_render, "render_syntax_error_native", lambda _result: "syntax failure")
    block = g._verification_plan_emission({"src/widget.py"}, {"widget"})
    assert block == "syntax failure"
    assert g._last_covering_candidate_input is None

    finalized: list[object] = []
    monkeypatch.setattr(
        "groundtruth.runtime.lane_attestation.finalize_covering_attestation",
        lambda candidate, **_kwargs: finalized.append(candidate),
    )
    g._persist_lane_producer_attestation(
        "verify.horizon.executed",
        "src/widget.py",
        block,
        "\n" + block,
        "candidate",
        "seal",
    )
    assert finalized == []


def test_profile_native_transform_retargets_verification_control_to_final_suffix(
    monkeypatch,
) -> None:
    rows = _capture(monkeypatch)
    tagged = "\n<gt-verify>\nGT: fix the covering failure\n</gt-verify>"
    g._stage_verification_terminal_controls(
        tagged, evidence_type="covering_red", verification_plan=False)

    native = g._steer_native(tagged, kind="verify.horizon.executed")

    assert native == "\nfix the covering failure\n"
    assert g._terminal_lane_control_key(
        "verify.horizon.executed", tagged) not in g._terminal_lane_controls
    final_key = g._terminal_lane_control_key("verify.horizon.executed", native)
    assert any(
        descriptor[0] == "GT_VERIFY_EXECUTE"
        for descriptor in g._terminal_lane_controls[final_key]
    )

    monkeypatch.setattr(
        g,
        "_last_verify_executed_identity",
        ("covering_runner", "covering_red", "test_result"),
    )
    out = {"output": "tool observation"}
    g._commit_prepared_steer(
        out,
        "pytest",
        "verify.horizon.executed",
        "",
        {"payload": native, "shipped_suffix": native},
        krel="src/widget.py",
        kf="",
        event=g.Event.TEST_RESULT,
        steer_base="tool observation",
    )

    control = next(
        row for row in rows
        if row.get("control_ref", {}).get("feature_id") == "GT_VERIFY_EXECUTE")
    delivery = next(
        row for row in rows
        if row.get("layer") == "verify.horizon.executed"
        and row.get("outcome") == "delivered")
    assert control["candidate_chars"] == len(native)
    assert control["candidate_sha256_16"] == delivery["content_sha256_16"]
    assert control["candidate_id"] == delivery["candidate_id"]
