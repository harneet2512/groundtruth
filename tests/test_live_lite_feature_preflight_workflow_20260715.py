"""Pins the exact-128 static authority preflight before any paid matrix."""
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"


def _steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["prepare"]["steps"]


def test_static_feature_preflight_is_persisted_then_enforced_before_matrix() -> None:
    steps = _steps()
    names = [step.get("name") for step in steps]
    generate_name = "Generate exact-128 static feature authority manifest"
    upload_name = "Persist static feature authority manifest"
    enforce_name = "Enforce static feature authority preflight before paid matrix"
    matrix_name = "Build Live Lite task matrix (swebench_live_lite.jsonl-driven)"
    assert names.index(generate_name) < names.index(upload_name) < names.index(enforce_name) < names.index(matrix_name)

    generate = steps[names.index(generate_name)]
    assert generate["id"] == "feature_preflight"
    assert generate["continue-on-error"] is True
    assert generate["if"] == "${{ inputs.baseline != true }}"
    assert "gt_dispatch_feature_preflight.py" in generate["run"]
    assert "--output artifacts/gt_static_dispatch_feature_manifest.json" in generate["run"]

    upload = steps[names.index(upload_name)]
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["if"] == "${{ inputs.baseline != true && always() }}"
    assert upload["with"]["path"] == "artifacts/gt_static_dispatch_feature_manifest.json"

    enforce = steps[names.index(enforce_name)]
    assert enforce["if"] == "${{ inputs.baseline != true }}"
    assert "steps.feature_preflight.outcome" in enforce["run"]
    assert "GT_STATIC_FEATURE_PREFLIGHT_FAILED" in enforce["run"]
