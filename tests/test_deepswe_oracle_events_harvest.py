"""FIX 3 (2026-06-11 measurement-gate closeout) — oracle telemetry harvest.

The oracle gate writes 8-dp suppression telemetry to GT_ORACLE_EVENTS
(default /tmp/gt_oracle_events.jsonl) INSIDE the task container
(gt_mini_patch._oracle_telemetry_write), but deepswe_full.yml never extracted
it — the per-emission decision record (the §15.2 "full suppression telemetry
even on silence") was lost on every run.

Red->green: the workflow (a) bind-mounts a WRITABLE host dir into the task
container (the /gt_artifacts substrate mount is read-only by design, so the
events file cannot go there), (b) forwards GT_ORACLE_EVENTS via the verified
`--ae` env path pointing into that mount, and (c) the always() Collect step
copies the events file into trial_results/ (which the existing Upload step
ships wholesale). Plus a yaml-parse lint of the whole workflow.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows" / "deepswe_full.yml"


def _text() -> str:
    return _WF.read_text(encoding="utf-8")


def test_workflow_yaml_parses():
    data = yaml.safe_load(_text())
    assert isinstance(data, dict) and "jobs" in data


def test_oracle_events_env_forwarded_into_container():
    t = _text()
    assert "--ae GT_ORACLE_EVENTS=" in t, (
        "GT_ORACLE_EVENTS is not forwarded into the task container — the "
        "oracle's suppression telemetry stays at the in-container default "
        "and is never harvested"
    )


def test_oracle_events_written_to_writable_mount():
    t = _text()
    # The substrate mount (/gt_artifacts) is read-only by design; the events
    # file must target a WRITABLE mount.
    # MOUNTS_JSON quotes are shell-escaped (\") inside the run block.
    assert '"read_only":false' in t.replace(" ", "").replace("\\", ""), (
        "no writable bind mount in MOUNTS_JSON — the container cannot "
        "persist the oracle events file to the host"
    )


def test_collect_step_harvests_oracle_events():
    t = _text()
    assert "gt_oracle_events" in t and "trial_results" in t
    # the Collect step must copy the harvested events file into trial_results/
    assert any(
        ("gt_oracle_events" in line and "trial_results" in line) for line in t.splitlines()
    ), "Collect step does not copy gt_oracle_events*.jsonl into trial_results/"
