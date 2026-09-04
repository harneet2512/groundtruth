"""Tests for scripts/swebench/reconcile.py."""

from __future__ import annotations

import importlib.util
import os
import sys


def _load():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "swebench", "reconcile.py")
    spec = importlib.util.spec_from_file_location("reconcile", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reconcile"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_witness_overrides_missing_handoff():
    mod = _load()
    signal = {
        "gt_prebuilt_active": True,
        "hook_hash_match": True,
        "gt_meta_present": True,
        "cert_verdicts": {
            "graph_certificate.json": {
                "is_fail": True,
                "verdict": "GRAPH_FAIL_MISSING_HANDOFF",
            }
        },
    }
    out = mod.reconcile_graph_handoff(signal)
    assert out["graph_handoff"] == "witness_overrides"
    assert out["witness_holds"] is True
