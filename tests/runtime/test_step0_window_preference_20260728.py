"""C18 — step 0 must spend its ONE capsule on the evidence whose window is CLOSING.

THE DEFECT (2026-07-28). `_stage_initial_canonical_evidence` hardcoded
`preferred_context = SOURCE_TARGET_SELECTION if any(...)`, which is exactly inverted.
Step 0 is the ONE `task_start` observation an attempt ever has and the dose law permits
ONE capsule per observation, so that preference spent the single slot on evidence with
many later windows and PERMANENTLY discarded the only evidence whose window was closing:

    obligations    deliver_by=task_start     <- the ONLY class; never recurs
    localization   deliver_by=search_result  <- recurs on every agent search
    def_partition  deliver_by=search_result  <- recurs on every agent search

`obligations` is the sole standing carrier of BEHAVIORAL_CONTRACT, the required role of
both PATCH_CONSTRUCTION and SOURCE_UNDERSTANDING. The seam's own comment records the
consequence: `unresolved_roles` is BEHAVIORAL_CONTRACT on 70 of 90 compile attempts.

This is not a hypothetical shape. `test_sealed_brief_blocks_become_two_typed_records_
without_task_tags` in this same directory already asserts a realistic brief yields BOTH
`localization` and `obligations` records — the contested case is the normal case.

These tests are RED before the registry-derived preference lands and GREEN after.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime.fact_registry import EVENT_TASK_START, registration_for
from groundtruth.runtime.reasoning_runtime import RevisionVector


REVISION = RevisionVector(
    repository_content="repo-c18",
    graph="graph-c18",
    lsp="lsp-c18",
    runtime_evidence="runtime-c18",
)


def _receipt(
    brief: str,
    block: str,
    *,
    block_id: str,
    label: str,
    fact_class: str,
    candidate_id: str,
) -> dict:
    start = brief.index(block)
    return {
        "block_id": block_id,
        "label": label,
        "fact_class": fact_class,
        "candidate_id": candidate_id,
        "char_span": [start, start + len(block)],
        "content_hash": hashlib.sha256(block.encode("utf-8")).hexdigest(),
    }


def _brief_with_both_contexts(tmp_path):
    """A sealed brief carrying localization AND obligations — the contested case."""
    localization = "1. src/auth/session.py\n   refreshSession handles rotation."
    obligations = (
        "<gt-obligations>\n- preserve the returned Session\n</gt-obligations>"
    )
    brief = (
        "<gt-task-brief>\n" f"{localization}\n" f"{obligations}\n" "</gt-task-brief>"
    )
    loc_receipt = _receipt(
        brief,
        localization,
        block_id="file-1",
        label="file-entry-1",
        fact_class="localization",
        candidate_id="loc-1",
    )
    obligation_receipt = _receipt(
        brief,
        obligations,
        block_id="obligations",
        label="obligations",
        fact_class="obligations",
        candidate_id="obl-1",
    )
    brief_path = tmp_path / "brief.txt"
    brief_path.write_text(brief, encoding="utf-8", newline="")
    (tmp_path / "brief_result.json").write_text(
        json.dumps(
            {
                "schema": "gt.brief_result.v1",
                "brief_text": brief,
                "metrics": {
                    "block_receipts": [loc_receipt, obligation_receipt],
                    "localization_proof": [
                        {
                            "candidate_id": "loc-1",
                            "path": "src/auth/session.py",
                            "witness": "resolved import path",
                            "witness_verified": True,
                        }
                    ],
                    "obligations_record": {
                        "schema": "gt.obligations_record.v1",
                        "candidate_id": "obl-1",
                        "block_content_sha256": obligation_receipt["content_hash"],
                    },
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return brief_path


# --------------------------------------------------------------------------- #
# POSITIVE CONTROL. Without this the main test is unreadable: the natural (wrong)
# implementation compares a registration against the seam's EVENT_STEP0, which is the
# string "step0" while the registry says "task_start" (translation table at
# gateway.py:1905). That cross-namespace compare matches NOTHING, so the helper would
# always return False, step 0 would fall through to records[0], and a run could LOOK
# fixed for the wrong reason. This asserts the instrument can produce BOTH answers.
# --------------------------------------------------------------------------- #
def test_deliver_by_helper_separates_closing_from_recurring_windows() -> None:
    assert seam._deliver_by_is_task_start(SimpleNamespace(feature_id="obligations"))
    assert not seam._deliver_by_is_task_start(
        SimpleNamespace(feature_id="localization")
    )
    assert not seam._deliver_by_is_task_start(
        SimpleNamespace(feature_id="def_partition")
    )
    # An unregistered / empty class is simply not due here — never an exception.
    assert not seam._deliver_by_is_task_start(SimpleNamespace(feature_id=""))
    assert not seam._deliver_by_is_task_start(SimpleNamespace(feature_id="nonsuch"))


def test_registry_premise_the_rule_depends_on() -> None:
    """Pin the premise, not the whole registry: obligations closes here, localization does not."""
    obligations = registration_for("obligations")
    localization = registration_for("localization")
    def_partition = registration_for("def_partition")
    assert obligations is not None
    assert localization is not None
    assert def_partition is not None
    assert obligations.deliver_by == EVENT_TASK_START
    assert localization.deliver_by != EVENT_TASK_START
    assert def_partition.deliver_by != EVENT_TASK_START


# --------------------------------------------------------------------------- #
# THE BEHAVIOURAL TEST — end to end through install_canonical_runtime, reading the
# step-0 row GT itself writes. Not source-text inspection: a source assertion would
# pass on a comment and prove nothing about what got staged.
# --------------------------------------------------------------------------- #
def test_step0_stages_the_closing_window_and_holds_the_recurring_one(
    tmp_path,
    monkeypatch,
) -> None:
    brief_path = _brief_with_both_contexts(tmp_path)
    ledger = tmp_path / "runtime.jsonl"

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
    # `_runtime_ledger_path()` reads os.environ (gt_mini_patch.py:9491), NOT the env
    # mapping handed to install_canonical_runtime. Passing GT_RUNTIME_LEDGER only in
    # that dict wrote the row to the DEFAULT path and this test failed with
    # FileNotFoundError -- a failure that looks like the defect but is not it.
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))

    attachment = seam.install_canonical_runtime(
        model=Model(),
        agent=Agent(),
        env={
            "GT_ATTEMPT_ID": "attempt-c18",
            "GT_RUNTIME_LEDGER": str(ledger),
            "GT_BRIEF_FILE": str(brief_path),
        },
        task="native issue",
    )
    try:
        # Assert on PRODUCT STATE (evidence lifecycle), not on a telemetry string. The
        # step-0 ledger row keys its layer as `layer` and drops `extra` from the
        # in-memory Ledger entirely, so a telemetry-based assertion here would grade an
        # instrument that cannot see the decision at all.
        store = getattr(attachment.attempt_runtime, "_evidence", {})
        by_class = {
            rec.feature_id: rec
            for rec in (store.values() if hasattr(store, "values") else ())
        }

        # The contest must be real: both contexts present, or the assertions below are
        # vacuous on a brief that only ever had one.
        assert "obligations" in by_class, by_class
        assert "localization" in by_class, by_class

        assert by_class["obligations"].lifecycle.name == "RELEASED", (
            "step 0 spent its ONE capsule on the RECURRING window and HELD the CLOSING "
            "one. obligations has deliver_by=task_start and task_start does not come "
            "again, so it can now never be delivered — and it is the sole standing "
            "carrier of BEHAVIORAL_CONTRACT. "
            f"obligations={by_class['obligations'].lifecycle.name} "
            f"localization={by_class['localization'].lifecycle.name}"
        )
        # localization is not lost: deliver_by=search_result recurs on every search.
        assert by_class["localization"].lifecycle.name == "HELD", (
            "localization should defer to its own recurring search_result window; "
            f"got {by_class['localization'].lifecycle.name}"
        )
    finally:
        attachment.attempt_runtime.journal.close()
