"""Pins for the OFFLINE-DELIVERED measurement path (`scripts/measure_brief.py`).

These lock the reader/writer contracts an adversarial pass exposed: the brief has
THREE localization formats (not one), the trajectory has TWO schemas (steps[] and
messages[]), and heredoc `/tmp` scratch writes must not steal the edit target.
Each test names the bug it guards; reverting a fix reddens it.

Hermetic: pure functions only — no graph.db, no ONNX, no repo checkout.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MOD = Path(__file__).resolve().parents[2] / "scripts" / "measure_brief.py"
_spec = importlib.util.spec_from_file_location("measure_brief", _MOD)
assert _spec and _spec.loader
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)


# ---- BUG 1: the brief's THREE producer formats all parse -------------------
def test_parse_high_edit_target_format() -> None:
    """HIGH single-target `Edit target: <file> :: <func>` (v1r_brief:2914) — the
    brief's most-confident delivery. Was scored empty before the fix."""
    brief = (
        '<gt-localization confidence="high">\n'
        "Edit target: aiomonitor/termui/commands.py :: format_running_task_list\n"
        "  guard/return to update: return tasks  [L42]\n"
        "  reason: called by monitor()\n"
        "</gt-localization>"
    )
    assert mb._parse_localization_block(brief) == ["aiomonitor/termui/commands.py"]


def test_parse_medium_numbered_list() -> None:
    brief = (
        '<gt-localization confidence="medium">\n'
        "Candidate edit targets (reason over these):\n"
        "  1. repl/repl.go — BeginRepl, Run\n"
        "  2. main.go — main\n"
        "</gt-localization>"
    )
    assert mb._parse_localization_block(brief) == ["repl/repl.go", "main.go"]


# ---- G12: leak scan over the ENTIRE brief, not just the candidate list --------
def test_g12_test_basename_outside_candidates_is_a_leak() -> None:
    """A verifier test basename in the evidence/graph-map section (OUTSIDE the
    numbered candidate list) is a leak. The old set(delivered)&set(test_files)
    check saw only the parsed candidates and MISSED it."""
    test_files = ["tests/unit/test_widget.py"]
    brief = (
        '<gt-localization confidence="medium">\n'
        "  1. src/widget.py — render\n"
        "</gt-localization>\n"
        "<gt-evidence>\n"
        "[INFO] related: tests/unit/test_widget.py exercises render()\n"
        "</gt-evidence>\n"
    )
    # the old exact-candidate check would MISS it (no test path among candidates)
    delivered = mb._parse_localization_block(brief)
    assert not (set(delivered) & set(test_files))
    # the G12 full-text scan CATCHES it
    leaks = mb._brief_leaks(brief, test_files)
    assert "tests/unit/test_widget.py" in leaks


def test_g12_test_function_name_anywhere_is_a_leak() -> None:
    brief = (
        '<gt-localization confidence="high">\n'
        "Edit target: src/auth.py :: login\n"
        "</gt-localization>\n"
        "reproduce with test_login_rejects_expired_token\n"
    )
    leaks = mb._brief_leaks(brief, [])
    assert "test_login_rejects_expired_token" in leaks


def test_g12_clean_brief_has_no_leak() -> None:
    brief = (
        '<gt-localization confidence="medium">\n'
        "  1. src/widget.py — render\n"
        "  2. src/model.py — build\n"
        "</gt-localization>\n"
    )
    assert mb._brief_leaks(brief, ["tests/unit/test_widget.py"]) == []


def test_parse_low_region_list() -> None:
    brief = (
        '<gt-localization confidence="low">\n'
        "Region: src/morphing/ — candidate edit targets (reason over these, confirm with grep):\n"
        "  1. src/morphing/facade/provider.py\n"
        "  2. src/morphing/model.py\n"
        "</gt-localization>"
    )
    assert mb._parse_localization_block(brief) == [
        "src/morphing/facade/provider.py",
        "src/morphing/model.py",
    ]


# ---- BUG 2: the trajectory's TWO schemas both yield commands ----------------
def test_iter_commands_steps_schema() -> None:
    traj = {"steps": [
        {"source": "agent", "tool_calls": [{"arguments": {"command": "grep -rn foo ."}}]},
        {"source": "user", "tool_calls": [{"arguments": {"command": "ignored"}}]},
    ]}
    assert mb._iter_agent_commands(traj) == ["grep -rn foo ."]


def test_iter_commands_mini_messages_schema() -> None:
    """Responses-API/mini: command nested under extra.actions[]. A steps[]-only
    reader silently returns nothing on the 19 mini-format tapes."""
    traj = {"messages": [
        {"role": "system", "content": "..."},
        {"extra": {"actions": [{"command": "cat pkg/core.py"}]}},
        {"type": "function_call_output", "output": "..."},
        {"extra": {"actions": [{"command": "sed -i s/a/b/ pkg/core.py"}]}},
    ]}
    assert mb._iter_agent_commands(traj) == ["cat pkg/core.py", "sed -i s/a/b/ pkg/core.py"]


# ---- BUG 4: heredoc /tmp scratch must not steal the edit target -------------
def _traj(cmds: list[str]) -> dict:
    return {"steps": [{"source": "agent", "tool_calls": [{"arguments": {"command": c}}]} for c in cmds]}


def test_heredoc_edit_target_is_the_real_file(tmp_path: Path) -> None:
    tp = tmp_path / "trajectory.json"
    import json
    tp.write_text(json.dumps(_traj([
        "cat > /tmp/patch.py << 'EOF'\nopen('aiomonitor/types.py','w')\nEOF",
    ])), encoding="utf-8")
    res = mb._agent_first_hits(str(tp), ["aiomonitor/types.py"])
    assert res["first_edit_file"] == "aiomonitor/types.py"
    assert res["first_edit_hit"] is True  # gold


def test_cat_write_is_not_a_view(tmp_path: Path) -> None:
    import json
    tp = tmp_path / "trajectory.json"
    # a `cat > file` write must not register as the first VIEW
    tp.write_text(json.dumps(_traj(["cat > pkg/real.py << 'EOF'\nx=1\nEOF"])), encoding="utf-8")
    res = mb._agent_first_hits(str(tp), ["pkg/real.py"])
    assert res["first_view_file"] is None
    assert res["first_edit_file"] == "pkg/real.py"


# ---- Latent: test-path detection is filename-anchored ----------------------
def test_testpath_anchored_not_substring() -> None:
    assert mb._TESTPATH.search("tests/test_core.py")       # a real test file
    assert mb._TESTPATH.search("pkg/core_test.go")          # go convention
    assert mb._TESTPATH.search("src/x.test.ts")             # js convention
    assert not mb._TESTPATH.search("pkg/latest_release.py")    # 'test' substring, not a test
    assert not mb._TESTPATH.search("pkg/contest.py")           # 'test' substring, not a test


def test_gold_test_named_file_is_not_filtered(tmp_path: Path) -> None:
    """A gold file that is test-NAMED (bandit/core/test_set.py — a product registry,
    not a pytest file) is the localization target and must be credited, not filtered.
    The naming heuristic yields to the gold set."""
    import json
    tp = tmp_path / "trajectory.json"
    tp.write_text(json.dumps(_traj(["cat bandit/core/test_set.py"])), encoding="utf-8")
    res = mb._agent_first_hits(str(tp), ["bandit/core/test_set.py"])
    assert res["first_view_file"] == "bandit/core/test_set.py"
    assert res["first_view_hit"] is True
    # ...but a NON-gold test file stays filtered (noise)
    tp2 = tmp_path / "t2.json"
    tp2.write_text(json.dumps(_traj(["cat tests/test_core.py"])), encoding="utf-8")
    assert mb._agent_first_hits(str(tp2), ["pkg/real.py"])["first_view_file"] is None


# ---- residual: _SCRATCH anchored so it drops only ABSOLUTE scratch ----------
def test_scratch_anchored_keeps_repo_dev_and_tmp_components() -> None:
    assert mb._SCRATCH.search("tmp/patch.py")        # absolute /tmp -> tmp/... after _np
    assert mb._SCRATCH.search("var/tmp/x.py")
    assert mb._SCRATCH.search("dev/null")
    assert not mb._SCRATCH.search("src/dev/server.js")   # repo dir component
    assert not mb._SCRATCH.search("core/tmp/cache.go")   # repo dir component
    assert not mb._SCRATCH.search("packages/dev/index.ts")


# ---- BUG 5: infra probes are not localization searches ---------------------
def test_infra_probe_not_repo_wide_search(tmp_path: Path) -> None:
    import json
    tp = tmp_path / "trajectory.json"
    tp.write_text(json.dumps(_traj([
        'find / -name "gt_root.txt" 2>/dev/null',   # infra — must be skipped
        "grep -rn Handler .",                          # first REAL repo-wide search
    ])), encoding="utf-8")
    res = mb._agent_first_hits(str(tp), ["x.py"])
    assert res["first_search_repo_wide"] is True  # set by the grep, not the find


# ---- residual: heredoc edit target is the WRITE, not a comment-named file ---
def test_edit_target_ignores_comment_named_file(tmp_path: Path) -> None:
    """A `cat > /tmp/p.py` body whose COMMENT names one file but whose write() names
    another must attribute the edit to the file actually written."""
    import json
    tp = tmp_path / "trajectory.json"
    cmd = "cat > /tmp/p.py << 'EOF'\n# first read config/settings.py for reference\nopen('app/models/user.py','w').write(x)\nEOF"
    tp.write_text(json.dumps(_traj([cmd])), encoding="utf-8")
    res = mb._agent_first_hits(str(tp), ["app/models/user.py"])
    assert res["first_edit_file"] == "app/models/user.py"   # the write target
    assert res["first_edit_hit"] is True
    # direct helper: the comment file must NOT win
    assert mb._edit_target(cmd, ["config/settings.py", "app/models/user.py"]) == "app/models/user.py"


# ---- residual: directory enumeration is NOT a localization search ------------
def test_localization_search_excludes_dir_enumeration() -> None:
    assert mb._is_localization_search("grep -rn Handler .")
    assert mb._is_localization_search('find . -name "*.go"')
    assert not mb._is_localization_search("find /app -type d -maxdepth 3")   # orientation
    assert not mb._is_localization_search('find / -name "gt_root.txt"')      # infra probe
    assert not mb._is_localization_search("ls -la src/")                       # not a search


# ---- BUG 3 + aggregate: exclusion + agent denominator ----------------------
def test_aggregate_excludes_and_counts_agent() -> None:
    reports = [
        {"stratum": "A", "language": "go", "gold_known": True, "delivered_known": True,
         "scorable": True, "gt_top1_hit": True, "gt_top3_hit": True, "gt_first_gold_rank": 1,
         "agent_first_view_hit": True, "agent_first_edit_hit": True, "has_agent_trajectory": True,
         "leak_count": 0},
        {"stratum": "A", "language": "python", "gold_known": False, "delivered_known": True,
         "scorable": False, "gt_top1_hit": None, "gt_top3_hit": None, "gt_first_gold_rank": None,
         "agent_first_view_hit": None, "agent_first_edit_hit": None, "has_agent_trajectory": False,
         "leak_count": 0},
    ]
    agg = mb.aggregate_offline(reports)
    assert agg["n_tapes"] == 2
    assert agg["n_gold_unknown"] == 1
    assert agg["overall"]["n_scorable"] == 1      # the non-scorable tape is excluded
    assert agg["overall"]["gt_hit_at_1"] == 1.0   # 1/1, not 1/2
    assert agg["overall"]["n_agent"] == 1         # only one tape had a trajectory
    assert agg["n_agent_trajectories"] == 1
