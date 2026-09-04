"""CP013 — phase detection + policy filter tests."""

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
        spec = importlib.util.spec_from_file_location("gt_patch_cp013", _PATCH_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["gt_patch_cp013"] = mod
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


def _reset(pm, monkeypatch):
    monkeypatch.setattr(pm, "_action_count", 0, raising=False)
    monkeypatch.setattr(pm, "_oracle_edited_rels", set(), raising=False)
    monkeypatch.setattr(pm, "_oracle_nonedit_streak", 0, raising=False)
    monkeypatch.setattr(pm, "_GT_STEP_LIMIT", 300, raising=False)


def test_orient_phase_early(pm, monkeypatch):
    _reset(pm, monkeypatch)
    monkeypatch.setattr(pm, "_action_count", 3, raising=False)
    assert pm._detect_phase() == pm.Phase.ORIENT
    assert pm._phase_allows("brief", pm.Phase.ORIENT)
    assert pm._phase_allows("orientation", pm.Phase.ORIENT)
    assert not pm._phase_allows("l3b.evidence", pm.Phase.ORIENT)


def test_view_phase_before_edits(pm, monkeypatch):
    _reset(pm, monkeypatch)
    monkeypatch.setattr(pm, "_action_count", 20, raising=False)
    assert pm._detect_phase() == pm.Phase.VIEW
    assert pm._phase_allows("l3b.evidence", pm.Phase.VIEW)
    assert not pm._phase_allows("spec.obligation", pm.Phase.VIEW)


def test_edit_phase_after_source_edit(pm, monkeypatch):
    _reset(pm, monkeypatch)
    monkeypatch.setattr(pm, "_oracle_edited_rels", {"pkg/mod.py"}, raising=False)
    monkeypatch.setattr(pm, "_action_count", 50, raising=False)
    assert pm._detect_phase() == pm.Phase.EDIT
    assert pm._phase_allows("spec.obligation", pm.Phase.EDIT)
    assert pm._phase_allows("l3.contract", pm.Phase.EDIT)


def test_submit_phase_high_budget(pm, monkeypatch):
    _reset(pm, monkeypatch)
    monkeypatch.setattr(pm, "_oracle_edited_rels", {"pkg/mod.py"}, raising=False)
    monkeypatch.setattr(pm, "_action_count", 281, raising=False)
    assert pm._detect_phase() == pm.Phase.SUBMIT
    assert pm._phase_allows("verify.horizon.gate", pm.Phase.SUBMIT)
    assert not pm._phase_allows("l3b.evidence", pm.Phase.SUBMIT)


def test_verify_phase_nonedit_streak(pm, monkeypatch):
    _reset(pm, monkeypatch)
    monkeypatch.setattr(pm, "_oracle_edited_rels", {"pkg/mod.py"}, raising=False)
    monkeypatch.setattr(pm, "_oracle_nonedit_streak", 4, raising=False)
    monkeypatch.setattr(pm, "_action_count", 120, raising=False)
    assert pm._detect_phase() == pm.Phase.VERIFY
    assert pm._phase_allows("l5.failure", pm.Phase.VERIFY)
