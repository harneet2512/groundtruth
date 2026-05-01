"""Language-neutral repository adapters for GT runtime control.

These adapters are intentionally shallow: they classify files, generated/vendor
space, public surfaces, and visible validation commands without running
language-specific analysis. Higher-fidelity adapters can extend this contract
without changing telemetry schemas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


SOURCE_EXTS = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".rb",
    ".php",
    ".cs",
    ".swift",
    ".scala",
}
TEST_DIR_PARTS = {"test", "tests", "__tests__", "spec", "specs"}
GENERATED_VENDOR_PARTS = {
    "vendor",
    "node_modules",
    "dist",
    "build",
    "target",
    "coverage",
    ".venv",
    "venv",
    "__pycache__",
}


@dataclass(frozen=True)
class RepoProfile:
    root: str
    languages: tuple[str, ...] = field(default_factory=tuple)
    manifests: tuple[str, ...] = field(default_factory=tuple)
    test_commands: tuple[list[str], ...] = field(default_factory=tuple)
    adapter_names: tuple[str, ...] = field(default_factory=tuple)


class RepoAdapter:
    name = "generic"
    language = "generic"
    manifests: tuple[str, ...] = ()
    source_exts: tuple[str, ...] = ()

    def matches(self, root: Path) -> bool:
        return any((root / manifest).exists() for manifest in self.manifests)

    def test_commands(self, root: Path) -> list[list[str]]:
        return []

    def is_source_file(self, path: str) -> bool:
        norm = _norm(path)
        return Path(norm).suffix.lower() in self.source_exts and not self.is_test_file(norm)

    def is_test_file(self, path: str) -> bool:
        return is_test_file(path)

    def is_generated_or_vendor(self, path: str) -> bool:
        return is_generated_or_vendor(path)

    def is_public_api_surface(self, path: str) -> bool:
        return is_public_api_surface(path)


class PythonRepoAdapter(RepoAdapter):
    name = "python"
    language = "python"
    manifests = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "setup.py")
    source_exts = (".py", ".pyi")

    def matches(self, root: Path) -> bool:
        return super().matches(root) or any(root.rglob("*.py"))

    def test_commands(self, root: Path) -> list[list[str]]:
        if (root / "pytest.ini").exists() or (root / "pyproject.toml").exists():
            return [["pytest"]]
        if (root / "tox.ini").exists():
            return [["tox"]]
        return [["pytest"]] if (root / "tests").exists() else []


class JavaScriptRepoAdapter(RepoAdapter):
    name = "javascript-typescript"
    language = "typescript"
    manifests = ("package.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json")
    source_exts = (".js", ".jsx", ".ts", ".tsx")

    def test_commands(self, root: Path) -> list[list[str]]:
        if (root / "pnpm-lock.yaml").exists():
            return [["pnpm", "test"]]
        if (root / "yarn.lock").exists():
            return [["yarn", "test"]]
        if (root / "package.json").exists():
            return [["npm", "test"]]
        return []


class GoRepoAdapter(RepoAdapter):
    name = "go"
    language = "go"
    manifests = ("go.mod",)
    source_exts = (".go",)

    def test_commands(self, root: Path) -> list[list[str]]:
        return [["go", "test", "./..."]] if (root / "go.mod").exists() else []


class RustRepoAdapter(RepoAdapter):
    name = "rust"
    language = "rust"
    manifests = ("Cargo.toml",)
    source_exts = (".rs",)

    def test_commands(self, root: Path) -> list[list[str]]:
        return [["cargo", "test"]] if (root / "Cargo.toml").exists() else []


class JavaRepoAdapter(RepoAdapter):
    name = "java"
    language = "java"
    manifests = ("pom.xml", "build.gradle", "build.gradle.kts", "gradlew")
    source_exts = (".java", ".kt")

    def test_commands(self, root: Path) -> list[list[str]]:
        if (root / "mvnw").exists() or (root / "pom.xml").exists():
            return [["./mvnw", "test"]] if (root / "mvnw").exists() else [["mvn", "test"]]
        if (root / "gradlew").exists():
            return [["./gradlew", "test"]]
        if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
            return [["gradle", "test"]]
        return []


class GenericRepoAdapter(RepoAdapter):
    name = "generic"
    language = "generic"
    source_exts = tuple(sorted(SOURCE_EXTS))

    def matches(self, root: Path) -> bool:
        return True


ADAPTERS: tuple[RepoAdapter, ...] = (
    PythonRepoAdapter(),
    JavaScriptRepoAdapter(),
    GoRepoAdapter(),
    RustRepoAdapter(),
    JavaRepoAdapter(),
)
GENERIC_ADAPTER = GenericRepoAdapter()


def detect_repo_profile(repo_root: str) -> RepoProfile:
    root = Path(repo_root)
    matched = [adapter for adapter in ADAPTERS if adapter.matches(root)]
    if not matched:
        matched = [GENERIC_ADAPTER]
    manifests: list[str] = []
    commands: list[list[str]] = []
    for adapter in matched:
        for manifest in adapter.manifests:
            if (root / manifest).exists() and manifest not in manifests:
                manifests.append(manifest)
        for command in adapter.test_commands(root):
            if command not in commands:
                commands.append(command)
    return RepoProfile(
        root=str(root),
        languages=tuple(dict.fromkeys(adapter.language for adapter in matched)),
        manifests=tuple(manifests),
        test_commands=tuple(commands),
        adapter_names=tuple(adapter.name for adapter in matched),
    )


def adapters_for_repo(repo_root: str) -> list[RepoAdapter]:
    root = Path(repo_root)
    matched = [adapter for adapter in ADAPTERS if adapter.matches(root)]
    return matched or [GENERIC_ADAPTER]


def is_test_file(path: str) -> bool:
    norm = _norm(path)
    parts = set(norm.lower().split("/")[:-1])
    name = Path(norm).name
    low = name.lower()
    if parts & TEST_DIR_PARTS:
        return True
    if low.startswith("test_") or low.startswith("test-"):
        return True
    return (
        low.endswith("_test.py")
        or low.endswith("_test.go")
        or low.endswith(".test.js")
        or low.endswith(".test.ts")
        or low.endswith(".test.jsx")
        or low.endswith(".test.tsx")
        or low.endswith(".spec.js")
        or low.endswith(".spec.ts")
        or low.endswith(".spec.jsx")
        or low.endswith(".spec.tsx")
        or low.endswith("test.java")
        or low.endswith("tests.cs")
        or low.endswith("_spec.rb")
    )


def is_source_file(path: str, repo_root: str | None = None) -> bool:
    norm = _norm(path)
    if is_test_file(norm) or is_generated_or_vendor(norm):
        return False
    if repo_root:
        return any(adapter.is_source_file(norm) for adapter in adapters_for_repo(repo_root))
    return Path(norm).suffix.lower() in SOURCE_EXTS


def is_generated_or_vendor(path: str) -> bool:
    norm = _norm(path)
    low = norm.lower()
    parts = set(low.split("/")[:-1])
    name = Path(low).name
    if parts & GENERATED_VENDOR_PARTS:
        return True
    return (
        name.endswith(".lock")
        or name.endswith(".min.js")
        or name.endswith(".map")
        or "generated" in parts
        or name.startswith("generated_")
        or name.endswith(".pb.go")
        or name.endswith(".g.dart")
    )


def is_public_api_surface(path: str) -> bool:
    norm = _norm(path)
    name = Path(norm).name
    return name in {
        "__init__.py",
        "index.ts",
        "index.js",
        "mod.rs",
        "lib.rs",
        "go.mod",
        "Cargo.toml",
        "package.json",
        "pom.xml",
    }


def select_repo_test_command(repo_root: str) -> tuple[list[str], str]:
    profile = detect_repo_profile(repo_root)
    if profile.test_commands:
        command = list(profile.test_commands[0])
        return command, profile.adapter_names[0] if profile.adapter_names else "generic"
    return [], "no_known_test_runner"


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")
