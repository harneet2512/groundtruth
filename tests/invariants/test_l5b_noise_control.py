"""Invariant tests for L5b noise control.

L5B-INV-1: Max 2 L5b firings per task.
L5B-INV-2: L5b only suggests files in brief_candidates.
L5B-INV-3: Same file never suggested twice by L5b.
"""
import types
import pytest


def _make_config(brief_candidates=None, max_iter=100):
    """Create a minimal GTRuntimeConfig-like object for testing."""
    config = types.SimpleNamespace()
    config._pending_next_actions = []
    config.action_count = 30
    config.max_iter = max_iter
    config.brief_candidates = brief_candidates or set()
    config._l5b_injection_count = 0
    config._l5b_suggested_files = set()
    config._last_source_edit_iter = 0
    config._iter_state = {}
    config._meta_instance_id = "test"
    config._agent_state = None
    config._telemetry_writer = None
    config._interaction_log = []
    config._last_gt_action = 0
    return config


def _add_pending(config, file_path, action_type="READ_CALLER_CONTRACT"):
    """Add a pending next_action that has been checked 2 times (about to expire)."""
    config._pending_next_actions.append({
        "event_id": "evt_test",
        "next_action_type": action_type,
        "next_action_file": file_path,
        "iter_emitted": config.action_count - 5,
        "checked_count": 2,
        "followed": False,
    })


def _simulate_l5b_check(config, obs="some observation text"):
    """Simulate _check_pending_next_actions logic (extracted for testing).

    Returns (obs, injected_count, injected_files).
    """
    injected_count = 0
    injected_files = []
    expired = []

    for i, pending in enumerate(config._pending_next_actions):
        pending["checked_count"] += 1
        if pending["checked_count"] >= 3:
            if not pending["followed"]:
                nat = pending["next_action_type"]
                naf = pending.get("next_action_file", "")

                # L5B-INV-1: cap at 2
                if getattr(config, "_l5b_injection_count", 0) >= 2:
                    expired.append(i)
                    continue
                # L5B-INV-2: brief_candidates filter
                bc = getattr(config, "brief_candidates", set())
                if naf and bc and not any(naf in c or c in naf for c in bc):
                    expired.append(i)
                    continue
                # L5B-INV-3: file dedup
                seen = getattr(config, "_l5b_suggested_files", set())
                if naf and naf in seen:
                    expired.append(i)
                    continue

                # Inject
                msg = f"[GT L5: Ignored Structural Witness] {nat} for {naf}"
                obs = obs + f"\n\n{msg}\n"
                config._l5b_injection_count = getattr(config, "_l5b_injection_count", 0) + 1
                if naf:
                    if not hasattr(config, "_l5b_suggested_files"):
                        config._l5b_suggested_files = set()
                    config._l5b_suggested_files.add(naf)
                injected_count += 1
                injected_files.append(naf)
            expired.append(i)

    for i in reversed(expired):
        config._pending_next_actions.pop(i)
    return obs, injected_count, injected_files


class TestL5bCap:
    """L5B-INV-1: Max 2 L5b firings per task."""

    def test_cap_at_2(self):
        config = _make_config(brief_candidates={"a.py", "b.py", "c.py", "d.py", "e.py"})
        for f in ["a.py", "b.py", "c.py", "d.py", "e.py"]:
            _add_pending(config, f)
        _, count, files = _simulate_l5b_check(config)
        assert count == 2
        assert len(files) == 2

    def test_already_at_cap(self):
        config = _make_config(brief_candidates={"a.py"})
        config._l5b_injection_count = 2
        _add_pending(config, "a.py")
        _, count, _ = _simulate_l5b_check(config)
        assert count == 0


class TestL5bRelevanceGate:
    """L5B-INV-2: L5b only suggests files in brief_candidates."""

    def test_relevant_file_passes(self):
        config = _make_config(brief_candidates={"src/core/auth.py"})
        _add_pending(config, "src/core/auth.py")
        _, count, files = _simulate_l5b_check(config)
        assert count == 1
        assert files == ["src/core/auth.py"]

    def test_irrelevant_file_blocked(self):
        config = _make_config(brief_candidates={"src/core/auth.py"})
        _add_pending(config, "src/utils/random_helper.py")
        _, count, _ = _simulate_l5b_check(config)
        assert count == 0

    def test_partial_path_match(self):
        config = _make_config(brief_candidates={"repo__task/src/core/auth.py"})
        _add_pending(config, "src/core/auth.py")
        _, count, files = _simulate_l5b_check(config)
        assert count == 1

    def test_empty_candidates_allows_all(self):
        config = _make_config(brief_candidates=set())
        _add_pending(config, "anything.py")
        _, count, _ = _simulate_l5b_check(config)
        assert count == 1


class TestL5bFileDedup:
    """L5B-INV-3: Same file never suggested twice."""

    def test_same_file_blocked_second_time(self):
        config = _make_config(brief_candidates={"a.py"})
        _add_pending(config, "a.py")
        _add_pending(config, "a.py")
        _, count, _ = _simulate_l5b_check(config)
        assert count == 1

    def test_different_files_both_pass(self):
        config = _make_config(brief_candidates={"a.py", "b.py"})
        _add_pending(config, "a.py")
        _add_pending(config, "b.py")
        _, count, _ = _simulate_l5b_check(config)
        assert count == 2


class TestL5bWeasyprint:
    """Regression: weasyprint-2300 got 9x L5b for table.py, float.py, column.py."""

    def test_weasyprint_scenario_capped(self):
        brief_files = {"weasyprint/layout/flex.py"}
        config = _make_config(brief_candidates=brief_files)
        irrelevant = [
            "weasyprint/layout/table.py",
            "weasyprint/layout/float.py",
            "weasyprint/layout/float.py",
            "weasyprint/layout/column.py",
            "weasyprint/layout/float.py",
            "tests/layout/test_block.py",
            "weasyprint/layout/column.py",
            "weasyprint/layout/float.py",
            "weasyprint/layout/column.py",
        ]
        for f in irrelevant:
            _add_pending(config, f)
        _, count, _ = _simulate_l5b_check(config)
        # All files are NOT in brief_candidates (only flex.py is)
        assert count == 0
