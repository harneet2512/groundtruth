"""Delivery engine STAGE 2 — review-transition obligation-STATUS emission
(red->green).  The #1 lever from DEEP_TRAJECTORY_ANALYSIS_ORACLE_RUN §3.3:
"review-transition unmet-obligation emission … would have intersected the
killing defect in 7-8 of 9 tasks; every trajectory HAD the event; every
trajectory got silence."

Contract verified here:
  - per-obligation status: [✓ edited, ✗ untested] / [✗ not addressed] /
    tested (summarized) — recomputed statelessly from edit/test evidence;
  - fires at EVERY review transition whose status VECTOR is new — NOT
    once-and-done (the awilix h=0e1a89ba one-shot defect closed);
  - same vector -> suppressed; vector changed by the agent's own progress ->
    re-fires with the updated checklist;
  - covering test named per untested obligation (graph.db, FACT-tier);
  - severity is a COMPUTED composite: base + 2*budget_fraction +
    1*unmet_ratio (Zilberstein 1996 contract-algorithm urgency form);
  - correct-or-quiet: no obligations / nothing edited-untested / all tested
    -> silence.

Mandatory properties:
  DYNAMIC — dose bounded by the agent's own status changes (no cap constant);
            severity scales with budget + unmet mass.
  HYBRID  — trigger composites 4 signals (review transition ∧ edited-untested
            present ∧ unmet exists ∧ vector unseen).
  RESEARCH-BACKED — TRAJEVAL once-per-context dosing; Wink class-keyed
            corrective text; Rothermel/Ekstazi covering-test selection.

RED before Stage 2: gt_oracle has no obligation_statuses/render_obligation_
status_block; the live producer fired once per task with a single-symbol
nudge and went silent for the rest of the run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"
_ORACLE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle.py"


def _load(path: Path, name: str):
    prev = os.environ.get("GT_BASELINE")
    os.environ["GT_BASELINE"] = "1"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if prev is None:
            os.environ.pop("GT_BASELINE", None)
        else:
            os.environ["GT_BASELINE"] = prev


@pytest.fixture(scope="module")
def oracle_mod():
    return _load(_ORACLE_PATH, "gt_oracle_stage2_status")


@pytest.fixture(scope="module")
def patch_mod():
    return _load(_PATCH_PATH, "gt_mini_patch_stage2_status")


_OBLS = [
    {
        "verbatim_text": "Multi-key conflicts raise ExtraFieldsLoadError.",
        "kind": "behavior",
        "symbols": ["ExtraFieldsLoadError"],
        "keywords": ["raise"],
        "checkable_forms": [],
    },
    {
        "verbatim_text": "The capture_snapshot method should be async.",
        "kind": "behavior",
        "symbols": ["capture_snapshot"],
        "keywords": ["async"],
        "checkable_forms": ["async"],
    },
    {
        "verbatim_text": "Eviction preserves named snapshots via max_snapshots.",
        "kind": "behavior",
        "symbols": ["max_snapshots"],
        "keywords": ["preserve"],
        "checkable_forms": [],
    },
]


# ---------------------------------------------------------------------------
# Pure machinery (gt_oracle) — statuses, hash, severity, renderer.
# ---------------------------------------------------------------------------
class TestStatusMachinery:
    def test_statuses_three_way(self, oracle_mod):
        views = oracle_mod._obligation_views(_OBLS)
        sts = oracle_mod.obligation_statuses(
            views,
            edited_tokens={"capture_snapshot", "noise"},
            tested_tokens={"test_max_snapshots", "passed"},
        )
        by_idx = {v.idx: s for v, s, _t, _c in sts}
        assert by_idx[0] == oracle_mod.OBL_UNADDRESSED
        assert by_idx[1] == oracle_mod.OBL_EDITED_UNTESTED
        assert by_idx[2] == oracle_mod.OBL_TESTED  # compound containment

    def test_status_vector_hash_changes_with_status(self, oracle_mod):
        views = oracle_mod._obligation_views(_OBLS)
        a = oracle_mod.obligation_statuses(views, {"capture_snapshot"}, set())
        b = oracle_mod.obligation_statuses(views, {"capture_snapshot"}, {"test_capture_snapshot"})
        assert oracle_mod.status_vector_hash(a) != oracle_mod.status_vector_hash(b)

    def test_composite_severity_monotonic(self, oracle_mod):
        s_early = oracle_mod.composite_severity(5, 0.1, 0.3)
        s_late = oracle_mod.composite_severity(5, 0.9, 0.3)
        s_more_unmet = oracle_mod.composite_severity(5, 0.1, 1.0)
        assert s_late > s_early
        assert s_more_unmet > s_early
        # the exact contract-algorithm form: base + 2B + 1*unmet
        assert abs(oracle_mod.composite_severity(5, 0.5, 0.5) - 6.5) < 1e-9

    def test_renderer_lists_statuses_and_covering_test(self, oracle_mod):
        views = oracle_mod._obligation_views(_OBLS)
        sts = oracle_mod.obligation_statuses(views, {"capture_snapshot"}, {"test_max_snapshots"})
        covering = {
            1: {
                "name": "test_capture",
                "file": "tests/test_m.py",
                "run_cmd": "pytest tests/test_m.py::test_capture",
            }
        }
        block = oracle_mod.render_obligation_status_block(sts, covering)
        assert 'reason="test_evidence_gap"' in block
        assert "[edited, untested]" in block
        assert "[not addressed]" in block
        assert "The capture_snapshot method should be async." in block
        assert "Multi-key conflicts raise ExtraFieldsLoadError." in block
        assert "pytest tests/test_m.py::test_capture" not in block
        assert "test_capture" not in block
        assert "tests/test_m.py" not in block
        # the tested obligation is summarized, never re-listed as unmet
        assert "1 requirement(s) already show test evidence." in block
        # edited-untested rows sort BEFORE unaddressed rows
        assert block.index("[edited, untested]") < block.index("[not addressed]")

    def test_renderer_quiet_when_all_tested(self, oracle_mod):
        views = oracle_mod._obligation_views(_OBLS)
        sts = oracle_mod.obligation_statuses(
            views,
            {"capture_snapshot"},
            {"ExtraFieldsLoadError", "test_capture_snapshot", "test_max_snapshots"},
        )
        assert oracle_mod.render_obligation_status_block(sts) == ""

    def test_renderer_lists_all_unmet_rows(self, oracle_mod):
        obls = [
            {
                "verbatim_text": f"Requirement number {i} must hold for api_{i}.",
                "kind": "behavior",
                "symbols": [f"api_sym_{i}"],
                "keywords": [],
                "checkable_forms": [],
            }
            for i in range(9)
        ]
        views = oracle_mod._obligation_views(obls)
        sts = oracle_mod.obligation_statuses(views, {"api_sym_0"}, set())
        block = oracle_mod.render_obligation_status_block(sts)
        assert "more unverified requirement" not in block
        for i in range(9):
            assert f"Requirement number {i} must hold" in block


# ---------------------------------------------------------------------------
# Live path — re-fire on status change; suppression on same vector.
# ---------------------------------------------------------------------------
def _write_anchors(tmp_path: Path, obligations: list[dict]) -> str:
    p = tmp_path / "gt_issue_anchors.json"
    p.write_text(json.dumps({"obligations": obligations}), encoding="utf-8")
    return str(p)


def _reset(patch_mod, monkeypatch):
    for name, val in [
        ("_GT_BASELINE", False),
        ("_ORACLE_ROUTE", True),
        ("_marker_sent", True),
        ("_action_count", 0),
        ("_source_edit_count", 0),
        ("_cmd_history", []),
        ("_l5_fired", False),
        ("_l5_failure_fired", False),
        ("_l5_notest_fired", False),
        ("_test_fail_history", []),
        ("_blind_test_runs", 0),
        ("_test_evidence_seen", False),
        ("_seen", set()),
        ("_contract_seen", set()),
        ("_consensus_fired", False),
        ("_consensus_scope", set()),
        ("_offscope_views", 0),
        ("_cochange_fired", False),
        ("_oracle_focus_cache", None),
        ("_oracle_delivered_hashes", set()),
        ("_oracle_edited_rels", set()),
        ("_oracle_nonedit_streak", 0),
        ("_oracle_review_fired", False),
        ("_oracle_last_losers", set()),
        ("_oracle_obligation_fired", False),
        ("_oblig_status_emitted", set()),
        ("_oblig_status_last_hash", None),
        ("_oracle_edited_tokens", set()),
        ("_oracle_tested_tokens", set()),
        ("_oracle_edited_tokens_by_file", {}),
        ("_edit_churn", {}),
        ("_gt_oracle_tried", False),
        ("_gt_oracle_mod", None),
        ("_horizon_advisory_fired", False),
        ("_horizon_gate_fire_count", 0),
        ("_DELIVERED_FACTS", set()),
        ("_ledger_consumed_kinds", set()),
        ("_ledger_ignore_counts", {}),
    ]:
        monkeypatch.setattr(patch_mod, name, val, raising=False)


def _drive(patch_mod, cmd, obs=""):
    out = {"output": obs, "returncode": 0}
    patch_mod._augment_output({"command": cmd}, out)
    return out.get("output") or ""


_EDIT_SNAP = "sed -i 's/def capture_snapshot/async def capture_snapshot/' pkg/mod.py"
_EDIT_ERR = "sed -i 's/pass/raise ExtraFieldsLoadError()/' pkg/loader_gen.py"
_NONEDITS = ["git status", "git diff", "ls -la"]


def test_refires_when_status_vector_changes(patch_mod, tmp_path, monkeypatch):
    """THE Stage-2 red->green: a changed status vector RE-FIRES the checklist
    at the next review transition (the prior build was once-and-done)."""
    _reset(patch_mod, monkeypatch)
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, _OBLS))
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))

    outs = [_drive(patch_mod, _EDIT_SNAP)]
    for c in _NONEDITS:
        outs.append(_drive(patch_mod, c))
    first = "\n".join(outs)
    assert 'reason="test_evidence_gap"' in first
    assert "[edited, untested]" in first

    # the agent now edits a SECOND obligation surface -> vector changes.
    outs2 = [_drive(patch_mod, _EDIT_ERR)]
    for c in _NONEDITS:
        outs2.append(_drive(patch_mod, c))
    second = "\n".join(outs2)
    assert 'reason="test_evidence_gap"' in second, (
        "a CHANGED status vector must re-fire at the next review transition"
    )
    # distinct vectors -> distinct h attributes.
    h1 = re.findall(r'h="([0-9a-f]+)"', first)
    h2 = re.findall(r'h="([0-9a-f]+)"', second)
    assert h1 and h2 and set(h1).isdisjoint(set(h2))


def test_unaddressed_alone_never_triggers(patch_mod, tmp_path, monkeypatch):
    """Precision gate: with NO edited-untested obligation the class stays
    quiet — unaddressed rows ride along, they never trigger alone (edit
    tokens are a noisy proxy; correct-or-quiet)."""
    _reset(patch_mod, monkeypatch)
    obls = [dict(_OBLS[0], symbols=["never_touched_api"])]
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, obls))
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
    outs = [_drive(patch_mod, "sed -i 's/x/y/' pkg/other.py")]
    for c in _NONEDITS:
        outs.append(_drive(patch_mod, c))
    assert 'reason="test_evidence_gap"' not in "\n".join(outs)


def test_unaddressed_listed_alongside_edited(patch_mod, tmp_path, monkeypatch):
    """When the class fires, NOT-addressed obligations appear as checklist
    rows (the adaptix ExtraFieldsLoadError shape: transcribed, then dropped)."""
    _reset(patch_mod, monkeypatch)
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, _OBLS))
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
    outs = [_drive(patch_mod, _EDIT_SNAP)]
    for c in _NONEDITS:
        outs.append(_drive(patch_mod, c))
    joined = "\n".join(outs)
    assert "[not addressed]" in joined
    assert "ExtraFieldsLoadError" in joined


def test_severity_is_composite_not_constant(patch_mod, tmp_path, monkeypatch):
    """The candidate severity must be the computed composite (base + 2B +
    unmet), not the bare class rank."""
    _reset(patch_mod, monkeypatch)
    monkeypatch.setattr(patch_mod, "_GT_STEP_LIMIT", 100, raising=False)
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, _OBLS))
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
    _drive(patch_mod, _EDIT_SNAP)
    for c in _NONEDITS[:2]:
        _drive(patch_mod, c)
    # action_count == 3 now; produce directly at the would-be transition.
    monkeypatch.setattr(patch_mod, "_oracle_nonedit_streak", 3, raising=False)
    got = patch_mod._obligation_nudge_block()
    assert got is not None
    sev, payload = got
    # base 5 + 2*(3/100) + 1*(3/3 unmet) = 6.06
    assert abs(sev - (5 + 2 * (3 / 100) + 1.0)) < 1e-9
    assert 'reason="test_evidence_gap"' in payload


def test_pre_submit_severity_boost_at_90pct_budget(patch_mod, tmp_path, monkeypatch):
    """CP012 option 1: >90% budget + unmet -> severity = _SEV_GATE."""
    _reset(patch_mod, monkeypatch)
    monkeypatch.setattr(patch_mod, "_GT_STEP_LIMIT", 300, raising=False)
    monkeypatch.setattr(patch_mod, "_action_count", 281, raising=False)
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, _OBLS))
    monkeypatch.setattr(patch_mod, "_oracle_edited_tokens", {"capture_snapshot"}, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_nonedit_streak", 3, raising=False)
    got = patch_mod._obligation_nudge_block()
    assert got is not None
    sev, _payload = got
    # D3 fix: composite severity with base _SEV_GATE+1 so obligation beats horizon.
    # At 281/300 budget (0.937), unmet_ratio=1.0: composite(7, 0.937, 1.0) ≈ 9.87
    assert sev > float(patch_mod._SEV_GATE), (
        f"boost {sev} must beat flat gate {patch_mod._SEV_GATE}"
    )


def test_pre_submit_no_boost_early_budget(patch_mod, tmp_path, monkeypatch):
    """CP012: below 90% budget uses composite severity, not gate max."""
    _reset(patch_mod, monkeypatch)
    monkeypatch.setattr(patch_mod, "_GT_STEP_LIMIT", 300, raising=False)
    monkeypatch.setattr(patch_mod, "_action_count", 100, raising=False)
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, _OBLS))
    monkeypatch.setattr(patch_mod, "_oracle_edited_tokens", {"capture_snapshot"}, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_nonedit_streak", 3, raising=False)
    got = patch_mod._obligation_nudge_block()
    assert got is not None
    sev, _payload = got
    budget_b = 100 / 300
    unmet_ratio = (
        len(
            patch_mod._load_gt_oracle().order_unmet(
                patch_mod._get_obligation_tracker(patch_mod._load_gt_oracle()).statuses_tuple(
                    patch_mod._oracle_edited_tokens, patch_mod._oracle_tested_tokens
                )
            )
        )
        / 3
    )
    expected = patch_mod._load_gt_oracle().composite_severity(
        patch_mod._SEV_OBLIGATION, budget_b, unmet_ratio
    )
    assert abs(sev - expected) < 1e-9
    assert sev != float(patch_mod._SEV_GATE)


def test_no_obligations_dormant(patch_mod, tmp_path, monkeypatch):
    _reset(patch_mod, monkeypatch)
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, []))
    outs = [_drive(patch_mod, _EDIT_SNAP)]
    for c in _NONEDITS:
        outs.append(_drive(patch_mod, c))
    assert 'reason="test_evidence_gap"' not in "\n".join(outs)
