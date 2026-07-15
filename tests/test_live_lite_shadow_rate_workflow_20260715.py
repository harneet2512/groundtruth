"""Pins explicit, fail-closed Live-Lite shadow-rate dispatch plumbing."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"
AE_BLOCK = ROOT / "artifact_deepswe" / "gt_integration" / "gt_ae_block.sh"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: str) -> list[dict]:
    return _workflow()["jobs"][job]["steps"]


def _step(job: str, name: str) -> dict:
    return next(step for step in _steps(job) if step.get("name") == name)


def _dispatch_inputs() -> dict:
    doc = _workflow()
    # PyYAML 1.1 resolves the plain key ``on`` as boolean True.
    trigger = doc.get("on") or doc.get(True)
    return trigger["workflow_dispatch"]["inputs"]


def _validator_code() -> str:
    run = _step(
        "prepare", "Shadow-rate precheck (fail-closed before paid matrix)"
    )["run"]
    match = re.search(r"python3 <<'PY'\n(.*?)\nPY(?:\n|$)", run, re.DOTALL)
    assert match, "shadow-rate precheck must use an executable Python validator"
    return match.group(1)


def _run_validator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str) -> dict[str, str]:
    output = tmp_path / "github-output.txt"
    monkeypatch.setenv("GT_SHADOW_RATE_REQUESTED", raw)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    exec(compile(_validator_code(), "<shadow-rate-precheck>", "exec"), {})
    return dict(
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
    )


def test_dispatch_input_and_prepare_output_are_explicit() -> None:
    shadow = _dispatch_inputs()["shadow_rate"]
    assert shadow["required"] is True
    assert shadow["default"] == "0"

    prepare = _workflow()["jobs"]["prepare"]
    assert prepare["outputs"]["shadow_rate_requested"] == (
        "${{ steps.shadow_rate.outputs.requested }}"
    )
    assert prepare["outputs"]["shadow_rate_effective"] == (
        "${{ steps.shadow_rate.outputs.effective }}"
    )
    names = [step.get("name") for step in _steps("prepare")]
    assert names.index("Shadow-rate precheck (fail-closed before paid matrix)") < names.index(
        "Build Live Lite task matrix (swebench_live_lite.jsonl-driven)"
    )


@pytest.mark.parametrize(
    ("raw", "effective"),
    [("0", "0"), ("0.5000", "0.5"), ("1", "1"), ("1.000", "1")],
)
def test_shadow_rate_validator_preserves_request_and_canonicalizes_effective(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str, effective: str,
) -> None:
    values = _run_validator(monkeypatch, tmp_path, raw)
    assert values == {"requested": raw, "effective": effective}


@pytest.mark.parametrize(
    "raw", ["", "-0.1", "1.0001", "nan", "inf", ".5", "1e-1", " 0.5", "0.5 "]
)
def test_shadow_rate_validator_rejects_malformed_or_out_of_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str,
) -> None:
    with pytest.raises(SystemExit):
        _run_validator(monkeypatch, tmp_path, raw)


def test_effective_rate_reaches_seam_and_is_cross_sealed() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    ae_block = AE_BLOCK.read_text(encoding="utf-8")
    trial = _workflow()["jobs"]["trial"]
    run_identity = _step("trial", "Run-identity gate (gt_math R01-R04 — resolved identity + fail-closed integrity before spend)")["run"]
    paid = _step("trial", "Run GT Pro trial")["run"]
    collect = _step("trial", "Collect results")["run"]

    assert trial["env"]["GT_SS_SHADOW_RATE_REQUESTED"] == (
        "${{ needs.prepare.outputs.shadow_rate_requested }}"
    )
    assert trial["env"]["GT_SS_SHADOW_RATE"] == (
        "${{ needs.prepare.outputs.shadow_rate_effective }}"
    )
    assert '-e GT_SS_SHADOW_RATE_REQUESTED="${GT_SS_SHADOW_RATE_REQUESTED}"' in paid
    assert '-e GT_SS_SHADOW_RATE="${GT_SS_SHADOW_RATE}"' in paid
    assert '--ae "GT_SS_SHADOW_RATE=${GT_SS_SHADOW_RATE:-0}"' in ae_block

    for field in ("shadow_rate_requested", "shadow_rate_effective"):
        assert field in run_identity
        assert field in paid
        assert field in collect
    assert "GT_TASK_COMPLETION_INVALID:shadow_rate" in collect
    assert '"shadow_rate_requested": identity["shadow_rate_requested"]' in collect
    assert '"shadow_rate_effective": identity["shadow_rate_effective"]' in collect
    assert "GT_SHADOW_RATE_INVALID" in workflow
