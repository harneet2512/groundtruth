"""Piece 3 — ledger-driven suppression + consumed boost."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"


def _load_patch():
    prev = os.environ.get("GT_BASELINE")
    os.environ["GT_BASELINE"] = "1"
    try:
        spec = importlib.util.spec_from_file_location("gt_patch_ledger", _PATCH_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["gt_patch_ledger"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if prev is None:
            os.environ.pop("GT_BASELINE", None)
        else:
            os.environ["GT_BASELINE"] = prev


@pytest.fixture
def pm():
    mod = _load_patch()
    mod._ledger_consumed_kinds.clear()
    mod._ledger_ignore_counts.clear()
    return mod


def test_consumed_kind_gets_severity_boost(pm):
    pm._ledger_consumed_kinds.add("spec.obligation")
    boosted = pm._ledger_boost_severity("spec.obligation", 5.0)
    assert boosted == 5.5
    assert pm._ledger_boost_severity("l3b.evidence", 5.0) == 5.0


def test_ignored_kind_skipped_after_two_deliveries(pm):
    pm._ledger_ignore_counts["l5.stuck"] = 2
    assert pm._ledger_should_skip_kind("l5.stuck")
    assert not pm._ledger_should_skip_kind("l5.failure")


def test_delivery_marks_consumed_on_action_cmd(pm):
    pm._ledger_note_delivery("spec.obligation", "pytest tests/test_foo.py")
    assert "spec.obligation" in pm._ledger_consumed_kinds
    pm._ledger_note_delivery("l5.stuck", "git status")
    assert pm._ledger_ignore_counts.get("l5.stuck", 0) == 1
