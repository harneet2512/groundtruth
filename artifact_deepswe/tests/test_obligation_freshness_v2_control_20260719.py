"""RED-first proof for the GT_OBLIGATION_FRESHNESS dark-fix (O-2).

Pre-fix, the ONLY GT_OBLIGATION_FRESHNESS control emission lived inside the legacy
``_obligation_nudge_block``, which ``_review_obligation_candidate`` SKIPS whenever V2
obligations are present (it dispatches to ``obligation.unexercised``). So on every V2 run
the feature was 100%-dark: the record never fired.

The fix lifts the freshness-demotion control into a standalone
``_emit_obligation_freshness_control(om)`` called at the TOP of
``_review_obligation_candidate`` BEFORE the V2/legacy dispatch. These tests assert:

  1. RED->GREEN: the GT_OBLIGATION_FRESHNESS record now fires on the V2 path.
  2. The DIRECT feature ``obligation.unexercised`` delivery is byte-for-byte unchanged.
  3. Flag-off is silent (byte-identical): no freshness record, V2 delivery unchanged.
"""
from __future__ import annotations

import gt_mini_patch as g


def _rows(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(dict(row)))
    return rows


class _FakeOracle:
    """Minimal gt_oracle stand-in: only the members the freshness helper touches."""

    def load_obligations(self, _path):
        return [{"id": 0, "verbatim": "the parser must reject empty input",
                 "symbols": ["widget_symbol"]}]


class _FakeTracker:
    """Records the exact calls the helper makes; simulates a stale-tested demotion."""

    def __init__(self, transitions):
        self._transitions = transitions
        self.update_calls: list[tuple] = []
        self.freshness_calls: list[tuple] = []

    def update(self, edited, tested, turn):
        self.update_calls.append((set(edited), set(tested), turn))
        return []

    def apply_edit_freshness(self, new_edits, turn):
        self.freshness_calls.append((set(new_edits), turn))
        return list(self._transitions)


def _wire_v2(monkeypatch, tracker):
    """V2 obligations active -> obligation.unexercised owns delivery; legacy tracker
    is NOT built by that branch, so the helper must build/drive it itself."""
    monkeypatch.setattr(g, "_load_gt_oracle", lambda: _FakeOracle())
    monkeypatch.setattr(g, "_get_obligation_tracker", lambda om: tracker)
    monkeypatch.setattr(g, "_edited_symbols_for_obligations", lambda: {"widget_symbol"})
    monkeypatch.setattr(g, "_prev_obl_edited", set())
    monkeypatch.setattr(g, "_oracle_tested_tokens", {"widget_symbol"})
    monkeypatch.setattr(g, "_action_count", 5)
    monkeypatch.setattr(g, "_anchors_path", lambda: "/tmp/anchors.json")
    monkeypatch.setattr(g, "_load_obligations_v2", lambda: {"obligations_version": 2})
    monkeypatch.setattr(g, "_unexercised_clause_candidate", lambda: (5.0, "clause-text"))


def test_freshness_control_fires_on_v2_path(monkeypatch) -> None:
    """RED pre-fix (dark), GREEN post-fix: the record fires even though V2 owns delivery."""
    rows = _rows(monkeypatch)
    monkeypatch.setenv("GT_OBLIGATION_FRESHNESS", "1")
    tracker = _FakeTracker(transitions=[(0, "tested", "edited")])  # a real stale demotion
    _wire_v2(monkeypatch, tracker)

    kind, candidate, is_lane_a = g._review_obligation_candidate()

    # (2) DIRECT feature obligation.unexercised delivery is UNCHANGED.
    assert (kind, candidate, is_lane_a) == ("obligation.unexercised", (5.0, "clause-text"), True)

    # (1) GT_OBLIGATION_FRESHNESS now emits on the V2 path (was 100%-dark before the lift).
    fresh = [r for r in rows
             if r.get("control_ref", {}).get("feature_id") == "GT_OBLIGATION_FRESHNESS"]
    assert len(fresh) == 1, "freshness control must fire exactly once on the V2 path"
    assert fresh[0]["participation_decision"] == "APPLIED"
    assert fresh[0]["decision_reason"] == "tested_obligation_demoted"
    assert fresh[0]["control_ref"]["role"] == "eligibility"

    # The helper drove the legacy tracker (kept it current + used the per-turn NEW edits).
    assert tracker.update_calls == [({"widget_symbol"}, {"widget_symbol"}, 5)]
    assert tracker.freshness_calls == [({"widget_symbol"}, 5)]


def test_freshness_no_effect_when_no_stale_obligation(monkeypatch) -> None:
    """No stale demotion -> a gradeable NO_EFFECT breadcrumb (not silence, not APPLIED)."""
    rows = _rows(monkeypatch)
    monkeypatch.setenv("GT_OBLIGATION_FRESHNESS", "1")
    tracker = _FakeTracker(transitions=[])  # nothing demoted this turn
    _wire_v2(monkeypatch, tracker)

    g._review_obligation_candidate()

    fresh = [r for r in rows
             if r.get("control_ref", {}).get("feature_id") == "GT_OBLIGATION_FRESHNESS"]
    assert len(fresh) == 1
    assert fresh[0]["participation_decision"] == "NO_EFFECT"
    assert fresh[0]["decision_reason"] == "no_stale_tested_obligation"


def test_flag_off_is_silent_and_v2_delivery_unchanged(monkeypatch) -> None:
    """Default-off: no freshness record AND obligation.unexercised delivery byte-identical."""
    rows = _rows(monkeypatch)
    monkeypatch.delenv("GT_OBLIGATION_FRESHNESS", raising=False)
    tracker = _FakeTracker(transitions=[(0, "tested", "edited")])
    _wire_v2(monkeypatch, tracker)

    kind, candidate, is_lane_a = g._review_obligation_candidate()

    assert (kind, candidate, is_lane_a) == ("obligation.unexercised", (5.0, "clause-text"), True)
    assert not [r for r in rows
                if r.get("control_ref", {}).get("feature_id") == "GT_OBLIGATION_FRESHNESS"]
    # flag off => the helper never even touches the tracker.
    assert tracker.update_calls == []
    assert tracker.freshness_calls == []
