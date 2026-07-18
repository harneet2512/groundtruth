"""RED-first tests for the Cluster-3 per-class decision-commit kernel (A), the WRONG_EVENT
verdict rollup (C), and block-level compound-brief chronology (D).

BITING MUTATIONS (each applied to chronology_extract / chronological_adjudication and observed
to turn a passing assertion RED, then reverted):
  M1 — the OPEN-commit branch is removed (localization falls through to mutation-only): a
       passive `cat` of the ranked file no longer commits, ``test_view_commits_localization``
       RED (decision_commit_index becomes None).
  M2 — the mutation-commit entity gate is dropped (``if _action_kind(cmd)=='mutation': return``
       without ``_names(cmd)``): an UNRELATED `rm` wrongly commits caller_contract,
       ``test_unrelated_mutation_does_not_commit_caller_contract`` RED.
  M3 — the WRONG_EVENT split is reverted (return UNMEASURED for a known-but-wrong event):
       ``test_wrong_known_event_is_false_not_unmeasured`` RED (correct_time becomes None).
  M4 — extract_block_chronologies is a no-op (returns []): the compound brief collapses to no
       per-class verdict, ``test_compound_brief_blocks_adjudicated_separately`` RED.
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

from chronology_extract import (  # noqa: E402
    ON_TIME,
    UNMEASURED,
    WRONG_EVENT,
    adjudicate_deliveries,
    extract_block_chronologies,
    extract_chronologies,
    timing_by_fact_class,
)


def _seal(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _assistant(command: str) -> dict:
    return {
        "role": "assistant",
        "content": command,
        "tool_calls": [
            {"function": {"name": "bash", "arguments": json.dumps({"command": command})}}
        ],
    }


def _tool(content: str) -> dict:
    return {"role": "tool", "content": content}


def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _row(seal_text: str, *, evidence_type: str, event_type: str, file_path: str) -> dict:
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


_PAYLOAD_LOC = "Ranked file: src/foo.py\ndef compute_widget(x, y):"
_PAYLOAD_CALLER = "Callers of compute_widget in src/foo.py must be preserved.\ndef compute_widget(x, y):"


# --------------------------------------------------------------------------- #
# A — OPEN commit: a PASSIVE view of the ranked file commits localization.
# --------------------------------------------------------------------------- #
def test_view_commits_localization() -> None:
    messages = [
        _user("Where is the widget computed?"),        # 0 task_start
        _assistant("grep -rn widget ."),               # 1 search (does NOT name foo.py)
        _tool(_PAYLOAD_LOC),                           # 2 search_result boundary + DELIVERY
        _assistant("cat src/foo.py"),                  # 3 PASSIVE open naming foo.py -> OPEN commit
        _tool("...file contents..."),                  # 4
    ]
    rows = [_row(_PAYLOAD_LOC, evidence_type="localization",
                 event_type="search_result", file_path="src/foo.py")]
    ec = extract_chronologies({"messages": messages}, rows)[0]
    assert ec.fact_class == "localization"
    assert ec.chronology.delivery_index == 2
    # A: the passive `cat src/foo.py` (NOT a mutation) commits the "which file to open" decision.
    assert ec.chronology.decision_commit_index == 3
    assert ec.chronology.native_acquisition_index is None
    assert ec.timing_verdict == ON_TIME


# --------------------------------------------------------------------------- #
# A — mutation-commit entity gate: an UNRELATED mutation does NOT commit caller_contract.
# --------------------------------------------------------------------------- #
def test_unrelated_mutation_does_not_commit_caller_contract() -> None:
    messages = [
        _user("Modify the fn."),                       # 0 task_start
        _assistant("cat src/bar.py"),                  # 1 view -> file_view boundary at 2
        _tool(_PAYLOAD_CALLER),                        # 2 file_view boundary + DELIVERY (names foo.py)
        _assistant("rm build.tmp"),                    # 3 UNRELATED mutation (does NOT name foo.py)
        _tool("removed"),                              # 4
    ]
    rows = [_row(_PAYLOAD_CALLER, evidence_type="caller_contract",
                 event_type="file_view", file_path="src/foo.py")]
    ec = extract_chronologies({"messages": messages}, rows)[0]
    assert ec.fact_class == "caller_contract"
    assert ec.chronology.delivery_index == 2
    # A: the unrelated `rm build.tmp` names no delivered entity -> it does NOT commit.
    assert ec.chronology.decision_commit_index is None
    assert ec.timing_verdict == UNMEASURED
    assert ec.unmeasured_reason == "decision_commit_unresolved"


def test_related_mutation_does_commit_caller_contract() -> None:
    # the sibling: a mutation that DOES name the delivered entity commits (the gate is a filter,
    # not a blanket block).
    messages = [
        _user("Modify the fn."),
        _assistant("cat src/bar.py"),
        _tool(_PAYLOAD_CALLER),
        _assistant("sed -i 's/a/b/' src/foo.py"),      # 3 mutation naming foo.py -> commit
        _tool("edited"),
    ]
    rows = [_row(_PAYLOAD_CALLER, evidence_type="caller_contract",
                 event_type="file_view", file_path="src/foo.py")]
    ec = extract_chronologies({"messages": messages}, rows)[0]
    assert ec.chronology.decision_commit_index == 3


# --------------------------------------------------------------------------- #
# C — WRONG_EVENT: a known-but-wrong delivery event is a MEASURED False, not UNMEASURED.
# --------------------------------------------------------------------------- #
def test_wrong_known_event_is_false_not_unmeasured() -> None:
    # caller_contract's declared boundary is file_view; delivering it at a KNOWN-but-wrong event
    # (search_result) is a measured timing FAILURE, never an unmeasured gap.
    messages = [
        _user("Modify the fn."),                       # 0 task_start
        _assistant("grep -rn compute_widget src ."),   # 1 search boundary
        _tool(_PAYLOAD_CALLER),                         # 2 DELIVERY at search_result (WRONG event)
    ]
    rows = [_row(_PAYLOAD_CALLER, evidence_type="caller_contract",
                 event_type="search_result", file_path="src/foo.py")]
    ec = extract_chronologies({"messages": messages}, rows)[0]
    assert ec.actual_event == "search_result"
    assert ec.timing_verdict == WRONG_EVENT
    # the wrong-event row is MEASURED (not labelled an unmeasured vocabulary gap).
    assert ec.unmeasured_reason is None

    join = adjudicate_deliveries({"messages": messages}, rows)
    fc = join["per_fact_class"]["caller_contract"]
    assert fc["verdict"] == WRONG_EVENT
    assert fc["correct_time"] is False           # measured FALSE, not None
    assert fc["rows_wrong_event"] == 1
    assert timing_by_fact_class(join)["caller_contract"] is False


# --------------------------------------------------------------------------- #
# D — block-level compound brief: obligations + localization blocks graded separately.
# --------------------------------------------------------------------------- #
def _compound_brief_fixture() -> tuple[dict, list[dict]]:
    obl_block = "OBLIGATIONS:\n- preserve compute_widget in src/foo.py\n"
    loc_block = "LOCALIZATION:\nRanked: src/foo.py def compute_widget\n"
    brief_text = obl_block + loc_block
    obl_span = [0, len(obl_block)]
    loc_span = [len(obl_block), len(brief_text)]
    block_lineage = [
        {
            "block_id": "b-obl",
            "label": "obligations",
            "candidate_id": "cand-obl",
            "char_span": obl_span,
            "chars_delivered": len(obl_block),
            "content_sha256_16": _seal(obl_block),
            "declared_fact_class": "obligations",
        },
        {
            "block_id": "b-loc",
            "label": "file-entry-0",
            "candidate_id": "cand-loc",
            "char_span": loc_span,
            "chars_delivered": len(loc_block),
            "content_sha256_16": _seal(loc_block),
            "declared_fact_class": "localization",
        },
    ]
    brief_row = {
        "layer": "brief.task",
        "event_type": "task_start",
        "file_path": "",
        "outcome": "delivered",
        "reason": "step0_brief_prepend",
        "chars_delivered": len(brief_text),
        "iteration": 0,
        "content_sha256_16": _seal(brief_text),
        "block_lineage": block_lineage,
    }
    messages = [
        _user(brief_text),                                             # 0 brief delivery
        _assistant("I will preserve compute_widget in src/foo.py"),   # 1 obligations plan-prose commit
        _tool("ok"),                                                   # 2
        _assistant("cat src/foo.py"),                                  # 3 localization OPEN commit
        _tool("...file..."),                                           # 4
    ]
    return {"messages": messages}, [brief_row]


def test_compound_brief_blocks_adjudicated_separately() -> None:
    traj, rows = _compound_brief_fixture()

    # the whole-row (unregistered "brief.task") never collapses to a single fact-class verdict.
    whole = extract_chronologies(traj, rows)[0]
    assert whole.fact_class is None

    blocks = extract_block_chronologies(traj, rows)
    by_block = {ec.block_id: ec for ec in blocks}
    assert set(by_block) == {"b-obl", "b-loc"}
    # each block is adjudicated as its OWN declared_fact_class.
    assert by_block["b-obl"].fact_class == "obligations"
    assert by_block["b-loc"].fact_class == "localization"
    # the obligations block (task_start decision) is committed by the plan-prose naming the subject.
    assert by_block["b-obl"].chronology.decision_commit_index == 1
    assert by_block["b-obl"].timing_verdict == ON_TIME

    join = adjudicate_deliveries(traj, rows)
    # BOTH classes appear in the join — the compound brief is never one collapsed verdict.
    assert "obligations" in join["per_fact_class"]
    assert "localization" in join["per_fact_class"]
    assert join["per_fact_class"]["obligations"]["verdict"] == ON_TIME
    # the localization block is delivered at task_start but its registry boundary is search_result
    # -> a MEASURED wrong-event (honest per-block verdict, not collapsed into the obligations one).
    assert join["per_fact_class"]["localization"]["verdict"] == WRONG_EVENT


# --------------------------------------------------------------------------- #
# E — logical-clock guard is exercised through the pure adjudicator elsewhere; here confirm the
# extract layer never produces an inverted action clock (action_index always > delivery_index).
# --------------------------------------------------------------------------- #
def test_extract_action_index_is_after_delivery() -> None:
    messages = [
        _user("Modify the fn."),
        _assistant("cat src/bar.py"),
        _tool(_PAYLOAD_CALLER),
        _assistant("sed -i 's/a/b/' src/foo.py"),
        _tool("edited"),
    ]
    rows = [_row(_PAYLOAD_CALLER, evidence_type="caller_contract",
                 event_type="file_view", file_path="src/foo.py")]
    ec = extract_chronologies({"messages": messages}, rows)[0]
    ch = ec.chronology
    if ch.action_index is not None and ch.delivery_index is not None:
        assert ch.action_index > ch.delivery_index
