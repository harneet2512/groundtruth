"""CP011 — ObligationTracker lifecycle tests."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_ORACLE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle.py"


def _load_oracle():
    spec = importlib.util.spec_from_file_location("gt_oracle_cp011", _ORACLE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gt_oracle_cp011"] = mod
    spec.loader.exec_module(mod)
    return mod


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
]


@pytest.fixture(scope="module")
def om():
    return _load_oracle()


def test_tracker_transitions_on_edit_then_test(om):
    tracker = om.ObligationTracker(_OBLS)
    t1 = tracker.update({"capture_snapshot"}, set(), turn=10)
    assert any(new == "edited" for _id, _old, new in t1)
    t2 = tracker.update(
        {"capture_snapshot"},
        {"test_capture_snapshot"},
        turn=20,
    )
    assert any(new == "tested" for _id, _old, new in t2)
    assert tracker.coverage_ratio() == 0.5


def test_unmet_returns_alias_collision_shape(om):
    """Adaptix-style miss: obligation 0 never edited stays unmet at submit."""
    tracker = om.ObligationTracker(_OBLS)
    tracker.update({"capture_snapshot"}, {"test_capture_snapshot"}, turn=50)
    unmet = tracker.unmet()
    ids = {o.id for o in unmet}
    assert 0 in ids
    assert 1 not in ids


def test_statuses_tuple_matches_obligation_statuses(om):
    tracker = om.ObligationTracker(_OBLS)
    edited = {"capture_snapshot"}
    tested = {"test_max_snapshots"}
    tracker.update(edited, tested, turn=3)
    via_tracker = tracker.statuses_tuple(edited, tested)
    via_fn = om.obligation_statuses(om._obligation_views(_OBLS), edited, tested)
    assert [s for _v, s, _t, _c in via_tracker] == [
        s for _v, s, _t, _c in via_fn
    ]
