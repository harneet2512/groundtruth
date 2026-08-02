from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime.episode_state import EpisodeState


class _Model:
    def _prepare_messages_for_api(self, messages):
        return messages

    def _query(self, messages, **kwargs):
        return SimpleNamespace(id="", status="failed", choices=[])


class _Agent:
    def add_messages(self, *messages):
        return list(messages)

    def execute_actions(self, message):
        return []


def test_canonical_task_start_binds_terminal_obligation_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_EPISODE", EpisodeState(episode_id="task-span"))
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(seam, "_db_path", lambda: str(tmp_path / "graph.db"))
    attachment = seam.install_canonical_runtime(
        model=_Model(), agent=_Agent(),
        env={
            "GT_ATTEMPT_ID": "attempt-task-span",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_CANONICAL_JOURNAL": str(tmp_path / "runtime.sqlite3"),
            "GT_BRIEF_FILE": str(tmp_path / "absent.txt"),
        },
        task="Préface: remove `old_url` parameter.",
    )
    assert attachment.attached
    session = seam._EPISODE._terminal_evidence_session
    delta = session.obligation_delta()
    assert len(delta.changed) == 1
    assert delta.changed[0].task_anchor.startswith("task:")
    attachment.attempt_runtime.journal.close()


def test_failure_event_records_exact_episode_identity(tmp_path: Path, monkeypatch) -> None:
    from groundtruth.runtime import hypothesis_ledger
    from groundtruth.runtime.terminal_evidence import bind_episode_terminal_evidence

    episode = EpisodeState(episode_id="failure-session")
    bind_episode_terminal_evidence(
        episode, issue_text="remove `old_url`", task_revision="task:r1"
    )
    monkeypatch.setattr(seam, "_EPISODE", episode)
    monkeypatch.setattr(seam, "_GT_BASELINE", False)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(hypothesis_ledger, "classify_all", lambda _state, _event: ())
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    seam._gt_hypothesis_classify_turn("pytest -q", "FAILED test_widget: assert 1 == 2")
    assert len(episode.failure_fingerprints) == 2
    assert len(episode.last_failure_record["failure_identity_sha256"]) == 64
    assert episode.last_failure_record["pre_state_revision"]
