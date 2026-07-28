"""C17: canonical post-edit graph facts require a refreshed graph generation.

The healthy canonical route bypasses ``_augment_output_legacy``, which owns both
existing calls to ``_invalidate_on_edit``.  Repository revision invalidation is
not a substitute: a newly produced fact is stamped with the current canonical
revision and therefore looks fresh even when its graph rows came from the
unchanged pre-edit database.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr


API_BEFORE = "def get_user(uid):\n    return [uid]\n"
API_AFTER = "def get_user(uid, name):\n    return [uid]\n"
CALLER_BEFORE = "def use():\n    return get_user(1)\n"
CALLER_AFTER = "def use():\n    return []\n"


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


def _write_graph(db) -> None:
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (
          id INTEGER PRIMARY KEY,
          label TEXT,
          name TEXT,
          file_path TEXT,
          start_line INTEGER,
          is_test INTEGER,
          language TEXT
        );
        CREATE TABLE edges (
          id INTEGER PRIMARY KEY,
          source_id INTEGER,
          target_id INTEGER,
          type TEXT,
          source_line INTEGER,
          source_file TEXT,
          resolution_method TEXT,
          confidence REAL,
          metadata TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
        [
            (1, "Function", "get_user", "src/api.py", 1, 0, "python"),
            (2, "Function", "use", "src/caller.py", 1, 0, "python"),
        ],
    )
    con.execute(
        "INSERT INTO edges VALUES (1,2,1,'CALLS',2,'src/caller.py',"
        "'import',1.0,NULL)"
    )
    con.commit()
    con.close()


def _initialize_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    src = repo / "src"
    src.mkdir(parents=True)
    (src / "api.py").write_text(API_BEFORE, encoding="utf-8")
    (src / "caller.py").write_text(CALLER_BEFORE, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "c17@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "C17 fixture"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "src"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "fixture"],
        cwd=repo,
        check=True,
    )
    db = tmp_path / "graph.db"
    _write_graph(db)
    return repo, db


def _observe_edit(attachment, path, before: str, after: str) -> None:
    action = {
        "command": "str_replace",
        "path": path,
        "old_str": before,
        "new_str": after,
    }
    attachment.observe_action_proposal(action)
    # This is the exact pre-execute sensing step in `_wrap_execute`.
    seam._gateway_capture_edit_preimage(action)
    (Path(seam._root()) / path).write_text(after, encoding="utf-8")
    native_result = {"output": "edit applied", "returncode": 0}
    attachment.observe_action_result(action, native_result)
    assert native_result == {"output": "edit applied", "returncode": 0}


def _observe_view(attachment, path: str, content: str) -> None:
    action = {"command": "view", "path": path}
    attachment.observe_action_proposal(action)
    attachment.observe_action_result(
        action,
        {"output": content, "returncode": 0},
    )


def test_canonical_edit_suppresses_graph_fact_when_reindex_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    """A removed call must not survive as a newly minted, revision-fresh fact."""
    repo, db = _initialize_repo(tmp_path)
    monkeypatch.setenv(
        "GT_INDEX_BIN",
        str(tmp_path / "missing-gt-index"),
    )
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(repo))
    monkeypatch.setattr(seam, "_db_path", lambda: str(db))
    ledger = tmp_path / "runtime.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))

    attachment = seam.install_canonical_runtime(
        model=_Model(),
        agent=_Agent(),
        env={
            "GT_ATTEMPT_ID": "attempt-c17-graph-freshness",
            "GT_RUNTIME_LEDGER": str(ledger),
            "GT_CANONICAL_JOURNAL": str(tmp_path / "canonical.sqlite3"),
            "GT_BRIEF_FILE": str(tmp_path / "absent-brief.txt"),
        },
        task="change get_user without breaking its callers",
    )
    initial_graph_revision = attachment.attempt_runtime.work_state.revision.graph

    # Establish the exact active identity without letting the initial view producer stage a
    # gateway fact. This makes the later caller_break relevant to PATCH_CONSTRUCTION.
    _observe_view(attachment, "src/api.py", API_BEFORE)
    assert "src/api.py::get_user" in (
        attachment.attempt_runtime.work_state.focused_symbols
    )
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")

    # The first canonical edit invalidates the graph's caller edge. The healthy route currently
    # performs no L6 reindex, so graph.db still says `use -> get_user`.
    _observe_edit(attachment, "src/caller.py", CALLER_BEFORE, CALLER_AFTER)
    assert "get_user" not in (repo / "src/caller.py").read_text(encoding="utf-8")

    # The second edit activates caller_contract. A stale graph claims the removed caller.
    _observe_edit(attachment, "src/api.py", API_BEFORE, API_AFTER)
    degraded_after_edit = attachment.attempt_runtime.work_state.revision.graph

    # Degradation is attempt state, not an edit-turn filter. The next view must not reopen
    # the same stale graph generation and mint the caller fact.
    _observe_view(attachment, "src/api.py", API_AFTER)

    caller_records = tuple(
        record
        for record in attachment.attempt_runtime._evidence.values()
        if record.feature_id == "caller_contract"
    )
    try:
        assert caller_records == (), (
            "canonical post-edit production accepted a caller fact from an unreindexed graph: "
            f"{caller_records!r}"
        )
        graph_revision = attachment.attempt_runtime.work_state.revision.graph
        assert graph_revision.startswith("degraded:")
        assert graph_revision == degraded_after_edit
        assert graph_revision != initial_graph_revision
        rows = tuple(
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        refresh_rows = tuple(
            row
            for row in rows
            if row.get("layer") == "canonical_runtime.graph_refresh"
        )
        assert refresh_rows
        assert all(row.get("chars_delivered") == 0 for row in refresh_rows)
        assert any(
            "reindex_binary_unavailable" in str(row.get("reason") or "")
            for row in refresh_rows
        )
    finally:
        attachment.attempt_runtime.journal.close()


def test_success_on_another_path_does_not_clear_prior_failed_graph_refresh(
    tmp_path,
    monkeypatch,
) -> None:
    """A partial refresh cannot certify graph-wide freshness after an earlier failure."""
    repo, db = _initialize_repo(tmp_path)
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(repo))
    monkeypatch.setattr(seam, "_db_path", lambda: str(db))

    def refresh(relative: str, root: str) -> seam._L6RefreshOutcome:
        assert root == str(repo)
        if relative == "src/caller.py":
            return seam._L6RefreshOutcome(False, "reindex_rc:1", str(db))
        return seam._L6RefreshOutcome(True, "reindex_ok", str(db))

    monkeypatch.setattr(seam, "_invalidate_on_edit", refresh)
    attachment = seam.install_canonical_runtime(
        model=_Model(),
        agent=_Agent(),
        env={
            "GT_ATTEMPT_ID": "attempt-c17-partial-refresh",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_CANONICAL_JOURNAL": str(tmp_path / "canonical.sqlite3"),
            "GT_BRIEF_FILE": str(tmp_path / "absent-brief.txt"),
        },
        task="change get_user without breaking its callers",
    )

    _observe_view(attachment, "src/api.py", API_BEFORE)
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")
    _observe_edit(attachment, "src/caller.py", CALLER_BEFORE, CALLER_AFTER)
    _observe_edit(attachment, "src/api.py", API_BEFORE, API_AFTER)
    degraded_after_edit = attachment.attempt_runtime.work_state.revision.graph
    _observe_view(attachment, "src/api.py", API_AFTER)

    graph_records = tuple(
        record
        for record in attachment.attempt_runtime._evidence.values()
        if "graph" in record.observed_substrates
    )
    try:
        assert graph_records == ()
        assert attachment.unresolved_graph_refreshes == {
            "src/caller.py": "reindex_rc:1"
        }
        assert degraded_after_edit.startswith("degraded:")
        assert attachment.attempt_runtime.work_state.revision.graph == (
            degraded_after_edit
        )
    finally:
        attachment.attempt_runtime.journal.close()


def test_canonical_edit_refreshes_graph_before_post_edit_revision_and_producers(
    tmp_path,
    monkeypatch,
) -> None:
    """A successful existing-L6 refresh supplies the generation used by producers."""
    repo, db = _initialize_repo(tmp_path)
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(repo))
    monkeypatch.setattr(seam, "_db_path", lambda: str(db))
    refreshes: list[str] = []

    def refresh(relative: str, root: str) -> seam._L6RefreshOutcome:
        assert root == str(repo)
        refreshes.append(relative)
        if relative == "src/caller.py":
            con = sqlite3.connect(db)
            try:
                con.execute("DELETE FROM edges WHERE source_id=2 AND target_id=1")
                con.commit()
            finally:
                con.close()
        return seam._L6RefreshOutcome(True, "reindex_ok", str(db))

    monkeypatch.setattr(seam, "_invalidate_on_edit", refresh)
    attachment = seam.install_canonical_runtime(
        model=_Model(),
        agent=_Agent(),
        env={
            "GT_ATTEMPT_ID": "attempt-c17-graph-refresh-success",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_CANONICAL_JOURNAL": str(tmp_path / "canonical.sqlite3"),
            "GT_BRIEF_FILE": str(tmp_path / "absent-brief.txt"),
        },
        task="change get_user without breaking its callers",
    )

    _observe_view(attachment, "src/api.py", API_BEFORE)
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")
    _observe_edit(attachment, "src/caller.py", CALLER_BEFORE, CALLER_AFTER)
    _observe_edit(attachment, "src/api.py", API_BEFORE, API_AFTER)

    caller_records = tuple(
        record
        for record in attachment.attempt_runtime._evidence.values()
        if record.feature_id == "caller_contract"
    )
    try:
        assert caller_records == ()
        assert refreshes == ["src/caller.py", "src/api.py"]
        graph_revision = attachment.attempt_runtime.work_state.revision.graph
        assert not graph_revision.startswith("degraded:")
        assert graph_revision == seam._canonical_file_digest(str(db))
    finally:
        attachment.attempt_runtime.journal.close()
