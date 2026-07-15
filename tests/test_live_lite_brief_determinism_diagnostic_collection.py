"""Preserve fail-closed brief determinism diagnostics in live task artifacts."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"


def _steps() -> list[dict]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return document["jobs"]["trial"]["steps"]


def _step(name: str) -> dict:
    return next(step for step in _steps() if step.get("name") == name)


def test_collect_preserves_optional_brief_determinism_diagnostic_fail_closed() -> None:
    collect = _step("Collect results")["run"]
    source = "/tmp/gt/brief_determinism_mismatch.json"
    destination = (
        "trial_results/gt_artifacts/brief_determinism_mismatch.json"
    )
    block_start = collect.index(f'if [ -e "{source}" ]; then')
    block_end = collect.index("fi", block_start) + len("fi")
    block = collect[block_start:block_end]

    assert f'cp "{source}" "{destination}"' in block
    assert f'rm -f "{destination}"' in block
    assert "GT_COLLECTION_COPY_FAIL:brief_determinism_mismatch.json" in block
    assert "exit 1" in block


def test_upload_includes_the_collected_diagnostic_directory_after_collection() -> None:
    steps = _steps()
    collect_index = next(
        index for index, step in enumerate(steps)
        if step.get("name") == "Collect results"
    )
    upload_index = next(
        index for index, step in enumerate(steps)
        if step.get("name") == "Upload results"
    )
    upload = steps[upload_index]

    assert collect_index < upload_index
    assert upload["if"] == "always()"
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"]["path"] == "trial_results/"

