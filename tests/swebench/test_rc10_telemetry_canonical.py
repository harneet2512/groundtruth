"""RC-10 unit tests — telemetry verifier disconnect resolution.

Covers the BUG_GRAPH cluster RC-10 findings (D-001..D-015 / E-010 / E-015)
that compose into the "verify_report doesn't read gt_layers.log; 3 writers
in 1 file; 4 L4 readers disagreeing" failure mode.

Tests by fix letter (BUG_GRAPH §Fix sketch):
- (a) verify_report layer-fire gates wired
- (b) single canonical writer + JSON sidecars
- (c) L4 sums query+search+navigate
- (d) all 6 stub JSONLs pre-created
- (e) shared canonical helper agrees across readers
- (f) Optional sentinels render as 'unknown', not 0.00 / 0.0000
- (g) failsafe lines flagged synthesized=true
- (h) L5 covers all 13 verdicts; infra_failure mapping
- (i) --per-task-all-layers gates per-task AND, not corpus OR
- (j) partial_pull excludes from rate gates

These tests exercise the REAL code paths. No real SWE-agent batches.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_RUNNER = REPO_ROOT / "scripts" / "swebench" / "swe_agent_smoke_runner.py"
LAYERS_VERIFIER = REPO_ROOT / "scripts" / "swebench" / "gt_layers_verifier.py"
VERIFY_REPORT = REPO_ROOT / "scripts" / "swebench" / "verify_report.py"
GT_LAYER_COUNTS = REPO_ROOT / "scripts" / "swebench" / "gt_layer_counts.py"


def _load(path: Path, name: str):
    """Load a script as a module without running main()."""
    rd = str(path.parent)
    if rd not in sys.path:
        sys.path.insert(0, rd)
    # Stub image_name_resolver if needed (smoke runner imports it).
    if name == "smoke_runner_rc10":
        try:
            import image_name_resolver  # noqa: F401
        except ImportError:
            stub = type(sys)("image_name_resolver")
            stub.resolve_image_name = lambda *a, **kw: ""  # type: ignore[attr-defined]
            sys.modules["image_name_resolver"] = stub
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def smoke_mod():
    return _load(SMOKE_RUNNER, "smoke_runner_rc10")


@pytest.fixture(scope="module")
def verifier_mod():
    return _load(LAYERS_VERIFIER, "gt_layers_verifier_rc10")


@pytest.fixture(scope="module")
def report_mod():
    return _load(VERIFY_REPORT, "verify_report_rc10")


@pytest.fixture(scope="module")
def counts_mod():
    return _load(GT_LAYER_COUNTS, "gt_layer_counts_rc10")


# ---- (c) L4 sums query+search+navigate ------------------------------------

def test_l4_counts_sum_query_search_navigate(counts_mod, tmp_path: Path) -> None:
    td = tmp_path / "task_x"
    td.mkdir()
    (td / "gt_query_calls.jsonl").write_text(
        '{"symbol":"a"}\n{"symbol":"b"}\n', encoding="utf-8"
    )
    (td / "gt_search_calls.jsonl").write_text(
        '{"q":"foo"}\n{"q":"bar"}\n{"q":"baz"}\n', encoding="utf-8"
    )
    (td / "gt_navigate_calls.jsonl").write_text(
        '{"to":"x"}\n', encoding="utf-8"
    )
    (td / "gt_validate_calls.jsonl").write_text("", encoding="utf-8")
    (td / "gt_reindex.jsonl").write_text(
        '{"path":"a.py"}\n{"path":"b.py"}\n', encoding="utf-8"
    )
    counts = counts_mod.count_layer_calls(td)
    assert counts["gt_query"] == 2
    assert counts["gt_search"] == 3
    assert counts["gt_navigate"] == 1
    assert counts["gt_validate"] == 0
    assert counts["L4_total"] == 6, "L4 must sum query+search+navigate"
    assert counts["L6_reindex"] == 2


def test_l4_count_missing_dir(counts_mod, tmp_path: Path) -> None:
    """No task_dir → all-zero dict, never raises."""
    counts = counts_mod.count_layer_calls(None)
    assert counts["L4_total"] == 0
    assert counts["gt_query"] == 0


def test_smoke_runner_l4_uses_shared_helper(smoke_mod, tmp_path: Path) -> None:
    """RC-10 (D-002): smoke runner's _read_l4 sums all three structural tools."""
    out = tmp_path / "run"
    td = out / "tx"
    td.mkdir(parents=True)
    (td / "gt_query_calls.jsonl").write_text("{}\n", encoding="utf-8")
    (td / "gt_search_calls.jsonl").write_text("{}\n{}\n", encoding="utf-8")
    (td / "gt_navigate_calls.jsonl").write_text("{}\n{}\n{}\n", encoding="utf-8")

    snap = smoke_mod.LayerSnapshot()
    smoke_mod._read_l4(td, snap)
    assert snap.L4 == 6, f"L4 should sum to 6; got {snap.L4}"


# ---- (d) all 6 stub JSONLs pre-created -------------------------------------

def test_init_host_artifact_stubs_creates_all_six(tmp_path: Path) -> None:
    """RC-10 (D-005): all 6 expected JSONL files must be pre-created."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "swebench"))
    # Use direct module reload pattern from test_pullback_hook.
    if "gt_track4_pre_run" in sys.modules:
        del sys.modules["gt_track4_pre_run"]
    # Stub sweagent + swerex (same approach as test_pullback_hook.py).
    import types
    if "sweagent.run.hooks.abstract" not in sys.modules:
        sw = types.ModuleType("sweagent")
        sw_run = types.ModuleType("sweagent.run")
        sw_hooks = types.ModuleType("sweagent.run.hooks")
        sw_abs = types.ModuleType("sweagent.run.hooks.abstract")
        class RunHook:  # noqa: D401
            pass
        sw_abs.RunHook = RunHook
        sys.modules["sweagent"] = sw
        sys.modules["sweagent.run"] = sw_run
        sys.modules["sweagent.run.hooks"] = sw_hooks
        sys.modules["sweagent.run.hooks.abstract"] = sw_abs
    if "swerex.runtime.abstract" not in sys.modules:
        sx = types.ModuleType("swerex")
        sx_r = types.ModuleType("swerex.runtime")
        sx_a = types.ModuleType("swerex.runtime.abstract")
        class UploadRequest:
            def __init__(self, source_path=None, target_path=None):
                self.source_path = source_path
                self.target_path = target_path
        sx_a.UploadRequest = UploadRequest
        sys.modules["swerex"] = sx
        sys.modules["swerex.runtime"] = sx_r
        sys.modules["swerex.runtime.abstract"] = sx_a
    import gt_track4_pre_run as hm
    log_dir = tmp_path / "logs" / "task_y"
    hm._init_host_artifact_stubs(log_dir)
    for name in (
        "gt_query_calls.jsonl",
        "gt_search_calls.jsonl",
        "gt_navigate_calls.jsonl",
        "gt_validate_calls.jsonl",
        "gt_reindex.jsonl",
    ):
        assert (log_dir / name).is_file(), f"missing pre-created stub: {name}"
    assert (log_dir / "gt_evidence").is_dir()


# ---- (e) canonical helper FAIL-LOUD on disagreement -----------------------

def test_disagreement_check_returns_none_on_agreement(counts_mod) -> None:
    assert counts_mod.disagreement_check(5, 5) is None


def test_disagreement_check_returns_reason_on_divergence(counts_mod) -> None:
    reason = counts_mod.disagreement_check(5, 3)
    assert reason is not None
    assert "jsonl=5" in reason and "trajectory=3" in reason


# ---- (f) Optional sentinels render unknown ---------------------------------

def test_format_layer_line_renders_unknown_for_missing_elapsed_cost(smoke_mod) -> None:
    snap = smoke_mod.LayerSnapshot(
        L1="fired", L2="noop", L3=2, L4=1, L5="pass", L6=1,
        elapsed_s=None, resolved=None, cost_usd=None,
    )
    line = smoke_mod.format_layer_line("repo__pkg-1", snap)
    assert "elapsed_s=unknown" in line
    assert "cost_usd=unknown" in line
    assert "resolved=unknown" in line


def test_format_layer_line_real_values_render_numeric(smoke_mod) -> None:
    snap = smoke_mod.LayerSnapshot(
        L1="fired", L2="noop", L3=2, L4=1, L5="pass", L6=1,
        elapsed_s=42.5, resolved=True, cost_usd=0.123456,
    )
    line = smoke_mod.format_layer_line("t-1", snap)
    assert "elapsed_s=42.50" in line
    assert "cost_usd=0.1235" in line
    assert "resolved=true" in line


# ---- (g) failsafe lines flagged synthesized=true --------------------------

def test_failsafe_line_marks_synthesized(smoke_mod, tmp_path: Path) -> None:
    """RC-10 (D-009): failsafe path emits synthesized=true."""
    out = tmp_path / "run"
    out.mkdir()
    # No per-task dir exists, so collect_layer_snapshot returns empty snapshot.
    global_log = out / "_global_gt_layers.log"
    smoke_mod._emit_for_completed_task(out, "missing_task", global_log,
                                       synthesized=True)
    line = global_log.read_text(encoding="utf-8")
    assert "synthesized=true" in line, (
        "failsafe path must mark synthesized=true so the verifier can "
        "filter wedge / drop tasks out of healthy bucket counts"
    )


# ---- (h) L5 verdict mapping covers all 13 values --------------------------

@pytest.mark.parametrize("verdict,expected_class", [
    ("pass", "pass"),
    ("force", "pass"),
    ("approved", "pass"),
    ("warn_soft_escape", "warn"),
    ("blocked", "fail"),
    ("autosubmit", "infra_failure"),
    ("pull_failed", "infra_failure"),
    ("no_close_wrap", "infra_failure"),
    ("no_graph_db", "infra_failure"),
    ("malformed", "infra_failure"),
    ("blocked_no_progress", "infra_failure"),
    ("unresolved", "infra_failure"),
    ("absent", "infra_failure"),
    ("db_open_error", "infra_failure"),
    ("db_open_error: sqlite3.OperationalError", "infra_failure"),
])
def test_l5_classify_covers_all_thirteen(smoke_mod, verdict, expected_class) -> None:
    """RC-10 (D-011): all 13 emitted verdicts have a mapping; none collapses
    silently to not_evaluated."""
    assert smoke_mod._classify_l5_verdict(verdict) == expected_class


def test_l5_classify_empty_is_not_evaluated(smoke_mod) -> None:
    assert smoke_mod._classify_l5_verdict("") == "not_evaluated"


def test_read_l5_db_open_error_renders_infra_failure(smoke_mod, tmp_path: Path) -> None:
    """Synthetic gt_pre_finish_gate.json with db_open_error → infra_failure."""
    td = tmp_path / "t"
    td.mkdir()
    (td / "gt_pre_finish_gate.json").write_text(
        json.dumps({"result": "db_open_error: sqlite3.OperationalError"})
    )
    snap = smoke_mod.LayerSnapshot()
    smoke_mod._read_l5(td, snap)
    assert snap.L5 == "infra_failure"
    assert snap.L5 != "not_evaluated", (
        "infra failure must NOT collapse to not_evaluated"
    )


# ---- (i) --per-task-all-layers per-task AND -------------------------------

def _write_task(out: Path, tid: str, *, L1: str, L3: int, L4: int,
                L5_gate: str, L6: int, brief_marker: str = "<gt-task-brief>") -> None:
    td = out / tid
    td.mkdir(parents=True)
    if L1 != "empty":
        (td / "gt_brief.txt").write_text(brief_marker + "\nbrief body\n")
    ev = td / "gt_evidence"
    ev.mkdir(parents=True, exist_ok=True)
    for i in range(L3):
        (ev / f"edit_{i:03d}.json").write_text("{}")
    if L4:
        (td / "gt_query_calls.jsonl").write_text(
            "\n".join('{"x":1}' for _ in range(L4)) + "\n"
        )
    else:
        (td / "gt_query_calls.jsonl").write_text("")
    (td / "gt_search_calls.jsonl").write_text("")
    (td / "gt_navigate_calls.jsonl").write_text("")
    (td / "gt_validate_calls.jsonl").write_text("")
    if L6:
        (td / "gt_reindex.jsonl").write_text(
            "\n".join('{"x":1}' for _ in range(L6)) + "\n"
        )
    else:
        (td / "gt_reindex.jsonl").write_text("")
    (td / "gt_pre_finish_gate.json").write_text(json.dumps({"result": L5_gate}))


def test_per_task_all_layers_fails_when_one_task_has_dead_l4(smoke_mod, tmp_path: Path) -> None:
    out = tmp_path / "run"
    out.mkdir()
    _write_task(out, "task_a", L1="fired", L3=2, L4=1, L5_gate="pass", L6=1)
    _write_task(out, "task_b", L1="fired", L3=2, L4=0, L5_gate="pass", L6=1)
    ok, reasons = smoke_mod._evaluate_layer_invocation(
        out, ["task_a", "task_b"],
        per_task_all_layers=True, per_task_min_pct=100.0,
    )
    assert ok is False
    assert any("per_task_all_layers" in r for r in reasons), reasons


def test_per_task_all_layers_passes_when_every_task_full(smoke_mod, tmp_path: Path) -> None:
    out = tmp_path / "run"
    out.mkdir()
    _write_task(out, "task_a", L1="fired", L3=2, L4=1, L5_gate="pass", L6=1)
    _write_task(out, "task_b", L1="fired", L3=1, L4=2, L5_gate="warn_soft_escape", L6=1)
    ok, _reasons = smoke_mod._evaluate_layer_invocation(
        out, ["task_a", "task_b"],
        per_task_all_layers=True, per_task_min_pct=100.0,
    )
    assert ok is True


# ---- (a) verify_report wires layer gates ----------------------------------

def test_verify_report_layer_gate_fails_when_log_empty(report_mod, tmp_path: Path) -> None:
    """A run dir with only the rate artifacts but NO gt_layers.log gives
    layer_gates present=False — backwards compat for archived runs."""
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "gt_arm_summary.json").write_text(json.dumps({
        "arm": "gt-nolsp",
        "task_count": 1,
        "ack_armed_total": 1,
        "steer_delivered_total": 1,
        "ack_engagement_total": 1,
        "material_edit_total": 1,
        "must_ok_rate": 1.0,
        "has_patch_rate": 1.0,
    }))
    (rd / "gt_report.csv").write_text(
        "run_id,arm,instance_id,cycle,material_edit_count,ack_armed_count,steer_delivered_count,ack_engagement_count\n"
        "r,gt-nolsp,task-1,1,1,1,1,1\n"
    )
    result = report_mod.compute(rd)
    # No log → layer_gates.present is False — does NOT veto verdict.
    lg = result["layer_gates"]
    assert lg["present"] is False


def test_verify_report_layer_gate_passes_with_global_log(report_mod, tmp_path: Path) -> None:
    """Synthesizing a healthy _global_gt_layers.log adds 4 PASS gate rows."""
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "gt_arm_summary.json").write_text(json.dumps({
        "arm": "gt-nolsp",
        "task_count": 1,
        "ack_armed_total": 1,
        "steer_delivered_total": 1,
        "ack_engagement_total": 1,
        "material_edit_total": 1,
        "must_ok_rate": 1.0,
        "has_patch_rate": 1.0,
    }))
    (rd / "gt_report.csv").write_text(
        "run_id,arm,instance_id,cycle,material_edit_count,ack_armed_count,steer_delivered_count,ack_engagement_count\n"
        "r,gt-nolsp,task-1,1,1,1,1,1\n"
    )
    # Healthy canonical line — all 6 layers fire.
    line = (
        "[GT_LAYERS] task=task-1 L1=fired L2=noop L3=1 L4=1 L5=pass L6=1 "
        "elapsed_s=12.5 resolved=true cost_usd=0.0123\n"
    )
    (rd / "_global_gt_layers.log").write_text(line)
    result = report_mod.compute(rd)
    lg = result["layer_gates"]
    assert lg["present"] is True
    layer_gate_chars = {g["characteristic"] for g in lg["gates"]}
    assert "layers_log_present" in layer_gate_chars
    assert "layers_parsed_nonempty" in layer_gate_chars
    assert "layers_no_unparseable" in layer_gate_chars
    assert "layers_all_six_fire" in layer_gate_chars
    # All four gates pass on a healthy line.
    for g in lg["gates"]:
        assert g["pass"] is True, f"unexpected fail: {g}"


def test_verify_report_layer_gate_fails_on_dead_l4(report_mod, tmp_path: Path) -> None:
    """layers_all_six_fire is FALSE when L4=0 across all healthy tasks."""
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "gt_arm_summary.json").write_text(json.dumps({
        "arm": "gt-nolsp", "task_count": 1,
        "ack_armed_total": 1, "steer_delivered_total": 1,
        "ack_engagement_total": 1, "material_edit_total": 1,
        "must_ok_rate": 1.0, "has_patch_rate": 1.0,
    }))
    (rd / "gt_report.csv").write_text(
        "run_id,arm,instance_id,cycle,material_edit_count,ack_armed_count,steer_delivered_count,ack_engagement_count\n"
        "r,gt-nolsp,task-1,1,1,1,1,1\n"
    )
    line = (
        "[GT_LAYERS] task=task-1 L1=fired L2=noop L3=1 L4=0 L5=pass L6=1 "
        "elapsed_s=12.5 resolved=true cost_usd=0.0123\n"
    )
    (rd / "_global_gt_layers.log").write_text(line)
    result = report_mod.compute(rd)
    six_gate = next(g for g in result["gates"]
                    if g["characteristic"] == "layers_all_six_fire")
    assert six_gate["pass"] is False
    assert result["verdict"] == "FAIL", (
        "verify_report verdict must FAIL when layer-fire gate fails"
    )


def test_verify_report_synthesized_lines_excluded_from_six_layer_check(
    report_mod, tmp_path: Path
) -> None:
    """RC-10 (D-009): synthesized failsafe lines never count toward
    'all 6 layers fire'. A run where only synthesized lines fired L1/L4
    must FAIL the layers_all_six_fire gate."""
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "gt_arm_summary.json").write_text(json.dumps({
        "arm": "gt-nolsp", "task_count": 2,
        "ack_armed_total": 0, "steer_delivered_total": 0,
        "ack_engagement_total": 0, "material_edit_total": 0,
        "must_ok_rate": 0.0, "has_patch_rate": 0.0,
    }))
    (rd / "gt_report.csv").write_text(
        "run_id,arm,instance_id,cycle,material_edit_count,ack_armed_count,steer_delivered_count,ack_engagement_count\n"
        "r,gt-nolsp,task-1,1,0,0,0,0\n"
    )
    # 2 lines: one healthy with all-zero, one synthesized with bogus high.
    healthy_zero = (
        "[GT_LAYERS] task=task-zero L1=empty L2=noop L3=0 L4=0 L5=not_evaluated L6=0 "
        "elapsed_s=0.00 resolved=false cost_usd=0.0000\n"
    )
    synth_bogus = (
        "[GT_LAYERS] task=task-synth L1=fired L2=fired L3=5 L4=5 L5=pass L6=5 "
        "elapsed_s=unknown resolved=unknown cost_usd=unknown synthesized=true\n"
    )
    (rd / "_global_gt_layers.log").write_text(healthy_zero + synth_bogus)
    result = report_mod.compute(rd)
    six_gate = next(g for g in result["gates"]
                    if g["characteristic"] == "layers_all_six_fire")
    # Healthy subset = the all-zero line; synth excluded → no L4 fired.
    assert six_gate["pass"] is False, (
        "synthesized lines must not count toward layer-fire — pre-fix "
        "they did and corpus health was false-claimed"
    )


# ---- (j) partial_pull surfaces in verify_report ---------------------------

def test_verify_report_surfaces_partial_pull_count(report_mod, tmp_path: Path) -> None:
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "gt_arm_summary.json").write_text(json.dumps({
        "arm": "gt-nolsp", "task_count": 1,
        "ack_armed_total": 1, "steer_delivered_total": 1,
        "ack_engagement_total": 1, "material_edit_total": 1,
        "must_ok_rate": 1.0, "has_patch_rate": 1.0,
    }))
    (rd / "gt_report.csv").write_text(
        "run_id,arm,instance_id,cycle,material_edit_count,ack_armed_count,steer_delivered_count,ack_engagement_count\n"
        "r,gt-nolsp,task-1,1,1,1,1,1\n"
    )
    line = (
        "[GT_LAYERS] task=task-1 L1=fired L2=noop L3=1 L4=1 L5=pass L6=1 "
        "elapsed_s=12.5 resolved=true cost_usd=0.0123 partial_pull=true\n"
    )
    (rd / "_global_gt_layers.log").write_text(line)
    result = report_mod.compute(rd)
    assert result["layer_gates"]["n_partial_pull"] == 1


# ---- gt_layers_verifier regex accepts unknown -----------------------------

def test_verifier_regex_accepts_unknown_elapsed_and_cost(verifier_mod, tmp_path: Path) -> None:
    log = tmp_path / "g.log"
    log.write_text(
        "[GT_LAYERS] task=t L1=fired L2=noop L3=1 L4=1 L5=pass L6=1 "
        "elapsed_s=unknown resolved=unknown cost_usd=unknown\n"
    )
    parsed, bad = verifier_mod.parse_log(log)
    assert len(parsed) == 1
    assert len(bad) == 0
    assert parsed[0].elapsed_s is None
    assert parsed[0].cost_usd is None


def test_verifier_regex_accepts_synthesized_and_partial_pull_tokens(
    verifier_mod, tmp_path: Path
) -> None:
    log = tmp_path / "g.log"
    log.write_text(
        "[GT_LAYERS] task=t L1=fired L2=noop L3=1 L4=1 L5=pass L6=1 "
        "elapsed_s=10.0 resolved=true cost_usd=0.01 synthesized=true partial_pull=true\n"
    )
    parsed, bad = verifier_mod.parse_log(log)
    assert len(parsed) == 1
    assert len(bad) == 0
    assert parsed[0].synthesized is True
    assert parsed[0].partial_pull is True
