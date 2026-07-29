"""CP015 — context budget trim + cross-turn dedup tests."""
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
        spec = importlib.util.spec_from_file_location("gt_patch_cp015", _PATCH_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["gt_patch_cp015"] = mod
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
    mod._DELIVERED_FACTS.clear()
    mod._DELIVERED_FACT_IDS.clear()
    return mod


def test_trim_caps_length(pm):
    imperative = "Inspect symbol_x before editing.\n"
    filler = "Background explanation " * 200 + "\n"
    block = imperative + filler
    out = pm._budget_trim(block, max_tokens=500)
    assert len(out) <= 500 * 4 + 50
    assert imperative.strip() in out


def test_dedup_after_commit(pm):
    """D1 fix: dedup only takes effect AFTER commit_delivered is called."""
    line = "Check all callers listed above before changing this interface."
    first = pm._budget_trim(line)
    assert first.strip() == line
    second = pm._budget_trim(line)
    assert second.strip() == line, "uncommitted facts must not be suppressed"
    pm._PRODUCT_BUDGETER.commit_delivered([line])
    third = pm._budget_trim(line)
    assert third == "", "committed facts must be suppressed"


def test_dedup_semantic_id_after_commit(pm):
    """C-3 (context_budget.py:106-115): the fact id is tag:symbol:hash(REMAINDER), not
    tag:symbol. This test used to assert that a PARAPHRASE ("invoked from" vs "called by")
    of the same location collapsed; C-3 deliberately gave that up, because tag+symbol alone
    made two genuinely DISTINCT facts ("get_user calls -> A" vs "get_user called by -> B")
    collide and permanently suppressed the second. Never falsely suppressing real evidence
    beats collapsing a paraphrase, so the assertions below pin the guarantee C-3 actually
    makes: an identical re-render (modulo whitespace/case) collides, a different fact does
    not. There is no synonym normalization anywhere in stable_fact_id."""
    pm._DELIVERED_FACTS.clear()
    pm._DELIVERED_FACT_IDS.clear()
    a = "[WITNESS] capture_snapshot called by -> pkg/mod.py:44"
    first = pm._budget_trim(a)
    assert first.strip() == a
    pm._PRODUCT_BUDGETER.commit_delivered([a])

    # true dedup: the SAME fact re-rendered with different whitespace still collides.
    respaced = "[WITNESS]  capture_snapshot   called by  ->  pkg/mod.py:44"
    assert pm._budget_trim(respaced) == "", "an identical re-render must be suppressed"

    # no FALSE suppression: same tag AND same symbol, but a different fact.
    other_direction = "[WITNESS] capture_snapshot calls -> pkg/other.py:9"
    assert pm._budget_trim(other_direction).strip() == other_direction, (
        "a distinct fact sharing tag+symbol must NOT be suppressed (the C-3 collision)"
    )


def test_imperative_survives_over_explanation(pm):
    lines = [
        "Changing foo risks breaking bar (a.py:1). Inspect before editing.",
        "This paragraph explains the graph structure in detail " * 30,
        "[FACT] edge confidence 0.9",
    ]
    block = "\n".join(lines)
    out = pm._budget_trim(block, max_tokens=80)
    assert "Changing foo" in out
    assert "explains the graph" not in out
