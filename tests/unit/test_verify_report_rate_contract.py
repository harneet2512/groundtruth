"""Behavior tests for the verify_report rate-gate contract.

TTD reproduction target: smoke v5 (2026-04-24 06:18 UTC) FAILED both arms with
delivery_rate=0.00 / engagement_rate=0.00 even though raw chain counters were
healthy. The failure is a reader/writer schema mismatch: verify_report reads
`delivery_rate` and `engagement_rate` as pre-computed keys out of
`gt_arm_summary.json`, but `gt_canary_report.arm_summary()` never emits them.
`_num(None) == 0.0`, so missing keys silently become a FAIL.

Each test here is a behavior test:
- it drives the REAL verify_report.compute() code path against artifacts it
  would actually see on disk during a smoke run,
- it asserts the observable output,
- it cannot be made to pass by editing the test.

Canonical formula, derived from historical PASS artifact
benchmarks/swebench/baseline_confirm_nolsp/gt_arm_summary.json
(delivery_rate=0.8 with steer=16 ack=20; engagement_rate=0.9375 with engage=15
steer=16) AND scripts/swebench/gt_finalization.py:469-470:

    delivery_rate   = steer_delivered_total / ack_armed_total
    engagement_rate = ack_engagement_total / steer_delivered_total

Tests 1-3 and 5 are synthetic-fixture behavior tests. Test 4 replays the
actual failed archive saved under benchmarks/swebench/fast_diag/ — the only
test that proves the failure mode is closed end-to-end.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_REPORT = REPO_ROOT / "scripts" / "swebench" / "verify_report.py"

# Import verify_report from the repo so we call the REAL compute() code path.
sys.path.insert(0, str(REPO_ROOT / "scripts" / "swebench"))
import verify_report  # noqa: E402


def _write_summary(run_dir: Path, summary: dict, arm: str = "gt-nolsp") -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    s = {"arm": arm, "task_count": summary.get("task_count", 10)}
    s.update(summary)
    (run_dir / "gt_arm_summary.json").write_text(json.dumps(s))
    # Minimal CSV row so _load_rows doesn't return empty
    (run_dir / "gt_report.csv").write_text(
        "run_id,arm,instance_id,cycle,material_edit_count,ack_armed_count,"
        "steer_delivered_count,ack_engagement_count\n"
        f"r,{arm},task-1,1,1,1,1,1\n"
    )


def _compute(run_dir: Path) -> dict:
    return verify_report.compute(run_dir)


# ──────────────────────────────────────────────────────────────────────────
# Test 1: delivery_rate must be nonzero when raw chain is present
# ──────────────────────────────────────────────────────────────────────────
def test_delivery_rate_nonzero_when_steer_and_ack_present(tmp_path: Path) -> None:
    """Reproduces smoke v5 failure on the nolsp-shaped summary: raw totals
    present but no pre-computed delivery_rate key. Before the fix, compute()
    returns delivery_rate=0.0 (FAIL). After the fix, it must derive the rate
    from the raw totals and return a positive value."""
    _write_summary(tmp_path, {
        "task_count": 10,
        "ack_armed_total": 29,
        "steer_delivered_total": 29,
        "ack_engagement_total": 28,
        "material_edit_total": 29,
        # Deliberately NO delivery_rate / engagement_rate pre-computed.
    })
    metrics = _compute(tmp_path)
    dr = metrics["raw"]["delivery_rate"]
    assert dr is not None and dr > 0.0, (
        f"delivery_rate must be derivable from raw totals when summary lacks "
        f"the pre-computed key. Got {dr!r}. Raw: ack={metrics['raw']['ack_armed_total']}, "
        f"steer={metrics['raw']['steer_delivered_total']}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Test 2: engagement_rate must be nonzero when chain is present
# ──────────────────────────────────────────────────────────────────────────
def test_engagement_rate_nonzero_when_engagement_and_steer_present(tmp_path: Path) -> None:
    """lsp-shaped summary from smoke v5. ack_engagement < steer is the normal
    case; engagement_rate must still be positive, not silently 0.0."""
    _write_summary(tmp_path, {
        "task_count": 10,
        "ack_armed_total": 24,
        "steer_delivered_total": 24,
        "ack_engagement_total": 20,
        "material_edit_total": 24,
    }, arm="gt-lsp-hybrid")
    metrics = _compute(tmp_path)
    er = metrics["raw"]["engagement_rate"]
    assert er is not None and er > 0.0, (
        f"engagement_rate must be derivable from raw totals. Got {er!r}"
    )
    # Sanity: 20/24 = 0.8333
    assert abs(er - (20.0 / 24.0)) < 1e-6, (
        f"engagement_rate formula mismatch: expected {20/24}, got {er}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Test 3: missing denominator must be schema_invalid, NOT silently zero
# ──────────────────────────────────────────────────────────────────────────
def test_missing_ack_armed_produces_schema_invalid_not_zero(tmp_path: Path) -> None:
    """If the denominator (ack_armed_total) is genuinely missing, the rate is
    undefined. verify_report must NOT report 0.0 — that would trip the gate
    for the wrong reason and hide the real schema problem. Accept either a
    None rate, a NaN, or an explicit 'schema_invalid' marker in the output."""
    _write_summary(tmp_path, {
        "task_count": 10,
        "steer_delivered_total": 29,
        "ack_engagement_total": 28,
        "material_edit_total": 29,
        # ack_armed_total deliberately omitted — denominator gone.
    })
    metrics = _compute(tmp_path)
    dr = metrics["raw"]["delivery_rate"]
    status = metrics["raw"].get("delivery_rate_status")
    assert dr is None or status == "schema_invalid" or (
        isinstance(dr, float) and dr != dr  # NaN check
    ), (
        f"Missing denominator must not produce a silent 0.0. Got dr={dr!r}, "
        f"status={status!r}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Test 4: replay the actual failed smoke v5 archive
# ──────────────────────────────────────────────────────────────────────────
FAILED_ARCHIVE = REPO_ROOT / "benchmarks" / "swebench" / "fast_diag" / "smoke_v5_failed_2026-04-24"

@pytest.mark.parametrize("arm_dir", ["nolsp", "lsp"])
def test_failed_smoke_v5_archive_now_passes_rate_gates(arm_dir: str) -> None:
    """End-to-end: the exact artifact that FAILED must now produce rate values
    that satisfy the gate thresholds. This is the load-bearing TTD test —
    before the fix it fails (delivery_rate=0.0), after it passes
    (delivery_rate=1.0, engagement_rate≥0.83)."""
    run_dir = FAILED_ARCHIVE / arm_dir
    assert run_dir.exists(), (
        f"failed archive missing: {run_dir}. Run the archive capture step from "
        f"the plan before these tests."
    )
    metrics = _compute(run_dir)
    dr = metrics["raw"]["delivery_rate"]
    er = metrics["raw"]["engagement_rate"]
    # Thresholds from the rule-of-record (.claude/CLAUDE.md): delivery ≥ 0.65,
    # engagement ≥ 0.80. Both arms, pre-computed by hand from the raw totals,
    # sit at 1.00 / 0.97 (nolsp) and 1.00 / 0.83 (lsp) — both comfortably above
    # the floor.
    assert dr is not None and dr >= 0.65, (
        f"{arm_dir}: delivery_rate must clear the 0.65 floor. Got {dr!r}"
    )
    assert er is not None and er >= 0.80, (
        f"{arm_dir}: engagement_rate must clear the 0.80 floor. Got {er!r}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Test 5: verify_report append CLI still emits the rate rows
# ──────────────────────────────────────────────────────────────────────────
def test_verify_report_append_cli_emits_rate_gates(tmp_path: Path) -> None:
    """Regression guard: the `append` subcommand's markdown table MUST still
    include delivery_rate and engagement_rate rows. A future 'simplification'
    that drops rate gates (or the shared helper misbehaving) fails this test.
    Exercises the real CLI entry point, not an internal helper."""
    _write_summary(tmp_path, {
        "task_count": 10,
        "ack_armed_total": 10,
        "steer_delivered_total": 9,
        "ack_engagement_total": 8,
        "material_edit_total": 10,
    })
    # Isolate: do NOT mutate a real verify_results.md. Point the writer at
    # a throwaway file via the --out flag if supported, else just confirm the
    # stdout table contains both rate labels.
    proc = subprocess.run(
        [sys.executable, str(VERIFY_REPORT), "append", "--run-dir", str(tmp_path)],
        capture_output=True, text=True,
    )
    stdout = proc.stdout + proc.stderr
    assert "delivery_rate" in stdout, (
        "verify_report append must show delivery_rate row in output"
    )
    assert "engagement_rate" in stdout, (
        "verify_report append must show engagement_rate row in output"
    )
