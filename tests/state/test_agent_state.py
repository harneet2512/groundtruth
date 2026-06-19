"""Tests for ``groundtruth.state.agent_state`` (FINAL_ARCH_V2 Layer 2).

Covers the four guarantees promised in DECISIONS.md `## FINAL_ARCH_V2` §3 Layer 2:

- canonical repo-relative path normalization
- TTL expiry of pending suggestions
- two parallel tasks use separate state files (isolation)
- mocked read/edit/search trajectories produce the expected canonical state

Also covers the backwards-compatibility contract:

- ``trajectory/state.py`` still re-exports L5TrajectoryState et al.
- legacy tmp-file mirrors are written on demand
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from groundtruth.state.agent_state import (
    AgentState,
    IterationBand,
    PendingSuggestion,
    SuggestionStatus,
    _agent_state_path,
    canonical_repo_path,
)


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------


class TestCanonicalRepoPath:
    def test_strips_repo_root_prefix(self) -> None:
        assert canonical_repo_path("/testbed/pkg/mod.py", "/testbed") == "pkg/mod.py"

    def test_strips_workspace_repo_prefix(self) -> None:
        # /workspace/<repo>/<path> -> <path>
        assert canonical_repo_path("/workspace/beets/beets/importer.py") == "beets/importer.py"

    def test_strips_leading_dot_slash(self) -> None:
        assert canonical_repo_path("./pkg/mod.py") == "pkg/mod.py"

    def test_handles_backslashes(self) -> None:
        assert canonical_repo_path("pkg\\mod.py") == "pkg/mod.py"

    def test_empty_returns_empty(self) -> None:
        assert canonical_repo_path("") == ""
        assert canonical_repo_path(None) == ""  # type: ignore[arg-type]

    def test_idempotent(self) -> None:
        first = canonical_repo_path("/testbed/a/b.py", "/testbed")
        assert canonical_repo_path(first, "/testbed") == first

    def test_no_match_returns_input_normalized(self) -> None:
        # Path that's not under the given root is returned with slashes only
        # (no false stripping). Leading slash is consumed by lstrip.
        got = canonical_repo_path("/other/loc/x.py", "/testbed")
        assert got == "other/loc/x.py"


# ---------------------------------------------------------------------------
# Construction + iteration band
# ---------------------------------------------------------------------------


class TestConstructionAndBand:
    def test_create_initializes_legacy(self) -> None:
        s = AgentState.create(task_id="task-1", max_iterations=100, repo_root="/testbed")
        assert s.task_id == "task-1"
        assert s.max_iterations == 100
        assert s.iteration == 0
        assert s.legacy.instance_id == "task-1"

    def test_band_transitions(self) -> None:
        s = AgentState.create(task_id="t", max_iterations=100)
        s.set_iteration(10)
        assert s.band == IterationBand.EARLY_EXPLORATION
        s.set_iteration(30)
        assert s.band == IterationBand.MID_COMMITMENT
        s.set_iteration(75)
        assert s.band == IterationBand.LATE_REPAIR
        s.set_iteration(95)
        assert s.band == IterationBand.FINALIZATION

    def test_set_iteration_propagates_to_legacy(self) -> None:
        s = AgentState.create(task_id="t", max_iterations=100)
        s.set_iteration(40)
        assert s.legacy.current_iter == 40
        assert s.legacy.max_iter == 100


# ---------------------------------------------------------------------------
# Record view / edit / search
# ---------------------------------------------------------------------------


class TestRecordView:
    def test_view_canonicalizes_path(self) -> None:
        s = AgentState.create(task_id="t", repo_root="/testbed")
        canon = s.record_view("/testbed/pkg/x.py", sync_legacy_file=False)
        assert canon == "pkg/x.py"
        assert s.viewed_files[0].path == "pkg/x.py"
        assert s.current_focus == "pkg/x.py"

    def test_view_recorded_once_per_canonical_path(self) -> None:
        s = AgentState.create(task_id="t", repo_root="/testbed")
        s.record_view("/testbed/pkg/x.py", sync_legacy_file=False)
        s.record_view("./pkg/x.py", sync_legacy_file=False)
        s.record_view("pkg/x.py", sync_legacy_file=False)
        assert len(s.viewed_files) == 1
        assert s.viewed_files[0].view_count == 3

    def test_view_tracks_iter_window(self) -> None:
        s = AgentState.create(task_id="t", repo_root="/testbed")
        s.set_iteration(5)
        s.record_view("a.py", sync_legacy_file=False)
        s.set_iteration(12)
        s.record_view("a.py", sync_legacy_file=False)
        v = s.viewed_files[0]
        assert v.first_iter == 5
        assert v.last_iter == 12
        assert v.view_count == 2

    def test_visited_files_set_returns_canonical(self) -> None:
        s = AgentState.create(task_id="t", repo_root="/testbed")
        s.record_view("/testbed/a.py", sync_legacy_file=False)
        s.record_view("/testbed/b.py", sync_legacy_file=False)
        assert s.visited_files_set() == {"a.py", "b.py"}


class TestRecordEdit:
    def test_edit_canonicalizes_and_dedups(self) -> None:
        s = AgentState.create(task_id="t", repo_root="/testbed")
        s.record_edit("/testbed/pkg/x.py")
        s.record_edit("pkg/x.py")
        assert s.edited_files == ["pkg/x.py"]
        assert s.current_file == "pkg/x.py"
        assert s.current_focus == "pkg/x.py"

    def test_edit_syncs_to_legacy(self) -> None:
        s = AgentState.create(task_id="t", repo_root="/testbed")
        s.record_edit("/testbed/a.py")
        assert "a.py" in s.legacy.edited_source_files


class TestRecordSearch:
    def test_search_appends(self) -> None:
        s = AgentState.create(task_id="t")
        s.set_iteration(3)
        s.record_search("grep -r foo", hits=5)
        s.set_iteration(7)
        s.record_search("rg bar")
        assert len(s.searches) == 2
        assert s.searches[0].iter == 3
        assert s.searches[0].hits == 5
        assert s.searches[1].command == "rg bar"


# ---------------------------------------------------------------------------
# Pending suggestion + TTL expiry
# ---------------------------------------------------------------------------


class TestPendingSuggestionLifecycle:
    def test_register_nonactionable_returns_none(self) -> None:
        s = AgentState.create(task_id="t")
        assert s.register_pending_suggestion("ev1", "", "x.py") is None
        assert s.register_pending_suggestion("ev1", "NONE", "x.py") is None
        assert s.register_pending_suggestion("ev1", "NONE_UNVERIFIABLE", "x.py") is None
        assert s.pending_suggestions == []

    def test_register_actionable_appends(self) -> None:
        s = AgentState.create(task_id="t", repo_root="/testbed")
        s.set_iteration(4)
        sug = s.register_pending_suggestion("ev1", "READ_CALLER_CONTRACT", "/testbed/a.py")
        assert sug is not None
        assert sug.next_action_file == "a.py"  # canonicalized
        assert sug.iter_emitted == 4
        assert sug.status == SuggestionStatus.PENDING

    def test_followed_when_agent_reads_suggested_file(self) -> None:
        s = AgentState.create(task_id="t", repo_root="/testbed")
        s.register_pending_suggestion("ev1", "READ_CALLER_CONTRACT", "callers.py")
        # First check: agent reads the suggested file => FOLLOWED_EXACT, not expired yet.
        expired = s.process_agent_action(action_file="/testbed/callers.py")
        assert expired == []
        assert s.pending_suggestions[0].status == SuggestionStatus.FOLLOWED_EXACT

    def test_ttl_expires_with_ignored_classification(self) -> None:
        s = AgentState.create(task_id="t", repo_root="/testbed")
        sug = s.register_pending_suggestion("ev1", "READ_CALLER_CONTRACT", "/testbed/a.py")
        assert sug is not None
        # Three unrelated actions.
        expired: list[PendingSuggestion] = []
        for _ in range(3):
            expired = s.process_agent_action(action_file="/testbed/other.py")
        # On the third check the TTL fires.
        assert sug.checked_count == 3
        assert sug.expired
        assert sug.status == SuggestionStatus.IGNORED
        assert sug in expired
        assert sug in s.ignored_suggestions
        assert s.pending_suggestions == []

    def test_followed_then_expires_is_not_ignored(self) -> None:
        s = AgentState.create(task_id="t", repo_root="/testbed")
        sug = s.register_pending_suggestion("ev1", "READ_CALLER_CONTRACT", "callers.py")
        assert sug is not None
        # Tick 1: followed.
        s.process_agent_action(action_file="callers.py")
        # Tick 2, 3: unrelated. TTL hits but status is already FOLLOWED.
        s.process_agent_action(action_file="other.py")
        expired = s.process_agent_action(action_file="other.py")
        assert sug.status == SuggestionStatus.FOLLOWED_EXACT
        # Followed-then-expired is dropped, not classified as ignored.
        assert expired == []
        assert sug not in s.ignored_suggestions

    def test_partial_suffix_match_followed(self) -> None:
        s = AgentState.create(task_id="t", repo_root="/testbed")
        s.register_pending_suggestion("ev1", "READ_CALLER_CONTRACT", "pkg/sub/x.py")
        # Agent reads a longer absolute path; canonical normalization handles the rest.
        s.process_agent_action(action_file="/workspace/repo/pkg/sub/x.py")
        # pkg/sub/x.py is contained in repo/pkg/sub/x.py via the substring check.
        assert s.pending_suggestions[0].status == SuggestionStatus.FOLLOWED_EXACT


# ---------------------------------------------------------------------------
# Persistence + parallel-task isolation
# ---------------------------------------------------------------------------


def _redirect_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make _agent_state_path / L5 state path point inside tmp_path.

    The module hardcodes ``/tmp/`` paths; we patch the helpers so tests don't
    collide with real artifacts and don't require /tmp to be writable.
    """
    import groundtruth.state.agent_state as mod

    def fake_agent(task_id: str = "") -> str:
        if task_id:
            safe = task_id.replace("/", "_").replace("\\", "_")
            return str(tmp_path / f"gt_agent_state_{safe}.json")
        return str(tmp_path / "gt_agent_state.json")

    def fake_l5(task_id: str = "") -> str:
        if task_id:
            safe = task_id.replace("/", "_").replace("\\", "_")
            return str(tmp_path / f"gt_l5_state_{safe}.json")
        return str(tmp_path / "gt_l5_state.json")

    monkeypatch.setattr(mod, "_agent_state_path", fake_agent)
    monkeypatch.setattr(mod, "_l5_state_path", fake_l5)
    monkeypatch.setattr(mod, "_state_path", fake_l5)


class TestPersistence:
    def test_save_then_load_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _redirect_state_dir(monkeypatch, tmp_path)
        s = AgentState.create(task_id="round-trip", max_iterations=100, repo_root="/testbed")
        s.set_iteration(15)
        s.set_issue_terms({"alpha", "beta"})
        s.set_brief_candidates(["/testbed/cand.py"])
        s.record_view("/testbed/seen.py", sync_legacy_file=False)
        s.record_edit("/testbed/edited.py")
        s.record_search("grep foo", hits=2)
        s.register_pending_suggestion("ev1", "READ_CALLER_CONTRACT", "/testbed/caller.py")
        s.save()

        loaded = AgentState.load_or_create(task_id="round-trip", repo_root="/testbed")
        assert loaded.iteration == 15
        assert loaded.issue_terms == {"alpha", "beta"}
        assert loaded.brief_candidates == {"cand.py"}
        assert [v.path for v in loaded.viewed_files] == ["seen.py"]
        assert loaded.edited_files == ["edited.py"]
        assert len(loaded.searches) == 1
        assert loaded.searches[0].command == "grep foo"
        assert len(loaded.pending_suggestions) == 1
        assert loaded.pending_suggestions[0].next_action_file == "caller.py"

    def test_two_tasks_use_separate_state_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _redirect_state_dir(monkeypatch, tmp_path)
        # Re-import through the module so the monkey-patched helper is used.
        import groundtruth.state.agent_state as mod

        s_a = AgentState.create(task_id="task-A", repo_root="/testbed")
        s_a.record_view("/testbed/a.py", sync_legacy_file=False)
        s_a.save()

        s_b = AgentState.create(task_id="task-B", repo_root="/testbed")
        s_b.record_view("/testbed/b.py", sync_legacy_file=False)
        s_b.save()

        # Files exist and are distinct.
        path_a = Path(mod._agent_state_path("task-A"))
        path_b = Path(mod._agent_state_path("task-B"))
        assert path_a.exists() and path_b.exists()
        assert path_a != path_b

        data_a = json.loads(path_a.read_text())
        data_b = json.loads(path_b.read_text())
        assert data_a["task_id"] == "task-A"
        assert data_b["task_id"] == "task-B"
        assert [v["path"] for v in data_a["viewed_files"]] == ["a.py"]
        assert [v["path"] for v in data_b["viewed_files"]] == ["b.py"]

        # Cross-loading does not contaminate.
        reloaded_a = AgentState.load_or_create(task_id="task-A", repo_root="/testbed")
        assert reloaded_a.visited_files_set() == {"a.py"}
        reloaded_b = AgentState.load_or_create(task_id="task-B", repo_root="/testbed")
        assert reloaded_b.visited_files_set() == {"b.py"}

    def test_load_with_unknown_task_returns_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _redirect_state_dir(monkeypatch, tmp_path)
        s = AgentState.load_or_create(task_id="never-saved", repo_root="/testbed")
        assert s.task_id == "never-saved"
        assert s.viewed_files == []
        assert s.iteration == 0


# ---------------------------------------------------------------------------
# Mocked end-to-end trajectory
# ---------------------------------------------------------------------------


class TestMockedTrajectory:
    def test_mocked_read_edit_search_trajectory(self) -> None:
        """Simulate a small agent trajectory and assert canonical state."""
        s = AgentState.create(task_id="traj-1", max_iterations=20, repo_root="/testbed")
        s.set_issue_terms({"timezone", "datetime"})
        s.set_brief_candidates(["/testbed/loguru/_logger.py", "/testbed/loguru/_file_sink.py"])

        # iter 1: agent searches.
        s.set_iteration(1)
        s.record_search("grep -rn datetime loguru/")

        # iter 2: reads a brief candidate.
        s.set_iteration(2)
        s.record_view("/testbed/loguru/_logger.py", sync_legacy_file=False)

        # iter 3: GT emits a next_action suggesting another file.
        s.set_iteration(3)
        s.register_pending_suggestion("ev1", "READ_CALLER_CONTRACT", "loguru/_datetime.py")

        # iter 4: agent does follow it.
        s.set_iteration(4)
        s.record_view("/testbed/loguru/_datetime.py", sync_legacy_file=False)
        s.process_agent_action(action_file="/testbed/loguru/_datetime.py")

        # iter 5: agent edits.
        s.set_iteration(5)
        s.record_edit("/testbed/loguru/_datetime.py")

        # Assertions
        assert s.iteration == 5
        # 5/20 = 0.25 — the boundary lands in MID_COMMITMENT per ``compute_band``
        # (EARLY is strictly ``ratio < 0.25``).
        assert s.band == IterationBand.MID_COMMITMENT
        assert [v.path for v in s.viewed_files] == ["loguru/_logger.py", "loguru/_datetime.py"]
        assert s.edited_files == ["loguru/_datetime.py"]
        assert s.current_focus == "loguru/_datetime.py"
        assert s.current_file == "loguru/_datetime.py"
        assert s.brief_candidates == {"loguru/_logger.py", "loguru/_file_sink.py"}
        assert s.pending_suggestions[0].status == SuggestionStatus.FOLLOWED_EXACT
        assert len(s.searches) == 1

    def test_mocked_trajectory_with_ignored_suggestion(self) -> None:
        s = AgentState.create(task_id="traj-2", repo_root="/testbed")
        s.register_pending_suggestion("ev1", "READ_CALLER_CONTRACT", "wanted.py")
        for path in ("a.py", "b.py", "c.py"):
            s.process_agent_action(action_file=path)
        assert s.ignored_suggestions
        assert s.ignored_suggestions[0].status == SuggestionStatus.IGNORED
        assert s.pending_suggestions == []


# ---------------------------------------------------------------------------
# Backwards-compatibility contract
# ---------------------------------------------------------------------------


class TestBackwardsCompatibility:
    def test_trajectory_state_reexports(self) -> None:
        """``groundtruth.trajectory.state`` still exposes the legacy names."""
        from groundtruth.trajectory.state import (
            AgentPhase as LegacyAgentPhase,
            FailureSnapshot as LegacyFailureSnapshot,
            IterationBand as LegacyIterationBand,
            L5TrajectoryState,
            compute_band as legacy_compute_band,
        )
        from groundtruth.state.agent_state import (
            AgentPhase,
            FailureSnapshot,
            IterationBand as CanonicalIterationBand,
            compute_band,
        )

        # Same objects (re-export, not copies).
        assert LegacyAgentPhase is AgentPhase
        assert LegacyFailureSnapshot is FailureSnapshot
        assert LegacyIterationBand is CanonicalIterationBand
        assert legacy_compute_band is compute_band
        # Construct + use to prove behavior is preserved.
        s = L5TrajectoryState(instance_id="legacy", max_iter=100)
        s.update_iter(80, 100)
        assert s.band == LegacyIterationBand.LATE_REPAIR

    def test_view_mirrors_to_tmp_when_requested(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ``sync_legacy_file=True`` (default) the tmp mirror is rewritten."""
        import groundtruth.state.agent_state as mod
        tmp_viewed = tmp_path / "gt_viewed.txt"
        monkeypatch.setattr(mod, "LEGACY_VIEWED_PATH", str(tmp_viewed))

        s = AgentState.create(task_id="t", repo_root="/testbed")
        s.record_view("/testbed/a.py")
        s.record_view("/testbed/b.py")
        contents = tmp_viewed.read_text().strip().splitlines()
        assert contents == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# PendingSuggestion dataclass helpers
# ---------------------------------------------------------------------------


class TestPendingSuggestionHelpers:
    def test_to_legacy_dict_shape(self) -> None:
        sug = PendingSuggestion(
            event_id="ev1",
            next_action_type="READ_CALLER_CONTRACT",
            next_action_file="a.py",
            iter_emitted=5,
        )
        d = sug.to_legacy_dict()
        # Matches what the wrapper's _pending_next_actions list expects.
        assert set(d) >= {
            "event_id", "next_action_type", "next_action_file",
            "iter_emitted", "checked_count", "followed",
        }
        assert d["followed"] is False
        sug.status = SuggestionStatus.FOLLOWED_EXACT
        assert sug.to_legacy_dict()["followed"] is True
