"""RED-first tests for the E5 decision-commit correctness + durability adjudicator.

Before this module existed, NOTHING in the tree decided whether a decision commit
was CORRECT, so CLAUDE.md §5's ``steps to durable correct decision commit`` and
``durable-correct-state risk difference`` endpoints were uncomputable.

The synthetic trajectories below use the EXACT mini-swe-agent message shape that
``gt_performance_metrics._parse_timeline`` reads (``tool_calls`` ->
``function.arguments`` -> a JSON string), because the adjudicator IMPORTS that
parser rather than mirroring it.  A fixture that drifted from the real shape
would silently test nothing.

The load-bearing case is (d): NO GOLD must yield ``correct=None`` everywhere and
a NAMED reason — never ``correct=False``.  Treating absence of gold as
incorrectness is the exact failure mode this module was written to prevent.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts", "swebench",
)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import decision_commit_adjudication as dca  # noqa: E402

GOLD = ["src/pkg/target.py"]


# --------------------------------------------------------------------------- #
# fixture builders — the exact shapes _parse_timeline consumes
# --------------------------------------------------------------------------- #
def _tool_call(args: dict) -> list[dict]:
    return [{"function": {"name": "bash", "arguments": json.dumps(args)}}]


def _edit(path: str) -> dict:
    """A structured str_replace edit — _is_edit_command's primary (verb) path."""
    return {
        "role": "assistant",
        "content": f"editing {path}",
        "tool_calls": _tool_call({"command": "str_replace", "path": path}),
    }


def _shell(cmd: str) -> dict:
    return {
        "role": "assistant",
        "content": cmd,
        "tool_calls": _tool_call({"command": cmd}),
    }


def _obs(text: str = "ok") -> dict:
    return {"role": "user", "content": text}


def _traj(*messages: dict) -> dict:
    return {"messages": [{"role": "system", "content": "prompt"}, *messages]}


# --------------------------------------------------------------------------- #
# (a) gold edit kept to the end -> durable correct at that step
# --------------------------------------------------------------------------- #
def test_gold_edit_kept_to_the_end_is_a_durable_correct_commit():
    traj = _traj(
        _shell("grep -rn 'thing' src/"), _obs(),
        _edit("src/pkg/target.py"), _obs(),
        _shell("python -m pytest tests/ -q"), _obs("1 passed"),
    )
    out = dca.adjudicate_decision_commits(traj, GOLD)

    assert len(out["decisions"]) == 1
    record = out["decisions"][0]
    assert record["target_file"] == "src/pkg/target.py"
    assert record["correct"] is True
    assert record["durable"] is True
    assert record["reverted_at"] is None
    assert record["durability_basis"] == dca.BASIS_NO_REVERT

    summary = out["summary"]
    assert summary["steps_to_first_durable_correct_commit"] == record["step"]
    assert summary["reason"] is None
    assert summary["n_commits"] == 1
    assert summary["n_correct"] == 1
    assert summary["n_durable"] == 1
    assert summary["gold_source"] == dca.GOLD_SOURCE_DATASET


def test_the_first_durable_correct_commit_is_the_earliest_not_the_last():
    traj = _traj(
        _edit("src/pkg/target.py"), _obs(),
        _edit("src/pkg/target.py"), _obs(),
    )
    out = dca.adjudicate_decision_commits(traj, GOLD)
    steps = [d["step"] for d in out["decisions"]]
    assert len(steps) == 2
    assert out["summary"]["steps_to_first_durable_correct_commit"] == min(steps)


# --------------------------------------------------------------------------- #
# (b) gold edit then reverted -> NOT durable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("revert_cmd", [
    "git checkout -- src/pkg/target.py",
    "git restore src/pkg/target.py",
    "git checkout -- .",
    "git revert HEAD",
])
def test_gold_edit_then_reverted_is_correct_but_not_durable(revert_cmd):
    traj = _traj(
        _edit("src/pkg/target.py"), _obs(),
        _shell(revert_cmd), _obs(),
    )
    out = dca.adjudicate_decision_commits(traj, GOLD)

    record = out["decisions"][0]
    assert record["correct"] is True, "revert does not change WHERE the agent edited"
    assert record["durable"] is False
    assert record["reverted_at"] is not None
    assert record["reverted_at"] > record["step"]
    assert record["durability_basis"] == dca.BASIS_REVERT_COMMAND

    summary = out["summary"]
    assert summary["steps_to_first_durable_correct_commit"] is None
    assert summary["reason"] == dca.REASON_NO_DURABLE_CORRECT
    assert summary["n_correct"] == 1
    assert summary["n_durable"] == 0


def test_a_revert_of_another_file_does_not_demote_this_commit():
    traj = _traj(
        _edit("src/pkg/target.py"), _obs(),
        _shell("git checkout -- src/pkg/other.py"), _obs(),
    )
    out = dca.adjudicate_decision_commits(traj, GOLD)
    record = out["decisions"][0]
    assert record["durable"] is True
    assert record["reverted_at"] is None


def test_a_revert_before_the_commit_cannot_undo_it():
    traj = _traj(
        _shell("git checkout -- src/pkg/target.py"), _obs(),
        _edit("src/pkg/target.py"), _obs(),
    )
    out = dca.adjudicate_decision_commits(traj, GOLD)
    record = out["decisions"][0]
    assert record["durable"] is True, "an EARLIER revert is not evidence about a LATER edit"
    assert record["reverted_at"] is None


def test_absence_from_the_final_patch_demotes_durability_only_when_patch_is_supplied():
    traj = _traj(_edit("src/pkg/target.py"), _obs())

    # No patch truth -> durability decided on reverts alone (fail-open, not a demotion).
    assert dca.adjudicate_decision_commits(traj, GOLD)["decisions"][0]["durable"] is True

    # Patch supplied and the target did not survive -> not durable, named basis.
    demoted = dca.adjudicate_decision_commits(
        traj, GOLD, final_patch_files=["src/pkg/other.py"],
    )["decisions"][0]
    assert demoted["durable"] is False
    assert demoted["durability_basis"] == dca.BASIS_ABSENT_FROM_PATCH
    assert demoted["reverted_at"] is None

    # Patch supplied and the target survived -> durable.
    kept = dca.adjudicate_decision_commits(
        traj, GOLD, final_patch_files=["src/pkg/target.py"],
    )["decisions"][0]
    assert kept["durable"] is True


# --------------------------------------------------------------------------- #
# (c) non-gold edit -> correct=False
# --------------------------------------------------------------------------- #
def test_non_gold_edit_is_measured_incorrect():
    traj = _traj(_edit("src/pkg/unrelated.py"), _obs())
    out = dca.adjudicate_decision_commits(traj, GOLD)

    record = out["decisions"][0]
    assert record["correct"] is False
    assert record["durable"] is True, "durability is independent of correctness"

    summary = out["summary"]
    assert summary["n_commits"] == 1
    assert summary["n_correct"] == 0
    assert summary["n_durable"] == 1
    assert summary["steps_to_first_durable_correct_commit"] is None
    assert summary["reason"] == dca.REASON_NO_DURABLE_CORRECT


def test_gold_membership_is_suffix_tolerant_like_every_other_gold_gated_section():
    """_path_match is imported, so a repo-relative target matches a bare gold name."""
    traj = _traj(_edit("src/pkg/target.py"), _obs())
    out = dca.adjudicate_decision_commits(traj, ["pkg/target.py"])
    assert out["decisions"][0]["correct"] is True


# --------------------------------------------------------------------------- #
# (d) no gold -> all None + a NAMED reason (never False)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("gold", [None, [], ["", None]])
def test_no_gold_is_unmeasured_never_incorrect(gold):
    traj = _traj(
        _edit("src/pkg/target.py"), _obs(),
        _edit("src/pkg/unrelated.py"), _obs(),
    )
    out = dca.adjudicate_decision_commits(traj, gold)

    assert [d["correct"] for d in out["decisions"]] == [None, None]
    assert all(d["correct"] is not False for d in out["decisions"])

    summary = out["summary"]
    assert summary["steps_to_first_durable_correct_commit"] is None
    assert summary["reason"] == dca.REASON_NO_GOLD
    assert summary["reason"] == "no_gold_available"
    assert summary["n_correct"] is None, "0 correct would read as MEASURED zero"
    assert summary["n_commits"] == 2
    assert summary["n_durable"] == 2
    assert summary["gold_source"] == dca.GOLD_SOURCE_NONE


def test_no_decision_commits_gets_its_own_reason():
    traj = _traj(_shell("grep -rn 'thing' src/"), _obs())
    out = dca.adjudicate_decision_commits(traj, GOLD)
    assert out["decisions"] == []
    assert out["summary"]["n_commits"] == 0
    assert out["summary"]["reason"] == dca.REASON_NO_COMMITS


def test_unreadable_trajectory_fails_closed_rather_than_raising(tmp_path):
    missing = tmp_path / "nope.json"
    out = dca.adjudicate_decision_commits(str(missing), GOLD)
    assert out["decisions"] == []
    assert out["summary"]["reason"] == dca.REASON_NO_COMMITS


def test_accepts_a_trajectory_path_a_dict_and_a_bare_message_list(tmp_path):
    messages = [_edit("src/pkg/target.py"), _obs()]
    path = tmp_path / "mini-swe-agent.trajectory.json"
    path.write_text(json.dumps({"messages": messages}), encoding="utf-8")

    from_path = dca.adjudicate_decision_commits(str(path), GOLD)
    from_dict = dca.adjudicate_decision_commits({"messages": messages}, GOLD)
    from_list = dca.adjudicate_decision_commits(messages, GOLD)
    assert from_path == from_dict == from_list


# --------------------------------------------------------------------------- #
# (e) determinism
# --------------------------------------------------------------------------- #
def test_output_is_byte_identical_across_repeated_calls():
    traj = _traj(
        _edit("src/pkg/target.py"), _obs(),
        _edit("src/pkg/unrelated.py"), _obs(),
        _shell("git checkout -- src/pkg/unrelated.py"), _obs(),
        _edit("src/pkg/target.py"), _obs(),
    )
    runs = [
        json.dumps(
            dca.adjudicate_decision_commits(traj, GOLD, final_patch_files=GOLD),
            sort_keys=True,
        )
        for _ in range(5)
    ]
    assert len(set(runs)) == 1


def test_decisions_are_ordered_by_step_then_target():
    traj = _traj(
        _edit("src/pkg/target.py"), _obs(),
        _edit("src/pkg/aaa.py"), _obs(),
        _edit("src/pkg/zzz.py"), _obs(),
    )
    out = dca.adjudicate_decision_commits(traj, GOLD)
    keys = [(d["step"], d["target_file"]) for d in out["decisions"]]
    assert keys == sorted(keys)


def test_the_adjudicator_is_pure_and_does_not_mutate_its_input():
    traj = _traj(_edit("src/pkg/target.py"), _obs())
    before = json.dumps(traj, sort_keys=True)
    dca.adjudicate_decision_commits(traj, GOLD)
    assert json.dumps(traj, sort_keys=True) == before
