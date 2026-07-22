"""Authoritative task-language derivation for metadata-only SWE task manifests."""

from __future__ import annotations

import re
from collections import Counter
from typing import Mapping

_EXTENSION_LANGUAGE = {
    ".py": "python", ".pyi": "python", ".go": "go", ".js": "js",
    ".jsx": "js", ".ts": "ts", ".tsx": "ts", ".rs": "rust",
    ".java": "java",
}
_DIFF_PATH = re.compile(r"^diff --git a/(.+?) b/", re.MULTILINE)

# Test-harness -> language. When a task's patch touches only non-source files (e.g. a
# Python repo whose fix lives in YAML/config), the recorded test invocation still reveals
# the repo language. ``log_parser``/``test_cmds`` are task-owned metadata (the exact runner),
# so a recognized harness is a principled, general signal — NOT a blind Python default.
_TEST_HARNESS_LANGUAGE = (
    ("pytest", "python"), ("unittest", "python"), ("nox", "python"), ("tox", "python"),
    ("go test", "go"), ("gotest", "go"),
    ("jest", "js"), ("mocha", "js"), ("vitest", "js"),
    ("cargo", "rust"),
    ("gradle", "java"), ("maven", "java"), ("mvn ", "java"), ("junit", "java"),
)


def _language_from_test_harness(task: Mapping[str, object]) -> str | None:
    """Derive language from the task's test invocation when patch paths are language-silent."""
    fields: list[str] = []
    parser = task.get("log_parser")
    if isinstance(parser, str):
        fields.append(parser.lower())
    cmds = task.get("test_cmds")
    if isinstance(cmds, str):
        fields.append(cmds.lower())
    elif isinstance(cmds, (list, tuple)):
        fields.extend(str(command).lower() for command in cmds)
    blob = " ".join(fields)
    for token, language in _TEST_HARNESS_LANGUAGE:
        if token in blob:
            return language
    return None


def normalize_language(value: object) -> str | None:
    """Normalize an explicitly declared language, rejecting unknown values."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    aliases = {"javascript": "js", "typescript": "ts"}
    value = aliases.get(value, value)
    return value if value in set(_EXTENSION_LANGUAGE.values()) | {"python", "go", "js", "ts", "rust", "java"} else None


def derive_task_language(task: Mapping[str, object]) -> str | None:
    """Return the task's declared language or deterministic dominant patch language.

    Live-Lite's release records omit ``repo_language``.  The patch/test patch are
    task-owned metadata, so derive from their changed source paths; a config-only patch
    then falls back to the task's own test harness (log_parser/test_cmds), and only a
    genuinely signalless task returns ``None`` — never a blind Python default.
    """
    for key in ("repo_language", "language"):
        declared = normalize_language(task.get(key))
        if declared:
            return declared
    counts: Counter[str] = Counter()
    for field in ("patch", "test_patch"):
        value = task.get(field)
        if not isinstance(value, str):
            continue
        for path in _DIFF_PATH.findall(value):
            path = path.split("\t", 1)[0]
            for suffix, language in _EXTENSION_LANGUAGE.items():
                if path.lower().endswith(suffix):
                    counts[language] += 1
                    break
    if not counts:
        # Patch touched no recognized source file (e.g. a Python repo whose fix is in
        # YAML/config). Fall back to the task's own test-harness signal; stay None only
        # when even that is absent.
        return _language_from_test_harness(task)
    return sorted(counts, key=lambda language: (-counts[language], language))[0]


__all__ = ["derive_task_language", "normalize_language"]
