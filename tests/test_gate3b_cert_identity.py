"""G7 pin: gate-3b embedder cert-reconcile must be IDENTITY-BOUND.

A STALE embedder_certificate from a PRIOR task (long-lived container) must NOT
flip a genuinely-failing gate to PASS. Reconcile only when the cert's
runtime_context_id matches the current run's.

Loaded by path — scripts/ is not a package.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "scripts" / "metrics" / "foundational_gates.py"
_spec = importlib.util.spec_from_file_location("foundational_gates", _MOD)
assert _spec and _spec.loader
fg = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fg
_spec.loader.exec_module(fg)


# Metrics that make ok=False (flat sem_components) but p1=True (w_sem>0), so the
# cert-reconcile branch runs — the exact state the identity check guards.
_FLAT_METRICS = {
    "effective_w_sem": 0.15,
    "semantic_signal_count": 0,
    "rendered_candidate_count": 3,
    "k_sem_top": 3,
    "sem_components": [0.0, 0.0, 0.0],
}


def _write_cert(path: Path, ctx_id: str) -> None:
    path.write_text(json.dumps({
        "schema": "gt.embedder_certificate.v1",
        "runtime_context_id": ctx_id,
        "upstream_semantic_nonzero_count": 5,
        "rendered_semantic_nonzero_count": 0,
        "effective_w_sem": 0.15,
    }), encoding="utf-8")


def test_g7_matching_context_id_reconciles_to_pass(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fg, "_load_brief_metrics", lambda *a, **k: dict(_FLAT_METRICS))
    cert = tmp_path / "embedder_certificate.json"
    _write_cert(cert, "CTX-MATCH")
    monkeypatch.setenv("GT_EMBEDDER_CERT", str(cert))
    monkeypatch.setenv("GT_CONTEXT_ID", "CTX-MATCH")
    assert fg.gate_embedder_consumption("db", "repo", "issue") is True


def test_g7_mismatched_context_id_refuses_reconcile(tmp_path, monkeypatch) -> None:
    """The stale-cert attack: a valid-looking cert from ANOTHER run must NOT pass."""
    monkeypatch.setattr(fg, "_load_brief_metrics", lambda *a, **k: dict(_FLAT_METRICS))
    cert = tmp_path / "embedder_certificate.json"
    _write_cert(cert, "CTX-STALE-OTHER-TASK")
    monkeypatch.setenv("GT_EMBEDDER_CERT", str(cert))
    monkeypatch.setenv("GT_CONTEXT_ID", "CTX-CURRENT")
    assert fg.gate_embedder_consumption("db", "repo", "issue") is False


def test_g7_missing_cert_context_id_refuses_reconcile(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fg, "_load_brief_metrics", lambda *a, **k: dict(_FLAT_METRICS))
    cert = tmp_path / "embedder_certificate.json"
    cert.write_text(json.dumps({
        "upstream_semantic_nonzero_count": 5,
        "rendered_semantic_nonzero_count": 0,
        "effective_w_sem": 0.15,
        # NO runtime_context_id
    }), encoding="utf-8")
    monkeypatch.setenv("GT_EMBEDDER_CERT", str(cert))
    monkeypatch.setenv("GT_CONTEXT_ID", "CTX-CURRENT")
    assert fg.gate_embedder_consumption("db", "repo", "issue") is False
