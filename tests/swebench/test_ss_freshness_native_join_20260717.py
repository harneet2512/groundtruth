"""RED-first tests for the SS-LIVE offline join defects (run #2 shapes).

Two defects the run-#2 audit named, both in the OFFLINE metrics/join layer (no
model-visible bytes — the S10 byte-identical invariant is untouched by construction):

DEFECT 4 — freshness discarded.
    ``attestation_join.TruthJoin`` carries a FRESHNESS verdict, but
    ``gt_feature_metrics._apply_attestation_truth`` applied only truth/authority and
    THREW THE FRESHNESS AWAY. A joined freshness FAIL must drive the gate-relevant
    ``stale`` lifecycle field True (→ ``correct_rl_adhered_time`` gate False); a joined
    freshness PASS must set it False with attestation provenance; an absent/UNMEASURED
    freshness must leave the honest ledger-derived value untouched.

DEFECT 5 — native form asserted, not proven.
    ``fact_class_lifecycle`` set ``native_valid = measured(True)`` on MERE DELIVERY.
    Replaced with an exact registry-renderer audit: a delivered row is native-valid only
    when its ``renderer_id`` proves a native-channel render (native/lane, NOT a bespoke
    ``tagged`` <gt-*> tag) AND its ``evidence_type`` resolves to a registered class with a
    required native renderer. A tagged render is FALSE; a render-identity-less row is
    UNMEASURED (never a fabricated True).

Documented biting mutations (each verified to fail a test, then restored):

  * MUTATION F1 (defect 4) — _apply_attestation_truth: delete the freshness override.
    ``test_freshness_fail_join_marks_stale_and_fails_time_gate`` then keeps ``stale``
    at its ledger default (False) and the ``correct_rl_adhered_time`` gate does not go
    False. Bite confirmed.

  * MUTATION F2 (defect 4) — apply freshness with the WRONG polarity
    (``measured(tj.freshness)`` instead of ``measured(not tj.freshness)``).
    ``test_freshness_pass_join_marks_not_stale`` then reports a fresh fact as stale.
    Bite confirmed.

  * MUTATION N1 (defect 5) — native_renderer_audit_by_fact_class: treat a ``tagged``
    render as native (drop the ``renderer_id in NATIVE_IDS`` clause).
    ``test_tagged_render_is_not_native_valid`` then wrongly reports native_valid True for
    a bespoke tag. Bite confirmed.

  * MUTATION N2 (defect 5) — fact_class_lifecycle: restore the fabricated
    ``measured(True)`` on delivery. ``test_native_valid_unmeasured_without_renderer_id``
    then reports True for a render-identity-less delivery. Bite confirmed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

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
    ArtifactRef,
    DecisionBinding,
    PredicateAttestation,
    ProducerAttestation,
    ProofRef,
)


# --------------------------------------------------------------------------- #
# Fixtures — REAL attestation dataclasses + REAL store (mirrors test_attestation_join).
# --------------------------------------------------------------------------- #
def _syntax_attestation(
    *, candidate_id: str, delivery_seal: str,
    truth_verdict: str = PASS, freshness_verdict: str = PASS,
) -> tuple[ProducerAttestation, dict[str, bytes]]:
    source_bytes = b'{"verdict":"syntax_error","line":12}'
    artifact_id = "diagnostic.json"
    ref = ArtifactRef(
        kind="producer_input", artifact_id=artifact_id,
        sha256=__import__("hashlib").sha256(source_bytes).hexdigest(), revision="edit:7",
    )
    proof = ProofRef("producer_observation", ref, "$.verdict")

    def _pred(kind: str, pid: str, verdict: str) -> PredicateAttestation:
        if verdict in (PASS, FAIL):
            return PredicateAttestation(kind, pid, "edit", "acceptable", "obs", verdict, (proof,))
        return PredicateAttestation(kind, pid, "edit", "acceptable", "", verdict, ())

    attestation = ProducerAttestation(
        schema=ATTESTATION_SCHEMA,
        evidence_type="syntax_result",
        runtime_producer_id="edit_check",
        registered_producer_id="edit_check",
        candidate_id=candidate_id,
        delivery_seal=delivery_seal,
        source_artifacts=(ref,),
        truth_predicates=(_pred("TRUTH", "truth.syntax", truth_verdict),),
        freshness_predicates=(_pred(FRESHNESS, "fresh.syntax", freshness_verdict),),
        decision=DecisionBinding("is the edit acceptable", "edit_result", "edit_result"),
    )
    return attestation, {artifact_id: source_bytes}


def _delivered_row(candidate_id: str, seal: str, *, chars: int = 36) -> dict:
    return {
        "layer": "edit.syntax", "event_type": "edit_result", "file_path": "src/core.py",
        "outcome": "delivered", "reason": "lane_delivery_sealed", "chars_delivered": chars,
        "iteration": 5, "content_sha256_16": seal, "seal_scope": "block",
        "candidate_id": candidate_id,
        # J6: the real seam stamps typed FACT lineage on the delivered syntax row.
        **lineage_ledger_extra(build_lineage(
            runtime_producer_id="edit_check", evidence_type="syntax_result",
            actual_event="edit_result")),
    }


def _persist(task_dir: Path, attestation: ProducerAttestation, artifacts: dict) -> None:
    persist_attestation(attestation, artifacts, task_dir / "art" / "producer_attestations")


def _write_trajectory(task_dir: Path) -> None:
    (task_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({
            "messages": [{"role": "user", "content": "fixture task"}],
            "info": {"submission": ""}, "trajectory_format": "mini-swe-agent",
        }), encoding="utf-8",
    )


def _write_ledger(task_dir: Path, rows: list[dict]) -> None:
    (task_dir / "gt_runtime_ledger_synthetic.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# DEFECT 4 — the freshness verdict is applied to the ``stale`` gate leg.
# --------------------------------------------------------------------------- #
def test_freshness_fail_join_marks_stale_and_fails_time_gate(tmp_path: Path) -> None:
    # MUTATION F1 bites here: with the freshness override removed, stale stays False and
    # the correct_rl_adhered_time gate never goes False on a freshness-FAIL attestation.
    seal, candidate = "a" * 16, "edit:src/core.py:7"
    attestation, artifacts = _syntax_attestation(
        candidate_id=candidate, delivery_seal=seal, freshness_verdict=FAIL,
    )
    _persist(tmp_path, attestation, artifacts)
    _write_trajectory(tmp_path)
    _write_ledger(tmp_path, [_delivered_row(candidate, seal)])

    record = gfm.collect_task("synthetic__fresh-fail", str(tmp_path), profile="2")

    fc = record["fact_classes"]["syntax_result"]
    assert fc["stale"]["status"] == "MEASURED"
    assert fc["stale"]["value"] is True  # freshness FAIL → stale
    assert fc["stale"]["source_artifact"] == "producer_attestations"
    assert record["ss_features"]["syntax_result"]["ss_readiness"]["gates"][
        "correct_rl_adhered_time"
    ] is False
    diag = record["ss_integrity"]["attestation_join"]
    assert diag["applied_freshness_overrides"] == ["syntax_result"]


def test_freshness_pass_join_marks_not_stale(tmp_path: Path) -> None:
    # MUTATION F2 bites here: wrong polarity reports a fresh fact as stale.
    seal, candidate = "b" * 16, "edit:src/core.py:9"
    attestation, artifacts = _syntax_attestation(
        candidate_id=candidate, delivery_seal=seal, freshness_verdict=PASS,
    )
    _persist(tmp_path, attestation, artifacts)
    _write_trajectory(tmp_path)
    _write_ledger(tmp_path, [_delivered_row(candidate, seal)])

    record = gfm.collect_task("synthetic__fresh-pass", str(tmp_path), profile="2")

    fc = record["fact_classes"]["syntax_result"]
    assert fc["stale"]["value"] is False
    assert fc["stale"]["source_artifact"] == "producer_attestations"


def test_absent_freshness_join_leaves_stale_untouched(tmp_path: Path) -> None:
    # An UNMEASURED freshness (no bool from the join) must NOT overwrite the honest
    # ledger-derived stale value. Here no attestation is persisted at all → no join.
    seal, candidate = "c" * 16, "edit:src/core.py:11"
    _write_trajectory(tmp_path)
    _write_ledger(tmp_path, [_delivered_row(candidate, seal)])

    record = gfm.collect_task("synthetic__fresh-absent", str(tmp_path), profile="2")

    fc = record["fact_classes"]["syntax_result"]
    # ledger-derived: reason has no "stale" → False, provenance is the LEDGER not the store.
    assert fc["stale"]["value"] is False
    assert fc["stale"]["source_artifact"] != "producer_attestations"
    assert record["ss_integrity"]["attestation_join"]["applied_freshness_overrides"] == []


# --------------------------------------------------------------------------- #
# DEFECT 5 — native_valid is an exact registry-renderer audit, never a fabricated True.
# --------------------------------------------------------------------------- #
def _gateway_row(evidence_type: str, *, renderer_id: str | None, seal: str) -> dict:
    row = {
        "layer": "gateway." + evidence_type, "event_type": "edit_result",
        "file_path": "src/x.py", "outcome": "delivered", "reason": "gateway_sealed",
        "chars_delivered": 40, "iteration": 3, "content_sha256_16": seal,
        "seal_scope": "block", "candidate_id": "gw:" + seal, "evidence_type": evidence_type,
    }
    if renderer_id is not None:
        row["renderer_id"] = renderer_id
    return row


def test_native_render_is_native_valid() -> None:
    rows = [_gateway_row("signature_mismatch", renderer_id="native", seal="a" * 16)]
    audit = gfm.native_renderer_audit_by_fact_class(rows)
    assert audit["signature_delta"] is True


def test_tagged_render_is_not_native_valid() -> None:
    # MUTATION N1 bites here: a bespoke tag is NOT the registry native form.
    rows = [_gateway_row("signature_mismatch", renderer_id="tagged", seal="b" * 16)]
    audit = gfm.native_renderer_audit_by_fact_class(rows)
    assert audit["signature_delta"] is False


def test_render_identity_less_row_is_absent_from_audit() -> None:
    rows = [_gateway_row("signature_mismatch", renderer_id=None, seal="c" * 16)]
    audit = gfm.native_renderer_audit_by_fact_class(rows)
    assert "signature_delta" not in audit  # UNMEASURED — no render identity to audit


def test_any_tagged_row_makes_the_class_false() -> None:
    rows = [
        _gateway_row("signature_mismatch", renderer_id="native", seal="d" * 16),
        _gateway_row("signature_mismatch", renderer_id="tagged", seal="e" * 16),
    ]
    audit = gfm.native_renderer_audit_by_fact_class(rows)
    assert audit["signature_delta"] is False  # one tagged render taints the class


def test_native_valid_unmeasured_without_renderer_id(tmp_path: Path) -> None:
    # MUTATION N2 bites here: the fabricated measured(True) would report True on a
    # delivery whose row carries NO renderer identity. The honest audit leaves it
    # UNMEASURED (fail-closed).
    seal, candidate = "a" * 16, "edit:src/core.py:7"
    attestation, artifacts = _syntax_attestation(candidate_id=candidate, delivery_seal=seal)
    _persist(tmp_path, attestation, artifacts)
    _write_trajectory(tmp_path)
    _write_ledger(tmp_path, [_delivered_row(candidate, seal)])  # no renderer_id column

    record = gfm.collect_task("synthetic__native-unmeasured", str(tmp_path), profile="2")

    fc = record["fact_classes"]["syntax_result"]
    assert fc["delivered"]["value"] is True
    assert fc["native_valid"]["status"] == "UNMEASURED"
    assert fc["native_valid"]["value"] is None


def test_native_valid_measured_true_with_native_renderer_id(tmp_path: Path) -> None:
    # A delivered row that carries renderer_id='lane' (native channel) for a registered
    # class is provably native-valid.
    seal, candidate = "a" * 16, "edit:src/core.py:7"
    attestation, artifacts = _syntax_attestation(candidate_id=candidate, delivery_seal=seal)
    _persist(tmp_path, attestation, artifacts)
    _write_trajectory(tmp_path)
    row = _delivered_row(candidate, seal)
    row["renderer_id"] = "lane"
    row["evidence_type"] = "syntax_result"
    _write_ledger(tmp_path, [row])

    record = gfm.collect_task("synthetic__native-true", str(tmp_path), profile="2")

    fc = record["fact_classes"]["syntax_result"]
    assert fc["native_valid"]["status"] == "MEASURED"
    assert fc["native_valid"]["value"] is True
