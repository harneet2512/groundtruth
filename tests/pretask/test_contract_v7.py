"""Unit tests for v7 contract extraction."""

from __future__ import annotations

from pathlib import Path

from groundtruth.pretask.anchors import extract_issue_anchors
from groundtruth.pretask.contract import (
    contract_telemetry,
    detect_test_layout,
    extract_contract,
    extract_issue_calls,
)


def test_extract_contract_uses_path_convention_and_asserts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "import pytest\n"
        "from src.calc import add\n\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
        "    with pytest.raises(TypeError):\n"
        "        add(None, 2)\n",
        encoding="utf-8",
    )
    issue = "add should raise for None\n```python\nadd(1, 2)\n```"
    anchors = extract_issue_anchors(issue, None)

    result = extract_contract(
        issue,
        str(repo),
        None,
        anchors,
        ["src/calc.py"],
    )

    assert result.selected_test_files == ("tests/test_calc.py",)
    assert result.extraction_mode == "path-convention+lexical"
    assert any("assert add(1, 2) == 3" in line for line in result.contract_lines)
    assert "add(...)" in result.issue_calls

    telemetry = contract_telemetry(result, wall_ms=3)
    assert telemetry["selected_test_files"] == ["tests/test_calc.py"]
    assert telemetry["enabled"] is True


def test_extract_issue_calls_reads_fenced_blocks() -> None:
    calls = extract_issue_calls("```python\nstate.get_value('name')\nprint('x')\n```")
    assert calls == ["state.get_value(...)"]


def test_detect_test_layout_counts_common_directories(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_root.py").write_text("def test_root(): pass\n", encoding="utf-8")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "__tests__").mkdir(parents=True)
    (tmp_path / "pkg" / "__tests__" / "a.test.ts").write_text("expect(a).toBe(1)\n", encoding="utf-8")

    layout = detect_test_layout(str(tmp_path))
    assert "tests" in layout
    assert "tests/unit" in layout
    assert "pkg/__tests__" in layout
