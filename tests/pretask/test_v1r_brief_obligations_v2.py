"""GT_OBLIGATIONS_V2 — T1/T2 brief-side tests (plan §8, tests 8-12).

Pins: dynamic budget + deterministic ordering, the end-to-end filterMap
render on the REAL true-myth issue (the 189/192 loss), the persisted-bridge
symbols fix, the three-tier leak guard, and flag-off inertness."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from groundtruth.pretask.v1r_brief import (
    _dynamic_obligation_budget,
    _render_obligations_block,
    _v2_order_key,
)

_FIX = Path(__file__).parent / "fixtures" / "obligations_v2"
TRUE_MYTH = (_FIX / "true-myth-iterable-collection-combinators.md").read_text(
    encoding="utf-8"
)
_CAP = lambda s: s  # noqa: E731 — identity body-line cap for tests


@pytest.fixture()
def v2_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GT_OBLIGATIONS_V2", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.delenv("GT_ANCHORS_PATH", raising=False)
    return tmp_path


# ── 8a. dynamic budget ────────────────────────────────────────────────────────
def test_dynamic_budget_scaling():
    assert _dynamic_obligation_budget(6) == 4    # today's dose preserved
    assert _dynamic_obligation_budget(12) == 4
    assert _dynamic_obligation_budget(25) == 9   # mobly-class spec
    assert _dynamic_obligation_budget(40) == 10  # hard ceiling
    assert _dynamic_obligation_budget(0) == 4


# ── 8b. deterministic ordering ────────────────────────────────────────────────
def test_v2_ordering_mandatory_compound_first():
    declarative = {"modality_strength": 1, "subject_symbols": ["thing"],
                   "kind": "behavior", "_doc_index": 0}
    mandatory = {"modality_strength": 3, "subject_symbols": ["signals.TestError"],
                 "kind": "error", "_doc_index": 5}
    rows = [declarative, mandatory]
    rows.sort(key=_v2_order_key)
    assert rows[0] is mandatory  # strength beats document order


# ── 9. END-TO-END: filterMap survives extraction + gate + budget ─────────────
def test_render_v2_filtermap_survives_gate_and_budget(v2_env):
    lines = _render_obligations_block(
        TRUE_MYTH, files=[], cap=_CAP, anchor_symbols={"filterMap", "traverse"},
    )
    block = "\n".join(lines)
    assert "filterMap" in block, "the 189/192-loss clause must reach the brief"
    assert "<gt-obligations>" in block


# ── T2 artifact: full checklist + render_path_tokens persisted ───────────────
def test_v2_artifact_written(v2_env):
    _render_obligations_block(
        TRUE_MYTH, files=[], cap=_CAP, anchor_symbols={"filterMap"},
    )
    md = v2_env / "gt_obligations.md"
    js = v2_env / "gt_obligations_v2.json"
    assert md.is_file() and js.is_file()
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["obligations_version"] == 2
    assert "render_path_tokens" in payload
    assert any("filterMap" in c["verbatim_text"] for c in payload["clauses"])
    # the artifact carries MORE clauses than the rendered top-K can
    assert len(payload["clauses"]) >= 4
    assert "- [ ] (" in md.read_text(encoding="utf-8")


# ── 10. persisted-bridge carries symbols (leak check regains its symbol leg) ─
def test_persisted_bridge_symbol_leak_drops(v2_env):
    anchors = {
        "obligations": [
            {
                # verbatim is CLEAN — only the symbols field carries the leak;
                # the v1 two-field bridge lost symbols and would render this.
                "verbatim_text": "the collector must always gather results",
                "kind": "behavior",
                "symbols": ["test_secret_gold"],
                "keywords": [], "checkable_forms": [],
                "clause_id": "x", "modality": "mandatory",
                "modality_strength": 3, "subject_symbols": ["collector"],
                "parent_id": "", "part_index": 0,
            },
            {
                "verbatim_text": "the parser should accept `filterMap` input",
                "kind": "behavior",
                "symbols": ["filterMap"], "keywords": [],
                "checkable_forms": [], "clause_id": "y",
                "modality": "expected", "modality_strength": 2,
                "subject_symbols": ["filterMap"], "parent_id": "",
                "part_index": 0,
            },
        ]
    }
    (v2_env / "gt_issue_anchors.json").write_text(json.dumps(anchors), encoding="utf-8")
    lines = _render_obligations_block(
        "irrelevant", files=[], cap=_CAP,
        anchor_symbols={"collector", "filterMap"},
    )
    block = "\n".join(lines)
    assert "collector" not in block, "symbol-borne leak must drop the clause whole"
    assert "filterMap" in block


# ── 11. leak guard at T1 AND T2 (T3 pinned in the detector suite) ────────────
def test_leak_guard_t1_and_t2(v2_env):
    issue = (
        "The runner must always execute test_secret_gold before merging.\n\n"
        "Add `filterMap` to `maybe`; `filterMap` has the non-curried signature "
        "`filterMap(items, fn)` and a curried form `filterMap(fn)`.\n"
    )
    lines = _render_obligations_block(
        issue, files=[], cap=_CAP, anchor_symbols={"filterMap"},
    )
    block = "\n".join(lines)
    assert "test_secret_gold" not in block  # T1
    payload = json.loads((v2_env / "gt_obligations_v2.json").read_text(encoding="utf-8"))
    assert not any(
        "test_secret_gold" in c["verbatim_text"] for c in payload["clauses"]
    )  # T2: artifact leak-screened identically


# ── 12. flag-off inertness ────────────────────────────────────────────────────
def test_flag_off_no_artifact_and_v1_budget(monkeypatch, tmp_path):
    monkeypatch.delenv("GT_OBLIGATIONS_V2", raising=False)
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.delenv("GT_ANCHORS_PATH", raising=False)
    lines = _render_obligations_block(
        TRUE_MYTH, files=[], cap=_CAP, anchor_symbols={"filterMap", "Result"},
    )
    body = [l for l in lines if l.strip().startswith("- ") or l.strip().startswith("-")]
    assert len([l for l in lines if l.lstrip().startswith("- ")]) <= 4  # v1 budget
    assert not (tmp_path / "gt_obligations_v2.json").exists()
    assert not (tmp_path / "gt_obligations.md").exists()
