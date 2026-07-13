"""Pin the step-0 BRIEF staging into the live-mini (Live-Lite) workflow (2026-07-13).

FINDING: the step-0 GT brief (brief.txt) is baked by the substrate-proof step to the host
artifacts dir (GT_CERT_DIR, default /tmp/gt) and bind-mounted READ-ONLY into the agent
container at /gt_artifacts (the ${HOST_ARTIFACTS}:${GT_C_ARTIFACTS}:ro mount) — but nothing
forwarded its path to gt_headless_runner.py, so the runner never read it and the agent saw the
issue text alone (step-0 channel dark on every mini run).

FIX (minimal, no new mount): the brief is ALREADY visible at /gt_artifacts/brief.txt via the
existing ro mount, exactly like graph.db is forwarded as GT_HOST_GRAPH_DB=${GT_C_ARTIFACTS}/
graph.db. So the trial `docker run` forwards GT_BRIEF_FILE=${GT_C_ARTIFACTS}/brief.txt on the
`-e` list; the runner reads it (default /gt_artifacts/brief.txt) and prepends it on the GT arm.

These are DETERMINISTIC staging pins (read the workflow + the runner, assert the in-container
path the workflow forwards on GT_BRIEF_FILE equals the mount target /gt_artifacts + /brief.txt).
They mirror tests/test_live_lite_l6_staging_pin_20260712.py. No live run — Stage-1 only.

RED-first: on the pre-fix tree these FAIL (no GT_BRIEF_FILE anywhere in the mini workflow, and
the runner has no GT_BRIEF_FILE reader). The mount + GT_C_ARTIFACTS=/gt_artifacts already exist
(the graph.db forward proves the mount is a valid brief source), so only the forward + reader
are new.
"""

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"
_RUNNER = _ROOT / "artifact_deepswe" / "gt_headless_runner.py"

# The in-container substrate-artifacts mount target (== GT_C_ARTIFACTS) and the brief basename.
_CONTAINER_ARTIFACTS = "/gt_artifacts"
_BRIEF_BASENAME = "brief.txt"


def _wf() -> str:
    return _WF.read_text(encoding="utf-8")


def test_workflow_is_valid_yaml():
    """Editing the staging must not corrupt the workflow document."""
    doc = yaml.safe_load(_wf())
    assert isinstance(doc, dict) and doc, "mini workflow did not parse to a mapping"


def test_workflow_defines_gt_c_artifacts_mount_target():
    """The substrate artifacts dir is mounted READ-ONLY at GT_C_ARTIFACTS (=/gt_artifacts); the
    baked brief.txt rides that existing mount, so no new mount is needed."""
    wf = _wf()
    assert f"GT_C_ARTIFACTS={_CONTAINER_ARTIFACTS}" in wf, "GT_C_ARTIFACTS mount target not /gt_artifacts"
    assert '-v "${HOST_ARTIFACTS}:${GT_C_ARTIFACTS}:ro"' in wf, (
        "the read-only substrate artifacts bind-mount (source of brief.txt) is missing/renamed"
    )


def test_workflow_forwards_gt_brief_file_env_on_docker_run():
    """GT_BRIEF_FILE must be forwarded to the container via `docker run -e`, pointing at the
    brief on the ro artifacts mount — mirrors the sibling GT_HOST_GRAPH_DB forward."""
    wf = _wf()
    assert '-e GT_BRIEF_FILE="${GT_C_ARTIFACTS}/brief.txt"' in wf, (
        "GT_BRIEF_FILE not forwarded on docker run (agent runner cannot find the step-0 brief)"
    )


def test_forwarded_brief_path_equals_mount_target_plus_basename():
    """The in-container path forwarded on GT_BRIEF_FILE MUST equal the artifacts mount target +
    /brief.txt, so the runner reads the baked brief off the durable ro mount (not a stray path).
    Both surfaces derive from GT_C_ARTIFACTS, and each is independently asserted, so drift trips a
    pin. Mirrors the GT_HOST_GRAPH_DB forward, which uses the same ${GT_C_ARTIFACTS}/ convention."""
    wf = _wf()
    resolved = f"{_CONTAINER_ARTIFACTS}/{_BRIEF_BASENAME}"
    assert f"GT_C_ARTIFACTS={_CONTAINER_ARTIFACTS}" in wf
    assert '-e GT_BRIEF_FILE="${GT_C_ARTIFACTS}/brief.txt"' in wf, (
        f"forwarded GT_BRIEF_FILE must resolve to the mounted brief path {resolved!r}"
    )
    # sibling parity: the graph.db forward proves the same mount is a valid substrate-file source
    assert '-e GT_HOST_GRAPH_DB="${GT_C_ARTIFACTS}/graph.db"' in wf, (
        "GT_HOST_GRAPH_DB sibling forward missing — the brief forward mirrors it"
    )


def test_runner_reads_forwarded_env_key():
    """Close the loop: the runner must actually read GT_BRIEF_FILE (the var the workflow forwards)
    and default to the mount path. Reads the runner source directly."""
    src = _RUNNER.read_text(encoding="utf-8")
    assert "GT_BRIEF_FILE" in src, "runner no longer reads GT_BRIEF_FILE"
    assert '"/gt_artifacts/brief.txt"' in src, "runner default brief path is not the /gt_artifacts mount"
    # the reader must be gated so the baseline arm never consumes the brief (byte-identical control)
    assert "GT_BASELINE" in src, "runner must gate the brief prepend on the baseline arm"
