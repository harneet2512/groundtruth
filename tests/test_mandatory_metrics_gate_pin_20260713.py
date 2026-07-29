"""Pin: a P1 MANDATORY-METRICS gate — every paid task must carry its FULL v3 metric set, and a
task that does not is fail-closed (NOT citable), per MANDATORY_METRICS.md line 1 ("A run without
its metrics is NOT done").

WHY (MANDATORY_METRICS.md §12, the v3 `gt.feature_metrics.v1` per-feature contract): the Collect
step already runs `gt_deep_metrics.py` (sections 1-11). §12 adds the per-FEATURE behavioural
contract, emitted by `scripts/swebench/gt_feature_metrics.py` into `gt_feature_metrics_<task>.json`.
Before this wave, a paid GT-on task could finish, upload, and be audited with NO feature-metrics
file and no hard stop — a run silently "not done" but treated as citable.

FIX (this file pins it), in the Collect step of swebench_live_lite_full.yml:
  1. INVOKE the v3 CLI: after the existing `gt_deep_metrics.py` line, run
     `scripts/swebench/gt_feature_metrics.py` (same PYTHONPATH pattern), copy its outputs into
     trial_results/.
  2. METRICS-COMPLETE gate (GT-on arm, GT_BASELINE!=1): after BOTH metric steps, require the full
     per-task set to exist non-empty — gt_deep_metrics_<task>.json, gt_feature_metrics_<task>.json,
     gt_profile_activation.json, gt_run_identity.json. Any missing => emit GT_METRICS_INCOMPLETE
     (tee'd to the trial log, citing MANDATORY_METRICS.md) + `exit 1`, the SAME fail-closed style as
     GT_PROFILE_UNPROVEN.
  3. BOTH arms require deep, performance-detail, and behavioral-detail metrics; GT-only proof
     artifacts remain under the GT_BASELINE guard.

WHERE the gate lives (justified): NOT in the liveness step but at the END of Collect, BECAUSE the
per-task metric artifacts are PRODUCED in Collect (the two metric CLIs run there); the liveness step
runs earlier (before the evaluator + before Collect) and cannot gate on files that do not yet exist.
Collect is `if: always()` so it runs on partial failures; its `exit 1` fails the job (fail-closed)
while the later Upload (`if: always()`) still captures every collected artifact — the same "run
captured but not citable" semantics as GT_PROFILE_UNPROVEN.

CRITICAL invariant kept green (the docker `bash -c '` block trap): the paid agent step is ONE
single-quoted `bash -c '...'`. A stray apostrophe closes the quote early and the agent silently never
launches. This gate is added to the Collect step and never touches that block; the apostrophe-parity
pin below re-asserts the block stayed quote-balanced.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"

_APOS = "'"


def _doc() -> dict:
    return yaml.safe_load(_WF.read_text(encoding="utf-8"))


def _all_steps() -> list[dict]:
    out: list[dict] = []
    for job in _doc().get("jobs", {}).values():
        for step in job.get("steps", []) or []:
            if isinstance(step, dict):
                out.append(step)
    return out


def _step_named(name: str) -> dict:
    for step in _all_steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"no workflow step named {name!r}")


def _step_run_containing(token: str) -> str:
    for step in _all_steps():
        run = step.get("run")
        if run and token in run:
            return run
    raise AssertionError(f"no workflow step run contains {token!r}")


def _code_lines(run: str) -> list[str]:
    # bash comment lines start with '#' (modulo indentation); exclude them so a token that only
    # appears in a comment cannot satisfy a code-presence assertion.
    return [ln for ln in run.splitlines() if not ln.strip().startswith("#")]


def _collect_run() -> str:
    # The Collect step, anchored on its distinctive mkdir (the eval step also creates trial_results).
    return _step_run_containing("mkdir -p trial_results trial_results/gt_artifacts")


def _gate_block(run: str) -> str:
    """The METRICS-COMPLETE gate: from its sentinel `_MM_MISSING` (the gate's first line) THROUGH the
    trailing GT_METRICS_COMPLETE success echo (inclusive). Isolated from the `cp` lines ABOVE it, so a
    token found here is CHECKED BY THE GATE — not merely copied earlier in the Collect step."""
    start = run.index("_MM_MISSING")
    ok = run.index("GT_METRICS_COMPLETE")
    end = run.index("\n", ok)
    return run[start:end]


def _docker_block(run: str) -> str:
    """The single-quoted `bash -c '...'` argument, opening quote THROUGH closing quote (inclusive),
    delimited by the unique `bash -c '` opener and the terminating apostrophe that precedes
    `2>&1 | tee trial_output.log` (the docker block's tee sink)."""
    marker = "bash -c "
    i = run.index(marker) + len(marker)
    assert run[i] == _APOS, "the docker invocation must open with a single-quoted bash -c argument"
    tail = run.index("2>&1 | tee -a trial_output.log", i)
    close = run.rindex(_APOS, i, tail)
    return run[i:close + 1]


# ── 0. structural integrity ───────────────────────────────────────────────────────────────────


def test_workflow_yaml_parses() -> None:
    doc = _doc()
    assert isinstance(doc, dict) and doc.get("jobs"), "workflow must remain valid, parseable YAML"


# ── 1. INVOKE the v3 feature-metrics CLI in Collect, with the task id ─────────────────────────


def test_collect_runs_gt_feature_metrics_with_task_id() -> None:
    collect = _collect_run()
    code = _code_lines(collect)
    hits = [ln for ln in code if "gt_feature_metrics.py" in ln and "GT_TASK_ID" in ln]
    assert hits, (
        "the Collect step must INVOKE scripts/swebench/gt_feature_metrics.py in a CODE line carrying "
        "the task id (${{ matrix.task }}) — found only in a comment, or without the task id, or not "
        "at all"
    )
    # it must run AFTER the existing gt_deep_metrics invocation (the v3 pass builds on the deep pass).
    joined = "\n".join(code)
    assert joined.index("gt_deep_metrics.py") < joined.index("gt_feature_metrics.py"), (
        "gt_feature_metrics.py must be invoked AFTER the existing gt_deep_metrics.py line"
    )


def test_deep_metrics_receives_task_scoped_authoritative_runtime_ledger() -> None:
    code = "\n".join(_code_lines(_collect_run()))
    ledger_copy = (
        'cp /tmp/gt_out/gt_runtime_ledger.jsonl "$_GT_LEDGER_DEST"'
    )
    assert ledger_copy in code, (
        "the sealed runtime ledger must be copied beside the task-scoped trajectory so the "
        "deep-metrics consumption reader can byte-join tagless native deliveries"
    )
    assert "GT_COLLECTION_MISSING:gt_runtime_ledger.jsonl" in code, (
        "a missing ledger must be diagnosed explicitly without aborting collection before the "
        "remaining failure evidence and metrics can be harvested"
    )
    assert 'GT_BASELINE_FLAG:-0' in code[: code.index(ledger_copy)], (
        "the hard ledger requirement must be scoped to GT-on; baseline has no GT runtime ledger"
    )
    assert code.index(ledger_copy) < code.index("gt_deep_metrics.py"), (
        "the authoritative runtime ledger must exist BEFORE gt_deep_metrics.py runs"
    )
    assert 'TASK_ARTIFACT_ROOT="/tmp/gt/${GT_TASK_ID}"' in code, (
        "the runtime ledger path must resolve through the explicit task-scoped root"
    )
    assert '_GT_LEDGER_DEST="$TASK_ARTIFACT_ROOT/gt_runtime_ledger_${GT_TASK_ID}.jsonl"' in code


def test_missing_gt_inputs_do_not_abort_failure_artifact_collection_early() -> None:
    collect = "\n".join(_code_lines(_collect_run()))
    deep = collect.index("gt_deep_metrics.py")
    for source, marker in (
        ("/tmp/gt_out/gt_runtime_ledger.jsonl", "GT_COLLECTION_MISSING:gt_runtime_ledger.jsonl"),
        ("/tmp/gt/brief_result.json", "GT_COLLECTION_MISSING:brief_result.json"),
    ):
        assert f'if [ -s "{source}" ]' in collect
        assert marker in collect
        assert collect.index(marker) < deep
    assert "GT_COLLECTION_COPY_FAIL:gt_runtime_ledger.jsonl" in collect
    assert "GT_COLLECTION_COPY_FAIL:brief_result.json" in collect
    gate = _gate_block(_collect_run())
    assert "trial_results/brief_result.json" in gate
    assert "gt_runtime_ledger_${GT_TASK_ID}.jsonl" in gate


def test_deep_metrics_receives_authoritative_swebench_gold_jsonl() -> None:
    code = "\n".join(_code_lines(_collect_run()))
    deep_line = next(line for line in code.splitlines() if "gt_deep_metrics.py" in line)
    assert "GT_GOLD_JSONL=benchmarks/data/swebench_live_lite.jsonl" in deep_line, (
        "the live producer must receive the existing authoritative dataset path; otherwise 22 "
        "gold-dependent PERF rows are falsely classified NOT_APPLICABLE"
    )


def test_collect_persists_task_scoped_performance_and_behavioral_detail() -> None:
    code = "\n".join(_code_lines(_collect_run()))
    task_trajectory = '"$TASK_ARTIFACT_ROOT/mini-swe-agent.trajectory.json"'
    performance_line = next(
        line for line in code.splitlines() if "gt_performance_metrics.py" in line
    )
    behavioral_line = next(
        line for line in code.splitlines() if "gt_behavioral_impact.py" in line
    )
    for line, output in (
        (performance_line, "gt_performance_metrics_${GT_TASK_ID}.json"),
        (behavioral_line, "gt_behavioral_impact_${GT_TASK_ID}.json"),
    ):
        assert task_trajectory in line, (
            "mandatory detail collectors must read the exact task-scoped trajectory"
        )
        assert output in line and "trial_results/" in line
    assert "--instance-id \"${GT_TASK_ID}\"" in performance_line
    assert "--gold-jsonl benchmarks/data/swebench_live_lite.jsonl" in performance_line
    assert code.index("gt_deep_metrics.py") < code.index("gt_performance_metrics.py")
    assert code.index("gt_deep_metrics.py") < code.index("gt_behavioral_impact.py")


# ── 2. the gate: all 4 GT-arm artifacts, GT_METRICS_INCOMPLETE, exit 1 (fail-closed) ──────────


def test_gate_checks_all_four_gt_artifacts_and_fails_closed() -> None:
    run = _step_run_containing("GT_METRICS_INCOMPLETE")
    gate = _gate_block(run)
    for artifact in (
        "gt_deep_metrics_${GT_TASK_ID}.json",
        "gt_feature_metrics_${GT_TASK_ID}.json",
        "gt_profile_activation.json",
        "gt_run_identity.json",
    ):
        assert artifact in gate, (
            f"the METRICS-COMPLETE gate must CHECK for {artifact!r} in its per-task artifact set "
            f"(a run without its metrics is NOT done)"
        )
    assert "GT_METRICS_INCOMPLETE" in gate, "the gate must emit the GT_METRICS_INCOMPLETE marker"
    assert "gt_agent_exit.json" in gate
    assert 'return_code", -1)) == 0' in gate
    assert 'trajectory_present") is True' in gate
    assert "visible_audit_complete" in gate
    assert "gt_task_completion.json" in gate
    assert "gt.task_completion.v1" in gate
    assert "os.replace" in gate
    # ORDER (2026-07-29, §2 MEASUREMENT taxonomy). The completion RECEIPT is attempted FIRST and
    # its own self-measurement failure (GT_TASK_COMPLETION_MISSING/INVALID:*) is DEMOTED to the
    # `gt_task_completion_invalid` reason instead of killing the job; the gt.metrics_incomplete.v1
    # marker is therefore written LAST, so ONE artifact carries EVERY uncitability reason — the
    # per-task metric set, the liveness step's recorded reasons, and a failed receipt.
    assert gate.index("gt_task_completion.json") < gate.index("if [ -n \"$_MM_MISSING\" ]")
    assert gate.index("if [ -n \"$_MM_MISSING\" ]") < gate.index("GT_METRICS_COMPLETE")
    assert "gt_task_completion_invalid" in gate, (
        "a failed completion receipt must be RECORDED as an uncitability reason, never exit the job"
    )
    assert "/tmp/gt_uncitable_reasons.txt" in gate, (
        "the gate must MERGE the liveness step's recorded uncitable reasons so citability stays "
        "fail-closed after job-killing was removed from the self-measurement checks"
    )
    idx = gate.index("GT_METRICS_INCOMPLETE")
    window = gate[idx: idx + 400]
    # OLD contract asserted `"exit 1" in window` — a metrics-incomplete task was fail-closed
    # (DISCARDED). NEW contract (2026-07-20 MEASUREMENT taxonomy): incomplete/missing metrics are a
    # GT SELF-MEASUREMENT gap on a task that ALREADY RAN — not a substrate/arm-validity breach — so
    # the gate RECORDS a gt.metrics_incomplete.v1 marker (uncitable=true + reasons) and CONTINUES; it
    # must NOT exit here. The genuine VALIDITY gates (identity/commit-parity/digest/REWARD_BRIDGE)
    # stay fail-closed elsewhere; only this metrics-incompleteness path degrades to a marker.
    assert "exit 1" not in window, (
        "GT_METRICS_INCOMPLETE must NOT be followed by `exit 1` — a task that ran is recorded "
        "uncitable, never discarded (MEASUREMENT taxonomy)"
    )
    assert "gt.metrics_incomplete.v1" in gate, (
        "the gate must WRITE the gt.metrics_incomplete.v1 marker instead of failing the task closed"
    )
    assert "uncitable" in gate, (
        "the marker must record the metrics-incomplete task uncitable (recorded + continue, not exit)"
    )


# ── 3. both arms require harness metrics; GT proofs stay under the baseline guard ────────────


def test_gate_validates_mandatory_detail_shape_and_deep_parity() -> None:
    gate = _gate_block(_step_run_containing("GT_METRICS_INCOMPLETE"))
    for artifact in (
        "gt_performance_metrics_${GT_TASK_ID}.json",
        "gt_behavioral_impact_${GT_TASK_ID}.json",
    ):
        assert artifact in gate
    for required_validation in (
        "performance_detail_invalid",
        "behavioral_detail_invalid",
        "performance_deep_parity",
        "behavioral_deep_parity",
        'isinstance(impact.get("deliveries"), list)',
        'deep.get("performance") != performance',
    ):
        assert required_validation in gate, (
            f"mandatory detail gate lacks validation {required_validation!r}"
        )


def _gate_fixture(tmp_path: Path):
    """The validator plus a KNOWN-GOOD performance payload, extracted so the single-fault
    sweep below can reuse the exact fixture the shape/parity test already proves passes.

    Factored rather than duplicated on purpose: two hand-maintained copies of this payload
    would drift, and a stale copy would make the sweep assert against a population that no
    longer matches `_MANDATORY_METRICS` -- which is the same "two things that must agree by
    hand" defect the metrics work has been unpicking all week.
    """
    gate = _gate_block(_step_run_containing("GT_METRICS_INCOMPLETE"))
    start = gate.index("import json, sys")
    validator = textwrap.dedent(gate[start:gate.index("\nPY", start)])
    from scripts.swebench.gt_run_metrics import _MANDATORY_METRICS

    performance = {"schema": "gt_performance_metrics.v1", "metric_applicability": {}}
    sample_by_type = {
        "bool": True,
        "bool_per_file": {"src/example.py": True},
        "rate": 0.5,
        "nonnegative_int": 1,
        "nonnegative_number": 1.0,
        "run_ratio": None,
    }
    for section, definitions in _MANDATORY_METRICS.items():
        if section == "behavioral_impact":
            continue
        performance[section] = {
            metric: sample_by_type[value_type] for metric, value_type in definitions
        }
    performance["token_efficiency"]["cost_per_resolved_scope"] = "run_aggregate"
    summary = {
        "total_deliveries": 0, "total_pivots": 0, "impact_rate": 0.0,
        "per_tag_impact": {}, "gt_tokens_injected": 0,
        "gt_tokens_per_pivot": 0, "nudge_compliance_rate": None,
    }

    def run(deep: dict, perf: dict, impact: dict) -> subprocess.CompletedProcess[str]:
        paths = []
        for index, payload in enumerate((deep, perf, impact)):
            path = tmp_path / f"sf_{index}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            paths.append(str(path))
        return subprocess.run(
            [sys.executable, "-c", validator, *paths],
            text=True, capture_output=True, check=False,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join((
                    str(_ROOT / "src"),
                    str(_ROOT / "scripts" / "swebench"),
                    str(_ROOT / "scripts" / "metrics"),
                )),
            },
        )

    return run, performance, summary


def _gate_read_metrics() -> list[tuple[str, str]]:
    """Every (section, metric) the PER-TASK gate validates individually.

    `validate_task_performance_record` skips `behavioral_impact` outright
    (`gt_run_metrics.py:379-381`), so those five are covered by the run-grain CLI gate, not
    this one. Derived from `_MANDATORY_METRICS` rather than hardcoded, so a metric added to
    the contract is swept automatically instead of silently escaping the sweep.
    """
    from scripts.swebench.gt_run_metrics import _MANDATORY_METRICS

    return [
        (section, metric)
        for section, definitions in _MANDATORY_METRICS.items()
        if section != "behavioral_impact"
        for metric, _value_type in definitions
    ]


def test_positive_control_the_gate_fixture_passes_before_any_metric_is_removed(
    tmp_path: Path,
) -> None:
    """Run FIRST. Every non-zero exit in the sweep below is unreadable until the unmutated
    fixture is shown to EXIT ZERO -- otherwise the sweep would be measuring a broken fixture
    and would pass for entirely the wrong reason."""
    run, performance, summary = _gate_fixture(tmp_path)
    deep = {"performance": performance, "behavioral_impact": dict(summary)}
    impact = {"deliveries": [], "summary": dict(summary)}
    result = run(deep, performance, impact)
    assert result.returncode == 0, (
        f"the known-good fixture does not pass the gate: {result.stderr}"
    )


@pytest.mark.parametrize("section,metric", _gate_read_metrics())
def test_single_fault_each_metric_is_individually_falsifiable(
    tmp_path: Path, section: str, metric: str,
) -> None:
    """SINGLE-FAULT ISOLATION -- the real C8 defect, and it is not vacuity.

    Every one of these metrics already has BOTH states demonstrable: the bulk fixture
    `empty_sections` drives all of them to `unmeasured` through the real gate and asserts a
    non-zero exit. So none of them is vacuous, and "55 of 58 have no positive/negative
    fixture" was the wrong diagnosis.

    What the bulk fixture CANNOT do is detect ONE metric silently losing its negative state:
    it flips all 53 simultaneously, so the gate fails for 53 reasons at once and would keep
    failing even if 52 of the checks had rotted away. Before this sweep, exactly one metric
    (`gold_rank`) had a named single-fault negative that flipped the gate.

    This deletes exactly ONE metric per case and requires the gate to (a) fail and (b) NAME
    that metric. Deriving the parameter list from `_MANDATORY_METRICS` means a newly
    contracted metric joins the sweep automatically rather than escaping it.
    """
    run, performance, summary = _gate_fixture(tmp_path)
    deep = {"performance": performance, "behavioral_impact": dict(summary)}
    impact = {"deliveries": [], "summary": dict(summary)}

    faulted = json.loads(json.dumps(performance))
    del faulted[section][metric]
    result = run({**deep, "performance": faulted}, faulted, impact)

    assert result.returncode != 0, (
        f"the per-task gate ACCEPTED a record missing {section}.{metric}; that metric is "
        f"contracted in _MANDATORY_METRICS but nothing enforces it per task"
    )
    # Assert the metric is NAMED, not that a particular reason code follows it. The first
    # draft required `:unmeasured` and the sweep immediately found the counterexample:
    # `token_efficiency.cost_per_resolved` is rejected as `:run_scope_contract`, because it
    # is run-scoped and carries a `cost_per_resolved_scope` companion, so removing it breaks
    # a different rule first. That is the gate behaving CORRECTLY, and over-specifying the
    # reason string would have turned a working check into a false failure. The invariant
    # that actually matters is that an operator can tell WHICH instrument went dark.
    assert f"{section}.{metric}:" in result.stderr, (
        f"the gate rejected the record but did not NAME {section}.{metric}; an unnamed "
        f"rejection cannot tell an operator which instrument went dark. stderr={result.stderr}"
    )


def test_mandatory_detail_validator_rejects_shape_and_parity_defects(
    tmp_path: Path,
) -> None:
    gate = _gate_block(_step_run_containing("GT_METRICS_INCOMPLETE"))
    start = gate.index("import json, sys")
    validator = textwrap.dedent(gate[start:gate.index("\nPY", start)])
    from scripts.swebench.gt_run_metrics import _MANDATORY_METRICS

    performance = {
        "schema": "gt_performance_metrics.v1",
        "metric_applicability": {},
    }
    sample_by_type = {
        "bool": True,
        "bool_per_file": {"src/example.py": True},
        "rate": 0.5,
        "nonnegative_int": 1,
        "nonnegative_number": 1.0,
        "run_ratio": None,
    }
    for section, definitions in _MANDATORY_METRICS.items():
        if section == "behavioral_impact":
            continue
        performance[section] = {
            metric: sample_by_type[value_type]
            for metric, value_type in definitions
        }
    performance["token_efficiency"]["cost_per_resolved_scope"] = "run_aggregate"
    summary = {
        "total_deliveries": 0, "total_pivots": 0, "impact_rate": 0.0,
        "per_tag_impact": {}, "gt_tokens_injected": 0,
        # gt_tokens_per_pivot ADDED 2026-07-27. This "valid" fixture was NOT valid: it
        # omitted one of the five behavioral_impact mandatory metrics
        # (gt_run_metrics._MANDATORY_METRICS), and it passed only because the per-task
        # gate's presence tuple omitted the same key. The fixture and the gate shared a
        # blind spot, so neither could reveal the other -- a matched pair of omissions
        # reads exactly like coverage. The gate now requires the key; this fixture is
        # corrected to actually be the positive case it claims to be.
        "gt_tokens_per_pivot": 0,
        "nudge_compliance_rate": None,
    }

    def run(deep: dict, perf: dict, impact: dict) -> subprocess.CompletedProcess[str]:
        paths = []
        for index, payload in enumerate((deep, perf, impact)):
            path = tmp_path / f"detail_{index}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            paths.append(str(path))
        return subprocess.run(
            [sys.executable, "-c", validator, *paths],
            text=True, capture_output=True, check=False,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join((
                    str(_ROOT / "src"),
                    str(_ROOT / "scripts" / "swebench"),
                    str(_ROOT / "scripts" / "metrics"),
                )),
            },
        )

    deep = {"performance": performance, "behavioral_impact": dict(summary)}
    impact = {"deliveries": [], "summary": dict(summary)}
    assert run(deep, performance, impact).returncode == 0

    empty_sections = {
        "schema": "gt_performance_metrics.v1",
        "metric_applicability": {},
        **{
            section: {}
            for section in _MANDATORY_METRICS
            if section != "behavioral_impact"
        },
    }
    empty = run({**deep, "performance": empty_sections}, empty_sections, impact)
    assert empty.returncode != 0
    assert "performance_detail_invalid" in empty.stderr

    missing_metric = json.loads(json.dumps(performance))
    del missing_metric["localization"]["gold_rank"]
    missing = run(
        {**deep, "performance": missing_metric}, missing_metric, impact
    )
    assert missing.returncode != 0
    assert "localization.gold_rank:unmeasured" in missing.stderr

    non_applicable = json.loads(json.dumps(performance))
    non_applicable["localization"]["gold_rank"] = None
    non_applicable["metric_applicability"] = {
        "localization": {
            "gold_rank": {
                "applicable": False,
                "predicate": "n_gold_files > 0",
                "reason": "true gold file denominator is absent",
            }
        }
    }
    assert run(
        {**deep, "performance": non_applicable}, non_applicable, impact
    ).returncode == 0

    invalid_applicability = json.loads(json.dumps(non_applicable))
    invalid_applicability["metric_applicability"]["localization"]["gold_rank"].pop(
        "reason"
    )
    assert run(
        {**deep, "performance": invalid_applicability},
        invalid_applicability,
        impact,
    ).returncode != 0

    wrong_schema = {**performance, "schema": "gt_performance_metrics.v0"}
    schema_result = run(
        {**deep, "performance": wrong_schema}, wrong_schema, impact
    )
    assert schema_result.returncode != 0
    assert "record:schema" in schema_result.stderr

    malformed_performance = dict(performance)
    malformed_performance.pop("token_efficiency")
    assert run(deep, malformed_performance, impact).returncode != 0
    assert run(deep, performance, {"deliveries": {}, "summary": summary}).returncode != 0
    assert run({**deep, "performance": {}}, performance, impact).returncode != 0
    divergent = {**deep, "behavioral_impact": {**summary, "total_pivots": 1}}
    assert run(divergent, performance, impact).returncode != 0


def test_baseline_arm_requires_all_harness_metrics_but_not_gt_proofs() -> None:
    gate = _gate_block(_step_run_containing("GT_METRICS_INCOMPLETE"))
    assert "GT_BASELINE" in gate, (
        "the gate must carry a GT_BASELINE guard for GT-only proof artifacts"
    )
    guard = gate.index("GT_BASELINE")
    for common_metric in (
        "gt_deep_metrics_${GT_TASK_ID}.json",
        "gt_performance_metrics_${GT_TASK_ID}.json",
        "gt_behavioral_impact_${GT_TASK_ID}.json",
    ):
        assert gate.index(common_metric) < guard, (
            f"{common_metric!r} must be required unconditionally on both arms"
        )
    # gt_deep_metrics is required on BOTH arms → checked BEFORE the guard (unconditional).
    assert gate.index("gt_deep_metrics_${GT_TASK_ID}.json") < guard, (
        "gt_deep_metrics_<task>.json must be required unconditionally (both arms), i.e. BEFORE the "
        "GT_BASELINE guard"
    )
    # the three GT-only artifacts are required only on the GT arm → checked AFTER the guard.
    for gt_only in (
        "gt_feature_metrics_${GT_TASK_ID}.json",
        "gt_profile_activation.json",
        "gt_run_identity.json",
    ):
        assert gt_only in gate, f"the gate must CHECK {gt_only!r} (a GT-only artifact)"
        assert gate.index(gt_only) > guard, (
            f"{gt_only!r} is a GT-only artifact and must be required only under the GT_BASELINE guard "
            f"(after it), never on the baseline arm"
        )


# ── 4. the marker cites MANDATORY_METRICS.md ("a run without its metrics is NOT done") ────────


def test_marker_cites_mandatory_metrics_not_done() -> None:
    gate = _gate_block(_step_run_containing("GT_METRICS_INCOMPLETE"))
    idx = gate.index("GT_METRICS_INCOMPLETE")
    marker_line = gate[idx: gate.index("\n", idx)]
    assert "MANDATORY_METRICS.md" in marker_line, (
        "the GT_METRICS_INCOMPLETE marker must cite MANDATORY_METRICS.md (line 1: a run without its "
        "metrics is NOT done)"
    )
    assert "NOT done" in marker_line or "not done" in marker_line, (
        "the marker must state the run is NOT done"
    )
    assert "tee -a trial_output.log" in marker_line, (
        "the marker must `tee -a trial_output.log` so the section-4 auditor sees it in the run log "
        "(same as GT_PROFILE_UNPROVEN's kin)"
    )


# ── 5. THE TRAP: the paid step's single-quoted docker block stays quote-balanced ──────────────


def test_docker_block_single_quote_is_balanced() -> None:
    # This gate is added to the Collect step and never touches the paid step's docker `bash -c '`
    # block. Re-assert the block is still quote-balanced (even apostrophe count opener..closing
    # inclusive). LIMIT: this catches an ODD number of stray apostrophes (the single-apostrophe
    # break, the actual trap); a re-balancing PAIR is an accepted blind spot, paired with the
    # yaml.safe_load parse above.
    run = _step_run_containing("_GT_PROFILE_EXPORTS")
    block = _docker_block(run)
    assert block.count(_APOS) % 2 == 0, (
        "the docker `bash -c '` block has an ODD number of apostrophes — a stray single quote was "
        "introduced and will close the block early (the agent never launches)"
    )
    assert run.count("bash -c " + _APOS) == 1, "expected exactly one `bash -c '` opener in the trial step"


# ── 6. PRE-RUN DIAGNOSIS INPUTS + EXACT RUN POPULATION ────────────────────────────


def test_collect_preserves_brief_result_for_downloaded_recomputation() -> None:
    collect = "\n".join(_code_lines(_collect_run()))
    copy = "cp /tmp/gt/brief_result.json trial_results/brief_result.json"
    assert copy in collect, (
        "Collect must preserve brief_result.json at the task artifact root; "
        "gt_feature_metrics' bounded named-input lookup does not search gt_artifacts/"
    )
    gate = _gate_block(_collect_run())
    assert "trial_results/brief_result.json" in gate, (
        "the GT-on metrics gate must fail closed if brief_result.json was not preserved"
    )


def test_summarize_builds_canonical_v2_and_exact_128_diagnosis_once() -> None:
    step = _step_named("Build canonical PERF and exact-128 diagnosis")
    env = step.get("env") or {}
    assert env.get("GT_EXPECTED_MATRIX") == "${{ needs.prepare.outputs.matrix }}"
    assert env.get("GT_EXPECTED_TOTAL") == "${{ needs.prepare.outputs.total }}"
    assert env.get("GT_RUN_ID") == "${{ github.run_id }}"
    run = step.get("run") or ""
    assert run.count("scripts/swebench/gt_run_metrics.py") == 1
    assert "--expected-tasks-file" in run
    assert "gt_run_metrics_v2_${GT_RUN_ID}.json" in run
    assert run.index("gt_run_metrics.py") < run.index("gt_feature_metrics.py")
    assert "--run-metrics-artifact" in run
    assert "--out \"$GT_DIAG_DIR\"" in run
    for required_guard in (
        "GT_EXPECTED_TOTAL",
        "Counter(task_ids)",
        "missing_tasks",
        "duplicate_tasks",
        "unexpected_tasks",
        "GT_EXACT_128_CARDINALITY_INVALID",
        "present_in_tasks",
    ):
        assert required_guard in run, f"diagnosis step lacks fail-closed guard {required_guard!r}"


def test_diagnosis_bundle_is_distinct_and_always_uploaded() -> None:
    upload = _step_named("Upload GT diagnosis bundle")
    assert upload.get("if") == "${{ always() }}"
    config = upload.get("with") or {}
    assert config.get("name") == "gt-diagnosis-${{ github.run_id }}"
    assert config.get("path") == "/tmp/gt-diagnosis/"
    assert config.get("if-no-files-found") == "error"
