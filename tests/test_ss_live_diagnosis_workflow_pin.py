"""Pins the summarize job's evidence-preserving, fail-closed diagnosis bundle."""
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"


def _steps() -> list[dict]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return document["jobs"]["summarize"]["steps"]


def _step(name: str) -> dict:
    return next(step for step in _steps() if step.get("name") == name)


def test_collectors_are_captured_and_use_exact_download_root() -> None:
    run = _step("Build canonical PERF and exact-128 diagnosis")["run"]
    assert "set -euo pipefail" in run
    assert "python3 - <<'PY' || GT_EXPECTED_POPULATION_RC=$?" in run
    assert run.count("python3 - <<'PY' || GT_FEATURE_POPULATION_RC=$?") == 1
    assert run.count("python3 - <<'PY' || GT_MANIFEST_RC=$?") == 1
    assert "GT_RUN_METRICS_RC=0" in run
    assert "scripts/swebench/gt_run_metrics.py /tmp/all" in run
    assert "|| GT_RUN_METRICS_RC=$?" in run
    assert "GT_FEATURE_METRICS_RC=0" in run
    assert "scripts/swebench/gt_feature_metrics.py /tmp/all" in run
    assert "|| GT_FEATURE_METRICS_RC=$?" in run
    assert 'gt_run_metrics_v2_${GT_RUN_ID}.json' in run


def test_exact_128_live_diagnosis_uses_canonical_v2_artifact() -> None:
    run = _step("Build canonical PERF and exact-128 diagnosis")["run"]
    command = "scripts/swebench/ss_live_diagnosis.py /tmp/all"
    assert run.count(command) == 2, "the bundle must contain machine and human diagnosis"
    assert run.count(
        '--run-metrics "$GT_DIAG_DIR/gt_run_metrics_v2_${GT_RUN_ID}.json"'
    ) == 2
    assert 'ss_live_diagnosis_${GT_RUN_ID}.json' in run
    assert 'ss_live_diagnosis_${GT_RUN_ID}.md' in run
    assert "GT_SS_DIAGNOSIS_JSON_RC" in run
    assert "GT_SS_DIAGNOSIS_MD_RC" in run


def test_manifest_precedes_terminal_failure_and_upload_is_unconditional() -> None:
    run = _step("Build canonical PERF and exact-128 diagnosis")["run"]
    manifest = run.index("diagnosis_manifest.json")
    terminal = run.index("GT_DIAGNOSIS_BUNDLE_FAILED")
    assert manifest < terminal
    assert "GT_RUN_METRICS_RC" in run[manifest - 4000 : terminal]
    assert "GT_FEATURE_METRICS_RC" in run[manifest - 4000 : terminal]
    assert "exit 1" in run[terminal:]

    upload = _step("Upload GT diagnosis bundle")
    assert upload.get("if") == "${{ always() }}"
    assert upload["with"]["path"] == "/tmp/gt-diagnosis/"
