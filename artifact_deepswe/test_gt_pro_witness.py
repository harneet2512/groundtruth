#!/usr/bin/env python3
"""Red->green tests for the A3 Pro certify->consume witness (gt_pro_runner._verify_graph_witness).

Fakes the two runtime deps (gt_mini_patch._db_path, groundtruth.runtime.proof.graph_edges_hash)
via sys.modules so the fail-closed DECISION is testable on the host:

  * REQUIRED-stack run + consumed-hash != certified-hash  -> HARD-STOP (SystemExit 1)   [was: no witness at all]
  * REQUIRED-stack run + consumed-hash == certified-hash  -> proceeds (no raise)
  * unflagged iteration run + mismatch                    -> print-only best-effort (no raise)

Mutation: flip the `hook_hash == post_lsp_hash` comparison in the runner and the match/mismatch
tests swap -> both fail.
"""
from __future__ import annotations

import json
import os
import sys
import types

import pytest

_HERE = os.path.dirname(__file__)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


@pytest.fixture(autouse=True)
def _isolate_sys_modules():
    """These tests inject FAKE gt_mini_patch / groundtruth.runtime.proof into sys.modules;
    snapshot+restore so the fakes never leak into other test files sharing the process
    (a fake proof module lacking is_proof_mode broke the FTS localizer test otherwise)."""
    keys = ("gt_mini_patch", "groundtruth", "groundtruth.runtime",
            "groundtruth.runtime.proof", "gt_pro_runner")
    saved = {k: sys.modules.get(k) for k in keys}
    yield
    for k in keys:
        if saved[k] is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = saved[k]


def _install_fakes(db_path: str, hook_hash: str) -> None:
    gmp = types.ModuleType("gt_mini_patch")
    gmp._db_path = lambda: db_path            # type: ignore[attr-defined]
    gmp._install = lambda *a, **k: None       # type: ignore[attr-defined]
    sys.modules["gt_mini_patch"] = gmp
    g = types.ModuleType("groundtruth")
    gr = types.ModuleType("groundtruth.runtime")
    gp = types.ModuleType("groundtruth.runtime.proof")
    gp.graph_edges_hash = lambda p: hook_hash  # type: ignore[attr-defined]
    g.runtime = gr                             # type: ignore[attr-defined]
    gr.proof = gp                              # type: ignore[attr-defined]
    sys.modules["groundtruth"] = g
    sys.modules["groundtruth.runtime"] = gr
    sys.modules["groundtruth.runtime.proof"] = gp


def _load_runner():
    sys.modules.pop("gt_pro_runner", None)
    import gt_pro_runner
    return gt_pro_runner


def _fixture(tmp_path, cert_after_lsp: str, hook_hash: str):
    db = tmp_path / "graph.db"
    db.write_bytes(b"edges")
    cert = tmp_path / "certs"
    cert.mkdir()
    (cert / "lsp_certificate.json").write_text(
        json.dumps({"graph_hash_after_lsp": cert_after_lsp}), encoding="utf-8")
    _install_fakes(str(db), hook_hash)
    return str(cert)


def test_witness_fail_closed_on_mismatch(tmp_path, monkeypatch):
    cert = _fixture(tmp_path, cert_after_lsp="CERT_DIFFERENT", hook_hash="HOOK_ABC")
    monkeypatch.setenv("GT_REQUIRE_FULL_STACK", "1")
    monkeypatch.setenv("GT_CERT_DIR", cert)
    r = _load_runner()
    with pytest.raises(SystemExit) as ei:
        r._verify_graph_witness()
    assert ei.value.code == 1


def test_witness_passes_on_match(tmp_path, monkeypatch):
    cert = _fixture(tmp_path, cert_after_lsp="HOOK_ABC", hook_hash="HOOK_ABC")
    monkeypatch.setenv("GT_REQUIRE_FULL_STACK", "1")
    monkeypatch.setenv("GT_CERT_DIR", cert)
    r = _load_runner()
    r._verify_graph_witness()  # must NOT raise


def test_witness_best_effort_when_unflagged(tmp_path, monkeypatch):
    cert = _fixture(tmp_path, cert_after_lsp="CERT_DIFFERENT", hook_hash="HOOK_ABC")
    monkeypatch.delenv("GT_REQUIRE_FULL_STACK", raising=False)
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)
    monkeypatch.setenv("GT_CERT_DIR", cert)
    r = _load_runner()
    r._verify_graph_witness()  # print-only best-effort, no raise
