"""B3-#7 — LSP warm-gate HONESTY (red->green).

The bug: the LSP "active/valid" certification self-certifies as ``LSP_ACTIVE_VALID`` from
TRANSPORT-LIVENESS alone — the process merely REPLIED (``lsp_warm`` / ``warm_probe_ok``) —
while ~44% of definition lookups returned empty. Transport-liveness is NOT readiness.

The fix, in two places (strict file ownership: only these two + this test):
  * ``foundational_gates._classify_lsp`` — ACTIVE_VALID now requires demonstrated readiness
    (``probe_answered_ok`` AND ``project_ready`` not RECORDED-False); otherwise LSP_DEGRADED.
  * ``LSPClient.warm_verdict`` — the honest higher-level verdict layered above the raw
    ``probe_ready`` transport report (which still returns transport-liveness, unchanged),
    plus an ``empty_definition_lookups`` counter incremented whenever a definition lookup
    answers empty (the "~44% empty" made measurable at the dispatch layer).

RED before the fix: a transport-alive-but-not-ready cert/client certifies ACTIVE_VALID.
GREEN after: it certifies LSP_DEGRADED, while a genuinely-ready one still reaches ACTIVE_VALID.

MUTATION-CHECK (proves the assertions are load-bearing):
  * revert ``_classify_lsp`` to ``return ("LSP_ACTIVE_VALID", True)`` (drop the readiness
    gate = transport-only) -> the two DEGRADED asserts below fail.
  * revert ``LSPClient.warm_verdict`` to ``return "LSP_ACTIVE_VALID"`` -> the client
    DEGRADED assert fails.
"""

from __future__ import annotations

import importlib.util
import os
import sys

# Pin the CO-LOCATED worktree `src` (src-layout) ahead of any external editable install so
# this test verifies the code IN ITS OWN TREE — not whatever `groundtruth` happens to be
# pip-installed elsewhere (the documented editable-install pytest gotcha). conftest.py
# pre-imports `groundtruth`, binding the package __path__ to the installed copy before this
# module loads, so we also evict those cached modules and re-import from the worktree src.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if getattr(sys.modules.get("groundtruth"), "__file__", "").replace("\\", "/").find(
    _SRC.replace("\\", "/")
) != 0:
    for _m in [k for k in list(sys.modules) if k == "groundtruth" or k.startswith("groundtruth.")]:
        del sys.modules[_m]

import pytest  # noqa: E402

from groundtruth.lsp.client import LSPClient  # noqa: E402
from groundtruth.utils.result import Ok  # noqa: E402

# Load foundational_gates.py the same way tests/fail_closed/test_lsp_liveness.py does
# (it lives under scripts/metrics, not an importable package).
_FG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "metrics", "foundational_gates.py"
)
_spec = importlib.util.spec_from_file_location("foundational_gates_warm_gate", _FG_PATH)
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)


def _warm_active_cert(**kw):
    """A transport-warm, active-resolution v2 cert that reaches the ACTIVE_VALID decision
    point (effective_work > 0). Override readiness fields per test."""
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
        "effective_work": 4,
        "failed_breakdown": {"empty": 0},
    }
    c.update(kw)
    return c


# ── cert-classifier (the actual certifier) ───────────────────────────────────

def test_transport_alive_but_not_ready_is_degraded_not_active_valid():
    """RED before fix: a transport-warm server that never answered a readiness probe
    (probe_answered_ok=False) and whose workspace never loaded (project_ready=False) —
    yet did some effective work — certified ACTIVE_VALID from transport alone. It must now
    certify LSP_DEGRADED (honest), never ACTIVE_VALID."""
    cert = _warm_active_cert(probe_answered_ok=False, project_ready=False)
    verdict, ok = fg._classify_lsp(cert)
    assert verdict == "LSP_DEGRADED", (
        f"transport-alive-but-not-ready must be DEGRADED, got {verdict}"
    )
    # DEGRADED is a deliver-always PASS (correct-or-quiet): the tree-sitter graph still
    # reaches the agent; only the FALSE 'active/valid' label is corrected.
    assert ok is True


def test_project_ready_false_alone_is_degraded():
    """Either readiness signal RECORDED False downgrades ACTIVE_VALID -> DEGRADED."""
    cert = _warm_active_cert(probe_answered_ok=True, project_ready=False)
    verdict, ok = fg._classify_lsp(cert)
    assert verdict == "LSP_DEGRADED" and ok is True


def test_probe_answered_ok_false_alone_is_degraded():
    cert = _warm_active_cert(probe_answered_ok=False, project_ready=True)
    verdict, ok = fg._classify_lsp(cert)
    assert verdict == "LSP_DEGRADED" and ok is True


def test_genuinely_ready_server_still_active_valid():
    """GREEN / no-regression: readiness proven (both True) -> ACTIVE_VALID as before."""
    cert = _warm_active_cert(probe_answered_ok=True, project_ready=True)
    verdict, ok = fg._classify_lsp(cert)
    assert verdict == "LSP_ACTIVE_VALID" and ok is True


def test_legacy_cert_without_probe_field_not_regressed():
    """A cert that never RECORDED probe_answered_ok (legacy fixture / demand=0 no-op) is
    judged on its other liveness fields — `is False` (not `is not True`) preserves the
    existing genuinely-ready pass. project_ready=True, probe_answered_ok absent."""
    cert = _warm_active_cert(project_ready=True)  # no probe_answered_ok key at all
    assert "probe_answered_ok" not in cert
    verdict, ok = fg._classify_lsp(cert)
    assert verdict == "LSP_ACTIVE_VALID" and ok is True


def test_gate_output_carries_readiness_fields_and_empty_counter():
    """The gate axis result must CARRY the honest readiness fields + the empty-lookup
    counter, so the '~44% empty' gap is measurable downstream."""
    cert = _warm_active_cert(
        probe_answered_ok=False, project_ready=False,
        failed_breakdown={"empty": 44, "lsp_error": 0, "exception": 0},
    )
    fg.gate_lsp("", cert=cert)
    axis = fg._DEEP["gate_lsp"]
    assert axis["verdict"] == "LSP_DEGRADED"
    assert axis["lsp_degraded"] is True
    assert axis["probe_answered_ok"] is False
    assert axis["project_ready"] is False
    assert float(axis["empty_definition_lookups"]) == 44.0
    # DEGRADED is transport-alive but NOT product-ready — surfaced honestly.
    assert axis["lsp_transport_ok"] is True
    assert axis["lsp_product_ready"] is False


# ── client-level honest verdict (the "stub client" path) ─────────────────────

def _stub_client() -> LSPClient:
    # __init__ spawns no subprocess — safe to construct and poke readiness attrs directly.
    return LSPClient(server_command=["fake-server", "--stdio"], root_uri="file:///project")


def test_client_warm_verdict_transport_alive_not_ready_is_degraded():
    """RED-equivalent at the client level: a client that is transport-alive but whose
    probes never answered must NOT certify ACTIVE_VALID from transport alone."""
    c = _stub_client()
    c.probe_answered_ok = False
    c.project_ready = False
    assert c.warm_verdict() == "LSP_DEGRADED"


def test_client_warm_verdict_ready_is_active_valid():
    c = _stub_client()
    c.probe_answered_ok = True
    c.project_ready = True
    assert c.warm_verdict() == "LSP_ACTIVE_VALID"


def test_client_default_readiness_is_degraded():
    """A freshly-constructed client (nothing warmed yet) is DEGRADED, never a fake
    ACTIVE_VALID — defaults are probe_answered_ok=False, project_ready=None."""
    c = _stub_client()
    assert c.probe_answered_ok is False
    assert c.project_ready is None
    assert c.empty_definition_lookups == 0
    assert c.warm_verdict() == "LSP_DEGRADED"


# ── empty-definition-lookup counter (the "~44% empty" made measurable) ───────

def _valid_location() -> dict:
    return {
        "uri": "file:///project/x.py",
        "range": {"start": {"line": 3, "character": 0}, "end": {"line": 3, "character": 7}},
    }


@pytest.mark.asyncio
async def test_definition_empty_result_increments_counter(monkeypatch):
    c = _stub_client()

    async def _empty_none(*_a, **_k):
        return Ok(None)  # server answered with no definition

    monkeypatch.setattr(c, "send_request", _empty_none)
    assert c.empty_definition_lookups == 0
    await c.definition("file:///project/x.py", 3, 4)
    assert c.empty_definition_lookups == 1

    async def _empty_list(*_a, **_k):
        return Ok([])  # answered with an empty location list

    monkeypatch.setattr(c, "send_request", _empty_list)
    await c.definition("file:///project/x.py", 3, 4)
    assert c.empty_definition_lookups == 2


@pytest.mark.asyncio
async def test_definition_nonempty_result_does_not_increment_counter(monkeypatch):
    c = _stub_client()

    async def _hit(*_a, **_k):
        return Ok([_valid_location()])

    monkeypatch.setattr(c, "send_request", _hit)
    res = await c.definition("file:///project/x.py", 3, 4)
    assert isinstance(res, Ok)
    assert len(res.value) == 1
    assert c.empty_definition_lookups == 0  # a real hit is NOT an empty lookup


def test_readiness_fields_snapshot_shape():
    c = _stub_client()
    c.probe_answered_ok = True
    c.project_ready = True
    c.empty_definition_lookups = 7
    snap = c.readiness_fields()
    assert snap == {
        "probe_answered_ok": True,
        "project_ready": True,
        "empty_definition_lookups": 7,
        "warm_verdict": "LSP_ACTIVE_VALID",
    }
