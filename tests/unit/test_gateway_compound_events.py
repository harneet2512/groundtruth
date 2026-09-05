import subprocess
import sys

import pytest

from groundtruth.runtime.gateway import (
    CoveringResult,
    GatewayState,
    ToolEvent,
    produce_raw,
)


@pytest.mark.parametrize("edited", [False, True])
def test_executed_covering_result_survives_compound_edit_event(tmp_path, monkeypatch, edited):
    monkeypatch.setenv("GT_GATEWAY", "1")
    (tmp_path / "app.py").write_text("value = 2\n")
    execution = subprocess.run(
        [sys.executable, "-c", "assert 2 == 1, 'expected 1 but got 2'"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=tmp_path,
    )
    assert execution.returncode != 0
    event = ToolEvent(
        kind="edit" if edited else "test",
        command="python -m pytest",
        output=execution.stderr,
        exit_status=execution.returncode,
        cwd=str(tmp_path),
        changed_files=("app.py",) if edited else (),
        semantic_events=("edit_result", "test_result") if edited else ("test_result",),
        semantics_authoritative=True,
        test_outcome="fail",
        covering=CoveringResult(
            target="app.py",
            verdict="fail",
            body_lines=execution.stderr.splitlines(),
            evidence=[("app.py", 1)],
        ),
    )
    state = GatewayState(repo_root=str(tmp_path))
    candidates = produce_raw(event, state)
    assert any(candidate.evidence_type == "covering_verdict" for candidate in candidates)
    assert len(state.edit_events) == int(edited)


def test_authoritative_empty_event_does_not_invent_edit_or_test(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    state = GatewayState(repo_root=str(tmp_path))
    event = ToolEvent(kind="edit", test_outcome="fail", semantics_authoritative=True)
    assert produce_raw(event, state) == []
    assert state.edit_events == []
