"""DeepSWE ADAPTER+PIPELINE — FAIL-CLOSED, NO-FALLBACK contract (the 6 holes).

The substrate-consume DeepSWE adapter (`artifact_deepswe/gt_agent.py` +
`artifact_deepswe/gt_mini_patch.py` + `.github/workflows/deepswe_full.yml`) must be
consume-or-fail-closed on every path in proof/substrate mode — never silent-degrade,
never rebuild a divergent graph, never host-fallback when the substrate is active.

These tests assert the closed-form of the 5-phase-audit holes:
  #1 DUAL-GRAPH removed     : _BUILD_GRAPH_DB step is NOT injected when the substrate
                              is active (GT_HOST_GRAPH_DB / GT_PORTABLE_SUBSTRATE set).
  #2 Witness FAIL-CLOSED    : _emit_gt_meta_witness RAISES (not warns) on a forced
                              hook != post-LSP hash mismatch under GT_PROOF_MODE=1.
  #3 Brief consume FAIL-CLOSED: _generate_brief RAISES in proof mode when the substrate
                              brief.txt is absent — NO host-side generate_v1r_brief.
  #5 Env injection present  : the workflow forwards the GT substrate env INTO the task
                              container (--ae lines) + bind-mounts /gt_artifacts.
  #6 One substrate graph    : gt_mini_patch._db_path reads GT_HOST_GRAPH_DB
                              unconditionally and never falls back to /tmp/graph.db in
                              substrate/proof mode; L6 reindex is OFF in substrate mode.

All deterministic, stdlib + a tiny sqlite graph fixture. No Go toolchain, no network,
no task IDs / gold / per-task logic.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_AGENT_PATH = _ROOT / "artifact_deepswe" / "gt_agent.py"
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"
_WORKFLOW = _ROOT / ".github" / "workflows" / "deepswe_full.yml"
_PIER_CFG = _ROOT / "artifact_deepswe" / "gt_integration" / "deepswe_gt_pier.yaml"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent_mod():
    return _load("gt_agent_failclosed_uut", _AGENT_PATH)


@pytest.fixture
def patch_mod():
    return _load("gt_mini_patch_failclosed_uut", _PATCH_PATH)


def _gt_env_clear(monkeypatch):
    """Strip every GT_* var so each test starts from a known, unset baseline."""
    for k in list(os.environ):
        if k.startswith("GT_"):
            monkeypatch.delenv(k, raising=False)


# ── a real edge-bearing graph so proof.graph_edges_hash returns a stable hash ──
def _make_graph(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, signature TEXT, "
        "return_type TEXT, is_exported INT, is_test INT, language TEXT, parent_id INT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INT, target_id INT, "
        "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, confidence REAL, metadata TEXT)"
    )
    conn.execute("INSERT INTO nodes (label,name,file_path,language) VALUES ('Function','A','a.py','python')")
    conn.execute("INSERT INTO nodes (label,name,file_path,language) VALUES ('Function','B','b.py','python')")
    conn.execute("INSERT INTO edges (source_id,target_id,type,source_line,resolution_method,confidence) "
                 "VALUES (1,2,'CALLS',4,'import',1.0)")
    conn.commit()
    conn.close()
    from groundtruth.runtime import proof as _proof
    return _proof.graph_edges_hash(str(db_path))


# ───────────────────────────── HOLE #1: NO DUAL GRAPH ─────────────────────────
def test_hole1_no_dual_graph_build_when_substrate_active(agent_mod, monkeypatch, tmp_path):
    """_inject_steps must NOT emit the in-container _BUILD_GRAPH_DB step when the
    substrate graph is handed off (GT_HOST_GRAPH_DB set). The substrate graph is the
    ONLY graph — no second build, no fallback to it."""
    _gt_env_clear(monkeypatch)
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    steps = agent_mod._inject_steps()
    runs = "\n".join(s.run for s in steps)
    # The build snippet's signature command must be absent.
    assert "gt-index -root" not in runs and "/tmp/gt-index" not in runs, (
        "DUAL-GRAPH: _BUILD_GRAPH_DB injected while substrate is active:\n" + runs
    )
    # And the explicit no-dual-graph marker IS present (proves we took the gated branch).
    assert "NOT building a second in-container graph" in runs


def test_hole1_build_graph_runs_on_legacy_nonsubstrate_path(agent_mod, monkeypatch):
    """Outside substrate mode (no GT_HOST_GRAPH_DB / GT_CERT_DIR / GT_PORTABLE_SUBSTRATE)
    the legacy in-container build step IS still injected (no behavior change off-path)."""
    _gt_env_clear(monkeypatch)
    steps = agent_mod._inject_steps()
    runs = "\n".join(s.run for s in steps)
    assert "/tmp/gt-index" in runs and "graph.db built at /tmp/graph.db" in runs


# ───────────────────────── HOLE #2: WITNESS FAIL-CLOSED ───────────────────────
def test_hole2_witness_raises_on_hash_mismatch_in_proof(agent_mod, monkeypatch, tmp_path):
    """A consumed graph whose edge hash != the substrate's post-LSP hash must RAISE
    DeepSweAdapterError under GT_PROOF_MODE=1 — NOT print a warning and continue."""
    _gt_env_clear(monkeypatch)
    db = tmp_path / "graph.db"
    _make_graph(db)  # real hook hash
    cert_dir = tmp_path
    # Force a MISMATCH: write an lsp cert whose post-LSP hash is deliberately wrong.
    (cert_dir / "lsp_certificate.json").write_text(
        '{"graph_hash_after_lsp": "deadbeef_not_the_real_hash"}', encoding="utf-8")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(db))
    monkeypatch.setenv("GT_CERT_DIR", str(cert_dir))
    monkeypatch.setenv("GT_LSP_CERT", str(cert_dir / "lsp_certificate.json"))
    with pytest.raises(agent_mod.DeepSweAdapterError, match="GRAPH_FAIL_HASH_MISMATCH"):
        agent_mod._emit_gt_meta_witness()


def test_hole2_witness_does_not_raise_on_match_in_proof(agent_mod, monkeypatch, tmp_path):
    """The witness must NOT raise when the consumed graph DOES match the substrate's
    post-LSP hash (the legitimate consume path — a flip from raise-on-mismatch)."""
    _gt_env_clear(monkeypatch)
    db = tmp_path / "graph.db"
    real_hash = _make_graph(db)
    cert_dir = tmp_path
    (cert_dir / "lsp_certificate.json").write_text(
        '{"graph_hash_after_lsp": "%s"}' % real_hash, encoding="utf-8")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(db))
    monkeypatch.setenv("GT_CERT_DIR", str(cert_dir))
    monkeypatch.setenv("GT_LSP_CERT", str(cert_dir / "lsp_certificate.json"))
    agent_mod._emit_gt_meta_witness()  # must not raise


def test_hole2_witness_warns_not_raises_off_proof(agent_mod, monkeypatch, tmp_path, capsys):
    """Outside proof mode the same mismatch must WARN (classified [GT_META] line) and
    NOT raise — dev/CI stays non-fatal."""
    _gt_env_clear(monkeypatch)
    db = tmp_path / "graph.db"
    _make_graph(db)
    cert_dir = tmp_path
    (cert_dir / "lsp_certificate.json").write_text(
        '{"graph_hash_after_lsp": "deadbeef_wrong"}', encoding="utf-8")
    # NO GT_PROOF_MODE. Substrate handoff present.
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(db))
    monkeypatch.setenv("GT_CERT_DIR", str(cert_dir))
    monkeypatch.setenv("GT_LSP_CERT", str(cert_dir / "lsp_certificate.json"))
    agent_mod._emit_gt_meta_witness()  # must NOT raise
    out = capsys.readouterr().out
    assert "DEEPSWE_ADAPTER_FAIL" in out and "GRAPH_FAIL_HASH_MISMATCH" in out


# ───────────────────────── HOLE #3: BRIEF CONSUME FAIL-CLOSED ─────────────────
def test_hole3_brief_raises_in_proof_when_brief_absent(agent_mod, monkeypatch, tmp_path):
    """In proof mode, an absent substrate brief.txt must RAISE DeepSweAdapterError —
    NOT fall back to host-side generate_v1r_brief (host GT scoring forbidden)."""
    _gt_env_clear(monkeypatch)
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))  # dir exists, brief.txt does NOT
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(tmp_path / "graph.db"))
    # A host graph.db + root present would otherwise tempt the legacy host-gen fallback.
    monkeypatch.setenv("GT_REPO_ROOT", str(tmp_path))
    with pytest.raises(agent_mod.DeepSweAdapterError, match="GT_ARTIFACT_MISSING"):
        agent_mod._generate_brief("fix the bug")


def test_hole3_brief_raises_in_proof_when_brief_empty(agent_mod, monkeypatch, tmp_path):
    """An EMPTY substrate brief is also fail-closed in proof mode (no brief-less paid run)."""
    _gt_env_clear(monkeypatch)
    (tmp_path / "brief.txt").write_text("   \n  ", encoding="utf-8")  # whitespace -> empty
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(tmp_path / "graph.db"))
    with pytest.raises(agent_mod.DeepSweAdapterError):
        agent_mod._generate_brief("fix the bug")


def test_hole3_brief_consumes_substrate_readonly(agent_mod, monkeypatch, tmp_path):
    """The happy path: a present substrate brief is consumed verbatim (read-only),
    never regenerated, no fallback."""
    _gt_env_clear(monkeypatch)
    (tmp_path / "brief.txt").write_text("SUBSTRATE BRIEF CONTENT", encoding="utf-8")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(tmp_path / "graph.db"))
    assert agent_mod._generate_brief("fix the bug") == "SUBSTRATE BRIEF CONTENT"


def test_hole3_brief_no_host_fallback_even_with_host_graph(agent_mod, monkeypatch, tmp_path):
    """Defence-in-depth: even if GT_GRAPH_DB + GT_REPO_ROOT point at a real graph, proof
    mode with an absent substrate brief still RAISES (never reaches host generate_v1r_brief).
    If a fallback existed, generate_v1r_brief would be importable+called; we assert raise."""
    _gt_env_clear(monkeypatch)
    db = tmp_path / "graph.db"
    _make_graph(db)
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))   # no brief.txt
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    monkeypatch.setenv("GT_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(db))
    with pytest.raises(agent_mod.DeepSweAdapterError):
        agent_mod._generate_brief("fix the bug")


# ───────────────────────── HOLE #6: ONE SUBSTRATE GRAPH ───────────────────────
def test_hole6_db_path_reads_host_graph_unconditionally(patch_mod, monkeypatch, tmp_path):
    _gt_env_clear(monkeypatch)
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(tmp_path / "graph.db"))
    assert patch_mod._db_path() == str(tmp_path / "graph.db")


def test_hole6_db_path_no_tmp_fallback_in_substrate_mode(patch_mod, monkeypatch):
    """In substrate/proof mode with GT_HOST_GRAPH_DB unset, _db_path must NOT silently
    return the legacy /tmp/graph.db (no divergent rebuild) — it returns '' (correct-or-
    quiet: the pillars then emit nothing)."""
    _gt_env_clear(monkeypatch)
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")  # substrate active, host graph unset
    assert patch_mod._db_path() == "", "substrate mode leaked the /tmp/graph.db fallback"
    # Proof-mode variant
    _gt_env_clear(monkeypatch)
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    assert patch_mod._db_path() == ""


def test_hole6_db_path_tmp_fallback_only_off_substrate(patch_mod, monkeypatch):
    """Off the substrate/proof path the legacy /tmp/graph.db fallback is preserved."""
    _gt_env_clear(monkeypatch)
    assert patch_mod._db_path() == "/tmp/graph.db"


def test_hole6_l6_reindex_off_in_substrate_mode(patch_mod, monkeypatch, tmp_path):
    """_invalidate_on_edit (L6) must short-circuit (no cache delete, no reindex subprocess)
    in substrate mode — the substrate graph is read-only and authoritative."""
    _gt_env_clear(monkeypatch)
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    cache = tmp_path / "gt_index.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(patch_mod, "_GT_INDEX_CACHE", str(cache))
    called = {"reindex": False}
    monkeypatch.setattr(patch_mod.subprocess, "run",
                        lambda *a, **k: called.__setitem__("reindex", True))
    patch_mod._invalidate_on_edit("a.py", str(tmp_path))
    assert cache.exists(), "L6 deleted the cache in substrate mode (must be a no-op)"
    assert called["reindex"] is False, "L6 ran a reindex subprocess in substrate mode"


# ───────────────────── HOLE #5: ENV INJECTION INTO THE CONTAINER ──────────────
def test_hole5_workflow_injects_gt_env_into_container():
    """The workflow's `pier run` must forward each GT substrate env var into the task
    container via --ae (the verified passthrough), and bind-mount /gt_artifacts."""
    wf = _WORKFLOW.read_text(encoding="utf-8")
    for needle in (
        "--mounts-json",
        "--ae GT_HOST_GRAPH_DB=",
        "--ae GT_CERT_DIR=",
        "--ae GT_PORTABLE_SUBSTRATE=",
        "--ae GT_FORBID_PREBUILT_GRAPH=",
        "--ae GT_PROOF_MODE=",
        "/gt_artifacts",
        # JSON is shell-escaped inside the bash double-quoted MOUNTS_JSON string.
        '\\"read_only\\":true',
        '\\"type\\":\\"bind\\"',
    ):
        assert needle in wf, f"workflow missing container env-injection token: {needle!r}"


def test_hole5_workflow_is_valid_yaml():
    import yaml
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert "jobs" in doc and "trial" in doc["jobs"]
    yaml.safe_load(_PIER_CFG.read_text(encoding="utf-8"))  # pier config still parses


# ───────────────────── substrate-active detection parity ──────────────────────
def test_substrate_active_parity(agent_mod, patch_mod, monkeypatch):
    """gt_agent and gt_mini_patch must agree on what 'substrate active' means."""
    for var in ("GT_PORTABLE_SUBSTRATE", "GT_HOST_GRAPH_DB", "GT_CERT_DIR"):
        _gt_env_clear(monkeypatch)
        monkeypatch.setenv(var, "1" if var == "GT_PORTABLE_SUBSTRATE" else "/x")
        assert agent_mod._substrate_active() is True
        assert patch_mod._substrate_active() is True
    _gt_env_clear(monkeypatch)
    assert agent_mod._substrate_active() is False
    assert patch_mod._substrate_active() is False
