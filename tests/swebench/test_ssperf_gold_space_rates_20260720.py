"""SS-PERF (2026-07-20, run 29714439700 llama-factory) — recall-type PERF rates
must count in GOLD space and stay in [0, 1].

Live kill: two edited path spellings suffix-matched ONE gold file (src/ layout),
so localization_recall = scope_coverage = 2/1 = 2.0 — impossible rates the
validity gate correctly refused (task not citable although the trial RESOLVED).

Contract: (RED) the duplicate-spelling shape yields recall/coverage <= 1.0 and
exactly 1 gold file edited; multi_file_discovery cannot be faked by spellings;
precision-type metrics unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "src"), str(_REPO / "scripts" / "swebench")):
    if p not in sys.path:
        sys.path.insert(0, p)

import gt_performance_metrics as gpm  # noqa: E402


def _timeline_two_spellings_one_gold():
    # the llama shape: same gold file edited under two path spellings
    return [
        {"role": "assistant", "step": 1, "viewed_file": "src/pkg/mod.py"},
        {"role": "assistant", "step": 2, "edited_file": "src/pkg/mod.py"},
        {"role": "assistant", "step": 3, "edited_file": "./src/pkg/mod.py"},
        {"role": "assistant", "step": 4, "edited_file": "src/pkg/other.py"},
    ]


def test_scope_coverage_bounded_and_gold_space():
    out = gpm._compute_scope_completeness(
        _timeline_two_spellings_one_gold(), ["src/pkg/mod.py"])
    assert out["scope_coverage"] is not None and 0.0 <= out["scope_coverage"] <= 1.0
    assert out["scope_coverage"] == 1.0
    assert out["_gold_edited_count"] == 1  # gold-space, not spelling-space
    # two spellings of ONE gold file must never fake a multi-file discovery
    out2 = gpm._compute_scope_completeness(
        _timeline_two_spellings_one_gold(), ["src/pkg/mod.py", "src/pkg/never.py"])
    assert out2["multi_file_discovery"] is False


def test_scope_excess_precision_space_unchanged():
    out = gpm._compute_scope_completeness(
        _timeline_two_spellings_one_gold(), ["src/pkg/mod.py"])
    # 1 non-gold edited of 3 edited spellings
    assert out["_non_gold_edited_count"] == 1
    assert out["_total_edited_count"] == 3
