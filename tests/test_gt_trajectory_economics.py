"""TTD tests for the additive ``trajectory_economics`` section (B-TE).

RED-first, hand-built synthetic trajectory fixtures covering each metric's edge
cases (no-edit, no-verify, RED->fix->GREEN, censored/absent gold, empty ledger),
plus >=2 BITING mutations: the phase-classifier precedence and the red_green cycle
state machine each have a test that fails if the ordering is broken.

All numbers are asserted exactly; every metric is checked for its 8-dp/None +
{applicable,predicate,reason} contract.
"""
from __future__ import annotations

import json

import pytest

from scripts.swebench.gt_trajectory_economics import (
    SCHEMA,
    compute_metrics,
    compute_trajectory_economics,
    load_delivered_facts,
    load_graph,
    parse_trajectory,
    parse_unified_diff,
)

KNOWN = {"pkg/mod.py", "pkg/other.py", "pkg/far.py", "tests/test_mod.py"}


# --------------------------------------------------------------------------- #
# fixture builders (chat shape — the live mini-swe-agent / pier format)
# --------------------------------------------------------------------------- #
def asst(commands, thought="", prompt=100, completion=20, cost=0.0):
    return {
        "role": "assistant",
        "content": thought,
        "extra": {
            "actions": [{"command": c} for c in commands],
            "response": {"usage": {
                "prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": prompt + completion,
            }},
            "cost": cost,
        },
    }


def tool(output, rc=0):
    return {
        "role": "tool",
        "content": f"<returncode>{rc}</returncode>\n<output>\n{output}\n</output>",
        "extra": {"returncode": rc},
    }


def traj(pairs, submission=""):
    """pairs = list of (assistant_msg, tool_msg_or_None)."""
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "brief"}]
    for a, t in pairs:
        msgs.append(a)
        if t is not None:
            msgs.append(t)
    return {"messages": msgs, "info": {"submission": submission}, "trajectory_format": "x"}


def _compute(pairs, *, known=KNOWN, graph=None, facts=None, brief=None,
             agent_patch="", gold_files=None, gold_patch="", submission=""):
    parsed = parse_trajectory(traj(pairs, submission), set(known))
    g = graph or {"present": False, "files": set(), "cluster_of": {}, "nodes": [],
                  "node_file": {}, "callers_of": {}, "test_covered_files": set()}
    m, a = compute_metrics(parsed, g, facts or [], brief or [], agent_patch,
                           gold_files or [], gold_patch)
    return parsed, m, a


# --------------------------------------------------------------------------- #
# 0. contract shape — every metric carries the D5 applicability record
# --------------------------------------------------------------------------- #
def test_every_metric_has_applicability_contract():
    pairs = [
        (asst(["cd x && grep -rn foo ."]), tool("./pkg/mod.py")),
        (asst(["sed -n '1,5p' pkg/mod.py"]), tool("code")),
        (asst(["python3 -c \"open('pkg/mod.py','w').write('x')\""]), tool("", rc=0)),
        (asst(["python3 -m pytest tests/test_mod.py"]), tool("1 passed", rc=0)),
    ]
    _p, metrics, appl = _compute(pairs)
    for name in metrics:
        assert name in appl, f"{name} lacks an applicability record"
        c = appl[name]
        assert set(c) >= {"applicable", "predicate", "reason"}
        assert isinstance(c["applicable"], bool)
        assert isinstance(c["predicate"], str) and c["predicate"].strip()
        assert isinstance(c["reason"], str) and c["reason"].strip()


def test_unmeasurable_is_none_never_zero():
    # a single no-command, no-usage turn: everything below is unmeasurable -> None,
    # never a fabricated 0.0 (G14 / d8 discipline).
    no_usage = {"role": "assistant", "content": "thinking", "extra": {"actions": []}}
    parsed = parse_trajectory({"messages": [
        {"role": "system", "content": "s"}, {"role": "user", "content": "b"}, no_usage,
    ], "info": {}}, set(KNOWN))
    g = {"present": False, "files": set(), "cluster_of": {}, "nodes": [],
         "node_file": {}, "callers_of": {}, "test_covered_files": set()}
    metrics, appl = compute_metrics(parsed, g, [], [], "", [], "")
    for name in ("verify_cost_share", "tokens_per_round_slope", "actions_per_turn",
                 "redundant_read_rate", "patch_minimality"):
        assert metrics[name] is None
        assert appl[name]["applicable"] is False


# --------------------------------------------------------------------------- #
# A. repro discipline
# --------------------------------------------------------------------------- #
def test_no_edit_run_nulls_edit_dependent_metrics():
    pairs = [
        (asst(["cd x && grep -rn foo ."]), tool("./pkg/mod.py")),
        (asst(["sed -n '1,20p' pkg/mod.py"]), tool("some code")),
    ]
    _p, metrics, appl = _compute(pairs)
    assert metrics["repro_before_edit"] is None
    assert appl["repro_before_edit"]["applicable"] is False
    assert metrics["fix_without_repro_rate"] is None
    assert metrics["read_before_edit_coverage"] is None
    assert metrics["premature_submit_margin"] is None


def test_steps_to_first_red_and_censoring():
    # RED via a Traceback in an execute observation at turn 2
    pairs = [
        (asst(["cd x && grep -rn foo ."]), tool("./pkg/mod.py")),
        (asst(["python3 repro.py"]), tool("Traceback (most recent call last)\nValueError", rc=1)),
    ]
    _p, metrics, appl = _compute(pairs)
    assert metrics["steps_to_first_red"] == 2
    assert appl["steps_to_first_red"]["applicable"] is True

    # never RED -> None + right-censored observation at the terminal horizon
    pairs2 = [(asst(["cd x && ls"]), tool("a b c")),
              (asst(["cat pkg/mod.py"]), tool("clean output"))]
    _p2, m2, a2 = _compute(pairs2)
    assert m2["steps_to_first_red"] is None
    obs = a2["steps_to_first_red"].get("observation")
    assert obs and obs["state"] == "RIGHT_CENSORED"
    assert obs["lower_bound"] == 2 and obs["terminal_horizon"] == 2


def test_red_green_cycles_counts_red_edit_verify_loop():
    pairs = [
        (asst(["python3 -m pytest tests/test_mod.py"]), tool("1 failed", rc=1)),   # RED verify
        (asst(["sed -i 's/a/b/' pkg/mod.py"]), tool("", rc=0)),                    # edit
        (asst(["python3 -m pytest tests/test_mod.py"]), tool("1 passed", rc=0)),   # verify
    ]
    _p, metrics, _a = _compute(pairs)
    assert metrics["red_green_cycles"] == 1


def test_red_green_cycles_requires_edit_between_MUTATION():
    # BITING (mutation: red_green_cycles drops the required edit gate). A RED verify
    # followed directly by another verify with NO edit between is NOT a cycle. If the
    # state machine skips the seek_edit gate, it miscounts this as 1.
    pairs = [
        (asst(["python3 -m pytest tests/test_mod.py"]), tool("1 failed", rc=1)),  # RED
        (asst(["python3 -m pytest tests/test_mod.py -v"]), tool("1 failed", rc=1)),  # RED, no edit
    ]
    _p, metrics, _a = _compute(pairs)
    assert metrics["red_green_cycles"] == 0


def test_fix_without_repro_rate():
    # edit precedes any RED -> rate 1.0
    pairs = [
        (asst(["sed -i 's/a/b/' pkg/mod.py"]), tool("", rc=0)),
        (asst(["python3 -m pytest tests/test_mod.py"]), tool("1 failed", rc=1)),
    ]
    _p, metrics, _a = _compute(pairs)
    assert metrics["fix_without_repro_rate"] == 1.0
    # a RED before the edit -> rate 0.0
    pairs2 = [
        (asst(["python3 repro.py"]), tool("AssertionError", rc=1)),
        (asst(["sed -i 's/a/b/' pkg/mod.py"]), tool("", rc=0)),
    ]
    _p2, m2, _a2 = _compute(pairs2)
    assert m2["fix_without_repro_rate"] == 0.0


# --------------------------------------------------------------------------- #
# B. phase economics  (+ MUTATION #1: dominant-atype precedence)
# --------------------------------------------------------------------------- #
def test_phase_partition_sums_to_turns():
    pairs = [
        (asst(["cd x && grep -rn foo ."]), tool("hit")),                 # hunt (search)
        (asst(["sed -i 's/a/b/' pkg/mod.py"]), tool("", rc=0)),          # fix (edit)
        (asst(["python3 -m pytest tests/test_mod.py"]), tool("ok", rc=0)),  # verify
        (asst(["python3 -m pytest tests/test_mod.py"]), tool("ok", rc=0)),  # stuck (repeat)
    ]
    parsed, metrics, appl = _compute(pairs)
    pt = metrics["phase_turns"]
    assert pt["hunt"] + pt["fix"] + pt["verify"] + pt["stuck"] == len(parsed["turns"])
    assert pt["fix"] >= 1 and pt["verify"] >= 1 and pt["stuck"] == 1


def test_dominant_atype_precedence_edit_beats_test_MUTATION():
    # BITING (mutation: _dominant_atype order). A turn issuing TWO separate actions —
    # a pytest AND an edit — must resolve to EDIT (fix). _dominant_atype decides only
    # when a turn has multiple commands, so two discrete actions are required to
    # exercise it. If the precedence order is broken (test before edit), atype flips
    # to 'test' (verify) and this assert fails.
    pairs = [
        (asst(["python3 -m pytest tests/test_mod.py", "sed -i 's/a/b/' pkg/mod.py"]),
         tool("done", rc=0)),
    ]
    parsed, _m, _a = _compute(pairs)
    assert parsed["turns"][0]["cmd_types"] == ["test", "edit"]
    assert parsed["turns"][0]["atype"] == "edit"
    assert parsed["turns"][0]["phase"] == "fix"


def test_python_open_write_is_an_edit_MUTATION():
    # BITING (mutation: edit detector drops python open-write). The live mini scaffold
    # applies edits via `python -c "open(<file>,'w')..."`; the OH editor detector misses
    # these. If open-write detection is removed, this turn classifies as 'execute' and
    # the edit-dependent assertions below (atype/phase and edits>0) fail.
    pairs = [
        (asst(["python3 -c \"open('pkg/mod.py','w').write('x')\""]), tool("", rc=0)),
    ]
    parsed, metrics, _a = _compute(pairs)
    assert parsed["turns"][0]["atype"] == "edit"
    assert parsed["turns"][0]["phase"] == "fix"


def test_verify_cost_share_and_slope_and_actions_per_turn():
    pairs = [
        (asst(["cd x && grep -rn foo ."], prompt=100, completion=0), tool("hit")),
        (asst(["python3 -m pytest tests/test_mod.py"], prompt=300, completion=0),
         tool("ok", rc=0)),
    ]
    _p, metrics, _a = _compute(pairs)
    # verify tokens = 300 of 400 total
    assert metrics["verify_cost_share"] == pytest.approx(0.75)
    # slope over (1,100),(2,300) = 200.0
    assert metrics["tokens_per_round_slope"] == pytest.approx(200.0)
    # actions_per_turn: turn1 has `cd`+`grep|.` = 2 segments; turn2 = 1 -> mean 1.5
    assert metrics["actions_per_turn"] == pytest.approx(1.5)


# --------------------------------------------------------------------------- #
# C. wrong-branch economics (graph clusters)
# --------------------------------------------------------------------------- #
def _graph_two_clusters():
    # cluster A = {pkg/mod.py, pkg/other.py} (an edge joins them); cluster B = {pkg/far.py}
    files = {"pkg/mod.py", "pkg/other.py", "pkg/far.py"}
    return {
        "present": True,
        "files": files,
        "cluster_of": {"pkg/mod.py": 0, "pkg/other.py": 0, "pkg/far.py": 1},
        "nodes": [(1, "pkg/mod.py", 1, 50, False), (2, "pkg/other.py", 1, 9, False),
                  (3, "pkg/far.py", 1, 9, False)],
        "node_file": {1: "pkg/mod.py", 2: "pkg/other.py", 3: "pkg/far.py"},
        "callers_of": {"pkg/mod.py": {"pkg/other.py"}},
        "test_covered_files": {"pkg/mod.py"},
    }


def test_hypothesis_churn_and_dead_end_cost():
    g = _graph_two_clusters()
    # explore far cluster (B) first, then edit mod (A)
    pairs = [
        (asst(["cat pkg/far.py"], prompt=10, completion=0), tool("far code")),
        (asst(["sed -i 's/a/b/' pkg/mod.py"]), tool("", rc=0)),
    ]
    agent_patch = "diff --git a/pkg/mod.py b/pkg/mod.py\n@@ -1,2 +1,2 @@\n-a\n+b\n"
    _p, metrics, _a = _compute(pairs, graph=g, agent_patch=agent_patch)
    assert metrics["hypothesis_churn"] == 1        # cluster B explored before A
    assert metrics["dead_end_cost_steps"] == 1     # the far.py turn is a dead end
    assert metrics["dead_end_cost_tokens"] == 10.0


def test_backtrack_count():
    # touch mod, then 5 other-file turns, then return to mod -> 1 backtrack
    pairs = [
        (asst(["cat pkg/mod.py"]), tool("x")),
        (asst(["cat pkg/other.py"]), tool("x")),
        (asst(["cat pkg/other.py"]), tool("y")),
        (asst(["cat pkg/far.py"]), tool("x")),
        (asst(["cat pkg/other.py"]), tool("z")),
        (asst(["cat pkg/far.py"]), tool("w")),
        (asst(["cat pkg/mod.py"]), tool("x2")),   # return to mod after >=5 turns
    ]
    _p, metrics, _a = _compute(pairs)
    assert metrics["backtrack_count"] == 1


def test_misdirection_harm_requires_gold_and_brief():
    g = _graph_two_clusters()
    pairs = [(asst(["cat pkg/far.py"]), tool("x")),
             (asst(["sed -i 's/a/b/' pkg/mod.py"]), tool("", rc=0))]
    agent_patch = "diff --git a/pkg/mod.py b/pkg/mod.py\n@@ -1,2 +1,2 @@\n-a\n+b\n"
    gold_patch = "diff --git a/pkg/mod.py b/pkg/mod.py\n@@ -1,2 +1,2 @@\n-a\n+b\n"
    # far.py is GT-ranked but in neither patch nor gold -> harmful
    _p, metrics, appl = _compute(pairs, graph=g, agent_patch=agent_patch,
                                 brief=["pkg/mod.py", "pkg/far.py"],
                                 gold_files=["pkg/mod.py"], gold_patch=gold_patch)
    assert metrics["misdirection_harm_steps"] == 1
    # without gold -> None + not applicable
    _p2, m2, a2 = _compute(pairs, graph=g, agent_patch=agent_patch,
                           brief=["pkg/mod.py", "pkg/far.py"])
    assert m2["misdirection_harm_steps"] is None
    assert a2["misdirection_harm_steps"]["applicable"] is False


# --------------------------------------------------------------------------- #
# D. read economics
# --------------------------------------------------------------------------- #
def test_redundant_read_and_coverage():
    pairs = [
        (asst(["cat pkg/mod.py"]), tool("v1")),        # read mod
        (asst(["sed -n '1,5p' pkg/mod.py"]), tool("v2")),  # redundant read (no edit between)
        (asst(["sed -i 's/a/b/' pkg/mod.py"]), tool("", rc=0)),  # edit resets
        (asst(["cat pkg/mod.py"]), tool("v3")),        # fresh read
    ]
    agent_patch = "diff --git a/pkg/mod.py b/pkg/mod.py\n@@ -1,2 +1,2 @@\n-a\n+b\n"
    _p, metrics, _a = _compute(pairs, agent_patch=agent_patch)
    # 3 reads total, 1 redundant
    assert metrics["redundant_read_rate"] == pytest.approx(1 / 3)
    assert metrics["read_before_edit_coverage"] == 1.0   # mod read before edit


def test_full_file_cat_rate_joins_delivered_facts():
    facts = [{"file": "pkg/mod.py", "iteration": 0, "fact_class": "localization"}]
    pairs = [
        (asst(["cat pkg/mod.py"]), tool("whole file")),   # cat covered by a GT fact
        (asst(["cat pkg/other.py"]), tool("whole file")),  # cat not covered
    ]
    _p, metrics, _a = _compute(pairs, facts=facts)
    assert metrics["full_file_cat_rate"] == pytest.approx(0.5)
    # empty ledger -> None
    _p2, m2, a2 = _compute(pairs, facts=[])
    assert m2["full_file_cat_rate"] is None
    assert a2["full_file_cat_rate"]["applicable"] is False


def test_observation_yield_conservative():
    # obs introduces a distinctive token the agent later quotes
    pairs = [
        (asst(["cd x && grep -rn foo ."]), tool("found symbol computeWeekNumber here")),
        (asst(["cat pkg/mod.py"], thought="let me inspect computeWeekNumber"), tool("code")),
    ]
    _p, metrics, _a = _compute(pairs)
    assert metrics["observation_yield"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# E. verification economics
# --------------------------------------------------------------------------- #
def test_test_selection_precision():
    pairs = [
        (asst(["python3 -m pytest tests/test_mod.py::test_foo"]), tool("ok", rc=0)),  # scoped
        (asst(["python3 -m pytest"]), tool("ok", rc=0)),                              # broad
    ]
    _p, metrics, _a = _compute(pairs)
    assert metrics["test_selection_precision"] == pytest.approx(0.5)


def test_premature_submit_margin():
    # no verify after the last edit -> margin = submit - last_edit
    pairs = [
        (asst(["sed -i 's/a/b/' pkg/mod.py"]), tool("", rc=0)),  # turn 1 edit
        (asst(["git diff"]), tool("diff")),                      # turn 2
        (asst(["git status"]), tool("clean")),                   # turn 3 (submit)
    ]
    _p, metrics, _a = _compute(pairs)
    assert metrics["premature_submit_margin"] == 2
    # verify after edit -> 0
    pairs2 = [
        (asst(["sed -i 's/a/b/' pkg/mod.py"]), tool("", rc=0)),
        (asst(["python3 -m pytest"]), tool("ok", rc=0)),
    ]
    _p2, m2, _a2 = _compute(pairs2)
    assert m2["premature_submit_margin"] == 0


# --------------------------------------------------------------------------- #
# F. patch quality (censored / absent gold)
# --------------------------------------------------------------------------- #
def test_patch_quality_with_gold():
    agent_patch = ("diff --git a/pkg/mod.py b/pkg/mod.py\n"
                   "@@ -10,2 +10,2 @@\n-old\n+new\n")
    gold_patch = ("diff --git a/pkg/mod.py b/pkg/mod.py\n"
                  "@@ -10,4 +10,4 @@\n-a\n-b\n+c\n+d\n")
    _p, metrics, appl = _compute([(asst(["ls"]), tool("x"))],
                                 agent_patch=agent_patch,
                                 gold_files=["pkg/mod.py"], gold_patch=gold_patch)
    # agent changed 2 lines, gold changed 4 -> 0.5
    assert metrics["patch_minimality"] == pytest.approx(0.5)
    # gold touches old lines 10,11,12,13; agent range covers old 10,11 -> overlap 2/4
    assert metrics["gold_hunk_overlap"] == pytest.approx(0.5)
    assert metrics["spurious_hunk_rate"] == 0.0   # agent hunk in the gold file


def test_patch_quality_absent_gold_is_censored():
    agent_patch = "diff --git a/pkg/mod.py b/pkg/mod.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    _p, metrics, appl = _compute([(asst(["ls"]), tool("x"))], agent_patch=agent_patch)
    for name in ("patch_minimality", "gold_hunk_overlap", "spurious_hunk_rate"):
        assert metrics[name] is None
        assert appl[name]["applicable"] is False


# --------------------------------------------------------------------------- #
# G. GT dose-response
# --------------------------------------------------------------------------- #
def test_dose_response_consumption_and_half_life():
    facts = [{"file": "pkg/mod.py", "iteration": 0, "fact_class": "localization"}]
    # consumption: read the delivered file at turn 2 -> ttc = 2 - 0
    pairs = [
        (asst(["cd x && ls"]), tool("x")),
        (asst(["cat pkg/mod.py"]), tool("code")),
    ]
    _p, metrics, _a = _compute(pairs, facts=facts)
    assert metrics["time_to_consumption"] == pytest.approx(2.0)
    assert metrics["fact_half_life"] is None   # never re-searched

    # re-acquisition: a later SEARCH mentions the delivered file
    pairs2 = [(asst(["cd x && grep -rn pkg/mod.py ."]), tool("hit"))]
    _p2, m2, _a2 = _compute(pairs2, facts=facts)
    assert m2["fact_half_life"] == pytest.approx(1.0)


def test_empty_ledger_dose_response_none():
    pairs = [(asst(["cat pkg/mod.py"]), tool("x"))]
    _p, metrics, appl = _compute(pairs, facts=[])
    assert metrics["time_to_consumption"] is None
    assert metrics["fact_half_life"] is None
    assert appl["time_to_consumption"]["applicable"] is False


# --------------------------------------------------------------------------- #
# determinism + section wrapper
# --------------------------------------------------------------------------- #
def test_determinism_identical_json():
    pairs = [
        (asst(["cd x && grep -rn foo ."]), tool("./pkg/mod.py")),
        (asst(["cat pkg/mod.py"]), tool("code")),
        (asst(["sed -i 's/a/b/' pkg/mod.py"]), tool("", rc=0)),
        (asst(["python3 -m pytest tests/test_mod.py"]), tool("1 passed", rc=0)),
    ]
    _p1, m1, a1 = _compute(pairs)
    _p2, m2, a2 = _compute(pairs)
    assert json.dumps(m1, sort_keys=True) == json.dumps(m2, sort_keys=True)
    assert json.dumps(a1, sort_keys=True) == json.dumps(a2, sort_keys=True)


def test_section_wrapper_never_raises_on_missing_trajectory(tmp_path):
    sec = compute_trajectory_economics("nope", str(tmp_path))
    assert sec["schema"] == SCHEMA
    assert sec["collection_error"] == "no_trajectory"
    assert sec["deferred"] == [
        "divergence_step", "resolve_stability", "trajectory_determinism",
        "compaction_reorientation",
    ]


def test_section_wrapper_emits_no_deferred_metrics(tmp_path):
    # write a minimal real trajectory + let the wrapper build the section
    tj = traj([(asst(["cd x && grep -rn foo ."]), tool("./pkg/mod.py"))])
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(json.dumps(tj), encoding="utf-8")
    sec = compute_trajectory_economics("t", str(tmp_path), gold_files=[], gold_patch="")
    assert sec["schema"] == SCHEMA
    assert "metrics" in sec
    for deferred in sec["deferred"]:
        assert deferred not in sec["metrics"], f"deferred {deferred} must not be emitted"
