"""Tests for the bounded test-feedback policy module."""

from __future__ import annotations

from groundtruth.policy.test_feedback import TestFeedbackPolicy, TestTarget


class TestFindRelevantTests:
    """Test find_relevant_tests matching logic."""

    def setup_method(self) -> None:
        self.policy = TestFeedbackPolicy(max_test_files=3)

    def test_name_convention_match(self) -> None:
        """src/validators/obligations.py -> tests/unit/test_obligations.py"""
        targets = self.policy.find_relevant_tests(
            changed_files=["src/validators/obligations.py"],
            available_test_files=["tests/unit/test_obligations.py"],
        )
        assert len(targets) == 1
        assert targets[0].test_file == "tests/unit/test_obligations.py"
        assert targets[0].source_file == "src/validators/obligations.py"
        assert targets[0].match_type == "name_convention"
        assert targets[0].confidence >= 0.9

    def test_name_convention_nested_path(self) -> None:
        """src/groundtruth/policy/abstention.py -> tests/unit/test_abstention.py"""
        targets = self.policy.find_relevant_tests(
            changed_files=["src/groundtruth/policy/abstention.py"],
            available_test_files=[
                "tests/unit/test_abstention.py",
                "tests/unit/test_store.py",
            ],
        )
        assert len(targets) == 1
        assert targets[0].test_file == "tests/unit/test_abstention.py"
        assert targets[0].match_type == "name_convention"

    def test_name_convention_shared_subdir_boosts_confidence(self) -> None:
        """Shared meaningful directory segments boost confidence."""
        targets = self.policy.find_relevant_tests(
            changed_files=["src/validators/obligations.py"],
            available_test_files=[
                "tests/validators/test_obligations.py",
                "tests/unit/test_obligations.py",
            ],
        )
        assert len(targets) == 1
        # The one with shared "validators" directory should win
        assert targets[0].test_file == "tests/validators/test_obligations.py"
        assert targets[0].confidence == 0.95

    def test_directory_match_fallback(self) -> None:
        """Falls back to directory match when no name convention match."""
        targets = self.policy.find_relevant_tests(
            changed_files=["src/foo/bar.py"],
            available_test_files=["tests/foo/test_something.py"],
        )
        assert len(targets) == 1
        assert targets[0].match_type == "directory_match"
        assert targets[0].confidence == 0.5

    def test_no_match_returns_empty(self) -> None:
        """No relevant tests -> empty list."""
        targets = self.policy.find_relevant_tests(
            changed_files=["src/completely/unrelated.py"],
            available_test_files=["tests/unit/test_obligations.py"],
        )
        assert targets == []

    def test_respects_max_test_files_cap(self) -> None:
        """Only returns up to max_test_files results."""
        policy = TestFeedbackPolicy(max_test_files=1)
        targets = policy.find_relevant_tests(
            changed_files=["src/foo.py", "src/bar.py"],
            available_test_files=["tests/test_foo.py", "tests/test_bar.py"],
        )
        assert len(targets) == 1

    def test_multiple_changed_files(self) -> None:
        """Multiple changed files each find their best match."""
        targets = self.policy.find_relevant_tests(
            changed_files=["src/foo.py", "src/bar.py"],
            available_test_files=[
                "tests/test_foo.py",
                "tests/test_bar.py",
                "tests/test_unrelated.py",
            ],
        )
        assert len(targets) == 2
        matched_tests = {t.test_file for t in targets}
        assert "tests/test_foo.py" in matched_tests
        assert "tests/test_bar.py" in matched_tests


class TestFormatTestNudge:
    """Test format_test_nudge output."""

    def setup_method(self) -> None:
        self.policy = TestFeedbackPolicy()

    def test_with_targets(self) -> None:
        targets = [
            TestTarget("tests/test_foo.py", "src/foo.py", "name_convention", 0.9),
        ]
        result = self.policy.format_test_nudge(targets)
        assert result is not None
        assert "pytest tests/test_foo.py -x" in result
        assert "src/foo.py" in result

    def test_no_targets_returns_none(self) -> None:
        result = self.policy.format_test_nudge([])
        assert result is None


class TestTruncateOutput:
    """Test output truncation."""

    def setup_method(self) -> None:
        self.policy = TestFeedbackPolicy(output_truncate_lines=50)

    def test_under_limit_unchanged(self) -> None:
        output = "\n".join(f"line {i}" for i in range(30))
        assert self.policy.truncate_output(output) == output

    def test_over_limit_truncated(self) -> None:
        lines = [f"line {i}" for i in range(100)]
        output = "\n".join(lines)
        result = self.policy.truncate_output(output)
        result_lines = result.split("\n")
        # 10 head + 1 marker + 40 tail = 51 lines
        assert len(result_lines) == 51
        assert result_lines[0] == "line 0"
        assert result_lines[9] == "line 9"
        assert "truncated 50 lines" in result_lines[10]
        assert result_lines[-1] == "line 99"
