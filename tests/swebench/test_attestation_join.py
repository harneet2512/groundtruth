"""RED-first tests for the producer-attestation → delivered-ledger truth join.

Fixtures are built with the REAL ``producer_attestation`` dataclasses and persisted
with the REAL ``attestation_store.persist_attestation`` (never hand-written JSON), so
the canonical-byte / index-key / sha integrity checks exercise the true bundle shape.

Documented biting mutations (each verified to fail a test, then restored):

  * MUTATION A — join_truth: relax the identity to candidate_id only (drop the
    ``content_sha256_16``/``delivery_seal`` leg). ``test_seal_mismatch_produces_no_join``
    then WRONGLY joins the seal-mismatched row → ``syntax_result`` appears with
    ``truth=True`` instead of being absent. Bite confirmed.

  * MUTATION B — load_attestations: delete the exact canonical-byte integrity check
    (``canonical_bytes(attestation) != raw_attestation``).
    ``test_tampered_bundle_is_rejected_with_reason`` then ACCEPTS the tampered bundle →
    attestations non-empty, no diagnostic. Bite confirmed.

J2b (authority leg) biting mutations (each verified to fail, then restored):

  * MUTATION C — join_truth: drop the truth-PASS guard on the authority leg (compute
    ``authority=True`` on ANY join instead of ``True`` only when ``truth is True``).
    ``test_authority_none_on_truth_fail_join`` /
    ``test_authority_none_on_truth_none_join`` then WRONGLY report ``authority=True`` on a
    FAIL / UNMEASURED join → authority no longer rides only a truth-PASS join. Bite
    confirmed.

  * MUTATION D — gt_feature_metrics._apply_attestation_truth: delete the
    ``authority_valid`` override (leave it at its hard-wired UNMEASURED).
    ``test_collect_task_correct_info_goes_true_for_attested_class`` then keeps
    ``correct_info`` at ``None`` (truth True, authority None) — the gate never moves.
    Bite confirmed.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import attestation_join as aj  # noqa: E402
import gt_feature_metrics as gfm  # noqa: E402
from groundtruth.runtime.attestation_store import persist_attestation  # noqa: E402
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    build_lineage,
    lineage_ledger_extra,
)
from groundtruth.runtime.producer_attestation import (  # noqa: E402
    ATTESTATION_SCHEMA,
    FAIL,
    FRESHNESS,
    PASS,
    UNMEASURED,
    ArtifactRef,
    DecisionBinding,
    PredicateAttestation,
    ProducerAttestation,
    ProofRef,
)


# --------------------------------------------------------------------------- #
# Fixture builders — REAL dataclasses, REAL store.
# --------------------------------------------------------------------------- #
def _syntax_attestation(
    *,
    candidate_id: str,
    delivery_seal: str,
    truth_verdict: str = PASS,
    freshness_verdict: str = PASS,
) -> tuple[ProducerAttestation, dict[str, bytes]]:
    """A valid ``syntax_result`` attestation (edit_check producer) + its artifact bytes.

    ``syntax_result`` is a registered §1 class: producer ``edit_check``, decision
    "is the edit acceptable", deliver_by ``edit_result``.
    """
    source_bytes = b'{"verdict":"syntax_error","line":12}'
    artifact_id = "diagnostic.json"
    ref = ArtifactRef(
        kind="producer_input",
        artifact_id=artifact_id,
        sha256=hashlib.sha256(source_bytes).hexdigest(),
        revision="edit:7",
    )
    proof = ProofRef("producer_observation", ref, "$.verdict")

    def _predicate(kind: str, pid: str, verdict: str) -> PredicateAttestation:
        # A PASS/FAIL predicate REQUIRES proof_refs + a non-empty observation;
        # UNMEASURED forbids proof_refs.
        if verdict in (PASS, FAIL):
            return PredicateAttestation(
                kind, pid, "edit", "acceptable", "observed", verdict, (proof,)
            )
        return PredicateAttestation(kind, pid, "edit", "acceptable", "", verdict, ())

    attestation = ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type="syntax_result",
        runtime_producer_id="edit_check",
        registered_producer_id="edit_check",
        candidate_id=candidate_id,
        delivery_seal=delivery_seal,
        source_artifacts=(ref,),
        truth_predicates=(_predicate("TRUTH", "truth.syntax", truth_verdict),),
        freshness_predicates=(
            _predicate(FRESHNESS, "fresh.syntax", freshness_verdict),
        ),
        decision=DecisionBinding("is the edit acceptable", "edit_result", "edit_result"),
    )
    return attestation, {artifact_id: source_bytes}


def _syntax_lineage_extra() -> dict:
    """The EXACT typed FACT lineage columns the seam stamps on a delivered syntax row
    (``_exact_profile_delivery_extra`` -> ``lineage_ledger_extra`` for edit.syntax)."""
    return lineage_ledger_extra(build_lineage(
        runtime_producer_id="edit_check",
        evidence_type="syntax_result",
        actual_event="edit_result",
    ))


def _delivered_row(candidate_id: str, seal: str, *, chars: int = 36) -> dict:
    """A DELIVERED runtime-ledger row carrying the join identity + typed FACT lineage the
    seam writer stamps (J6: a lineage-less row can no longer seat a truth join)."""
    return {
        "layer": "edit.syntax",
        "event_type": "edit_result",
        "file_path": "src/core.py",
        "outcome": "delivered",
        "reason": "lane_delivery_sealed",
        "chars_delivered": chars,
        "iteration": 5,
        "content_sha256_16": seal,
        "seal_scope": "block",
        "candidate_id": candidate_id,
        **_syntax_lineage_extra(),
    }


def _persist(task_dir: Path, attestation: ProducerAttestation, artifacts: dict) -> Path:
    root = task_dir / "art" / "producer_attestations"
    persist_attestation(attestation, artifacts, root)
    return root


# --------------------------------------------------------------------------- #
# load_attestations
# --------------------------------------------------------------------------- #
def test_load_accepts_a_valid_persisted_bundle(tmp_path: Path) -> None:
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal="a" * 16
    )
    _persist(tmp_path, attestation, artifacts)

    load = aj.load_attestations(str(tmp_path))

    assert len(load.attestations) == 1
    assert load.diagnostics == ()
    assert load.attestations[0].candidate_id == "edit:src/core.py:7"
    assert load.attestations[0].evidence_type == "syntax_result"


def test_load_of_absent_dir_is_empty_not_error(tmp_path: Path) -> None:
    load = aj.load_attestations(str(tmp_path))
    assert load == aj.AttestationLoad()


def test_missing_proof_field_is_unmeasured_even_with_valid_store_bytes(tmp_path: Path) -> None:
    """MUTATION W03-A: dropping semantic field resolution must not admit PASS."""
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:71", delivery_seal="a" * 16
    )
    predicate = replace(
        attestation.truth_predicates[0],
        proof_refs=(replace(attestation.truth_predicates[0].proof_refs[0], field_path="$.missing"),),
    )
    attestation = replace(attestation, truth_predicates=(predicate,))
    _persist(tmp_path, attestation, artifacts)

    load = aj.load_attestations(str(tmp_path))

    assert load.attestations == ()
    assert "semantic:" in load.diagnostics[0]
    assert "field_missing" in load.diagnostics[0]


def test_scalar_observation_contradiction_is_not_authority(tmp_path: Path) -> None:
    """MUTATION W03-B: trusting producer PASS despite a contradictory scalar fails."""
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:72", delivery_seal="b" * 16
    )
    predicate = replace(attestation.truth_predicates[0], observation="FAIL")
    attestation = replace(attestation, truth_predicates=(predicate,))
    _persist(tmp_path, attestation, artifacts)

    load = aj.load_attestations(str(tmp_path))

    assert load.attestations == ()
    assert "observation_contradicts_field" in load.diagnostics[0]


# --------------------------------------------------------------------------- #
# join_truth — the four required cases.
# --------------------------------------------------------------------------- #
def test_valid_join_yields_truth_true(tmp_path: Path) -> None:
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:src/core.py:7", seal)]

    load = aj.load_attestations(str(tmp_path))
    joins = aj.join_truth(load.attestations, rows)

    assert set(joins) == {"syntax_result"}
    tj = joins["syntax_result"]
    assert tj.truth is True
    assert tj.freshness is True
    assert tj.attestation_count == 1
    assert tj.joined_delivery_row_indices == (0,)


def test_fail_predicate_yields_truth_false(tmp_path: Path) -> None:
    seal = "b" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:9", delivery_seal=seal, truth_verdict=FAIL
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:src/core.py:9", seal)]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert joins["syntax_result"].truth is False
    # freshness was PASS and independent — it must NOT inherit the truth FAIL.
    assert joins["syntax_result"].freshness is True


def test_seal_mismatch_produces_no_join(tmp_path: Path) -> None:
    # MUTATION A bites here: a candidate_id-only join would wrongly match this row.
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal="a" * 16
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:src/core.py:7", "f" * 16)]  # seal differs

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert "syntax_result" not in joins
    assert joins == {}


def test_candidate_mismatch_produces_no_join(tmp_path: Path) -> None:
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:OTHER.py:1", seal)]  # candidate differs

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert joins == {}


def test_non_delivered_row_does_not_join(tmp_path: Path) -> None:
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    _persist(tmp_path, attestation, artifacts)
    row = _delivered_row("edit:src/core.py:7", seal)
    row["outcome"] = "suppressed_internal_only"  # not a delivered row
    rows = [row]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert joins == {}


def test_unattested_class_is_untouched(tmp_path: Path) -> None:
    # Only syntax_result is attested here; an unrelated delivered row for another
    # class must never appear in the join dict.
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [
        _delivered_row("edit:src/core.py:7", seal),
        {
            "layer": "l3.contract", "event_type": "file_view",
            "outcome": "delivered", "chars_delivered": 10,
            "content_sha256_16": "c" * 16, "candidate_id": "contract:x",
            "file_path": "src/other.py",
        },
    ]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert set(joins) == {"syntax_result"}
    assert "caller_contract" not in joins


# --------------------------------------------------------------------------- #
# Tampering / malformed bundles — fail closed.
# --------------------------------------------------------------------------- #
def _bundle_dir(root: Path) -> Path:
    (index,) = list((root / "index").iterdir())
    return index


def test_noncanonical_bytes_bundle_is_rejected(tmp_path: Path) -> None:
    # MUTATION B bites here: reformat attestation.json (pretty-print) WITHOUT changing
    # any content. The semantic sha (over canonical form) is unchanged, so only the
    # exact canonical-BYTES check distinguishes it. Removing that check accepts it.
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    root = _persist(tmp_path, attestation, artifacts)
    bundle = _bundle_dir(root)
    payload = json.loads((bundle / "attestation.json").read_bytes())
    # Pretty-print with indentation — different BYTES, identical content/sha.
    (bundle / "attestation.json").write_bytes(
        json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
    )

    load = aj.load_attestations(str(tmp_path))

    assert load.attestations == ()
    assert len(load.diagnostics) == 1
    assert "noncanonical_or_tampered" in load.diagnostics[0]
    # A rejected bundle never joins.
    rows = [_delivered_row("edit:src/core.py:7", seal)]
    assert aj.join_truth(load.attestations, rows) == {}


def test_tampered_content_bundle_is_rejected_with_reason(tmp_path: Path) -> None:
    # A content tamper (a flipped observation string) is caught by the entry sha:
    # the reconstructed canonical sha no longer equals the entry's recorded sha.
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    root = _persist(tmp_path, attestation, artifacts)
    bundle = _bundle_dir(root)
    payload = json.loads((bundle / "attestation.json").read_bytes())
    payload["truth_predicates"][0]["observation"] = "TAMPERED"
    (bundle / "attestation.json").write_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    load = aj.load_attestations(str(tmp_path))

    assert load.attestations == ()
    assert len(load.diagnostics) == 1
    assert "entry_sha_mismatch" in load.diagnostics[0]
    rows = [_delivered_row("edit:src/core.py:7", seal)]
    assert aj.join_truth(load.attestations, rows) == {}


def test_malformed_entry_json_is_skipped_not_crashing(tmp_path: Path) -> None:
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal="a" * 16
    )
    root = _persist(tmp_path, attestation, artifacts)
    bundle = _bundle_dir(root)
    (bundle / "entry.json").write_bytes(b"{ this is not json")

    load = aj.load_attestations(str(tmp_path))

    assert load.attestations == ()
    assert len(load.diagnostics) == 1
    assert "entry_not_json" in load.diagnostics[0]


def test_seal_length_forgery_fails_validation(tmp_path: Path) -> None:
    # A delivery_seal that is not 16 lower-hex is rejected by validate() at load,
    # so it can never reach the join.
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal="a" * 16
    )
    root = _persist(tmp_path, attestation, artifacts)
    bundle = _bundle_dir(root)
    payload = json.loads((bundle / "attestation.json").read_bytes())
    payload["delivery_seal"] = "NOT_HEX"
    (bundle / "attestation.json").write_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    load = aj.load_attestations(str(tmp_path))

    assert load.attestations == ()
    assert len(load.diagnostics) == 1
    assert "invalid:" in load.diagnostics[0]


def test_multiple_attestations_one_fail_aggregates_to_false(tmp_path: Path) -> None:
    seal_ok = "a" * 16
    seal_bad = "b" * 16
    ok, ok_art = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal_ok
    )
    bad, bad_art = _syntax_attestation(
        candidate_id="edit:src/core.py:9", delivery_seal=seal_bad, truth_verdict=FAIL
    )
    _persist(tmp_path, ok, ok_art)
    _persist(tmp_path, bad, bad_art)
    rows = [
        _delivered_row("edit:src/core.py:7", seal_ok),
        _delivered_row("edit:src/core.py:9", seal_bad),
    ]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert joins["syntax_result"].attestation_count == 2
    assert joins["syntax_result"].truth is False  # any FAIL → False


# --------------------------------------------------------------------------- #
# J2b — the authority leg (correct_info's second half).
#
# authority rides ONLY a truth-PASS join: True when (validated ∧ exact-join ∧
# truth=True); None otherwise. It is NEVER False — no producer claims negative
# authority; absence is UNMEASURED (None), not a refutation.
# --------------------------------------------------------------------------- #
def test_authority_true_on_truth_pass_join(tmp_path: Path) -> None:
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:src/core.py:7", seal)]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    tj = joins["syntax_result"]
    assert tj.truth is True
    assert tj.authority is True  # rides the truth-PASS join


def test_authority_none_on_truth_fail_join(tmp_path: Path) -> None:
    # MUTATION C bites here: an unguarded authority leg would report True on a FAIL.
    seal = "b" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:9", delivery_seal=seal, truth_verdict=FAIL
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:src/core.py:9", seal)]

    tj = aj.join_truth(
        aj.load_attestations(str(tmp_path)).attestations, rows
    )["syntax_result"]

    assert tj.truth is False
    assert tj.authority is None  # correct-or-quiet: no authority on a truth-FAIL


def test_authority_none_on_truth_none_join(tmp_path: Path) -> None:
    # MUTATION C also bites here: an UNMEASURED truth aggregates to None; authority
    # must NOT be granted.
    seal = "c" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:11",
        delivery_seal=seal,
        truth_verdict=UNMEASURED,
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:src/core.py:11", seal)]

    tj = aj.join_truth(
        aj.load_attestations(str(tmp_path)).attestations, rows
    )["syntax_result"]

    assert tj.truth is None
    assert tj.authority is None


def test_authority_absent_on_seal_mismatch(tmp_path: Path) -> None:
    # A valid PASS attestation that joins NOTHING (seal differs) grants no authority:
    # authority can never be minted without an exact delivered-row join.
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal="a" * 16
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:src/core.py:7", "f" * 16)]  # seal differs

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert joins == {}  # no join → no TruthJoin → no authority at all


def test_truth_join_to_dict_surfaces_authority(tmp_path: Path) -> None:
    seal = "a" * 16
    attestation, artifacts = _syntax_attestation(
        candidate_id="edit:src/core.py:7", delivery_seal=seal
    )
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("edit:src/core.py:7", seal)]

    tj = aj.join_truth(
        aj.load_attestations(str(tmp_path)).attestations, rows
    )["syntax_result"]
    projected = aj.truth_join_to_dict(tj)

    assert projected["truth"] is True
    assert projected["authority"] is True  # the leg is surfaced in the sidecar


# --------------------------------------------------------------------------- #
# J2b — end-to-end through collect_task: the gate finally MOVES.
# --------------------------------------------------------------------------- #
def _write_trajectory(task_dir: Path) -> None:
    (task_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({
            "messages": [{"role": "user", "content": "fixture task"}],
            "info": {"submission": ""},
            "trajectory_format": "mini-swe-agent",
        }),
        encoding="utf-8",
    )


def _write_ledger(task_dir: Path, rows: list[dict]) -> None:
    ledger = task_dir / "gt_runtime_ledger_synthetic.jsonl"
    ledger.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def test_collect_task_correct_info_goes_true_for_attested_class(tmp_path: Path) -> None:
    # THE load-bearing test: a valid PASS bundle + a matching DELIVERED row drives
    # correct_info (truth ∧ authority) to True for the attested class. Before J2b the
    # authority leg was hard-wired UNMEASURED, so this gate could NEVER be True.
    # MUTATION D (remove the authority override) keeps correct_info at None.
    seal = "a" * 16
    candidate = "edit:src/core.py:7"
    attestation, artifacts = _syntax_attestation(
        candidate_id=candidate, delivery_seal=seal
    )
    _persist(tmp_path, attestation, artifacts)
    _write_trajectory(tmp_path)
    _write_ledger(tmp_path, [_delivered_row(candidate, seal)])

    record = gfm.collect_task("synthetic__attested-pass", str(tmp_path), profile="2")

    fc = record["fact_classes"]["syntax_result"]
    assert fc["truth_valid"]["value"] is True
    assert fc["authority_valid"]["value"] is True  # J2b: the second leg is measured

    readiness = record["ss_features"]["syntax_result"]["ss_readiness"]
    assert readiness["gates"]["correct_info"] is True  # the gate MOVES

    diag = record["ss_integrity"]["attestation_join"]
    assert diag["applied_truth_overrides"] == ["syntax_result"]
    assert diag["applied_authority_overrides"] == ["syntax_result"]
    assert diag["joined_fact_classes"]["syntax_result"]["authority"] is True


def test_collect_task_authority_is_join_gated_not_blanket(tmp_path: Path) -> None:
    # The unattested-untouched pin, downstream: ONLY the joined attested class receives
    # authority. Every other fact class — including attested-but-unjoined classes
    # (covering_red/caller_contract/signature_delta here) and every unattested class —
    # keeps its honest UNMEASURED authority (never True).
    seal = "a" * 16
    candidate = "edit:src/core.py:7"
    attestation, artifacts = _syntax_attestation(
        candidate_id=candidate, delivery_seal=seal
    )
    _persist(tmp_path, attestation, artifacts)
    _write_trajectory(tmp_path)
    _write_ledger(tmp_path, [_delivered_row(candidate, seal)])

    record = gfm.collect_task("synthetic__join-gated", str(tmp_path), profile="2")

    for fact_class, lifecycle in record["fact_classes"].items():
        if fact_class == "syntax_result":
            continue
        assert lifecycle["authority_valid"]["value"] is not True, fact_class

    # covering_red is an ATTESTED class, yet unjoined here → still UNMEASURED, proving
    # authority is join-gated, not granted by attested-set membership.
    assert record["fact_classes"]["covering_red"]["authority_valid"]["value"] is None
    assert record["ss_integrity"]["attestation_join"]["applied_authority_overrides"] == [
        "syntax_result"
    ]


# --------------------------------------------------------------------------- #
# INDEXED PROOF-PATH DIALECT (2026-07-29) — the second reader/writer mismatch of the
# ``bytes[0:N]`` shape.
#
# ``gateway_attestation_factory.py:562-568`` emits per-row source bindings on the
# freshness predicate of EVERY caller_break / signature_mismatch attestation:
#     $.caller_rows[0].source_state.sha256
#     $.caller_rows[0].source_state.revision
#     $.caller_usage_rows[0].source_revision
# ``_json_field`` split only on "." and did dict lookups, so ``caller_rows[0]`` resolved
# to ``field_missing`` — which rejects the WHOLE bundle. Every caller_break /
# signature_mismatch attestation carrying at least one caller row was therefore
# structurally unable to validate: ``caller_contract`` / ``signature_delta`` could never
# earn truth/authority on a real run, and no reader could reach their structured inputs.
#
# MUTATION (verified RED, then restored): drop the indexed branch from ``_json_field``.
# ``test_caller_break_bundle_with_indexed_proof_paths_validates`` then reports the
# ``predicate[1].proof[4]:field_missing:$.caller_rows[0].source_state.revision``
# diagnostic and zero validated attestations.
# --------------------------------------------------------------------------- #
def _caller_break_bundle():
    import dataclasses

    from groundtruth.runtime.evidence_envelope import EvidenceEnvelope
    from groundtruth.runtime.gateway_attestation_factory import build_gateway_attestation
    from groundtruth.runtime.producer_inputs import (
        PRODUCER_INPUTS_SCHEMA,
        CallerEvidenceRow,
        ProducerInputs,
        SignatureChange,
        SourceState,
    )

    def source(file: str, token: str) -> SourceState:
        return SourceState(file=file, sha256=token * 64, revision="source:" + token * 64)

    env = EvidenceEnvelope.build(
        producer="caller_contract", fact_id="get_user", target="src/api.py",
        evidence_type="caller_break",
        payload=("get_user() signature changed - callers must update the call sites",),
        provenance=(("src/caller.py", 12),), confidence=0.95, tier="WARNING",
        graph_revision="graph-9", preferred_event="edit",
    )
    inputs = ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA, evidence_type="caller_break",
        candidate_id=env.dedup_key,
        before_state=source("src/api.py", "a"), after_state=source("src/api.py", "b"),
        caller_rows=(CallerEvidenceRow(
            identity="render_template", file="src/caller.py", line=12, confidence=0.95,
            resolution_method="import", source_state=source("src/caller.py", "c"),
            edge_id=11, definition_id=4,
        ),),
        graph_revision="graph-9",
        signature_changes=(SignatureChange(
            symbol="get_user", edited_file="src/api.py",
            before_parameters=("uid",), after_parameters=("uid", "name"),
            old_min_params=None, old_max_params=None,
            new_min_params=None, new_max_params=None, positional_args=None,
        ),),
    )
    shipped = b"get_user() signature changed - callers must update the call sites\n"
    return build_gateway_attestation(
        dataclasses.replace(env, producer_inputs=inputs),
        delivery_seal=hashlib.sha256(shipped).hexdigest()[:16],
        shipped_bytes=shipped, actual_event="edit_result", open_event="edit_result",
    )


def test_caller_break_bundle_with_indexed_proof_paths_validates(tmp_path: Path) -> None:
    """A REAL caller_break bundle, built and persisted by the real factory/store, must
    survive ``load_attestations`` — the indexed proof paths are a dialect, not a defect."""
    attestation, artifacts = _caller_break_bundle()
    _persist(tmp_path, attestation, artifacts)

    load = aj.load_attestations(str(tmp_path))
    assert load.diagnostics == (), load.diagnostics
    assert len(load.attestations) == 1
    assert load.attestations[0].evidence_type == "caller_break"


def test_indexed_proof_path_resolution_is_fail_closed() -> None:
    """The dialect resolves a real element and FAILS CLOSED on anything absent —
    out-of-range, a non-list container, and a missing key are all not-found."""
    document = {"caller_rows": [{"source_state": {"revision": "r1"}}], "scalar": 3}
    assert aj._json_field(document, "$.caller_rows[0].source_state.revision") == (True, "r1")
    assert aj._json_field(document, "$.caller_rows[1].source_state.revision") == (False, None)
    assert aj._json_field(document, "$.scalar[0]") == (False, None)
    assert aj._json_field(document, "$.absent[0].x") == (False, None)
    assert aj._json_field(document, "$.caller_rows[0].missing") == (False, None)
