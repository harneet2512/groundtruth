"""TRUE steps-to-gold via DATASET GOLD — measurement pins for the localization reader.

Mission: `performance.localization.steps_to_gold_*` are null everywhere because the
reader only ever had a submission-PROXY "gold" (the agent's own diff), which G1 nulls.
Wiring the SWE-bench dataset `patch` as the PREFERRED, non-proxy gold source
(`gold_source="dataset_gold"`) makes the timing fields COMPUTE against TRUE gold.

RED-first: every test here fails against the pre-wiring reader (no dataset-gold path,
no instance_id param, sed-with-spaces edit mis-extraction).

HARD LAW pins:
  * gold is OFFLINE-ONLY — the gold file list may live in the metric artifact but must
    NEVER appear outside `performance` (leak pin, test_gold_never_leaks_*).
  * gold-assisted numbers are labelled `gold_source="dataset_gold"`, `gold_is_proxy=False`
    so they can never be confused with the circular proxy.

Modules are loaded by path — scripts/ is not a package.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PM = _ROOT / "scripts" / "swebench" / "gt_performance_metrics.py"
_spec = importlib.util.spec_from_file_location("gt_performance_metrics", _PM)
assert _spec and _spec.loader
pm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pm)


# --------------------------------------------------------------------------- helpers
def _bash(cmd: str) -> dict:
    """A mini-swe-agent assistant turn: a single bash tool call (the real shape)."""
    return {
        "role": "assistant",
        "content": "step",
        "tool_calls": [{"function": {"name": "bash",
                                     "arguments": json.dumps({"command": cmd})}}],
    }


def _write_jsonl(dirpath: str, instance_id: str, patch: str) -> str:
    p = os.path.join(dirpath, "gold.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        # a decoy line first, then the real record, then a prefix-collision decoy
        f.write(json.dumps({"instance_id": "other__repo-9", "patch":
                            "diff --git a/z.py b/z.py\n"}) + "\n")
        f.write(json.dumps({"instance_id": instance_id, "patch": patch}) + "\n")
        f.write(json.dumps({"instance_id": instance_id + "0", "patch":
                            "diff --git a/decoy.py b/decoy.py\n"}) + "\n")
    return p


# gold patch: one SOURCE file (src/gold.py) + one TEST file (tests/test_gold.py, excluded)
_PATCH = (
    "diff --git a/src/gold.py b/src/gold.py\n"
    "--- a/src/gold.py\n+++ b/src/gold.py\n@@ -1 +1 @@\n-foo bar\n+baz qux\n"
    "diff --git a/tests/test_gold.py b/tests/test_gold.py\n"
    "--- a/tests/test_gold.py\n+++ b/tests/test_gold.py\n@@ -1 +1 @@\n-x\n+y\n"
)


def _traj() -> dict:
    """view non-gold -> view gold -> sed-edit gold (sed script HAS spaces)."""
    return {
        "messages": [
            _bash("cd /r && cat src/other.py"),
            {"role": "tool", "content": "def other(): pass"},
            _bash("cd /r && cat src/gold.py"),
            {"role": "tool", "content": "foo bar"},
            _bash("cd /r && sed -i 's/foo bar/baz qux/' src/gold.py"),
            {"role": "tool", "content": "File updated."},
        ],
        "info": {"submission": "diff --git a/src/gold.py b/src/gold.py\n+baz qux\n-foo bar"},
    }


# =========================================================================== unit
def test_is_test_file_matches_canonical_rule() -> None:
    assert pm._is_test_file("tests/test_gold.py") is True
    assert pm._is_test_file("pkg/test_foo.py") is True
    assert pm._is_test_file("pkg/foo_test.go") is True
    assert pm._is_test_file("a/b/conftest.py") is True
    assert pm._is_test_file("src/foo.spec.ts") is True
    assert pm._is_test_file("src/__tests__/foo.js") is True
    # NOT tests — real source
    assert pm._is_test_file("src/gold.py") is False
    assert pm._is_test_file("aiogram/fsm/context.py") is False
    assert pm._is_test_file("CHANGES/1431.feature.rst") is False


def test_gold_files_from_dataset_extracts_source_excludes_tests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        jp = _write_jsonl(tmp, "acme__widget-1", _PATCH)
        gf = pm.gold_files_from_dataset("acme__widget-1", jp)
    assert gf == ["src/gold.py"], gf  # test file excluded, source kept, order preserved


def test_gold_files_from_dataset_missing_instance_is_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        jp = _write_jsonl(tmp, "acme__widget-1", _PATCH)
        assert pm.gold_files_from_dataset("nope__missing-1", jp) == []
        assert pm.gold_files_from_dataset("acme__widget-1", "/no/such/file.jsonl") == []


def test_sed_with_spaces_edit_target_is_the_file_not_the_script() -> None:
    """REGRESSION: `sed -i 's/a b/c d/' path` must extract `path`, not a script token."""
    cmd = "cd /testbed && sed -i 's/from typing import Any, Dict/from typing import Any/' aiogram/fsm/context.py"
    f = pm._extract_edited_file("", cmd)
    assert f == "aiogram/fsm/context.py", f


def test_structured_sed_range_is_a_view_and_ignores_assistant_prose(
    tmp_path: Path,
) -> None:
    """A decoded mini tool call is authoritative for view extraction.

    The address expression belongs to ``sed``; the following token is the file.
    Assistant prose must never become a second command-parsing surface when the
    structured command is available.
    """
    turn = _bash("cd /testbed && sed -n '596,1000p' sh.py")
    tool_calls_json = json.dumps(turn["tool_calls"])
    full_cmd = tool_calls_json + " I need more decoy.py before deciding."

    assert pm._extract_viewed_file(tool_calls_json, full_cmd) == "sh.py"

    trajectory = tmp_path / "mini-swe-agent.trajectory.json"
    turn["content"] = "I need more decoy.py before deciding."
    trajectory.write_text(json.dumps({
        "messages": [turn, {"role": "tool", "content": "source"}],
        "info": {"submission": ""},
    }), encoding="utf-8")
    result = pm.compute_performance_metrics(
        str(trajectory), str(tmp_path), gold_files=["sh.py"]
    )

    assert result["localization"]["first_gold_view_step"] == 1
    assert result["localization"]["files_to_gold_view"] == 0
    assert result["localization"]["steps_to_gold_view"] == 0


def test_structured_non_view_does_not_fall_through_to_assistant_prose() -> None:
    """Structured non-view commands cannot acquire a view from model prose."""
    turn = _bash("cd /testbed && grep -n 'RunningCommand' sh.py")
    tool_calls_json = json.dumps(turn["tool_calls"])
    full_cmd = tool_calls_json + " I need more decoy.py before deciding."

    assert pm._extract_viewed_file(tool_calls_json, full_cmd) is None


def test_empty_structured_args_do_not_fall_through_to_assistant_prose() -> None:
    """A valid empty arguments object still establishes structured authority."""
    tool_calls_json = json.dumps([{
        "function": {"name": "bash", "arguments": json.dumps({})},
    }])
    full_cmd = tool_calls_json + " I need more decoy.py before deciding."

    assert pm._extract_viewed_file(tool_calls_json, full_cmd) is None


def test_unstructured_sed_range_preserves_legacy_fallback() -> None:
    """Older trajectories without tool-call arguments retain shell parsing."""
    assert pm._extract_viewed_file(
        "", "cd /testbed && sed -n '596,1000p' sh.py"
    ) == "sh.py"


# =========================================================================== wiring
def test_dataset_gold_is_preferred_and_timing_computes() -> None:
    """The core mission: dataset gold -> gold_source=dataset_gold (NOT proxy),
    gold_is_proxy=False, and every timing field COMPUTES to its exact value."""
    with tempfile.TemporaryDirectory() as tmp:
        jp = _write_jsonl(tmp, "acme__widget-1", _PATCH)
        tp = os.path.join(tmp, "mini-swe-agent.trajectory.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(_traj(), f)
        res = pm.compute_performance_metrics(
            tp, tmp, instance_id="acme__widget-1", gold_jsonl=jp)

    assert res["gold_source"] == "dataset_gold"
    assert res["gold_is_proxy"] is False
    assert res["gold_files_used"] == ["src/gold.py"]  # test excluded
    loc = res["localization"]
    assert loc["gold_never_reached"] is False
    assert loc["first_gold_view_step"] == 2          # gold viewed at 2nd assistant step
    assert loc["steps_to_gold_view"] == 1            # step-1 == index 2 minus 1
    assert loc["files_to_gold_view"] == 1            # src/other.py viewed before gold
    assert loc["steps_to_gold_edit"] == 2            # sed edit at step 3 -> 3-1
    assert loc["files_to_gold_edit"] == 0            # no non-gold edited before
    assert loc["localization_precision"] == 1.0
    assert loc["localization_recall"] == 1.0


def test_env_gt_gold_jsonl_override_is_honoured() -> None:
    """GT_GOLD_JSONL env selects the dataset with no explicit gold_jsonl arg."""
    with tempfile.TemporaryDirectory() as tmp:
        jp = _write_jsonl(tmp, "acme__widget-1", _PATCH)
        tp = os.path.join(tmp, "mini-swe-agent.trajectory.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(_traj(), f)
        old = os.environ.get("GT_GOLD_JSONL")
        os.environ["GT_GOLD_JSONL"] = jp
        try:
            res = pm.compute_performance_metrics(tp, tmp, instance_id="acme__widget-1")
        finally:
            if old is None:
                os.environ.pop("GT_GOLD_JSONL", None)
            else:
                os.environ["GT_GOLD_JSONL"] = old
    assert res["gold_source"] == "dataset_gold"
    assert res["localization"]["steps_to_gold_view"] == 1


def test_proxy_fallback_byte_identical_without_dataset_gold() -> None:
    """No GT_GOLD_JSONL, no gold_jsonl -> passing instance_id changes NOTHING.
    The whole record is byte-identical to the pre-wiring (no-instance-id) call."""
    with tempfile.TemporaryDirectory() as tmp:
        tp = os.path.join(tmp, "mini-swe-agent.trajectory.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(_traj(), f)
        os.environ.pop("GT_GOLD_JSONL", None)
        base = pm.compute_performance_metrics(tp, tmp)               # old signature
        withid = pm.compute_performance_metrics(tp, tmp, instance_id="acme__widget-1")
    assert withid["gold_source"] == "submission_proxy"
    assert withid["gold_is_proxy"] is True
    assert withid["localization"]["steps_to_gold_view"] is None      # nulled proxy
    # BYTE-IDENTICAL: instance_id with no dataset must not perturb any field
    assert json.dumps(base, sort_keys=True) == json.dumps(withid, sort_keys=True)


def test_view_detection_mutation_breaks_steps_to_gold(monkeypatch) -> None:
    """MUTATION: cripple the view detector -> the steps_to_gold-view fixture must fail
    (gold is never seen). Proves steps_to_gold_view genuinely rides view detection."""
    monkeypatch.setattr(pm, "_extract_viewed_file", lambda *a, **k: None)
    with tempfile.TemporaryDirectory() as tmp:
        jp = _write_jsonl(tmp, "acme__widget-1", _PATCH)
        tp = os.path.join(tmp, "mini-swe-agent.trajectory.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(_traj(), f)
        res = pm.compute_performance_metrics(
            tp, tmp, instance_id="acme__widget-1", gold_jsonl=jp)
    loc = res["localization"]
    assert loc["first_gold_view_step"] is None
    assert loc["steps_to_gold_view"] is None
    assert loc["gold_never_reached"] is True


# =========================================================================== leak
def _load_gt_deep_metrics():
    p = _ROOT / "scripts" / "swebench" / "gt_deep_metrics.py"
    spec = importlib.util.spec_from_file_location("gt_deep_metrics", p)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gold_never_leaks_outside_performance_in_deep_metrics() -> None:
    """HARD LAW leak pin: with dataset gold wired end-to-end through gt_deep_metrics,
    the gold file path appears ONLY inside deep['performance'] — never in any other
    (model-facing-adjacent) field of the record."""
    gdm = _load_gt_deep_metrics()
    with tempfile.TemporaryDirectory() as tmp:
        jp = _write_jsonl(tmp, "acme__widget-1", _PATCH)
        # trajectory dir MUST contain the task id (miniswe finder is task-scoped)
        rdir = os.path.join(tmp, "acme__widget-1")
        os.makedirs(rdir)
        tp = os.path.join(rdir, "mini-swe-agent.trajectory.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(_traj(), f)
        old = os.environ.get("GT_GOLD_JSONL")
        os.environ["GT_GOLD_JSONL"] = jp
        try:
            deep = gdm.build("acme__widget-1", rdir)
        finally:
            if old is None:
                os.environ.pop("GT_GOLD_JSONL", None)
            else:
                os.environ["GT_GOLD_JSONL"] = old

    perf = deep.pop("performance", {})
    # gold reached the metric artifact (allowed) under performance
    assert perf.get("gold_source") == "dataset_gold"
    assert "src/gold.py" in json.dumps(perf)
    # ... and NOWHERE else in the record
    assert "src/gold.py" not in json.dumps(deep), "GOLD LEAKED outside performance"
