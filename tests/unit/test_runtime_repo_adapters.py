from __future__ import annotations

from groundtruth.runtime.repo_adapters import (
    detect_repo_profile,
    is_generated_or_vendor,
    is_source_file,
    is_test_file,
    select_repo_test_command,
)
from groundtruth.runtime.test_runner import select_test_command
from groundtruth.runtime.test_runner import classify_environment_failure


def test_repo_profile_detects_python_without_locking_control_plane(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    profile = detect_repo_profile(str(tmp_path))

    assert "python" in profile.languages
    assert "pyproject.toml" in profile.manifests
    assert ["pytest"] in [list(command) for command in profile.test_commands]


def test_repo_profile_detects_non_python_stacks(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "vitest run"}}', encoding="utf-8"
    )
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")

    profile = detect_repo_profile(str(tmp_path))

    assert "typescript" in profile.languages
    assert "go" in profile.languages
    assert "rust" in profile.languages
    assert ["npm", "test"] in [list(command) for command in profile.test_commands]
    assert ["go", "test", "./..."] in [list(command) for command in profile.test_commands]
    assert ["cargo", "test"] in [list(command) for command in profile.test_commands]


def test_package_json_without_test_script_emits_no_npm_test(tmp_path) -> None:
    """Smoke20 claude-code: a package.json with no scripts.test produced
    ``npm test`` -> "Missing script: test" -> no_tests_observed while the
    real suites lived in backend/+frontend/ subpackages."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    command, _reason = select_repo_test_command(str(tmp_path))

    assert command != ["npm", "test"]


def test_package_json_placeholder_script_is_not_a_test_command(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}',
        encoding="utf-8",
    )

    command, _reason = select_repo_test_command(str(tmp_path))

    assert command != ["npm", "test"]


def test_subpackage_test_scripts_are_reached(tmp_path) -> None:
    """The monorepo shape: root package.json declares no test script; the
    suites live one level down. The command scopes the manager lifecycle to
    the subpackage rather than failing at the root."""
    (tmp_path / "package.json").write_text(
        '{"name": "root", "private": true}', encoding="utf-8"
    )
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "package.json").write_text(
        '{"scripts": {"test": "vitest run"}}', encoding="utf-8"
    )
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        '{"scripts": {"test": "jest"}}', encoding="utf-8"
    )

    command, reason = select_repo_test_command(str(tmp_path))

    assert command == ["npm", "--prefix", "backend", "test"]
    assert reason == "javascript-typescript"


def test_subpackage_respects_lockfile_package_manager(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "package.json").write_text(
        '{"scripts": {"test": "vitest run"}}', encoding="utf-8"
    )

    command, _reason = select_repo_test_command(str(tmp_path))

    assert command == ["pnpm", "--dir", "backend", "test"]


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


def test_java_adapter_emits_wrapper_and_plain_forms(tmp_path) -> None:
    """All four Java branches: wrapper preferred, plain managers otherwise."""
    from groundtruth.runtime.repo_adapters import JavaRepoAdapter

    adapter = JavaRepoAdapter()

    (tmp_path / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    assert adapter.test_commands(tmp_path) == [["mvn", "test"]]

    (tmp_path / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    assert adapter.test_commands(tmp_path) == [["./mvnw", "test"]]

    (tmp_path / "mvnw").unlink()
    (tmp_path / "pom.xml").unlink()
    (tmp_path / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
    assert adapter.test_commands(tmp_path) == [["gradle", "test"]]

    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    assert adapter.test_commands(tmp_path) == [["./gradlew", "test"]]


def test_python_adapter_manifest_variants(tmp_path) -> None:
    from groundtruth.runtime.repo_adapters import PythonRepoAdapter

    adapter = PythonRepoAdapter()

    # No manifest and no tests dir: nothing to bind.
    assert adapter.test_commands(tmp_path) == []

    # A bare tests/ tree implies pytest.
    (tmp_path / "tests").mkdir()
    assert adapter.test_commands(tmp_path) == [["pytest"]]

    # tox.ini switches the lifecycle to tox.
    (tmp_path / "tox.ini").write_text("[tox]\n", encoding="utf-8")
    assert adapter.test_commands(tmp_path) == [["tox"]]

    # pyproject/pytest.ini are the pytest signal.
    (tmp_path / "tox.ini").unlink()
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert adapter.test_commands(tmp_path) == [["pytest"]]


def test_every_adapter_emitted_command_binds_as_test_runner(tmp_path) -> None:
    """Adapter output must satisfy the canonical TEST_RUNNER_RE.

    The plan binder gates on TEST_RUNNER_RE.match; an adapter that emits a
    command the binder rejects produces checks that pend forever — the
    ./mvnw / ./gradlew shape did exactly that until the runner pattern
    learned script-path prefixes. Every adapter command must round-trip.
    """
    from groundtruth.runtime.patterns import TEST_RUNNER_RE
    from groundtruth.runtime.repo_adapters import (
        GoRepoAdapter,
        JavaScriptRepoAdapter,
        JavaRepoAdapter,
        PythonRepoAdapter,
        RustRepoAdapter,
    )
    import shlex

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (repo / "package.json").write_text(
        '{"scripts": {"test": "vitest run"}}', encoding="utf-8"
    )
    (repo / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    (repo / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    (repo / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")

    emitted = []
    for adapter in (
        PythonRepoAdapter(),
        JavaScriptRepoAdapter(),
        GoRepoAdapter(),
        RustRepoAdapter(),
        JavaRepoAdapter(),
    ):
        emitted.extend(adapter.test_commands(repo))

    assert emitted, "no adapter emitted a command on a five-language repo"
    for argv in emitted:
        command = shlex.join(argv)
        assert TEST_RUNNER_RE.match(command), (
            f"adapter emitted an unbindable test command: {command}"
        )


def test_select_repo_test_command_prefers_detected_adapter_order(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "vitest run"}}', encoding="utf-8"
    )

    command, reason = select_repo_test_command(str(tmp_path))

    assert command == ["npm", "test"]
    assert reason == "javascript-typescript"


def test_environment_classifier_generic_categories() -> None:
    cases = {
        "missing_runner": "pytest: command not found",
        "missing_manifest": "go: go.mod file not found in current directory",
        "package_manager_mismatch": "ERR_PNPM_OUTDATED_LOCKFILE frozen-lockfile",
        "unresolved_module_or_artifact": "Cannot find module '@app/core'",
        "missing_linker_or_toolchain": "error: linker `cc` not found",
        "offline_install_or_proxy": "Temporary failure in name resolution while downloading",
    }
    for expected, text in cases.items():
        assert classify_environment_failure(text) == expected
