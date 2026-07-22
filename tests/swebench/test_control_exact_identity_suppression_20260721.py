"""RED-first proof for the control exact-identity / mediation-enforcement fixes (2026-07-21).

Three mediator controls declared a decision the referee grader could not bind to the exact
candidate they mediated, so ``mediation_correct`` was either a guaranteed FALSE (a lie the
control could never clear) or an un-gradeable ``None`` (class-level laundering — the withheld
candidate escaped grading entirely because its record carried no byte identity):

  * GT_SS_PROVENANCE        — a fully-suppressed provenance seal recorded ``final_text=""``
                              (empty bytes) → identity-less → the grader DROPS it → None.
  * GT_SS_ARBITER_V2        — the empty-payload guard declared APPLIED then DROPPED the
                              candidate → a guaranteed non-join → permanent mediation_correct
                              FALSE that blocks the whole terminal.
  * GT_GATEWAY_EDIT_BRIDGES — a filtered (SUPPRESSED) bridge candidate recorded empty bytes
                              → identity-less → the withheld candidate escaped grading.

Each test asserts the CURRENT (pre-fix) writer row shape is laundered/false (RED) and the
FIXED writer row shape is exactly enforced by ``_control_declared_effect_correctness`` (GREEN),
plus a biting mutation that flips the verdict. One test drives the actual PROVENANCE writer
(``_record_lane_provenance_control``) end-to-end and asserts it now seals the exact
pre-suppression candidate bytes.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src", ROOT / "scripts" / "swebench", ROOT / "artifact_deepswe"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_feature_metrics as metrics  # noqa: E402
from groundtruth.runtime.evidence_envelope import (  # noqa: E402
    build_observation_binding,
    observation_binding_to_dict,
    observation_candidate_id,
)


# --------------------------------------------------------------------------- #
# minimal builders — the exact fields _control_declared_effect_correctness reads
# --------------------------------------------------------------------------- #
def _mediator_rec(feature_id, decision, *, seal, chars, row_index=0,
                  fact_class="caller_contract", bound=True):
    raw_id = f"{fact_class}:dedup"
    binding = observation_binding_to_dict(build_observation_binding(
        batch_start_iteration=4,
        parent_policy_sha256="1" * 64,
        parent_policy_chars=8,
        action_batch_sha256="2" * 64,
        candidate_ordinal=0,
        candidate_kind=f"gateway.{fact_class}",
        candidate_id=raw_id,
    )) if bound else None
    return {
        "row_index": row_index,
        "brief_row_index": None,
        "feature_id": feature_id,
        "role": "mediator",
        "decision": decision,
        "candidate_sha256_16": seal,
        "candidate_chars": chars,
        "candidate_id": observation_candidate_id(raw_id),
        "fact_class": fact_class,
        "iteration": 4,
        "observation_binding": binding,
    }


def _join(rec):
    return {"row_index": rec["row_index"], "brief_row_index": None}


def _opportunity(rec):
    return {
        "layer": "feature.opportunity",
        "candidate_id": rec["candidate_id"],
        "fact_class": rec["fact_class"],
        "candidate_sha256_16": rec["candidate_sha256_16"],
        "candidate_chars": rec["candidate_chars"],
        "observation_binding": rec["observation_binding"],
    }


def _delivered(seal, chars, rec=None):
    row = {"outcome": "delivered", "content_sha256_16": seal,
           "chars_delivered": chars}
    if rec is not None:
        row.update({
            "iteration": 4,
            "lineage_schema": "gt.feature_lineage.v1",
            "fact_class": rec["fact_class"],
            "candidate_id": rec["candidate_id"],
            "observation_binding": rec["observation_binding"],
        })
    return row


def _grade(rows, records, joins):
    return metrics._control_declared_effect_correctness(rows, records, joins)


def test_unbound_or_wrong_opportunity_never_earns_suppression_credit() -> None:
    direct = _mediator_rec(
        "GT_GATEWAY_EDIT_BRIDGES", "SUPPRESSED", seal="f" * 16,
        chars=7, bound=False)
    assert _grade([], {"GT_GATEWAY_EDIT_BRIDGES": [direct]}, {})[
        "GT_GATEWAY_EDIT_BRIDGES"] is None

    bound = _mediator_rec(
        "GT_GATEWAY_EDIT_BRIDGES", "SUPPRESSED", seal="f" * 16, chars=7)
    wrong_class = dict(_opportunity(bound), fact_class="impact")
    assert _grade([wrong_class], {"GT_GATEWAY_EDIT_BRIDGES": [bound]}, {})[
        "GT_GATEWAY_EDIT_BRIDGES"] is None


def test_actual_gateway_writer_defers_and_commits_final_bound_bytes(monkeypatch) -> None:
    from groundtruth.runtime.gateway import GatewayState, _record_control
    rows: list[dict] = []
    candidate = SimpleNamespace(
        evidence_type="caller_contract", dedup_key="caller_contract:dedup")
    queue: list[dict] = []
    gateway_state = GatewayState(control_recorder=lambda *a, **k: rows.append(k),
                                 control_queue=queue)
    _record_control(
        gateway_state, "GT_GATEWAY_EDIT_BRIDGES",
        "gateway.augment.edit_bridge_candidate", "SUPPRESSED",
        fact_class="caller_contract", candidate_id=candidate.dedup_key,
        reason="candidate_filtered", candidate=candidate)
    assert rows == []

    import gt_mini_patch as gmp
    monkeypatch.setattr(gmp, "_inseam_metrics_on", lambda: True)
    monkeypatch.setattr(gmp, "_ledger_line_direct", lambda row: rows.append(dict(row)))
    state = {
        "batch_start_iteration": 4,
        "parent_policy_sha256": "1" * 64,
        "parent_policy_chars": 8,
        "action_batch_sha256": "2" * 64,
        "pool": [], "opportunity_bindings": {},
        "gateway_control_records": [],
    }
    token = gmp._batch_context.set(state)
    try:
        assert rows == []  # no durable row before formatter commit
        gmp._register_gateway_control_records(
            queue, {"output": "base"},
            lambda _candidate, **_kwargs: "FINAL WOULD SHIP\n", native=False)
        gmp._prepare_batch_opportunity_bindings(state)
        gmp._record_batch_opportunities(state, {}, None)
        gmp._commit_gateway_control_records(state, None)
    finally:
        gmp._batch_context.reset(token)

    control = next(row for row in rows if row.get("layer") == "control.participation")
    opportunity = next(row for row in rows if row.get("layer") == "feature.opportunity")
    expected = "\nFINAL WOULD SHIP\n"
    assert control["candidate_chars"] == len(expected)
    assert control["candidate_sha256_16"] == hashlib.sha256(
        expected.encode("utf-8")).hexdigest()[:16]
    assert control["observation_binding"] == opportunity["observation_binding"]
    assert control["candidate_id"] == opportunity["candidate_id"]
    assert control["fact_class"] == opportunity["fact_class"]


# --------------------------------------------------------------------------- #
# GT_SS_PROVENANCE — the SUPPRESSED seal must be the exact pre-suppression candidate
# --------------------------------------------------------------------------- #
def test_provenance_suppressed_empty_bytes_is_laundered_then_sealed_is_enforced() -> None:
    other = [_delivered("a" * 16, 12)]  # an unrelated delivery; the suppressed bytes are not here

    # RED — old writer sealed final_text="" → chars=0, seal="" → no identity → the grader
    # DROPS the record, so a fully-suppressed provenance decision is UN-GRADEABLE (None):
    # the class-level laundering the audit named.
    old = _mediator_rec("GT_SS_PROVENANCE", "SUPPRESSED", seal="", chars=0)
    assert _grade(other, {"GT_SS_PROVENANCE": [old]}, {})["GT_SS_PROVENANCE"] is None

    # GREEN — new writer seals the pre-suppression candidate bytes. The referee's exact
    # ``not _carried`` absence check now confirms those exact bytes never shipped → True.
    new = _mediator_rec("GT_SS_PROVENANCE", "SUPPRESSED", seal="c" * 16, chars=30)
    assert _grade([_opportunity(new), *other], {"GT_SS_PROVENANCE": [new]}, {})["GT_SS_PROVENANCE"] is True

    # BITING MUTATION — the exact pre-suppression bytes WERE delivered → the suppression is
    # contradicted by the ledger → False (the lie is now exposed, was previously invisible).
    assert _grade([_opportunity(new), _delivered("c" * 16, 30, new)], {"GT_SS_PROVENANCE": [new]},
                  {})["GT_SS_PROVENANCE"] is False


def test_provenance_writer_now_seals_pre_suppression_candidate(monkeypatch) -> None:
    """Drive the ACTUAL writer: SUPPRESSED must seal ``original_text`` (pre-suppression),
    never the empty ``final_text``."""
    import gt_mini_patch as gmp

    captured: list[dict] = []
    monkeypatch.setattr(gmp, "_inseam_metrics_on", lambda: True)
    monkeypatch.setattr(gmp, "_ledger_line_direct", lambda row: captured.append(row))

    pre = "PRE-SUPPRESSION CANDIDATE BYTES that cite a scratch path"
    gmp._record_lane_provenance_control(
        "l3.contract", pre, "", "pkg/mod.py", "SUPPRESSED",
        fact_class="caller_contract", candidate_id="caller_contract:pkg/mod.py::f",
    )
    rows = [r for r in captured if r.get("participation_decision") == "SUPPRESSED"]
    assert rows, f"no SUPPRESSED provenance participation row emitted; got {captured!r}"
    row = rows[0]
    expected_seal = hashlib.sha256(pre.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    # GREEN: the exact pre-suppression candidate is sealed (non-empty, exact) — was "" before.
    assert row["candidate_sha256_16"] == expected_seal
    assert row["candidate_chars"] == len(pre)
    assert row["candidate_sha256_16"] != ""  # RED anchor: the old writer produced exactly this


# --------------------------------------------------------------------------- #
# GT_SS_ARBITER_V2 — the empty-payload guard must not POISON the member with a
# guaranteed-non-join APPLIED; it is an honest SUPPRESSION.
# --------------------------------------------------------------------------- #
def test_arbiter_v2_empty_payload_guard_no_longer_poisons_the_member() -> None:
    # a real winner_preserved NO_EFFECT that JOINED a delivery → contributes True.
    noeff = _mediator_rec("GT_SS_ARBITER_V2", "NO_EFFECT", seal="a" * 16, chars=12, row_index=0)
    rows = [_opportunity(noeff), _delivered("a" * 16, 12, noeff)]
    joins = {"GT_SS_ARBITER_V2": [_join(noeff)]}

    # RED — the old guard declared APPLIED for a candidate it dropped: never joins → False →
    # ANY-False rollup makes the whole member FALSE (a permanent terminal block).
    old_guard = _mediator_rec("GT_SS_ARBITER_V2", "APPLIED", seal="e" * 16, chars=5, row_index=1)
    assert _grade([*rows, _opportunity(old_guard)],
                  {"GT_SS_ARBITER_V2": [noeff, old_guard]}, joins)["GT_SS_ARBITER_V2"] is False

    # GREEN — the fixed guard records SUPPRESSED sealing the exact pre-drop bytes (not
    # delivered) → honest suppression True; the member is no longer poisoned.
    new_guard = _mediator_rec("GT_SS_ARBITER_V2", "SUPPRESSED", seal="e" * 16, chars=5, row_index=1)
    assert _grade([*rows, _opportunity(new_guard)],
                  {"GT_SS_ARBITER_V2": [noeff, new_guard]}, joins)["GT_SS_ARBITER_V2"] is True

    # BITING MUTATION — if the "dropped" bytes were in fact delivered, the suppression is a
    # lie → False (the referee still bites; the fix does not weaken the bar).
    assert _grade([_opportunity(new_guard), _delivered("e" * 16, 5, new_guard)], {"GT_SS_ARBITER_V2": [new_guard]},
                  {})["GT_SS_ARBITER_V2"] is False


# --------------------------------------------------------------------------- #
# GT_GATEWAY_EDIT_BRIDGES — the SUPPRESSED (filtered) arm must seal its exact
# pre-suppression bytes so the withheld candidate is graded, not laundered.
# --------------------------------------------------------------------------- #
def test_edit_bridges_suppressed_arm_sealed_and_gradeable() -> None:
    other = [_delivered("a" * 16, 12)]

    # RED — old writer recorded empty bytes for the filtered candidate → identity-less →
    # dropped → the withheld candidate escapes grading (None).
    old = _mediator_rec("GT_GATEWAY_EDIT_BRIDGES", "SUPPRESSED", seal="", chars=0)
    assert _grade(other, {"GT_GATEWAY_EDIT_BRIDGES": [old]}, {})["GT_GATEWAY_EDIT_BRIDGES"] is None

    # GREEN — new writer seals the exact pre-suppression bytes (not delivered) → enforced True.
    new = _mediator_rec("GT_GATEWAY_EDIT_BRIDGES", "SUPPRESSED", seal="f" * 16, chars=7)
    assert _grade([_opportunity(new), *other], {"GT_GATEWAY_EDIT_BRIDGES": [new]}, {})["GT_GATEWAY_EDIT_BRIDGES"] is True

    # BITING MUTATION — the "filtered" bytes actually shipped → the suppression is a lie → False.
    assert _grade([_opportunity(new), _delivered("f" * 16, 7, new)], {"GT_GATEWAY_EDIT_BRIDGES": [new]},
                  {})["GT_GATEWAY_EDIT_BRIDGES"] is False
