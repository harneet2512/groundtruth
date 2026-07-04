"""TTD tests for trajectory-derived per-run + per-layer metrics.

Artifact-first: parts (A) and (B) read REAL frozen beets trajectories
(baseline + GT). Parts (C) and (D) use synthetic fixtures that encode the NEW
markers, corruption, and leak conditions — red-before-green is enforced because
the detectors must (i) fire on the synthetic positives and (ii) NOT fire on the
OLD real GT run that lacks the new markers.

These tests are deterministic and never call an LLM.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from groundtruth.metrics.run_metrics import (
    compute_run_metrics,
    emit_run_metrics,
    paired_deltas,
    two_sided_view,
)

# ---------------------------------------------------------------------------
# Real artifact locations (frozen runs committed to the working tree).
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
_REL = (
    "results/SWE-bench-Live__SWE-bench-Live-lite/CodeActAgent/"
    "deepseek-v4-flash_maxiter_100/output.jsonl"
)
BASELINE_OUTPUT = REPO_ROOT / ".tmp_run_20_baseline" / "task-beetbox__beets-5495" / _REL
GT_OUTPUT = REPO_ROOT / ".tmp_run_l1_contracts" / "task-beetbox__beets-5495" / _REL
GOLD_FILES = ["beets/importer.py"]

_real_artifacts = pytest.mark.skipif(
    not (BASELINE_OUTPUT.exists() and GT_OUTPUT.exists()),
    reason="real beets trajectory artifacts not present",
)


# ===========================================================================
# (A) REAL beets BASELINE
# ===========================================================================
@_real_artifacts
class TestRealBaseline:
    def test_baseline_navigation(self, capsys):
        m = compute_run_metrics(str(BASELINE_OUTPUT), GOLD_FILES)
        nav = m["navigation"]
        out = m["outcome"]

        assert nav["gold_reached"] is True
        assert isinstance(nav["reach_to_gold_action"], int)
        assert nav["reach_to_gold_action"] <= 8
        assert nav["first_edit_action"] is not None
        assert nav["action_count"] > 0
        assert out["n_history_events"] == 153

        # PRINT captured real numbers (visible with -v -s).
        print(
            f"\n[BASELINE] reach_to_gold_action={nav['reach_to_gold_action']} "
            f"edit_to_gold_action={nav['edit_to_gold_action']} "
            f"action_count={nav['action_count']} "
            f"first_edit_action={nav['first_edit_action']} "
            f"error_count={nav['error_count']}"
        )

    def test_baseline_known_values(self):
        # Lock the exact captured values so a parser regression is caught.
        m = compute_run_metrics(str(BASELINE_OUTPUT), GOLD_FILES)
        nav = m["navigation"]
        assert nav["action_count"] == 48
        assert nav["reach_to_gold_action"] == 2
        assert nav["edit_to_gold_action"] == 23
        assert nav["first_edit_action"] == 23
        assert nav["error_count"] == 51
        assert m["outcome"]["patch_touches_gold"] is True


# ===========================================================================
# (B) REAL beets GT
# ===========================================================================
@_real_artifacts
class TestRealGT:
    def test_gt_brief_and_layers(self):
        m = compute_run_metrics(str(GT_OUTPUT), GOLD_FILES)
        assert m["brief_delivered"] is True
        pl = m["per_layer"]
        assert pl["L1_brief"]["delivered"] is True

        # nav metrics compute
        nav = m["navigation"]
        assert isinstance(nav["action_count"], int) and nav["action_count"] > 0
        assert nav["gold_reached"] is True

        # NEW markers are ABSENT in this OLD run — detector must NOT false-positive.
        assert pl["L3b_similar"]["twin_delivered"] is False
        assert pl["L1_brief"]["edit_target_contracts_delivered"] is False

        # curation leak must be absent on the real run.
        assert pl["curation"]["delivered"] is False

        print(
            f"\n[GT] reach_to_gold_action={nav['reach_to_gold_action']} "
            f"edit_to_gold_action={nav['edit_to_gold_action']} "
            f"action_count={nav['action_count']} "
            f"first_edit_action={nav['first_edit_action']} "
            f"error_count={nav['error_count']}"
        )

    def test_gt_known_values(self):
        m = compute_run_metrics(str(GT_OUTPUT), GOLD_FILES)
        nav = m["navigation"]
        assert nav["action_count"] == 44
        assert nav["reach_to_gold_action"] == 2
        assert nav["edit_to_gold_action"] == 17
        assert nav["first_edit_action"] == 17
        assert m["outcome"]["n_history_events"] == 94
        assert m["regression_guards"]["boundary_corruption_count"] == 0
        assert m["regression_guards"]["empty_contract_count"] == 0

    def test_gt_homonym_vs_cross_file_ref(self):
        # #44 fix: the real beets GT run has NO true homonym (the [GT] Called by:
        # lines list cross-file CALLERS, not the grepped symbol's definition in
        # another file). homonym_count must now be 0 (was overstated at 14),
        # while cross_file_ref_count keeps the loose cross-file signal (~14).
        m = compute_run_metrics(str(GT_OUTPUT), GOLD_FILES)
        gi = m["per_layer"]["grep_intercept"]
        assert gi["homonym_count"] == 0
        assert gi["cross_file_ref_count"] == 14
        # two-sided view surfaces both on the GT (production) side.
        gs = m["two_sided"]["gt_side"]
        assert gs["homonym_count"] == 0
        assert gs["cross_file_ref_count"] == 14
        # reach unchanged by Change 1 on the real run.
        assert m["two_sided"]["agent_side"]["reach_to_gold_action"] == 2


# ===========================================================================
# Synthetic fixture builders
# ===========================================================================
def _write_instance(path: Path, history: list[dict], *, git_patch: str = "",
                    instance_id: str = "synthetic__demo-1") -> str:
    inst = {
        "instance_id": instance_id,
        "instruction": "fix the bug",
        "history": history,
        "test_result": {"git_patch": git_patch},
        "instance": {},
        "metadata": {},
        "metrics": {},
        "error": None,
    }
    path.write_text(json.dumps(inst) + "\n", encoding="utf-8")
    return str(path)


def _obs(content: str, *, observation: str = "run", source: str = "agent",
         eid: int = 0) -> dict:
    return {
        "id": eid, "timestamp": "t", "source": source,
        "message": "tool result", "observation": observation,
        "content": content, "extras": {},
    }


def _action(action: str, *, path: str = "", command: str = "", eid: int = 0,
            new_str: str = "") -> dict:
    args = {}
    if path:
        args["path"] = path
    if command:
        args["command"] = command
    if new_str:
        args["new_str"] = new_str
    return {
        "id": eid, "timestamp": "t", "source": "agent",
        "message": "act", "action": action, "args": args,
    }


# ===========================================================================
# (C) SYNTHETIC fixtures: new markers, corruption, clean, leak
# ===========================================================================
class TestSyntheticNewMarkers:
    def test_new_markers_all_detected(self, tmp_path: Path):
        new_marker_blob = (
            "EDIT-TARGET CONTRACTS\n"
            "  set_fields -> calls set_parse(self, key, string: str)"
            "  [beets/dbcore/db.py:722]\n"
            "[TWIN] set_fields() also defined at importer.py:1071\n"
            'Calls into: set_parse(self, key, string: str) (beets/dbcore/db.py)\n'
            "[GT_VERIFY] Run: pytest test/test_importer.py\n"
        )
        history = [
            {"id": 1, "source": "user", "action": "message",
             "message": "<gt-task-brief>\n1. beets/dbcore/db.py (def set_parse)\n"
                        "EDIT-TARGET CONTRACTS\n  set_fields -> calls set_parse"},
            _action("read", path="/ws/beets/dbcore/db.py", eid=2),
            _obs(new_marker_blob, observation="read", eid=3),
            _action("run", command="pytest test/test_importer.py", eid=4),
        ]
        out = tmp_path / "synthetic_new.jsonl"
        _write_instance(out, history)

        m = compute_run_metrics(str(out), ["beets/dbcore/db.py"])
        pl = m["per_layer"]
        assert pl["L1_brief"]["edit_target_contracts_delivered"] is True
        assert pl["L3b_similar"]["twin_delivered"] is True
        assert pl["L3_postedit"]["callee_sig_delivered"] is True
        assert pl["L6_verify"]["delivered"] is True
        assert pl["L6_verify"]["test_targets_delivered"] is True

    def test_corrupted_fixture(self, tmp_path: Path):
        corrupt_blob = (
            "some code line\n"
            "[CATCHEValueError raised here with no closing bracket\n"
            "guard_clause: \n"
            "more text\n"
        )
        history = [_obs(corrupt_blob, observation="run", eid=1)]
        out = tmp_path / "synthetic_corrupt.jsonl"
        _write_instance(out, history)

        m = compute_run_metrics(str(out), ["x.py"])
        g = m["regression_guards"]
        assert g["boundary_corruption_count"] >= 1
        assert g["empty_contract_count"] >= 1

    def test_clean_fixture(self, tmp_path: Path):
        clean_blob = (
            "[SIGNATURE] def set_parse(self, key, string: str)\n"
            "[BEHAVIORAL CONTRACT] guards on key presence\n"
            "PRESERVE: returns the parsed value\n"
        )
        history = [_obs(clean_blob, observation="run", eid=1)]
        out = tmp_path / "synthetic_clean.jsonl"
        _write_instance(out, history)

        m = compute_run_metrics(str(out), ["x.py"])
        g = m["regression_guards"]
        assert g["boundary_corruption_count"] == 0
        assert g["empty_contract_count"] == 0

    def test_curation_leak_detected(self, tmp_path: Path):
        leak_blob = (
            "normal output\n"
            "[GT_CURATION] internal ranking debug that should be on stderr\n"
        )
        history = [_obs(leak_blob, observation="run", eid=1)]
        out = tmp_path / "synthetic_leak.jsonl"
        _write_instance(out, history)

        m = compute_run_metrics(str(out), ["x.py"])
        assert m["per_layer"]["curation"]["delivered"] is True

    def test_grep_intercept_homonym(self, tmp_path: Path):
        # #44 defect: agent greps `set_fields` in importer.py; GT surfaces the
        # SAME symbol defined in a DIFFERENT file (zero.py::set_fields) -> a true
        # homonym. The "defined at" prose form must also count.
        history = [
            _action("run",
                    command='grep -n "set_fields" /ws/beets/importer.py', eid=1),
            _obs(
                "match line in importer.py\n"
                "[GT] Called by: set_fields defined at beets/zero.py:10\n"
                "[SIGNATURE] beets/zero.py::set_fields(self, lib)\n",
                observation="run", eid=2,
            ),
        ]
        out = tmp_path / "synthetic_grep.jsonl"
        _write_instance(out, history)

        m = compute_run_metrics(str(out), ["beets/importer.py"])
        gi = m["per_layer"]["grep_intercept"]
        assert gi["delivered"] is True
        # The grepped symbol set_fields is asserted defined in zero.py != importer.py.
        assert gi["homonym_count"] >= 1
        # cross_file_ref_count is the loose signal (zero.py differs from the
        # grepped file) and stays present alongside the true-homonym count.
        assert gi["cross_file_ref_count"] >= 1

    def test_grep_intercept_callers_are_not_homonyms(self, tmp_path: Path):
        # The REAL beets pattern: grep set_fields in importer.py, GT lists
        # cross-file CALLERS (file::caller_func). The token after '::' is the
        # CALLER's name, NOT the grepped symbol -> NOT a homonym. homonym_count
        # must be 0 (correct-or-quiet) while cross_file_ref_count stays > 0.
        history = [
            _action("run",
                    command='grep -n "set_fields" /ws/beets/importer.py', eid=1),
            _obs(
                "    def set_fields(self, lib):\n"
                "[GT] Called by: test/test_importer.py (33x), "
                "beetsplug/convert.py::encode (2x), "
                "beets/ui/commands.py::func (3x)\n",
                observation="run", eid=2,
            ),
        ]
        out = tmp_path / "synthetic_grep_callers.jsonl"
        _write_instance(out, history)

        m = compute_run_metrics(str(out), ["beets/importer.py"])
        gi = m["per_layer"]["grep_intercept"]
        assert gi["delivered"] is True
        assert gi["homonym_count"] == 0          # no true homonym
        assert gi["cross_file_ref_count"] >= 1   # legitimate cross-file callers


# ===========================================================================
# (C2) reach_to_gold_action rigorous path-suffix matching (Change 1)
# ===========================================================================
class TestReachGoldMatching:
    def test_notimporter_does_not_reach_importer(self, tmp_path: Path):
        # Bare-basename gold "importer.py". A read of "notimporter.py" must NOT
        # register as reaching gold (the old loose substring test false-fired
        # because "importer.py" is a substring of "notimporter.py").
        history = [
            _action("run", command="cat src/notimporter.py", eid=1),
            _obs("contents of notimporter", observation="run", eid=2),
        ]
        out = _write_instance(tmp_path / "notimporter.jsonl", history)
        m = compute_run_metrics(out, ["importer.py"])
        nav = m["navigation"]
        assert nav["reach_to_gold_action"] is None
        assert nav["gold_reached"] is False

    def test_real_grep_and_sed_reach_importer(self, tmp_path: Path):
        # A real grep and a sed targeting beets/importer.py MUST register as
        # reaching gold "importer.py" (bare basename).
        for cmd in (
            'grep -n "set_fields" beets/importer.py',
            "sed -n '600,620p' beets/importer.py",
        ):
            history = [
                _action("run", command=cmd, eid=1),
                _obs("...", observation="run", eid=2),
            ]
            out = _write_instance(tmp_path / "reach.jsonl", history)
            m = compute_run_metrics(out, ["importer.py"])
            assert m["navigation"]["reach_to_gold_action"] == 1, cmd
            assert m["navigation"]["gold_reached"] is True, cmd


# ===========================================================================
# (C3) two_sided_view: gt_side / agent_side grouping (Change 3)
# ===========================================================================
class TestTwoSidedView:
    def test_two_sided_keys_and_placement(self, tmp_path: Path):
        history = [
            {"id": 1, "source": "user", "action": "message",
             "message": "<gt-task-brief>\n1. beets/importer.py (def set_fields)"},
            _action("run", command="grep -n set_fields beets/importer.py", eid=2),
            _obs("def set_fields(self, lib):", observation="run", eid=3),
            _action("edit", path="/ws/beets/importer.py", eid=4),
            _obs("ok", observation="edit", eid=5),
        ]
        out = _write_instance(tmp_path / "ts.jsonl", history)
        m = compute_run_metrics(out, ["beets/importer.py"])

        assert "two_sided" in m
        ts = m["two_sided"]
        assert set(ts.keys()) == {"gt_side", "agent_side"}

        # gt_side carries localization + guards; agent_side carries navigation.
        assert "hit_at_1" in ts["gt_side"]
        assert "homonym_count" in ts["gt_side"]
        assert "reach_to_gold_action" in ts["agent_side"]
        assert "action_count" in ts["agent_side"]

        # back-compat: all flat keys still present (added view, not restructure).
        for k in ("localization", "navigation", "outcome", "regression_guards",
                  "per_layer"):
            assert k in m

        # two_sided_view is pure over the metrics dict (deterministic).
        again = two_sided_view(m)
        assert again["gt_side"]["hit_at_1"] == ts["gt_side"]["hit_at_1"]
        assert again["agent_side"]["reach_to_gold_action"] == \
            ts["agent_side"]["reach_to_gold_action"]

    def test_two_sided_per_layer_split(self, tmp_path: Path):
        history = [
            _action("run", command="grep -n set_fields beets/importer.py", eid=1),
            _obs("[GT] Called by: test/test_importer.py (3x)\n"
                 "    def set_fields(self, lib):", observation="run", eid=2),
        ]
        out = _write_instance(tmp_path / "ts2.jsonl", history)
        m = compute_run_metrics(out, ["beets/importer.py"])
        ts = m["two_sided"]
        gi_gt = ts["gt_side"]["per_layer"]["grep_intercept"]
        gi_agent = ts["agent_side"]["per_layer"]["grep_intercept"]
        # delivery facts live on the GT side; uptake lives on the agent side.
        assert "delivered" in gi_gt
        assert "homonym_count" not in gi_gt  # homonym is a run-level guard field
        assert "uptake" in gi_agent
        assert "delivered" not in gi_agent


# ===========================================================================
# (D) paired_deltas
# ===========================================================================
class TestPairedDeltas:
    def test_numeric_deltas(self, tmp_path: Path):
        gt_hist = [
            {"id": 1, "source": "user", "action": "message",
             "message": "<gt-task-brief>\n1. a.py (def f)"},
            _action("edit", path="/ws/a.py", eid=2),
            _obs("ok", observation="edit", eid=3),
        ]
        bl_hist = [
            _action("read", path="/ws/a.py", eid=1),
            _obs("ok", observation="read", eid=2),
            _action("edit", path="/ws/a.py", eid=3),
            _obs("ok", observation="edit", eid=4),
        ]
        gt = _write_instance(tmp_path / "gt.jsonl", gt_hist)
        bl = _write_instance(tmp_path / "bl.jsonl", bl_hist)

        mg = compute_run_metrics(gt, ["a.py"])
        mb = compute_run_metrics(bl, ["a.py"])
        deltas = paired_deltas(mg, mb)

        assert isinstance(deltas, dict)
        assert "navigation.action_count" in deltas
        # gt has 1 counted action, baseline has 2 -> delta -1
        assert deltas["navigation.action_count"] == pytest.approx(-1.0)
        # every delta value is numeric
        assert all(isinstance(v, (int, float)) for v in deltas.values())

    @_real_artifacts
    def test_real_paired_deltas(self):
        mg = compute_run_metrics(str(GT_OUTPUT), GOLD_FILES)
        mb = compute_run_metrics(str(BASELINE_OUTPUT), GOLD_FILES)
        deltas = paired_deltas(mg, mb)
        # GT reduced action_count and error_count vs baseline on this task.
        assert deltas["navigation.action_count"] == pytest.approx(-4.0)
        assert deltas["navigation.error_count"] == pytest.approx(-50.0)


# ===========================================================================
# emit_run_metrics writes a file
# ===========================================================================
class TestEmit:
    def test_emit_writes_json(self, tmp_path: Path):
        history = [_action("read", path="/ws/a.py", eid=1),
                   _obs("ok", observation="read", eid=2)]
        out = _write_instance(tmp_path / "in.jsonl", history)
        dest = tmp_path / "run_metrics.json"
        result = emit_run_metrics(out, ["a.py"], str(dest))
        assert dest.exists()
        on_disk = json.loads(dest.read_text(encoding="utf-8"))
        assert on_disk["navigation"]["action_count"] == result["navigation"]["action_count"]
        # deterministic: keys sorted
        assert list(on_disk.keys()) == sorted(on_disk.keys())
