"""Artifact-first TTD tests for regression autopsy.

Every test replays a FROZEN artifact from tests/fixtures/trajectory/ and
asserts an expected classification written in EXPECTED_BEHAVIOR.md BEFORE
any classifier implementation exists. These tests MUST fail on first run.

Artifacts:
  lsp_13453/   — Qwen3-Coder lsp arm, baseline RESOLVED, GT FAILED
                 5 steers to html.py, 5 edits to html.py, 3 ack_engagement,
                 3 ack_not_observed, 2483-byte wrong patch
  nolsp_13453/ — same task, nolsp arm, 4 events total, 0 edits, bootstrap crash
  nolsp_13579/ — nolsp arm, 3 events total, 0 edits, bootstrap crash
  gold_13453.patch — ground truth: 2 lines in astropy/io/ascii/html.py

Expected behaviors derived from artifacts (not from implementation):
  See tests/fixtures/trajectory/EXPECTED_BEHAVIOR.md
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "trajectory"


def _load_events(case_dir: str) -> list[dict]:
    p = FIXTURES / case_dir / "gt_hook_telemetry.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _load_patch_files(case_dir: str) -> list[str]:
    """Extract file paths touched by the agent's patch."""
    p = FIXTURES / case_dir / "preds.json"
    preds = json.load(open(p))
    files = []
    for v in preds.values():
        patch = v.get("model_patch", "")
        for line in patch.split("\n"):
            if line.startswith("+++ b/"):
                files.append(line[6:])
    return files


def _load_gold_files() -> list[str]:
    """Extract file paths touched by the gold patch for 13453."""
    p = FIXTURES / "gold_13453.patch"
    files = []
    for line in p.read_text().splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:])
    return files


# ─────────────────────────────────────────────────────────────────────
# The classifier under test. It does NOT exist yet. Importing it must
# fail on first run, which is what makes these tests red-before-green.
# ─────────────────────────────────────────────────────────────────────
try:
    from scripts.swebench.trajectory_classifier import classify_trace
except ImportError:
    classify_trace = None

CLASSIFIER_EXISTS = classify_trace is not None
requires_classifier = pytest.mark.skipif(
    not CLASSIFIER_EXISTS,
    reason="trajectory_classifier not implemented yet — test is RED (expected)"
)


# ═════════════════════════════════════════════════════════════════════
# TEST 1 — EB-1: lsp/13453 behavioral alignment
# ═════════════════════════════════════════════════════════════════════
@requires_classifier
def test_lsp_13453_behavioral_alignment():
    """From EXPECTED_BEHAVIOR.md EB-1:

    All 5 material_edit events target html.py. All 5 steer_delivered events
    target html.py. The agent edited the file GT steered it toward on every
    cycle where both occurred.

    Expected: behavioral_alignment >= 1 (ideally 5).
    This test must FAIL before the classifier is implemented.
    """
    events = _load_events("lsp_13453")
    result = classify_trace(events)
    assert result["behavioral_alignment"] >= 1, (
        f"Agent edited the steered file 5 times. behavioral_alignment must "
        f"be >= 1. Got {result.get('behavioral_alignment')}"
    )


# ═════════════════════════════════════════════════════════════════════
# TEST 2 — EB-2: lsp/13453 steer relevance (targets gold file)
# ═════════════════════════════════════════════════════════════════════
@requires_classifier
def test_lsp_13453_steer_targets_gold_file():
    """From EXPECTED_BEHAVIOR.md EB-2:

    All steers target html.py. Gold patch modifies html.py.
    steer_targets_gold_file must be true.
    """
    events = _load_events("lsp_13453")
    gold_files = _load_gold_files()
    result = classify_trace(events, gold_files=gold_files)
    assert result["steer_targets_gold_file"] is True, (
        f"GT steered to html.py which IS the gold file. "
        f"steer_targets_gold_file must be True."
    )


# ═════════════════════════════════════════════════════════════════════
# TEST 3 — EB-3: lsp/13453 low-information confirmation
# ═════════════════════════════════════════════════════════════════════
@requires_classifier
def test_lsp_13453_low_information_confirmation():
    """From EXPECTED_BEHAVIOR.md EB-3:

    First steer arrives at cycle 14. First material_edit also at cycle 14.
    Agent was already editing html.py when steer arrived. Steer confirmed
    what agent was already doing — did not provide new localization.

    Expected: low_information_confirmation = true.
    """
    events = _load_events("lsp_13453")
    result = classify_trace(events)
    assert result["low_information_confirmation"] is True, (
        f"Agent was editing html.py at cycle 14 when steer for html.py "
        f"arrived at cycle 14. This is a confirmation, not a discovery."
    )


# ═════════════════════════════════════════════════════════════════════
# TEST 4 — EB-4: lsp/13453 steer repetition
# ═════════════════════════════════════════════════════════════════════
@requires_classifier
def test_lsp_13453_steer_repetition():
    """From EXPECTED_BEHAVIOR.md EB-4:

    5 steers all targeting html.py. Steers 2-5 repeat steer 1's target
    after agent has demonstrated it is working on that file.

    Expected: repeated_steer_count >= 3 (steers 3-5 are pure noise).
    """
    events = _load_events("lsp_13453")
    result = classify_trace(events)
    assert result["repeated_steer_count"] >= 3, (
        f"5 steers to the same file; steers 3-5 are noise after agent "
        f"demonstrated alignment. repeated_steer_count must be >= 3. "
        f"Got {result.get('repeated_steer_count')}"
    )


# ═════════════════════════════════════════════════════════════════════
# TEST 5 — EB-5: lsp/13453 is NOT agent noncompliance
# ═════════════════════════════════════════════════════════════════════
@requires_classifier
def test_lsp_13453_not_agent_noncompliance():
    """From EXPECTED_BEHAVIOR.md EB-5:

    Agent edited the steered file 5 times. The patch is wrong (16 lines
    vs gold 2 lines) but the agent was working on the correct file/function.

    Expected: agent_noncompliance = false.
    """
    events = _load_events("lsp_13453")
    result = classify_trace(events)
    assert result["agent_noncompliance"] is False, (
        f"Agent edited the steered file. Cannot be noncompliance. "
        f"The fix being wrong is a different failure class."
    )


# ═════════════════════════════════════════════════════════════════════
# TEST 6 — EB-6: nolsp/13453 bootstrap failure
# ═════════════════════════════════════════════════════════════════════
@requires_classifier
def test_nolsp_13453_bootstrap_failure():
    """From EXPECTED_BEHAVIOR.md EB-6:

    4 events total. 0 material edits. 0 steers. 0 patch bytes.
    Agent died at cycle 1.

    Expected: failure_class = bootstrap_infra_failure.
    Must be excluded from steer effectiveness analysis.
    Must NOT be classified as agent_noncompliance or steer_harmful.
    """
    events = _load_events("nolsp_13453")
    result = classify_trace(events)
    assert result["failure_class"] == "bootstrap_infra_failure", (
        f"4 events, 0 edits, agent died at cycle 1. "
        f"Must be bootstrap_infra_failure, not {result.get('failure_class')}"
    )
    assert result["excluded_from_steer_effectiveness"] is True
    assert result["agent_noncompliance"] is False
    assert result.get("steer_harmful") is not True


# ═════════════════════════════════════════════════════════════════════
# TEST 7 — EB-7: nolsp/13579 bootstrap failure (same pattern)
# ═════════════════════════════════════════════════════════════════════
@requires_classifier
def test_nolsp_13579_bootstrap_failure():
    """From EXPECTED_BEHAVIOR.md EB-7:

    3 events total. 0 edits. 0 steers. 0 patch.
    Same bootstrap crash pattern as nolsp/13453.
    """
    events = _load_events("nolsp_13579")
    result = classify_trace(events)
    assert result["failure_class"] == "bootstrap_infra_failure"
    assert result["excluded_from_steer_effectiveness"] is True
    assert result["agent_noncompliance"] is False


# ═════════════════════════════════════════════════════════════════════
# TEST 8 — EB-8: Negative control — edit different file than steered
# ═════════════════════════════════════════════════════════════════════
@requires_classifier
def test_negative_control_edit_different_file():
    """From EXPECTED_BEHAVIOR.md EB-8:

    Synthetic trace: steer targets file_A.py, agent edits file_B.py.
    behavioral_alignment must be 0. possible_noncompliance must be true.

    This is the negative control for TEST 1. If TEST 1 passes but this
    test also passes with alignment > 0, the classifier is broken.
    """
    synthetic_events = [
        {"event": "checkpoint_startup", "cycle": 1},
        {"event": "material_edit", "cycle": 5, "files": ["src/file_B.py"]},
        {"event": "steer_delivered", "cycle": 5, "file": "src/file_A.py",
         "channel": "micro"},
        {"event": "ack_not_observed", "cycle": 9, "file": "src/file_A.py"},
    ]
    result = classify_trace(synthetic_events)
    assert result["behavioral_alignment"] == 0, (
        "Agent edited file_B after steer for file_A. "
        "behavioral_alignment must be 0."
    )
    assert result["possible_noncompliance"] is True, (
        "Steer was specific (file_A), agent went to file_B. "
        "possible_noncompliance must be true."
    )


# ═════════════════════════════════════════════════════════════════════
# TEST 9 — EB-9: Negative control — pre-steer edits don't count
# ═════════════════════════════════════════════════════════════════════
@requires_classifier
def test_negative_control_pre_steer_edit_not_alignment():
    """From EXPECTED_BEHAVIOR.md EB-9:

    Agent edits file_X at cycle 5. Steer for file_X arrives at cycle 10.
    The cycle-5 edit is BEFORE the steer. It must NOT count as
    behavioral_alignment. Only post-steer edits count.
    """
    synthetic_events = [
        {"event": "checkpoint_startup", "cycle": 1},
        {"event": "material_edit", "cycle": 5, "files": ["src/file_X.py"]},
        {"event": "steer_delivered", "cycle": 10, "file": "src/file_X.py",
         "channel": "micro"},
        {"event": "ack_not_observed", "cycle": 14, "file": "src/file_X.py"},
    ]
    result = classify_trace(synthetic_events)
    assert result["behavioral_alignment"] == 0, (
        "Edit at cycle 5 happened BEFORE steer at cycle 10. "
        "Pre-steer edits must not count as alignment."
    )


# ═════════════════════════════════════════════════════════════════════
# TEST 10 — Mutation killer: backslash path normalization
# ═════════════════════════════════════════════════════════════════════
@requires_classifier
def test_backslash_path_still_matches_forward_slash():
    r"""Mutation check: removing _normalize_path must break this test.

    Steer uses backslash path (src\utils\helper.py), edit uses forward
    slash (src/utils/helper.py). These refer to the same file. The
    classifier must normalize both before comparing.

    Without normalization, alignment = 0 (paths don't match as strings).
    With normalization, alignment = 1.
    """
    synthetic_events = [
        {"event": "checkpoint_startup", "cycle": 1},
        {"event": "steer_delivered", "cycle": 5,
         "file": "src\\utils\\helper.py", "channel": "micro"},
        {"event": "material_edit", "cycle": 6,
         "files": ["src/utils/helper.py"]},
    ]
    result = classify_trace(synthetic_events)
    assert result["behavioral_alignment"] == 1, (
        r"Steer with backslash path (src\utils\helper.py) must match "
        "edit with forward slash (src/utils/helper.py). "
        f"Got alignment={result['behavioral_alignment']}"
    )
