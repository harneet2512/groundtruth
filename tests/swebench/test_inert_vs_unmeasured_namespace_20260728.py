r"""``inert`` must never be MANUFACTURED out of an unjoined receipt.

THE DEFECT (2026-07-28). ``fact_class_lifecycle`` wrote::

    lc["inert"] = measured(delivered > 0 and lvl < 2, source_artifact=traj_artifact)

``delivered`` comes from ``classify_ledger`` — EVERY delivered runtime-ledger row for the
class. ``lvl`` comes from ``_consumption_by_fact_class`` — the max receipt over ONLY the rows
whose bytes could be JOINED to a model-visible observation. The two live in different
namespaces and the writer compared them anyway.

Consequence: a class with 3 delivered rows, 1 of which joined at receipt 1 and 2 of which
never joined at all, is reported ``inert = MEASURED True`` — "GT delivered something that did
nothing" — when the truth for 2 of the 3 deliveries is "we could not measure what it did".
And the failure is DISCONTINUOUS: with 0 joined rows ``cons`` is empty and the field correctly
stays UNMEASURED; with 1-of-N joined it flips to a confident MEASURED True. Worse evidence
produced a more confident verdict.

THE FIX follows the C15 ``acquired_*``/``delivered_*`` precedent (commit 8f60643f4): two
separately NAMED, separately COUNTED families rather than one name carrying both meanings.

  * ``inert_receipt_joined``   — deliveries with a graded receipt that showed no reference.
  * ``inert_receipt_unjoined`` — deliveries with NO model-visible receipt to grade. Holes.
  * ``inert`` — the class-level boolean, MEASURED only when the hole count is 0.

As in C15, the UNMEASURED verdict is decided by EVIDENCE (a counted ledger_only entry), never
by reading a flag.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swebench"))

from scripts.swebench import gt_feature_schema as schema
from scripts.swebench.gt_feature_metrics import (
    _consumption_by_fact_class,
    classify_ledger,
    fact_class_lifecycle,
    unjoined_receipts_by_fact_class,
)

_LAYER = "l3.contract"          # -> caller_contract
_FACT = "caller_contract"


def _payloads(n: int) -> list[str]:
    return [f"src/pkg.py:{10 * i}: preserve parse_config callers #{i}" for i in range(n)]


def _ledger_row(payload: str) -> dict:
    return {
        "layer": _LAYER,
        "event_type": "file_view",
        "iteration": 1,
        "outcome": "delivered",
        "chars_delivered": len(payload),
        "content_sha256_16": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        "file_path": "src/pkg.py",
    }


def _timeline() -> list[dict]:
    # one assistant edit step -> caller_contract is ELIGIBLE (a function was edited).
    return [{"role": "assistant", "is_edit": True, "is_test": False,
             "is_search": False, "viewed_file": None, "edited_file": "src/pkg.py"}]


def _lifecycle(tmp_path: Path, visible: list[str], all_payloads: list[str]) -> dict:
    """Build the artifacts, run the real collector helpers, return the lifecycle."""
    rows = [_ledger_row(p) for p in all_payloads]
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    messages: list[dict] = [{"role": "assistant", "content": "inspect"}]
    for payload in visible:
        messages.append({"role": "tool", "content": "command output\n" + payload})
    # the agent never mentions pkg.py again -> receipt stays at level 1 (visible, unreferenced)
    messages.append({
        "role": "assistant",
        "content": "Nothing relevant there.",
        "tool_calls": [{"function": {"arguments": json.dumps({"command": "ls other/"})}}],
    })
    trajectory = {"messages": messages}

    consumption_by_fc, cons_ledger = _consumption_by_fact_class(trajectory, str(ledger))
    return fact_class_lifecycle(
        _FACT,
        timeline=_timeline(),
        ledger_by_fc=classify_ledger(rows),
        consumption_by_fc=consumption_by_fc,
        state_by_fc={},
        oracle_rows=[],
        has_submission=False,
        baseline_status=schema.BASELINE_UNAVAILABLE,
        registry=None,
        ledger_artifact=ledger.name,
        traj_artifact="trajectory.json",
        unjoined_receipts=unjoined_receipts_by_fact_class(cons_ledger).get(_FACT, 0),
    )


# --------------------------------------------------------------------------- #
# THE BITE — a partially-joined class may not be called inert.
# --------------------------------------------------------------------------- #
def test_partially_joined_class_is_unmeasured_not_inert(tmp_path: Path) -> None:
    """3 delivered, 1 joined at receipt 1, 2 unjoinable.

    Pre-fix this asserted ``inert = MEASURED True`` off 1/3 of the evidence."""
    payloads = _payloads(3)
    lc = _lifecycle(tmp_path, visible=payloads[:1], all_payloads=payloads)

    # the holes are COUNTED, not silently folded into the verdict
    assert lc["inert_receipt_unjoined"]["status"] == schema.STATUS_MEASURED
    assert lc["inert_receipt_unjoined"]["value"] == 2
    assert lc["inert_receipt_joined"]["status"] == schema.STATUS_MEASURED
    assert lc["inert_receipt_joined"]["value"] == 1

    # ...and the class-level boolean REFUSES to answer
    assert lc["inert"]["status"] == schema.STATUS_UNMEASURED, (
        f"inert was MANUFACTURED from a 1-of-3 join: {lc['inert']!r}"
    )
    assert lc["inert"]["value"] is None
    assert "2" in lc["inert"].get("reason", "")


def test_fully_joined_unreferenced_class_is_still_measured_inert(tmp_path: Path) -> None:
    """The fix must not kill the writer. Full receipt coverage + no reference = MEASURED True."""
    payloads = _payloads(2)
    lc = _lifecycle(tmp_path, visible=payloads, all_payloads=payloads)

    assert lc["inert_receipt_unjoined"]["value"] == 0
    assert lc["inert_receipt_joined"]["value"] == 2
    assert lc["inert"]["status"] == schema.STATUS_MEASURED
    assert lc["inert"]["value"] is True


def test_one_referenced_receipt_falsifies_inert_even_with_holes(tmp_path: Path) -> None:
    """THE ASYMMETRY. "did nothing" is universal — one hole defeats it. "did something" is
    existential — one joined receipt at level >= 2 falsifies inertness and the holes become
    irrelevant. Withdrawing THAT to UNMEASURED would be the same defect inverted: refusing to
    report a fact the artifacts prove. Measured on run 30390877219 as 4 real cells."""
    payloads = _payloads(3)
    rows = [_ledger_row(p) for p in payloads]
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    trajectory = {"messages": [
        {"role": "tool", "content": "command output\n" + payloads[0]},
        # the agent REFERENCES the delivered file -> receipt level >= 2 on the joined row
        {"role": "assistant", "content": "src/pkg.py holds the parse_config callers; opening it.",
         "tool_calls": [{"function": {"arguments": json.dumps(
             {"command": "sed -n '1,80p' src/pkg.py"})}}]},
    ]}
    consumption_by_fc, cons_ledger = _consumption_by_fact_class(trajectory, str(ledger))
    lc = fact_class_lifecycle(
        _FACT, timeline=_timeline(), ledger_by_fc=classify_ledger(rows),
        consumption_by_fc=consumption_by_fc, state_by_fc={}, oracle_rows=[],
        has_submission=False, baseline_status=schema.BASELINE_UNAVAILABLE, registry=None,
        ledger_artifact=ledger.name, traj_artifact="trajectory.json",
        unjoined_receipts=unjoined_receipts_by_fact_class(cons_ledger).get(_FACT, 0),
    )
    assert lc["inert_receipt_unjoined"]["value"] == 2      # holes are still COUNTED
    assert lc["inert_receipt_joined"]["value"] == 0        # the joined row WAS referenced
    assert lc["inert"]["status"] == schema.STATUS_MEASURED
    assert lc["inert"]["value"] is False


def test_zero_joined_rows_remain_unmeasured(tmp_path: Path) -> None:
    """The pre-existing correct case: nothing joined -> nothing claimed. Guarded so the
    fix cannot 'improve' this into a measured False."""
    payloads = _payloads(2)
    lc = _lifecycle(tmp_path, visible=[], all_payloads=payloads)

    assert lc["inert_receipt_unjoined"]["value"] == 2
    assert lc["inert_receipt_joined"]["value"] == 0
    assert lc["inert"]["status"] == schema.STATUS_UNMEASURED


def test_collector_coverage_omitted_is_unmeasured_not_a_zero(tmp_path: Path) -> None:
    """MUTATION guard: a caller that does NOT supply the coverage must get UNMEASURED, not a
    fabricated zero-hole 'everything joined' reading. Fail-closed, per gt-math rule 12."""
    payloads = _payloads(3)
    rows = [_ledger_row(p) for p in payloads]
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    trajectory = {"messages": [
        {"role": "assistant", "content": "inspect"},
        {"role": "tool", "content": "command output\n" + payloads[0]},
        {"role": "assistant", "content": "no.",
         "tool_calls": [{"function": {"arguments": json.dumps({"command": "ls o/"})}}]},
    ]}
    consumption_by_fc, _ = _consumption_by_fact_class(trajectory, str(ledger))
    lc = fact_class_lifecycle(
        _FACT, timeline=_timeline(), ledger_by_fc=classify_ledger(rows),
        consumption_by_fc=consumption_by_fc, state_by_fc={}, oracle_rows=[],
        has_submission=False, baseline_status=schema.BASELINE_UNAVAILABLE, registry=None,
        ledger_artifact=ledger.name, traj_artifact="trajectory.json",
        # unjoined_receipts deliberately NOT passed
    )
    assert lc["inert"]["status"] == schema.STATUS_UNMEASURED
    assert lc["inert_receipt_unjoined"]["status"] == schema.STATUS_UNMEASURED


def test_unjoined_counter_reads_ledger_only_entries(tmp_path: Path) -> None:
    """The hole count is EVIDENCE-derived (``source == 'ledger_only'``), never inferred from
    a count subtraction across the two namespaces that caused the defect."""
    payloads = _payloads(4)
    rows = [_ledger_row(p) for p in payloads]
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    trajectory = {"messages": [
        {"role": "tool", "content": "out\n" + payloads[0]},
        {"role": "tool", "content": "out\n" + payloads[1]},
        {"role": "assistant", "content": "done"},
    ]}
    _, cons_ledger = _consumption_by_fact_class(trajectory, str(ledger))
    only = [e for e in cons_ledger["entries"] if e.get("source") == "ledger_only"]
    assert len(only) == 2
    assert all(e["receipt"] is None and e["joined"] is False for e in only)
    assert unjoined_receipts_by_fact_class(cons_ledger) == {_FACT: 2}


def test_zero_delivered_with_a_receipt_has_no_subject(tmp_path: Path) -> None:
    """The VACUOUS form of the same bug. Ledger says 0 delivered, trajectory carries a graded
    receipt. ``measured(delivered > 0 and lvl < 2)`` answered a confident False — a disposition
    of delivered evidence asserted where there is none. 34 such cells on run 29714439700."""
    payload = "obligation: charset errors must raise DecodingError"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    # the row exists but was SUPPRESSED, so classify_ledger counts delivered == 0
    ledger.write_text(json.dumps({
        "layer": _LAYER, "event_type": "file_view", "iteration": 1,
        "outcome": "suppressed_duplicate", "chars_delivered": len(payload),
        "content_sha256_16": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        "file_path": "src/pkg.py",
    }) + "\n", encoding="utf-8")
    trajectory = {"messages": [
        {"role": "tool", "content": "<gt-caller-contract>\n" + payload + "\n</gt-caller-contract>"},
        {"role": "assistant", "content": "ok"},
    ]}
    consumption_by_fc, cons_ledger = _consumption_by_fact_class(trajectory, str(ledger))
    ledger_by_fc = classify_ledger([json.loads(l) for l in
                                    ledger.read_text(encoding="utf-8").splitlines() if l.strip()])
    assert ledger_by_fc[_FACT]["delivered"] == 0
    lc = fact_class_lifecycle(
        _FACT, timeline=_timeline(), ledger_by_fc=ledger_by_fc,
        consumption_by_fc=consumption_by_fc, state_by_fc={}, oracle_rows=[],
        has_submission=False, baseline_status=schema.BASELINE_UNAVAILABLE, registry=None,
        ledger_artifact=ledger.name, traj_artifact="trajectory.json",
        unjoined_receipts=unjoined_receipts_by_fact_class(cons_ledger).get(_FACT, 0),
    )
    assert lc["inert"]["status"] == schema.STATUS_UNMEASURED
    if consumption_by_fc.get(_FACT):
        # the reason must NAME the disagreement, not fall back to the schema default
        assert "0 delivered rows" in lc["inert"]["reason"]


def test_schema_declares_both_namespaces() -> None:
    for field in ("inert", "inert_receipt_joined", "inert_receipt_unjoined"):
        assert field in schema.LIFECYCLE_FIELDS
    assert schema.new_lifecycle("x")["inert_receipt_unjoined"]["status"] == (
        schema.STATUS_UNMEASURED
    )
