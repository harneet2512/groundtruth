"""Pin the L6-FRESH gt-index staging into the live-mini (Live-Lite) workflow (#48).

GT_L6_FRESH's in-container reindex is a no-op unless a runnable `gt-index` binary is
present at the path the consumer reads from GT_INDEX_BIN. `swebench_live_lite_full.yml`
runs the agent via a direct `docker run` (no pier), so — unlike deepswe_full.yml — the
binary must be built into the /opt/gt bind-mount and GT_INDEX_BIN / GT_L6_FRESH must be
forwarded on the `docker run -e` line, not via `--ae`.

These are DETERMINISTIC staging pins (read the workflow + the consumer, assert the
in-container path the workflow stages equals the path the consumer expects). They mirror
`tests/test_deepswe_adapter_failclosed.py::test_hole5_*` but for the mini workflow.
No live run — Stage-1 correctness only.
"""

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"
_CONSUMER = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"

# The path convention shared with deepswe_full.yml / gt_agent_run.sh.
_MOUNT_SOURCE = "/tmp/gt_inject/opt/gt"   # HOST_GT_INJECT
_MOUNT_TARGET = "/opt/gt"                 # in-container
_INDEX_BASENAME = "gt-index"


def _wf() -> str:
    return _WF.read_text(encoding="utf-8")


def test_workflow_is_valid_yaml():
    """Editing the staging must not corrupt the workflow document."""
    doc = yaml.safe_load(_wf())
    assert isinstance(doc, dict) and doc, "mini workflow did not parse to a mapping"


def test_workflow_builds_gt_index_with_fts5_static_into_mounted_dir():
    """The binary must be built with the MANDATORY sqlite_fts5 tag, statically (execs in
    any task image), into HOST_GT_INJECT (which bind-mounts to /opt/gt)."""
    wf = _wf()
    assert "CC=musl-gcc go build" in wf, "L6 gt-index static/musl build step missing"
    assert "sqlite_fts5" in wf, "sqlite_fts5 tag missing — FTS5 would be compiled out"
    assert '-extldflags "-static"' in wf, "static link flag missing — binary may not exec in task image"
    assert '-o "${HOST_GT_INJECT}/gt-index"' in wf, "build output is not the mounted /opt/gt dir"


def test_workflow_forwards_l6_env_on_docker_run():
    """GT_INDEX_BIN + GT_L6_FRESH must be forwarded to the container via `docker run -e`,
    matching the mini workflow's ungated GT_* env-forward pattern."""
    wf = _wf()
    assert '-e GT_INDEX_BIN="/opt/gt/gt-index"' in wf, "GT_INDEX_BIN not forwarded on docker run"
    assert '-e GT_L6_FRESH="1"' in wf, "GT_L6_FRESH not forwarded on docker run"


def test_staged_binary_path_equals_consumer_expected_path():
    """The in-container path the workflow stages the binary to MUST equal the path the
    consumer reads from GT_INDEX_BIN. Derive it from the workflow's own mount + build
    output rather than hard-coding, so drift in either surface trips this pin."""
    wf = _wf()
    # The /opt/gt mount source==HOST_GT_INJECT, target==/opt/gt, read-only.
    assert f"{_MOUNT_SOURCE}:{_MOUNT_TARGET}:ro" in wf, "/opt/gt bind-mount missing/renamed"
    assert f"HOST_GT_INJECT={_MOUNT_SOURCE}" in wf, "HOST_GT_INJECT is not the /opt/gt mount source"
    # Binary is built into ${HOST_GT_INJECT}/gt-index -> in-container /opt/gt/gt-index.
    staged_in_container = f"{_MOUNT_TARGET}/{_INDEX_BASENAME}"
    assert f'-e GT_INDEX_BIN="{staged_in_container}"' in wf, (
        f"forwarded GT_INDEX_BIN must equal the staged in-container path {staged_in_container!r}"
    )


def test_consumer_reads_forwarded_env_key():
    """Close the loop: the consumer must actually read GT_INDEX_BIN (the var the workflow
    forwards) and gate the reindex on GT_L6_FRESH. Reads the consumer source directly."""
    src = _CONSUMER.read_text(encoding="utf-8")
    assert 'os.environ.get("GT_INDEX_BIN"' in src, "consumer no longer reads GT_INDEX_BIN"
    assert 'os.environ.get("GT_L6_FRESH")' in src, "consumer no longer gates on GT_L6_FRESH"
