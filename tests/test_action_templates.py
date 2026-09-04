"""CP014 — graph-to-action template tests."""

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
        spec = importlib.util.spec_from_file_location("gt_patch_cp014", _PATCH_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["gt_patch_cp014"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if prev is None:
            os.environ.pop("GT_BASELINE", None)
        else:
            os.environ["GT_BASELINE"] = prev


@pytest.fixture(scope="module")
def pm():
    return _load_patch()


def test_orient_passes_through(pm):
    raw = "[CALLERS] 3 cross-file callers\n[WITNESS] foo called by -> bar.py:12"
    out = pm._translate_to_action(raw, pm.Phase.ORIENT)
    assert out == raw


def test_edit_translates_witness_to_imperative(pm):
    raw = "[WITNESS] capture_snapshot called by -> pkg/mod.py:44"
    out = pm._translate_to_action(raw, pm.Phase.EDIT)
    # D5: original fact kept + imperative appended (append, not replace)
    assert "[WITNESS]" in out, "original fact must be preserved"
    assert "Changing capture_snapshot risks breaking" in out, "caller_risk template must fire"
    assert "pkg/mod.py:44" in out


def test_edit_callers_becomes_check_instruction(pm):
    raw = "[CALLERS] 2 callers in other files"
    out = pm._translate_to_action(raw, pm.Phase.VERIFY)
    assert "Check all callers" in out


def test_edit_verified_callers_becomes_contract_risk_instruction(pm):
    raw = "[CALLERS] capture_snapshot: 3 verified caller(s) in 2 file(s) — preserve this interface"
    out = pm._translate_to_action(raw, pm.Phase.EDIT)
    assert "Preserve the capture_snapshot interface" in out
    assert "callers" in out
