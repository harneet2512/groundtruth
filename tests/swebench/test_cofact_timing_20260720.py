"""Part 3a — the caller_contract co-fact TIMING join (2026-07-20).

A pre-edit `post_search.localize` def-facts block delivered at the agent's own grep
carries a `callers:` line = authorized caller-contract bytes; the seam stamps a `co_fact`
sidecar on that ledger row. `extract_cofact_chronologies` credits the caller_contract
FACT class on that SAME physical delivery at the caller_contract_search boundary
(search_result), so its class verdict can grade ON_TIME instead of the WRONG_EVENT its
file_view window forces on a separate post-view/edit delivery.

The co-fact is NOVEL vs the agent's grep: the agent searched the DEF symbol; it did NOT
grep the CALLERS. So self-acquisition must be judged on the caller entities, NOT the def
symbol — otherwise the grep the co-fact rides false-flags STEP_BEHIND.
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
    ON_TIME,
    STEP_BEHIND,
    WRONG_EVENT,
    adjudicate_deliveries,
    extract_cofact_chronologies,
    timing_by_fact_class,
)


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


# a real post_search def-facts block: def site for load_config + a callers line naming
# TWO caller files/symbols the agent has NOT searched. The agent grepped `load_config`.
_DEF_BLOCK = (
    "src/app/config.py:12:load_config\n"
    "callers: src/app/api.py:40 (get_settings); src/app/cli.py:8 (run_cli)\n"
    "test refs: 2"
)


def _cofact_row() -> dict:
    from groundtruth.runtime.feature_lineage import build_lineage, lineage_ledger_extra
    co = lineage_ledger_extra(build_lineage(
        runtime_producer_id="contract_map",
        evidence_type="caller_contract_search",
        actual_event="search_result"))
    return {
        "layer": "post_search.localize",
        "evidence_type": "def_partition",
        "event_type": "search_result",
        "file_path": "src/app/config.py",
        "outcome": "delivered",
        "reason": "delivery",
        "chars_delivered": len(_DEF_BLOCK),
        "content_sha256_16": _seal(_DEF_BLOCK),
        "co_fact": co,
    }


def _fixture() -> tuple[dict, list[dict]]:
    messages = [
        _user("Where is load_config?"),                        # 0 task_start
        _assistant("grep -rn load_config src/app/config.py"),  # 1 is_search -> boundary at 2
        _tool(_DEF_BLOCK),                                      # 2 search_result boundary + DELIVERY
        _assistant("apply_patch src/app/config.py"),           # 3 mutation -> decision_commit
        _tool("edit applied"),                                 # 4
    ]
    return {"messages": messages}, [_cofact_row()]


def test_cofact_credits_caller_contract():
    traj, rows = _fixture()
    ecs = extract_cofact_chronologies(traj, rows)
    assert len(ecs) == 1, "one co-fact chronology from the co_fact row"
    ec = ecs[0]
    assert ec.fact_class == "caller_contract"
    assert ec.evidence_type == "caller_contract_search"
    assert ec.actual_event == "search_result"
    # the WHOLE point: caller_contract is credited at search_result, NOT WRONG_EVENT
    print("CO-FACT VERDICT:", ec.timing_verdict)
    assert ec.timing_verdict != WRONG_EVENT


def test_cofact_folds_into_join_as_caller_contract():
    traj, rows = _fixture()
    join = adjudicate_deliveries(traj, rows)
    pfc = join["per_fact_class"]
    assert "caller_contract" in pfc, "caller_contract must appear in the join via the co-fact"
    print("CALLER_CONTRACT VERDICT:", pfc["caller_contract"]["verdict"])
    print("CORRECT_TIME:", timing_by_fact_class(join).get("caller_contract"))
    # the goal: caller_contract grades ON_TIME (correct_time True) on this pre-edit-only task
    assert pfc["caller_contract"]["verdict"] == ON_TIME
    assert timing_by_fact_class(join)["caller_contract"] is True


def test_cofact_still_step_behind_when_callers_self_acquired():
    # HONESTY CONTROL: the novelty refinement judges self-acquisition on the CALLERS, so
    # when the agent DID grep a caller before the co-fact delivered, STEP_BEHIND must still
    # fire — the refinement narrows the entity set, it does not blanket-suppress the check.
    messages = [
        _user("Trace get_settings."),                           # 0
        _assistant("grep -rn get_settings src/app/api.py"),     # 1 self-acquire a CALLER
        _tool("src/app/api.py:40: def get_settings():"),        # 2
        _assistant("grep -rn load_config src/app/config.py"),   # 3 the search the co-fact rides
        _tool(_DEF_BLOCK),                                       # 4 DELIVERY (co-fact)
        _assistant("apply_patch src/app/config.py"),            # 5 commit
        _tool("edit applied"),                                  # 6
    ]
    traj, rows = {"messages": messages}, [_cofact_row()]
    ec = extract_cofact_chronologies(traj, rows)[0]
    print("SELF-ACQUIRED-CALLER VERDICT:", ec.timing_verdict)
    assert ec.timing_verdict == STEP_BEHIND
    assert ec.chronology.native_acquisition_index == 1


def test_cofact_shares_host_physical_id_dose_stays_one():
    # DOSE-SAFETY: the co-fact credits caller_contract on the SAME physical delivery as its
    # host def_partition row — identical physical_id + delivery_seal. It is NOT a new physical
    # entry, so a physical-id dose count can never see a second dose from the co-fact.
    traj, rows = _fixture()
    join = adjudicate_deliveries(traj, rows)
    deliveries = join["deliveries"]
    dp = [d for d in deliveries if d["fact_class"] == "def_partition"]
    cc = [d for d in deliveries if d["fact_class"] == "caller_contract"]
    assert len(dp) == 1 and len(cc) == 1, "one host row + one co-fact credit"
    assert dp[0]["ledger_row_index"] == cc[0]["ledger_row_index"], "same physical ledger row"
    assert dp[0]["delivery_seal"] == cc[0]["delivery_seal"], "shared content seal"
    assert cc[0]["physical_id"] == dp[0]["physical_id"], "shared physical_id -> dose once"
    # exactly ONE distinct physical_id across both credited classes
    assert len({d["physical_id"] for d in deliveries if d["physical_id"]}) == 1
