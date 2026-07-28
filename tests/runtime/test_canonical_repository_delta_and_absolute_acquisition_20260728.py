"""RED contracts for repository-delta freshness and canonical VIEW subjects."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr


API = "def get_user(uid):\n    return [uid]\n"
CALLER_BEFORE = "def use():\n    return get_user(1)\n"
CALLER_AFTER = "def use():\n    return []\n"

SATISFIED = frozenset(
    {
        rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
        rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
        rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
        rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
        rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
        rr.TemporalPredicate.COMMITMENT_WINDOW_OPEN,
        rr.TemporalPredicate.AUTHORIZED_BYTE_OWNER_LINEAGE_PRESENT,
    }
)


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


def _initialize_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    source = repo / "src"
    source.mkdir(parents=True)
    (source / "api.py").write_text(API, encoding="utf-8")
    (source / "caller.py").write_text(CALLER_BEFORE, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "canonical@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Canonical fixture"],
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
        (
            (1, "Function", "get_user", "src/api.py", 1, 0, "python"),
            (2, "Function", "use", "src/caller.py", 1, 0, "python"),
        ),
    )
    con.execute(
        "INSERT INTO edges VALUES "
        "(1,2,1,'CALLS',2,'src/caller.py','import',1.0,NULL)"
    )
    con.commit()
    con.close()
    return repo, db


def _install(
    tmp_path: Path,
    monkeypatch,
    attempt_id: str,
) -> tuple[seam.CanonicalRuntimeAttachment, Path]:
    repo, db = _initialize_repo(tmp_path)
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(repo))
    monkeypatch.setattr(seam, "_db_path", lambda: str(db))
    ledger = tmp_path / "runtime.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    attachment = seam.install_canonical_runtime(
        model=_Model(),
        agent=_Agent(),
        env={
            "GT_ATTEMPT_ID": attempt_id,
            "GT_RUNTIME_LEDGER": str(ledger),
            "GT_CANONICAL_JOURNAL": str(tmp_path / "canonical.sqlite3"),
            "GT_BRIEF_FILE": str(tmp_path / "absent-brief.txt"),
        },
        task="inspect src/api.py and preserve its callers",
    )
    assert attachment.attached is True
    return attachment, repo


def _observe_view(
    attachment: seam.CanonicalRuntimeAttachment,
    subject: str,
) -> rr.CanonicalEvent:
    before = {
        event.event_id
        for event in attachment.attempt_runtime.journal.events(
            attachment.attempt_runtime.attempt_id
        )
    }
    action = {
        "operation": "VIEW_SOURCE",
        "command": "view",
        "path": subject,
    }
    native_result = {"output": API, "returncode": 0}
    native_before = dict(native_result)
    proposal = attachment.observe_action_proposal(action)
    attachment.observe_action_result(action, native_result)

    assert native_result == native_before
    assert proposal is not None
    results = tuple(
        event
        for event in attachment.attempt_runtime.journal.events(
            attachment.attempt_runtime.attempt_id
        )
        if event.event_id not in before
        and event.kind is rr.EventKind.ACTION_RESULT
    )
    assert len(results) == 1
    return proposal


def _localization_record(
    revision: rr.RevisionVector,
    *,
    subject: str = "src/api.py",
) -> rr.EvidenceRecord:
    contract = rr.feature_contract_for("localization")
    assert contract is not None
    observed = tuple(
        sorted(
            set(contract.fallback_policy.preferred_substrates)
            or set(contract.fallback_policy.fallback_substrates)
        )
    )
    return rr.EvidenceRecord(
        evidence_id="GT-E-absolute-acquisition-localization",
        feature_id="localization",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject=subject,
        claim=f"localize {subject}",
        actionable_consequence=f"inspect {subject} before editing",
        provenance=(f"{subject}:1",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=revision,
        causal_neighborhood=(
            f"decision:{contract.decision_context.value}",
            f"subject:{subject}",
        ),
        lifecycle=rr.EvidenceLifecycle.PENDING,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        token_cost=24,
        failure_prevention=5,
        causal_value=5,
        contradiction_resolution=0,
        anchoring_risk=0,
        revision_dependencies=contract.revision_dependencies,
        observed_substrates=observed,
    )


def _prepare_localization(
    attachment: seam.CanonicalRuntimeAttachment,
    record: rr.EvidenceRecord,
) -> rr.InferencePlan:
    runtime = attachment.attempt_runtime
    runtime.ingest_evidence(record)
    decision = rr.ActiveDecision(
        decision_id="decision:absolute-acquisition",
        context=record.decision_context,
        primary_claim="choose the source target",
        required_roles=record.roles,
        causal_neighborhood=(f"subject:{record.subject}",),
        token_budget=180,
        current_revision=runtime.work_state.revision,
    )
    return runtime.prepare_next_inference(
        decisions=(decision,),
        satisfied_predicates=SATISFIED,
        commitment_window=rr.CommitmentWindowState.OPEN,
        available_substrates=(
            seam.CanonicalRuntimeAttachment._available_substrates((record,))
        ),
        native_observation="native file-view observation",
        observation_id="observation:absolute-acquisition",
        source_model_call_id="model:absolute-acquisition:source",
        model_call_id="model:absolute-acquisition:next",
    )


def test_unclassified_repository_delta_degrades_graph_and_suppresses_graph_facts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, repo = _install(
        tmp_path,
        monkeypatch,
        "attempt-unclassified-repository-delta",
    )
    covering_calls: list[tuple[object, ...]] = []
    graph_candidates_after_filter: list[object] = []
    try:
        _observe_view(attachment, "src/api.py")
        assert "src/api.py::get_user" in (
            attachment.attempt_runtime.work_state.focused_symbols
        )
        monkeypatch.setenv("GT_GATEWAY", "1")
        monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
        monkeypatch.setattr(
            "groundtruth.runtime.covering_runner.select_covering_tests",
            lambda *args, **kwargs: covering_calls.append(args) or [],
        )
        graph_candidate = SimpleNamespace(
            canonical_semantics=SimpleNamespace(
                observed_substrates=("graph",)
            )
        )
        monkeypatch.setattr(
            "groundtruth.runtime.gateway.produce_raw",
            lambda *_args, **_kwargs: (graph_candidate,),
        )

        def capture_candidates(envelopes, **_kwargs):
            graph_candidates_after_filter.extend(envelopes)
            return ()

        monkeypatch.setattr(
            rr,
            "canonicalize_evidence_envelopes",
            capture_candidates,
        )

        action = {
            "operation": "OTHER",
            "command": "python tools/rewrite_repository.py",
        }
        attachment.observe_action_proposal(action)
        (repo / "src" / "caller.py").write_text(
            CALLER_AFTER,
            encoding="utf-8",
        )
        native_result = {"output": "rewrite complete", "returncode": 0}
        native_before = dict(native_result)
        attachment.observe_action_result(action, native_result)
        degraded_after_delta = (
            attachment.attempt_runtime.work_state.revision.graph
        )

        _observe_view(attachment, "src/api.py")
        ledger_rows = tuple(
            json.loads(line)
            for line in (tmp_path / "runtime.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
        refresh_rows = tuple(
            row
            for row in ledger_rows
            if row.get("layer") == "canonical_runtime.graph_refresh"
        )
        persisted_precise_failure = any(
            row.get("chars_delivered") == 0
            and row.get("unresolved_graph_refreshes") == {
                "src/caller.py": "reindex_binary_unavailable"
            }
            and row.get("graph_changed_files") == ["src/caller.py"]
            for row in refresh_rows
        )
        actual = (
            native_result,
            attachment.unresolved_graph_refreshes,
            degraded_after_delta.startswith("degraded:"),
            attachment.attempt_runtime.work_state.revision.graph
            == degraded_after_delta,
            graph_candidates_after_filter,
            covering_calls,
            persisted_precise_failure,
        )
        expected = (
            native_before,
            {
                "src/caller.py": "reindex_binary_unavailable"
            },
            True,
            True,
            [],
            [],
            True,
        )
        assert actual == expected
    finally:
        attachment.attempt_runtime.journal.close()


def test_nonchanging_other_action_does_not_degrade_graph(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, _repo = _install(
        tmp_path,
        monkeypatch,
        "attempt-nonchanging-other",
    )
    try:
        initial = attachment.attempt_runtime.work_state.revision
        action = {"operation": "OTHER", "command": "python -c \"print('ok')\""}
        attachment.observe_action_proposal(action)
        native_result = {"output": "ok\n", "returncode": 0}
        native_before = dict(native_result)
        attachment.observe_action_result(action, native_result)

        assert native_result == native_before
        assert attachment.unresolved_graph_refreshes == {}
        assert not attachment.attempt_runtime.work_state.revision.graph.startswith(
            "degraded:"
        )
        assert (
            attachment.attempt_runtime.work_state.revision.graph
            == initial.graph
        )
    finally:
        attachment.attempt_runtime.journal.close()


def test_graph_degradation_blocks_all_graph_derived_focus_and_decision_subjects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, repo = _install(
        tmp_path,
        monkeypatch,
        "attempt-degraded-graph-focus",
    )
    try:
        rewrite = {
            "operation": "OTHER",
            "command": "python tools/rewrite_repository.py",
        }
        attachment.observe_action_proposal(rewrite)
        (repo / "src" / "api.py").write_text(
            "def replacement(uid):\n    return uid\n",
            encoding="utf-8",
        )
        attachment.observe_action_result(
            rewrite,
            {"output": "rewrite complete", "returncode": 0},
        )
        assert attachment.attempt_runtime.work_state.revision.graph.startswith(
            "degraded:"
        )

        _observe_view(attachment, "src/api.py")
        search = {
            "operation": "SEARCH",
            "command": "rg get_user src",
        }
        attachment.observe_action_proposal(search)
        attachment.observe_action_result(
            search,
            {"output": "", "returncode": 1},
        )

        state = attachment.attempt_runtime.work_state
        active = attachment._active_decision(
            (),
            state,
            state.revision,
            graph_fresh=False,
        )
        assert "src/api.py::get_user" not in state.focused_symbols
        assert "subject:src/api.py::get_user" not in active.causal_neighborhood
        assert "subject:get_user" not in active.causal_neighborhood
    finally:
        attachment.attempt_runtime.journal.close()


def test_historical_graph_focus_is_audit_only_while_graph_is_degraded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, repo = _install(
        tmp_path,
        monkeypatch,
        "attempt-historical-degraded-focus",
    )
    try:
        _observe_view(attachment, "src/api.py")
        assert "src/api.py::get_user" in (
            attachment.attempt_runtime.work_state.focused_symbols
        )

        rewrite = {
            "operation": "OTHER",
            "command": "python tools/rewrite_repository.py",
        }
        attachment.observe_action_proposal(rewrite)
        (repo / "src" / "api.py").write_text(
            "def replacement(uid):\n    return uid\n",
            encoding="utf-8",
        )
        attachment.observe_action_result(
            rewrite,
            {"output": "rewrite complete", "returncode": 0},
        )

        state = attachment.attempt_runtime.work_state
        assert "src/api.py::get_user" in state.focused_symbols
        active = attachment._active_decision(
            (),
            state,
            state.revision,
            graph_fresh=False,
        )
        assert "subject:src/api.py::get_user" not in active.causal_neighborhood
        assert "subject:get_user" not in active.causal_neighborhood
    finally:
        attachment.attempt_runtime.journal.close()


def test_proven_graph_irrelevant_repository_delta_keeps_graph_fresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, repo = _install(
        tmp_path,
        monkeypatch,
        "attempt-graph-irrelevant-delta",
    )
    try:
        initial_graph = attachment.attempt_runtime.work_state.revision.graph
        action = {
            "operation": "OTHER",
            "command": "python tools/write_run_report.py",
        }
        attachment.observe_action_proposal(action)
        (repo / "RUN_REPORT.txt").write_text(
            "host-only run report\n",
            encoding="utf-8",
        )
        native_result = {"output": "report written", "returncode": 0}
        native_before = dict(native_result)
        attachment.observe_action_result(action, native_result)

        assert native_result == native_before
        assert attachment.unresolved_graph_refreshes == {}
        assert attachment.attempt_runtime.work_state.revision.graph == initial_graph
    finally:
        attachment.attempt_runtime.journal.close()


def test_clean_head_switch_detects_graph_input_delta(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, repo = _install(
        tmp_path,
        monkeypatch,
        "attempt-clean-head-switch",
    )
    try:
        original_branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "checkout", "-q", "-b", "alternate-clean-head"],
            cwd=repo,
            check=True,
        )
        (repo / "src" / "api.py").write_text(
            "def replacement(uid):\n    return uid\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "src/api.py"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "alternate source"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "-q", original_branch],
            cwd=repo,
            check=True,
        )

        action = {
            "operation": "OTHER",
            "command": "git checkout alternate-clean-head",
        }
        attachment.observe_action_proposal(action)
        subprocess.run(
            ["git", "checkout", "-q", "alternate-clean-head"],
            cwd=repo,
            check=True,
        )
        attachment.observe_action_result(
            action,
            {"output": "", "returncode": 0},
        )

        assert attachment.attempt_runtime.work_state.revision.graph.startswith(
            "degraded:"
        )
        assert attachment.unresolved_graph_refreshes == {
            "src/api.py": "reindex_binary_unavailable"
        }
    finally:
        attachment.attempt_runtime.journal.close()


def test_committing_existing_source_delta_does_not_create_second_graph_delta(
    tmp_path: Path,
) -> None:
    repo, _db = _initialize_repo(tmp_path)
    before = seam._git_graph_input_snapshot(str(repo))
    (repo / "src" / "api.py").write_text(
        "def replacement(uid):\n    return uid\n",
        encoding="utf-8",
    )
    dirty = seam._git_graph_input_snapshot(str(repo))
    assert seam._graph_input_delta(before, dirty) == (("src/api.py",), True)

    subprocess.run(["git", "add", "src/api.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "commit existing source delta"],
        cwd=repo,
        check=True,
    )
    committed = seam._git_graph_input_snapshot(str(repo))
    assert seam._graph_input_delta(dirty, committed) == ((), True)


def test_committing_existing_source_delete_does_not_create_second_graph_delta(
    tmp_path: Path,
) -> None:
    repo, _db = _initialize_repo(tmp_path)
    before = seam._git_graph_input_snapshot(str(repo))
    (repo / "src" / "caller.py").unlink()
    deleted = seam._git_graph_input_snapshot(str(repo))
    assert seam._graph_input_delta(before, deleted) == (
        ("src/caller.py",),
        True,
    )

    subprocess.run(["git", "add", "-u"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "commit existing source delete"],
        cwd=repo,
        check=True,
    )
    committed = seam._git_graph_input_snapshot(str(repo))
    assert seam._graph_input_delta(deleted, committed) == ((), True)


def test_commit_only_action_after_verified_refresh_keeps_graph_fresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, repo = _install(
        tmp_path,
        monkeypatch,
        "attempt-commit-after-refresh",
    )
    refreshes: list[str] = []
    monkeypatch.setattr(
        seam,
        "_invalidate_on_edit",
        lambda relative, _root: (
            refreshes.append(relative)
            or seam._L6RefreshOutcome(True, "reindex_ok", "graph.db")
        ),
    )
    try:
        rewrite = {
            "operation": "OTHER",
            "command": "python tools/rewrite_repository.py",
        }
        attachment.observe_action_proposal(rewrite)
        (repo / "src" / "api.py").write_text(
            "def replacement(uid):\n    return uid\n",
            encoding="utf-8",
        )
        attachment.observe_action_result(
            rewrite,
            {"output": "rewrite complete", "returncode": 0},
        )
        assert attachment.unresolved_graph_refreshes == {}
        assert refreshes == ["src/api.py"]

        commit = {
            "operation": "OTHER",
            "command": "git add src/api.py && git commit -m source",
        }
        attachment.observe_action_proposal(commit)
        subprocess.run(["git", "add", "src/api.py"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "source"],
            cwd=repo,
            check=True,
        )
        attachment.observe_action_result(
            commit,
            {"output": "[master source]", "returncode": 0},
        )

        assert attachment.unresolved_graph_refreshes == {}
        assert refreshes == ["src/api.py"]
        assert not attachment.attempt_runtime.work_state.revision.graph.startswith(
            "degraded:"
        )
    finally:
        attachment.attempt_runtime.journal.close()


@pytest.mark.parametrize(
    "subject_factory",
    (
        lambda repo: str(repo / "src" / "api.py"),
        lambda repo: str(repo / "src" / "api.py").replace("/", "\\"),
        lambda _repo: "/testbed/src/api.py",
        lambda _repo: r"\testbed\src\api.py",
    ),
    ids=("host-absolute", "windows-separators", "testbed", "testbed-windows"),
)
def test_absolute_view_subject_is_canonicalized_before_acquisition(
    tmp_path: Path,
    monkeypatch,
    subject_factory,
) -> None:
    attachment, repo = _install(
        tmp_path,
        monkeypatch,
        "attempt-absolute-acquisition",
    )
    try:
        proposal = _observe_view(attachment, subject_factory(repo))
        state = attachment.attempt_runtime.work_state
        record = _localization_record(state.revision)
        plan = _prepare_localization(attachment, record)
        reasons = tuple(
            suppression.reason
            for suppression in plan.oracle_decision.suppressed
            if suppression.evidence_id == record.evidence_id
        )

        assert proposal.action is not None
        actual = (
            proposal.action.subject,
            proposal.action.targets,
            state.viewed_files,
            state.focused_files,
            plan.delivery_attempt_id,
            reasons,
        )
        expected = (
            "src/api.py",
            ("src/api.py",),
            ("src/api.py",),
            ("src/api.py",),
            "",
            (rr.SuppressionReason.ALREADY_ACQUIRED,),
        )
        assert actual == expected
    finally:
        attachment.attempt_runtime.journal.close()


def test_outside_root_absolute_view_fails_quiet_without_aliasing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, _repo = _install(
        tmp_path,
        monkeypatch,
        "attempt-outside-root-acquisition",
    )
    outside = tmp_path / "outside" / "src" / "api.py"
    outside.parent.mkdir(parents=True)
    outside.write_text(API, encoding="utf-8")
    try:
        _observe_view(attachment, str(outside))
        state = attachment.attempt_runtime.work_state
        result_event = next(
            event
            for event in reversed(
                attachment.attempt_runtime.journal.events(
                    attachment.attempt_runtime.attempt_id
                )
            )
            if event.kind is rr.EventKind.ACTION_RESULT
        )
        record = _localization_record(state.revision)
        plan = _prepare_localization(attachment, record)

        acquired_suppressions = tuple(
            suppression
            for suppression in plan.oracle_decision.suppressed
            if suppression.evidence_id == record.evidence_id
            and suppression.reason is rr.SuppressionReason.ALREADY_ACQUIRED
        )
        actual = (
            state.viewed_files,
            state.focused_files,
            tuple(
                outcome.kind
                for outcome in result_event.outcomes
                if outcome.kind is rr.SemanticKind.SOURCE_VIEWED
            ),
            acquired_suppressions,
            bool(plan.delivery_attempt_id),
            plan.compilation.state,
        )
        expected = (
            (),
            (),
            (),
            (),
            True,
            rr.CapsuleCompilationState.COMPILED,
        )
        assert actual == expected
    finally:
        attachment.attempt_runtime.journal.close()


def test_relative_view_subject_behavior_is_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, _repo = _install(
        tmp_path,
        monkeypatch,
        "attempt-relative-acquisition-control",
    )
    try:
        proposal = _observe_view(attachment, "src/api.py")
        state = attachment.attempt_runtime.work_state
        record = _localization_record(state.revision)
        plan = _prepare_localization(attachment, record)

        assert proposal.action is not None
        assert proposal.action.subject == "src/api.py"
        assert state.viewed_files == ("src/api.py",)
        assert state.focused_files == ("src/api.py",)
        assert plan.delivery_attempt_id == ""
        assert tuple(
            suppression.reason
            for suppression in plan.oracle_decision.suppressed
            if suppression.evidence_id == record.evidence_id
        ) == (rr.SuppressionReason.ALREADY_ACQUIRED,)
    finally:
        attachment.attempt_runtime.journal.close()


@pytest.mark.parametrize("path_field", ("path", "file_path", "subject", "target"))
def test_explicit_view_path_fields_are_confined_without_command_classification(
    tmp_path: Path,
    monkeypatch,
    path_field: str,
) -> None:
    attachment, _repo = _install(
        tmp_path,
        monkeypatch,
        f"attempt-explicit-view-{path_field}",
    )
    try:
        action = {
            "operation": "VIEW_SOURCE",
            "command": "custom_read_operation",
            path_field: "/testbed/src/api.py",
        }
        proposal = attachment.observe_action_proposal(action)
        attachment.observe_action_result(
            action,
            {"output": API, "returncode": 0},
        )

        assert proposal is not None
        assert proposal.action is not None
        assert proposal.action.subject == "src/api.py"
        assert proposal.action.targets == ("src/api.py",)
        assert attachment.attempt_runtime.work_state.viewed_files == (
            "src/api.py",
        )
    finally:
        attachment.attempt_runtime.journal.close()


def test_explicit_view_never_uses_raw_command_as_file_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attachment, _repo = _install(
        tmp_path,
        monkeypatch,
        "attempt-explicit-view-missing-path",
    )
    try:
        action = {
            "operation": "VIEW_SOURCE",
            "command": "custom_read_operation",
        }
        proposal = attachment.observe_action_proposal(action)
        attachment.observe_action_result(
            action,
            {"output": API, "returncode": 0},
        )

        assert proposal is not None
        assert proposal.action is not None
        assert proposal.action.subject == ""
        assert proposal.action.targets == ()
        assert attachment.attempt_runtime.work_state.viewed_files == ()
    finally:
        attachment.attempt_runtime.journal.close()


def test_posix_confinement_is_case_sensitive() -> None:
    assert (
        seam._confined_repo_relative_path(
            "/testbed/src/api.py",
            "/testbed",
        )
        == "src/api.py"
    )
    assert (
        seam._confined_repo_relative_path(
            "/TESTBED/src/api.py",
            "/testbed",
        )
        == ""
    )


def test_graph_input_source_extensions_match_gt_index_registry() -> None:
    """Every language accepted by gt-index must participate in delta sensing."""
    specs_dir = Path(__file__).parents[2] / "gt-index" / "internal" / "specs"
    registered: set[str] = set()
    for spec_path in specs_dir.glob("*.go"):
        source = spec_path.read_text(encoding="utf-8")
        match = re.search(r"Extensions:\s*\[\]string\{([^}]*)\}", source)
        if match is not None:
            registered.update(re.findall(r'"(\.[^"]+)"', match.group(1)))

    assert registered
    assert {
        extension
        for extension in registered
        if seam._graph_input_kind(f"src/example{extension}") != "incremental"
    } == set()


@pytest.mark.parametrize(
    "path",
    (
        ".gitignore",
        "Cargo.toml",
        "go.mod",
        "go.work",
        "jsconfig.json",
        "package.json",
        "tsconfig.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradle.properties",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
    ),
)
def test_repository_control_and_build_inputs_require_full_reindex(
    path: str,
) -> None:
    """Inputs with repository-wide effects must never take the single-file route."""
    assert seam._graph_input_kind(path) == "metadata"


def test_gitignore_change_is_visible_to_graph_input_snapshot(
    tmp_path: Path,
) -> None:
    """Walker inclusion rules are graph input even though they are not source code."""
    repo, _db = _initialize_repo(tmp_path)
    gitignore = repo / ".gitignore"
    gitignore.write_text("ignored/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add walker policy"],
        cwd=repo,
        check=True,
    )
    before = seam._git_graph_input_snapshot(str(repo))

    gitignore.write_text("ignored/\n!ignored/source.py\n", encoding="utf-8")
    after = seam._git_graph_input_snapshot(str(repo))

    assert seam._graph_input_delta(before, after) == ((".gitignore",), True)
