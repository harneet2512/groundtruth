"""A1 (Priority B) — the substrate proof generates the v1r brief ONCE.

gate3b (foundational_gates, the earlier subprocess) generates + PERSISTS its
V1RBriefResult to <out_dir>/brief_result.json; emit_brief (gt_run_proof, later)
LOADS it instead of regenerating. Locks:
  1. brief_cache.get_or_generate: generate once, reuse on the second call, stable sha.
  2. fail-safe: a cache miss regenerates (never blocks brief.txt).
  3. end-to-end: gate persist -> emit_brief reuse (generator NOT called twice),
     brief.txt == the gate's brief, gate sha == delivered sha.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(modname: str, rel: str):
    spec = importlib.util.spec_from_file_location(modname, ROOT / rel)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[modname] = m
    spec.loader.exec_module(m)
    return m


class _FakeResult:
    def __init__(self, text: str):
        self.brief_text = text
        self.effective_w_sem = 0.40
        self.semantic_signal_count = 3
        self.rendered_candidate_count = 5
        self.k_sem_top = 2
        self.sem_components = [0.1, 0.2]


def test_brief_cache_single_generation_and_stable_sha():
    from groundtruth.runtime import brief_cache as bc

    calls = {"n": 0}

    def gen(issue_text, repo_root, graph_db, bug_id):
        calls["n"] += 1
        return _FakeResult(f"BRIEF::{issue_text}::call{calls['n']}")

    import tempfile

    with tempfile.TemporaryDirectory() as out:
        r1 = bc.get_or_generate(out, "fix bug", "/work", "g.db", generator=gen)
        assert r1["generated"] is True
        r2 = bc.get_or_generate(out, "fix bug", "/work", "g.db", generator=gen)
        assert r2["generated"] is False  # reused — no 2nd generation
        assert calls["n"] == 1  # exactly one generation
        assert r1["brief_text"] == r2["brief_text"]
        assert r1["brief_sha256"] == r2["brief_sha256"]
        assert bc.brief_sha256(r2["brief_text"]) == r1["brief_sha256"]


def test_brief_cache_failsafe_regenerates_on_miss():
    from groundtruth.runtime import brief_cache as bc

    calls = {"n": 0}

    def gen(issue_text, repo_root, graph_db, bug_id):
        calls["n"] += 1
        return _FakeResult("X")

    import tempfile

    with tempfile.TemporaryDirectory() as out:
        r = bc.get_or_generate(out, "x", "/w", "g", generator=gen)
        assert r["generated"] is True and calls["n"] == 1


def test_gate_persist_then_emit_reuses(tmp_path, monkeypatch):
    """The real cross-process flow: gate3b persists, emit_brief reuses (no 2nd gen)."""
    fg = _load("fg_a1", "scripts/metrics/foundational_gates.py")
    gp = _load("gp_a1", "scripts/swebench/gt_run_proof.py")

    monkeypatch.setenv("GT_BRIEF_CACHE_DIR", str(tmp_path))
    # gate3b generated this brief and persists it.
    fg._persist_brief_for_emit(_FakeResult("THE GATE BRIEF"), "issue text", "g.db")
    assert (tmp_path / "brief_result.json").is_file()

    calls = {"n": 0}

    def gen(issue_text, repo_root, graph_db, bug_id):
        calls["n"] += 1
        return _FakeResult("REGENERATED — SHOULD NOT HAPPEN")

    ok, detail = gp.emit_brief(str(tmp_path), "issue text", "/work", "g.db", generator=gen)
    assert ok, detail
    assert calls["n"] == 0, "emit_brief regenerated instead of reusing the gate brief"
    assert (tmp_path / "brief.txt").read_text(encoding="utf-8").strip() == "THE GATE BRIEF"
    assert "reused_gate_brief=True" in detail


def test_emit_brief_generates_and_writes_when_no_cache(tmp_path):
    """No gate cache (e.g. gates skipped) -> emit_brief generates + writes (fail-safe)."""
    gp = _load("gp_a1b", "scripts/swebench/gt_run_proof.py")
    calls = {"n": 0}

    def gen(issue_text, repo_root, graph_db, bug_id):
        calls["n"] += 1
        return _FakeResult("FRESH BRIEF")

    ok, detail = gp.emit_brief(str(tmp_path), "issue", "/work", "g.db", generator=gen)
    assert ok, detail
    assert calls["n"] == 1
    assert (tmp_path / "brief.txt").read_text(encoding="utf-8").strip() == "FRESH BRIEF"
    assert "reused_gate_brief=False" in detail


def test_emit_brief_empty_fails_closed(tmp_path):
    """An empty brief is still a fail-closed GT_ARTIFACT_MISSING (proof contract)."""
    gp = _load("gp_a1c", "scripts/swebench/gt_run_proof.py")

    def gen(issue_text, repo_root, graph_db, bug_id):
        return _FakeResult("   ")  # whitespace only -> empty after strip

    ok, detail = gp.emit_brief(str(tmp_path), "issue", "/work", "g.db", generator=gen)
    assert ok is False
    assert "EMPTY" in detail
