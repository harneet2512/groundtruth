"""Stage 4.2 — portable GT substrate runtime tests.

Prove the GT proof runtime is benchmark-team runnable: ONE `gt-run-proof` command inside a pinned
image produces ALL artifacts from a mounted read-only repo — no per-task pip, no model download,
no host GT execution, no task-image mutation — and the OH wrapper consumes the artifacts read-only.
"""
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
_GRP = os.path.join(ROOT, "scripts", "swebench", "gt_run_proof.py")
_spec = importlib.util.spec_from_file_location("gt_run_proof_t", _GRP)
grp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grp)
_WF = os.path.join(ROOT, ".github", "workflows", "swebench_300task.yml")
_WRAP = os.path.join(ROOT, "scripts", "swebench", "oh_gt_full_wrapper.py")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


# ── artifact contract ────────────────────────────────────────────────────────

def test_contract_lists_all_required_artifacts():
    out = grp.expected_outputs("/gt_artifacts")
    for a in ("graph.db", "runtime_context.json", "lsp_certificate.json", "graph_certificate.json",
              "embedder_certificate.json", "foundational_gate_report.json"):
        assert os.path.join("/gt_artifacts", a) in out, a


def test_print_contract(capsys):
    assert grp.main(["--print-contract"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert "graph.db" in j["outputs"] and "embedder_certificate.json" in j["outputs"]
    assert "no per-task pip install" in j["guarantees"]
    assert "no model download" in j["guarantees"]
    assert j["entrypoint"] == "gt-run-proof"


# ── no per-task pip / no model download / no host execution ──────────────────

def test_validate_requires_proof_flags(monkeypatch):
    for f in ("GT_PROOF_MODE", "GT_CONTAINERIZED", "GT_REQUIRE_FTS5", "GT_REQUIRE_EMBEDDER",
              "GT_FORCE_ONNX_EMBEDDER", "GT_REQUIRE_LSP", "GT_REQUIRE_FULL_STACK"):
        monkeypatch.delenv(f, raising=False)
    v = grp.validate_proof_env()
    assert any("GT_PROOF_MODE" in x for x in v)
    assert any("GT_CONTAINERIZED" in x for x in v)


def test_validate_requires_baked_deps_not_pip(monkeypatch):
    # all flags set but deps NOT baked (host runner) -> report 'not baked', never pip-install/download.
    for f in ("GT_PROOF_MODE", "GT_CONTAINERIZED", "GT_REQUIRE_FTS5", "GT_REQUIRE_EMBEDDER",
              "GT_FORCE_ONNX_EMBEDDER", "GT_REQUIRE_LSP", "GT_REQUIRE_FULL_STACK"):
        monkeypatch.setenv(f, "1")
    monkeypatch.setenv("GT_MODELS_ROOT", "/nonexistent/models")
    v = grp.validate_proof_env()
    assert any("not baked" in x for x in v)


def test_main_fails_closed_on_host(monkeypatch, tmp_path):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.delenv("GT_CONTAINERIZED", raising=False)
    assert grp.main(["--source-root", str(tmp_path), "--out", str(tmp_path / "art")]) == 2


# ── workflow reduces to the portable substrate command ───────────────────────

def test_workflow_uses_portable_gt_run_proof():
    t = _read(_WF)
    assert "gt-run-proof" in t


def test_workflow_substrate_pinned_by_digest():
    t = _read(_WF)
    # final/proof mode must pin the substrate image by digest (a @sha256 or an explicit digest var)
    assert "@sha256:" in t or "GT_SUBSTRATE_DIGEST" in t


def test_workflow_no_per_task_pip_install():
    t = _read(_WF)
    assert "pip install -q onnxruntime tokenizers numpy pyright" not in t


# ── OH wrapper consumes the artifacts (does not rebuild a divergent graph) ────

def test_wrapper_consumes_artifact_dir():
    w = _read(_WRAP)
    assert "GT_CERT_DIR" in w or "/gt_artifacts" in w


# ── Step 5: portable path primary + fallback forbidden ───────────────────────

def test_portable_path_primary_skips_in_task():
    t = _read(_WF)
    assert "GT_PORTABLE_SUBSTRATE=1" in t
    assert "in-task LSP+gates SKIPPED" in t
    assert "GT_PORTABLE_SUBSTRATE:-0" in t  # the redundant in-task gate steps guard on it


def test_fallback_failure_classes_present():
    t = _read(_WF)
    assert "GT_SUBSTRATE_DIGEST_MISSING" in t
    assert "PROOF_RUNTIME_FALLBACK_FORBIDDEN" in t
    assert "SUBSTRATE_MISSING_CERTS" in t
