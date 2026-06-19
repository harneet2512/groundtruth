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


def classify_file(path: str) -> str:
    """Classify a single patched file path."""
    base = (path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base in _LOCKFILE_NAMES or base.endswith(".lock"):
        return "lockfile"
    if any(m in path for m in _GENERATED_MARKERS):
        return "generated"
    if any(path.endswith(ext) for ext in _NOISE_MARKERS):
        return "noise"
    if re.search(r"\.(py|go|rs|js|ts|tsx|jsx|java|rb|c|cpp|h|hpp|cs|kt|swift)$", path, re.I):
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
    for m in DIFF_HEADER.finditer(patch):
        fp = m.group(2)
        if fp in seen:
            continue
        seen.add(fp)
        cls = classify_file(fp)
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
