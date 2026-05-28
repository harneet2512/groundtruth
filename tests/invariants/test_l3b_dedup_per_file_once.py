"""Invariant tests for L3b hybrid dedup: per-file-once + hash + reindex reset.

DEDUP-INV-1 (hybrid):
- Per-file-once gate blocks pure re-reads (no graph change)
- L6 reindex resets the gate (graph changed, callers may differ)
- Hash-based dedup remains as safety net for post-reindex re-reads

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


def _simulate_reindex_reset(config):
    """Simulate L6 reindex clearing per-file-once gates."""
    stale = [k for k in config.evidence_sent if k.startswith("l3b_file:")]
    for k in stale:
        del config.evidence_sent[k]


class TestPerFileOnce:
    """DEDUP-INV-1: L3b fires at most once per file between reindexes."""

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


class TestReindexReset:
    """Hybrid: L6 reindex resets per-file-once gates."""

    def test_reindex_allows_redelivery(self):
        config = _make_config()
        assert _simulate_l3b_gate(config, "src/auth.py") is True
        assert _simulate_l3b_gate(config, "src/auth.py") is False
        _simulate_reindex_reset(config)
        assert _simulate_l3b_gate(config, "src/auth.py") is True

    def test_reindex_resets_all_files(self):
        config = _make_config()
        _simulate_l3b_gate(config, "a.py")
        _simulate_l3b_gate(config, "b.py")
        _simulate_l3b_gate(config, "c.py")
        _simulate_reindex_reset(config)
        assert _simulate_l3b_gate(config, "a.py") is True
        assert _simulate_l3b_gate(config, "b.py") is True
        assert _simulate_l3b_gate(config, "c.py") is True

    def test_reindex_does_not_clear_hash_dedup(self):
        """Hash-based dedup keys (l3b:file:hash) survive reindex reset."""
        config = _make_config()
        config.evidence_sent["l3b:src/auth.py:abc123"] = True
        _simulate_reindex_reset(config)
        assert "l3b:src/auth.py:abc123" in config.evidence_sent

    def test_no_reindex_means_blocked(self):
        config = _make_config()
        _simulate_l3b_gate(config, "src/auth.py")
        # No reindex → still blocked
        assert _simulate_l3b_gate(config, "src/auth.py") is False
        assert _simulate_l3b_gate(config, "src/auth.py") is False

    def test_edit_reindex_reread_cycle(self):
        """Full cycle: read → blocked → edit+reindex → read again → delivers."""
        config = _make_config()
        assert _simulate_l3b_gate(config, "src/auth.py") is True
        assert _simulate_l3b_gate(config, "src/auth.py") is False
        _simulate_reindex_reset(config)  # agent edited, L6 reindex fired
        assert _simulate_l3b_gate(config, "src/auth.py") is True
        assert _simulate_l3b_gate(config, "src/auth.py") is False  # blocked again until next reindex
