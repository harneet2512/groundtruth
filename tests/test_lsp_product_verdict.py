"""P1-06 — product verdict must not collapse to a single lsp_warm boolean."""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_lsp_certificate_schema_has_polyglot_verdicts_not_warm_only():
    """LSP cert uses per-language verdict fields; lsp_warm alone is not product truth."""
    sample = {
        "schema": "gt.lsp_certificate.v1",
        "languages": {
            "go": {"verdict": "LSP_FAIL_NOT_READY", "project_ready": False},
            "typescript": {"verdict": "LSP_ACTIVE_VALID", "project_ready": True},
        },
        "aggregate_verdict": "LSP_FAIL_NOT_READY",
    }
    assert "lsp_warm" not in sample
    assert sample["aggregate_verdict"] != "warm"
    langs = sample["languages"]
    assert all("verdict" in v for v in langs.values())


def test_proof_progress_includes_memory_heartbeat(tmp_path):
    import importlib.util
    import sys

    path = _ROOT / "scripts" / "swebench" / "gt_run_proof.py"
    spec = importlib.util.spec_from_file_location("gt_run_proof_uut", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gt_run_proof_uut"] = mod
    spec.loader.exec_module(mod)
    tracker = mod._ProofTracker(str(tmp_path))
    tracker.complete("env_validation")
    data = json.loads((tmp_path / "proof_progress.json").read_text(encoding="utf-8"))
    stage = data["stages"][-1]
    assert stage["stage"] == "env_validation"
    assert "rss_kb" in stage


def test_lsp_ready_budget_owned_by_proof_runtime():
    import importlib.util
    import sys

    path = _ROOT / "scripts" / "swebench" / "gt_run_proof.py"
    spec = importlib.util.spec_from_file_location("gt_run_proof_budget_uut", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gt_run_proof_budget_uut"] = mod
    spec.loader.exec_module(mod)

    assert mod.lsp_ready_budget_seconds("go", {}) == 30
    assert mod.lsp_ready_budget_seconds("rust", {}) == 45
    assert mod.lsp_ready_budget_seconds("typescript", {}) == 20
    assert mod.lsp_ready_budget_seconds("python", {}) == 20
    assert mod.lsp_ready_budget_seconds("go", {"GT_LSP_READY_BUDGET_S_OVERRIDE": "99"}) == 99
