from __future__ import annotations

from groundtruth.runtime.repo_adapters import (
    detect_repo_profile,
    is_generated_or_vendor,
    is_source_file,
    is_test_file,
    select_repo_test_command,
)
from groundtruth.runtime.test_runner import select_test_command


def test_repo_profile_detects_python_without_locking_control_plane(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    profile = detect_repo_profile(str(tmp_path))

    assert "python" in profile.languages
    assert "pyproject.toml" in profile.manifests
    assert ["pytest"] in [list(command) for command in profile.test_commands]


def test_repo_profile_detects_non_python_stacks(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")

    profile = detect_repo_profile(str(tmp_path))

    assert "typescript" in profile.languages
    assert "go" in profile.languages
    assert "rust" in profile.languages
    assert ["npm", "test"] in [list(command) for command in profile.test_commands]
    assert ["go", "test", "./..."] in [list(command) for command in profile.test_commands]
    assert ["cargo", "test"] in [list(command) for command in profile.test_commands]


def test_repo_adapter_file_classification_is_language_neutral() -> None:
    assert is_test_file("tests/test_auth.py")
    assert is_test_file("src/auth/auth.test.ts")
    assert is_test_file("pkg/auth/auth_test.go")
    assert is_test_file("src/test/java/UserTest.java")
    assert is_source_file("src/auth/service.ts")
    assert is_source_file("pkg/auth/service.go")
    assert not is_source_file("src/auth/auth.test.ts")
    assert is_generated_or_vendor("node_modules/lib/index.js")
    assert is_generated_or_vendor("pkg/api/service.pb.go")
    assert is_generated_or_vendor("dist/app.js")


def test_select_test_command_uses_repo_profile_for_non_python(tmp_path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")

    result = select_test_command(str(tmp_path), mode="contract", plan={})

    assert result["command"] == ["go", "test", "./..."]
    assert result["reason"] == "go"
    assert result["repo_profile"]["languages"] == ["go"]


def test_select_repo_test_command_prefers_detected_adapter_order(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    command, reason = select_repo_test_command(str(tmp_path))

    assert command == ["npm", "test"]
    assert reason == "javascript-typescript"
