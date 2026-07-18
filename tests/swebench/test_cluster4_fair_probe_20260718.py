"""Cluster-4 — Gate-7 fair-probe STRENGTHENING (predecision pairing · precommit-use ·
registry-grounded control · the safe one-decision fork · the CAUSAL_PAIRED gate rejection).

RED-first, artifact-first. Each invariant carries >=1 BITING MUTATION comment — the exact source
change that re-reddens it — so the suite proves behavior, not the happy path. The fixtures are the
same synthetic shapes gt_performance_metrics._parse_timeline / consumption_ledger._emitted_commands
read (mirrors test_fair_probe_result / test_chronology_extract).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from groundtruth.runtime.chronological_adjudication import (  # noqa: E402
    CAUSAL,
    LATE,
    ON_TIME,
    Chronology,
)
from groundtruth.runtime.lane_attestation import lane_delivery_candidate_id  # noqa: E402
from groundtruth.runtime.syntax_observation import build_syntax_observation  # noqa: E402
from chronology_extract import ExtractedChronology, extract_chronologies  # noqa: E402
from fair_probe_result import (  # noqa: E402
    CAUSAL_FORK,
    CAUSAL_PAIRED,
    ProbeResult,
    _treatment_acted,
    compute_matched_probes,
    fair_probe_bool_by_fact_class,
    join_fair_probes,
)


# --------------------------------------------------------------------------- #
# fixture builders
# --------------------------------------------------------------------------- #
def _seal(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _canon(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _assistant(command: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"function": {"name": "bash", "arguments": json.dumps({"command": command})}}
        ],
    }


def _tool(content: str) -> dict:
    return {"role": "tool", "content": content}


def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _delivered_row(seal_text: str, *, evidence_type: str, event_type: str, file_path: str) -> dict:
    return {
        "layer": "gateway." + evidence_type,
        "evidence_type": evidence_type,
        "event_type": event_type,
        "file_path": file_path,
        "outcome": "delivered",
        "reason": "delivery",
        "chars_delivered": len(seal_text),
        "content_sha256_16": _seal(seal_text),
    }


def _ack_row(seal_text: str, iteration: int) -> dict:
    return {
        "layer": "ss.ack",
        "event_type": "ack",
        "reason": "ss_ack",
        "outcome": "ack",
        "chars_delivered": 0,
        "iteration": iteration,
        "content_sha256_16": _seal(seal_text),
    }


def _holdout_row(withheld_text: str, *, fact_class: str, file_path: str, iteration: int) -> dict:
    return {
        "layer": "gateway.signature_mismatch",
        "outcome": "shadow_holdout",
        "reason": "ss_shadow_holdout",
        "chars_delivered": 0,
        "content_sha256_16": _seal(withheld_text),
        "file_path": file_path,
        "iteration": iteration,
        "fact_class": fact_class,
        "dedup_key": "k:" + file_path,
        "chars_would": len(withheld_text),
        "shadow_rate": "0.5",
    }


_PAYLOAD_A = "Your edit to src/foo.py changed a signature.\ndef compute_widget(x, y):"
_PAYLOAD_B = "Your SECOND edit to src/foo.py changed a signature.\ndef compute_widget(x, y, z):"


# =========================================================================== #
# B1 — ASSIGNMENT KEYED BY PRE-DECISION STATE: same class, DIFFERENT state cannot pair.
# =========================================================================== #
def test_same_class_different_predecision_state_cannot_pair() -> None:
    # Treatment is delivered into the SECOND edit boundary (msg 6); the withheld control sits at
    # the FIRST edit boundary (msg 2). Both are signature_delta and the control window is a clean
    # "not_acted" — under the OLD bare-fact_class join this would be CAUSAL. Under B1 the two arms
    # have DIFFERENT pre-decision states (different decision-open index -> different observation
    # prefix), so they do NOT pair: no MatchedProbe is bound and the verdict is NOT CAUSAL.
    messages = [
        _user("Fix the signature bug."),               # 0 task_start
        _assistant("apply_patch src/foo.py"),           # 1 is_edit -> boundary 2
        _tool(_PAYLOAD_A),                              # 2 edit_result boundary=2; WITHHOLD (iter1) — STATE 1
        _assistant("cat notes.txt"),                    # 3 inspected control decision (names nothing)
        _tool("notes"),                                 # 4 file_view boundary=4 (closes control window)
        _assistant("apply_patch src/foo.py"),           # 5 is_edit -> boundary 6
        _tool(_PAYLOAD_B),                              # 6 edit_result boundary=6 + DELIVERY — STATE 2
        _assistant("apply_patch src/foo.py"),           # 7 treatment mutation -> commit/action
        _tool("edit applied; ack"),                     # 8 ordinal4 -> ack
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_B, evidence_type="signature_mismatch",
            event_type="edit_result", file_path="src/foo.py",
        ),
        _ack_row(_PAYLOAD_B, iteration=4),
        _holdout_row(
            "withheld signature fact for other",
            fact_class="signature_delta", file_path="src/other.py", iteration=1,
        ),
    ]
    traj = {"messages": messages}
    chron = extract_chronologies(traj, rows)
    probes = compute_matched_probes(traj, rows, chron)
    assert len(probes) == 1
    p = probes[0]
    # the control is a clean not_acted, yet the pre-decision states differ -> no pairing.
    assert p.control_outcome == "not_acted"
    assert p.matched_probe is None
    assert p.artifacts["assignment"]["paired"] is False
    assert (
        p.artifacts["assignment"]["treatment_predecision_state_id"]
        != p.artifacts["assignment"]["control_predecision_state_id"]
    )
    assert p.causal_verdict != CAUSAL
    join = join_fair_probes(traj, rows)
    assert fair_probe_bool_by_fact_class(join).get("signature_delta") is not True
    # MUTATION[in compute_matched_probes force `paired = True`] -> the mismatched arms pair, a
    # MatchedProbe binds, and this probe becomes CAUSAL -> RED.


def test_same_class_same_predecision_state_pairs_and_is_causal() -> None:
    # The positive control for B1: when the withheld control sits at the SAME edit boundary as the
    # delivery (same decision-open, byte-identical prefix), the arms pair and the probe is CAUSAL.
    messages = [
        _user("Fix the signature bug."),               # 0 task_start
        _assistant("apply_patch src/foo.py"),           # 1 is_edit -> boundary 2
        _tool(_PAYLOAD_A),                              # 2 edit_result boundary=2 + DELIVERY; WITHHOLD (iter1)
        _assistant("cat notes.txt"),                    # 3 inspected control decision (names nothing)
        _tool("notes"),                                 # 4 file_view boundary=4 (closes control window)
        _assistant("apply_patch src/foo.py"),           # 5 treatment mutation -> commit/action
        _tool("edit applied; ack"),                     # 6 ordinal3 -> ack
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_A, evidence_type="signature_mismatch",
            event_type="edit_result", file_path="src/foo.py",
        ),
        _ack_row(_PAYLOAD_A, iteration=3),
        _holdout_row(
            "withheld signature fact for other",
            fact_class="signature_delta", file_path="src/other.py", iteration=1,
        ),
    ]
    traj = {"messages": messages}
    chron = extract_chronologies(traj, rows)
    probes = compute_matched_probes(traj, rows, chron)
    assert len(probes) == 1
    p = probes[0]
    assert p.control_outcome == "not_acted"
    assert p.matched_probe is not None
    assert p.artifacts["assignment"]["paired"] is True
    # the assignment_unit_id IS the shared pre-decision identity (never a bare fact_class).
    assert p.matched_probe.assignment_unit_id == p.artifacts["assignment"]["treatment_predecision_state_id"]
    assert p.matched_probe.assignment_unit_id != "signature_delta"
    assert p.causal_verdict == CAUSAL


def test_control_self_acquired_before_withhold_invalidates() -> None:
    # B3 guard: the withheld control file (src/other.py) was grepped BEFORE the withholding point,
    # so the agent already had the fact — withholding it cost nothing and the control is
    # contaminated. Even though the in-window receipt is False (no mutation on it in the window),
    # the prior self-acquisition marks the control "acted" -> the probe is INVALID for CAUSAL.
    messages = [
        _user("Fix the signature bug."),               # 0 task_start
        _assistant("grep -rn other src/other.py"),      # 1 is_search naming src/other.py (self-acquire)
        _tool("found other"),                           # 2 search_result boundary; ordinal1
        _assistant("apply_patch src/foo.py"),           # 3 is_edit -> boundary 4
        _tool(_PAYLOAD_A),                              # 4 edit_result boundary=4 + DELIVERY; WITHHOLD(iter2)
        _assistant("cat notes.txt"),                    # 5 inspected control decision (no mutation on other)
        _tool("notes"),                                 # 6 file_view boundary; ordinal3 (closes window)
        _assistant("apply_patch src/foo.py"),           # 7 treatment mutation -> commit/action
        _tool("edit applied; ack"),                     # 8 ordinal4 -> ack
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_A, evidence_type="signature_mismatch",
            event_type="edit_result", file_path="src/foo.py",
        ),
        _ack_row(_PAYLOAD_A, iteration=4),
        _holdout_row(
            "withheld signature fact for other",
            fact_class="signature_delta", file_path="src/other.py", iteration=2,
        ),
    ]
    traj = {"messages": messages}
    chron = extract_chronologies(traj, rows)
    probes = compute_matched_probes(traj, rows, chron)
    assert len(probes) == 1
    p = probes[0]
    assert p.artifacts["assignment"]["paired"] is True   # arms share the pre-decision state
    assert p.control_outcome == "acted"                  # but the control self-acquired earlier
    assert p.causal_verdict != CAUSAL
    # MUTATION[drop `or self_acquired` in _control_outcome] -> the in-window receipt is False so
    # the contaminated control reads not_acted and the probe becomes CAUSAL -> RED.


# =========================================================================== #
# B2 — TREATMENT 'acted' = CLUSTER-3-PROVEN PRECOMMIT USE (direct unit tests).
# =========================================================================== #
def _ec(
    *,
    evidence_type: str,
    timing: str,
    decision_open: int | None,
    delivery: int | None,
    decision_commit: int | None,
    action: int | None,
    ack: int | None = None,
    native: int | None = None,
) -> ExtractedChronology:
    return ExtractedChronology(
        ledger_row_index=0,
        evidence_type=evidence_type,
        fact_class=evidence_type,
        delivery_seal=_seal(evidence_type),
        actual_event="edit_result",
        chronology=Chronology(
            decision_open_index=decision_open,
            delivery_index=delivery,
            decision_commit_index=decision_commit,
            native_acquisition_index=native,
            acknowledgment_index=ack,
            action_index=action,
        ),
        timing_verdict=timing,
        unmeasured_reason=None,
    )


def test_treatment_acted_positive_precommit_use() -> None:
    ec = _ec(
        evidence_type="signature_mismatch", timing=ON_TIME,
        decision_open=2, delivery=2, decision_commit=5, action=5, ack=6,
    )
    assert _treatment_acted(ec) is True


def test_treatment_not_acted_when_not_on_time() -> None:
    # ack + action both present AFTER delivery (the OLD naive check -> "acted"), but the timing is
    # LATE -> B2 clause (a) rejects it: post-hoc naming after a LATE delivery is not consumption.
    ec = _ec(
        evidence_type="signature_mismatch", timing=LATE,
        decision_open=2, delivery=2, decision_commit=5, action=5, ack=6,
    )
    assert _treatment_acted(ec) is False
    # MUTATION[drop `if ec.timing_verdict != ON_TIME: return False`] -> LATE reads acted -> RED.


def test_treatment_not_acted_when_action_after_commit() -> None:
    # ON_TIME + receipt True, but the action fell AFTER the decision commit (action=8 > commit=5):
    # the fact was not used to SHAPE the decision it targeted -> B2 clause (c) rejects it.
    ec = _ec(
        evidence_type="signature_mismatch", timing=ON_TIME,
        decision_open=2, delivery=2, decision_commit=5, action=8, ack=6,
    )
    assert _treatment_acted(ec) is False
    # MUTATION[use `d < a` instead of `d < a <= c`] -> action-after-commit reads acted -> RED.


def test_treatment_not_acted_when_receipt_unmeasured() -> None:
    # ON_TIME + precommit window OK, but the covering receipt needs a bound attestation and none
    # is supplied -> B2 clause (b) rejects it (the registry acknowledgment is unobservable).
    ec = _ec(
        evidence_type="covering_verdict", timing=ON_TIME,
        decision_open=2, delivery=2, decision_commit=5, action=5, ack=6,
    )
    assert _treatment_acted(ec, attestations=None) is False
    # MUTATION[drop the `if ack is not True: return False` receipt check] -> a covering delivery
    # with no attestation reads acted -> RED.


# =========================================================================== #
# B4 — SAFE ONE-DECISION FORK (CAUSAL_FORK) for the safety-excluded classes.
# =========================================================================== #
def _checkpoint(observation_id: int, prefix_hash: str, repo_state_hash: str) -> dict:
    cid = hashlib.sha256(_canon([observation_id, prefix_hash, repo_state_hash])).hexdigest()
    return {
        "observation_id": observation_id,
        "prefix_hash": prefix_hash,
        "repo_state_hash": repo_state_hash,
        "checkpoint_id": cid,
    }


def _submit_fork(*, checkpoint: dict | None = None, citation: dict | None = None,
                 treatment_covering=None, control_covering=None) -> dict:
    if treatment_covering is None:
        treatment_covering = {"verdict": "fail", "failing_test_names": ["t_x"]}
    return {
        "fact_class": "submit_refusal",
        "delivery_seal": _seal("submit-refusal-render"),
        "checkpoint": checkpoint if checkpoint is not None else _checkpoint(12, "p" * 64, "r" * 40),
        "citation": citation if citation is not None else {"observation_id": 12, "char_span": [0, 48]},
        "producer_inputs": {
            "treatment": {"covering": treatment_covering, "bounce_count": 0, "max_bounces": 1},
            "control": {"covering": control_covering, "bounce_count": 0, "max_bounces": 1},
        },
    }


def test_submit_refusal_fork_mints_causal_fork_and_sets_the_gate() -> None:
    # Treatment: the submit gate BLOCKS on a real covering FAIL. Control: with the GT candidate
    # withheld (covering=None) the gate ALLOWS. The decisions DIFFER -> a CAUSAL_FORK is minted by
    # the PURE gate_verdict re-derivation, and (B5) a valid adjudication SETS the gate True.
    join = join_fair_probes({"messages": []}, [], safety_forks=[_submit_fork()])
    entry = join["per_fact_class"]["submit_refusal"]
    assert entry["verdict"] == CAUSAL_FORK
    assert entry["safety_fork_probes"] == 1
    assert fair_probe_bool_by_fact_class(join)["submit_refusal"] is True
    fork_probes = [pr for pr in join["probes"] if pr["probe_kind"] == "safety_fork"]
    assert len(fork_probes) == 1
    fp = fork_probes[0]
    assert fp["bounded_confidence"] == "N1_checkpoint"          # explicit N=1
    assert fp["artifacts"]["citation_anchor"]["char_span"] == [0, 48]
    assert fp["artifacts"]["treatment"]["signal"] == "BLOCK"
    assert fp["artifacts"]["control"]["signal"] == "ALLOW"
    # MUTATION[in _safety_fork_probes `if not causal: pass` instead of `continue`] -> a
    # non-differing re-derivation would still mint -> the negative test below goes RED.


def test_submit_refusal_fork_no_mint_when_decision_does_not_differ() -> None:
    # Treatment ALLOWS (covering passes) and control ALLOWS -> the decisions do NOT differ ->
    # instrument presence is NEVER a verdict -> no fork.
    fork = _submit_fork(treatment_covering={"verdict": "pass"}, control_covering=None)
    join = join_fair_probes({"messages": []}, [], safety_forks=[fork])
    assert "submit_refusal" not in join["per_fact_class"]
    assert not [pr for pr in join["probes"] if pr["probe_kind"] == "safety_fork"]


def test_fork_rejected_on_mismatched_checkpoint_identity() -> None:
    bad = _checkpoint(12, "p" * 64, "r" * 40)
    bad["checkpoint_id"] = "0" * 64  # forged identity (does not equal sha256 of the components)
    join = join_fair_probes({"messages": []}, [], safety_forks=[_submit_fork(checkpoint=bad)])
    assert "submit_refusal" not in join["per_fact_class"]
    # MUTATION[in _fork_checkpoint_valid `return True` unconditionally] -> the forged checkpoint
    # is accepted and mints a fork -> RED.


def test_fork_rejected_on_forged_citation_anchor() -> None:
    # a malformed char span (end < start) is a forged citation anchor -> rejected.
    join = join_fair_probes(
        {"messages": []}, [],
        safety_forks=[_submit_fork(citation={"observation_id": 12, "char_span": [40, 0]})],
    )
    assert "submit_refusal" not in join["per_fact_class"]
    # MUTATION[drop the `a <= b` check in _fork_citation_valid] -> the forged anchor mints -> RED.


def test_fork_does_not_mutate_workspace_or_allow_submission(tmp_path: Path) -> None:
    # The fork is a PURE re-derivation: it must never touch the working tree or perform a submit.
    work = tmp_path / "repo"
    work.mkdir()
    f = work / "keep.txt"
    f.write_text("original", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in work.iterdir()}

    join = join_fair_probes({"messages": []}, [], safety_forks=[_submit_fork()])
    assert join["per_fact_class"]["submit_refusal"]["verdict"] == CAUSAL_FORK

    after = {p.name: p.read_bytes() for p in work.iterdir()}
    assert after == before                                   # no files added/removed/changed
    assert f.read_text(encoding="utf-8") == "original"       # no mutation of the working tree


def test_syntax_result_fork_mints_via_pure_lane_attestation() -> None:
    # The syntax fork re-derives through the lane's PURE finalize_syntax_attestation: treatment
    # (candidate present) is a COMPLETE attestation (truth PASS); control (candidate withheld from
    # the model-visible surface -> empty shipped suffix) is incomplete (not PASS). They DIFFER ->
    # CAUSAL_FORK.
    source = b"def f(\n"
    block = 'File "src/mod.py", line 1\n    def f(\n         ^\nSyntaxError: never closed'
    observation = build_syntax_observation(
        file_path="src/mod.py", source_bytes=source,
        check_result={"verdict": "syntax_error", "diagnostic": block, "language": ".py",
                      "reason": "parse_error", "checker": ["ast.parse"]},
        actual_event="edit_result", rendered_block=block,
    )
    seal = hashlib.sha256(block.encode()).hexdigest()[:16]
    treatment = {
        "observation": observation, "source_bytes": source, "producer_block": block,
        "shipped_suffix": block, "target": "src/mod.py",
        "candidate_id": lane_delivery_candidate_id("edit.syntax", "src/mod.py", block),
        "delivery_seal": seal,
    }
    control = dict(treatment)
    control["shipped_suffix"] = ""  # the GT candidate withheld from the model-visible surface
    fork = {
        "fact_class": "syntax_result",
        "delivery_seal": seal,
        "checkpoint": _checkpoint(7, "p" * 64, "r" * 40),
        "citation": {"observation_id": 7, "char_span": [0, len(block)]},
        "producer_inputs": {"treatment": treatment, "control": control},
    }
    join = join_fair_probes({"messages": []}, [], safety_forks=[fork])
    entry = join["per_fact_class"]["syntax_result"]
    assert entry["verdict"] == CAUSAL_FORK
    assert fair_probe_bool_by_fact_class(join)["syntax_result"] is True


# =========================================================================== #
# B5 — CAUSAL_PAIRED (a bare resolution delta) is ENRICHMENT ONLY: it never sets the gate.
# =========================================================================== #
def _syntax_paired_fixture() -> tuple[dict, list[dict]]:
    payload = "Syntax check on src/foo.py failed.\ndef compute_widget(x, y):"
    messages = [
        _user("Fix the syntax error."),                # 0 task_start
        _assistant("apply_patch src/foo.py"),           # 1 is_edit -> boundary 2
        _tool(payload),                                 # 2 edit_result + DELIVERY; ordinal1
        _assistant("apply_patch src/foo.py"),           # 3 mutation -> commit/action
        _tool("ok"),                                    # 4 ordinal2 -> ack
    ]
    rows = [
        _delivered_row(payload, evidence_type="syntax_result",
                       event_type="edit_result", file_path="src/foo.py"),
        _ack_row(payload, iteration=2),
    ]
    return {"messages": messages}, rows


def test_causal_paired_is_enrichment_only_and_does_not_set_the_gate() -> None:
    traj, rows = _syntax_paired_fixture()
    join = join_fair_probes(traj, rows, gt_resolved=True, baseline_resolved=False)
    entry = join["per_fact_class"]["syntax_result"]
    assert entry["verdict"] == CAUSAL_PAIRED          # still REPORTED (enrichment)
    assert entry["paired_baseline_probes"] == 1
    assert fair_probe_bool_by_fact_class(join)["syntax_result"] is None   # but NOT the gate
    # MUTATION[map CAUSAL_PAIRED -> True in _verdict_to_bool] -> the bare resolution delta sets
    # the gate again -> RED (the exact shortcut B5 rejects).


def test_probe_result_default_bounded_confidence_is_empty() -> None:
    # a non-fork ProbeResult carries no bounded confidence (the field is fork-only).
    pr = ProbeResult(
        probe_kind="randomized", fact_class="localization",
        delivered_row_index=0, holdout_row_index=1, treatment_seal="a" * 16,
        control_seal="b" * 16, treatment_outcome="acted", control_outcome="not_acted",
        causal_verdict=CAUSAL, matched_probe=None, artifacts={},
    )
    assert pr.bounded_confidence == ""
    assert pr.to_dict()["bounded_confidence"] == ""
