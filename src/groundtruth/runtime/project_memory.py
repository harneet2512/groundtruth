"""Deterministic per-repo project memory.

Memory is opt-in and local to the repository. Comparative SWE-bench runs can
leave it disabled by simply not calling this module.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def build_project_memory(repo_root: str, *, log_dir: str | None = None, task_id: str = "unknown") -> dict[str, Any]:
    root = Path(repo_root)
    memory = {
        "repo": _repo_identity(root),
        "package_manager": _package_manager(root),
        "test_layout": _test_layout(root),
        "changelog_convention": _first_existing(root, ["CHANGELOG.md", "CHANGES.rst", "docs/changelog.rst"]),
        "generated_vendor_patterns": _generated_vendor_patterns(root),
        "common_side_file_rules": _side_file_rules(root),
        "slow_flaky_test_commands": [],
        "recurring_cochange_clusters": _cochange_clusters(root),
        "enabled": True,
    }
    if log_dir is not None:
        from groundtruth.runtime.telemetry import append_block

        append_block("gt_project_memory", memory, log_dir=log_dir, task_id=task_id)
    return memory


def write_project_memory(repo_root: str, *, output: str | None = None) -> str | None:
    root = Path(repo_root)
    memory = build_project_memory(repo_root)
    target = Path(output) if output else root / ".groundtruth" / "project_memory.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(memory, indent=2, sort_keys=True), encoding="utf-8")
        return str(target)
    except (OSError, TypeError, ValueError):
        return None


def _repo_identity(root: Path) -> dict[str, str]:
    remote = _git(root, ["config", "--get", "remote.origin.url"])
    name = root.name
    if remote:
        cleaned = remote.rstrip("/").removesuffix(".git")
        name = cleaned.rsplit("/", 1)[-1] or name
    return {"root_name": root.name, "remote": remote, "name": name}


def _package_manager(root: Path) -> str:
    checks = [
        ("pnpm", "pnpm-lock.yaml"),
        ("npm", "package-lock.json"),
        ("yarn", "yarn.lock"),
        ("cargo", "Cargo.toml"),
        ("go", "go.mod"),
        ("poetry", "poetry.lock"),
        ("pip", "requirements.txt"),
        ("pytest", "pyproject.toml"),
    ]
    for manager, marker in checks:
        if (root / marker).exists():
            return manager
    return "unknown"


def _test_layout(root: Path) -> list[str]:
    layouts: list[str] = []
    for candidate in ["tests", "test", "__tests__", "spec", "src/test"]:
        if (root / candidate).is_dir():
            layouts.append(candidate)
    return layouts


def _generated_vendor_patterns(root: Path) -> list[str]:
    patterns = ["vendor/", "node_modules/", "dist/", "build/", "target/"]
    found = [pattern for pattern in patterns if (root / pattern.rstrip("/")).exists()]
    for name in ["package-lock.json", "pnpm-lock.yaml", "Cargo.lock", "go.sum"]:
        if (root / name).exists():
            found.append(name)
    return found


def _side_file_rules(root: Path) -> list[dict[str, str]]:
    rules: list[dict[str, str]] = []
    if _first_existing(root, ["CHANGELOG.md", "CHANGES.rst", "docs/changelog.rst"]):
        rules.append({"kind": "public_api_change", "side_file": "changelog"})
    if _first_existing(root, ["py.typed", "types", "typings"]):
        rules.append({"kind": "typing_change", "side_file": "typing_surface"})
    if _first_existing(root, ["__init__.py", "src/__init__.py", "index.ts", "src/index.ts"]):
        rules.append({"kind": "export_change", "side_file": "export_surface"})
    return rules


def _cochange_clusters(root: Path) -> list[dict[str, Any]]:
    raw = _git(root, ["log", "-n", "100", "--name-only", "--pretty=format:__GT__%H"])
    if not raw:
        return []
    clusters: dict[tuple[str, ...], int] = {}
    current: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("__GT__"):
            _add_cluster(clusters, current)
            current = []
        else:
            current.append(line.replace("\\", "/"))
    _add_cluster(clusters, current)
    ranked = sorted(clusters.items(), key=lambda item: (-item[1], item[0]))
    return [{"files": list(files), "count": count} for files, count in ranked[:10]]


def _add_cluster(clusters: dict[tuple[str, ...], int], files: list[str]) -> None:
    clean = tuple(sorted(f for f in files if f and not f.endswith(".lock"))[:8])
    if len(clean) >= 2:
        clusters[clean] = clusters.get(clean, 0) + 1


def _first_existing(root: Path, names: list[str]) -> str:
    for name in names:
        if (root / name).exists():
            return name
    return ""


def _git(root: Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""
