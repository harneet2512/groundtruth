"""C13: the canonical Mini-SWE route must mint a real observation join key.

The retired legacy batch formatter already populated ``ObservationBinding``. The
canonical runtime replaced that formatter but never re-homed the binding, leaving
every canonical delivery impossible to join to its originating policy observation.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from artifact_deepswe import gt_mini_patch as seam


def _compilation() -> SimpleNamespace:
    return SimpleNamespace(
        capsule_hash="a" * 64,
        capsule_text="canonical capsule bytes",
        observation_id="attempt-c13:observation:4",
        model_call_id="attempt-c13:model:5",
    )


def _plan() -> SimpleNamespace:
    return SimpleNamespace(
        compilation=_compilation(),
        delivery_attempt_id="attempt-c13:delivery:4",
    )


class _Boundary:
    def __init__(self) -> None:
        self.seen = []

    # SIGNATURE MUST TRACK THE REAL BOUNDARY. `MiniSweProviderBoundary.stage` accepts
    # `observation_binding` (miniswe_provider_boundary.py:275-281); this double did not, so the
    # seam's correct call raised TypeError and BOTH tests in this file failed against WORKING
    # production code. A stale test double reads exactly like a product defect -- and here it
    # masked the one safety property that most needs to stay green, that minting the binding
    # cannot switch shadow-holdout live.
    #
    # Accepting it positionally-by-keyword AND recording it means the double now proves the
    # argument actually arrives, rather than merely tolerating it.
    def stage(
        self,
        compilation,
        *,
        delivery_attempt_id: str = "",
        observation_binding=None,
    ) -> None:
        self.seen.append(
            (
                compilation,
                delivery_attempt_id,
                # Prefer the explicitly passed binding; fall back to the ContextVar so the
                # test still detects the older context-propagation route if it is used.
                observation_binding or seam._current_observation_binding(),
            )
        )


def _rows(path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_canonical_stage_mints_a_capsule_join_key_and_resets_context(
    monkeypatch,
    tmp_path,
) -> None:
    ledger = tmp_path / "runtime.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    message = {
        "content": "I will inspect the parser.",
        "extra": {
            "actions": [
                {"command": "grep -R parse_url src"},
                {"command": "sed -n '1,160p' src/url.py"},
            ]
        },
    }
    context = seam._canonical_observation_context(
        message,
        batch_start_iteration=3,
    )
    boundary = _Boundary()
    plan = _plan()

    binding = seam._stage_canonical_compilation(
        boundary,
        plan,
        observation_context=context,
    )

    assert boundary.seen == [
        (plan.compilation, plan.delivery_attempt_id, binding)
    ]
    assert binding is not None
    assert binding.batch_start_iteration == 3
    assert binding.parent_policy_chars == len(message["content"])
    assert binding.parent_policy_sha256 == hashlib.sha256(
        message["content"].encode("utf-8")
    ).hexdigest()
    assert binding.action_batch_sha256 == seam._batch_action_sha256(
        seam._batch_identity(message["extra"]["actions"])[1]
    )
    assert binding.candidate_id == plan.compilation.capsule_hash
    assert binding.candidate_ordinal == 0
    assert seam._current_observation_binding() is None

    rows = _rows(ledger)
    join = next(
        row
        for row in rows
        if row.get("layer") == "canonical_runtime.observation_binding"
    )
    assert join["delivery_attempt_id"] == plan.delivery_attempt_id
    assert join["capsule_hash"] == plan.compilation.capsule_hash
    assert join["canonical_observation_id"] == plan.compilation.observation_id
    assert join["canonical_model_call_id"] == plan.compilation.model_call_id
    assert join["candidate_id"] == plan.compilation.capsule_hash
    assert join["observation_binding"]["opportunity_id"] == binding.opportunity_id


def test_binding_proof_path_cannot_activate_shadow_holdout(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "runtime.jsonl"))
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "1")
    assignments = []
    monkeypatch.setattr(
        seam,
        "_record_shadow_assignment",
        lambda *args, **kwargs: assignments.append((args, kwargs)),
    )
    boundary = _Boundary()
    plan = _plan()
    context = seam._canonical_observation_context(
        {"content": "parent", "extra": {"actions": [{"command": "view x.py"}]}},
        batch_start_iteration=0,
    )

    seam._stage_canonical_compilation(
        boundary,
        plan,
        observation_context=context,
    )

    assert len(boundary.seen) == 1
    assert boundary.seen[0][0].capsule_text == "canonical capsule bytes"
    assert assignments == []


def test_non_mapping_result_clears_pending_observation_identity() -> None:
    action = object()
    attachment = seam.CanonicalRuntimeAttachment(
        attached=True,
        attempt_runtime=object(),
        provider_boundary=object(),
        gateway_state=object(),
        graph_revision="graph",
    )
    attachment.pending_observation_contexts[id(action)] = {"stale": True}

    attachment.observe_action_result(action, None)

    assert id(action) not in attachment.pending_observation_contexts


def test_commitment_context_mints_parent_identity_for_every_native_action(
    monkeypatch,
    tmp_path,
) -> None:
    class Model:
        def _prepare_messages_for_api(self, messages):
            return messages

        def _query(self, messages, **kwargs):
            return SimpleNamespace(id="", status="failed", choices=[])

    class Agent:
        def add_messages(self, *messages):
            return list(messages)

        def execute_actions(self, message):
            return []

    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(seam, "_db_path", lambda: str(tmp_path / "graph.db"))
    attachment = seam.install_canonical_runtime(
        model=Model(),
        agent=Agent(),
        env={
            "GT_ATTEMPT_ID": "attempt-c13-context",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_BRIEF_FILE": str(tmp_path / "missing-brief.txt"),
        },
        task="repair parser",
    )
    actions = [
        {"command": "grep -R parse_url src"},
        {"command": "sed -n '1,160p' src/url.py"},
    ]
    message = {
        "content": "I will locate and inspect parse_url.",
        "extra": {
            "response": {"id": "provider-call-c13"},
            "actions": actions,
        },
    }
    try:
        attachment._commitment_context(message)

        contexts = [
            attachment.pending_observation_contexts[id(action)]
            for action in actions
        ]
        assert contexts[0] == contexts[1]
        assert contexts[0] == seam._canonical_observation_context(
            message,
            batch_start_iteration=0,
        )
    finally:
        attachment.attempt_runtime.journal.close()
