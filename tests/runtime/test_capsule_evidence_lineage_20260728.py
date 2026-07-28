"""C30 half 1 — the compiled capsule must carry the IDENTITY of the evidence it delivers.

WHY THIS EXISTS. `gt.canonical_delivery.v1` is the only row the capsule path writes, and
`attestation_join._delivered_row_index` keys DELIVERED rows on `(candidate_id,
content_sha256_16)`. The canonical row carries neither a candidate id nor a fact class, so it is
skipped entirely: localization and obligations — the two classes that ship on the step-0 capsule
path — can never receive joined truth, so `correct_info` is `None` and their terminal is
SEALED_DELIVERED_UNGRADED on every run, however good the run.

The eight classes that DO have working attestations all ship on the gateway/native lanes, which
stamp `candidate_id` = the envelope's `dedup_key`. That asymmetry is the bug, not a design choice.

WHY NOT PARSE IT OFFLINE. The dedup key IS recoverable from the evidence id
(`GT-E-{dedup_key}-g{sha(revision)}`), so the join could be "fixed" by parsing, with no runtime
change at all. That was rejected: J6 admits a row to the index only when the ROW proves its own
attribution, and a canonical row proves nothing about registration on its own bytes. The
registration check in `canonical_evidence_from_envelope` is real but UPSTREAM and invisible to an
offline reader — indexing on it would downgrade J6 from "the row proves it" to "trust that
someone checked earlier". So the identity is carried FORWARD onto the artifact instead.

THE FORMAT KNOWLEDGE STAYS IN ONE PLACE. `_dedup_key_from_evidence_id` is the inverse of
`_revision_bound_evidence_id` and lives beside it, so the id format is written down once.

BITING MUTATIONS (applied, observed RED, reverted by a TARGETED restore -- `git checkout --`
on a file with uncommitted work throws the work away, which is how I lost this implementation
once already):
  M1 — populate `evidence_lineage` with `(feature_id, feature_id)`, dropping the dedup key:
       `test_canonical_brief_ingestion_wave8::test_sealed_brief_is_compiled_and_staged_for_
       first_provider_call` goes RED (it pins the id-to-key binding on a REAL staged capsule).
  M2 — drop `evidence_lineage` from the JSON round-trip:
       `test_lineage_survives_the_journal_round_trip` and
       `test_malformed_lineage_entries_are_skipped_not_coerced` go RED (a dropped field is
       invisible until a replayed capsule silently loses its join key).
"""

from __future__ import annotations

from groundtruth.runtime import reasoning_runtime as rr


def test_dedup_key_round_trips_through_the_evidence_id() -> None:
    """CALIBRATION for the parse: the inverse must recover exactly what was put in.

    Without this, a helper that always returned "" would make every assertion below read as a
    passing empty case.
    """
    revision = rr.RevisionVector(
        repository_content="repo",
        graph="graph",
        lsp="lsp",
        runtime_evidence="runtime",
    )
    evidence_id = rr._revision_bound_evidence_id("ac032ea694307691", revision)
    assert evidence_id.startswith("GT-E-ac032ea694307691-g")
    assert rr._dedup_key_from_evidence_id(evidence_id) == "ac032ea694307691"


def test_dedup_key_parse_is_fail_closed_on_a_foreign_id() -> None:
    """An id this module did not mint yields "" — never a guessed identity.

    A wrong candidate id joins an attestation to the wrong delivery, which is worse than no
    join at all: it would seat truth against bytes that never carried that fact.
    """
    for foreign in ("", "GT-E-", "not-an-id", "GT-E-nog-suffix", "GT-X-abc-gdef"):
        assert rr._dedup_key_from_evidence_id(foreign) == ""


def test_the_evidence_id_split_survives_a_g_inside_the_generation_hash() -> None:
    """The split is on the LAST `-g`, and dedup keys are hex so `-g` cannot occur inside one."""
    revision = rr.RevisionVector(
        repository_content="r", graph="g", lsp="l", runtime_evidence="e",
    )
    evidence_id = rr._revision_bound_evidence_id("deadbeefdeadbeef", revision)
    assert rr._dedup_key_from_evidence_id(evidence_id) == "deadbeefdeadbeef"


def test_capsule_compilation_defaults_lineage_to_empty() -> None:
    """The failure/disabled constructors do not pass it; they must stay constructible."""
    assert rr.CapsuleCompilation.__dataclass_fields__["evidence_lineage"].default == ()


def _compilation(**overrides) -> rr.CapsuleCompilation:
    fields = {
        "state": rr.CapsuleCompilationState.COMPILED,
        "native_observation": "obs",
        "decision_context": rr.DecisionContext.PATCH_CONSTRUCTION,
        "observation_id": "observation-1",
        "source_model_call_id": "call-0",
        "model_call_id": "call-1",
        "evidence_ids": ("GT-E-ac032ea694307691-gdeadbeef",),
        "evidence_lineage": (("ac032ea694307691", "obligations"),),
        "capsule_text": "[GroundTruth] Evidence",
        "rendered_content_hash": "d" * 64,
        "evidence_manifest_hash": "e" * 64,
        "capsule_hash": "c" * 64,
        "delivery_attempt": rr.DeliveryAttempt(
            evidence_ids=("GT-E-ac032ea694307691-gdeadbeef",),
            capsule_hash="c" * 64,
            model_call_id="call-1",
        ),
    }
    fields.update(overrides)
    return rr.CapsuleCompilation(**fields)


def test_lineage_normalizes_to_tuples_of_str() -> None:
    """A list-of-lists (the JSON shape) must not survive as a list on a frozen record."""
    compilation = _compilation(evidence_lineage=[["ac032ea694307691", "obligations"]])
    assert compilation.evidence_lineage == (("ac032ea694307691", "obligations"),)


def test_lineage_survives_the_journal_round_trip() -> None:
    """M2. A dropped field is invisible until a REPLAYED capsule silently loses its join key.

    The compilation journal serializes with the generic canonical encoder and rehydrates
    through `_capsule_compilation_from_json`, so the field has to be read back explicitly.
    """
    original = _compilation()
    restored = rr._capsule_compilation_from_json(rr._canonical_json(original))
    assert restored.evidence_lineage == original.evidence_lineage
    assert restored == original


def test_malformed_lineage_entries_are_skipped_not_coerced() -> None:
    """A half-read pair is not an identity, and a coerced one would join the wrong bytes."""
    payload = rr._canonical_json(_compilation())
    broken = payload.replace(
        '[["ac032ea694307691","obligations"]]',
        '[["ac032ea694307691"],["a","b","c"],"scalar",["ok","localization"]]',
    )
    assert broken != payload, "mutation did not apply -- the assertion below would be vacuous"
    restored = rr._capsule_compilation_from_json(broken)
    assert restored.evidence_lineage == (("ok", "localization"),)
