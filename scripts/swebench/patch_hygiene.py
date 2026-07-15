#!/usr/bin/env python3
"""Patch hygiene classification for submission packaging."""
from __future__ import annotations

import re
from typing import Any

DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)

_LOCKFILE_NAMES: frozenset[str] = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
    "go.sum", "poetry.lock", "Gemfile.lock", "composer.lock",
})
_GENERATED_MARKERS: tuple[str, ...] = (
    ".pb.go", ".pb.gw.go", "_pb2.py", "_pb2_grpc.py", ".generated.",
    "_generated.go", ".g.dart", ".freezed.dart", "zz_generated",
)
_NOISE_MARKERS: tuple[str, ...] = (
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
    ".svg", ".png", ".jpg", ".ico",
)

_EXECUTABLE_YAML_PATH_RE = re.compile(
    r"(?:^|/)(?:\.github/(?:workflows|actions)/|\.circleci/|"
    r"(?:k8s|kubernetes|manifests|charts|playbooks|roles)/)|"
    r"(?:^|/)(?:\.gitlab-ci|docker-compose|compose)(?:[.-].*)?\.ya?ml$",
    re.I,
)
_YAML_KEY_RE = re.compile(
    r"^[ +\-]*([A-Za-z][A-Za-z0-9_-]*)\s*:",
    re.MULTILINE,
)


def _yaml_has_executable_structure(patch_section: str) -> bool:
    """Recognize operational YAML by a structural key pair, not one vague word."""
    keys = {match.group(1).lower() for match in _YAML_KEY_RE.finditer(patch_section or "")}
    return bool(
        {"apiversion", "kind"} <= keys
        or ("jobs" in keys and keys.intersection({"steps", "runs-on", "uses", "run"}))
        or ("services" in keys and keys.intersection({"image", "build", "command"}))
        or ({"stages", "script"} <= keys)
        or (
            {"definition", "cond_type"} <= keys
            and keys.intersection({"operator", "resource_types"})
        )
    )


def _is_yaml_fixture_path(path: str) -> bool:
    """Keep expected/snapshot YAML test data out of production changed paths."""
    normalized = path.lower().strip("/")
    parts = normalized.split("/")
    base = parts[-1] if parts else ""
    under_tests = any(part in {"test", "tests"} for part in parts[:-1])
    fixture_tree = any(part in {"fixture", "fixtures", "snapshot", "snapshots", "golden"}
                       for part in parts[:-1])
    expected_name = base.startswith(("expected.", "expected_", "snapshot.", "golden."))
    return fixture_tree or (under_tests and expected_name)


def classify_file(path: str, patch_section: str = "") -> str:
    """Classify a single patched file path."""
    normalized = (path or "").replace("\\", "/")
    base = normalized.rsplit("/", 1)[-1].lower()
    if base in _LOCKFILE_NAMES or base.endswith(".lock"):
        return "lockfile"
    if any(m in normalized for m in _GENERATED_MARKERS):
        return "generated"
    if normalized.lower().endswith((".yaml", ".yml")) and _is_yaml_fixture_path(normalized):
        return "noise"
    if normalized.lower().endswith((".yaml", ".yml")) and (
        _EXECUTABLE_YAML_PATH_RE.search(normalized)
        or _yaml_has_executable_structure(patch_section)
    ):
        return "source_fix"
    if any(normalized.lower().endswith(ext) for ext in _NOISE_MARKERS):
        return "noise"
    if re.search(
        r"\.(py|pyi|go|rs|js|ts|tsx|jsx|java|rb|c|cpp|h|hpp|cs|kt|swift)$",
        normalized,
        re.I,
    ):
        return "source_fix"
    return "noise"


def classify_patch(patch: str) -> dict[str, Any]:
    """Classify all files in a unified diff patch."""
    if not (patch or "").strip():
        return {
            "classification": "missing_model_patch",
            "files": [],
            "summary": {"source_fix": 0, "lockfile": 0, "generated": 0, "noise": 0},
        }
    files: list[dict[str, str]] = []
    summary: dict[str, int] = {"source_fix": 0, "lockfile": 0, "generated": 0, "noise": 0}
    seen: set[str] = set()
    matches = list(DIFF_HEADER.finditer(patch))
    for index, m in enumerate(matches):
        fp = m.group(2)
        if fp in seen:
            continue
        seen.add(fp)
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(patch)
        cls = classify_file(fp, patch[m.start():section_end])
        files.append({"path": fp, "class": cls})
        if cls in summary:
            summary[cls] += 1
    primary = "source_fix" if summary["source_fix"] else (
        "lockfile" if summary["lockfile"] else (
            "generated" if summary["generated"] else (
                "noise" if summary["noise"] else "missing_model_patch"
            )
        )
    )
    return {"classification": primary, "files": files, "summary": summary}
