"""A DECISION_INCOMPLETE row must name WHICH suppression emptied the coalition.

WHAT THE RUN SHOWS. In live run 30390877219 every task emitted repeated
``canonical_runtime.compilation | suppressed_internal_only | FAILED:DECISION_INCOMPLETE``
rows. In ``gt_runtime_ledger_aiogram__aiogram-1594.jsonl`` all nine of them carry the SAME
fingerprint: ``unresolved_roles: ["TARGET_IDENTITY"]``, ``evidence_store: 1``,
``evidence_lifecycles: {"ACTIVE": 1}``, ``coalition_size: 0``.

WHY THAT IS STILL UNREADABLE. The seam's own discriminator comment
(``gt_mini_patch.py``, the ``POOL vs COALITION`` block above ``evidence_store``) says
``evidence_store > 0`` with ``coalition_size == 0`` means "evidence existed and none was
eligible" -- i.e. a SUPPRESSION happened. It does not say WHICH one, and
``select_evidence_coalition`` (``reasoning_runtime.py:6496-6558`` for the per-item pass,
plus the dedup / budget / value passes below it) can stamp any of THIRTEEN
``SuppressionReason`` values. Their fixes point in opposite directions:

  * ``ALREADY_ACQUIRED`` -> GT is correctly quiet; the agent already has the file. Nothing
    to fix upstream, and "make the producer louder" would be actively wrong.
  * ``NOT_ACTIONABLE_FOR_DECISION`` -> the pool holds evidence whose ROLES do not match the
    open decision. A producer/role-mapping gap; louder producers of the same class change
    nothing.
  * ``STALE`` / ``NOT_READY`` -> a freshness or lifecycle-promotion bug, a third fix again.

One number, three incompatible diagnoses. That is the same ambiguity the sibling files
(`test_compilation_explains_why_20260727.py`, `test_capsule_can_compile_at_all_20260727.py`)
removed one layer up, stopping exactly short of the reason itself.

WHAT THIS FILE PINS. The row must carry ``suppression_reasons``: a name -> count map over
``plan.oracle_decision.suppressed``. TWO cases with DIFFERENT reasons, because a single case
passes just as well against a hardcoded constant and diagnoses nothing -- the field has to
VARY with the actual suppression. Both cases also re-assert the fields that exist today
(``unresolved_roles`` / ``evidence_store`` / ``coalition_size``), so this doubles as a
regression guard on the row's shape.

METHOD. The suppression records are NOT hand-written: ``test_the_two_reasons_are_reachable_
from_real_selection`` builds them by running the REAL ``select_evidence_coalition``, and the
same real ``OracleDecision`` is then handed to the seam inside an ``InferencePlan``. The plan
is the seam's INPUT (it comes back from ``prepare_next_inference``), so substituting it
controls the variable under test without faking the code under test: the row-writing block
in ``observe_action_result`` runs for real, on a real installed attachment, and the assertion
reads the durable ledger file the run itself would produce.

Telemetry only: ``chars=0``, ``outcome=suppressed_internal_only``, never model-facing. A fix
that makes this green by widening ``held_evidence`` or by deriving a reason from
``unresolved_roles`` is not the fix -- read ``suppressed``.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)

ACQUIRED = "src/pkg/already_seen.py"


# --------------------------------------------------------------------------------------
# Real selection: build the two OracleDecisions with the production selector.
# --------------------------------------------------------------------------------------


def _decision() -> rr.ActiveDecision:
    work_state = dataclasses.replace(
        rr.WorkState.initial(attempt_id="attempt-1", revision=REVISION),
        focused_files=(ACQUIRED,),
    )
    return seam.CanonicalRuntimeAttachment._active_decision(
        (), work_state, REVISION, ()
    )


def _record(
    evidence_id: str,
    *,
    subject: str,
    feature_id: str = "localization",
    claim: str = "",
) -> rr.EvidenceRecord:
    """A record whose roles/context/dependencies come from the REAL feature contract.

    They cannot be hand-picked: ``EvidenceRecord.__post_init__`` rejects any record whose
    roles do not exactly match its contract, which is also why the two cases below differ by
    FEATURE rather than by an invented role tuple.
    """
    contract = rr.feature_contract_for(feature_id)
    decision_context = contract.decision_context
    return rr.EvidenceRecord(
        evidence_id=evidence_id,
        feature_id=feature_id,
        decision_context=decision_context,
        roles=contract.roles,
        subject=subject,
        claim=claim or f"claim about {subject} ({evidence_id})",
        actionable_consequence=f"edit {subject} ({evidence_id})",
        provenance=(f"{subject}:7",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        causal_neighborhood=(
            f"decision:{decision_context.value}",
            f"fact:{feature_id}",
            f"subject:{subject}",
        ),
        lifecycle=rr.EvidenceLifecycle.READY,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        revision_dependencies=contract.revision_dependencies,
        token_cost=120,
        failure_prevention=3,
        causal_value=3,
        contradiction_resolution=0,
        anchoring_risk=0,
    )


def _already_acquired_oracle() -> rr.OracleDecision:
    """CASE 1. The agent already possesses the only target-identity carrier, so GT is
    CORRECTLY quiet. Nothing upstream is broken; a louder producer would be a regression."""
    decision = _decision()
    record = _record("ev-acquired", subject=ACQUIRED, feature_id="localization")
    return rr.select_evidence_coalition(
        decision,
        (record,),
        role_driven=True,
        acquired_subjects=(ACQUIRED,),
    )


def _not_actionable_oracle() -> rr.OracleDecision:
    """CASE 2. The pool holds evidence whose ROLES the open decision neither requires nor
    finds useful -- a role/producer gap, the opposite diagnosis and the opposite fix.

    ``syntax_result`` carries (BLOCKER, VALIDATION); the open SOURCE_TARGET_SELECTION
    decision requires TARGET_IDENTITY and finds EXECUTION_REACHABILITY / STATE_DEPENDENCY /
    MATERIAL_UNCERTAINTY / BEHAVIORAL_CONTRACT useful. Role-driven eligibility is what makes
    this reachable at all: under producer partitioning the record would be stamped
    OTHER_DECISION first and the role mismatch would never be reached.
    """
    decision = _decision()
    records = tuple(
        _record(
            f"ev-inert-{index}",
            subject=ACQUIRED,
            feature_id="syntax_result",
            # Distinct claims: identical ones dedup to DUPLICATE_CLAIM instead.
            claim=f"inert claim {index}",
        )
        for index in range(2)
    )
    return rr.select_evidence_coalition(decision, records, role_driven=True)


CASES = (
    (
        "already_acquired",
        _already_acquired_oracle,
        {"ALREADY_ACQUIRED": 1},
    ),
    (
        "not_actionable",
        _not_actionable_oracle,
        {"NOT_ACTIONABLE_FOR_DECISION": 2},
    ),
)


def test_the_two_reasons_are_reachable_from_real_selection() -> None:
    """POSITIVE CONTROL. If the selector cannot actually produce these two reasons, the
    ledger assertions below would be testing an invented enum value and would say nothing
    about production."""
    for name, factory, expected in CASES:
        oracle = factory()
        counts: dict[str, int] = {}
        for suppression in oracle.suppressed:
            counts[suppression.reason.name] = counts.get(suppression.reason.name, 0) + 1
        assert counts == expected, f"{name}: real selector produced {counts}"
        # The production fingerprint this file exists to explain.
        assert oracle.coalition == ()
        assert oracle.decision_complete is False
        assert [role.name for role in oracle.unresolved_roles] == ["TARGET_IDENTITY"]


# --------------------------------------------------------------------------------------
# The seam: drive the installed attachment and read the durable ledger row.
# --------------------------------------------------------------------------------------


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


def _attachment(tmp_path: Path, monkeypatch, attempt_id: str):
    repo = tmp_path / "repo"
    source = repo / "src" / "a.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "suppression@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Suppression fixture"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "src/a.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=True)

    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None)
    monkeypatch.setattr(seam, "_root", lambda: str(repo))
    monkeypatch.setattr(seam, "_db_path", lambda: str(tmp_path / "graph.db"))
    monkeypatch.setattr(
        seam,
        "_invalidate_on_edit",
        lambda *_args, **_kwargs: seam._L6RefreshOutcome(
            True,
            "test_refresh",
            str(tmp_path / "graph.db"),
        ),
    )

    ledger = tmp_path / "runtime.jsonl"
    # `_ledger_line_direct` resolves the durable sink from os.environ, NOT from the env dict
    # handed to `install_canonical_runtime`. Setting only the latter writes the rows to the
    # process-wide default path and this fixture would read an empty/absent file.
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
        task="edit src/a.py and verify the behavior",
    )
    assert attachment.attached is True
    return attachment, ledger


def _plan_for(attachment, oracle: rr.OracleDecision) -> rr.InferencePlan:
    """The exact shape ``prepare_next_inference`` returns for a quiet turn: no delivery
    attempt, and a compilation the REAL compiler failed with DECISION_INCOMPLETE."""
    compilation = rr.compile_observation_capsule(
        native_observation="",
        decision=oracle,
        observation_id="obs-suppression",
        source_model_call_id="mc-0",
        model_call_id="mc-1",
        enabled=True,
    )
    assert compilation.state is rr.CapsuleCompilationState.FAILED
    assert compilation.failure_code == "DECISION_INCOMPLETE"
    return rr.InferencePlan(
        active_decision=_decision(),
        oracle_decision=oracle,
        compilation=compilation,
        delivery_attempt_id="",
        held_evidence_ids=(),
        suppressed_decision_ids=(),
        native_observation="",
        assurance=rr.AssuranceStatus.UNASSURED,
    )


def _seed_store(attachment) -> None:
    """One stored record, so the seam reaches the plan branch and ``evidence_store`` is 1 --
    the live fingerprint."""
    attachment.attempt_runtime.ingest_evidence(
        dataclasses.replace(
            _record("ev-store-seed", subject=ACQUIRED, feature_id="localization"),
            revision=attachment.attempt_runtime.work_state.revision,
            # The journal only accepts DISCOVERED/PENDING as an INITIAL lifecycle. The live
            # fingerprint's ACTIVE is reached by later transitions; irrelevant here, since
            # only the STORE SIZE feeds `evidence_store`.
            lifecycle=rr.EvidenceLifecycle.PENDING,
        )
    )


def _compilation_rows(ledger: Path) -> list[dict]:
    rows: list[dict] = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("layer") == "canonical_runtime.compilation":
            rows.append(row)
    return rows


def _drive_one_quiet_observation(attachment, oracle: rr.OracleDecision) -> None:
    plan = _plan_for(attachment, oracle)
    attachment.attempt_runtime.prepare_next_inference = (
        lambda **_kwargs: plan  # type: ignore[method-assign]
    )
    action = {"operation": "SEARCH", "command": "grep -rn value src"}
    attachment.observe_action_proposal(action)
    attachment.observe_action_result(action, {"output": "src/a.py:1:value", "returncode": 0})


@pytest.mark.parametrize(
    ("case", "oracle_factory", "expected_reasons"),
    CASES,
    ids=[case for case, _factory, _expected in CASES],
)
def test_decision_incomplete_row_names_the_suppression_reason(
    tmp_path: Path,
    monkeypatch,
    case: str,
    oracle_factory,
    expected_reasons: dict[str, int],
) -> None:
    attachment, ledger = _attachment(tmp_path, monkeypatch, f"attempt-suppress-{case}")
    try:
        _seed_store(attachment)
        _drive_one_quiet_observation(attachment, oracle_factory())

        rows = [
            row
            for row in _compilation_rows(ledger)
            if row.get("reason") == "FAILED:DECISION_INCOMPLETE"
        ]
        assert rows, (
            "no FAILED:DECISION_INCOMPLETE compilation row was written -- the fixture no "
            "longer reproduces the production quiet turn, so nothing below is meaningful"
        )
        row = rows[-1]

        # ---- REGRESSION GUARD on the fields that already exist today. ----
        assert row["outcome"] == "suppressed_internal_only"
        assert row["chars_delivered"] == 0
        assert row["unresolved_roles"] == ["TARGET_IDENTITY"]
        assert row["coalition_size"] == 0
        assert row["evidence_store"] == len(attachment.attempt_runtime._evidence)
        assert row["evidence_store"] >= 1

        # ---- THE GAP. ----
        assert "suppression_reasons" in row, (
            "the row says evidence existed (evidence_store>0) and none was eligible "
            "(coalition_size==0) but never says WHICH of the 13 SuppressionReasons fired. "
            "ALREADY_ACQUIRED (correctly quiet) and NOT_ACTIONABLE_FOR_DECISION (a role/"
            "producer gap) are indistinguishable here, and their fixes are opposite."
        )
        assert row["suppression_reasons"] == expected_reasons, (
            "the field must be derived from plan.oracle_decision.suppressed -- a constant "
            "or a value derived from unresolved_roles cannot vary between these two cases"
        )
    finally:
        attachment.attempt_runtime.journal.close()
