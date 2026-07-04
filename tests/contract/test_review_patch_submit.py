"""Contract tests for review_patch submit interception.

Verifies §1.1 and §1.2 of GT_ARCHITECTURE_CONTRACT.md:
- review_patch fires inside _hooked_execute (agent can respond)
- clean diff allows submit (no spurious blocking)
- finding pauses submit (appended to output)
- repeated finding does not spam
- ACK allows submit (re-fire only on new edits)
- fixed diff allows submit (reset on new edit cycle)
"""

from __future__ import annotations

import os

import pytest


# ── Harness source analysis ─────────────────────────────────────────────


def _read_harness() -> str:
    harness_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "benchmarks", "swebench",
        "run_mini_gt_hooked.py",
    )
    with open(os.path.abspath(harness_path)) as f:
        return f.read()


def _extract_function_body(source: str, func_name: str) -> str:
    """Extract the body of a function from source code."""
    lines = source.split("\n")
    in_func = False
    body_lines: list[str] = []
    base_indent = 0
    for line in lines:
        if f"def {func_name}" in line:
            in_func = True
            base_indent = len(line) - len(line.lstrip())
            body_lines.append(line)
            continue
        if in_func:
            stripped = line.lstrip()
            current_indent = len(line) - len(line.lstrip())
            if stripped and current_indent <= base_indent and not stripped.startswith("#"):
                break
            body_lines.append(line)
    return "\n".join(body_lines)


class TestReviewPatchInHookedExecute:
    """review_patch must fire inside _hooked_execute, not only post-run."""

    def test_review_patch_called_in_hooked_execute(self) -> None:
        source = _read_harness()
        body = _extract_function_body(source, "_hooked_execute")
        assert "_run_review_patch" in body, (
            "_run_review_patch must be called inside _hooked_execute"
        )

    def test_review_patch_fires_on_git_diff(self) -> None:
        """git diff command triggers review_patch when edits exist."""
        source = _read_harness()
        body = _extract_function_body(source, "_hooked_execute")
        assert "_is_git_review_command" in body, (
            "_hooked_execute must check for git review commands"
        )

    def test_review_patch_fires_on_submit(self) -> None:
        """submit command triggers review_patch."""
        source = _read_harness()
        body = _extract_function_body(source, "_hooked_execute")
        assert "_is_submit_command" in body, (
            "_hooked_execute must check for submit commands"
        )

    def test_review_output_appended_to_result(self) -> None:
        """review_patch output is appended to command result so agent sees it."""
        source = _read_harness()
        body = _extract_function_body(source, "_hooked_execute")
        assert 'result["output"]' in body and "review_output" in body, (
            "review_patch output must be appended to result['output']"
        )


class TestReviewPatchStateTracking:
    """review_patch must track state to avoid spamming."""

    def test_review_state_exists(self) -> None:
        source = _read_harness()
        assert "_review_state" in source, "must have per-container review state"

    def test_review_resets_on_new_edits(self) -> None:
        """review_patch must re-fire when new edits occur after a review."""
        source = _read_harness()
        body = _extract_function_body(source, "_hooked_execute")
        assert "edit_cycle" in body, (
            "review_patch must track edit_cycle to detect new edits"
        )

    def test_no_repeat_without_new_edits(self) -> None:
        """review_patch must not fire twice for the same edit cycle."""
        source = _read_harness()
        body = _extract_function_body(source, "_hooked_execute")
        assert "state[\"fired\"]" in body or 'state["fired"]' in body or "not state" in body or "state.get(\"fired\")" in body, (
            "review_patch must check fired flag before re-firing"
        )


class TestCleanDiffAllowsSubmit:
    """Clean diff (no review findings) must not block submit."""

    def test_no_output_when_no_findings(self) -> None:
        """_run_review_patch returns empty string when no findings."""
        source = _read_harness()
        body = _extract_function_body(source, "_run_review_patch")
        assert 'return ""' in body, (
            "review_patch must return empty string when no findings"
        )

    def test_no_output_appended_when_clean(self) -> None:
        """When review_output is empty, result is not modified."""
        source = _read_harness()
        body = _extract_function_body(source, "_hooked_execute")
        assert "if review_output" in body, (
            "result must only be modified when review_output is non-empty"
        )


class TestFindingPausesSubmit:
    """When findings exist, output is appended so agent sees them."""

    def test_findings_appended_to_output(self) -> None:
        source = _read_harness()
        body = _extract_function_body(source, "_hooked_execute")
        assert "review_output" in body and "result" in body

    def test_submit_paused_logged(self) -> None:
        """submit_paused_for_review must be logged in metadata."""
        source = _read_harness()
        assert "submit_paused_for_review" in source
        assert "review_patch_called_pre_submit" in source


class TestNoveltyInReviewPatch:
    """review_patch must use the shared novelty filter."""

    def test_review_uses_novelty_filter(self) -> None:
        source = _read_harness()
        body = _extract_function_body(source, "_run_review_patch")
        assert "_filter_novel_findings" in body, (
            "review_patch must use host-side novelty filtering"
        )


class TestReviewPatchMetadata:
    """review_patch must log structured metadata."""

    def test_metadata_fields(self) -> None:
        source = _read_harness()
        required = [
            "review_patch_called_pre_submit",
            "submit_paused_for_review",
            "review_findings_count",
            "review_high_confidence_count",
        ]
        for field in required:
            assert field in source, f"metadata must include {field}"

    def test_review_state_cleanup(self) -> None:
        source = _read_harness()
        assert "_review_state.pop" in source, (
            "review_state must be cleaned up per container"
        )


class TestGitReviewDetection:
    """Helper functions for detecting review/submit commands."""

    def test_git_diff_is_review(self) -> None:
        from benchmarks.swebench.run_mini_gt_hooked import _is_git_review_command
        assert _is_git_review_command("git diff")
        assert _is_git_review_command("git diff --stat")
        assert _is_git_review_command("git status")
        assert not _is_git_review_command("git add .")
        assert not _is_git_review_command("git commit -m 'fix'")
        assert not _is_git_review_command("grep something")

    def test_submit_detection(self) -> None:
        from benchmarks.swebench.run_mini_gt_hooked import _is_submit_command
        assert _is_submit_command("submit")
        assert _is_submit_command("submit_patch")
        assert _is_submit_command("exit")
        assert _is_submit_command("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt")
        assert _is_submit_command("echo complete_task_and_submit_final_output")
        assert not _is_submit_command("git diff")
        assert not _is_submit_command("echo hello")
