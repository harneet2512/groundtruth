"""Stage 4 — container-boundary lockdown tests.

Prove (1) the RUNTIME guarantee: GT fails-closed FINAL_PIPELINE_HOST_SPLIT_FAIL if it runs on
the host in proof mode (so the pipeline can never silently host-split), and (2) the WORKFLOW
structure: the host/image split is killed — LSP + foundational gates run via `docker exec gtsrc`
with the 8 proof flags, substrate is forbidden under proof, and the certificates upload. No
SWE-bench tasks, no gold, no per-repo logic.
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
_WF = os.path.join(ROOT, ".github", "workflows", "swebench_300task.yml")


def _wf_text():
    with open(_WF, encoding="utf-8") as f:
        return f.read()


# ── (1) runtime boundary assertion ───────────────────────────────────────────

def test_boundary_inert_outside_proof(monkeypatch):
    from groundtruth.runtime.context import assert_container_boundary
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)
    assert_container_boundary("x")  # no raise


def test_boundary_raises_on_host_in_proof(monkeypatch):
    from groundtruth.runtime.context import assert_container_boundary
    from groundtruth.runtime.proof import GTProofModeError
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.delenv("GT_CONTAINERIZED", raising=False)
    with pytest.raises(GTProofModeError) as e:
        assert_container_boundary("foundational_gates")
    assert "FINAL_PIPELINE_HOST_SPLIT_FAIL" in str(e.value)


def test_boundary_raises_with_flag_but_host_cgroup(monkeypatch):
    # GT_CONTAINERIZED set but cgroup/.dockerenv say host (the test runner) -> still fail.
    from groundtruth.runtime.context import assert_container_boundary
    from groundtruth.runtime.proof import GTProofModeError
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_CONTAINERIZED", "1")
    with pytest.raises(GTProofModeError):
        assert_container_boundary("foundational_gates")


def test_foundational_gates_main_fails_on_host_in_proof(monkeypatch):
    fg_path = os.path.join(ROOT, "scripts", "metrics", "foundational_gates.py")
    spec = importlib.util.spec_from_file_location("fg_cl", fg_path)
    fg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fg)
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.delenv("GT_CONTAINERIZED", raising=False)
    monkeypatch.setattr(sys, "argv", ["foundational_gates.py", "/tmp/does_not_exist.db"])
    assert fg.main() == 1  # FINAL_PIPELINE_HOST_SPLIT_FAIL before any gate runs


# ── (2) workflow structure: host/image split killed ──────────────────────────

def test_workflow_yaml_parses():
    import yaml
    with open(_WF, encoding="utf-8") as f:
        yaml.safe_load(f)


def test_lsp_runs_in_container_not_host():
    t = _wf_text()
    assert "groundtruth.resolve --db /tmp/graph.db --root \"$ROOT\"" in t
    # the old host LSP invocation must be gone
    assert "python -m groundtruth.resolve --db /tmp/gt/graph.db --root /tmp/gt/src" not in t


def test_gates_run_in_container_not_host():
    t = _wf_text()
    assert "/opt/gt/scripts/metrics/foundational_gates.py" in t
    assert 'python3 scripts/metrics/foundational_gates.py "$DB"' not in t


def test_all_proof_flags_on_in_container_execs():
    t = _wf_text()
    for flag in ("GT_PROOF_MODE=1", "GT_CONTAINERIZED=1", "GT_REQUIRE_FTS5=1",
                 "GT_REQUIRE_EMBEDDER=1", "GT_FORCE_ONNX_EMBEDDER=1", "GT_REQUIRE_LSP=1",
                 "GT_REQUIRE_FULL_STACK=1", "GT_FORBID_PREBUILT_GRAPH=1"):
        assert flag in t, f"missing in-container exec flag: {flag}"


def test_legacy_divergent_substrate_forbidden():
    # Stage 4.1: forbid the LEGACY DIVERGENT shell gate, NOT unified substrate.
    t = _wf_text()
    assert "LEGACY_DIVERGENT_SUBSTRATE_FORBIDDEN" in t
    assert "SUBSTRATE_PROOF_PATH_FORBIDDEN" not in t  # the old blanket-forbid wording is gone


def test_classify_runtime_strategy():
    from groundtruth.runtime.context import classify_runtime_strategy
    assert classify_runtime_strategy(gate_module="foundational_gates.py", in_container=False, proof=True) == ("HOST_GT_EXEC_FORBIDDEN", False)
    assert classify_runtime_strategy(gate_module="gt-substrate-run.sh", in_container=True, proof=True) == ("LEGACY_DIVERGENT_SUBSTRATE_FORBIDDEN", False)
    assert classify_runtime_strategy(gate_module="foundational_gates.py", in_container=True, proof=True) == ("UNIFIED_GT_SUBSTRATE_OK", True)


def test_run_v74_forbids_host_in_proof(monkeypatch):
    # Stage 4.1: the host-primary brief proof leak is closed — run_v74 on the host in proof fails.
    from groundtruth.pretask.v7_4_brief import run_v74
    from groundtruth.runtime.proof import GTProofModeError
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.delenv("GT_CONTAINERIZED", raising=False)
    with pytest.raises(GTProofModeError) as e:
        run_v74("some issue", "/tmp/repo", "/tmp/does_not_exist.db")
    assert "FINAL_PIPELINE_HOST_SPLIT_FAIL" in str(e.value)


def test_run_v74_guard_inert_outside_proof(monkeypatch):
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)
    from groundtruth.pretask.v7_4_brief import run_v74
    from groundtruth.runtime.proof import GTProofModeError
    try:
        run_v74("x", "/tmp/repo", "/tmp/does_not_exist.db")
    except GTProofModeError:
        pytest.fail("boundary guard must be inert outside proof mode")
    except Exception:
        pass  # other failures (missing db etc.) are fine; we only assert the proof guard is inert


def test_certificates_collected_and_uploaded():
    t = _wf_text()
    for cert in ("lsp_certificate.json", "graph_certificate.json", "embedder_certificate.json"):
        assert cert in t, f"certificate not collected/uploaded: {cert}"
    # certs are docker cp'd OUT of the container
    assert "docker cp" in t and "gtsrc:/tmp/gt/lsp_certificate.json" in t


def test_agent_path_receives_proof_env():
    t = _wf_text()
    assert 'GT_PROOF_MODE: "1"' in t and 'GT_CONTAINERIZED: "1"' in t


def test_gtsrc_kept_alive_and_provisioned():
    t = _wf_text()
    assert "GT runtime provisioned" in t
    assert "docker cp \"${{ github.workspace }}/src/groundtruth\" gtsrc:/opt/gt/groundtruth" in t
