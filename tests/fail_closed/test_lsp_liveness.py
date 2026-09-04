"""Stage 1 — LSP-liveness gate tests.

A residual==0 pass must be impossible without a warmed language server. These tests drive
`foundational_gates._classify_lsp` (and `gate_lsp`) with synthetic certificates covering the
required verdict matrix. No SWE-bench tasks, no gold, no per-repo logic — pure verdict logic.
"""

import importlib.util
import json
import os

_FG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "metrics", "foundational_gates.py"
)
_spec = importlib.util.spec_from_file_location("foundational_gates_t", _FG_PATH)
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)


def _base_cert(**kw):
    """A valid, warm, active-resolution v2 certificate; override fields per test.

    v2 (P1-g): carries install_missing_reason + verdict_hint — the fields whose ABSENCE
    marks a version-skewed (v1) cert that _classify_lsp must FAIL, never PASS."""
    c = {
        "schema": "gt.lsp_certificate.v2",
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
        "install_missing_reason": "",
        "verdict_hint": "",
        "lsp_started_at": 1000.0,
        "lsp_finished_at": 1002.0,
        "closure_rebuilt_after_lsp": True,
        "closure_rebuilt_at": 1003.0,
        "graph_hash_before_lsp": "aaa",
        "graph_hash_after_lsp": "bbb",
        "project_ready": True,
        "project_ready_wait_ms": 100.0,
        "project_ready_attempts": 1,
        "effective_work": 4,
        "failed_breakdown": {},
    }
    c.update(kw)
    if "effective_work" not in kw:
        c["effective_work"] = (
            int(c.get("verified_edges", 0) or 0)
            + int(c.get("corrected_edges", 0) or 0)
            + int(c.get("deleted_edges", 0) or 0)
        )
    return c


# ── the required verdict matrix ──────────────────────────────────────────────


def test_residual_zero_never_launched_fails():
    # FIX-A: a hard NO_WARM fail is reserved for a server that NEVER LAUNCHED
    # (binary missing / un-spawnable = a real substrate defect). server_launched=False.
    cert = _base_cert(
        lsp_warm=False,
        warm_probe_ok=False,
        probe_latency_ms=0.0,
        server_launched=False,
        residual=0,
        demand_edges=0,
        attempted_edges=0,
    )
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_FAIL_NO_WARM" and not ok


def test_residual_zero_warm_noop_valid_passes():
    cert = _base_cert(
        residual=0,
        demand_edges=0,
        attempted_edges=0,
        no_op_valid=True,
        no_op_reason="zero in-scope name_match method-call edges",
    )
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_NO_OP_VALID_WITH_WARM_SERVER" and ok


def test_demand_present_no_attempts_warm_warns():
    cert = _base_cert(demand_edges=5, residual=5, attempted_edges=0)
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_WARN_NOT_ATTEMPTED" and ok


def test_demand_present_never_launched_fails():
    # FIX-A: demand present, server NEVER LAUNCHED → hard NO_WARM fail (real defect).
    cert = _base_cert(
        demand_edges=5,
        residual=5,
        attempted_edges=0,
        server_launched=False,
        lsp_warm=False,
        warm_probe_ok=False,
        probe_latency_ms=0.0,
    )
    v, ok = fg._classify_lsp(cert)
    # Never-launched → fails at the warm check, never reaches the attempted check
    assert v == "LSP_FAIL_NO_WARM" and not ok


def test_demand_present_launched_not_warm_warns():
    # FIX-A: demand present, server LAUNCHED but not yet warm (cold rust-analyzer /
    # gopls still indexing) → WARN, not fail. The tree-sitter graph + contract/sibling/
    # cochange (deliver-always items 1/2/4) still reach the agent; the LSP residual is a
    # known dep-env/timing limitation on a live server, not a substrate defect.
    cert = _base_cert(
        demand_edges=5,
        residual=5,
        attempted_edges=0,
        server_launched=True,
        lsp_warm=False,
        warm_probe_ok=False,
        probe_latency_ms=0.0,
    )
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_WARN_NOT_READY" and ok


def test_stale_closure_fails():
    cert = _base_cert(lsp_finished_at=1002.0, closure_rebuilt_at=1001.0)
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_FAIL_STALE_CLOSURE" and not ok


def test_closure_not_rebuilt_fails():
    cert = _base_cert(closure_rebuilt_after_lsp=False)
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_FAIL_STALE_CLOSURE" and not ok


def test_server_command_exists_probe_not_run_warns():
    # FIX-A: binary launched but workspace/symbol probe never returned (latency 0) is a
    # WARN, not a fail — a live server still warming/indexing. Never-launched is the only
    # hard NO_WARM fail (see test_*_never_launched_fails).
    cert = _base_cert(
        server_launched=True, lsp_warm=False, warm_probe_ok=False, probe_latency_ms=0.0
    )
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_WARN_NOT_READY" and ok


def test_non_python_python_only_lsp_unsupported_explicit():
    cert = _base_cert(
        language="go",
        server_launched=False,
        lsp_warm=False,
        warm_probe_ok=False,
        probe_latency_ms=0.0,
        unsupported_reason="no LSP server installed for language 'go'",
    )
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_UNSUPPORTED_EXPLICIT" and ok


def test_scoring_before_lsp_finished_fails():
    cert = _base_cert(lsp_finished_at=None)
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_FAIL_NOT_RUN_BEFORE_SCORING" and not ok


def test_missing_certificate_fails():
    v, ok = fg._classify_lsp(None)
    assert v == "LSP_FAIL_MISSING_CERTIFICATE" and not ok


def test_v1_cert_without_new_fields_is_not_pass():
    """P1-g version skew: a v1-shaped cert (NO install_missing_reason, NO verdict_hint —
    e.g. emitted by a stale binary) must classify UNKNOWN -> FAIL, never PASS, even when
    every other field looks like a healthy warm active pass."""
    cert = _base_cert()
    del cert["install_missing_reason"]
    del cert["verdict_hint"]
    cert["schema"] = "gt.lsp_certificate.v1"
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_FAIL_CERT_VERSION_SKEW" and not ok


def test_v2_cert_with_empty_new_fields_still_judged_normally():
    """The skew check keys on FIELD PRESENCE, not truthiness: a v2 cert with the fields
    present-but-empty is judged on its real liveness fields (here: a healthy PASS)."""
    v, ok = fg._classify_lsp(_base_cert())
    assert v == "LSP_ACTIVE_VALID" and ok


def test_v1_cert_only_one_field_present_is_judged_normally():
    """A cert carrying EITHER new field has the v2 semantics — only both-absent is skew."""
    cert = _base_cert()
    del cert["install_missing_reason"]  # verdict_hint still present
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_ACTIVE_VALID" and ok


def test_active_valid_passes():
    v, ok = fg._classify_lsp(_base_cert())
    assert v == "LSP_ACTIVE_VALID" and ok


def test_warm_residual_zero_effective_work_warns_when_project_ready():
    cert = _base_cert(
        residual=5,
        demand_edges=5,
        attempted_edges=5,
        verified_edges=0,
        corrected_edges=0,
        deleted_edges=0,
        effective_work=0,
        project_ready=True,
        verdict_hint="LSP_WARN_ZERO_CONVERSION",
    )
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_WARN_ZERO_CONVERSION" and ok


def test_project_ready_false_zero_effective_work_warns():
    # FIX-A: a WARM server (transport answering) that cannot load the project
    # (project_ready=False — e.g. gopls with no `go list` metadata offline) and does
    # zero effective work is a WARN, not a fail. The server is live; the workspace just
    # isn't resolvable in this dep-env. deliver-always still ships the tree-sitter graph.
    cert = _base_cert(
        residual=5,
        demand_edges=5,
        attempted_edges=5,
        verified_edges=0,
        corrected_edges=0,
        deleted_edges=0,
        effective_work=0,
        project_ready=False,
        verdict_hint="LSP_WARN_NOT_READY",
    )
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_WARN_NOT_READY" and ok


def test_deleted_edges_count_as_effective_lsp_work():
    cert = _base_cert(
        residual=5,
        demand_edges=5,
        attempted_edges=5,
        verified_edges=0,
        corrected_edges=0,
        deleted_edges=2,
    )
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_ACTIVE_VALID" and ok


# ── gate_lsp end-to-end (cert path + line fallback) ──────────────────────────


def test_gate_lsp_reads_cert_arg():
    assert fg.gate_lsp("", cert=_base_cert()) is True
    # FIX-A: a never-launched server (server_launched=False) is the hard fail.
    assert (
        fg.gate_lsp(
            "",
            cert=_base_cert(
                server_launched=False, lsp_warm=False, warm_probe_ok=False, probe_latency_ms=0.0
            ),
        )
        is False
    )
    # A launched-but-not-warm server is a WARN → gate PASSES (deliver-always).
    assert (
        fg.gate_lsp(
            "",
            cert=_base_cert(
                server_launched=True, lsp_warm=False, warm_probe_ok=False, probe_latency_ms=0.0
            ),
        )
        is True
    )


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


def test_gate_lsp_warm_line_without_verdict_fails(tmp_path, monkeypatch):
    # No-cert fallback hardening: a warm LSP_METRICS line with residual>0 but NO verdict=
    # field must NOT synthesize LSP_ACTIVE_VALID (that skips the closure/timing/effective-work
    # cert checks). Fail-closed.
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)
    monkeypatch.delenv("GT_REQUIRE_LSP", raising=False)
    monkeypatch.setenv("GT_LSP_CERT", str(tmp_path / "nope.json"))
    assert (
        fg.gate_lsp("LSP_METRICS resolved=3 residual=5 scoped_source_files=3 lsp_warm=1") is False
    )
    # residual==0 warm line with no verdict is ALSO no longer a vacuous NO_OP pass.
    assert (
        fg.gate_lsp("LSP_METRICS resolved=0 residual=0 scoped_source_files=3 lsp_warm=1") is False
    )


def test_gate_lsp_no_cert_under_proof_mode_fails(tmp_path, monkeypatch):
    # Under GT_PROOF_MODE the cert is mandatory: even a warm line with an explicit pass verdict
    # must fail-closed (the codespace witness writes no cert -> cannot ride the line fallback).
    monkeypatch.setenv("GT_LSP_CERT", str(tmp_path / "nope.json"))
    line = "LSP_METRICS resolved=0 residual=0 scoped_source_files=3 lsp_warm=1 verdict=LSP_NO_OP_VALID_WITH_WARM_SERVER"
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    assert fg.gate_lsp(line) is False
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)
    monkeypatch.setenv("GT_REQUIRE_LSP", "1")
    assert fg.gate_lsp(line) is False
    # With both gates off, the explicit-verdict warm line still passes (non-proof witness).
    monkeypatch.delenv("GT_REQUIRE_LSP", raising=False)
    assert fg.gate_lsp(line) is True


# ── BUG#3 FIX: no-cert witness-reconcile (mirror gate_embedder_consumption) ──
# When the LSP cert is absent the gate must not pass blindly — but it must also not
# false-RED a run where an INDEPENDENT runtime witness (the LSP-stamped edges PERSISTED
# in the final graph.db the agent navigates) proves real conversion. Reconcile to PASS
# iff the witness shows conversion; fail-closed when NEITHER cert NOR witness shows it.


def test_gate_lsp_no_cert_no_witness_fails_closed(tmp_path, monkeypatch):
    # No cert, no conversion line, witness explicitly zero -> NEITHER source shows
    # conversion -> fail-closed (the blind-pass the bug describes must NOT happen).
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)
    monkeypatch.delenv("GT_REQUIRE_LSP", raising=False)
    monkeypatch.setenv("GT_LSP_CERT", str(tmp_path / "nope.json"))
    assert fg.gate_lsp("no contract line", graph_lsp_witness=0) is False


def test_gate_lsp_no_cert_witness_conversion_reconciles_to_pass(tmp_path, monkeypatch):
    # No cert, no usable line, but the FINAL graph carries LSP-stamped edges (real
    # conversion landed) -> reconcile to PASS (witness-over-gate, /goal §7), the same
    # discipline gate_embedder_consumption uses for the embedder certificate.
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)
    monkeypatch.delenv("GT_REQUIRE_LSP", raising=False)
    monkeypatch.setenv("GT_LSP_CERT", str(tmp_path / "nope.json"))
    assert fg.gate_lsp("no contract line", graph_lsp_witness=7) is True


def test_gate_lsp_proof_mode_no_cert_witness_conversion_reconciles(tmp_path, monkeypatch):
    # Under PROOF mode a missing cert hard-fails TODAY even when conversion really happened
    # (the dark-lever false-RED). With an independent witness of conversion, reconcile to
    # PASS; with no witness it still fails closed (existing test_*_under_proof_mode_fails).
    monkeypatch.setenv("GT_LSP_CERT", str(tmp_path / "nope.json"))
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    assert fg.gate_lsp("", graph_lsp_witness=4) is True
    # witness zero under proof mode -> still fail-closed (no conversion proven anywhere).
    assert fg.gate_lsp("", graph_lsp_witness=0) is False


def test_gate_lsp_witness_default_is_byte_identical(tmp_path, monkeypatch):
    # The reconcile is ADDITIVE: the default (witness not provided, -1) must reproduce the
    # pre-fix behavior exactly, so no existing caller/test regresses.
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)
    monkeypatch.delenv("GT_REQUIRE_LSP", raising=False)
    monkeypatch.setenv("GT_LSP_CERT", str(tmp_path / "nope.json"))
    # default -1 == "not provided" -> same FAIL as the legacy no-cert/no-line path
    assert fg.gate_lsp("no contract line here") is False
    # a cert-present PASS is unaffected by the witness param (cert is authoritative)
    assert fg.gate_lsp("", cert=_base_cert(), graph_lsp_witness=0) is True


def test_gate_lsp_loads_cert_from_file(tmp_path, monkeypatch):
    p = tmp_path / "lsp_certificate.json"
    p.write_text(json.dumps(_base_cert()), encoding="utf-8")
    monkeypatch.setenv("GT_LSP_CERT", str(p))
    assert fg.gate_lsp("") is True
    # FIX-A: a never-launched cert on disk must fail (server_launched=False).
    p.write_text(
        json.dumps(
            _base_cert(
                server_launched=False, lsp_warm=False, warm_probe_ok=False, probe_latency_ms=0.0
            )
        ),
        encoding="utf-8",
    )
    assert fg.gate_lsp("") is False
