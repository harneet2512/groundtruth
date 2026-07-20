"""Part 1 of the caller_contract search co-fact (2026-07-20).

The pre-edit `def_partition` physical delivery (fired at `search_result` on the
agent's own grep) ALREADY carries authorized caller-contract bytes: when a symbol
resolves to a single def file, `_resolve_symbol_defs` sets
`callers_render = _caller_contract_for_file(...)` (the contract_map engine, every
leak guard inherited) and `_fmt_def_facts` renders it as the `callers: …` line.

Those bytes answer the caller_contract DECISION ("how to modify a fn") but were
only ever TYPED as def_partition, so the canonical caller_contract class — whose
boundary is `file_view` (PRE-EDIT) — could only ever be credited by a SEPARATE,
post-view/post-edit delivery, grading WRONG_EVENT relative to its own window.

Registry Part 1 introduces the fine evidence_type `caller_contract_search`, the
PRE-EDIT mirror of the existing `caller_break` (which aliases to caller_contract
by DECISION and overrides its boundary to `edit_result` by TIMING). This one
aliases to caller_contract by decision and overrides its boundary to
`search_result` by timing — so a caller-contract co-fact delivered ON the
def_partition search observation is ON_TIME against ITS OWN declared window,
without disturbing the canonical caller_contract row (still file_view).

Part 1 is INERT until Part 2 emits the type: nothing ships `caller_contract_search`
yet, so no delivery, dose, or grade changes. This test pins the registry contract.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from groundtruth.runtime import fact_registry as fr  # noqa: E402
from groundtruth.runtime import chronological_adjudication as ca  # noqa: E402

CO = "caller_contract_search"


def test_cofact_aliases_to_caller_contract():
    assert fr._canonical_fact_class(CO) == "caller_contract"
    reg = fr.registration_for(CO)
    assert reg is not None
    assert reg.fact_class == "caller_contract"
    assert reg.producer == "contract_map"


def test_cofact_boundary_is_search_result():
    # the whole point: the co-fact's last-useful boundary is the search observation
    assert fr.required_event(CO) == fr.EVENT_SEARCH_RESULT
    assert fr.earliest_event_for(CO) == fr.EVENT_SEARCH_RESULT


def test_cofact_is_not_reactive():
    # it is a genuine pre-edit contract delivered at a FIXED boundary (search_result),
    # not an observation-reactive trace — the wrong-event check must still apply.
    assert fr.is_reactive(CO) is False


def test_canonical_caller_contract_row_undisturbed():
    # Part 1 must not move the canonical class's own boundary or reactivity.
    assert fr.required_event("caller_contract") == fr.EVENT_FILE_VIEW
    assert fr.is_reactive("caller_contract") is False


def test_contract_map_is_authoritative_producer_for_cofact():
    # the caller bytes are contract_map's (via _caller_contract_for_file); the
    # producer-authority fallback (reg.producer) must accept contract_map.
    assert fr.producer_matches(CO, "contract_map") is True
    assert fr.producer_matches(CO, "not_a_producer") is False


def test_search_delivered_cofact_grades_on_time_not_wrong_event():
    # end-to-end intent through the real adjudicator: a co-fact delivered AT
    # search_result grades ON_TIME (sane indices), NOT WRONG_EVENT.
    chron = ca.Chronology(
        decision_open_index=2,
        delivery_index=3,
        decision_commit_index=6,
        native_acquisition_index=None,
        acknowledgment_index=4,
        action_index=5,
    )
    adj = ca.adjudicate(
        evidence_type=CO,
        actual_event="search_result",
        delivery_seal="0123456789abcdef",
        chronology=chron,
    )
    assert adj.required_event == "search_result"
    assert adj.timing_verdict == ca.ON_TIME

    # control: the SAME bytes typed as the canonical caller_contract (window
    # file_view) but delivered at search_result would be WRONG_EVENT — which is
    # exactly the mis-attribution the co-fact type exists to correct.
    adj_wrong = ca.adjudicate(
        evidence_type="caller_contract",
        actual_event="search_result",
        delivery_seal="0123456789abcdef",
        chronology=chron,
    )
    assert adj_wrong.timing_verdict == ca.WRONG_EVENT
