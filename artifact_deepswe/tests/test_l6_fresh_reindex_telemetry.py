"""Stage-1 regression: L6-fresh reindex WORKS + emits HARVESTED, positive telemetry.

Root cause (run 28754452453): L6-fresh silently FROZE in-container — an agent-created
file never entered the graph (proven: 13 post-create views, 0 <gt-evidence> on the new
file, vs 14 on pre-existing files) — yet NOT ONE failure line survived, because every L6
outcome printed only to ``sys.stderr``, which the pier harness DROPS. A broken layer read
as a silent green no-op and passed every prior review.

The fix (all in gt_mini_patch.py, SYNCED not baked -> no rebuild):
  * ``_l6_emit`` routes every L6 outcome to the HARVESTED runtime ledger AND stdout, so a
    freeze is observable next run instead of inferred.
  * ``_invalidate_on_edit`` emits a one-time PROBE (which precondition failed), a
    STAGING_FELLBACK guard (never reindex the authoritative RO mount), and on success a
    ``REINDEX_OK ... nodes_before=N nodes_after=M`` line — the POSITIVE freshness proof.

These are behavioral pins on the REAL Go binary + a REAL graph, per TTD (artifact-first):
they assert the mechanism actually adds a new file's node AND surfaces the proof line.
Skips (does NOT silently pass) when the gt-index binary is unavailable.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MINI = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"


def _find_gt_index() -> str | None:
    """Native gt-index binary for THIS platform (env override wins). Skip if absent —
    never a silent pass."""
    env = os.environ.get("GT_INDEX_BIN")
    if env and os.path.isfile(env):
        return env
    cands = (["gt-index.exe", "gt-index-new.exe"] if sys.platform == "win32"
             else ["gt-index-linux", "gt-index"])
    for c in cands:
        p = _ROOT / "gt-index" / c
        if p.is_file():
            return str(p)
    return None


def _load_mini():
    spec = importlib.util.spec_from_file_location("gtmp_l6", str(_MINI))
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def repo_with_graph(tmp_path):
    gtx = _find_gt_index()
    if not gtx:
        pytest.skip("gt-index binary unavailable — cannot exercise the real reindex path")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    mount = repo / "mount.db"
    rc = subprocess.run([gtx, f"-root={repo}", f"-output={mount}"],
                        capture_output=True, timeout=120)
    if rc.returncode != 0 or not mount.is_file():
        pytest.skip(f"initial index failed rc={rc.returncode}: {rc.stderr[:200]!r}")
    return gtx, repo, mount


def test_l6_fresh_reindex_adds_new_file_and_emits_proof(repo_with_graph, capsys, monkeypatch):
    """GREEN: staging + `-file` reindex add a brand-new file's node to the read-path
    graph, and the HARVESTED-channel proof line fires with nodes_before<nodes_after."""
    gtx, repo, mount = repo_with_graph
    monkeypatch.setenv("GT_L6_FRESH", "1")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(mount))
    monkeypatch.setenv("GT_CERT_DIR", str(repo))
    monkeypatch.setenv("GT_INDEX_BIN", gtx)
    m = _load_mini()

    # the agent CREATES a new file this trajectory (the stratum-D case GT must not go blind on)
    (repo / "newmod.py").write_text("def brandnew_symbol():\n    return 7\n", encoding="utf-8")

    db = m._db_path()                      # triggers the writable work-copy staging
    assert m._l6_work_db, "work-copy was not staged under GT_L6_FRESH=1"
    assert db == m._l6_work_db, "read path must be the work-copy, not the RO mount"

    m._invalidate_on_edit("newmod.py", str(repo))   # the post-edit L6 reindex

    # (1) FRESHNESS: the new symbol is now in the graph the per-turn pillars read.
    con = sqlite3.connect(f"file:{db.replace(chr(92), '/')}?mode=ro", uri=True, timeout=5)
    names = {r[0] for r in con.execute("SELECT name FROM nodes")}
    con.close()
    assert "brandnew_symbol" in names, "new-file symbol NOT in read-path graph = FROZEN"

    # (2) OBSERVABILITY: the positive proof line lands on the harvested channel. P-A
    # (2026-07-05) routes _l6_emit to STDERR + the runtime ledger — NOT stdout, which
    # leaks [GT_META] into the agent's context on PATH B. Capture both streams so the
    # assertion tracks the leak-safe channel wherever it lands.
    _c = capsys.readouterr(); out = _c.out + _c.err
    assert "L6_REINDEX_OK" in out, f"no REINDEX_OK proof line emitted:\n{out}"
    assert "nodes_before=0" in out and "nodes_after=1" in out, (
        f"node-count freshness proof missing/wrong:\n{out}")


def test_l6_staging_fallback_refuses_to_reindex_ro_mount(repo_with_graph, capsys, monkeypatch):
    """GUARD: if staging fell back to the authoritative (real, populated) mount, L6 must
    REFUSE to reindex it — never mutate the witnessed graph — and say so, not silently
    corrupt it. Force the fallback by memoizing the staging result to '' (copy failed)."""
    gtx, repo, mount = repo_with_graph
    monkeypatch.setenv("GT_L6_FRESH", "1")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(mount))
    monkeypatch.setenv("GT_CERT_DIR", str(repo))
    monkeypatch.setenv("GT_INDEX_BIN", gtx)
    m = _load_mini()
    setattr(m, "_l6_work_db", "")          # staging fell back -> _db_path returns the mount
    assert m._db_path() == str(mount), "with staging failed, read path must be the mount"

    m._invalidate_on_edit("newmod.py", str(repo))
    _c = capsys.readouterr(); out = _c.out + _c.err  # P-A: L6 telemetry on stderr
    assert "L6_STAGING_FELLBACK" in out, (
        f"guard did not fire — L6 would have reindexed the authoritative mount:\n{out}")
