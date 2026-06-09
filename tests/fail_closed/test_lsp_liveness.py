"""Stage 1 — LSP-liveness gate tests.

A residual==0 pass must be impossible without a warmed language server. These tests drive
`foundational_gates._classify_lsp` (and `gate_lsp`) with synthetic certificates covering the
required verdict matrix. No SWE-bench tasks, no gold, no per-repo logic — pure verdict logic.
"""
import importlib.util
import json
import os

_FG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "metrics", "foundational_gates.py")
_spec = importlib.util.spec_from_file_location("foundational_gates_t", _FG_PATH)
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)


def _base_cert(**kw):
    """A valid, warm, active-resolution certificate; override fields per test."""
    c = {
        "schema": "gt.lsp_certificate.v1",
        "language": "python",
        "server_command": "pyright-langserver",
        "server_launched": True,
        "warm_probe_ok": True,
        "lsp_warm": True,
        "probe_method": "workspace/symbol",
        "probe_latency_ms": 12.5,
        "demand_edges": 5,
        "attempted_edges": 5,
        "residual": 5,
        "verified_edges": 3,
        "corrected_edges": 1,
        "deleted_edges": 0,
        "failed_edges": 1,
        "no_op_valid": False,
        "no_op_reason": "",
        "unsupported_reason": "",
        "lsp_started_at": 1000.0,
        "lsp_finished_at": 1002.0,
        "closure_rebuilt_after_lsp": True,
        "closure_rebuilt_at": 1003.0,
        "graph_hash_before_lsp": "aaa",
        "graph_hash_after_lsp": "bbb",
    }
    c.update(kw)
    return c


# ── the required verdict matrix ──────────────────────────────────────────────

def test_residual_zero_no_warm_fails():
    cert = _base_cert(lsp_warm=False, warm_probe_ok=False, probe_latency_ms=0.0,
                      residual=0, demand_edges=0, attempted_edges=0)
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_FAIL_NO_WARM" and not ok


def test_residual_zero_warm_noop_valid_passes():
    cert = _base_cert(residual=0, demand_edges=0, attempted_edges=0,
                      no_op_valid=True, no_op_reason="zero in-scope name_match method-call edges")
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_NO_OP_VALID_WITH_WARM_SERVER" and ok


def test_demand_present_no_attempts_fails():
    cert = _base_cert(demand_edges=5, residual=5, attempted_edges=0)
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_FAIL_NOT_RUN_BEFORE_SCORING" and not ok


def test_stale_closure_fails():
    cert = _base_cert(lsp_finished_at=1002.0, closure_rebuilt_at=1001.0)
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_FAIL_STALE_CLOSURE" and not ok


def test_closure_not_rebuilt_fails():
    cert = _base_cert(closure_rebuilt_after_lsp=False)
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_FAIL_STALE_CLOSURE" and not ok


def test_server_command_exists_probe_not_run_fails():
    # binary launched but workspace/symbol probe never returned (latency 0)
    cert = _base_cert(server_launched=True, warm_probe_ok=False, probe_latency_ms=0.0)
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_FAIL_NO_WARM" and not ok


def test_non_python_python_only_lsp_unsupported_explicit():
    cert = _base_cert(language="go", server_launched=False, lsp_warm=False,
                      warm_probe_ok=False, probe_latency_ms=0.0,
                      unsupported_reason="no LSP server installed for language 'go'")
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_UNSUPPORTED_EXPLICIT" and ok


def test_scoring_before_lsp_finished_fails():
    cert = _base_cert(lsp_finished_at=None)
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_FAIL_NOT_RUN_BEFORE_SCORING" and not ok


def test_missing_certificate_fails():
    v, ok = fg._classify_lsp(None)
    assert v == "LSP_FAIL_MISSING_CERTIFICATE" and not ok


def test_active_valid_passes():
    v, ok = fg._classify_lsp(_base_cert())
    assert v == "LSP_ACTIVE_VALID" and ok


# ── gate_lsp end-to-end (cert path + line fallback) ──────────────────────────

def test_gate_lsp_reads_cert_arg():
    assert fg.gate_lsp("", cert=_base_cert()) is True
    assert fg.gate_lsp("", cert=_base_cert(lsp_warm=False, warm_probe_ok=False,
                                           probe_latency_ms=0.0)) is False


def test_gate_lsp_missing_cert_and_no_line_fails(tmp_path, monkeypatch):
    # point the cert path at a nonexistent file so the loader returns None
    monkeypatch.setenv("GT_LSP_CERT", str(tmp_path / "nope.json"))
    assert fg.gate_lsp("no contract line here") is False


def test_gate_lsp_line_without_warm_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_LSP_CERT", str(tmp_path / "nope.json"))
    # legacy 3-field line (no lsp_warm) must NOT vacuously pass on residual==0
    assert fg.gate_lsp("LSP_METRICS resolved=0 residual=0 scoped_source_files=0") is False


def test_gate_lsp_line_with_warm_noop_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_LSP_CERT", str(tmp_path / "nope.json"))
    line = "LSP_METRICS resolved=0 residual=0 scoped_source_files=3 lsp_warm=1 verdict=LSP_NO_OP_VALID_WITH_WARM_SERVER"
    assert fg.gate_lsp(line) is True


def test_gate_lsp_loads_cert_from_file(tmp_path, monkeypatch):
    p = tmp_path / "lsp_certificate.json"
    p.write_text(json.dumps(_base_cert()), encoding="utf-8")
    monkeypatch.setenv("GT_LSP_CERT", str(p))
    assert fg.gate_lsp("") is True
    # a no-warm cert on disk must fail
    p.write_text(json.dumps(_base_cert(lsp_warm=False, warm_probe_ok=False, probe_latency_ms=0.0)),
                 encoding="utf-8")
    assert fg.gate_lsp("") is False
