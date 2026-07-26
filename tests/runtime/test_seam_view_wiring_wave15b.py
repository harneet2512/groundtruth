"""Seam-level guard: the structured VIEW subject must reach ``normalize_event``.

WHY THIS FILE EXISTS (LIPI / Integration).  ``tests/runtime/
test_gateway_caller_contract_view_wave15.py`` is green, but every one of its
cases calls ``normalize_event(..., viewed_files=("src/api.py",))`` -- it hands
in the exact value the seam is supposed to PRODUCE.  Meanwhile
``artifact_deepswe/gt_mini_patch.py`` contains zero references to
``viewed_files``, so in the installed path the argument defaults to ``()`` and
``gateway.produce_raw`` returns ``[]`` (that file's own last case proves the
empty-path branch).  Net effect: ``caller_contract`` at its contracted
``file_view`` boundary is dead in production and the green component suite
conceals it.

These tests fail on the seam, not on the producer, so they cannot be satisfied
by any amount of component-level work.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-wave15b",
    graph="graph-wave15b",
    lsp="lsp-wave15b",
    runtime_evidence="runtime-wave15b",
)


def _attachment(tmp_path) -> seam.CanonicalRuntimeAttachment:
    """A real runtime (so ``_revision``/sequence work) with inert boundaries."""
    journal = rr.RuntimeJournal(tmp_path / "view-wave15b.sqlite3")
    journal.open()
    runtime = rr.AttemptReasoningRuntime(
        attempt_id="attempt-wave15b-view",
        journal=journal,
        initial_revision=REVISION,
    )
    return seam.CanonicalRuntimeAttachment(
        attached=True,
        attempt_runtime=runtime,
        provider_boundary=SimpleNamespace(),
        gateway_state=SimpleNamespace(),
        graph_revision=REVISION.graph,
    )


def _capture_normalize_event(monkeypatch) -> dict:
    """Spy on the adapter boundary and stop the turn right after the call."""
    captured: dict = {}

    class _Stop(Exception):
        pass

    def _spy(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        raise _Stop("captured")

    monkeypatch.setattr(
        "groundtruth.runtime.adapters.miniswe.normalize_event",
        _spy,
    )
    return captured


def _drive(attachment, action, out):
    """Run proposal + result the way the live seam does for one turn."""
    attachment.observe_action_proposal(action)
    try:
        attachment.observe_action_result(action, out)
    except Exception:  # noqa: BLE001 -- the spy stops the turn deliberately
        pass


def test_structured_view_subject_reaches_normalize_event(
    tmp_path,
    monkeypatch,
) -> None:
    """RED until the seam forwards ``viewed_files`` from proposal truth."""
    attachment = _attachment(tmp_path)
    captured = _capture_normalize_event(monkeypatch)
    action = {"command": "cat src/api.py"}

    _drive(attachment, action, {"returncode": 0, "output": "def get_user():\n"})

    assert captured, "observe_action_result never reached normalize_event"
    viewed = captured["kwargs"].get("viewed_files")
    assert viewed, (
        "seam did not forward viewed_files; caller_contract@file_view is dead "
        "in the installed path"
    )
    assert any(str(path).endswith("src/api.py") for path in viewed), viewed


def test_view_subject_comes_from_proposal_not_command_text(
    tmp_path,
    monkeypatch,
) -> None:
    """The forwarded tuple must be exactly the structured proposal subject.

    HONEST SCOPE (measured 2026-07-26, do not overstate this test).  It does
    NOT discriminate structured-subject from regex-derived identity, because on
    this seam the two cannot disagree: ``_effective_cmd`` SYNTHESISES
    ``cat <subject>`` from the structured proposal, so
    ``_view_target(_effective_cmd(action)) == proposal.action.subject`` for
    every reachable VIEW_SOURCE.  Probed 6 shapes -- including the mini-swe
    ``cd /testbed && ...`` prefix that historically muted post_search -- with
    zero divergence, so a regex-authoritative mutant is an EQUIVALENT mutant
    here, not a surviving one.

    What it does guard: the forwarded value is the subject itself (not a
    truncated, joined, or command-shaped string) and is a one-tuple.
    """
    attachment = _attachment(tmp_path)
    captured = _capture_normalize_event(monkeypatch)
    action = {"command": "cat src/api.py"}

    proposal = attachment.observe_action_proposal(action)
    if proposal is None or getattr(proposal.action, "subject", None) is None:
        pytest.skip("host did not resolve a structured VIEW subject")
    subject = proposal.action.subject

    try:
        attachment.observe_action_result(action, {"returncode": 0, "output": ""})
    except Exception:  # noqa: BLE001
        pass

    assert captured, "observe_action_result never reached normalize_event"
    assert captured["kwargs"].get("viewed_files") == (subject,)


def test_structured_view_yields_caller_contract_evidence_end_to_end() -> None:
    """The whole chain, on the INSTALLED path: view -> viewed_files ->
    file_view boundary -> caller_contract_view producer -> canonical evidence.

    The other tests here prove the seam FORWARDS the subject.  Forwarding is
    not producing -- that is the component-vs-wiring gap this file exists to
    close, and it applies to this file's own fix too.  Measured through the
    real gate driver:

        produce_raw   -> ('caller_contract_view',)
        evidence      -> feature_id='caller_contract'

    TRIGGER-ABSENCE NOTE (do not mistake for a defect).  Viewing
    ``pkg/mod_a.py`` yields NOTHING even though the fixture graph has
    ``CALLS consumer@pkg/mod_b.py -> alpha@pkg/mod_a.py``: that edge carries no
    ``source_line``, so the producer correctly declines rather than cite a line
    it does not have.  ``pkg/util.py`` is used here because
    ``handler -> sig_target`` is the ONLY fixture edge with a ``source_line``.
    """
    ss_dir = str(Path(__file__).resolve().parents[2] / "scripts" / "swebench")
    if ss_dir not in sys.path:
        sys.path.insert(0, ss_dir)
    import ss_gate as gate  # noqa: PLC0415 -- gate is a script, not a package

    from groundtruth.runtime import gateway

    produced: list[tuple] = []
    original = gateway.produce_raw

    def _spy(event, state):
        out = original(event, state)
        produced.append(tuple(getattr(e, "evidence_type", None) for e in out))
        return out

    features: list = []

    class _Probe(gate.CanonicalSeamDriver):
        def _canonical_facts(self):
            runtime = getattr(self._attachment, "attempt_runtime", None)
            if runtime is not None:
                features.extend(
                    getattr(record, "feature_id", None)
                    for record in runtime.journal.evidence_records_for_attempt(
                        runtime.attempt_id
                    )
                )
            return super()._canonical_facts()

    gateway.produce_raw = _spy
    try:
        _Probe().run(
            [
                gate.Event(
                    action={"command": "cat pkg/util.py"},
                    output="def sig_target(a, b):\n    return a\n",
                    rc=0,
                )
            ],
            {},
        )
    finally:
        gateway.produce_raw = original

    assert any("caller_contract_view" in row for row in produced), produced
    assert "caller_contract" in features, features


def test_non_view_operations_forward_no_viewed_files(
    tmp_path,
    monkeypatch,
) -> None:
    """Correct-or-quiet: a test/edit turn must not fabricate a file view."""
    attachment = _attachment(tmp_path)
    captured = _capture_normalize_event(monkeypatch)
    action = {"command": "pytest tests/test_api.py"}

    _drive(attachment, action, {"returncode": 1, "output": "1 failed"})

    assert captured, "observe_action_result never reached normalize_event"
    assert not captured["kwargs"].get("viewed_files")
