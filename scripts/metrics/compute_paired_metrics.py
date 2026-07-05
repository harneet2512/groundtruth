#!/usr/bin/env python3
"""
compute_paired_metrics.py — GT Metrics Framework (all 26 metrics, M01–M26)

Computes M01–M26 from GT_METRICS_FRAMEWORK.md against a pair of run directories
(baseline vs oracle).  Produces:
  - gt_metrics_report.json  (8-decimal precision, machine-readable)
  - GT_METRICS_REPORT.md    (human-readable)

M20–M26 added 2026-06-11: code-quality / correctness metrics motivated by the
oracle_tenpack_27321848581 run where M13 showed no signal (+1.89 steps, p=0.625)
but trajectory evidence showed the oracle improved code correctness (aiomonitor
4/5 vs 1/5 hidden tests, katex +10, csstree +4, fd +1).

Self-test:
  python scripts/metrics/compute_paired_metrics.py \
      --baseline-dir .claude/reports/runs/tenpack_27307362054 \
      --oracle-dir   .claude/reports/runs/tenpack_27307362054 \
      --output-dir   /tmp/gt_metrics_selftest
Expected: every delta == 0.00000000.

Usage (real paired run):
  python scripts/metrics/compute_paired_metrics.py \
      --baseline-dir .claude/reports/runs/tenpack_27307362054 \
      --oracle-dir   .claude/reports/runs/tenpack_<oracle_run_id> \
      --output-dir   .claude/reports/metrics/<timestamp>
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import scipy.stats as stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FROZEN_BASELINE_MARKER = Path(
    ".claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json"
)

GT_BLOCK_RE = re.compile(r"<gt-[^/][^>]*>.*?</gt-\w[\w-]*>", re.DOTALL)
GT_LOCALIZATION_RE = re.compile(
    r'<gt-localization\s+confidence="([^"]+)">(.*?)</gt-localization>', re.DOTALL
)
GT_CONFIDENCE_RE = re.compile(r'confidence="([^"]+)"')

# File-read commands: cat / head / tail / less / more + a source-code path
FILE_READ_RE = re.compile(
    r'(?:^|\s)(?:cat|head|tail|less|more|bat)\s+(?!>)([^\s|;&><\'"$`]+?'
    r'\.(?:rs|py|ts|tsx|js|jsx|go|java|cpp|c|h|rb|swift|kt|cs|fs|ml|'
    r'scala|lua|php|pl|sh|bash|ex|exs|elm|zig|nim|clj|cr|d|hx|jl|m|mm|'
    r'r|R|R|sql|toml|yaml|yml|json|lock|mod|sum|gradle|makefile|cmake'
    r'))',
    re.IGNORECASE | re.MULTILINE,
)

# Grep reads: grep -r ... <filepath> or rg ... <filepath>
GREP_RE = re.compile(
    r'(?:grep|rg)\s.*?([^\s|;&><\'"$`]+?\.(?:rs|py|ts|tsx|js|jsx|go|java))',
    re.IGNORECASE | re.MULTILINE,
)

# Write commands — cat >, cat >>, tee, sed -i, echo >, python writes
WRITE_CMD_PATTERNS = [
    # cat > <file>  or  cat >> <file>
    re.compile(
        r'cat\s+>>?\s*([^\s|;&><\'"$`]+?'
        r'\.(?:rs|py|ts|tsx|js|jsx|go|java|cpp|c|h|rb|swift|kt|cs|fs|toml|yaml|json))',
        re.IGNORECASE | re.MULTILINE,
    ),
    # tee <file>
    re.compile(
        r'tee\s+([^\s|;&><\'"$`]+?'
        r'\.(?:rs|py|ts|tsx|js|jsx|go|java|cpp|c|h|rb|swift|kt|cs|fs|toml|yaml|json))',
        re.IGNORECASE | re.MULTILINE,
    ),
    # sed -i ... <file>
    re.compile(
        r'sed\s+(?:-i|--in-place)\S*\s+[^\s]+\s+([^\s|;&><\'"$`]+?'
        r'\.(?:rs|py|ts|tsx|js|jsx|go|java|cpp|c|h|rb|swift|kt|cs|fs|toml|yaml|json))',
        re.IGNORECASE | re.MULTILINE,
    ),
    # echo ... > <file>  or  printf ... > <file>
    re.compile(
        r'(?:echo|printf)\s+.*?>>?\s*([^\s|;&><\'"$`]+?'
        r'\.(?:rs|py|ts|tsx|js|jsx|go|java|cpp|c|h|rb|swift|kt|cs|fs|toml|yaml|json))',
        re.IGNORECASE | re.MULTILINE,
    ),
    # python3 -c "... open('<file>', 'w') ..."
    re.compile(
        r"""python3?\s+-c\s+["'].*?open\(['"]([^'"]+\.(?:rs|py|ts|js|go|java|cpp|c|h|rb|swift|kt|cs))['"]\s*,\s*['"]w['"]""",
        re.IGNORECASE | re.MULTILINE,
    ),
    # python3 << 'HEREDOC' ... open('file', 'w') or open('file', 'r+') ...
    # Matches the heredoc pattern used by mini-swe-agent for multi-line python writes
    re.compile(
        r"""open\(['"]([^'"]+\.(?:rs|py|ts|tsx|js|jsx|go|java|cpp|c|h|rb|swift|kt|cs|fs|toml|yaml|json))['"]\s*,\s*['"]w""",
        re.IGNORECASE | re.MULTILINE,
    ),
    # cp /tmp/new.ts target.ts  or  mv /tmp/new.ts target.ts
    # Matches the /tmp-stage-then-cp pattern used by mini-swe-agent
    re.compile(
        r'\b(?:cp|mv)\s+\S+\s+([^\s|;&><\'"$`]+?'
        r'\.(?:rs|py|ts|tsx|js|jsx|go|java|cpp|c|h|rb|swift|kt|cs|fs|toml|yaml|json))',
        re.IGNORECASE | re.MULTILINE,
    ),
]

# Test runner patterns (cargo test / pytest / npm test / etc.)
TEST_RUNNER_RE = re.compile(
    r'(?:cargo\s+test|pytest|python\s+-m\s+pytest|npm\s+(?:test|run\s+test)|'
    r'yarn\s+test|npx\s+(?:jest|mocha)|go\s+test|mvn\s+test|gradle\s+test|'
    r'bundle\s+exec\s+rspec)',
    re.IGNORECASE,
)

# GT block markers for delivery detection
GT_MARKER_RE = re.compile(r'<(gt-[a-z\-]+)', re.IGNORECASE)

# Localization numbered file list:  "  N. <filepath> —" or "  N. <filepath> ::"
LOC_FILE_RE = re.compile(r'^\s*\d+\.\s+([^\s—:]+(?:\.[\w]+))', re.MULTILINE)

# Scaffold paths
SCAFFOLD_PATH_RE = re.compile(r'^/tmp/', re.IGNORECASE)

# Patch file path extractor
PATCH_FILE_RE = re.compile(r'^\+\+\+ b/(.+)$', re.MULTILINE)

# ---------------------------------------------------------------------------
# M20–M26: verifier output parsing constants
# ---------------------------------------------------------------------------

# ANSI escape code stripper — verifier output is ANSI-colorized; strip before regex
ANSI_ESCAPE_RE = re.compile(r'\x1B\[[0-9;]*[mK]')

# Step 4 section separator in verifier/test-stdout.txt
VERIFIER_STEP4_RE = re.compile(
    r'\[verifier\] --- Step 4: Running new tests ---(.*)$',
    re.DOTALL,
)

# Python/pytest summary: "N passed, M failed"
PYTEST_SUMMARY_RE = re.compile(
    r'(\d+)\s+passed(?:,\s*(\d+)\s+failed)?',
    re.IGNORECASE,
)
PYTEST_FAIL_ONLY_RE = re.compile(r'(\d+)\s+failed', re.IGNORECASE)

# Rust/cargo: "test result: FAILED. N passed; M failed" or "test result: ok. N passed"
CARGO_RESULT_RE = re.compile(
    r'test result: (?:ok|FAILED)\.\s+(\d+)\s+passed;\s+(\d+)\s+failed',
    re.IGNORECASE,
)
CARGO_OK_RE = re.compile(r'test result: ok\.\s+(\d+)\s+passed', re.IGNORECASE)

# Go: count --- PASS: and --- FAIL: prefix lines
GO_PASS_RE = re.compile(r'^--- PASS:', re.MULTILINE)
GO_FAIL_RE = re.compile(r'^--- FAIL:', re.MULTILINE)

# Node/mocha: "  N passing" and "  N failing"
MOCHA_PASSING_RE = re.compile(r'(\d+)\s+passing', re.IGNORECASE)
MOCHA_FAILING_RE = re.compile(r'(\d+)\s+failing', re.IGNORECASE)

# TypeScript compile error marker
TS_COMPILE_ERR_RE = re.compile(r'error TS\d+:', re.IGNORECASE)

# Rust compile error marker
RUST_COMPILE_ERR_RE = re.compile(
    r'error\[E\d+\]:|error: could not compile', re.IGNORECASE
)

# Python import/syntax error markers
PYTHON_IMPORT_ERR_RE = re.compile(
    r'(?:SyntaxError|ImportError|ModuleNotFoundError):', re.IGNORECASE
)

# Branch-flow keywords for M23 cyclomatic proxy
BRANCH_KEYWORD_RE = re.compile(
    r'\b(if|else|elif|for|while|match|switch|case|try|catch|except|finally|'
    r'break|continue|return|throw|raise|yield)\b'
)

# New function/method definition for M23
FUNCTION_DEF_RE = re.compile(
    r'^\+\s*(?:async\s+)?(?:def |fn |function |class |impl |pub\s+fn |pub\s+async\s+fn )',
    re.MULTILINE,
)

# Self-correction: test failure indicators in tool response text
TEST_FAIL_INDICATOR_RE = re.compile(
    r'(?:FAILED|FAIL:|error\[|TypeError|AssertionError|panicked at|'
    r'test result: FAILED|Tests:.*\d+ failed|failing)',
    re.IGNORECASE,
)

N_BOOTSTRAP = 10_000
FLOAT_FMT = ":.8f"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TaskMetrics:
    task_id: str = ""
    # Provenance
    resolved: bool = False
    n_agent_steps: float = 0.0
    llm_calls: float = 0.0
    llm_tokens_in: float = 0.0
    llm_tokens_out: float = 0.0
    llm_tokens_cached: float = 0.0
    llm_cost_usd: float = 0.0
    gt_observation_chars_total: float = 0.0

    # M01
    m01_steps_to_first_gold_read: float = math.nan
    m01_steps_to_first_gold_read_normalized: float = math.nan
    m01_available: bool = False

    # M02
    m02_pre_edit_exploration_steps: float = math.nan
    m02_available: bool = False

    # M03
    m03_total_steps: float = 0.0

    # M04
    m04_brief_precision_at_3: float = math.nan
    m04_brief_recall_at_3: float = math.nan
    m04_brief_hit_at_1: float = math.nan
    m04_brief_top3: List[str] = field(default_factory=list)
    m04_brief_confidence: str = "unknown"
    m04_available: bool = False

    # M05
    m05_consumption_rate_explicit: float = math.nan
    m05_consumption_rate_behavioral: float = math.nan
    m05_consumption_rate_inert: float = math.nan
    m05_harm_rate: float = math.nan
    m05_deliveries_total: float = 0.0
    m05_consumption_grade_source: str = "trajectory_automated"
    m05_available: bool = False

    # M06
    m06_edited_file_count: float = 0.0
    m06_task_type: str = "unknown"
    m06_available: bool = False

    # M07 — oracle-era only; proxy for baseline
    m07_suppression_precision: float = math.nan
    m07_available: bool = False

    # M08
    m08_resolved_given_consumed: float = math.nan
    m08_available: bool = False

    # M09
    m09_steps_to_resolution: float = math.nan
    m09_first_edit_step: float = math.nan
    m09_first_edit_step_confidence: str = "UNKNOWN"
    m09_available: bool = False

    # M10
    m10_regression_rate: float = math.nan
    m10_available: bool = False

    # M11
    m11_high_confidence_pins: float = 0.0
    m11_wrong_high_confidence_pins: float = 0.0
    m11_inverted_confidence_rate: float = math.nan
    m11_available: bool = False

    # M12
    m12_emission_precision: float = math.nan
    m12_emission_precision_confidence: str = "LOW"
    m12_available: bool = False

    # M13
    m13_steps_to_gold_edit: float = math.nan
    m13_steps_to_gold_edit_confidence: str = "UNKNOWN"
    m13_available: bool = False

    # M14
    m14_gt_injected_tokens_approx: float = 0.0
    m14_gt_token_count_source: str = "chars_proxy"
    m14_gt_injection_overhead_pct: float = 0.0
    m14_available: bool = False

    # M15
    m15_precision_high: float = math.nan
    m15_precision_medium: float = math.nan
    m15_precision_low: float = math.nan
    m15_confidence_calibration_score: float = math.nan
    m15_available: bool = False

    # M16
    m16_scaffold_waste_rate: float = math.nan
    m16_scaffold_writes: float = 0.0
    m16_total_writes: float = 0.0
    m16_available: bool = False

    # M17
    m17_wasted_steps_gt_caused: float = 0.0
    m17_wasted_step_rate: float = math.nan
    m17_confidence: str = "AUTOMATED_HEURISTIC"
    m17_available: bool = False

    # M18
    m18_exploration_waste_rate: float = math.nan
    m18_pre_edit_useful_reads: float = 0.0
    m18_pre_edit_total_reads: float = 0.0
    m18_available: bool = False

    # M19
    m19_test_evidence_delivered: float = 0.0
    m19_test_evidence_consumed: float = 0.0
    m19_available: bool = False

    # M20 — Hidden Test Pass Rate
    m20_hidden_tests_pass: float = math.nan
    m20_hidden_tests_total: float = math.nan
    m20_hidden_test_pass_rate: float = math.nan
    m20_compile_fail: bool = False
    m20_available: bool = False

    # M21 — Patch Completeness
    m21_patch_completeness: float = math.nan
    m21_obligations_total: float = 0.0
    m21_obligations_addressed: float = 0.0
    m21_available: bool = False

    # M22 — Patch Correctness (compile/import clean)
    m22_compile_clean: float = math.nan
    m22_compile_error_count: float = 0.0
    m22_available: bool = False

    # M23 — Implementation Depth
    m23_patch_lines_added: float = 0.0
    m23_branch_density: float = math.nan
    m23_new_functions: float = 0.0
    m23_implementation_depth_score: float = math.nan
    m23_available: bool = False

    # M24 — Requirement Fidelity
    m24_patch_fidelity: float = math.nan
    m24_n_checkable: float = 0.0
    m24_available: bool = False

    # M25 — Test Evidence Before Submit
    m25_test_coverage_before_submit: float = math.nan
    m25_test_runs_post_edit: float = 0.0
    m25_available: bool = False

    # M26 — Agent Self-Correction Events
    m26_self_correction_events: float = math.nan
    m26_available: bool = False


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _f8(v: Any) -> float:
    """Return float rounded to 8 decimal places; nan stays nan."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return math.nan
    return round(float(v), 8)


def _safe_div(num: float, denom: float, fallback: float = math.nan) -> float:
    if denom == 0 or math.isnan(denom):
        return fallback
    return num / denom


def _normalise_path(p: str) -> str:
    """Strip leading ./ and normalise slashes."""
    return p.lstrip("./").replace("\\", "/").strip()


def _extract_edited_files(submission: str) -> Set[str]:
    """Extract the set of files in the submission patch (from +++ b/ lines)."""
    files: Set[str] = set()
    for m in PATCH_FILE_RE.finditer(submission):
        files.add(_normalise_path(m.group(1)))
    return files


def _extract_file_reads(messages: List[Dict]) -> List[Tuple[int, str]]:
    """
    Extract (assistant_step_idx, filepath) for each file-read command.
    assistant_step_idx = 1-based index counting only assistant messages.
    """
    reads: List[Tuple[int, str]] = []
    step = 0
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        step += 1
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            if fn.get("name") != "bash":
                continue
            args = fn.get("arguments", "")
            if isinstance(args, str):
                try:
                    args_d = json.loads(args)
                    cmd = args_d.get("command", "")
                except Exception:
                    cmd = args
            else:
                cmd = args.get("command", "") if isinstance(args, dict) else ""
            for m in FILE_READ_RE.finditer(cmd):
                reads.append((step, _normalise_path(m.group(1))))
            # Also grep reads
            for m in GREP_RE.finditer(cmd):
                reads.append((step, _normalise_path(m.group(1))))
    return reads


def _extract_writes(messages: List[Dict]) -> List[Tuple[int, str]]:
    """
    Extract (assistant_step_idx, filepath) for each write command.
    """
    writes: List[Tuple[int, str]] = []
    step = 0
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        step += 1
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            if fn.get("name") != "bash":
                continue
            args = fn.get("arguments", "")
            if isinstance(args, str):
                try:
                    args_d = json.loads(args)
                    cmd = args_d.get("command", "")
                except Exception:
                    cmd = args
            else:
                cmd = args.get("command", "") if isinstance(args, dict) else ""
            for pattern in WRITE_CMD_PATTERNS:
                for m in pattern.finditer(cmd):
                    writes.append((step, _normalise_path(m.group(1))))
    return writes


def _extract_gt_deliveries(messages: List[Dict]) -> List[Tuple[int, str]]:
    """
    Extract (message_idx, gt_block_text) for each GT payload block delivered
    to the agent (tool messages or user messages containing <gt-* blocks).
    message_idx = 0-based index in messages array.
    """
    deliveries: List[Tuple[int, str]] = []
    for idx, msg in enumerate(messages):
        role = msg.get("role", "")
        # GT content appears in user message (initial brief) or tool messages
        if role not in ("user", "tool"):
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            text = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            )
        else:
            text = str(content)
        for m in GT_BLOCK_RE.finditer(text):
            deliveries.append((idx, m.group(0)))
    return deliveries


def _extract_gt_delivery_step(msg_idx: int, messages: List[Dict]) -> int:
    """
    Convert a message index to the assistant step that will SEE this content.
    Tool messages (role=tool) are responses to assistant actions; the NEXT
    assistant message is the consumer.  Return the next assistant step index.
    """
    step = 0
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            step += 1
            if i > msg_idx:
                return step
    return step  # last step if none follows


def _parse_brief_localization(brief_text: str) -> Tuple[str, List[str]]:
    """
    Parse the <gt-localization> block from brief.txt.
    Returns (confidence_str, [file1, file2, file3, ...]).
    """
    m = GT_LOCALIZATION_RE.search(brief_text)
    if not m:
        return "unknown", []
    confidence = m.group(1).strip().lower()
    body = m.group(2)

    files: List[str] = []
    # Numbered list: "  N. <filepath> — ..." or "Edit target: <filepath> ::"
    # Try numbered list first
    numbered = LOC_FILE_RE.findall(body)
    if numbered:
        files = [_normalise_path(f) for f in numbered]
    else:
        # Single "Edit target: <filepath> ::" line, OR the ledger HIGH head
        # "<filepath> :: <func> — N/3 signals agree" (GT_CONSENSUS_LEDGER; Fable C5)
        et_match = re.search(r'Edit target:\s+([^\s:]+)', body) or \
            re.search(r'(?m)^\s*([\w.\-/]+\.\w+)\s*::.*signals agree', body)
        if et_match:
            files = [_normalise_path(et_match.group(1))]
    return confidence, files


def _get_next_3_assistant_steps(msg_idx: int, messages: List[Dict]) -> List[str]:
    """
    Collect bash commands from the next 3 assistant turns after msg_idx.
    Used for BEHAVIORAL consumption check.
    """
    cmds: List[str] = []
    count = 0
    for i in range(msg_idx + 1, len(messages)):
        m = messages[i]
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls", []):
            fn = tc.get("function", {})
            if fn.get("name") == "bash":
                args = fn.get("arguments", "")
                if isinstance(args, str):
                    try:
                        args_d = json.loads(args)
                        cmd = args_d.get("command", "")
                    except Exception:
                        cmd = args
                else:
                    cmd = args.get("command", "") if isinstance(args, dict) else ""
                cmds.append(cmd)
        count += 1
        if count >= 3:
            break
    return cmds


def _files_cited_in_gt_block(block: str) -> List[str]:
    """Extract file paths mentioned in a GT block."""
    # Match patterns like:  word.ext  or  path/to/word.ext
    file_re = re.compile(
        r'([a-zA-Z0-9_\-/\.]+\.(?:rs|py|ts|tsx|js|jsx|go|java|cpp|c|h|rb|'
        r'swift|kt|cs|fs|toml|yaml|json|lock|mod|sum))',
        re.IGNORECASE,
    )
    return [_normalise_path(f) for f in file_re.findall(block)]


# ---------------------------------------------------------------------------
# M20–M26 helper functions
# ---------------------------------------------------------------------------

def _find_verifier_stdout(task_dir: Path) -> Optional[Path]:
    """Find verifier/test-stdout.txt in the task directory's job subdirectory."""
    # Path: jobs/<date>/<trial>/verifier/test-stdout.txt
    jobs_dir = task_dir / "jobs"
    if not jobs_dir.is_dir():
        return None
    for job_date_dir in sorted(jobs_dir.iterdir()):
        if not job_date_dir.is_dir():
            continue
        for trial_dir in sorted(job_date_dir.iterdir()):
            if not trial_dir.is_dir():
                continue
            p = trial_dir / "verifier" / "test-stdout.txt"
            if p.exists():
                return p
    return None


def _find_anchors_path(task_dir: Path) -> Optional[Path]:
    """Find gt_artifacts/gt_issue_anchors.json."""
    p = task_dir / "gt_artifacts" / "gt_issue_anchors.json"
    return p if p.exists() else None


def _parse_verifier_hidden_tests(verifier_stdout: str) -> Tuple[int, int, bool, int]:
    """
    Parse Step 4 (hidden test) section from verifier/test-stdout.txt.
    Returns: (pass_count, total_count, compile_fail, compile_error_count)

    Strategy:
    1. Extract Step 4 section only (after "[verifier] --- Step 4:").
    2. Try framework-specific patterns in order.
    3. compile_fail = True if compile error markers present with no test output.
    """
    # Extract Step 4 section
    m = VERIFIER_STEP4_RE.search(verifier_stdout)
    if not m:
        return 0, 0, False, 0
    step4 = m.group(1)

    # Check compile failures first (they produce 0 test output)
    ts_errors = len(TS_COMPILE_ERR_RE.findall(step4))
    rust_errors = len(RUST_COMPILE_ERR_RE.findall(step4))
    py_errors = len(PYTHON_IMPORT_ERR_RE.findall(step4))
    compile_error_count = ts_errors + rust_errors + py_errors

    # TypeScript: if TS errors present AND no tests actually ran, compile fail
    if ts_errors > 0:
        # "Tests: 0 total" or no "Tests:" line at all → 0 tests ran → compile fail
        # "Tests: N passed" (N>0) → some tests ran despite TS errors in other files
        tests_ran_match = re.search(r'Tests:\s+(\d+)\s+(?:passed|failed)', step4)
        if not tests_ran_match:
            return 0, 0, True, compile_error_count

    # Rust: if "could not compile" present
    if re.search(r'error: could not compile', step4, re.IGNORECASE):
        return 0, 0, True, compile_error_count

    # Rust cargo test result line (most reliable)
    cargo_m = CARGO_RESULT_RE.search(step4)
    if cargo_m:
        passed = int(cargo_m.group(1))
        failed = int(cargo_m.group(2))
        return passed, passed + failed, False, compile_error_count
    cargo_ok_m = CARGO_OK_RE.search(step4)
    if cargo_ok_m:
        passed = int(cargo_ok_m.group(1))
        return passed, passed, False, compile_error_count

    # Go: count --- PASS: and --- FAIL: lines
    go_passes = len(GO_PASS_RE.findall(step4))
    go_fails = len(GO_FAIL_RE.findall(step4))
    if go_passes + go_fails > 0:
        return go_passes, go_passes + go_fails, False, compile_error_count

    # Node/mocha: "N passing" and "N failing"
    mocha_pass_m = MOCHA_PASSING_RE.search(step4)
    mocha_fail_m = MOCHA_FAILING_RE.search(step4)
    if mocha_pass_m:
        passed = int(mocha_pass_m.group(1))
        failed = int(mocha_fail_m.group(1)) if mocha_fail_m else 0
        return passed, passed + failed, False, compile_error_count

    # Python/pytest: "N passed, M failed" summary line
    pytest_m = PYTEST_SUMMARY_RE.search(step4)
    if pytest_m:
        passed = int(pytest_m.group(1))
        failed = int(pytest_m.group(2)) if pytest_m.group(2) else 0
        return passed, passed + failed, False, compile_error_count

    # Pytest fail-only (stop-at-first-failure): "N failed" without pass count
    pytest_fail_m = PYTEST_FAIL_ONLY_RE.search(step4)
    if pytest_fail_m:
        # Only know how many failed; passed count is from prior PASSED lines
        passed_lines = len(re.findall(r'PASSED', step4))
        failed = int(pytest_fail_m.group(1))
        return passed_lines, passed_lines + failed, False, compile_error_count

    # TypeScript Jest "Tests: N failed, M passed" format
    jest_m = re.search(r'Tests:\s+(?:(\d+)\s+failed,\s+)?(\d+)\s+passed', step4)
    if jest_m:
        failed = int(jest_m.group(1)) if jest_m.group(1) else 0
        passed = int(jest_m.group(2))
        return passed, passed + failed, False, compile_error_count

    # Fall through: can't parse
    return 0, 0, (compile_error_count > 0), compile_error_count


def _parse_patch_for_code_quality(submission: str) -> Tuple[int, float, int]:
    """
    Parse the submission patch for M23 metrics.
    Returns: (lines_added, branch_density, new_functions)
    """
    added_lines = [
        line[1:]  # strip the leading '+'
        for line in submission.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    lines_added = len(added_lines)
    if lines_added == 0:
        return 0, 0.0, 0

    added_text = "\n".join(added_lines)
    branch_count = len(BRANCH_KEYWORD_RE.findall(added_text))
    branch_density = branch_count / lines_added

    # Count new function definitions (from the raw patch lines, not stripped)
    new_functions = len(FUNCTION_DEF_RE.findall(submission))

    return lines_added, branch_density, new_functions


def _compute_patch_completeness(obligations: List[Dict], submission: str) -> Tuple[float, int, int]:
    """
    M21: For each obligation, check if any of its symbols/keywords appear in '+' lines.
    Returns: (completeness_rate, total_obligations, addressed_count)
    """
    if not obligations:
        return math.nan, 0, 0

    plus_lines = " ".join(
        line[1:].lower()
        for line in submission.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )

    addressed = 0
    for ob in obligations:
        symbols = [s.lower() for s in ob.get("symbols", [])]
        keywords = [k.lower() for k in ob.get("keywords", [])]
        all_terms = symbols + keywords
        if any(term in plus_lines for term in all_terms if len(term) > 2):
            addressed += 1

    total = len(obligations)
    return _safe_div(float(addressed), float(total), math.nan), total, addressed


def _compute_requirement_fidelity(obligations: List[Dict], submission: str) -> Tuple[float, int]:
    """
    M24: For each obligation with checkable_forms, verify all forms appear in '+' lines.
    Returns: (fidelity_rate, n_checkable)
    """
    plus_lines = " ".join(
        line[1:].lower()
        for line in submission.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )

    checkable = [ob for ob in obligations if ob.get("checkable_forms")]
    if not checkable:
        return math.nan, 0

    correct = 0
    for ob in checkable:
        forms = [cf.lower() for cf in ob.get("checkable_forms", [])]
        if all(f in plus_lines for f in forms):
            correct += 1

    return _safe_div(float(correct), float(len(checkable)), math.nan), len(checkable)


def _count_self_correction_events(messages: List[Dict], file_writes: List[Tuple[int, str]]) -> int:
    """
    M26: Count (write_F, test_run, test_fail, write_F_again) patterns.
    Window: within 10 steps of initial write.
    """
    events = 0
    # Build step-indexed view of writes and test runs
    write_by_step: Dict[int, List[str]] = {}
    for step, path in file_writes:
        write_by_step.setdefault(step, []).append(path)

    # Extract test run steps and their response content
    test_run_steps: List[Tuple[int, str]] = []  # (step, response_text)
    step = 0
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        step += 1
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            if fn.get("name") != "bash":
                continue
            args = fn.get("arguments", "")
            if isinstance(args, str):
                try:
                    args_d = json.loads(args)
                    cmd = args_d.get("command", "")
                except Exception:
                    cmd = args
            else:
                cmd = args.get("command", "") if isinstance(args, dict) else ""
            if TEST_RUNNER_RE.search(cmd):
                # Get the response (tool message following this)
                response_text = ""
                for j in range(i + 1, min(i + 3, len(messages))):
                    if messages[j].get("role") == "tool":
                        content = messages[j].get("content", "")
                        if isinstance(content, list):
                            response_text = " ".join(
                                c.get("text", "") for c in content if isinstance(c, dict)
                            )
                        else:
                            response_text = str(content)
                        break
                test_run_steps.append((step, response_text))

    # For each write step, look for: test_run(within +10) + test_fail + write_same_file(within +10)
    WINDOW = 10
    for write_step, paths in sorted(write_by_step.items()):
        for path in paths:
            # Find a test run within the window that failed
            for test_step, test_response in test_run_steps:
                if test_step <= write_step:
                    continue
                if test_step > write_step + WINDOW:
                    break
                if not TEST_FAIL_INDICATOR_RE.search(test_response):
                    continue
                # Found a failure — look for a re-write of the same file
                for re_write_step, re_write_path in file_writes:
                    if re_write_step <= test_step:
                        continue
                    if re_write_step > test_step + WINDOW:
                        break
                    if re_write_path == path or (
                        re_write_path.endswith("/" + path.split("/")[-1])
                        and path.split("/")[-1] != ""
                    ):
                        events += 1
                        break  # count once per initial write
    return events


def _bootstrap_ci(
    deltas: List[float],
    n_resamples: int = N_BOOTSTRAP,
    ci: float = 0.95,
) -> Tuple[float, float, float]:
    """
    Bootstrap 95% CI on the mean delta.
    Returns (mean_delta, ci_lo, ci_hi).
    """
    if not deltas:
        return math.nan, math.nan, math.nan
    arr = np.array(deltas, dtype=float)
    rng = np.random.default_rng(seed=42)
    boot_means = np.array(
        [rng.choice(arr, size=len(arr), replace=True).mean() for _ in range(n_resamples)]
    )
    lo = float(np.percentile(boot_means, (1 - ci) / 2 * 100))
    hi = float(np.percentile(boot_means, (1 + ci) / 2 * 100))
    return float(arr.mean()), lo, hi


def _wilcoxon_test(
    deltas: List[float],
    alternative: str = "two-sided",
) -> Dict[str, Any]:
    """Run Wilcoxon signed-rank on the deltas.  Returns stats dict."""
    valid = [d for d in deltas if not math.isnan(d)]
    if len(valid) < 2:
        return {"W": math.nan, "p_value": math.nan, "n": len(valid), "error": "insufficient_data"}
    # scipy.stats.wilcoxon requires non-zero differences; handle ties gracefully
    non_zero = [d for d in valid if d != 0.0]
    if not non_zero:
        return {
            "W": 0.0,
            "p_value": 1.0,
            "n": len(valid),
            "note": "all_deltas_zero",
        }
    try:
        result = stats.wilcoxon(non_zero, alternative=alternative)
        return {
            "W": float(result.statistic),
            "p_value": float(result.pvalue),
            "n": len(valid),
        }
    except Exception as exc:
        return {"W": math.nan, "p_value": math.nan, "n": len(valid), "error": str(exc)}


def _mcnemar_test(b: int, c: int) -> Dict[str, Any]:
    """McNemar test on off-diagonal counts b (baseline yes, oracle no) / c (oracle yes, baseline no)."""
    if b + c == 0:
        return {"b": b, "c": c, "p_value": 1.0, "significant_p05": False, "note": "no_discordant_pairs"}
    result = stats.binomtest(c, b + c, p=0.5)
    return {
        "b": b,
        "c": c,
        "p_value": float(result.pvalue),
        "significant_p05": float(result.pvalue) < 0.05,
    }


def _flips_mcnemar(flip_count: int, regression_count: int, n_eligible: int) -> Dict[str, Any]:
    """Significance of the flip/regression asymmetry via McNemar on the DISCORDANT
    pairs (G9). The old test was ``binomtest(flip_count, n_flip_eligible, p=0.5)`` —
    a nonsense null: it asks whether HALF of all baseline-unresolved tasks flip,
    which no realistic system approaches, so it can essentially never fire. The
    correct paired test compares flips (oracle resolved, baseline not) against
    regressions (baseline resolved, oracle not) — exactly the discordant cells
    McNemar operates on, the same test the file already uses for M11/M22/M25."""
    out = {
        **_mcnemar_test(regression_count, flip_count),  # b=regressions, c=flips
        "flips_observed": flip_count,
        "regressions_observed": regression_count,
        "n_eligible": n_eligible,
        "direction": "oracle_higher",
        "test": "mcnemar_discordant_pairs",
    }
    return out


def _apply_holm(tests: Dict[str, Dict], alpha: float = 0.05) -> None:
    """Holm–Bonferroni step-down FWER control over the per-metric significance
    battery (G9), IN PLACE. Each test independently set ``significant_p05`` from its
    own raw p<0.05; with ~15 one-sided tests the family-wise error rate is ~54%.
    Holm controls it at ``alpha`` without assuming independence. The raw decision is
    preserved under ``significant_p05_raw`` and ``significant_p05`` is OVERWRITTEN
    with the corrected decision so every downstream reader is corrected by default.
    Tests with no numeric p_value (e.g. all-NaN Wilcoxon, empty flip pool) are left
    untouched — their ``significant_p05`` is already False."""
    entries = []
    for name, t in tests.items():
        if not isinstance(t, dict):
            continue
        p = t.get("p_value")
        if isinstance(p, bool) or not isinstance(p, (int, float)):
            continue
        entries.append((name, float(p)))
    entries.sort(key=lambda kv: kv[1])
    m = len(entries)
    still_rejecting = True
    for i, (name, p) in enumerate(entries):
        tests[name]["significant_p05_raw"] = bool(tests[name].get("significant_p05", False))
        thresh = alpha / (m - i)  # Holm: alpha/(m), alpha/(m-1), ... alpha/1
        reject = still_rejecting and (p <= thresh)
        if not reject:
            still_rejecting = False
        tests[name]["significant_p05"] = bool(reject)
        tests[name]["holm_threshold"] = round(thresh, 10)
    tests["_holm"] = {"n_tests": m, "alpha": alpha, "method": "holm_bonferroni_step_down"}


# ---------------------------------------------------------------------------
# Task loader
# ---------------------------------------------------------------------------

def _find_trajectory_path(task_dir: Path) -> Optional[Path]:
    """Find mini-swe-agent.trajectory.json under task_dir."""
    for p in task_dir.rglob("mini-swe-agent.trajectory.json"):
        return p
    return None


def _find_deep_metrics_path(task_dir: Path, task_id: str) -> Optional[Path]:
    p = task_dir / f"gt_deep_metrics_{task_id}.json"
    if p.exists():
        return p
    # Some runs use a slightly different name; try glob
    candidates = list(task_dir.glob("gt_deep_metrics_*.json"))
    return candidates[0] if candidates else None


def _find_brief_path(task_dir: Path) -> Optional[Path]:
    p = task_dir / "gt_artifacts" / "brief.txt"
    return p if p.exists() else None


def _find_outcome_path(task_dir: Path) -> Optional[Path]:
    p = task_dir / "outcome.json"
    return p if p.exists() else None


def _find_task_truth_path(task_dir: Path) -> Optional[Path]:
    p = task_dir / "task_truth.json"
    return p if p.exists() else None


def _resolved_from_task_truth(truth: dict) -> tuple[bool | None, dict]:
    """Prefer reconciled task_truth outcome over raw pier outcome.json (P0-06)."""
    outcome = truth.get("outcome") or {}
    if outcome.get("resolved") is not None:
        return bool(outcome["resolved"]), outcome
    if outcome.get("failure_class") == "RESOLVED":
        return True, outcome
    if outcome.get("failure_class"):
        return False, outcome
    return None, outcome


# ---------------------------------------------------------------------------
# Core metrics computation per task
# ---------------------------------------------------------------------------

def compute_task_metrics(task_id: str, task_dir: Path) -> Optional[TaskMetrics]:
    traj_path = _find_trajectory_path(task_dir)
    if not traj_path:
        print(f"  [WARN] {task_id}: no trajectory found in {task_dir}", file=sys.stderr)
        return None

    with open(traj_path, encoding="utf-8", errors="replace") as f:
        traj = json.load(f)

    messages = traj.get("messages", [])
    info = traj.get("info", {})
    model_stats = info.get("model_stats", {})
    submission = info.get("submission", "")

    # Load deep metrics (optional)
    dm_path = _find_deep_metrics_path(task_dir, task_id)
    dm: Dict = {}
    if dm_path:
        try:
            with open(dm_path, encoding="utf-8", errors="replace") as f:
                dm = json.load(f)
        except Exception:
            pass

    # Load brief (optional)
    brief_path = _find_brief_path(task_dir)
    brief_text = ""
    if brief_path:
        try:
            with open(brief_path, encoding="utf-8", errors="replace") as f:
                brief_text = f.read()
        except Exception:
            pass

    # Load outcome — task_truth.json is authoritative when present (P0-06).
    truth_path = _find_task_truth_path(task_dir)
    truth_data: Dict = {}
    if truth_path:
        try:
            with open(truth_path, encoding="utf-8", errors="replace") as f:
                truth_data = json.load(f)
        except Exception:
            pass

    outcome_path = _find_outcome_path(task_dir)
    outcome_data: Dict = {}
    if outcome_path:
        try:
            with open(outcome_path, encoding="utf-8", errors="replace") as f:
                outcome_data = json.load(f)
        except Exception:
            pass

    m = TaskMetrics(task_id=task_id)

    # ----- Resolved -----
    tasks_list = outcome_data.get("tasks", [{}])
    task_record = tasks_list[0] if tasks_list else {}
    resolved_truth, _outcome_block = _resolved_from_task_truth(truth_data)
    if resolved_truth is not None:
        m.resolved = resolved_truth
    else:
        m.resolved = bool(task_record.get("reward", 0) > 0)
    m.n_agent_steps = float(
        _outcome_block.get("n_agent_steps")
        if truth_data and _outcome_block.get("n_agent_steps") is not None
        else task_record.get("n_agent_steps", model_stats.get("api_calls", 0))
    )

    # ----- Efficiency fields from deep metrics -----
    eff = dm.get("efficiency", {})
    agent_dm = dm.get("agent", {})
    gt_delivery_dm = dm.get("gt_delivery", {})

    m.llm_calls = float(eff.get("llm_calls", model_stats.get("api_calls", m.n_agent_steps)))
    m.llm_tokens_in = float(eff.get("llm_tokens_in", 0.0))
    m.llm_tokens_out = float(eff.get("llm_tokens_out", 0.0))
    m.llm_tokens_cached = float(eff.get("llm_tokens_cached", 0.0))
    m.llm_cost_usd = float(eff.get("llm_cost_usd", 0.0))
    m.gt_observation_chars_total = float(
        gt_delivery_dm.get("gt_observation_chars_total",
                           agent_dm.get("gt_observation_chars_total", 0.0))
    )

    # ----- Edited file set -----
    edited_files: Set[str] = _extract_edited_files(submission)
    m.m06_edited_file_count = float(len(edited_files))
    if len(edited_files) == 0:
        m.m06_task_type = "no_patch"
    elif len(edited_files) == 1:
        m.m06_task_type = "single_file"
    elif len(edited_files) == 2:
        m.m06_task_type = "multi_file"
    else:
        m.m06_task_type = "deep_multi_file"
    m.m06_available = len(edited_files) > 0

    # ----- File reads and writes -----
    file_reads = _extract_file_reads(messages)   # [(step, path), ...]
    file_writes = _extract_writes(messages)       # [(step, path), ...]

    # Total steps = number of assistant messages
    total_steps = sum(1 for msg in messages if msg.get("role") == "assistant")
    m.m03_total_steps = float(total_steps)

    # ----- M13 / M09: steps-to-gold-edit -----
    # First step that writes a file in edited_files
    gold_write_steps = [
        step for step, path in file_writes
        if any(path.endswith(ef) or ef.endswith(path) or path == ef
               for ef in edited_files)
    ]
    if gold_write_steps:
        first_gold_edit = min(gold_write_steps) - 1
        m.m13_steps_to_gold_edit = float(max(0, first_gold_edit))
        m.m13_steps_to_gold_edit_confidence = "HIGH"
        m.m13_available = True
    else:
        # Fallback: first_edit_action from deep metrics
        fea = float(agent_dm.get("first_edit_action", 0.0))
        if fea > 0:
            m.m13_steps_to_gold_edit = float(max(0, fea - 1))
            m.m13_steps_to_gold_edit_confidence = "LOW (deep_metrics_fallback)"
            m.m13_available = True
        # else: not available

    # M09: steps_to_resolution = steps_to_gold_edit when that's available, else total
    m.m09_first_edit_step = m.m13_steps_to_gold_edit
    m.m09_first_edit_step_confidence = m.m13_steps_to_gold_edit_confidence
    m.m09_steps_to_resolution = m.n_agent_steps  # always available
    m.m09_available = True

    # ----- M01: steps-to-first-gold-read -----
    gold_read_steps = [
        step for step, path in file_reads
        if any(path.endswith(ef) or ef.endswith(path) or path == ef
               for ef in edited_files)
    ]
    if gold_read_steps:
        m.m01_steps_to_first_gold_read = float(max(0, min(gold_read_steps) - 1))
        m.m01_steps_to_first_gold_read_normalized = _safe_div(
            m.m01_steps_to_first_gold_read, float(total_steps), math.nan
        )
        m.m01_available = True

    # ----- M02: pre-edit exploration steps -----
    if m.m13_available and not math.isnan(m.m13_steps_to_gold_edit):
        first_edit_step = m.m13_steps_to_gold_edit + 1  # back to 1-based
        pre_edit_reads = [
            path for step, path in file_reads
            if step < first_edit_step and not any(
                path.endswith(ef) or ef.endswith(path) or path == ef
                for ef in edited_files
            )
        ]
        m.m02_pre_edit_exploration_steps = float(len(pre_edit_reads))
        m.m02_available = True

    # ----- M18: exploration waste rate -----
    if m.m13_available and not math.isnan(m.m13_steps_to_gold_edit):
        first_edit_step = m.m13_steps_to_gold_edit + 1
        all_pre_edit = [p for step, p in file_reads if step < first_edit_step]
        useful_pre_edit = [
            p for p in all_pre_edit
            if any(p.endswith(ef) or ef.endswith(p) or p == ef for ef in edited_files)
        ]
        m.m18_pre_edit_total_reads = float(len(all_pre_edit))
        m.m18_pre_edit_useful_reads = float(len(useful_pre_edit))
        m.m18_exploration_waste_rate = _safe_div(
            float(len(all_pre_edit) - len(useful_pre_edit)),
            float(len(all_pre_edit)),
            0.0,
        )
        m.m18_available = True

    # ----- M16: scaffold waste rate -----
    if file_writes:
        scaffold_writes = [
            path for step, path in file_writes
            if SCAFFOLD_PATH_RE.match(path)
            or not any(
                path.endswith(ef) or ef.endswith(path) or path == ef
                for ef in edited_files
            )
        ]
        m.m16_scaffold_writes = float(len(scaffold_writes))
        m.m16_total_writes = float(len(file_writes))
        m.m16_scaffold_waste_rate = _safe_div(
            float(len(scaffold_writes)), float(len(file_writes)), 0.0
        )
        m.m16_available = True

    # ----- M04: brief top-3 precision / recall / hit@1 -----
    if brief_text and edited_files:
        confidence, top_files = _parse_brief_localization(brief_text)
        m.m04_brief_confidence = confidence
        m.m04_brief_top3 = top_files[:3]
        # Fuzzy match: the brief file path (relative to repo root) vs edited set
        def _match(brief_path: str, edited: Set[str]) -> bool:
            bp = _normalise_path(brief_path)
            for ef in edited:
                if bp == ef or bp.endswith("/" + ef) or ef.endswith("/" + bp) or bp in ef or ef in bp:
                    return True
            return False

        top3 = top_files[:3]
        hits = sum(1 for f in top3 if _match(f, edited_files))
        n_top3 = max(len(top3), 1)
        n_edited = max(len(edited_files), 1)
        m.m04_brief_precision_at_3 = float(hits) / float(n_top3)
        m.m04_brief_recall_at_3 = min(1.0, float(hits) / float(n_edited))
        m.m04_brief_hit_at_1 = 1.0 if top3 and _match(top3[0], edited_files) else 0.0
        m.m04_available = True

    # ----- M11: inverted confidence rate -----
    if brief_text and edited_files:
        loc_m = GT_LOCALIZATION_RE.search(brief_text)
        if loc_m:
            overall_conf = loc_m.group(1).strip().lower()
            body = loc_m.group(2)
            # Check if HIGH confidence
            if overall_conf == "high":
                # Single-target brief: "Edit target: <file> :: <symbol>" OR the ledger
                # HIGH head "<file> :: <func> — N/3 signals agree" (GT_CONSENSUS_LEDGER)
                et_match = re.search(r'Edit target:\s+([^\s:]+)', body) or \
                    re.search(r'(?m)^\s*([\w.\-/]+\.\w+)\s*::.*signals agree', body)
                if et_match:
                    pin_file = _normalise_path(et_match.group(1))
                    m.m11_high_confidence_pins = 1.0
                    def _match_pin(p: str, edited: Set[str]) -> bool:
                        p = _normalise_path(p)
                        for ef in edited:
                            if p == ef or p.endswith("/" + ef) or ef.endswith("/" + p) or p in ef or ef in p:
                                return True
                        return False
                    wrong = 0 if _match_pin(pin_file, edited_files) else 1
                    m.m11_wrong_high_confidence_pins = float(wrong)
                    m.m11_inverted_confidence_rate = float(wrong)
                    m.m11_available = True
            else:
                # Medium/low: no HIGH pins, inverted rate = 0
                m.m11_high_confidence_pins = 0.0
                m.m11_wrong_high_confidence_pins = 0.0
                m.m11_inverted_confidence_rate = 0.0
                m.m11_available = True

    # ----- M15: confidence calibration -----
    if brief_text and edited_files:
        # Map confidence → precision (are top files correct?)
        def _match(p: str, edited: Set[str]) -> bool:
            p = _normalise_path(p)
            for ef in edited:
                if p == ef or p.endswith("/" + ef) or ef.endswith("/" + p) or p in ef or ef in p:
                    return True
            return False

        conf_str, top_files = _parse_brief_localization(brief_text)
        conf_lower = conf_str.lower()
        # Build per-tier precision
        # For the brief we have one confidence level for all candidates
        # Compute fraction of top candidates that are correct
        if top_files:
            correct = sum(1 for f in top_files if _match(f, edited_files))
            prec = float(correct) / float(len(top_files))
            if conf_lower == "high":
                m.m15_precision_high = prec
                m.m15_precision_medium = math.nan
                m.m15_precision_low = math.nan
                m.m15_confidence_calibration_score = prec  # P(correct|HIGH) - P(correct|LOW) ~ prec - 0
            elif conf_lower == "medium":
                m.m15_precision_high = math.nan
                m.m15_precision_medium = prec
                m.m15_precision_low = math.nan
                m.m15_confidence_calibration_score = math.nan  # need both tiers
            else:
                m.m15_precision_high = math.nan
                m.m15_precision_medium = math.nan
                m.m15_precision_low = prec
                m.m15_confidence_calibration_score = math.nan
            m.m15_available = True

    # ----- GT deliveries (M05, M14, M17, M19) -----
    gt_deliveries = _extract_gt_deliveries(messages)
    total_deliveries = len(gt_deliveries)
    m.m05_deliveries_total = float(total_deliveries)
    m.m05_consumption_grade_source = "trajectory_automated"

    if total_deliveries > 0:
        # Automated consumption grading (heuristic):
        # EXPLICIT: GT block cites a file, next 3 agent steps quote/use that exact file+symbol
        # BEHAVIORAL: next 1 agent step reads/writes a GT-cited file
        # INERT: no match in next 3 steps
        # HARMFUL: agent reads a GT-cited file AND that file is NOT in edited_files
        explicit = 0
        behavioral = 0
        inert = 0
        harmful = 0
        for msg_idx, block in gt_deliveries:
            cited_files = _files_cited_in_gt_block(block)
            next_cmds = _get_next_3_assistant_steps(msg_idx, messages)
            next_text = " ".join(next_cmds).lower()

            # EXPLICIT: GT-cited file or symbol appears literally in next assistant text
            explicit_hit = any(
                cf.lower() in next_text for cf in cited_files if len(cf) > 4
            )
            # BEHAVIORAL: next step visits a GT-cited file
            behavioral_hit = any(
                any(cf.lower() in cmd.lower() for cf in cited_files)
                for cmd in next_cmds[:1]
            )
            # HARMFUL: behavioral hit on a file NOT in edited set
            if behavioral_hit:
                behavioral_cited = [
                    cf for cf in cited_files
                    if any(cmd.lower().find(cf.lower()) >= 0 for cmd in next_cmds[:1])
                ]
                harmful_hit = bool(behavioral_cited) and not any(
                    any(
                        bc.endswith("/" + ef) or ef.endswith("/" + bc) or bc == ef or bc in ef or ef in bc
                        for ef in edited_files
                    )
                    for bc in behavioral_cited
                )
            else:
                harmful_hit = False

            if explicit_hit:
                explicit += 1
            elif harmful_hit:
                harmful += 1
            elif behavioral_hit:
                behavioral += 1
            else:
                inert += 1

        m.m05_consumption_rate_explicit = _safe_div(float(explicit), float(total_deliveries), 0.0)
        m.m05_consumption_rate_behavioral = _safe_div(float(behavioral), float(total_deliveries), 0.0)
        m.m05_consumption_rate_inert = _safe_div(float(inert), float(total_deliveries), 0.0)
        m.m05_harm_rate = _safe_div(float(harmful), float(total_deliveries), 0.0)
        m.m05_available = True

        # M17: GT-caused wasted steps (steps on GT-cited wrong files)
        wasted = 0
        for msg_idx, block in gt_deliveries:
            cited = _files_cited_in_gt_block(block)
            next_cmds = _get_next_3_assistant_steps(msg_idx, messages)
            for cmd in next_cmds[:1]:
                cmd_lower = cmd.lower()
                for cf in cited:
                    if cf.lower() in cmd_lower and len(cf) > 4:
                        # Check if that file is in edited set
                        if not any(
                            cf.endswith("/" + ef) or ef.endswith("/" + cf) or cf == ef or cf in ef or ef in cf
                            for ef in edited_files
                        ):
                            wasted += 1
        m.m17_wasted_steps_gt_caused = float(wasted)
        m.m17_wasted_step_rate = _safe_div(float(wasted), float(total_steps), 0.0)
        m.m17_confidence = "AUTOMATED_HEURISTIC"
        m.m17_available = True

        # M14: token injection overhead
        # Use chars/4 proxy since gt_injected_tokens_total is broken in baseline
        gt_injected_tokens_raw = float(eff.get("gt_injected_tokens_total", 0.0))
        if gt_injected_tokens_raw > 0:
            m.m14_gt_injected_tokens_approx = gt_injected_tokens_raw
            m.m14_gt_token_count_source = "deep_metrics"
        else:
            # chars proxy: sum of all GT block lengths / 4
            chars = sum(len(block) for _, block in gt_deliveries)
            # Also add gt_observation_chars_total from deep metrics
            obs_chars = m.gt_observation_chars_total
            m.m14_gt_injected_tokens_approx = max(float(chars), obs_chars) / 4.0
            m.m14_gt_token_count_source = "chars_proxy"
        m.m14_gt_injection_overhead_pct = _safe_div(
            m.m14_gt_injected_tokens_approx * 100.0, m.llm_tokens_in, 0.0
        )
        m.m14_available = True

        # M19: test evidence delivery and consumption
        test_keywords = re.compile(
            r'#\[test\]|def\s+test_|fn\s+test_|it\(|describe\(|'
            r'assert|expect\(|assertEqual|assertEquals|should\.equal|'
            r'FAIL_TO_PASS|failing test',
            re.IGNORECASE
        )
        gt_test_deliveries = [
            (idx, block) for idx, block in gt_deliveries
            if test_keywords.search(block)
        ]
        m.m19_test_evidence_delivered = 1.0 if gt_test_deliveries else 0.0
        # Consumed = test evidence delivered AND agent ran tests after
        if gt_test_deliveries:
            first_test_delivery_idx = gt_test_deliveries[0][0]
            for i in range(first_test_delivery_idx + 1, len(messages)):
                msg = messages[i]
                if msg.get("role") != "assistant":
                    continue
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    if fn.get("name") == "bash":
                        args = fn.get("arguments", "")
                        if isinstance(args, str):
                            try:
                                args_d = json.loads(args)
                                cmd = args_d.get("command", "")
                            except Exception:
                                cmd = args
                        else:
                            cmd = args.get("command", "") if isinstance(args, dict) else ""
                        if TEST_RUNNER_RE.search(cmd):
                            m.m19_test_evidence_consumed = 1.0
                            break
                if m.m19_test_evidence_consumed:
                    break
        m.m19_available = True
    else:
        # No GT deliveries at all
        m.m05_consumption_rate_explicit = 0.0
        m.m05_consumption_rate_behavioral = 0.0
        m.m05_consumption_rate_inert = 0.0
        m.m05_harm_rate = 0.0
        m.m05_available = True

    # M12: emission precision (heuristic — file coordinates check against graph.db is out-of-scope
    # for the automated script; use brief availability as a proxy)
    # Precision proxy: fraction of GT-cited files (from brief top3) that ended in edited set
    if brief_text and edited_files and m.m04_available:
        m.m12_emission_precision = m.m04_brief_precision_at_3
        m.m12_emission_precision_confidence = "PARTIAL (brief_precision_proxy)"
        m.m12_available = True

    # M07: oracle suppression precision
    # For baseline (pre-oracle), proxy = fraction of deliveries that SHOULD have been suppressed
    # (inert + harmful).  For oracle arm, read from telemetry if present.
    if m.m05_available and total_deliveries > 0:
        inert_count = m.m05_consumption_rate_inert * total_deliveries
        harmful_count = m.m05_harm_rate * total_deliveries
        m.m07_suppression_precision = _safe_div(inert_count + harmful_count, total_deliveries, math.nan)
        m.m07_available = True

    # M08: resolved given consumption
    # Degenerate at 0/N resolved; compute anyway
    if m.m05_available:
        consumed = (
            m.m05_consumption_rate_explicit + m.m05_consumption_rate_behavioral
        ) * total_deliveries
        m.m08_resolved_given_consumed = _f8(
            1.0 if (consumed > 0 and m.resolved) else 0.0
        )
        m.m08_available = True

    # M10: regression rate — requires comparing verifier outputs across arms; set to 0.0 per task
    # (filled in at the paired level if verifier outputs are available)
    m.m10_regression_rate = 0.0
    m.m10_available = True

    # =========================================================================
    # M20–M26: Code-Quality / Correctness metrics
    # Added 2026-06-11: motivated by oracle run proving code correctness
    # improvement (aiomonitor 4/5 vs 1/5 hidden tests) while M13 showed no
    # signal. Source: verifier/test-stdout.txt, gt_issue_anchors.json, patch.
    # =========================================================================

    # ----- M20: Hidden Test Pass Rate -----
    verifier_path = _find_verifier_stdout(task_dir)
    if verifier_path:
        try:
            with open(verifier_path, encoding="utf-8", errors="replace") as f:
                verifier_text = ANSI_ESCAPE_RE.sub("", f.read())  # strip ANSI color codes
            pass_count, total_count, compile_fail, _ = _parse_verifier_hidden_tests(verifier_text)
            m.m20_hidden_tests_pass = float(pass_count)
            m.m20_hidden_tests_total = float(total_count)
            m.m20_hidden_test_pass_rate = _safe_div(float(pass_count), float(total_count), 0.0)
            m.m20_compile_fail = compile_fail
            m.m20_available = True
        except Exception as exc:
            print(f"  [WARN] {task_id}: M20 verifier parse error: {exc}", file=sys.stderr)

    # ----- M21: Patch Completeness -----
    # ----- M24: Requirement Fidelity -----
    anchors_path = _find_anchors_path(task_dir)
    obligations: List[Dict] = []
    if anchors_path:
        try:
            with open(anchors_path, encoding="utf-8", errors="replace") as f:
                anchors_data = json.load(f)
            obligations = anchors_data.get("obligations", [])
        except Exception as exc:
            print(f"  [WARN] {task_id}: M21 anchors parse error: {exc}", file=sys.stderr)

    if submission and obligations:
        completeness, ob_total, ob_addressed = _compute_patch_completeness(obligations, submission)
        m.m21_patch_completeness = completeness
        m.m21_obligations_total = float(ob_total)
        m.m21_obligations_addressed = float(ob_addressed)
        m.m21_available = True

        fidelity, n_checkable = _compute_requirement_fidelity(obligations, submission)
        m.m24_patch_fidelity = fidelity
        m.m24_n_checkable = float(n_checkable)
        m.m24_available = n_checkable > 0
    elif submission:
        # No obligations available — M21 not computable but note it
        m.m21_available = False
        m.m24_available = False

    # ----- M22: Patch Correctness (compile clean) -----
    if verifier_path and m.m20_available:
        # compile_clean = 1 if not compile_fail AND (tests ran OR exit_code suggests test failure only)
        # Use m20_compile_fail as the primary signal
        m.m22_compile_clean = 0.0 if m.m20_compile_fail else 1.0
        if not m.m20_compile_fail:
            # Also check for Python import errors in verifier output (compile_error_count > 0
            # even though tests may have run)
            try:
                with open(verifier_path, encoding="utf-8", errors="replace") as f:
                    vtext = ANSI_ESCAPE_RE.sub("", f.read())  # strip ANSI color codes
                _, _, _, err_count = _parse_verifier_hidden_tests(vtext)
                # High error count with 0 tests = compile fail even if not flagged
                if err_count > 0 and m.m20_hidden_tests_total == 0:
                    m.m22_compile_clean = 0.0
                    m.m22_compile_error_count = float(err_count)
            except Exception:
                pass
        m.m22_available = True

    # ----- M23: Implementation Depth -----
    if submission:
        lines_added, branch_density, new_funcs = _parse_patch_for_code_quality(submission)
        m.m23_patch_lines_added = float(lines_added)
        m.m23_branch_density = _f8(branch_density)
        m.m23_new_functions = float(new_funcs)
        # Composite depth score: 0.5*size + 0.3*complexity + 0.2*abstraction
        size_component = min(lines_added / 100.0, 1.0)
        complexity_component = min(branch_density / 0.3, 1.0) if not math.isnan(branch_density) else 0.0
        abstraction_component = min(new_funcs / 5.0, 1.0)
        m.m23_implementation_depth_score = _f8(
            0.5 * size_component + 0.3 * complexity_component + 0.2 * abstraction_component
        )
        m.m23_available = lines_added > 0

    # ----- M25: Test Evidence Before Submit -----
    # Count test runs that occur after the first edit step
    if m.m13_available and not math.isnan(m.m13_steps_to_gold_edit):
        first_edit_s = m.m13_steps_to_gold_edit + 1  # back to 1-based
        test_runs_post_edit = 0
        step_iter = 0
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            step_iter += 1
            if step_iter <= first_edit_s:
                continue
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                if fn.get("name") != "bash":
                    continue
                args = fn.get("arguments", "")
                if isinstance(args, str):
                    try:
                        args_d = json.loads(args)
                        cmd = args_d.get("command", "")
                    except Exception:
                        cmd = args
                else:
                    cmd = args.get("command", "") if isinstance(args, dict) else ""
                if TEST_RUNNER_RE.search(cmd):
                    test_runs_post_edit += 1
        m.m25_test_runs_post_edit = float(test_runs_post_edit)
        m.m25_test_coverage_before_submit = 1.0 if test_runs_post_edit > 0 else 0.0
        m.m25_available = True

    # ----- M26: Agent Self-Correction Events -----
    if file_writes:
        events = _count_self_correction_events(messages, file_writes)
        m.m26_self_correction_events = float(events)
        m.m26_available = True

    return m


# ---------------------------------------------------------------------------
# Run-level computation
# ---------------------------------------------------------------------------

def load_run(run_dir: Path) -> Dict[str, TaskMetrics]:
    """Load all tasks in a run directory. Returns {task_id: TaskMetrics}."""
    results: Dict[str, TaskMetrics] = {}
    for task_dir in sorted(run_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        task_id = task_dir.name
        print(f"  Loading {task_id}...", file=sys.stderr)
        tm = compute_task_metrics(task_id, task_dir)
        if tm is not None:
            results[task_id] = tm
    return results


def _safe_mean(values: List[float]) -> float:
    valid = [v for v in values if not math.isnan(v)]
    return float(np.mean(valid)) if valid else math.nan


def _safe_median(values: List[float]) -> float:
    valid = [v for v in values if not math.isnan(v)]
    return float(np.median(valid)) if valid else math.nan


def _safe_sum(values: List[float]) -> float:
    valid = [v for v in values if not math.isnan(v)]
    return float(sum(valid)) if valid else math.nan


# ---------------------------------------------------------------------------
# JSON output helpers
# ---------------------------------------------------------------------------

def _fmt(v: Any) -> Any:
    """Format a value for JSON output — floats to 8dp, nan → null."""
    if isinstance(v, float):
        if math.isnan(v):
            return None
        return round(v, 8)
    if isinstance(v, bool):
        return v
    if isinstance(v, list):
        return [_fmt(x) for x in v]
    if isinstance(v, dict):
        return {k: _fmt(vv) for k, vv in v.items()}
    return v


def _task_to_dict(tm: TaskMetrics) -> Dict:
    d = asdict(tm)
    out = {k: _fmt(v) for k, v in d.items()}
    # P1-26 — stable public alias (gold proxy metric, not oracle gold file hits).
    out["steps_to_first_gold_edit"] = out.get("m13_steps_to_gold_edit")
    out["steps_to_first_gold_edit_confidence"] = out.get("m13_steps_to_gold_edit_confidence")
    return out


# ---------------------------------------------------------------------------
# Main reporting
# ---------------------------------------------------------------------------

def compute_trajectory_scorecard(
    baseline_run: Dict[str, TaskMetrics],
    oracle_run: Dict[str, TaskMetrics],
    shared_ids: List[str],
) -> Dict[str, Any]:
    """P7 — Stage-2 trajectory scorecard (post-hoc, paired runs only).

    Surfaces gt_caused_flip (heuristic: flip + behavioral consumption or test
    evidence consumed), obligation_coverage_at_submit (M21 proxy), and mean
    consumption rates. Does not rerun GT-OFF; uses paired TaskMetrics only.
    """
    def _baseline_resolved(tid: str) -> bool:
        bm = baseline_run.get(tid)
        return bool(bm and bm.resolved)

    def _oracle_resolved(tid: str) -> bool:
        om = oracle_run.get(tid)
        return bool(om and om.resolved)

    flips = [
        tid for tid in shared_ids
        if _oracle_resolved(tid) and not _baseline_resolved(tid)
    ]
    regressions = [
        tid for tid in shared_ids
        if _baseline_resolved(tid) and not _oracle_resolved(tid)
    ]
    flip_details: List[Dict[str, Any]] = []
    gt_caused_heuristic_flips = 0
    for tid in flips:
        o = oracle_run[tid]
        behavioral = (
            not math.isnan(o.m05_consumption_rate_behavioral)
            and o.m05_consumption_rate_behavioral > 0
        )
        test_consumed = o.m19_test_evidence_consumed > 0
        obligation_ok = (
            not math.isnan(o.m21_patch_completeness)
            and o.m21_patch_completeness >= 0.5
        )
        gt_caused = (behavioral or test_consumed) and (
            obligation_ok or math.isnan(o.m21_patch_completeness)
        )
        if gt_caused:
            gt_caused_heuristic_flips += 1
        flip_details.append({
            "task_id": tid,
            "gt_caused_heuristic": gt_caused,
            "obligation_coverage_at_submit": _fmt(o.m21_patch_completeness),
            "consumption_behavioral": _fmt(o.m05_consumption_rate_behavioral),
            "test_evidence_consumed": _fmt(o.m19_test_evidence_consumed),
        })

    obligation_cov = [
        oracle_run[tid].m21_patch_completeness
        for tid in shared_ids
        if not math.isnan(oracle_run[tid].m21_patch_completeness)
    ]
    consumption = [
        oracle_run[tid].m05_consumption_rate_behavioral
        for tid in shared_ids
        if not math.isnan(oracle_run[tid].m05_consumption_rate_behavioral)
    ]
    explicit = [
        oracle_run[tid].m05_consumption_rate_explicit
        for tid in shared_ids
        if not math.isnan(oracle_run[tid].m05_consumption_rate_explicit)
    ]
    return {
        "gt_caused_heuristic_flips": gt_caused_heuristic_flips,
        "flip_count": len(flips),
        "regression_count": len(regressions),
        "flip_tasks": flip_details,
        "obligation_coverage_at_submit_mean": _fmt(_safe_mean(obligation_cov)),
        "consumption_rate_behavioral_mean": _fmt(_safe_mean(consumption)),
        "consumption_rate_explicit_mean": _fmt(_safe_mean(explicit)),
    }


def compute_paired_report(
    baseline_run: Dict[str, TaskMetrics],
    oracle_run: Dict[str, TaskMetrics],
    baseline_run_id: str,
    oracle_run_id: str,
) -> Dict:
    """Compute the full paired report dict."""
    import datetime
    import subprocess

    # Shared tasks
    shared_ids = sorted(set(baseline_run.keys()) & set(oracle_run.keys()))
    n_tasks = len(shared_ids)

    # Try to get git info
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:
        branch = "unknown"
        git_commit = "unknown"

    per_task = {}
    delta_records = {}

    # Per-metric delta lists for statistical tests
    m01_deltas: List[float] = []
    m03_deltas: List[float] = []
    m09_deltas: List[float] = []
    m13_deltas: List[float] = []
    m04_p3_deltas: List[float] = []
    m05_explicit_deltas: List[float] = []
    m11_ic_deltas: List[float] = []
    m17_deltas: List[float] = []
    m16_deltas: List[float] = []
    # M20–M26 delta lists
    m20_deltas: List[float] = []
    m21_deltas: List[float] = []
    m23_deltas: List[float] = []
    m25_deltas: List[float] = []
    m26_deltas: List[float] = []

    # McNemar tables
    m11_mcnemar_b = 0   # baseline high-wrong, oracle not
    m11_mcnemar_c = 0   # oracle high-wrong, baseline not
    # M22 McNemar (compile clean regression)
    m22_mcnemar_b = 0   # baseline compile_clean, oracle not
    m22_mcnemar_c = 0   # oracle compile_clean, baseline not
    # M25 McNemar
    m25_mcnemar_b = 0
    m25_mcnemar_c = 0

    for tid in shared_ids:
        base = baseline_run[tid]
        oracle = oracle_run[tid]

        def delta(a: float, b: float) -> float:
            if math.isnan(a) or math.isnan(b):
                return math.nan
            return b - a

        d: Dict[str, Any] = {
            "task_id": tid,
            "baseline_present": True,
            "m01_delta": _fmt(delta(base.m01_steps_to_first_gold_read,
                                    oracle.m01_steps_to_first_gold_read)),
            "m02_delta": _fmt(delta(base.m02_pre_edit_exploration_steps,
                                    oracle.m02_pre_edit_exploration_steps)),
            "m03_delta": _fmt(delta(base.m03_total_steps, oracle.m03_total_steps)),
            "m04_precision_delta": _fmt(delta(base.m04_brief_precision_at_3,
                                              oracle.m04_brief_precision_at_3)),
            "m04_hit1_delta": _fmt(delta(base.m04_brief_hit_at_1,
                                         oracle.m04_brief_hit_at_1)),
            "m05_explicit_consumption_delta": _fmt(
                delta(base.m05_consumption_rate_explicit,
                      oracle.m05_consumption_rate_explicit)),
            "m05_harm_rate_delta": _fmt(delta(base.m05_harm_rate, oracle.m05_harm_rate)),
            "m09_delta": _fmt(delta(base.m09_steps_to_resolution,
                                    oracle.m09_steps_to_resolution)),
            "m10_delta": _fmt(delta(base.m10_regression_rate, oracle.m10_regression_rate)),
            "m11_inverted_confidence_delta": _fmt(
                delta(base.m11_inverted_confidence_rate,
                      oracle.m11_inverted_confidence_rate)),
            "m12_emission_precision_delta": _fmt(
                delta(base.m12_emission_precision, oracle.m12_emission_precision)),
            "m13_delta": _fmt(delta(base.m13_steps_to_gold_edit,
                                    oracle.m13_steps_to_gold_edit)),
            "m13_delta_pct": _fmt(
                _safe_div(
                    delta(base.m13_steps_to_gold_edit, oracle.m13_steps_to_gold_edit),
                    base.m13_steps_to_gold_edit,
                    math.nan,
                ) * 100.0
            ),
            "m14_overhead_delta": _fmt(
                delta(base.m14_gt_injection_overhead_pct,
                      oracle.m14_gt_injection_overhead_pct)),
            "m15_calibration_delta": _fmt(
                delta(base.m15_confidence_calibration_score,
                      oracle.m15_confidence_calibration_score)),
            "m16_scaffold_waste_delta": _fmt(
                delta(base.m16_scaffold_waste_rate, oracle.m16_scaffold_waste_rate)),
            "m17_wasted_steps_delta": _fmt(
                delta(base.m17_wasted_steps_gt_caused, oracle.m17_wasted_steps_gt_caused)),
            "m18_exploration_waste_delta": _fmt(
                delta(base.m18_exploration_waste_rate, oracle.m18_exploration_waste_rate)),
            "m19_test_evidence_delta": _fmt(
                delta(base.m19_test_evidence_delivered, oracle.m19_test_evidence_delivered)),
            # M20–M26 deltas
            "m20_hidden_tests_pass_delta": _fmt(
                delta(base.m20_hidden_tests_pass, oracle.m20_hidden_tests_pass)),
            "m20_hidden_test_pass_rate_delta": _fmt(
                delta(base.m20_hidden_test_pass_rate, oracle.m20_hidden_test_pass_rate)),
            "m21_patch_completeness_delta": _fmt(
                delta(base.m21_patch_completeness, oracle.m21_patch_completeness)),
            "m22_compile_clean_delta": _fmt(
                delta(base.m22_compile_clean, oracle.m22_compile_clean)),
            "m23_depth_score_delta": _fmt(
                delta(base.m23_implementation_depth_score, oracle.m23_implementation_depth_score)),
            "m24_patch_fidelity_delta": _fmt(
                delta(base.m24_patch_fidelity, oracle.m24_patch_fidelity)),
            "m25_test_coverage_delta": _fmt(
                delta(base.m25_test_coverage_before_submit, oracle.m25_test_coverage_before_submit)),
            "m26_self_correction_delta": _fmt(
                delta(base.m26_self_correction_events, oracle.m26_self_correction_events)),
            "resolved_delta": _fmt(
                float(oracle.resolved) - float(base.resolved)),
        }
        delta_records[tid] = d

        # Accumulate for statistical tests
        def _acc(lst: List[float], a: float, b: float) -> None:
            if not math.isnan(a) and not math.isnan(b):
                lst.append(b - a)

        _acc(m01_deltas, base.m01_steps_to_first_gold_read, oracle.m01_steps_to_first_gold_read)
        _acc(m03_deltas, base.m03_total_steps, oracle.m03_total_steps)
        _acc(m09_deltas, base.m09_steps_to_resolution, oracle.m09_steps_to_resolution)
        _acc(m13_deltas, base.m13_steps_to_gold_edit, oracle.m13_steps_to_gold_edit)
        _acc(m04_p3_deltas, base.m04_brief_precision_at_3, oracle.m04_brief_precision_at_3)
        _acc(m05_explicit_deltas, base.m05_consumption_rate_explicit,
             oracle.m05_consumption_rate_explicit)
        _acc(m11_ic_deltas, base.m11_inverted_confidence_rate,
             oracle.m11_inverted_confidence_rate)
        _acc(m17_deltas, base.m17_wasted_steps_gt_caused, oracle.m17_wasted_steps_gt_caused)
        _acc(m16_deltas, base.m16_scaffold_waste_rate, oracle.m16_scaffold_waste_rate)
        # M20–M26
        _acc(m20_deltas, base.m20_hidden_tests_pass, oracle.m20_hidden_tests_pass)
        _acc(m21_deltas, base.m21_patch_completeness, oracle.m21_patch_completeness)
        _acc(m23_deltas, base.m23_implementation_depth_score, oracle.m23_implementation_depth_score)
        _acc(m25_deltas, base.m25_test_coverage_before_submit, oracle.m25_test_coverage_before_submit)
        _acc(m26_deltas, base.m26_self_correction_events, oracle.m26_self_correction_events)

        # McNemar for M11
        base_hi_wrong = (
            not math.isnan(base.m11_inverted_confidence_rate)
            and base.m11_inverted_confidence_rate > 0
        )
        ora_hi_wrong = (
            not math.isnan(oracle.m11_inverted_confidence_rate)
            and oracle.m11_inverted_confidence_rate > 0
        )
        if base_hi_wrong and not ora_hi_wrong:
            m11_mcnemar_b += 1
        elif ora_hi_wrong and not base_hi_wrong:
            m11_mcnemar_c += 1

        # McNemar for M22
        base_compile_clean = (
            not math.isnan(base.m22_compile_clean) and base.m22_compile_clean > 0.5
        )
        ora_compile_clean = (
            not math.isnan(oracle.m22_compile_clean) and oracle.m22_compile_clean > 0.5
        )
        if base_compile_clean and not ora_compile_clean:
            m22_mcnemar_b += 1  # regression: oracle broke compile
        elif ora_compile_clean and not base_compile_clean:
            m22_mcnemar_c += 1

        # McNemar for M25
        base_test_cov = (
            not math.isnan(base.m25_test_coverage_before_submit)
            and base.m25_test_coverage_before_submit > 0.5
        )
        ora_test_cov = (
            not math.isnan(oracle.m25_test_coverage_before_submit)
            and oracle.m25_test_coverage_before_submit > 0.5
        )
        if base_test_cov and not ora_test_cov:
            m25_mcnemar_b += 1
        elif ora_test_cov and not base_test_cov:
            m25_mcnemar_c += 1

        per_task[tid] = {
            "baseline": _task_to_dict(base),
            "oracle": _task_to_dict(oracle),
            "delta": d,
        }

    # ----- Statistical tests -----
    def _full_wilcoxon(deltas: List[float], name: str, direction: str = "oracle_lower") -> Dict:
        wres = _wilcoxon_test(deltas, alternative="less" if direction == "oracle_lower" else "greater")
        m, lo, hi = _bootstrap_ci(deltas)
        med = _safe_median(deltas)
        return {
            "W": _fmt(wres.get("W")),
            "p_value": _fmt(wres.get("p_value")),
            "n": wres.get("n", 0),
            "direction": direction,
            "mean_delta": _fmt(m),
            "median_delta": _fmt(med),
            "bootstrap_ci_lo": _fmt(lo),
            "bootstrap_ci_hi": _fmt(hi),
            "n_bootstrap_resamples": N_BOOTSTRAP,
            "significant_p05": bool(
                not math.isnan(wres.get("p_value", math.nan))
                and wres.get("p_value", 1.0) < 0.05
            ),
            **({"note": wres["note"]} if "note" in wres else {}),
            **({"error": wres["error"]} if "error" in wres else {}),
        }

    statistical_tests = {
        "m01_wilcoxon": _full_wilcoxon(m01_deltas, "m01", "oracle_lower"),
        "m03_wilcoxon": _full_wilcoxon(m03_deltas, "m03", "oracle_lower"),
        "m09_wilcoxon": _full_wilcoxon(m09_deltas, "m09", "oracle_lower"),
        "m13_wilcoxon": _full_wilcoxon(m13_deltas, "m13", "oracle_lower"),
        "m04_precision_wilcoxon": _full_wilcoxon(m04_p3_deltas, "m04", "oracle_higher"),
        "m05_explicit_wilcoxon": _full_wilcoxon(
            m05_explicit_deltas, "m05", "oracle_higher"
        ),
        "m11_mcnemar": _fmt(
            {**_mcnemar_test(m11_mcnemar_b, m11_mcnemar_c), "direction": "oracle_lower"}
        ),
        "m17_wilcoxon": _full_wilcoxon(m17_deltas, "m17", "oracle_lower"),
        "m16_wilcoxon": _full_wilcoxon(m16_deltas, "m16", "oracle_lower"),
        # M20–M26 statistical tests
        "m20_wilcoxon": _full_wilcoxon(m20_deltas, "m20", "oracle_higher"),
        "m21_wilcoxon": _full_wilcoxon(m21_deltas, "m21", "oracle_higher"),
        "m22_mcnemar": _fmt(
            {**_mcnemar_test(m22_mcnemar_b, m22_mcnemar_c), "direction": "oracle_not_worse",
             "note": "b=compile_regression(base_ok_oracle_fail), c=compile_improvement"}
        ),
        "m23_wilcoxon": _full_wilcoxon(m23_deltas, "m23", "oracle_higher"),
        "m25_mcnemar": _fmt(
            {**_mcnemar_test(m25_mcnemar_b, m25_mcnemar_c), "direction": "oracle_higher"}
        ),
        "m26_wilcoxon": _full_wilcoxon(m26_deltas, "m26", "oracle_higher"),
    }

    # Flip count
    flip_count = sum(
        1 for tid in shared_ids
        if oracle_run[tid].resolved and not baseline_run[tid].resolved
    )
    regression_count = sum(
        1 for tid in shared_ids
        if baseline_run[tid].resolved and not oracle_run[tid].resolved
    )

    # Flip significance (G9): McNemar on the discordant flip/regression pairs, NOT a
    # binomial vs a p=0.5 null over all eligible tasks (that null can never fire).
    n_flip_eligible = sum(1 for tid in shared_ids if not baseline_run[tid].resolved)
    statistical_tests["flips_mcnemar"] = _fmt(
        _flips_mcnemar(flip_count, regression_count, n_flip_eligible)
    )

    # G9: Holm–Bonferroni correction over the WHOLE per-metric battery (all wilcoxon +
    # mcnemar entries, incl. flips) BEFORE any significant_p05 is trusted true. Applied
    # here, after the battery is complete and before _metric_aggregate reads it.
    _apply_holm(statistical_tests)

    # ----- Regression guards -----
    # Compare oracle vs baseline means
    def _run_mean(run: Dict[str, TaskMetrics], attr: str) -> float:
        vals = [getattr(t, attr) for t in run.values() if not math.isnan(getattr(t, attr, math.nan))]
        return float(np.mean(vals)) if vals else math.nan

    base_harm = _run_mean(baseline_run, "m05_harm_rate")
    oracle_harm = _run_mean(oracle_run, "m05_harm_rate")
    base_m11 = _run_mean(
        {tid: baseline_run[tid] for tid in shared_ids},
        "m11_inverted_confidence_rate",
    )
    oracle_m11 = _run_mean(
        {tid: oracle_run[tid] for tid in shared_ids},
        "m11_inverted_confidence_rate",
    )
    base_scaffold = _run_mean(
        {tid: baseline_run[tid] for tid in shared_ids},
        "m16_scaffold_waste_rate",
    )
    oracle_scaffold = _run_mean(
        {tid: oracle_run[tid] for tid in shared_ids},
        "m16_scaffold_waste_rate",
    )

    harm_increased = (
        not math.isnan(base_harm) and not math.isnan(oracle_harm)
        and oracle_harm > base_harm
    )
    m11_increased = (
        not math.isnan(base_m11) and not math.isnan(oracle_m11)
        and oracle_m11 > base_m11
    )
    scaffold_increased = (
        not math.isnan(base_scaffold) and not math.isnan(oracle_scaffold)
        and oracle_scaffold > base_scaffold
    )

    # M22: compile regressions (oracle introduced compile error that baseline didn't have)
    compile_regression_triggered = m22_mcnemar_b > 0

    regression_guards = {
        "m10_regression_rate_increased": False,  # not computable without verifier diff
        "m11_inverted_confidence_increased": bool(m11_increased),
        "harm_rate_increased": bool(harm_increased),
        "m16_scaffold_waste_increased": bool(scaffold_increased),
        "m22_compile_regressions": int(m22_mcnemar_b),
        "m22_compile_regression_triggered": bool(compile_regression_triggered),
        "any_regression_triggered": bool(
            m11_increased or harm_increased or scaffold_increased or compile_regression_triggered
        ),
    }

    # ----- Aggregate -----
    def _metric_aggregate(
        base_vals: List[float],
        oracle_vals: List[float],
        deltas: List[float],
        wilcoxon_key: Optional[str] = None,
    ) -> Dict:
        w = statistical_tests.get(wilcoxon_key, {}) if wilcoxon_key else {}
        m, lo, hi = _bootstrap_ci(deltas)
        return {
            "baseline_mean": _fmt(_safe_mean(base_vals)),
            "baseline_median": _fmt(_safe_median(base_vals)),
            "oracle_mean": _fmt(_safe_mean(oracle_vals)),
            "oracle_median": _fmt(_safe_median(oracle_vals)),
            "delta_mean": _fmt(m),
            "delta_median": _fmt(_safe_median(deltas)),
            "ci_lo": _fmt(lo),
            "ci_hi": _fmt(hi),
            "wilcoxon_p": _fmt(w.get("p_value")),
            "significant_p05": bool(w.get("significant_p05", False)),
            "n": len(deltas),
        }

    def _vals(run: Dict[str, TaskMetrics], attr: str) -> List[float]:
        return [
            getattr(run[tid], attr)
            for tid in shared_ids
            if not math.isnan(getattr(run[tid], attr, math.nan))
        ]

    aggregate = {
        "M01": _metric_aggregate(
            _vals(baseline_run, "m01_steps_to_first_gold_read"),
            _vals(oracle_run, "m01_steps_to_first_gold_read"),
            m01_deltas,
            "m01_wilcoxon",
        ),
        "M02": _metric_aggregate(
            _vals(baseline_run, "m02_pre_edit_exploration_steps"),
            _vals(oracle_run, "m02_pre_edit_exploration_steps"),
            [],
        ),
        "M03": _metric_aggregate(
            _vals(baseline_run, "m03_total_steps"),
            _vals(oracle_run, "m03_total_steps"),
            m03_deltas,
            "m03_wilcoxon",
        ),
        "M04_P3": _metric_aggregate(
            _vals(baseline_run, "m04_brief_precision_at_3"),
            _vals(oracle_run, "m04_brief_precision_at_3"),
            m04_p3_deltas,
            "m04_precision_wilcoxon",
        ),
        "M05_explicit": _metric_aggregate(
            _vals(baseline_run, "m05_consumption_rate_explicit"),
            _vals(oracle_run, "m05_consumption_rate_explicit"),
            m05_explicit_deltas,
            "m05_explicit_wilcoxon",
        ),
        "M09": _metric_aggregate(
            _vals(baseline_run, "m09_steps_to_resolution"),
            _vals(oracle_run, "m09_steps_to_resolution"),
            m09_deltas,
            "m09_wilcoxon",
        ),
        "M11": _metric_aggregate(
            _vals(baseline_run, "m11_inverted_confidence_rate"),
            _vals(oracle_run, "m11_inverted_confidence_rate"),
            m11_ic_deltas,
        ),
        "M13": _metric_aggregate(
            _vals(baseline_run, "m13_steps_to_gold_edit"),
            _vals(oracle_run, "m13_steps_to_gold_edit"),
            m13_deltas,
            "m13_wilcoxon",
        ),
        "M14_overhead": {
            "baseline_mean": _fmt(_safe_mean(_vals(baseline_run, "m14_gt_injection_overhead_pct"))),
            "oracle_mean": _fmt(_safe_mean(_vals(oracle_run, "m14_gt_injection_overhead_pct"))),
            "note": "report_only_no_paired_test",
        },
        "M16": _metric_aggregate(
            _vals(baseline_run, "m16_scaffold_waste_rate"),
            _vals(oracle_run, "m16_scaffold_waste_rate"),
            m16_deltas,
            "m16_wilcoxon",
        ),
        "M17": _metric_aggregate(
            _vals(baseline_run, "m17_wasted_steps_gt_caused"),
            _vals(oracle_run, "m17_wasted_steps_gt_caused"),
            m17_deltas,
            "m17_wilcoxon",
        ),
        "M18": _metric_aggregate(
            _vals(baseline_run, "m18_exploration_waste_rate"),
            _vals(oracle_run, "m18_exploration_waste_rate"),
            [],
        ),
        # M20–M26 aggregates
        "M20_pass_count": _metric_aggregate(
            _vals(baseline_run, "m20_hidden_tests_pass"),
            _vals(oracle_run, "m20_hidden_tests_pass"),
            m20_deltas,
            "m20_wilcoxon",
        ),
        "M20_pass_rate": _metric_aggregate(
            _vals(baseline_run, "m20_hidden_test_pass_rate"),
            _vals(oracle_run, "m20_hidden_test_pass_rate"),
            [
                d.get("m20_hidden_test_pass_rate_delta", math.nan)
                for d in [per_task[tid]["delta"] for tid in shared_ids]
                if not math.isnan(d.get("m20_hidden_test_pass_rate_delta", math.nan))
            ],
        ),
        "M21": _metric_aggregate(
            _vals(baseline_run, "m21_patch_completeness"),
            _vals(oracle_run, "m21_patch_completeness"),
            m21_deltas,
            "m21_wilcoxon",
        ),
        "M22": {
            "baseline_compile_clean_rate": _fmt(
                sum(
                    1 for tid in shared_ids
                    if not math.isnan(baseline_run[tid].m22_compile_clean)
                    and baseline_run[tid].m22_compile_clean > 0.5
                ) / max(1, sum(1 for tid in shared_ids if baseline_run[tid].m22_available))
                if any(baseline_run[tid].m22_available for tid in shared_ids) else math.nan
            ),
            "oracle_compile_clean_rate": _fmt(
                sum(
                    1 for tid in shared_ids
                    if not math.isnan(oracle_run[tid].m22_compile_clean)
                    and oracle_run[tid].m22_compile_clean > 0.5
                ) / max(1, sum(1 for tid in shared_ids if oracle_run[tid].m22_available))
                if any(oracle_run[tid].m22_available for tid in shared_ids) else math.nan
            ),
            "mcnemar_b_regressions": m22_mcnemar_b,
            "mcnemar_c_improvements": m22_mcnemar_c,
        },
        "M23": _metric_aggregate(
            _vals(baseline_run, "m23_implementation_depth_score"),
            _vals(oracle_run, "m23_implementation_depth_score"),
            m23_deltas,
            "m23_wilcoxon",
        ),
        "M26": _metric_aggregate(
            _vals(baseline_run, "m26_self_correction_events"),
            _vals(oracle_run, "m26_self_correction_events"),
            m26_deltas,
            "m26_wilcoxon",
        ),
    }

    resolved_baseline = sum(1 for t in baseline_run.values() if t.resolved)
    resolved_oracle = sum(1 for t in oracle_run.values() if t.resolved)

    # M20 headline aggregates
    m20_pass_base = _safe_sum([baseline_run[tid].m20_hidden_tests_pass for tid in shared_ids])
    m20_total_base = _safe_sum([baseline_run[tid].m20_hidden_tests_total for tid in shared_ids])
    m20_pass_oracle = _safe_sum([oracle_run[tid].m20_hidden_tests_pass for tid in shared_ids])
    m20_total_oracle = _safe_sum([oracle_run[tid].m20_hidden_tests_total for tid in shared_ids])
    m20_delta_total = m20_pass_oracle - m20_pass_base
    m20_wilcoxon_p = statistical_tests["m20_wilcoxon"].get("p_value")
    m20_sig = statistical_tests["m20_wilcoxon"].get("significant_p05", False)

    trajectory_scorecard = compute_trajectory_scorecard(
        baseline_run, oracle_run, shared_ids)

    report = {
        "schema": "gt_metrics.v2",
        "baseline_run_id": baseline_run_id,
        "oracle_run_id": oracle_run_id,
        "branch": branch,
        "git_commit": git_commit,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "n_tasks": n_tasks,
        "n_shared_tasks": n_tasks,
        "baseline_arm": "frozen_gt_on_pre_oracle",
        "per_task": per_task,
        "aggregate": aggregate,
        "statistical_tests": statistical_tests,
        "regression_guards": regression_guards,
        "trajectory_scorecard": trajectory_scorecard,
        "headline": {
            "resolved_baseline": resolved_baseline,
            "resolved_oracle": resolved_oracle,
            "flip_count": flip_count,
            "regression_count": regression_count,
            "gt_caused_heuristic_flips": trajectory_scorecard["gt_caused_heuristic_flips"],
            "obligation_coverage_at_submit_mean": (
                trajectory_scorecard["obligation_coverage_at_submit_mean"]
            ),
            "consumption_rate_behavioral_mean": (
                trajectory_scorecard["consumption_rate_behavioral_mean"]
            ),
            # M20 — PRIMARY code correctness metric
            "m20_hidden_tests_pass_baseline": _fmt(m20_pass_base),
            "m20_hidden_tests_pass_oracle": _fmt(m20_pass_oracle),
            "m20_hidden_tests_total_baseline": _fmt(m20_total_base),
            "m20_hidden_tests_total_oracle": _fmt(m20_total_oracle),
            "m20_delta_total": _fmt(m20_delta_total),
            "m20_delta_mean": _fmt(_safe_mean(m20_deltas)),
            "m20_delta_median": _fmt(_safe_median(m20_deltas)),
            "m20_wilcoxon_p": _fmt(m20_wilcoxon_p),
            "m20_significant": bool(m20_sig),
            "m20_tasks_with_positive_delta": int(
                sum(1 for d in m20_deltas if d > 0)
            ),
            "m20_tasks_with_delta_gte3": int(
                sum(1 for tid in shared_ids
                    if not math.isnan(per_task[tid]["delta"].get("m20_hidden_tests_pass_delta", math.nan))
                    and per_task[tid]["delta"].get("m20_hidden_tests_pass_delta", 0) >= 3)
            ),
            # M13 — speed metric (kept for comparison)
            "m13_delta_mean": _fmt(_safe_mean(m13_deltas)),
            "m13_delta_median": _fmt(_safe_median(m13_deltas)),
            "m13_wilcoxon_p": _fmt(statistical_tests["m13_wilcoxon"].get("p_value")),
            "m04_precision_at_3_mean_baseline": _fmt(
                _safe_mean(_vals(baseline_run, "m04_brief_precision_at_3"))
            ),
            "m04_precision_at_3_mean_oracle": _fmt(
                _safe_mean(_vals(oracle_run, "m04_brief_precision_at_3"))
            ),
            "m05_harm_rate_baseline": _fmt(base_harm),
            "m05_harm_rate_oracle": _fmt(oracle_harm),
            "any_regression_guard_triggered": regression_guards["any_regression_triggered"],
        },
        "instrumentation_flags": {
            "first_edit_detection_confidence": (
                "PARTIAL — trajectory write-regex fallback used; "
                "heredoc writes may be missed if no matching pattern found"
            ),
            "gt_injected_tokens_source": "chars_proxy (gt_injected_tokens_total=0 in baseline)",
            "consumption_grades_source": "trajectory_automated_heuristic",
            "consumption_grades_note": (
                "Automated heuristic overestimates BEHAVIORAL; "
                "manual adversarial audit required for EXPLICIT/INERT/HARMFUL ground truth"
            ),
            "baseline_arm_available": True,
            "baseline_arm_run_id": baseline_run_id,
            "paired_comparison_valid": n_tasks > 0,
            "m08_degenerate": resolved_baseline == 0,
            "m10_note": "regression_rate=0 per task — verifier diff not automated; compare verifier/test-stdout.txt manually",
        },
    }
    return report


# ---------------------------------------------------------------------------
# Markdown report generator
# ---------------------------------------------------------------------------

def _md_val(v: Any, fmt: str = ".2f") -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return str(v)
    try:
        f = float(v)
        if math.isnan(f):
            return "—"
        return f"{f:{fmt}}"
    except Exception:
        return str(v)


def generate_markdown_report(report: Dict) -> str:
    lines: List[str] = []
    h = report.get("headline", {})
    ag = report.get("aggregate", {})
    st = report.get("statistical_tests", {})
    rg = report.get("regression_guards", {})
    flags = report.get("instrumentation_flags", {})
    per_task = report.get("per_task", {})

    lines.append(f"# GT Metrics Report")
    lines.append(f"")
    lines.append(f"## 0. Run Identity")
    lines.append(f"")
    lines.append(f"| Field | Value |")
    lines.append(f"|---|---|")
    lines.append(f"| Schema | {report.get('schema', '—')} |")
    lines.append(f"| Baseline Run ID | {report.get('baseline_run_id', '—')} |")
    lines.append(f"| Oracle Run ID | {report.get('oracle_run_id', '—')} |")
    lines.append(f"| Branch | {report.get('branch', '—')} |")
    lines.append(f"| Git Commit | {report.get('git_commit', '—')[:12]} |")
    lines.append(f"| Timestamp | {report.get('timestamp', '—')} |")
    lines.append(f"| N Tasks | {report.get('n_shared_tasks', 0)} |")
    lines.append(f"")

    lines.append(f"## 1. Headline")
    lines.append(f"")
    flip_ct = h.get('flip_count', 0)
    reg_ct = h.get('regression_count', 0)
    res_base = h.get('resolved_baseline', 0)
    res_ora = h.get('resolved_oracle', 0)
    n_tasks = report.get("n_shared_tasks", 0)
    lines.append(f"- **Resolved:** {res_ora}/{n_tasks} oracle vs {res_base}/{n_tasks} baseline — "
                 f"**{flip_ct} flip(s), {reg_ct} regression(s)**")

    # M20 PRIMARY correctness headline
    m20_pass_b = h.get("m20_hidden_tests_pass_baseline")
    m20_pass_o = h.get("m20_hidden_tests_pass_oracle")
    m20_tot_b = h.get("m20_hidden_tests_total_baseline")
    m20_tot_o = h.get("m20_hidden_tests_total_oracle")
    m20_delta_total = h.get("m20_delta_total")
    m20_p = h.get("m20_wilcoxon_p")
    m20_sig = h.get("m20_significant", False)
    m20_pos_tasks = h.get("m20_tasks_with_positive_delta", 0)
    m20_gte3_tasks = h.get("m20_tasks_with_delta_gte3", 0)
    m20_sig_str = " **[SIGNIFICANT p<0.05]**" if m20_sig else ""
    lines.append(
        f"- **PRIMARY CODE CORRECTNESS (M20 hidden test pass count):** "
        f"baseline={_md_val(m20_pass_b, '.0f')}/{_md_val(m20_tot_b, '.0f')}, "
        f"oracle={_md_val(m20_pass_o, '.0f')}/{_md_val(m20_tot_o, '.0f')}, "
        f"delta=+{_md_val(m20_delta_total, '.0f')}, "
        f"p={_md_val(m20_p, '.4f')} (Wilcoxon), "
        f"{m20_pos_tasks} tasks improved, {m20_gte3_tasks} with delta≥3"
        f"{m20_sig_str}"
    )

    m13_p = h.get("m13_wilcoxon_p")
    m13_med = h.get("m13_delta_median")
    m13_mean = h.get("m13_delta_mean")
    lines.append(f"- **Speed metric (M13 steps-to-gold-edit):** "
                 f"delta median={_md_val(m13_med)}, mean={_md_val(m13_mean)}, "
                 f"p={_md_val(m13_p, '.4f')} (Wilcoxon) — measures efficiency, not correctness")

    m04_base = h.get("m04_precision_at_3_mean_baseline")
    m04_ora = h.get("m04_precision_at_3_mean_oracle")
    lines.append(f"- **Brief quality (M04 P@3):** baseline={_md_val(m04_base)}, oracle={_md_val(m04_ora)}")

    harm_b = h.get("m05_harm_rate_baseline")
    harm_o = h.get("m05_harm_rate_oracle")
    harm_str = f"baseline={_md_val(harm_b)}, oracle={_md_val(harm_o)}"
    if h.get("any_regression_guard_triggered"):
        harm_str += " **— REGRESSION GUARD TRIGGERED**"
    lines.append(f"- **Harm (M05 harm_rate):** {harm_str}")
    lines.append(f"")

    # Per-task table
    lines.append(f"## 2. Per-Task Metrics Table")
    lines.append(f"")
    lines.append(
        "| Task | Resolved | Steps | M01 | M02 | M04 P@3 | M04 H@1 | M09 | M13 | "
        "M13 conf | M16 | M17 | M18 | M19 del |"
    )
    lines.append(
        "|---|:---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|"
    )
    for tid, rec in per_task.items():
        base = rec.get("baseline", {})
        ora = rec.get("oracle", {})
        # Use baseline arm for the main table (both identical in self-test)
        arm = base
        lines.append(
            f"| {tid} "
            f"| {'YES' if arm.get('resolved') else 'no'} "
            f"| {_md_val(arm.get('m03_total_steps'), '.0f')} "
            f"| {_md_val(arm.get('m01_steps_to_first_gold_read'), '.1f')} "
            f"| {_md_val(arm.get('m02_pre_edit_exploration_steps'), '.1f')} "
            f"| {_md_val(arm.get('m04_brief_precision_at_3'), '.2f')} "
            f"| {_md_val(arm.get('m04_brief_hit_at_1'), '.0f')} "
            f"| {_md_val(arm.get('m09_steps_to_resolution'), '.0f')} "
            f"| {_md_val(arm.get('m13_steps_to_gold_edit'), '.0f')} "
            f"| {str(arm.get('m13_steps_to_gold_edit_confidence', '—'))[:12]} "
            f"| {_md_val(arm.get('m16_scaffold_waste_rate'), '.2f')} "
            f"| {_md_val(arm.get('m17_wasted_steps_gt_caused'), '.0f')} "
            f"| {_md_val(arm.get('m18_exploration_waste_rate'), '.2f')} "
            f"| {_md_val(arm.get('m19_test_evidence_delivered'), '.0f')} |"
        )
    lines.append(f"")

    # Efficiency deep-dive
    lines.append(f"## 3. Efficiency Metrics Deep-Dive (M01, M02, M09, M13, M18)")
    lines.append(f"")
    lines.append(f"| Metric | Baseline Mean | Oracle Mean | Delta Mean | p-value | Significant? |")
    lines.append(f"|---|---:|---:|---:|---:|:---:|")
    for metric_key, label in [
        ("M01", "M01 Steps-to-First-Gold-Read"),
        ("M03", "M03 Total Steps"),
        ("M09", "M09 Steps-to-Resolution"),
        ("M13", "M13 Steps-to-Gold-Edit **[PRIMARY]**"),
        ("M18", "M18 Exploration Waste Rate"),
    ]:
        a = ag.get(metric_key, {})
        lines.append(
            f"| {label} "
            f"| {_md_val(a.get('baseline_mean'))} "
            f"| {_md_val(a.get('oracle_mean'))} "
            f"| {_md_val(a.get('delta_mean'))} "
            f"| {_md_val(a.get('wilcoxon_p'), '.4f')} "
            f"| {'YES' if a.get('significant_p05') else 'no'} |"
        )
    lines.append(f"")

    # Quality deep-dive
    lines.append(f"## 4. Quality Metrics Deep-Dive (M04, M05, M11, M12, M15)")
    lines.append(f"")
    lines.append(f"### M04 — Brief Precision/Recall/Hit@1")
    lines.append(f"")
    lines.append(f"| Task | P@3 (base) | P@3 (oracle) | R@3 (base) | H@1 (base) | H@1 (oracle) | Confidence |")
    lines.append(f"|---|---:|---:|---:|---:|---:|---|")
    for tid, rec in per_task.items():
        b = rec.get("baseline", {})
        o = rec.get("oracle", {})
        lines.append(
            f"| {tid} "
            f"| {_md_val(b.get('m04_brief_precision_at_3'), '.2f')} "
            f"| {_md_val(o.get('m04_brief_precision_at_3'), '.2f')} "
            f"| {_md_val(b.get('m04_brief_recall_at_3'), '.2f')} "
            f"| {_md_val(b.get('m04_brief_hit_at_1'), '.0f')} "
            f"| {_md_val(o.get('m04_brief_hit_at_1'), '.0f')} "
            f"| {b.get('m04_brief_confidence', '—')} |"
        )
    lines.append(f"")

    lines.append(f"### M05 — GT Consumption (automated heuristic)")
    lines.append(f"")
    lines.append(f"| Task | Deliveries | Explicit | Behavioral | Inert | Harmful |")
    lines.append(f"|---|---:|---:|---:|---:|---:|")
    for tid, rec in per_task.items():
        b = rec.get("baseline", {})
        lines.append(
            f"| {tid} "
            f"| {_md_val(b.get('m05_deliveries_total'), '.0f')} "
            f"| {_md_val(b.get('m05_consumption_rate_explicit'), '.2f')} "
            f"| {_md_val(b.get('m05_consumption_rate_behavioral'), '.2f')} "
            f"| {_md_val(b.get('m05_consumption_rate_inert'), '.2f')} "
            f"| {_md_val(b.get('m05_harm_rate'), '.2f')} |"
        )
    lines.append(f"")
    lines.append(f"*Note: consumption grades are automated heuristics (trajectory_automated). "
                 f"Manual adversarial audit required for verified EXPLICIT/INERT/HARMFUL grading.*")
    lines.append(f"")

    lines.append(f"### M11 — Inverted Confidence Rate")
    lines.append(f"")
    lines.append(f"| Task | Brief Confidence | HIGH Pins | Wrong HIGH Pins | Inverted Rate |")
    lines.append(f"|---|---|---:|---:|---:|")
    for tid, rec in per_task.items():
        b = rec.get("baseline", {})
        lines.append(
            f"| {tid} "
            f"| {b.get('m04_brief_confidence', '—')} "
            f"| {_md_val(b.get('m11_high_confidence_pins'), '.0f')} "
            f"| {_md_val(b.get('m11_wrong_high_confidence_pins'), '.0f')} "
            f"| {_md_val(b.get('m11_inverted_confidence_rate'), '.2f')} |"
        )
    lines.append(f"")

    # Harm metrics
    lines.append(f"## 5. Harm Metrics (M10, M17) — any increase = STOP")
    lines.append(f"")
    lines.append(f"| Task | M17 Wasted Steps (base) | M17 Wasted Steps (oracle) | M17 delta |")
    lines.append(f"|---|---:|---:|---:|")
    for tid, rec in per_task.items():
        b = rec.get("baseline", {})
        o = rec.get("oracle", {})
        d = rec.get("delta", {})
        lines.append(
            f"| {tid} "
            f"| {_md_val(b.get('m17_wasted_steps_gt_caused'), '.0f')} "
            f"| {_md_val(o.get('m17_wasted_steps_gt_caused'), '.0f')} "
            f"| {_md_val(d.get('m17_wasted_steps_delta'), '.0f')} |"
        )
    lines.append(f"")
    lines.append(f"**M10 (regression rate):** Not auto-computed — compare verifier/test-stdout.txt manually across arms.")
    lines.append(f"")

    # Code quality / correctness section (M20–M26)
    lines.append(f"## 5b. Code Quality / Correctness Metrics (M20–M26) **[PRIMARY]**")
    lines.append(f"")
    lines.append(f"*These metrics measure whether the oracle helped the agent write MORE CORRECT code.*")
    lines.append(f"*M13 measures speed; M20 measures correctness. M20 is the primary oracle signal.*")
    lines.append(f"")
    lines.append(f"### M20 — Hidden Test Pass Count (PRIMARY)")
    lines.append(f"")
    lines.append(
        "| Task | Base Pass | Base Total | Oracle Pass | Oracle Total | Delta | Available |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|:---:|")
    for tid, rec in per_task.items():
        b = rec.get("baseline", {})
        o = rec.get("oracle", {})
        d = rec.get("delta", {})
        b_pass = b.get("m20_hidden_tests_pass")
        b_total = b.get("m20_hidden_tests_total")
        o_pass = o.get("m20_hidden_tests_pass")
        o_total = o.get("m20_hidden_tests_total")
        delta = d.get("m20_hidden_tests_pass_delta")
        avail = "yes" if o.get("m20_available") else "no"
        delta_str = _md_val(delta, "+.0f") if delta is not None else "—"
        lines.append(
            f"| {tid} "
            f"| {_md_val(b_pass, '.0f')} "
            f"| {_md_val(b_total, '.0f')} "
            f"| {_md_val(o_pass, '.0f')} "
            f"| {_md_val(o_total, '.0f')} "
            f"| {delta_str} "
            f"| {avail} |"
        )
    lines.append(f"")
    m20_agg = ag.get("M20_pass_count", {})
    m20_p_str = _md_val(m20_agg.get("wilcoxon_p"), ".4f")
    m20_med_str = _md_val(m20_agg.get("delta_median"), ".2f")
    m20_mean_str = _md_val(m20_agg.get("delta_mean"), ".2f")
    lines.append(
        f"**M20 aggregate:** baseline mean={_md_val(m20_agg.get('baseline_mean'), '.2f')}, "
        f"oracle mean={_md_val(m20_agg.get('oracle_mean'), '.2f')}, "
        f"delta mean={m20_mean_str}, median={m20_med_str}, "
        f"Wilcoxon p={m20_p_str}, "
        f"significant={'YES' if m20_agg.get('significant_p05') else 'no'}"
    )
    lines.append(f"")

    lines.append(f"### M21 — Patch Completeness (obligation coverage)")
    lines.append(f"")
    lines.append("| Task | Base Completeness | Oracle Completeness | Delta | Obligations |")
    lines.append("|---|---:|---:|---:|---:|")
    for tid, rec in per_task.items():
        b = rec.get("baseline", {})
        o = rec.get("oracle", {})
        d = rec.get("delta", {})
        lines.append(
            f"| {tid} "
            f"| {_md_val(b.get('m21_patch_completeness'), '.2f')} "
            f"| {_md_val(o.get('m21_patch_completeness'), '.2f')} "
            f"| {_md_val(d.get('m21_patch_completeness_delta'), '.2f')} "
            f"| {_md_val(o.get('m21_obligations_total'), '.0f')} |"
        )
    lines.append(f"")

    lines.append(f"### M22 — Compile Clean (no compile errors)")
    lines.append(f"")
    lines.append("| Task | Base Compile OK | Oracle Compile OK | Delta | Oracle Errors |")
    lines.append("|---|:---:|:---:|---:|---:|")
    for tid, rec in per_task.items():
        b = rec.get("baseline", {})
        o = rec.get("oracle", {})
        d = rec.get("delta", {})
        b_ok = "YES" if (b.get("m22_compile_clean") is not None and b.get("m22_compile_clean", 0) > 0.5) else "no"
        o_ok = "YES" if (o.get("m22_compile_clean") is not None and o.get("m22_compile_clean", 0) > 0.5) else "no"
        lines.append(
            f"| {tid} "
            f"| {b_ok} "
            f"| {o_ok} "
            f"| {_md_val(d.get('m22_compile_clean_delta'), '.0f')} "
            f"| {_md_val(o.get('m22_compile_error_count'), '.0f')} |"
        )
    m22_agg = ag.get("M22", {})
    lines.append(f"")
    lines.append(
        f"**M22 aggregate:** baseline compile-clean={_md_val(m22_agg.get('baseline_compile_clean_rate'), '.2f')}, "
        f"oracle compile-clean={_md_val(m22_agg.get('oracle_compile_clean_rate'), '.2f')}, "
        f"McNemar b(regressions)={m22_agg.get('mcnemar_b_regressions', 0)}, "
        f"c(improvements)={m22_agg.get('mcnemar_c_improvements', 0)}"
    )
    lines.append(f"")

    lines.append(f"### M23 — Implementation Depth Score")
    lines.append(f"")
    lines.append("| Task | Base Depth | Oracle Depth | Delta | Lines+ (ora) | Branches (ora) | New Fns (ora) |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for tid, rec in per_task.items():
        b = rec.get("baseline", {})
        o = rec.get("oracle", {})
        d = rec.get("delta", {})
        lines.append(
            f"| {tid} "
            f"| {_md_val(b.get('m23_implementation_depth_score'), '.3f')} "
            f"| {_md_val(o.get('m23_implementation_depth_score'), '.3f')} "
            f"| {_md_val(d.get('m23_depth_score_delta'), '.3f')} "
            f"| {_md_val(o.get('m23_patch_lines_added'), '.0f')} "
            f"| {_md_val(o.get('m23_branch_density'), '.2f')} "
            f"| {_md_val(o.get('m23_new_functions'), '.0f')} |"
        )
    lines.append(f"")

    # Statistical tests
    lines.append(f"## 6. Statistical Tests")
    lines.append(f"")
    lines.append(f"### Primary Tests")
    lines.append(f"")
    lines.append(f"| Test | Metric | W / McNemar | p-value | n | CI lo | CI hi | Significant? |")
    lines.append(f"|---|---|---:|---:|---:|---:|---:|:---:|")
    for key, label in [
        ("m20_wilcoxon", "M20 Hidden Test Pass Count **[PRIMARY]** (H1: oracle>baseline)"),
        ("m13_wilcoxon", "M13 Steps-to-Gold-Edit (H1: oracle<baseline)"),
        ("m04_precision_wilcoxon", "M04 Brief P@3 (H1: oracle>baseline)"),
        ("m11_mcnemar", "M11 Inverted Confidence (McNemar)"),
        ("flips_mcnemar", "Resolution Flips (McNemar, Holm-corrected)"),
    ]:
        t = st.get(key, {})
        w_val = t.get("W", t.get("b", t.get("flips_observed", "—")))
        p_val = t.get("p_value")
        n_val = t.get("n", t.get("n_eligible", "—"))
        ci_lo = t.get("bootstrap_ci_lo", "—")
        ci_hi = t.get("bootstrap_ci_hi", "—")
        sig = "YES" if t.get("significant_p05") else "no"
        lines.append(
            f"| {key} | {label} "
            f"| {_md_val(w_val, '.2f')} "
            f"| {_md_val(p_val, '.4f')} "
            f"| {n_val} "
            f"| {_md_val(ci_lo)} "
            f"| {_md_val(ci_hi)} "
            f"| {sig} |"
        )
    lines.append(f"")

    lines.append(f"### Secondary Tests — Speed Metrics")
    lines.append(f"")
    lines.append(f"| Test | W | p-value | n | Direction | Significant? |")
    lines.append(f"|---|---:|---:|---:|---|:---:|")
    for key in ["m01_wilcoxon", "m03_wilcoxon", "m09_wilcoxon", "m17_wilcoxon", "m16_wilcoxon"]:
        t = st.get(key, {})
        lines.append(
            f"| {key} "
            f"| {_md_val(t.get('W'), '.2f')} "
            f"| {_md_val(t.get('p_value'), '.4f')} "
            f"| {t.get('n', '—')} "
            f"| {t.get('direction', '—')} "
            f"| {'YES' if t.get('significant_p05') else 'no'} |"
        )
    lines.append(f"")

    lines.append(f"### Secondary Tests — Code Quality Metrics (M21, M22, M23, M25, M26)")
    lines.append(f"")
    lines.append(f"| Test | W / McNemar | p-value | n | Direction | Significant? | Note |")
    lines.append(f"|---|---:|---:|---:|---|:---:|---|")
    for key in ["m21_wilcoxon", "m22_mcnemar", "m23_wilcoxon", "m25_mcnemar", "m26_wilcoxon"]:
        t = st.get(key, {})
        w_val = t.get("W", t.get("b", "—"))
        p_val = t.get("p_value")
        n_val = t.get("n", t.get("n_eligible", "—"))
        note = t.get("note", "")
        lines.append(
            f"| {key} "
            f"| {_md_val(w_val, '.2f')} "
            f"| {_md_val(p_val, '.4f')} "
            f"| {n_val} "
            f"| {t.get('direction', '—')} "
            f"| {'YES' if t.get('significant_p05') else 'no'} "
            f"| {note} |"
        )
    lines.append(f"")

    # Regression guards
    lines.append(f"## 7. Regression Guard Results")
    lines.append(f"")
    lines.append(f"| Guard | Status |")
    lines.append(f"|---|---|")
    for k, v in rg.items():
        status = "**TRIGGERED**" if v else "OK"
        lines.append(f"| {k} | {status} |")
    lines.append(f"")

    # Instrumentation flags
    lines.append(f"## 8. Instrumentation Flags")
    lines.append(f"")
    lines.append(f"| Flag | Value |")
    lines.append(f"|---|---|")
    for k, v in flags.items():
        lines.append(f"| {k} | {v} |")
    lines.append(f"")

    lines.append(f"## 9. Per-Task Paired Deltas (8dp)")
    lines.append(f"")
    lines.append(
        f"| Task | M01Δ | M03Δ | M09Δ | M13Δ | M04 P3Δ | M05 explΔ | M11 ICΔ | "
        f"M16Δ | M17Δ | M18Δ | resolvedΔ |"
    )
    lines.append(
        f"|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    for tid, d in [(tid, per_task[tid]["delta"]) for tid in per_task]:
        lines.append(
            f"| {tid} "
            f"| {_md_val(d.get('m01_delta'), '.2f')} "
            f"| {_md_val(d.get('m03_delta'), '.2f')} "
            f"| {_md_val(d.get('m09_delta'), '.2f')} "
            f"| {_md_val(d.get('m13_delta'), '.2f')} "
            f"| {_md_val(d.get('m04_precision_delta'), '.2f')} "
            f"| {_md_val(d.get('m05_explicit_consumption_delta'), '.4f')} "
            f"| {_md_val(d.get('m11_inverted_confidence_delta'), '.2f')} "
            f"| {_md_val(d.get('m16_scaffold_waste_delta'), '.4f')} "
            f"| {_md_val(d.get('m17_wasted_steps_delta'), '.2f')} "
            f"| {_md_val(d.get('m18_exploration_waste_delta'), '.4f')} "
            f"| {_md_val(d.get('resolved_delta'), '.0f')} |"
        )
    lines.append(f"")

    lines.append(f"## 9b. Per-Task Code Quality Deltas (M20–M26, 8dp) **[PRIMARY]**")
    lines.append(f"")
    lines.append(
        f"| Task | M20 PassΔ | M20 RateΔ | M21 CompletΔ | M22 CompileΔ | M23 DepthΔ | M26 SelfCorrΔ |"
    )
    lines.append(f"|---|---:|---:|---:|---:|---:|---:|")
    for tid, d in [(tid, per_task[tid]["delta"]) for tid in per_task]:
        lines.append(
            f"| {tid} "
            f"| {_md_val(d.get('m20_hidden_tests_pass_delta'), '.0f')} "
            f"| {_md_val(d.get('m20_hidden_test_pass_rate_delta'), '.4f')} "
            f"| {_md_val(d.get('m21_patch_completeness_delta'), '.4f')} "
            f"| {_md_val(d.get('m22_compile_clean_delta'), '.0f')} "
            f"| {_md_val(d.get('m23_depth_score_delta'), '.4f')} "
            f"| {_md_val(d.get('m26_self_correction_delta'), '.0f')} |"
        )
    lines.append(f"")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _assert_baseline_arm_is_off(baseline_dir: Path) -> None:
    """P2-06 — refuse GT-on directories posing as GT-OFF baseline."""
    if os.environ.get("GT_ALLOW_BASELINE_RERUN") == "1":
        return
    marker = baseline_dir / "GT_ON_ARM"
    if marker.is_file():
        raise SystemExit(
            f"[ERROR] baseline-dir {baseline_dir} is marked GT_ON_ARM — "
            "pair against frozen GT-OFF baseline only"
        )
    for outcome in baseline_dir.rglob("outcome.json"):
        try:
            data = json.loads(outcome.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for task in data.get("tasks") or []:
            if task.get("gt_prebuilt_active") is True:
                raise SystemExit(
                    f"[ERROR] baseline-dir appears GT-on (gt_prebuilt_active in {outcome}); "
                    f"use frozen baseline {FROZEN_BASELINE_MARKER}"
                )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute GT Metrics Framework (M01–M26, including M20–M26 code quality) against a paired run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__ or ""),
    )
    parser.add_argument(
        "--baseline-dir",
        required=True,
        help="Path to the frozen baseline run directory (e.g. .claude/reports/runs/tenpack_27307362054)",
    )
    parser.add_argument(
        "--oracle-dir",
        required=True,
        help="Path to the oracle (treatment) run directory",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write gt_metrics_report.json and GT_METRICS_REPORT.md",
    )
    args = parser.parse_args()

    baseline_dir = Path(args.baseline_dir).resolve()
    oracle_dir = Path(args.oracle_dir).resolve()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not baseline_dir.is_dir():
        print(f"[ERROR] baseline-dir does not exist: {baseline_dir}", file=sys.stderr)
        return 1
    _assert_baseline_arm_is_off(baseline_dir)
    if not oracle_dir.is_dir():
        print(f"[ERROR] oracle-dir does not exist: {oracle_dir}", file=sys.stderr)
        return 1

    baseline_run_id = baseline_dir.name
    oracle_run_id = oracle_dir.name

    print(f"[INFO] Loading baseline: {baseline_dir}", file=sys.stderr)
    baseline_run = load_run(baseline_dir)
    print(f"[INFO] Loading oracle:   {oracle_dir}", file=sys.stderr)
    oracle_run = load_run(oracle_dir)

    if not baseline_run:
        print("[ERROR] No tasks loaded from baseline-dir", file=sys.stderr)
        return 1
    if not oracle_run:
        print("[ERROR] No tasks loaded from oracle-dir", file=sys.stderr)
        return 1

    shared = set(baseline_run.keys()) & set(oracle_run.keys())
    if not shared:
        print("[ERROR] No shared tasks between baseline and oracle", file=sys.stderr)
        return 1

    print(
        f"[INFO] Shared tasks ({len(shared)}): {sorted(shared)}",
        file=sys.stderr,
    )

    print("[INFO] Computing paired report...", file=sys.stderr)
    report = compute_paired_report(baseline_run, oracle_run, baseline_run_id, oracle_run_id)

    # Write JSON
    json_path = output_dir / "gt_metrics_report.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"[INFO] Written: {json_path}", file=sys.stderr)

    # Write Markdown
    md_path = output_dir / "GT_METRICS_REPORT.md"
    md = generate_markdown_report(report)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[INFO] Written: {md_path}", file=sys.stderr)

    # Self-test validation: when baseline == oracle, all numeric deltas should be ~0
    headline = report.get("headline", {})
    print(f"\n[RESULT] Resolved baseline={headline.get('resolved_baseline')}, "
          f"oracle={headline.get('resolved_oracle')}", file=sys.stderr)
    print(f"[RESULT] Flips={headline.get('flip_count')}, "
          f"Regressions={headline.get('regression_count')}", file=sys.stderr)
    m13_delta_mean = headline.get("m13_delta_mean")
    if m13_delta_mean is not None:
        print(f"[RESULT] M13 delta mean = {m13_delta_mean:.8f}", file=sys.stderr)

    # Self-test check: if baseline_run_id == oracle_run_id, verify all deltas are 0
    if baseline_run_id == oracle_run_id:
        print("\n[SELF-TEST] baseline == oracle — verifying all deltas are zero...",
              file=sys.stderr)
        failures: List[str] = []
        for tid, rec in report["per_task"].items():
            d = rec["delta"]
            for k, v in d.items():
                if k in ("task_id", "baseline_present"):
                    continue
                if v is None:
                    continue  # NaN → None, skip
                try:
                    fv = float(v)
                    if not math.isnan(fv) and abs(fv) > 1e-9:
                        failures.append(f"  {tid}: {k} = {fv:.10f} (expected 0)")
                except (TypeError, ValueError):
                    pass
        if failures:
            print(f"[SELF-TEST FAIL] {len(failures)} non-zero deltas:", file=sys.stderr)
            for f_msg in failures[:20]:
                print(f_msg, file=sys.stderr)
            return 2
        else:
            print("[SELF-TEST PASS] All deltas are zero.", file=sys.stderr)

    print(f"\n[DONE] Reports written to {output_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
