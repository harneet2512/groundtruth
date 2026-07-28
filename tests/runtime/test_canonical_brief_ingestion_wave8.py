"""Task-start brief enters the canonical runtime as typed evidence, not task text."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime.reasoning_runtime import RevisionVector


REVISION = RevisionVector(
    repository_content="repo-brief",
    graph="graph-brief",
    lsp="lsp-brief",
    runtime_evidence="runtime-brief",
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


def test_sealed_brief_blocks_become_two_typed_records_without_task_tags(
    tmp_path,
) -> None:
    localization = "1. src/auth/session.py\n   refreshSession handles rotation."
    obligations = (
        "<gt-obligations>\n"
        "- preserve the returned Session\n"
        "</gt-obligations>"
    )
    brief = (
        "<gt-task-brief>\n"
        f"{localization}\n"
        f"{obligations}\n"
        "</gt-task-brief>"
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
                        "block_content_sha256": obligation_receipt[
                            "content_hash"
                        ],
                    },
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    records = seam._canonical_brief_records(
        {"GT_BRIEF_FILE": str(brief_path)},
        REVISION,
    )

    assert {record.feature_id for record in records} == {
        "localization",
        "obligations",
    }
    assert all(record.revision == REVISION for record in records)
    assert all("<gt-" not in record.claim for record in records)
    assert all(record.provenance for record in records)


def test_unsealed_or_unknown_brief_stays_quiet(tmp_path) -> None:
    brief_path = tmp_path / "brief.txt"
    brief_path.write_text("native issue", encoding="utf-8")
    (tmp_path / "brief_result.json").write_text(
        json.dumps(
            {
                "schema": "gt.brief_result.v1",
                "brief_text": "different bytes",
                "metrics": {"block_receipts": []},
            }
        ),
        encoding="utf-8",
    )

    assert seam._canonical_brief_records(
        {"GT_BRIEF_FILE": str(brief_path)},
        REVISION,
    ) == ()


def test_sealed_brief_is_compiled_and_staged_for_first_provider_call(
    tmp_path,
    monkeypatch,
) -> None:
    localization = "1. src/auth/session.py\n   resolved import path"
    brief = f"<gt-task-brief>\n{localization}\n</gt-task-brief>"
    receipt = _receipt(
        brief,
        localization,
        block_id="file-1",
        label="file-entry-1",
        fact_class="localization",
        candidate_id="loc-stage",
    )
    brief_path = tmp_path / "brief.txt"
    brief_path.write_text(brief, encoding="utf-8", newline="")
    (tmp_path / "brief_result.json").write_text(
        json.dumps(
            {
                "schema": "gt.brief_result.v1",
                "brief_text": brief,
                "metrics": {
                    "block_receipts": [receipt],
                    "localization_proof": [
                        {
                            "candidate_id": "loc-stage",
                            "path": "src/auth/session.py",
                            "witness": "resolved import path",
                            "witness_verified": True,
                        }
                    ],
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

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
    monkeypatch.setattr(
        seam,
        "_db_path",
        lambda: str(tmp_path / "graph.db"),
    )
    attachment = seam.install_canonical_runtime(
        model=Model(),
        agent=Agent(),
        env={
            "GT_ATTEMPT_ID": "attempt-brief-stage",
            "GT_RUNTIME_LEDGER": str(tmp_path / "runtime.jsonl"),
            "GT_BRIEF_FILE": str(brief_path),
        },
        task="native issue",
    )

    assert attachment.attached is True
    assert attachment.commitment_boundary is not None
    assert attachment.provider_boundary._active is not None
    assert (
        attachment.provider_boundary._active.delivery_attempt.state
        is not None
    )

    # C30 half 1 (M1): the staged capsule must carry the IDENTITY of what it delivers, paired
    # (candidate_id, fact_class) and parallel to evidence_ids. Without the candidate id the
    # canonical delivery row has no join key and this class can never receive attested truth;
    # without the fact class the row cannot prove its own attribution to an offline reader.
    # Asserted STRUCTURALLY (not against a literal dedup key) so it stays true when the brief
    # fixture changes -- the binding it pins is id-to-key, not one particular hash.
    active = attachment.provider_boundary._active
    assert len(active.evidence_lineage) == len(active.evidence_ids) == 1
    candidate_id, fact_class = active.evidence_lineage[0]
    assert fact_class == "localization"
    assert candidate_id and active.evidence_ids[0] == (
        f"GT-E-{candidate_id}-g" + active.evidence_ids[0].rsplit("-g", 1)[1]
    )
    attachment.attempt_runtime.journal.close()
