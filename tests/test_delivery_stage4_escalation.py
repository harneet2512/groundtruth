"""Delivery engine STAGE 4 — dynamic escalation (red->green).

Replaces the static B>=0.5/0.8 + 1.5xV bands with behavioral-signal bands:
  advisory : edit_coverage>0 ∧ test_coverage<0.5 ∧ budget_fraction>0.4
  urgent   : test_coverage<0.3 ∧ budget_fraction>0.7
  gate     : remaining < 2xV ∧ test_coverage<0.5
  pivot    : last observed outcome FAILED in the critical zone
V = median of the agent's OWN observed EDIT->TEST spans (25 is the DEFAULT
when nothing is observed — never the RULE).  Severity is a COMPUTED
composite: base + 2*budget_fraction + 1*unmet_ratio.

Mandatory properties:
  DYNAMIC — V observed; bands from sensed coverage ratios; thresholds are
            the env-overridable GT_ESC_* calibration channel.
  HYBRID  — every band predicate composites >=3 signals; severity composites
            3 signals into one number.
  RESEARCH-BACKED — Zilberstein 1996 (contract-algorithm urgency); BATS
            arXiv 2511.17006 (budget-aware dominates budget-blind); SWE-Next
            (87.4% budget-exhaustion failures); arXiv 2604.02547 ρ=+0.50
            validation share.

RED before Stage 4: verify_horizon_band took token sets (binary gap), V came
from test->test spans, severity was a class constant.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"


# FIX 5 (2026-06-11): the IN-CODE defaults are now corpus-derived (see
# tests/test_esc_calibration.py). This suite tests band STRUCTURE/latches/
# severity, so it PINS the historical reference thresholds via the env
# calibration channel — the scenarios stay threshold-independent.
_REF_ESC = {
    "GT_ESC_ADV_TCOV": "0.5",
    "GT_ESC_ADV_B": "0.4",
    "GT_ESC_URG_TCOV": "0.3",
    "GT_ESC_URG_B": "0.7",
    "GT_ESC_GATE_TCOV": "0.5",
    "GT_ESC_GATE_KV": "2.0",
}


def _load_patch(step_limit: str = "300", vcc: str = "25"):
    prev = {
        k: os.environ.get(k)
        for k in ["GT_BASELINE", "GT_STEP_LIMIT", "GT_VERIFICATION_CYCLE_COST"] + list(_REF_ESC)
    }
    os.environ["GT_BASELINE"] = "1"
    os.environ["GT_STEP_LIMIT"] = step_limit
    os.environ["GT_VERIFICATION_CYCLE_COST"] = vcc
    os.environ.update(_REF_ESC)
    name = f"gt_mini_patch_stage4_{step_limit}_{vcc}"
    try:
        spec = importlib.util.spec_from_file_location(name, _PATCH_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for var, p in prev.items():
            if p is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = p


@pytest.fixture(scope="module")
def gmp():
    return _load_patch("300", "25")


def _reset(gmp, monkeypatch):
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
        ("_horizon_urgent_fired", False),
        ("_horizon_pivot_fired", False),
        ("_last_test_outcome_failed", False),
        ("_cycle_edit_start", None),
        ("_test_cycle_spans", []),
        ("_edit_action_steps", []),
        ("_last_test_step", None),
        ("_traj_state_keys", []),
        ("_traj_loop_sigs", []),
        ("_lr_history", []),
        ("_nsr_history", []),
        ("_detect_loop_fired", False),
        ("_coherence_fired_files", set()),
        ("_coherence_last_rel", None),
        ("_oblig_syms_cache", None),
    ]:
        monkeypatch.setattr(gmp, name, val, raising=False)


def _drive(gmp, cmd, obs=""):
    out = {"output": obs, "returncode": 0}
    gmp._augment_output({"command": cmd}, out)
    return out.get("output") or ""


# ---------------------------------------------------------------------------
# V estimation — EDIT->TEST spans (the agent's own pace), 25 = default only.
# ---------------------------------------------------------------------------
class TestVFromEditTestSpans:
    def test_span_is_edit_to_test_not_test_to_test(self, gmp, monkeypatch, tmp_path):
        """The cycle opens at the FIRST source edit after the last result and
        closes at the next observed result."""
        _reset(gmp, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        _drive(gmp, "cat src/a.py", "content")  # action 1
        _drive(gmp, "sed -i 's/a/b/' src/a.py")  # action 2 (cycle opens)
        _drive(gmp, "git diff", "diff")  # action 3
        _drive(gmp, "sed -i 's/b/c/' src/a.py")  # action 4 (same cycle)
        _drive(gmp, "pytest -q", "1 passed")  # action 5 (closes)
        assert gmp._test_cycle_spans == [3]  # 5 - 2 (edit at 2, result at 5)

    def test_no_spans_uses_default(self, gmp, monkeypatch):
        _reset(gmp, monkeypatch)
        assert gmp._estimate_v() == 25  # the DEFAULT, not the RULE

    def test_observed_pace_wins(self, gmp, monkeypatch):
        _reset(gmp, monkeypatch)
        monkeypatch.setattr(gmp, "_test_cycle_spans", [9, 11, 13], raising=False)
        assert gmp._estimate_v() == 11  # the agent's own median


# ---------------------------------------------------------------------------
# Live escalation — coverage-driven bands + computed severity.
# ---------------------------------------------------------------------------
class TestLiveEscalation:
    def test_uncovered_edit_late_budget_produces_candidate(self, gmp, monkeypatch, tmp_path):
        _reset(gmp, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        monkeypatch.setattr(gmp, "_action_count", 160, raising=False)
        monkeypatch.setattr(gmp, "_oracle_edited_rels", {"src/x.py"}, raising=False)
        monkeypatch.setattr(gmp, "_oracle_edited_tokens", {"unverified_func"}, raising=False)
        monkeypatch.setattr(
            gmp, "_oracle_edited_tokens_by_file", {"src/x.py": {"unverified_func"}}, raising=False
        )
        got = gmp._verification_horizon_candidate()
        assert got is not None
        sev, kind, block, _eb = got
        assert kind == "verify.horizon.advisory"
        # severity is the COMPUTED composite: 4 + 2*(160/300) + 0 (no obligations)
        assert abs(sev - (4 + 2 * (160 / 300))) < 1e-9
        assert "<gt-verify" in block

    def test_test_coverage_suppresses(self, gmp, monkeypatch, tmp_path):
        """An edit whose identifiers SHOW test evidence earns silence —
        coverage, not binary token overlap, is the gap measure."""
        _reset(gmp, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        monkeypatch.setattr(gmp, "_action_count", 160, raising=False)
        monkeypatch.setattr(gmp, "_oracle_edited_rels", {"src/x.py"}, raising=False)
        monkeypatch.setattr(
            gmp, "_oracle_edited_tokens_by_file", {"src/x.py": {"covered_func"}}, raising=False
        )
        monkeypatch.setattr(
            gmp, "_oracle_tested_tokens", {"test_covered_func", "passed"}, raising=False
        )
        assert gmp._verification_horizon_candidate() is None

    def test_partial_coverage_half_files(self, gmp, monkeypatch, tmp_path):
        """2 edited files, 1 covered -> tc=0.5 -> advisory suppressed (the
        threshold is strict <0.5), urgent/gate quiet at mid budget."""
        _reset(gmp, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        monkeypatch.setattr(gmp, "_action_count", 160, raising=False)
        monkeypatch.setattr(gmp, "_oracle_edited_rels", {"src/x.py", "src/y.py"}, raising=False)
        monkeypatch.setattr(
            gmp,
            "_oracle_edited_tokens_by_file",
            {"src/x.py": {"covered_func"}, "src/y.py": {"naked_func"}},
            raising=False,
        )
        monkeypatch.setattr(gmp, "_oracle_tested_tokens", {"test_covered_func"}, raising=False)
        assert gmp._verification_horizon_candidate() is None

    def test_gate_keyed_to_observed_pace(self, gmp, monkeypatch, tmp_path):
        """A fast agent (V=8 observed) gets the gate later than the default
        would give it — V is the agent's own pace."""
        _reset(gmp, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        monkeypatch.setattr(gmp, "_test_cycle_spans", [8, 8, 8], raising=False)
        monkeypatch.setattr(gmp, "_action_count", 270, raising=False)
        monkeypatch.setattr(gmp, "_oracle_edited_rels", {"src/x.py"}, raising=False)
        monkeypatch.setattr(
            gmp, "_oracle_edited_tokens_by_file", {"src/x.py": {"naked_func"}}, raising=False
        )
        # R=30 > 2*8=16 -> NOT gate; B=0.9>0.7, tc=0 -> urgent
        got = gmp._verification_horizon_candidate()
        assert got is not None and got[1] == "verify.horizon.urgent"
        # with the DEFAULT V=25 the same point would be a gate (R=30 < 50)
        monkeypatch.setattr(gmp, "_test_cycle_spans", [], raising=False)
        monkeypatch.setattr(gmp, "_horizon_advisory_fired", False, raising=False)
        got2 = gmp._verification_horizon_candidate()
        assert got2 is not None and got2[1] == "verify.horizon.gate"

    def test_urgent_fires_once_per_band(self, gmp, monkeypatch, tmp_path):
        """LIPI 2026-06-11 replay receipt: fd turns 209-218 got TEN consecutive
        urgent emissions (the countdown re-hashed every render). The band fires
        ONCE; the ladder (gate) is the escalation, never repetition (Wink:
        multi-intervention recovery degrades 90.9% -> 79%)."""
        _reset(gmp, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        monkeypatch.setattr(gmp, "_action_count", 220, raising=False)
        monkeypatch.setattr(gmp, "_oracle_edited_rels", {"src/x.py"}, raising=False)
        monkeypatch.setattr(
            gmp, "_oracle_edited_tokens_by_file", {"src/x.py": {"naked_func"}}, raising=False
        )
        got = gmp._verification_horizon_candidate()
        assert got is not None and got[1] == "verify.horizon.urgent"
        for nxt in (221, 222, 223):  # band holds, R counts down — no re-fire
            monkeypatch.setattr(gmp, "_action_count", nxt, raising=False)
            assert gmp._verification_horizon_candidate() is None

    def test_pivot_fires_once_per_band(self, gmp, monkeypatch, tmp_path):
        _reset(gmp, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        monkeypatch.setattr(gmp, "_action_count", 220, raising=False)
        monkeypatch.setattr(gmp, "_oracle_edited_rels", {"src/x.py"}, raising=False)
        monkeypatch.setattr(
            gmp, "_oracle_edited_tokens_by_file", {"src/x.py": {"naked_func"}}, raising=False
        )
        monkeypatch.setattr(gmp, "_last_test_outcome_failed", True, raising=False)
        got = gmp._verification_horizon_candidate()
        assert got is not None and got[1] == "verify.horizon.pivot"
        monkeypatch.setattr(gmp, "_action_count", 221, raising=False)
        assert gmp._verification_horizon_candidate() is None

    def test_unmet_obligations_raise_severity(self, gmp, monkeypatch, tmp_path):
        """unmet_ratio enters the composite: with obligations present and
        unedited, severity is strictly higher."""
        import json

        _reset(gmp, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        anchors = tmp_path / "gt_issue_anchors.json"
        anchors.write_text(
            json.dumps(
                {
                    "obligations": [
                        {"verbatim_text": "x must hold", "symbols": ["edited_sym"]},
                        {"verbatim_text": "y must hold", "symbols": ["never_edited_sym"]},
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("GT_ANCHORS_PATH", str(anchors))
        monkeypatch.setattr(gmp, "_action_count", 220, raising=False)
        monkeypatch.setattr(gmp, "_oracle_edited_rels", {"src/x.py"}, raising=False)
        monkeypatch.setattr(gmp, "_oracle_edited_tokens", {"edited_sym"}, raising=False)
        monkeypatch.setattr(
            gmp, "_oracle_edited_tokens_by_file", {"src/x.py": {"edited_sym"}}, raising=False
        )
        got = gmp._verification_horizon_candidate()
        assert got is not None
        sev, kind, _b, _e = got
        assert kind == "verify.horizon.urgent"
        # base 5 + 2*(220/300) + 1*(1 - 0.5 edit_coverage) = 5 + 1.4666... + 0.5
        assert abs(sev - (5 + 2 * (220 / 300) + 0.5)) < 1e-9
