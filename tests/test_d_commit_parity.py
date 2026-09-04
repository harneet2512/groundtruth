"""D (Priority D) — substrate commit-parity + manifest provenance.

The substrate bakes its BUILD commit (GT_SUBSTRATE_BUILD_COMMIT, Dockerfile ENV);
the proof records it + compares to the run commit (GT_GIT_COMMIT). Integration runs
the baked /opt/gt/src, so a divergence = a stale substrate. Locks:
  1. commit_parity_status: match / mismatch / unknown.
  2. assert_commit_parity: record-only by default; fail-closed under GT_REQUIRE_COMMIT_PARITY=1.
  3. run_manifest carries substrate_build_commit + commit_parity + brief_sha256 + gt_git_commit.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_gp(modname: str = "gp_dtest"):
    spec = importlib.util.spec_from_file_location(
        modname, ROOT / "scripts" / "swebench" / "gt_run_proof.py"
    )
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[modname] = m
    spec.loader.exec_module(m)
    return m


def _clear(monkeypatch):
    for k in ("GT_SUBSTRATE_BUILD_COMMIT", "GT_GIT_COMMIT", "GT_REQUIRE_COMMIT_PARITY"):
        monkeypatch.delenv(k, raising=False)


def test_status_match(monkeypatch):
    gp = _load_gp()
    _clear(monkeypatch)
    monkeypatch.setenv("GT_SUBSTRATE_BUILD_COMMIT", "abc123")
    monkeypatch.setenv("GT_GIT_COMMIT", "abc123")
    assert gp.commit_parity_status()["status"] == "match"


def test_status_mismatch(monkeypatch):
    gp = _load_gp()
    _clear(monkeypatch)
    monkeypatch.setenv("GT_SUBSTRATE_BUILD_COMMIT", "abc123")
    monkeypatch.setenv("GT_GIT_COMMIT", "def456")
    st = gp.commit_parity_status()
    assert st["status"] == "mismatch"
    assert st["substrate_build_commit"] == "abc123" and st["run_commit"] == "def456"


def test_status_unknown_for_dev_or_unset(monkeypatch):
    gp = _load_gp()
    _clear(monkeypatch)
    # dev build sentinel
    monkeypatch.setenv("GT_SUBSTRATE_BUILD_COMMIT", "dev")
    monkeypatch.setenv("GT_GIT_COMMIT", "abc123")
    assert gp.commit_parity_status()["status"] == "unknown"
    # unset
    monkeypatch.delenv("GT_SUBSTRATE_BUILD_COMMIT", raising=False)
    assert gp.commit_parity_status()["status"] == "unknown"


def test_gate_off_is_record_only(monkeypatch):
    gp = _load_gp()
    _clear(monkeypatch)
    monkeypatch.setenv("GT_SUBSTRATE_BUILD_COMMIT", "abc123")
    monkeypatch.setenv("GT_GIT_COMMIT", "def456")  # mismatch
    ok, detail = gp.assert_commit_parity()  # flag unset
    assert ok is True and "record-only" in detail


def test_gate_on_mismatch_fails_closed(monkeypatch):
    gp = _load_gp()
    _clear(monkeypatch)
    monkeypatch.setenv("GT_SUBSTRATE_BUILD_COMMIT", "abc123")
    monkeypatch.setenv("GT_GIT_COMMIT", "def456")
    monkeypatch.setenv("GT_REQUIRE_COMMIT_PARITY", "1")
    ok, detail = gp.assert_commit_parity()
    assert ok is False and "GT_COMMIT_PARITY_MISMATCH" in detail


def test_gate_on_match_passes(monkeypatch):
    gp = _load_gp()
    _clear(monkeypatch)
    monkeypatch.setenv("GT_SUBSTRATE_BUILD_COMMIT", "abc123")
    monkeypatch.setenv("GT_GIT_COMMIT", "abc123")
    monkeypatch.setenv("GT_REQUIRE_COMMIT_PARITY", "1")
    ok, _ = gp.assert_commit_parity()
    assert ok is True


def test_manifest_carries_provenance(monkeypatch, tmp_path):
    gp = _load_gp()
    _clear(monkeypatch)
    monkeypatch.setenv("GT_SUBSTRATE_BUILD_COMMIT", "buildsha")
    monkeypatch.setenv("GT_GIT_COMMIT", "runsha")
    # write a brief.txt so brief_sha256 is recorded (not null)
    (tmp_path / "brief.txt").write_text("hello brief", encoding="utf-8")
    man = gp.build_run_manifest(
        graph_db=str(tmp_path / "nope.db"),
        out_dir=str(tmp_path),
        languages=["python"],
        lsp_scope_files=0,
        lsp_max_edges="0",
        lsp_ready_budgets={},
        gate_rc=0,
        artifacts_present={},
        source_root=str(tmp_path),
    )
    assert man["schema"] == "gt.run_manifest.v2"
    assert man["substrate_build_commit"] == "buildsha"
    assert man["gt_git_commit"] == "runsha"
    assert man["commit_parity"]["status"] == "mismatch"
    assert man["brief_sha256"] is not None  # brief.txt present -> hashed
