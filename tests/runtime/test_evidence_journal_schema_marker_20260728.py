"""C28(a) — the evidence journal must record WHICH schema wrote each row.

THE DEFECT, in the seam's own words (reasoning_runtime.py:5237-5242):

    "`_evidence_record_from_json` reads `raw.get("observed_substrates", ())`, and the
    evidence journal has NO schema marker or migration, so every row written before the
    field existed rehydrates to `()` and is permanently HELD on replay/resume. That is a
    JOURNAL VERSIONING gap and must be fixed there -- the same way `canonical_events` got
    `hash_schema` -- not by making this gate permissive."

A row that PREDATES `observed_substrates` and a row that legitimately observed NOTHING
rehydrate to the byte-identical `()`. The substrate gate then holds both, correctly for the
second and undiagnosably for the first.

WHAT THIS FIX DOES AND DOES NOT DO. It makes the two cases DISTINGUISHABLE and makes an
unknown future schema fail LOUDLY instead of being silently misread. It does NOT "unhold"
legacy rows: substrate evidence that was never recorded cannot be recovered, and holding a
record that cannot evidence its own substrate is correct-or-quiet, not a bug. The gate at
:5243 stays strict — weakening it re-opens cross-record substrate lending and was already
adjudicated wrong.

Mirrors the canonical_events pattern verified under C5: exported constant + column with
DEFAULT '' + ALTER migration + writer stamp + fail-closed reader.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from groundtruth.runtime import reasoning_runtime as rr


REVISION = rr.RevisionVector(
    repository_content="repo-c28a",
    graph="graph-c28a",
    lsp="lsp-c28a",
    runtime_evidence="runtime-c28a",
)


def _journal(tmp_path, name: str = "c28a.sqlite3"):
    journal = rr.RuntimeJournal(tmp_path / name)
    journal.open()
    return journal


def _record(
    observed_substrates: tuple[str, ...] | None = None,
) -> rr.EvidenceRecord:
    contract = rr.feature_contract_for("caller_contract")
    assert contract is not None
    substrates = (
        tuple(sorted(contract.fallback_policy.preferred_substrates))
        if observed_substrates is None
        else observed_substrates
    )
    return rr.EvidenceRecord(
        evidence_id="GT-E-c28a",
        feature_id="caller_contract",
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject="refreshSession",
        claim="Callers require the Session return contract.",
        actionable_consequence="Preserve the Session return contract.",
        provenance=("src/auth/session.py:41",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        causal_neighborhood=("decision:patch-c28a",),
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
        authority=rr.Authority.STRUCTURED,
        observed_substrates=substrates,
    )


def _columns(journal, table: str) -> set[str]:
    return {
        str(row[1])
        for row in journal.connection.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }


# --------------------------------------------------------------------------- #
# 1. ONE exported constant. The capsule-schema defect (D4) was a literal hand-copied into
#    four files; readers reported tamper instead of version skew. Never repeat it.
# --------------------------------------------------------------------------- #
def test_evidence_record_schema_is_a_single_exported_constant() -> None:
    assert isinstance(rr.EVIDENCE_RECORD_SCHEMA, str)
    assert rr.EVIDENCE_RECORD_SCHEMA.startswith("gt.evidence_record.v")


# --------------------------------------------------------------------------- #
# 2 + 4. The column exists on BOTH evidence tables and the writer stamps it.
# --------------------------------------------------------------------------- #
def test_both_evidence_tables_carry_a_record_schema_column(tmp_path) -> None:
    journal = _journal(tmp_path)
    try:
        assert "record_schema" in _columns(journal, "evidence_attempt_journal")
        assert "record_schema" in _columns(journal, "evidence_journal")
    finally:
        journal.close()


def test_writer_stamps_the_current_schema_on_every_row(tmp_path) -> None:
    journal = _journal(tmp_path)
    try:
        journal.append_evidence(_record(), attempt_id="attempt-c28a")
        rows = journal.connection.execute(
            "SELECT record_schema FROM evidence_attempt_journal"
        ).fetchall()
        assert rows, "no evidence row was written at all — instrument is dead"
        assert {str(r[0]) for r in rows} == {rr.EVIDENCE_RECORD_SCHEMA}
    finally:
        journal.close()


# --------------------------------------------------------------------------- #
# 3. MIGRATION. Without it an older journal keeps reporting the field as absent, which is
#    the exact failure canonical_events' ALTER TABLE exists to prevent.
# --------------------------------------------------------------------------- #
def test_pre_existing_journal_without_the_column_is_migrated(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE evidence_attempt_journal (
            attempt_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            journal_sequence INTEGER NOT NULL,
            lifecycle TEXT NOT NULL,
            state_hash TEXT NOT NULL,
            canonical_json TEXT NOT NULL,
            PRIMARY KEY(attempt_id, evidence_id, journal_sequence)
        );
        CREATE TABLE evidence_journal (
            evidence_id TEXT NOT NULL,
            journal_sequence INTEGER NOT NULL,
            attempt_id TEXT NOT NULL DEFAULT '',
            lifecycle TEXT NOT NULL,
            state_hash TEXT NOT NULL,
            canonical_json TEXT NOT NULL,
            PRIMARY KEY(evidence_id, journal_sequence)
        );
        """
    )
    raw.commit()
    raw.close()

    journal = rr.RuntimeJournal(path)
    journal.open()
    try:
        assert "record_schema" in _columns(journal, "evidence_attempt_journal")
        assert "record_schema" in _columns(journal, "evidence_journal")
    finally:
        journal.close()


# --------------------------------------------------------------------------- #
# 5. FAIL-CLOSED READ on an unknown schema, and the DISTINCTION that is the whole point.
# --------------------------------------------------------------------------- #
def test_unknown_record_schema_raises_instead_of_being_misread(tmp_path) -> None:
    journal = _journal(tmp_path)
    try:
        # The journal is append-only (an UPDATE trigger RAISES), so a future-schema row
        # has to be INSERTED, which is also how one would really arrive: written by a
        # newer build sharing the same file.
        payload = _record().canonical_json()
        journal.connection.execute(
            """
            INSERT INTO evidence_attempt_journal(
                attempt_id, evidence_id, journal_sequence, lifecycle,
                state_hash, canonical_json, record_schema
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "attempt-future",
                "GT-E-c28a",
                1,
                rr.EvidenceLifecycle.PENDING.value,
                rr._sha256(payload),
                payload,
                "gt.evidence_record.v999",
            ),
        )
        journal.connection.commit()
        with pytest.raises(rr.StateIntegrityError) as excinfo:
            journal.evidence_history("GT-E-c28a", attempt_id="attempt-future")
        assert "schema" in str(excinfo.value).lower()
    finally:
        journal.close()


def test_legacy_row_is_distinguishable_from_a_genuinely_empty_one(
    tmp_path,
) -> None:
    """The defect itself: both rehydrate to (), so only the marker separates them."""
    journal = _journal(tmp_path)
    try:
        # (a) a CURRENT-schema row that legitimately observed nothing.
        journal.append_evidence(
            _record(observed_substrates=()), attempt_id="attempt-empty"
        )
        # (b) a LEGACY row: written before the field existed, so the key is ABSENT from
        #     the payload entirely and its schema column is ''.
        legacy = json.loads(_record().canonical_json())
        legacy.pop("observed_substrates", None)
        payload = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
        journal.connection.execute(
            """
            INSERT INTO evidence_attempt_journal(
                attempt_id, evidence_id, journal_sequence, lifecycle,
                state_hash, canonical_json, record_schema
            ) VALUES (?, ?, ?, ?, ?, ?, '')
            """,
            (
                "attempt-legacy",
                "GT-E-c28a",
                1,
                legacy["lifecycle"],
                rr._sha256(payload),
                payload,
            ),
        )
        journal.connection.commit()

        schemas = dict(
            journal.connection.execute(
                "SELECT attempt_id, record_schema FROM evidence_attempt_journal"
            ).fetchall()
        )
        assert schemas["attempt-empty"] == rr.EVIDENCE_RECORD_SCHEMA
        assert schemas["attempt-legacy"] == ""

        # Both still rehydrate to () — that is NOT what this fix changes. What changes is
        # that a reader can now tell WHY, instead of conflating "never recorded" with
        # "observed nothing". A legacy row stays readable, so SS-10 replay of recorded
        # artifacts is not broken.
        legacy_record = journal.evidence_history(
            "GT-E-c28a", attempt_id="attempt-legacy"
        )[-1]
        empty_record = journal.evidence_history(
            "GT-E-c28a", attempt_id="attempt-empty"
        )[-1]
        assert legacy_record.observed_substrates == ()
        assert empty_record.observed_substrates == ()
    finally:
        journal.close()
