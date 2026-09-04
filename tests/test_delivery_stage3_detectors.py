"""Delivery engine STAGE 3 — loop / coherence detector classes (red->green).

(a) detect.loop — TIDE (arXiv 2602.02196) degenerate-cycle detector over the
    WHOLE trajectory: fires when the current (command, observation) signature
    has recurred >=3 times AND loop_ratio exceeds the trajectory's own
    median+MAD AND new_state_rate is below its own median AND >= 2*window
    steps elapsed.  This catches the fd stale-binary loop (5x identical,
    spread >12 actions apart) that the window-12 arm measured SILENT on
    (~75 wasted steps, DEEP_TRAJECTORY §1.8) — and replaces that arm on the
    oracle route.

(b) detect.coherence_collapse — TRAJEVAL (arXiv 2603.24631): re-edits of the
    same file >=3 times with no passing test between (churn resets on
    observed pass), anchored or last-test-failed.

Mandatory properties:
  DYNAMIC — loop thresholds are median/MAD of the trajectory's OWN signal
            history (no constants); churn reset is the agent's own pass.
  HYBRID  — loop composites 4 signals; coherence composites 3.
  RESEARCH-BACKED — TIDE loop-ratio/new-state; TRAJEVAL coherence collapse;
            Wink (94.29% loop-recovery from one message) for the payload form.

RED before Stage 3: no `degenerate_loop` / `coherence_collapse` reason
exists anywhere in the live module; fd's loop shape produces zero output.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"


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
def patch_mod():
    return _load(_PATCH_PATH, "gt_mini_patch_stage3_detect")


def _reset(patch_mod, monkeypatch, route=True):
    for name, val in [
        ("_GT_BASELINE", False),
        ("_ORACLE_ROUTE", route),
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
        ("_last_test_outcome_failed", False),
        ("_traj_state_keys", []),
        ("_traj_loop_sigs", []),
        ("_lr_history", []),
        ("_nsr_history", []),
        ("_detect_loop_fired", False),
        ("_coherence_fired_files", set()),
        ("_coherence_last_rel", None),
        ("_DELIVERED_FACTS", set()),
        ("_ledger_consumed_kinds", set()),
        ("_ledger_ignore_counts", {}),
    ]:
        monkeypatch.setattr(patch_mod, name, val, raising=False)


def _drive(patch_mod, cmd, obs=""):
    out = {"output": obs, "returncode": 0}
    patch_mod._augment_output({"command": cmd}, out)
    return out.get("output") or ""


# ---------------------------------------------------------------------------
# (a) detect.loop
# ---------------------------------------------------------------------------
class TestDegenerateLoop:
    def test_fd_shape_spread_loop_fires(self, patch_mod, monkeypatch, tmp_path):
        """The fd stale-binary signature: identical (command, output) repeats
        SPREAD between varied actions — beyond any 12-window — must fire
        detect.loop. This was the measured silent failure (~75 steps)."""
        _reset(patch_mod, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        outs = []
        idx = 0
        stale_cmd = "./target/debug/fd --sort name"
        stale_out = "file7.txt\nfile007.txt\nWRONG ORDER, IDENTICAL EVERY RUN"
        for block in range(6):
            for j in range(6):
                outs.append(_drive(patch_mod, f"ls dir_{block}_{j}", f"listing {block}{j}"))
                idx += 1
            outs.append(_drive(patch_mod, stale_cmd, stale_out))
            idx += 1
        joined = "\n".join(outs)
        assert 'reason="degenerate_loop"' in joined, (
            "the spread stale-binary loop must fire detect.loop"
        )
        # the retired window arm must NOT be the one firing on this route.
        assert 'reason="loop"' not in joined

    def test_same_command_new_output_never_fires(self, patch_mod, monkeypatch, tmp_path):
        """Iteration (same command, NEW output each run) is progress — the
        '13453 false fire' discipline holds for the new detector too."""
        _reset(patch_mod, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        outs = []
        for i in range(40):
            outs.append(_drive(patch_mod, "pytest -x", f"collected {i} items, run {i}"))
        assert 'reason="degenerate_loop"' not in "\n".join(outs)

    def test_min_steps_window_derived(self, patch_mod, monkeypatch, tmp_path):
        """Before 2*window actions of history the detector stays silent —
        the trajectory's own distribution does not exist yet."""
        _reset(patch_mod, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        outs = []
        for _ in range(5):
            outs.append(_drive(patch_mod, "make check", "same output"))
        assert 'reason="degenerate_loop"' not in "\n".join(outs)

    def test_diverse_progress_silent(self, patch_mod, monkeypatch, tmp_path):
        _reset(patch_mod, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        outs = []
        for i in range(50):
            outs.append(_drive(patch_mod, f"cat src/file_{i}.py", f"content {i}"))
        assert 'reason="degenerate_loop"' not in "\n".join(outs)

    def test_legacy_route_keeps_windowed_loop(self, patch_mod, monkeypatch):
        """GT_ORACLE_ROUTE=0: the original window-12 arm is unchanged."""
        _reset(patch_mod, monkeypatch, route=False)
        outs = []
        for _ in range(5):
            outs.append(_drive(patch_mod, "python runner.py", "same error, no change"))
        joined = "\n".join(outs)
        assert 'reason="loop"' in joined
        assert 'reason="degenerate_loop"' not in joined


# ---------------------------------------------------------------------------
# (b) detect.coherence_collapse
# ---------------------------------------------------------------------------
class TestCoherenceCollapse:
    def test_three_rewrites_no_pass_fires(self, patch_mod, monkeypatch, tmp_path):
        _reset(patch_mod, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        outs = []
        for s in ("a/b", "b/c", "c/d"):
            outs.append(_drive(patch_mod, f"sed -i 's/{s}/' src/core.py"))
            outs.append(_drive(patch_mod, "git diff", "diff text"))
        joined = "\n".join(outs)
        assert 'reason="coherence_collapse"' in joined
        assert "3 times" in joined
        assert "Run targeted verification FIRST" in joined

    def test_pass_between_resets_and_stays_silent(self, patch_mod, monkeypatch, tmp_path):
        _reset(patch_mod, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        outs = [
            _drive(patch_mod, "sed -i 's/a/b/' src/core.py"),
            _drive(patch_mod, "sed -i 's/b/c/' src/core.py"),
            _drive(patch_mod, "pytest -q", "5 passed in 0.2s"),
            _drive(patch_mod, "sed -i 's/c/d/' src/core.py"),
            _drive(patch_mod, "sed -i 's/d/e/' src/core.py"),
        ]
        assert 'reason="coherence_collapse"' not in "\n".join(outs)

    def test_fires_once_per_file(self, patch_mod, monkeypatch, tmp_path):
        _reset(patch_mod, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        outs = []
        for i in range(6):
            outs.append(_drive(patch_mod, f"sed -i 's/x{i}/y{i}/' src/core.py"))
            outs.append(_drive(patch_mod, "git diff", f"diff {i}"))
        fires = "\n".join(outs).count('reason="coherence_collapse"')
        assert fires == 1

    def test_two_edits_silent(self, patch_mod, monkeypatch, tmp_path):
        _reset(patch_mod, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        outs = [
            _drive(patch_mod, "sed -i 's/a/b/' src/core.py"),
            _drive(patch_mod, "sed -i 's/b/c/' src/core.py"),
        ]
        assert 'reason="coherence_collapse"' not in "\n".join(outs)
