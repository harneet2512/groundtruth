"""#41 hole 1 — the capsule must carry the CAP byte-owner attribution it delivers.

`_member_delivery_byte_proven` (gt_feature_metrics.py:3635-3703) proves a byte owner ONLY through
`feature_ids` (typed_lineage) or `profile_member` (exact_profile_member) on a delivered row. The
canonical delivery row carries NEITHER, and no script reads `EvidenceRecord.owner_feature_ids`.
So on the canonical route — the intended production posture — all 7 CAP byte-owner rows are
unprovable REGARDLESS of whether their producers fired.

SCOPE, verified rather than assumed: this closes it for the FOUR `typed_lineage` CAPs, whose
identity really is authorized and reaches `owner_feature_ids`:

    GT_CHANGE_SURFACE · GT_LOC_RESLOT · GT_PATCH_DELTA · GT_SS_SUBMIT_RED

The other three (GT_CERT_DELIVERY, GT_EDIT_CHECK, GT_HYPOTHESIS) use `exact_profile_member`, and
`build_lineage` deliberately REFUSES to mint a CAP ref for them — they carry no FACT lineage by
product decision. Stamping owners cannot help them and would fabricate an authorization the
mechanism withholds on purpose. They are a separate hole with a separate fix.

WHY THE OWNERS AND NOT A FLAG: `_authorized_cap_byte_owners` (reasoning_runtime.py:4789-4815)
returns a CAP id only when the lineage carries an explicit byte_owner ref AND the mechanism's
bindings include this fact class. It is the already-validated authority, not an inference — the
codebase forbids inferring a feature from a flag, a layer name, or an artifact's existence.

BITING MUTATIONS (applied, observed RED, reverted by targeted restore):
  M1 — populate the owners from `feature_id` instead of `owner_feature_ids`: the CAP column would
       carry a FACT class name, and `test_owners_are_the_authorized_cap_ids` goes RED.
  M2 — drop owners from the journal round-trip: `test_owners_survive_the_journal_round_trip` goes
       RED, and a replayed capsule silently loses its byte-owner attribution.
"""

from __future__ import annotations

from groundtruth.runtime import reasoning_runtime as rr


def _compilation(**over):
    fields = {
        "state": rr.CapsuleCompilationState.COMPILED,
        "native_observation": "obs",
        "decision_context": rr.DecisionContext.SOURCE_TARGET_SELECTION,
        "observation_id": "observation-1",
        "source_model_call_id": "call-0",
        "model_call_id": "call-1",
        "evidence_ids": ("GT-E-ac032ea694307691-gdeadbeef",),
        "evidence_lineage": (
            ("ac032ea694307691", "localization", ("GT_LOC_RESLOT",)),
        ),
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
    fields.update(over)
    return rr.CapsuleCompilation(**fields)


def test_lineage_entries_carry_candidate_class_and_cap_owners() -> None:
    compilation = _compilation()
    candidate_id, fact_class, owners = compilation.evidence_lineage[0]
    assert candidate_id == "ac032ea694307691"
    assert fact_class == "localization"
    assert owners == ("GT_LOC_RESLOT",)


def test_owners_normalize_to_a_tuple_of_str() -> None:
    """The JSON shape is lists-of-lists; a frozen record must not keep them as lists."""
    compilation = _compilation(
        evidence_lineage=[["cand", "localization", ["GT_LOC_RESLOT"]]]
    )
    assert compilation.evidence_lineage == (("cand", "localization", ("GT_LOC_RESLOT",)),)


def test_an_evidence_with_no_authorized_owner_carries_an_empty_tuple() -> None:
    """Most evidence owns no CAP bytes. Absence must be empty, never a guessed owner."""
    compilation = _compilation(
        evidence_lineage=(("cand", "obligations", ()),)
    )
    assert compilation.evidence_lineage[0][2] == ()


def test_owners_survive_the_journal_round_trip() -> None:
    """M2. A dropped field is invisible until a REPLAYED capsule loses its attribution."""
    original = _compilation()
    restored = rr._capsule_compilation_from_json(rr._canonical_json(original))
    assert restored.evidence_lineage == original.evidence_lineage
    assert restored == original


def test_malformed_entries_are_skipped_not_coerced() -> None:
    payload = rr._canonical_json(_compilation())
    broken = payload.replace(
        '[["ac032ea694307691","localization",["GT_LOC_RESLOT"]]]',
        '[["only-two","localization"],["a","b","c","d"],"scalar",'
        '["ok","localization",["GT_PATCH_DELTA"]]]',
    )
    assert broken != payload, "mutation did not apply -- the assertion below would be vacuous"
    restored = rr._capsule_compilation_from_json(broken)
    assert restored.evidence_lineage == (("ok", "localization", ("GT_PATCH_DELTA",)),)
