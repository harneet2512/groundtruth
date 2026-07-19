"""RED-first tests for SPEC-J4 diagnostics and live opportunity extraction.

Distinct delivered and withheld candidates in one trajectory are never treated as the same
counterfactual unit. Instrument presence is never a verdict. Exact-state ``MatchedProbe`` tests
exercise only the adjudicator contract; repeated live opportunities feed the cohort analyzer.

Fixtures are synthetic (built in-test). Each names the exact contrast it proves:

* diagnostic pair — delivered and withheld instances remain unpaired even when state matches.
* control-acted   — the withheld instance's entity IS named after withholding -> control
                    "acted" -> the probe is INVALID for CAUSAL (GT was not needed).
* SELF_LOCALIZED  — the agent grepped the fact BEFORE GT delivered it -> not causal.
* no-holdout      — a delivered ON_TIME row with no holdout + no self-acquire -> UNMEASURED.
* paired-baseline — a SAFETY-excluded class (syntax_result) delivered ON_TIME with ack+action,
                    GT-on resolved while baseline did not -> CAUSAL_PAIRED (distinct schema).
* tampered seal   — a MatchedProbe whose treatment_seal != delivery_seal -> not CAUSAL.

BITING MUTATIONS (documented; each applied to the module, observed to turn its target test(s)
RED, then reverted — the full run restores 11/11 green):
  MA — ``_control_outcome`` empty-window guard removed (``outcome = "acted" if entity_named else
       "not_acted"`` — the old censoring form): an activity-free window now reads ``not_acted`` ->
       false CAUSAL. BOTH ``test_end_of_trajectory_holdout_window_is_unresolved`` and
       ``test_adjacent_boundary_empty_window_is_unresolved`` go RED.
  MB — ``_control_outcome`` drops the ``not _valid_seal(control_seal)`` guard: a hash-less holdout
       row is accepted -> ``test_hashless_holdout_row_is_not_causal`` goes RED.
  M1 — ``_control_outcome`` window end set to ``len(messages)`` instead of the next boundary:
       ``test_control_window_closes_at_next_boundary`` names the withheld entity AFTER the
       window; the widened window now "sees" it, control flips to "acted", and that test REDs.
  M2 — ``_control_outcome`` entity-named case forced to ``"not_acted"``:
       ``test_control_acted_is_not_causal`` (which asserts control "acted") goes RED — the
       "did the agent act anyway" detection is load-bearing.
  M3 — ``_verdict_to_bool`` maps SELF_LOCALIZED to ``None`` instead of ``False``:
       ``test_self_localized_when_agent_self_acquired_first`` (fair_probe is False) goes RED —
       self-acquisition is honestly reported as a NON-causal (False), not merely unmeasured.
  M4 — ``_paired_baseline_probes`` drops the ``baseline_resolved is not False`` requirement:
       ``test_paired_baseline_absent_input_is_unmeasured`` (baseline ALSO resolved -> no lift ->
       UNMEASURED) goes RED — the paired path requires a real GT-on-over-baseline lift.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from groundtruth.runtime.chronological_adjudication import (  # noqa: E402
    CAUSAL,
    SELF_LOCALIZED,
    UNMEASURED,
    MatchedProbe,
    adjudicate,
)
from chronology_extract import extract_chronologies  # noqa: E402
from fair_probe_result import (  # noqa: E402
    CAUSAL_PAIRED,
    ControlResult,
    FAIR_PROBE_RESULT_SCHEMA,
    compute_matched_probes,
    extract_live_opportunity_observations,
    fair_probe_bool_by_fact_class,
    join_fair_probes,
)
import fair_probe_result as fair_probe_module  # noqa: E402


# --------------------------------------------------------------------------- #
# fixture builders (the exact shapes gt_performance_metrics._parse_timeline and
# consumption_ledger._emitted_commands read — mirrors test_chronology_extract).
# --------------------------------------------------------------------------- #
def _seal(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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


def _exit(content: str) -> dict:
    return {"role": "exit", "content": content}


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


_PAYLOAD_SIG = "Your edit to src/foo.py changed a signature.\ndef compute_widget(x, y):"
_PAYLOAD_SYN = "Syntax check on src/foo.py failed.\ndef compute_widget(x, y):"


# --------------------------------------------------------------------------- #
# CAUSAL — delivered instance acted; a same-class holdout instance not self-acquired.
# --------------------------------------------------------------------------- #
def _causal_fixture(*, holdout_file: str = "src/other.py") -> tuple[dict, list[dict]]:
    # B1 (Cluster-4): treatment and control must share a PRE-DECISION STATE to pair — the same
    # class's decision, opened at the SAME edit_result boundary (msg 2), over a byte-identical
    # observation prefix. So the holdout WITHHELD instance (iteration 1 -> the tool message at
    # msg 2) sits at the very same edit boundary the treatment was delivered into. The control
    # window (msgs 3..4, closing at the file_view boundary) inspects a decision but never commits
    # a signature-delta mutation on the withheld file -> registry receipt False -> not_acted.
    messages = [
        _user("Fix the signature bug."),               # 0 task_start
        _assistant("apply_patch src/foo.py"),           # 1 is_edit -> boundary 2
        _tool(_PAYLOAD_SIG),                            # 2 edit_result boundary + DELIVERY; ordinal 1; WITHHOLD(iter1)
        _assistant("cat notes.txt"),                    # 3 inspected control decision (names nothing relevant)
        _tool("notes"),                                 # 4 file_view boundary; ordinal 2 (closes control window)
        _assistant("apply_patch src/foo.py"),           # 5 treatment mutation -> commit 5, action 5
        _tool("edit applied; ack"),                     # 6 ordinal 3 -> ack
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_SIG, evidence_type="signature_mismatch",
            event_type="edit_result", file_path="src/foo.py",
        ),
        _ack_row(_PAYLOAD_SIG, iteration=3),            # -> ack at message 6 (> delivery 2)
        _holdout_row(
            "withheld signature fact for another file",
            fact_class="signature_delta", file_path=holdout_file, iteration=1,
        ),
    ]
    return {"messages": messages}, rows


def test_distinct_same_state_opportunities_do_not_mint_causality(tmp_path: Path) -> None:
    traj, rows = _causal_fixture()
    chron = extract_chronologies(traj, rows)
    probes = compute_matched_probes(traj, rows, chron)
    assert len(probes) == 1
    p = probes[0]
    assert p.probe_kind == "randomized"
    assert p.fact_class == "signature_delta"
    assert p.treatment_outcome == "acted"
    assert p.control_outcome == "not_acted"
    assert p.causal_verdict != CAUSAL
    assert p.matched_probe is None
    assert p.artifacts["assignment"]["same_predecision_state"] is True
    assert p.artifacts["assignment"]["pairing_rejection"].startswith(
        "distinct_mutually_exclusive_opportunities"
    )

    join = join_fair_probes(traj, rows, output_dir=str(tmp_path), task_label="synthetic__causal")
    assert join["schema"] == FAIR_PROBE_RESULT_SCHEMA
    assert join["per_fact_class"]["signature_delta"]["verdict"] == UNMEASURED
    assert fair_probe_bool_by_fact_class(join)["signature_delta"] is None
    # the sealed sidecar is written with deterministic bytes.
    sidecar = Path(join["sidecar_path"])
    assert sidecar.exists()
    first = sidecar.read_bytes()
    join_fair_probes(traj, rows, output_dir=str(tmp_path), task_label="synthetic__causal")
    assert sidecar.read_bytes() == first  # deterministic


def test_control_acted_is_not_causal() -> None:
    # B3 (Cluster-4): control "acted" is grounded in the CLASS REGISTRY RECEIPT, not a bare
    # mention. For signature_delta (a mutation-commit class) the agent must COMMIT the class
    # decision on the withheld file (a mutation naming src/bar.py) inside the window -> the
    # receipt evaluates True -> the agent self-acquired the fact anyway -> control "acted" -> the
    # probe is INVALID for CAUSAL (honest: GT was not needed). The withheld holdout is at the same
    # edit boundary as the treatment delivery so the two arms share a pre-decision state (pairing
    # is satisfied; the invalidation comes from the control receipt, not a pairing miss).
    messages = [
        _user("Fix the signature bug."),               # 0 task_start
        _assistant("apply_patch src/foo.py"),           # 1 is_edit -> boundary 2
        _tool(_PAYLOAD_SIG),                            # 2 edit_result + DELIVERY; ordinal 1 (WITHHOLD iter1)
        _assistant("apply_patch src/bar.py"),           # 3 MUTATION naming src/bar.py -> control commits the class receipt
        _tool("bar edited"),                            # 4 edit_result boundary; ordinal 2
        _assistant("apply_patch src/foo.py"),           # 5 treatment mutation -> commit/action
        _tool("done; ack"),                             # 6 ordinal 3 -> ack
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_SIG, evidence_type="signature_mismatch",
            event_type="edit_result", file_path="src/foo.py",
        ),
        _ack_row(_PAYLOAD_SIG, iteration=3),
        _holdout_row(
            "withheld signature fact for bar",
            fact_class="signature_delta", file_path="src/bar.py", iteration=1,
        ),
    ]
    traj = {"messages": messages}
    chron = extract_chronologies(traj, rows)
    probes = compute_matched_probes(traj, rows, chron)
    assert len(probes) == 1
    assert probes[0].control_outcome == "acted"
    assert probes[0].causal_verdict != CAUSAL

    join = join_fair_probes(traj, rows)
    assert join["per_fact_class"]["signature_delta"]["verdict"] != CAUSAL
    assert fair_probe_bool_by_fact_class(join)["signature_delta"] is not True


# --------------------------------------------------------------------------- #
# SELF_LOCALIZED — the agent grepped the fact before GT delivered it.
# --------------------------------------------------------------------------- #
def test_control_window_closes_at_next_boundary() -> None:
    # B3 window-bounding: the withheld entity (src/zed.py) is COMMITTED (a mutation) only AFTER
    # the next observation boundary; the control receipt is evaluated over the window that closes
    # at that boundary, so the later commit is EXCLUDED -> the control is honestly "not_acted" and
    # the probe is CAUSAL. (Pins the window end to the next boundary, not the end of the run.) The
    # holdout sits at the treatment's edit boundary so the two arms share a pre-decision state.
    messages = [
        _user("Fix the signature bug."),               # 0 task_start
        _assistant("apply_patch src/foo.py"),           # 1 is_edit -> boundary 2
        _tool(_PAYLOAD_SIG),                            # 2 edit_result + DELIVERY; ordinal 1 (WITHHOLD iter1)
        _assistant("cat notes.txt"),                    # 3 view -> boundary 4 (CLOSES the window)
        _tool("notes"),                                 # 4 file_view boundary; ordinal 2
        _assistant("apply_patch src/zed.py"),           # 5 mutates src/zed.py AFTER the window
        _tool("zed edited"),                            # 6 edit_result boundary; ordinal 3
        _assistant("apply_patch src/foo.py"),           # 7 treatment mutation -> commit, action
        _tool("edit applied; ack"),                     # 8 ordinal 4 -> ack
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_SIG, evidence_type="signature_mismatch",
            event_type="edit_result", file_path="src/foo.py",
        ),
        _ack_row(_PAYLOAD_SIG, iteration=4),            # -> ack at message 8 (> delivery 2)
        _holdout_row(
            "withheld signature fact for zed",
            fact_class="signature_delta", file_path="src/zed.py", iteration=1,
        ),
    ]
    traj = {"messages": messages}
    chron = extract_chronologies(traj, rows)
    probes = compute_matched_probes(traj, rows, chron)
    assert len(probes) == 1
    assert probes[0].control_outcome == "not_acted"
    assert probes[0].causal_verdict != CAUSAL


def test_end_of_trajectory_holdout_window_is_unresolved() -> None:
    # The withholding point sits at the LAST tool message: the decision window is EMPTY (no agent
    # decision follows). An activity-free window is censoring, NOT a counterfactual -> "unresolved"
    # (never a false CAUSAL). Treatment is a genuine ack+action, isolating the control as the block.
    messages = [
        _user("Fix the signature bug."),               # 0 task_start
        _assistant("apply_patch src/foo.py"),           # 1 is_edit -> boundary 2
        _tool(_PAYLOAD_SIG),                            # 2 edit_result + DELIVERY; ordinal 1
        _assistant("apply_patch src/foo.py"),           # 3 mutation -> commit, action
        _tool("edit applied; ack"),                     # 4 ordinal 2 (LAST tool) -> ack + WITHHOLD
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_SIG, evidence_type="signature_mismatch",
            event_type="edit_result", file_path="src/foo.py",
        ),
        _ack_row(_PAYLOAD_SIG, iteration=2),            # ack at message 4 (> delivery 2)
        _holdout_row(
            "withheld signature fact at end",
            fact_class="signature_delta", file_path="src/other.py", iteration=2,
        ),
    ]
    traj = {"messages": messages}
    chron = extract_chronologies(traj, rows)
    probes = compute_matched_probes(traj, rows, chron)
    assert len(probes) == 1
    assert probes[0].control_outcome == "unresolved"
    assert probes[0].causal_verdict != CAUSAL

    join = join_fair_probes(traj, rows)
    assert join["per_fact_class"]["signature_delta"]["verdict"] != CAUSAL
    assert fair_probe_bool_by_fact_class(join)["signature_delta"] is not True


def test_adjacent_boundary_empty_window_is_unresolved() -> None:
    # A submit (exit) boundary sits immediately after the withholding point -> the window range is
    # empty -> "unresolved" (never CAUSAL from an empty window).
    messages = [
        _user("Fix the signature bug."),               # 0 task_start
        _assistant("apply_patch src/foo.py"),           # 1 is_edit -> boundary 2
        _tool(_PAYLOAD_SIG),                            # 2 edit_result + DELIVERY; ordinal 1
        _assistant("apply_patch src/foo.py"),           # 3 mutation -> commit, action
        _tool("done"),                                  # 4 ordinal 2 -> WITHHOLD
        _exit("submit"),                                # 5 submit boundary (adjacent to withhold)
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_SIG, evidence_type="signature_mismatch",
            event_type="edit_result", file_path="src/foo.py",
        ),
        _holdout_row(
            "withheld signature fact adjacent",
            fact_class="signature_delta", file_path="src/other.py", iteration=2,
        ),
    ]
    traj = {"messages": messages}
    chron = extract_chronologies(traj, rows)
    probes = compute_matched_probes(traj, rows, chron)
    assert len(probes) == 1
    assert probes[0].control_outcome == "unresolved"
    assert probes[0].causal_verdict != CAUSAL


def test_hashless_holdout_row_is_not_causal() -> None:
    # The shadow instrument ALWAYS seals the withheld render (invariant 5); a hash-less holdout row
    # is malformed -> control "unresolved" -> no randomized CAUSAL (a control arm without a valid
    # withheld-hash is never accepted).
    traj, rows = _causal_fixture()
    rows[2].pop("content_sha256_16", None)             # strip the withheld-render hash
    chron = extract_chronologies(traj, rows)
    probes = compute_matched_probes(traj, rows, chron)
    assert len(probes) == 1
    assert probes[0].control_seal == ""
    assert probes[0].control_outcome == "unresolved"
    assert probes[0].causal_verdict != CAUSAL

    join = join_fair_probes(traj, rows)
    assert fair_probe_bool_by_fact_class(join)["signature_delta"] is not True


def test_self_localized_when_agent_self_acquired_first() -> None:
    payload_loc = "Ranked file: src/foo.py\ndef compute_widget(x, y):"
    messages = [
        _user("Where is the widget computed?"),          # 0 task_start
        _assistant("grep -rn compute_widget src/foo.py"),  # 1 is_search + SELF-ACQUIRE
        _tool("src/foo.py: def compute_widget"),          # 2 search_result boundary
        _assistant("cat src/foo.py"),                     # 3 passive read
        _tool(payload_loc),                               # 4 DELIVERY (after self-acquire)
        _assistant("apply_patch src/foo.py"),             # 5 mutation -> commit
        _tool("edit applied"),                            # 6
    ]
    rows = [
        _delivered_row(
            payload_loc, evidence_type="localization",
            event_type="search_result", file_path="src/foo.py",
        )
    ]
    traj = {"messages": messages}
    chron = extract_chronologies(traj, rows)
    # no holdout row -> no randomized probe.
    assert compute_matched_probes(traj, rows, chron) == []
    join = join_fair_probes(traj, rows)
    assert join["per_fact_class"]["localization"]["verdict"] == SELF_LOCALIZED
    assert fair_probe_bool_by_fact_class(join)["localization"] is False


# --------------------------------------------------------------------------- #
# no-holdout — a delivered ON_TIME row with no holdout + no self-acquire is UNMEASURED.
# --------------------------------------------------------------------------- #
def test_no_holdout_is_unmeasured() -> None:
    messages = [
        _user("Fix the signature bug."),               # 0 task_start
        _assistant("apply_patch src/foo.py"),           # 1 is_edit -> boundary 2
        _tool(_PAYLOAD_SIG),                            # 2 edit_result + DELIVERY
        _assistant("apply_patch src/foo.py"),           # 3 mutation -> commit
        _tool("edit applied"),                          # 4
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_SIG, evidence_type="signature_mismatch",
            event_type="edit_result", file_path="src/foo.py",
        )
    ]
    traj = {"messages": messages}
    chron = extract_chronologies(traj, rows)
    assert compute_matched_probes(traj, rows, chron) == []
    join = join_fair_probes(traj, rows)
    assert join["per_fact_class"]["signature_delta"]["verdict"] == UNMEASURED
    assert fair_probe_bool_by_fact_class(join).get("signature_delta") is None


# --------------------------------------------------------------------------- #
# paired-baseline — a SAFETY-excluded class can never have a holdout row; the causal
# claim comes from the paired baseline verdict passed by the caller (distinct schema).
# --------------------------------------------------------------------------- #
def _syntax_fixture() -> tuple[dict, list[dict]]:
    messages = [
        _user("Fix the syntax error."),                # 0 task_start
        _assistant("apply_patch src/foo.py"),           # 1 is_edit -> boundary 2
        _tool(_PAYLOAD_SYN),                            # 2 edit_result + DELIVERY; ordinal 1
        _assistant("apply_patch src/foo.py"),           # 3 mutation -> commit 3, action 3
        _tool("ok"),                                    # 4 ordinal 2 -> ack
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_SYN, evidence_type="syntax_result",
            event_type="edit_result", file_path="src/foo.py",
        ),
        _ack_row(_PAYLOAD_SYN, iteration=2),            # -> ack at message 4 (> delivery 2)
    ]
    return {"messages": messages}, rows


def test_paired_baseline_causal_paired_when_gt_resolved_baseline_not(tmp_path: Path) -> None:
    traj, rows = _syntax_fixture()
    # No shadow holdout is possible for a safety class -> no randomized probe.
    chron = extract_chronologies(traj, rows)
    assert compute_matched_probes(traj, rows, chron) == []

    join = join_fair_probes(
        traj, rows, output_dir=str(tmp_path), task_label="synthetic__syntax",
        gt_resolved=True, baseline_resolved=False,
    )
    # B5 (Cluster-4): CAUSAL_PAIRED is still REPORTED (enrichment) but no longer SETS the gate —
    # a bare resolution delta is not a valid causal adjudication. The gate value is None.
    assert join["per_fact_class"]["syntax_result"]["verdict"] == CAUSAL_PAIRED
    assert fair_probe_bool_by_fact_class(join)["syntax_result"] is None
    paired = [pr for pr in join["probes"] if pr["probe_kind"] == "paired_baseline"]
    assert len(paired) == 1
    assert paired[0]["fact_class"] == "syntax_result"


def test_paired_baseline_absent_input_is_unmeasured() -> None:
    traj, rows = _syntax_fixture()
    # No baseline verdict supplied -> the paired path stays UNMEASURED (fail-closed).
    join = join_fair_probes(traj, rows)
    assert join["per_fact_class"]["syntax_result"]["verdict"] == UNMEASURED
    assert fair_probe_bool_by_fact_class(join).get("syntax_result") is None
    # And it must NOT fire when GT resolved but baseline ALSO resolved (no lift).
    join2 = join_fair_probes(traj, rows, gt_resolved=True, baseline_resolved=True)
    assert join2["per_fact_class"]["syntax_result"]["verdict"] == UNMEASURED


# --------------------------------------------------------------------------- #
# tampered seal — a MatchedProbe whose treatment_seal != delivery_seal is not causal.
# --------------------------------------------------------------------------- #
def test_tampered_treatment_seal_is_not_causal() -> None:
    traj, rows = _causal_fixture()
    chron = extract_chronologies(traj, rows)
    ec = chron[0]
    mp = MatchedProbe(
        probe_id="explicit-repeated-state-probe",
        assignment_unit_id="frozen-state-1",
        treatment_seal=ec.delivery_seal,
        treatment_outcome="acted",
        control_outcome="not_acted",
        assignment_sha256="a" * 64,
        treatment_sha256="b" * 64,
        control_sha256="c" * 64,
        outcome_sha256="d" * 64,
    )
    # Re-adjudicate with the ORIGINAL probe -> CAUSAL (control).
    good = adjudicate(
        evidence_type=ec.evidence_type, actual_event=ec.actual_event,
        delivery_seal=ec.delivery_seal, chronology=ec.chronology, matched_probe=mp,
    )
    assert good.fair_probe_verdict == CAUSAL
    # Tamper the treatment seal (a different 16-hex) -> the seal binding breaks -> not causal.
    tampered = MatchedProbe(
        probe_id=mp.probe_id, assignment_unit_id=mp.assignment_unit_id,
        treatment_seal="0" * 16, treatment_outcome=mp.treatment_outcome,
        control_outcome=mp.control_outcome, assignment_sha256=mp.assignment_sha256,
        treatment_sha256=mp.treatment_sha256, control_sha256=mp.control_sha256,
        outcome_sha256=mp.outcome_sha256,
    )
    bad = adjudicate(
        evidence_type=ec.evidence_type, actual_event=ec.actual_event,
        delivery_seal=ec.delivery_seal, chronology=ec.chronology, matched_probe=tampered,
    )
    assert bad.fair_probe_verdict != CAUSAL


def test_live_opportunity_export_keeps_both_arms_as_distinct_units(
    monkeypatch,
) -> None:
    deliver_opportunity = "1" * 64
    holdout_opportunity = "2" * 64
    deliver_candidate = "3" * 64
    holdout_candidate = "4" * 64
    deliver_seal = "a" * 16
    holdout_seal = "b" * 16

    def assignment(
        opportunity_id: str, candidate_id: str, seal: str, arm: str,
    ) -> dict:
        return {
            "schema": "gt.shadow_assignment.v1",
            "layer": "shadow.assignment",
            "outcome": "assigned",
            "fact_class": "caller_contract",
            "candidate_id": candidate_id,
            "candidate_sha256_16": seal,
            "assignment": arm,
            "assignment_probability": 0.5,
            "observation_binding": {"opportunity_id": opportunity_id},
        }

    rows = [
        assignment(
            deliver_opportunity, deliver_candidate, deliver_seal, "DELIVER"
        ),
        {
            "outcome": "delivered",
            "fact_class": "caller_contract",
            "candidate_id": deliver_candidate,
            "content_sha256_16": deliver_seal,
            "observation_binding": {"opportunity_id": deliver_opportunity},
        },
        assignment(
            holdout_opportunity, holdout_candidate, holdout_seal, "HOLDOUT"
        ),
        {
            "outcome": "shadow_holdout",
            "fact_class": "caller_contract",
            "candidate_id": holdout_candidate,
            "content_sha256_16": holdout_seal,
            "file_path": "src/caller.py",
            "observation_binding": {"opportunity_id": holdout_opportunity},
        },
    ]
    chronology = SimpleNamespace(
        fact_class="caller_contract",
        evidence_type="caller_contract",
        ledger_row_index=1,
        chronology=SimpleNamespace(
            delivery_index=1,
            decision_open_index=0,
            decision_commit_index=2,
            native_acquisition_index=None,
        ),
    )
    monkeypatch.setattr(
        fair_probe_module,
        "_control_outcome",
        lambda *args, **kwargs: ControlResult(
            holdout_row_index=3,
            fact_class="caller_contract",
            control_seal=holdout_seal,
            withhold_index=1,
            window_end=2,
            entity_named=False,
            receipt=False,
            predecision_state_id="state",
            opportunity_id=holdout_opportunity,
            outcome="not_acted",
        ),
    )
    monkeypatch.setattr(
        fair_probe_module, "_native_acquisition_index", lambda *args, **kwargs: None
    )

    exported = extract_live_opportunity_observations(
        {"messages": []},
        rows,
        {1: chronology},
        task_label="task",
        live_witness=True,
    )
    assert [row["assignment"] for row in exported] == ["DELIVER", "HOLDOUT"]
    assert [row["opportunity_id"] for row in exported] == [
        deliver_opportunity,
        holdout_opportunity,
    ]
    assert [row["endpoint"] for row in exported] == [True, False]
    assert all(row["assignment_index"] < row["outcome_index"] for row in exported)
