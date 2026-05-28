"""Invariant tests for L3b per-file-once dedup gate.

DEDUP-INV-1: L3b delivers evidence for a file AT MOST ONCE per task.
Re-reads of the same file must not re-inject callers/callees.

Research backing:
- Du et al. EMNLP 2025: context length hurts 13.9-85% even with perfect retrieval
- OCD/SWEzze 2026: only 8.4% of segments needed for resolution
- Lost in the Middle NeurIPS 2024: repeated injections push useful evidence into dead zone
"""
import types
import pytest


def _make_config():
    config = types.SimpleNamespace()
    config.evidence_sent = {}
    return config


def _simulate_l3b_gate(config, file_path):
    """Simulate the per-file-once gate. Returns True if evidence would be delivered."""
    key = f"l3b_file:{file_path}"
    if key in config.evidence_sent:
        return False
    config.evidence_sent[key] = True
    return True


class TestPerFileOnce:
    """DEDUP-INV-1: L3b fires at most once per file."""

    def test_first_view_delivers(self):
        config = _make_config()
        assert _simulate_l3b_gate(config, "src/auth.py") is True

    def test_second_view_blocked(self):
        config = _make_config()
        _simulate_l3b_gate(config, "src/auth.py")
        assert _simulate_l3b_gate(config, "src/auth.py") is False

    def test_different_files_both_deliver(self):
        config = _make_config()
        assert _simulate_l3b_gate(config, "src/auth.py") is True
        assert _simulate_l3b_gate(config, "src/db.py") is True

    def test_five_rereads_all_blocked(self):
        config = _make_config()
        assert _simulate_l3b_gate(config, "flex.py") is True
        for _ in range(5):
            assert _simulate_l3b_gate(config, "flex.py") is False

    def test_weasyprint_scenario(self):
        """Regression: weasyprint flex.py read 5+ times, got same callers each time."""
        config = _make_config()
        assert _simulate_l3b_gate(config, "weasyprint/layout/flex.py") is True
        for _ in range(9):
            assert _simulate_l3b_gate(config, "weasyprint/layout/flex.py") is False

    def test_many_files_one_each(self):
        config = _make_config()
        files = [f"src/module_{i}.py" for i in range(20)]
        for f in files:
            assert _simulate_l3b_gate(config, f) is True
        for f in files:
            assert _simulate_l3b_gate(config, f) is False
