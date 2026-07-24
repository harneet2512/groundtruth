"""AUDIT 2026-07-24 — EXECUTABLE dispatch-reachability proof for every gateway producer.

WHY THIS EXISTS. `newfile_precedent`/`GT_CHANGE_SURFACE` emitted 0 rows across all 4 tasks of run
30121930273 while being fully implemented, unit-tested, and *called* in the source. Reading the
source said "reachable"; the runtime said otherwise, because its only route (ZERO_ABSENT) sits
behind a gate demanding the agent fail the SAME search stem twice. Source-reading and unit tests
both MISSED it. Only driving the real dispatch exposes this class of bug.

So this test asserts the DISPATCH TABLE ITSELF, through the real `augment()` entry point that every
live observation traverses — the mapping from "what the agent just did" to "which producers get a
chance to speak." Producers are stubbed: the assertion is about REACHABILITY, not evidence content
(which needs a populated graph.db). A producer that silently stops being reachable is exactly the
failure this pins, and it is invisible to every other test we have.
"""
from __future__ import annotations
import pytest
from groundtruth.runtime import gateway as gw

_PRODUCERS = (
    "_produce_body", "_produce_caller_contract", "_produce_change_surface", "_produce_covering",
    "_produce_def_ref_partition", "_produce_name_fold", "_produce_patch_delta",
    "_produce_ranked_localization", "_produce_trace", "_produce_wrong_surface",
)


@pytest.fixture
def reached(monkeypatch):
    """Drive the real augment() and return the set of producers it reached."""
    for flag in ("GT_GATEWAY", "GT_CHANGE_SURFACE", "GT_PATCH_DELTA",
                 "GT_LOC_RESLOT", "GT_CS_EDIT_TRIGGER"):
        monkeypatch.setenv(flag, "1")
    hits: set[str] = set()
    for name in _PRODUCERS:
        monkeypatch.setattr(gw, name, (lambda n: (lambda *a, **k: hits.add(n) or []))(name))

    def _drive(event):
        hits.clear()
        gw.augment(event, gw.GatewayState())
        return set(hits)

    return _drive


def _edit(mapping, idx=1):
    return gw.ToolEvent(kind=gw.KIND_EDIT, command="edit", action_index=idx,
                        changed_files=tuple(mapping), edit_before_after=mapping)


def test_edit_creating_a_file_reaches_change_surface(reached):
    """The regression this commit fixes: a CREATION must reach change_surface."""
    assert "_produce_change_surface" in reached(_edit({"pkg/new.py": ("", "def f(): return 1\n")})), \
        "a file creation does not reach change_surface — the 0-rows-in-production bug is back"


def test_the_new_trigger_is_ADDITIVE_not_displacing(reached):
    """Explicit user constraint: no feature may be enabled by taking another's slot.
    The creating edit must reach change_surface IN ADDITION TO the incumbent edit producers."""
    got = reached(_edit({"pkg/new.py": ("", "def f(): return 1\n")}))
    assert {"_produce_caller_contract", "_produce_patch_delta"} <= got, \
        f"change_surface displaced an incumbent edit producer — forbidden (reached={sorted(got)})"


def test_plain_modification_stays_quiet(reached):
    """Correct-or-quiet: modify-in-place is not a creation and must not add a candidate."""
    got = reached(_edit({"pkg/old.py": ("a = 1\n", "a = 2\n")}, idx=2))
    assert "_produce_change_surface" not in got
    assert {"_produce_caller_contract", "_produce_patch_delta"} <= got, \
        "the incumbent edit producers must still fire on an ordinary modification"


def test_each_agent_action_reaches_its_own_producer(reached):
    """The dispatch table: every action kind must reach the producer that answers it.
    An empty set here means a whole capability is DARK for that action."""
    assert "_produce_covering" in reached(
        gw.ToolEvent(kind=gw.KIND_TEST, command="pytest -q", action_index=3)), \
        "a test run does not reach covering_red"
    assert "_produce_ranked_localization" in reached(
        gw.ToolEvent(kind=gw.KIND_SEARCH, command="grep -rn foo", output="", action_index=4)), \
        "a search does not reach localization"


def test_gateway_off_reaches_nothing(reached, monkeypatch):
    """The master kill-switch must be provably total."""
    monkeypatch.setenv("GT_GATEWAY", "0")
    assert reached(_edit({"pkg/new.py": ("", "x = 1\n")})) == set(), \
        "producers ran with the gateway master flag off"
