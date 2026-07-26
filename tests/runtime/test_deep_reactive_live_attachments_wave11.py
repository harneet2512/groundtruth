from __future__ import annotations

from types import SimpleNamespace

import artifact_deepswe.gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.covering_runner import CoveringAttribution


REVISION = rr.RevisionVector(
    repository_content="repo-11",
    graph="graph-11",
    lsp="lsp-11",
    runtime_evidence="runtime-11",
)
EVENT_HASH = "d" * 64


def _internal_event(component: str, outcomes=()):
    return SimpleNamespace(
        event_id=f"attempt-11:internal:{component}",
        content_hash=EVENT_HASH,
        outcomes=tuple(outcomes),
    )


def _canonicalize_runtime(envelopes):
    return rr.canonicalize_evidence_envelopes(
        envelopes,
        committed_event_hashes={
            witness.witness_id: witness.content_sha256
            for envelope in envelopes
            for witness in envelope.runtime_witnesses
            if witness.kind == "canonical_event"
        },
    )


def _attachment(
    *,
    phase: rr.Phase = rr.Phase.IMPLEMENTATION,
    failures: tuple[str, ...] = (),
    transition_rules: tuple[str, ...] = (),
) -> seam.CanonicalRuntimeAttachment:
    work_state = SimpleNamespace(
        phase=phase,
        focused_symbols=("refresh_session",),
        focused_files=("src/auth/session.py",),
        edited_files=("src/auth/session.py",),
        current_failures=failures,
        transition_rules=transition_rules,
    )
    recovery_event = _internal_event(
        "recovery-source",
        (
            rr.SemanticOutcome(
                kind=rr.SemanticKind.TEST_FAIL,
                subject="tests/auth/test_session.py",
                failure_fingerprint=(failures[0] if failures else ""),
            ),
        ),
    )
    return seam.CanonicalRuntimeAttachment(
        attached=True,
        attempt_runtime=SimpleNamespace(
            work_state=work_state,
            attempt_id="attempt-11",
            journal=SimpleNamespace(
                events=lambda _attempt_id: (
                    (recovery_event,) if failures else ()
                )
            ),
        ),
        provider_boundary=None,
        gateway_state=None,
        graph_revision=REVISION.graph,
    )


def test_live_deep_hook_produces_syntax_evidence_with_physical_owner(
    monkeypatch,
) -> None:
    attachment = _attachment()
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    internal_events: list[dict[str, object]] = []
    def append_internal(**kwargs):
        internal_events.append(kwargs)
        return _internal_event(kwargs["component"], kwargs["outcomes"])

    monkeypatch.setattr(
        attachment,
        "_append_internal_validation_event",
        append_internal,
    )
    monkeypatch.setattr(seam, "_root", lambda: "/repo")
    monkeypatch.setattr(seam, "_build_edit_check_executor", lambda: None)
    monkeypatch.setattr(
        "groundtruth.runtime.edit_check.check_edit_syntax",
        lambda *args, **kwargs: {
            "verdict": "syntax_error",
            "diagnostic": "src/auth/session.py:41: SyntaxError: invalid syntax",
            "reason": "parse_error",
            "checker": ["ast.parse"],
        },
    )

    envelopes = attachment._deep_reactive_envelopes(
        changed_files=("src/auth/session.py",),
        revision=REVISION,
    )

    records = _canonicalize_runtime(envelopes)
    assert len(records) == 1
    assert records[0].feature_id == "syntax_result"
    assert records[0].owner_feature_ids == ("GT_EDIT_CHECK",)
    assert attachment.last_covering_result is None
    assert internal_events[0]["component"] == "syntax_result"
    assert internal_events[0]["outcomes"][0].kind is rr.SemanticKind.COMPILE_RESULT


def test_live_deep_hook_produces_only_attributable_executed_covering_red(
    monkeypatch,
) -> None:
    attachment = _attachment()
    monkeypatch.delenv("GT_EDIT_CHECK", raising=False)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    internal_events: list[dict[str, object]] = []
    def append_internal(**kwargs):
        internal_events.append(kwargs)
        return _internal_event(kwargs["component"], kwargs["outcomes"])

    monkeypatch.setattr(
        attachment,
        "_append_internal_validation_event",
        append_internal,
    )
    result = {
        "executed": True,
        "verdict": "fail",
        "reason": "test_failure",
        "files": ["tests/auth/test_session.py"],
        "ran": ["tests/auth/test_session.py"],
        "command": ["pytest", "-q", "tests/auth/test_session.py"],
        "stdout_tail": "1 failed",
        "stderr_tail": "",
        "exit_code": 1,
        "failing_test_names": ["test_rotation"],
    }
    attribution = CoveringAttribution(
        attributed=True,
        method="unresolved_covering",
        current_verdict="fail",
        base_verdict="fail",
        implicated_edited_paths=("src/auth/session.py",),
        covering_files=("tests/auth/test_session.py",),
    )
    monkeypatch.setattr(seam, "_root", lambda: "/repo")
    monkeypatch.setattr(seam, "_db_path", lambda: "/repo/graph.db")
    monkeypatch.setattr(seam, "_build_edit_check_executor", lambda: None)
    monkeypatch.setattr(seam, "_build_verification_executor", lambda: None)
    monkeypatch.setattr(
        "groundtruth.runtime.edit_check.check_edit_syntax",
        lambda *args, **kwargs: {"verdict": "ok"},
    )
    monkeypatch.setattr(
        "groundtruth.runtime.covering_runner.select_covering_tests",
        lambda *args, **kwargs: [{"file": "tests/auth/test_session.py"}],
    )
    monkeypatch.setattr(
        "groundtruth.runtime.covering_runner.run_covering_tests",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        "groundtruth.runtime.covering_runner.attribute_covering_red",
        lambda *args, **kwargs: attribution,
    )

    envelopes = attachment._deep_reactive_envelopes(
        changed_files=("src/auth/session.py",),
        revision=REVISION,
    )

    records = _canonicalize_runtime(envelopes)
    assert len(records) == 1
    assert records[0].feature_id == "covering_red"
    assert attachment.last_covering_result is result
    assert attachment.last_covering_attribution is attribution
    assert internal_events[0]["component"] == "covering_red"
    assert tuple(
        outcome.kind for outcome in internal_events[0]["outcomes"]
    ) == (
        rr.SemanticKind.TEST_RESULT,
        rr.SemanticKind.TEST_FAIL,
    )


def test_live_deep_hook_emits_recovery_only_from_repeated_failure_state(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    attachment = _attachment(
        phase=rr.Phase.RECOVERY,
        failures=("failure-fingerprint-11",),
        transition_rules=("repeated_failure_after_edit",),
    )

    envelopes = attachment._deep_reactive_envelopes(
        changed_files=(),
        revision=REVISION,
    )

    records = _canonicalize_runtime(envelopes)
    assert len(records) == 1
    assert records[0].feature_id == "recovery"
    assert records[0].owner_feature_ids == ("GT_HYPOTHESIS",)
    assert records[0].grade is rr.EvidenceGrade.WARNING


def test_new_edit_clears_prior_covering_result_before_recomputation(
    monkeypatch,
) -> None:
    attachment = _attachment()
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    attachment.last_covering_result = {"executed": True, "verdict": "fail"}
    attachment.last_covering_attribution = object()
    monkeypatch.setattr(seam, "_root", lambda: "/repo")
    monkeypatch.setattr(seam, "_build_edit_check_executor", lambda: None)
    monkeypatch.setattr(
        "groundtruth.runtime.edit_check.check_edit_syntax",
        lambda *args, **kwargs: {
            "verdict": "syntax_error",
            "diagnostic": "SyntaxError: invalid syntax",
            "reason": "parse_error",
            "checker": ["ast.parse"],
        },
    )

    attachment._deep_reactive_envelopes(
        changed_files=("src/auth/session.py",),
        revision=REVISION,
    )

    assert attachment.last_covering_result is None
    assert attachment.last_covering_attribution is None


def test_deep_capability_kill_switches_are_true_computation_gates(
    monkeypatch,
) -> None:
    attachment = _attachment(
        phase=rr.Phase.RECOVERY,
        failures=("failure-fingerprint-11",),
        transition_rules=("repeated_failure_after_edit",),
    )
    monkeypatch.delenv("GT_EDIT_CHECK", raising=False)
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    monkeypatch.delenv("GT_HYPOTHESIS", raising=False)

    monkeypatch.setattr(
        "groundtruth.runtime.edit_check.check_edit_syntax",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("disabled syntax computation ran")
        ),
    )
    monkeypatch.setattr(
        "groundtruth.runtime.covering_runner.select_covering_tests",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("disabled covering computation ran")
        ),
    )

    assert attachment._deep_reactive_envelopes(
        changed_files=("src/auth/session.py",),
        revision=REVISION,
    ) == ()


def test_isolated_deep_component_does_not_rerun_on_later_turn(
    monkeypatch,
) -> None:
    attachment = _attachment()
    attachment.attempt_runtime.failure_state = SimpleNamespace(
        gt_emission_enabled=True,
        isolated_components=("syntax_result",),
    )
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    monkeypatch.setattr(
        "groundtruth.runtime.edit_check.check_edit_syntax",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("isolated syntax component reran")
        ),
    )

    assert attachment._deep_reactive_envelopes(
        changed_files=("src/auth/session.py",),
        revision=REVISION,
    ) == ()
