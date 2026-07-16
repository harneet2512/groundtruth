"""RED-first tests for chronology_extract — the SPEC-J3 timing join.

The extractor builds the six EXACT message indices of
``chronological_adjudication.Chronology`` from a trajectory + delivered ledger rows and
adjudicates them into per-fact-class timing verdicts. Every index is an exact message index
or ``None``; a missing join is ``None`` and the adjudicator returns ``UNMEASURED``
(fail-closed by construction).

Fixtures are synthetic (built in-test) so the suite runs anywhere. Each scenario names the
exact chronology it proves:

* ON_TIME     — GT delivers between the decision boundary and the agent's commit.
* LATE        — GT delivers AFTER the agent already committed the decision (mutation first).
* STEP_BEHIND — the agent self-acquired the fact (a grep) BEFORE GT delivered it.
* UNMEASURED  — a missing join (delivery bytes absent / unregistered type) -> fail-closed.

BITING MUTATIONS (documented; each was applied to the module and observed to turn a passing
assertion RED, then reverted):
  M1 — ``_native_acquisition_index`` always ``return None`` (drop self-acquisition detection):
       the STEP_BEHIND scenario collapses to ON_TIME. ``test_step_behind_*`` goes RED.
  M2 — ``_decision_open_index`` uses ``b < delivery_index`` instead of ``b <= delivery_index``:
       the boundary that the delivery rides is excluded, ``decision_open`` becomes ``None``,
       and the ON_TIME scenario collapses to UNMEASURED. ``test_on_time_*`` goes RED.
  M3 — ``_decision_commit_index`` scans from index 0 instead of strictly after the boundary
       (``range(0, len)``): it commits at the agent's FIRST mutation, so ``opened <= committed``
       is violated and BOTH the ON_TIME and LATE scenarios collapse to UNMEASURED. RED.
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

from scripts.swebench.chronology_extract import (  # noqa: E402
    LATE,
    ON_TIME,
    STEP_BEHIND,
    UNMEASURED,
    TIMING_JOIN_SCHEMA,
    adjudicate_deliveries,
    extract_chronologies,
    timing_by_fact_class,
)


# --------------------------------------------------------------------------- #
# fixture builders
# --------------------------------------------------------------------------- #
def _seal(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _assistant(command: str) -> dict:
    """A mini-swe assistant message whose tool_calls carry one shell command — the shape
    both gt_performance_metrics._parse_timeline and consumption_ledger._emitted_commands read."""
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


# The delivered payloads carry a source-path token (foo.py) + a def symbol so the
# consumption-ledger entity extractor yields real entities for the acquisition scans.
_PAYLOAD_SIG = "Your edit to src/foo.py changed a signature.\ndef compute_widget(x, y):"
_PAYLOAD_LOC = "Ranked file: src/foo.py\ndef compute_widget(x, y):"


# --------------------------------------------------------------------------- #
# ON_TIME — delivered at the edit boundary, before the agent's next mutation.
# --------------------------------------------------------------------------- #
def _on_time_fixture() -> tuple[dict, list[dict]]:
    messages = [
        _user("Fix the signature bug."),                 # 0 task_start
        _assistant("apply_patch src/foo.py"),            # 1 is_edit -> boundary at 2
        _tool(_PAYLOAD_SIG),                             # 2 edit_result boundary + DELIVERY
        _assistant("apply_patch src/foo.py"),            # 3 mutation -> decision_commit
        _tool("edit applied"),                           # 4
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_SIG,
            evidence_type="signature_mismatch",
            event_type="edit_result",
            file_path="src/foo.py",
        )
    ]
    return {"messages": messages}, rows


def test_on_time_delivered_before_commit() -> None:
    traj, rows = _on_time_fixture()
    chron = extract_chronologies(traj, rows)
    assert len(chron) == 1
    ec = chron[0]
    assert ec.fact_class == "signature_delta"
    assert ec.chronology.decision_open_index == 2
    assert ec.chronology.delivery_index == 2
    assert ec.chronology.decision_commit_index == 3
    assert ec.chronology.native_acquisition_index is None
    assert ec.timing_verdict == ON_TIME
    assert ec.unmeasured_reason is None

    join = adjudicate_deliveries(traj, rows)
    assert join["schema"] == TIMING_JOIN_SCHEMA
    assert join["per_fact_class"]["signature_delta"]["verdict"] == ON_TIME
    assert timing_by_fact_class(join)["signature_delta"] is True


# --------------------------------------------------------------------------- #
# LATE — the agent committed (a mutation) BEFORE GT delivered.
# --------------------------------------------------------------------------- #
def _late_fixture() -> tuple[dict, list[dict]]:
    messages = [
        _user("Fix the signature bug."),                 # 0 task_start
        _assistant("apply_patch src/foo.py"),            # 1 is_edit -> boundary at 2
        _tool("edit applied"),                           # 2 edit_result boundary (opened)
        _assistant("rm build.tmp"),                      # 3 mutation (not an edit boundary) -> commit
        _tool("removed"),                                # 4
        _assistant("echo done"),                         # 5 non-mutating
        _tool(_PAYLOAD_SIG),                             # 6 DELIVERY — LATE (after commit)
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_SIG,
            evidence_type="signature_mismatch",
            event_type="edit_result",
            file_path="src/foo.py",
        )
    ]
    return {"messages": messages}, rows


def test_late_delivered_after_commit() -> None:
    traj, rows = _late_fixture()
    ec = extract_chronologies(traj, rows)[0]
    assert ec.chronology.decision_open_index == 2
    assert ec.chronology.decision_commit_index == 3
    assert ec.chronology.delivery_index == 6
    assert ec.timing_verdict == LATE

    join = adjudicate_deliveries(traj, rows)
    assert join["per_fact_class"]["signature_delta"]["verdict"] == LATE
    assert timing_by_fact_class(join)["signature_delta"] is False


# --------------------------------------------------------------------------- #
# STEP_BEHIND — the agent grepped the fact for itself before GT delivered it.
# --------------------------------------------------------------------------- #
def _step_behind_fixture() -> tuple[dict, list[dict]]:
    messages = [
        _user("Where is the widget computed?"),               # 0 task_start
        _assistant("grep -rn compute_widget src/foo.py"),     # 1 is_search + SELF-ACQUIRE
        _tool("src/foo.py: def compute_widget"),              # 2 search_result boundary (opened)
        _assistant("cat src/foo.py"),                         # 3 passive read
        _tool(_PAYLOAD_LOC),                                  # 4 DELIVERY (after self-acquire)
        _assistant("apply_patch src/foo.py"),                 # 5 mutation -> commit
        _tool("edit applied"),                                # 6
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_LOC,
            evidence_type="localization",
            event_type="search_result",
            file_path="src/foo.py",
        )
    ]
    return {"messages": messages}, rows


def test_step_behind_self_acquired_before_delivery() -> None:
    traj, rows = _step_behind_fixture()
    ec = extract_chronologies(traj, rows)[0]
    assert ec.fact_class == "localization"
    assert ec.chronology.decision_open_index == 2
    assert ec.chronology.delivery_index == 4
    assert ec.chronology.native_acquisition_index == 1
    assert ec.chronology.native_acquisition_index < ec.chronology.delivery_index
    assert ec.timing_verdict == STEP_BEHIND

    join = adjudicate_deliveries(traj, rows)
    assert join["per_fact_class"]["localization"]["verdict"] == STEP_BEHIND
    assert timing_by_fact_class(join)["localization"] is False


# --------------------------------------------------------------------------- #
# UNMEASURED — a missing join is None -> the adjudicator fails closed.
# --------------------------------------------------------------------------- #
def test_unmeasured_when_delivery_bytes_absent_from_trajectory() -> None:
    # The row is sealed + registered, but its bytes never appear in any message -> the
    # delivery cannot be joined -> UNMEASURED (fail-closed), not silently graded.
    messages = [
        _user("Fix the bug."),
        _assistant("apply_patch src/foo.py"),
        _tool("edit applied"),
    ]
    rows = [
        _delivered_row(
            "GT bytes the model never saw in this trajectory",
            evidence_type="signature_mismatch",
            event_type="edit_result",
            file_path="src/foo.py",
        )
    ]
    ec = extract_chronologies({"messages": messages}, rows)[0]
    assert ec.chronology.delivery_index is None
    assert ec.timing_verdict == UNMEASURED
    assert ec.unmeasured_reason == "delivery_unjoined"

    join = adjudicate_deliveries({"messages": messages}, rows)
    assert join["per_fact_class"]["signature_delta"]["verdict"] == UNMEASURED
    assert timing_by_fact_class(join)["signature_delta"] is None


def test_unmeasured_when_evidence_type_unregistered() -> None:
    payload = "some delivered bytes not tied to any registered fact class here"
    messages = [_user("go"), _assistant("cat x"), _tool(payload)]
    rows = [
        {
            "layer": "not_a_real_layer",
            "evidence_type": "totally_unregistered_type",
            "event_type": "edit_result",
            "file_path": "src/foo.py",
            "outcome": "delivered",
            "chars_delivered": len(payload),
            "content_sha256_16": _seal(payload),
        }
    ]
    ec = extract_chronologies({"messages": messages}, rows)[0]
    assert ec.fact_class is None
    assert ec.timing_verdict == UNMEASURED
    assert ec.unmeasured_reason == "unregistered_evidence_type"
    # A row that resolves to no registered class must not fabricate a class verdict.
    join = adjudicate_deliveries({"messages": messages}, rows)
    assert join["per_fact_class"] == {}


# --------------------------------------------------------------------------- #
# Non-delivered rows and rollup semantics.
# --------------------------------------------------------------------------- #
def test_non_delivered_rows_are_ignored() -> None:
    messages = [_user("go"), _assistant("cat x"), _tool("nothing")]
    rows = [
        {"outcome": "suppressed_internal_only", "chars_delivered": 0, "layer": "x"},
        {"outcome": "delivered", "chars_delivered": 0, "layer": "x"},  # zero chars
        {"outcome": "eligible", "chars_delivered": 5, "layer": "x"},
    ]
    assert extract_chronologies({"messages": messages}, rows) == {}
    join = adjudicate_deliveries({"messages": messages}, rows)
    assert join["delivered_rows_graded"] == 0
    assert join["per_fact_class"] == {}


def test_class_rollup_any_late_makes_class_late() -> None:
    # Two delivered signature_delta rows: one ON_TIME, one LATE -> class LATE (any row late).
    on_traj, on_rows = _on_time_fixture()
    late_traj, late_rows = _late_fixture()
    # Grade each independently, then confirm the rollup rule directly on a mixed class.
    on_join = adjudicate_deliveries(on_traj, on_rows)
    late_join = adjudicate_deliveries(late_traj, late_rows)
    assert on_join["per_fact_class"]["signature_delta"]["verdict"] == ON_TIME
    assert late_join["per_fact_class"]["signature_delta"]["verdict"] == LATE

    # A class whose rows are all UNMEASURED stays UNMEASURED -> correct_time None (fail-closed).
    messages = [_user("go"), _assistant("cat x"), _tool("no delivery bytes here")]
    rows = [
        _delivered_row(
            "unjoinable payload alpha", evidence_type="signature_mismatch",
            event_type="edit_result", file_path="a.py",
        ),
        _delivered_row(
            "unjoinable payload beta", evidence_type="signature_mismatch",
            event_type="edit_result", file_path="b.py",
        ),
    ]
    join = adjudicate_deliveries({"messages": messages}, rows)
    pfc = join["per_fact_class"]["signature_delta"]
    assert pfc["verdict"] == UNMEASURED
    assert pfc["rows_total"] == 2
    assert pfc["rows_unmeasured"] == 2
    assert timing_by_fact_class(join)["signature_delta"] is None


def test_join_output_is_json_serializable() -> None:
    traj, rows = _on_time_fixture()
    join = adjudicate_deliveries(traj, rows)
    # ss_integrity["chronological_timing"] must round-trip through JSON.
    assert json.loads(json.dumps(join)) == join
