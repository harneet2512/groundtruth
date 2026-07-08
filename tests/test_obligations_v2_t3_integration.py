"""GT_OBLIGATIONS_V2 — T3 integration (plan §8 t13/14/16/18/19).

Drives the REAL artifact->loader->statuses->render->dose pipeline inside
gt_mini_patch with clauses extracted from the REAL true-myth v1.0.0 issue:
the filterMap clause must be flagged unexercised when test evidence names
everything BUT filterMap (the 189/192 loss, replayed at the unit seam)."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

_FIX = Path(__file__).parent / "pretask" / "fixtures" / "obligations_v2"
TRUE_MYTH = (_FIX / "true-myth-iterable-collection-combinators.md").read_text(
    encoding="utf-8"
)


@pytest.fixture()
def gmp(monkeypatch, tmp_path):
    """Import gt_mini_patch fresh-ish with GT_CERT_DIR at tmp and clean state."""
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("GT_OBLIGATIONS_V2", "1")
    repo = str(Path(__file__).resolve().parents[1])
    for p in (repo, str(Path(repo) / "artifact_deepswe")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import gt_mini_patch as m  # noqa: PLC0415
    importlib.reload(m) if getattr(m, "_obligations_v2_cache", None) else None
    m._obligations_v2_cache = None
    m._unexercised_emitted.clear()
    m._oracle_tested_tokens.clear()
    m._oracle_edited_tokens.clear()
    return m, tmp_path


def _write_artifact(tmp_path: Path) -> None:
    from groundtruth.pretask.spec import extract_spec_v2
    spec = extract_spec_v2(TRUE_MYTH)
    payload = {
        "obligations_version": 2,
        "render_path_tokens": [],
        "clauses": spec.to_serializable(version=2),
    }
    (tmp_path / "gt_obligations_v2.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# ── t13: the filterMap replay at the unit seam ───────────────────────────────
def test_t3_filtermap_flagged_unexercised(gmp):
    m, tmp = gmp
    _write_artifact(tmp)
    # the real run's shape: test evidence names other combinators, never filterMap
    m._oracle_tested_tokens.update(
        {"traverse", "partition", "sequence", "zipWith", "retryN", "vitest"}
    )
    got = m._unexercised_clause_candidate()
    assert got is not None
    block = got[1]
    assert "filterMap" in block and "unexercised_clauses" in block
    assert not m._V2_LEAK_TEST_RE.search(block.replace("test/run output", ""))


# ── t14: exercising the clause silences it ───────────────────────────────────
def test_t3_exercised_clause_not_flagged(gmp):
    m, tmp = gmp
    _write_artifact(tmp)
    m._oracle_tested_tokens.add("test_filtermap")  # substring credit (compound)
    got = m._unexercised_clause_candidate()
    if got is not None:
        assert "filterMap" not in got[1].split("could not be auto-checked")[0]


# ── t16: inactive without the artifact — v1 site behavior preserved ──────────
def test_t3_inactive_without_artifact(gmp):
    m, _tmp = gmp
    assert m._load_obligations_v2() is None
    assert m._unexercised_clause_candidate() is None


# ── t18: dose — at most 2 emissions, deduped by status vector ────────────────
def test_t3_dose_max_two_and_dedup(gmp):
    m, tmp = gmp
    _write_artifact(tmp)
    first = m._unexercised_clause_candidate()
    assert first is not None
    assert m._unexercised_clause_candidate() is None  # same vector -> dedup
    m._oracle_tested_tokens.add("filterMap")  # vector changes
    second = m._unexercised_clause_candidate()
    assert second is not None
    m._oracle_tested_tokens.add("partition")  # vector changes again
    assert m._unexercised_clause_candidate() is None  # hard cap 2


# ── reset law: latch + cache clear on _reset_oracle_state ────────────────────
def test_t3_reset_law_clears_latch_and_cache(gmp):
    m, tmp = gmp
    _write_artifact(tmp)
    assert m._unexercised_clause_candidate() is not None
    assert m._unexercised_emitted
    m._reset_oracle_state()
    assert not m._unexercised_emitted
    assert m._obligations_v2_cache is None


# ── leak screen: render_path_tokens drop rows whole ──────────────────────────
def test_t3_path_token_leak_drops_row(gmp):
    m, tmp = gmp
    payload = {
        "obligations_version": 2,
        "render_path_tokens": ["goldstem"],
        "clauses": [{
            "verbatim_text": "the goldstem module must always frobnicate",
            "kind": "behavior", "symbols": ["frobnicate_fn"],
            "subject_symbols": ["frobnicate_fn"], "keywords": [],
            "checkable_forms": [], "clause_id": "z", "modality": "mandatory",
            "modality_strength": 3, "parent_id": "", "part_index": 0,
        }],
    }
    (tmp / "gt_obligations_v2.json").write_text(json.dumps(payload), encoding="utf-8")
    got = m._unexercised_clause_candidate()
    assert got is None  # only row leaks -> whole block silent (fail closed)


# ── t19: coverage_v2 lands in the persisted status artifact ──────────────────
def test_t3_coverage_v2_in_status_artifact(gmp, monkeypatch):
    m, tmp = gmp
    _write_artifact(tmp)
    status_path = tmp / "obligation_status.json"
    monkeypatch.setenv("GT_OBLIGATION_STATUS", str(status_path))
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp / "ev.jsonl"))

    class _Tr:
        def coverage_ratio(self):
            return 0.5
        def snapshot(self):
            return []

    m._persist_obligation_status(_Tr(), turn=1)
    snap = json.loads(status_path.read_text(encoding="utf-8"))
    assert snap["coverage_v2"]["coverage_version"] == 2
    assert "coverage_exercised" in snap["coverage_v2"]
