"""D8 — grader honesty defects from run 29544917048 / beetbox__beets-5457.

Two defects, both proven RED-before-GREEN against the REAL writers:

(a) gt_performance_metrics._compute_localization counted gold edits with an EXACT
    set intersection (``edited_set & gold_set``) while every other section
    (edit_quality, scope, token, and its OWN files_to_gold_edit) uses the
    suffix-tolerant ``_path_match``. A runtime ``post_edit`` path normalizes to a
    ``./``-prefixed spelling (``./beetsplug/lyrics.py``) that exact ``&`` misses,
    so ``_gold_edited_count`` fell to 0 even though gold WAS edited. That split
    made ``build_metric_applicability`` declare edit_attempts_per_gold /
    first_edit_correctness ``applicable=False`` while edit_quality carried a real
    value, and ``gt_run_metrics._metric_state`` returns ``"failed"`` for
    value-present + applicable-False.

(b) gt_feature_metrics._canonical_task_features set required_inputs_complete=False
    off ``missing_feature_inputs`` but only listed dot-less items in
    ``missing_required_inputs`` — a False flag with an empty named list is
    un-actionable. The named list must enumerate the culprits.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src", ROOT / "scripts" / "swebench", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_feature_metrics as metrics  # noqa: E402
import gt_performance_metrics as performance  # noqa: E402
import gt_run_metrics as run_metrics  # noqa: E402

from test_gt_feature_metrics_128 import _write_task  # noqa: E402


_EDIT_QUALITY_VALUE_TYPES = dict(
    metrics.performance_metric_definitions()["edit_quality"]
)


def _write_trajectory(tmp_path: Path, messages: list[dict], submission: str) -> str:
    traj = {
        "messages": messages,
        "info": {"submission": submission, "exit_status": "Submitted", "model_stats": {}},
    }
    path = tmp_path / "mini-swe-agent.trajectory.json"
    path.write_text(json.dumps(traj), encoding="utf-8")
    return str(path)


def _assistant(command: str, timestamp: float) -> dict:
    return {
        "role": "assistant",
        "content": "step",
        "tool_calls": [{"function": {"arguments": json.dumps({"command": command})}}],
        "extra": {"timestamp": timestamp},
    }


def _metric_state_for(result: dict, name: str) -> str:
    """Feed the perf result through the exact run-aggregator classifier."""
    deep = {
        "schema": "gt_deep_metrics.v2",
        "precision_decimals": 8,
        "task_id": "probe",
        "performance": result,
        "metric_applicability": result.get("metric_applicability", {}),
    }
    state, _value, _contract = run_metrics._metric_state(
        deep, "edit_quality", name, _EDIT_QUALITY_VALUE_TYPES[name]
    )
    return state


# ---------------------------------------------------------------------------
# (a) no-command-edit gold recovered via runtime post_edit ('./'-prefixed path)
# ---------------------------------------------------------------------------

def test_runtime_post_edit_gold_is_measured_not_failed(tmp_path: Path) -> None:
    """The beets shape: gold edited via a runtime post_edit whose path is
    './pkg/mod.py'. Pre-fix localization undercounted _gold_edited_count to 0
    (exact &) while edit_quality read a real value -> _metric_state='failed'.
    Post-fix both agree on _path_match -> applicable=True -> 'measured'."""
    messages = [
        {"role": "user", "content": "issue"},
        _assistant("cat pkg/mod.py", 1.0),
        {"role": "tool", "content": "<contents>"},
        # a python open().write() shape the command detector does NOT classify
        _assistant("python - <<'PY'\nopen('pkg/mod.py','a').write('x')\nPY", 2.0),
        {"role": "tool", "content": "done"},
        _assistant("echo submit", 3.0),
        {"role": "tool", "content": "ok"},
    ]
    tp = _write_trajectory(
        tmp_path, messages, "diff --git a/pkg/mod.py b/pkg/mod.py\n+x\n"
    )
    ledger = tmp_path / "gt_runtime_ledger_probe.jsonl"
    ledger.write_text(
        json.dumps(
            {"event_type": "post_edit", "file_path": "./pkg/mod.py", "timestamp_ms": 2500}
        )
        + "\n",
        encoding="utf-8",
    )

    result = performance.compute_performance_metrics(
        tp,
        str(tmp_path),
        gold_files=["pkg/mod.py"],
        consumption_ledger={
            "schema": "gt.consumption_ledger.v2",
            "runtime_ledger_path": str(ledger),
            "entries": [],
        },
    )

    # the runtime post_edit was joined; the './'-prefixed gold path IS the edit
    assert result["authoritative_post_edit_count"] == 1
    assert result["edit_quality"]["edit_attempts_per_gold"] == 1.0
    assert result["edit_quality"]["first_edit_correctness"] == {"./pkg/mod.py": True}

    # BITING: localization now agrees with edit_quality (both detect the gold edit).
    # Pre-fix this was 0 (exact &) -> the whole defect.
    assert result["localization"]["_gold_edited_count"] == 1

    applic = result["metric_applicability"]["edit_quality"]
    assert applic["edit_attempts_per_gold"]["applicable"] is True
    assert applic["first_edit_correctness"]["applicable"] is True

    # BITING: the run-aggregator classifier no longer trips the value/applicability
    # contradiction. Pre-fix: 'failed' (rejected by validate_task_performance_record).
    assert _metric_state_for(result, "edit_attempts_per_gold") == "measured"
    assert _metric_state_for(result, "first_edit_correctness") == "measured"


def test_no_gold_edit_at_all_is_not_applicable_not_failed(tmp_path: Path) -> None:
    """Honest N/A contract: when NO gold file is edited, edit_quality yields
    None/{} and applicability is applicable=False with no observation ->
    'not_applicable', never 'failed' and never a fabricated 0."""
    messages = [
        {"role": "user", "content": "issue"},
        _assistant("cat other.py", 1.0),
        {"role": "tool", "content": "x"},
        _assistant("sed -i 's/a/b/' other.py", 2.0),  # edits a NON-gold file
        {"role": "tool", "content": "done"},
    ]
    tp = _write_trajectory(
        tmp_path, messages, "diff --git a/other.py b/other.py\n+b\n"
    )
    result = performance.compute_performance_metrics(
        tp, str(tmp_path), gold_files=["pkg/gold.py"],
        consumption_ledger={
            "schema": "gt.consumption_ledger.v2", "runtime_ledger_path": "", "entries": [],
        },
    )
    assert result["localization"]["_gold_edited_count"] == 0
    assert result["edit_quality"]["edit_attempts_per_gold"] is None
    assert result["edit_quality"]["first_edit_correctness"] == {}
    assert _metric_state_for(result, "edit_attempts_per_gold") == "not_applicable"
    assert _metric_state_for(result, "first_edit_correctness") == "not_applicable"


def test_clean_command_edit_gold_unchanged_regression(tmp_path: Path) -> None:
    """Regression: an edits-present trajectory whose gold path is spelled cleanly
    ('pkg/mod.py', no './' prefix) is unaffected — exact and _path_match agree, so
    the metric stays measured with the same value."""
    messages = [
        {"role": "user", "content": "issue"},
        _assistant("cat pkg/mod.py", 1.0),
        {"role": "tool", "content": "x"},
        _assistant("sed -i 's/a/b/' pkg/mod.py", 2.0),
        {"role": "tool", "content": "done"},
    ]
    tp = _write_trajectory(
        tmp_path, messages, "diff --git a/pkg/mod.py b/pkg/mod.py\n+b\n"
    )
    result = performance.compute_performance_metrics(
        tp, str(tmp_path), gold_files=["pkg/mod.py"],
        consumption_ledger={
            "schema": "gt.consumption_ledger.v2", "runtime_ledger_path": "", "entries": [],
        },
    )
    assert result["localization"]["_gold_edited_count"] == 1
    assert result["edit_quality"]["edit_attempts_per_gold"] == 1.0
    assert _metric_state_for(result, "edit_attempts_per_gold") == "measured"


# ---------------------------------------------------------------------------
# (b) required_inputs_complete=False MUST name a culprit in missing_required_inputs
# ---------------------------------------------------------------------------

def test_incomplete_inputs_false_flag_names_culprit(tmp_path: Path) -> None:
    """A present-but-empty deep_metrics leaves every PERF metric unmeasured. The
    flag must be False AND the flag-bound named list must enumerate the culprit —
    an unnamed False (missing_required_inputs == []) is un-actionable."""
    task = "synthetic__d8-incomplete"
    _write_task(tmp_path, task, deep_metrics={"task_id": task, "performance": {}})

    record = metrics.collect_task(task, str(tmp_path), profile="2")
    ss = record["ss_integrity"]

    assert ss["required_inputs_complete"] is False
    # BITING: the flag-bound named list is NON-EMPTY and names the specific culprit.
    # Pre-fix missing_required_inputs was [] while the flag was False.
    assert ss["missing_required_inputs"], "False flag with empty missing_required_inputs"
    assert "PERF.gold_rank" in ss["missing_required_inputs"]
    # the finer-grained breakout is preserved
    assert "PERF.gold_rank" in ss["missing_feature_inputs"]


def test_required_inputs_flag_report_invariant_holds(tmp_path: Path) -> None:
    """Invariant: required_inputs_complete is False  <=>  missing_required_inputs
    is non-empty (the honest flag/report binding)."""
    task = "synthetic__d8-invariant"
    _write_task(tmp_path, task, deep_metrics={"task_id": task, "performance": {}})
    ss = metrics.collect_task(task, str(tmp_path), profile="2")["ss_integrity"]
    assert (ss["required_inputs_complete"] is False) == bool(
        ss["missing_required_inputs"]
    )
