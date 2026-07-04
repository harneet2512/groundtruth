from __future__ import annotations

from pathlib import Path

from groundtruth.pretask.project_instructions import extract_project_instructions


def test_project_instructions_prefers_nearest_scoped_file(tmp_path: Path) -> None:
    (tmp_path / "pkg" / "feature").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text(
        "Always run pytest before submitting.\nNever edit generated files.\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "feature" / "AGENTS.md").write_text(
        "Run pytest tests/test_feature.py for feature changes.\n"
        "Do not modify snapshots unless behavior changes.\n",
        encoding="utf-8",
    )

    result = extract_project_instructions(
        str(tmp_path),
        focus_files=[{"file": "pkg/feature/core.py"}],
        candidate_files=["pkg/feature/core.py"],
    )

    assert result.selected_sources[0] == "pkg/feature/AGENTS.md"
    assert result.rendered_constraints
    assert "tests/test_feature.py" in result.rendered_constraints[0]
    assert result.evidence[0].path == "pkg/feature/AGENTS.md"
    assert result.evidence[0].precedence > result.evidence[1].precedence


def test_project_instructions_reads_readme_test_sections_as_low_precedence(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Project\n\n"
        "General text.\n\n"
        "## Testing\n\n"
        "Run go test ./... before submitting changes.\n",
        encoding="utf-8",
    )

    result = extract_project_instructions(str(tmp_path), candidate_files=["pkg/core.go"])

    assert result.selected_sources == ("README.md",)
    assert result.rendered_constraints == (
        "Repo validation hint from README.md: Run go test ./... before submitting changes.",
    )
