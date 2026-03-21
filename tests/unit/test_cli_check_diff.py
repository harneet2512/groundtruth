"""Tests for the `groundtruth check-diff` CLI command.

Covers: stdin reading, --diff-file, exit codes, grouped output,
terse vs verbose modes, and missing-index error path.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from groundtruth.validators.obligations import Obligation

# We test check_diff_cmd directly — it's the thin wrapper under test.
from groundtruth.cli.commands import check_diff_cmd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_DIFF = textwrap.dedent("""\
    diff --git a/src/models.py b/src/models.py
    --- a/src/models.py
    +++ b/src/models.py
    @@ -10,6 +10,7 @@ class Point:
         def __init__(self, x, y):
             self.x = x
             self.y = y
    +    def distance(self, other):
    +        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2) ** 0.5
""")

OBLIGATION_OVERRIDE = Obligation(
    kind="override_contract",
    source="Point.distance",
    target="Point3D.distance",
    target_file="src/models3d.py",
    target_line=42,
    reason="overrides Point.distance — signature change propagates",
    confidence=0.9,
)

OBLIGATION_CALLER = Obligation(
    kind="caller_contract",
    source="distance",
    target="call site in tests/test_models.py",
    target_file="tests/test_models.py",
    target_line=8,
    reason="calls distance — argument changes may be needed",
    confidence=0.7,
)


def _mock_engine(obligations: list[Obligation]) -> MagicMock:
    engine = MagicMock()
    engine.infer_from_patch.return_value = obligations
    return engine


def _patch_all(obligations: list[Obligation]):
    """Return a context-manager stack that patches _load_store, ImportGraph, ObligationEngine."""
    mock_store = MagicMock()
    mock_graph = MagicMock()
    engine = _mock_engine(obligations)

    patches = [
        patch("groundtruth.cli.commands._load_store", return_value=mock_store),
        patch("groundtruth.index.graph.ImportGraph", return_value=mock_graph),
        patch("groundtruth.validators.obligations.ObligationEngine", return_value=engine),
    ]
    return patches, engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStdinReading:
    def test_stdin_diff_is_passed_to_engine(self, capsys: pytest.CaptureFixture[str]) -> None:
        patches, engine = _patch_all([])
        for p in patches:
            p.start()
        try:
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = False
                mock_stdin.read.return_value = SAMPLE_DIFF

                with pytest.raises(SystemExit) as exc_info:
                    check_diff_cmd("/fake/root")

                assert exc_info.value.code == 0
                engine.infer_from_patch.assert_called_once_with(SAMPLE_DIFF)
        finally:
            for p in patches:
                p.stop()

    def test_tty_stdin_without_diff_file_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            with pytest.raises(SystemExit) as exc_info:
                check_diff_cmd("/fake/root")
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "No diff provided" in captured.err


class TestDiffFile:
    def test_reads_diff_from_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        diff_path = tmp_path / "test.patch"
        diff_path.write_text(SAMPLE_DIFF, encoding="utf-8")

        patches, engine = _patch_all([])
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc_info:
                check_diff_cmd("/fake/root", diff_file=str(diff_path))

            assert exc_info.value.code == 0
            engine.infer_from_patch.assert_called_once_with(SAMPLE_DIFF)
        finally:
            for p in patches:
                p.stop()

    def test_missing_diff_file_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            check_diff_cmd("/fake/root", diff_file="/nonexistent/file.patch")
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "Diff file not found" in captured.err


class TestExitCodes:
    def test_no_obligations_exit_zero(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        diff_path = tmp_path / "empty.patch"
        diff_path.write_text(SAMPLE_DIFF, encoding="utf-8")

        patches, _ = _patch_all([])
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc_info:
                check_diff_cmd("/fake/root", diff_file=str(diff_path))
            assert exc_info.value.code == 0

            captured = capsys.readouterr()
            assert "No obligations found" in captured.out
        finally:
            for p in patches:
                p.stop()

    def test_obligations_exit_nonzero(self, tmp_path: Path) -> None:
        diff_path = tmp_path / "change.patch"
        diff_path.write_text(SAMPLE_DIFF, encoding="utf-8")

        patches, _ = _patch_all([OBLIGATION_CALLER])
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc_info:
                check_diff_cmd("/fake/root", diff_file=str(diff_path))
            assert exc_info.value.code == 1
        finally:
            for p in patches:
                p.stop()


class TestOutput:
    def _run_with_obligations(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], obligations: list[Obligation], *, verbose: bool = False
    ) -> str:
        diff_path = tmp_path / "test.patch"
        diff_path.write_text(SAMPLE_DIFF, encoding="utf-8")

        patches, _ = _patch_all(obligations)
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit):
                check_diff_cmd("/fake/root", diff_file=str(diff_path), verbose=verbose)
            return capsys.readouterr().out
        finally:
            for p in patches:
                p.stop()

    def test_grouped_by_kind(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        output = self._run_with_obligations(tmp_path, capsys, [OBLIGATION_OVERRIDE, OBLIGATION_CALLER])
        assert "override_contract:" in output
        assert "caller_contract:" in output

    def test_terse_one_line_per_obligation(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        output = self._run_with_obligations(tmp_path, capsys, [OBLIGATION_OVERRIDE])
        # Terse: target + reason on one line
        assert "Point3D.distance" in output
        assert "overrides Point.distance" in output
        # Should NOT contain verbose fields
        assert "confidence:" not in output
        assert "source:" not in output

    def test_verbose_shows_details(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        output = self._run_with_obligations(
            tmp_path, capsys, [OBLIGATION_OVERRIDE], verbose=True
        )
        assert "confidence: 0.9" in output
        assert "source:" in output
        assert "file:" in output
        assert "src/models3d.py:42" in output

    def test_count_in_header(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        output = self._run_with_obligations(tmp_path, capsys, [OBLIGATION_OVERRIDE, OBLIGATION_CALLER])
        assert "2 obligations found" in output

    def test_singular_obligation(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        output = self._run_with_obligations(tmp_path, capsys, [OBLIGATION_OVERRIDE])
        assert "1 obligation found" in output


class TestEmptyDiff:
    def test_empty_diff_exits_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        diff_path = tmp_path / "empty.patch"
        diff_path.write_text("", encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            check_diff_cmd("/fake/root", diff_file=str(diff_path))
        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "Empty diff" in captured.out
