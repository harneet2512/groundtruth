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
    task-owned metadata, so derive from their changed source paths; never silently
    classify an unknown task as Python.
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
        # Third task-owned source: the graded test identifiers. A task whose patches
        # touch only data files (bridgecrewio__checkov-6893: a Terraform-check YAML +
        # .tf fixture) still names its runtime language in FAIL_TO_PASS/PASS_TO_PASS —
        # `tests/.../test_yaml_policies.py::TestYamlPolicies::...` is a Python task by
        # the file the harness will execute. Paths only (the part before `::`), same
        # extension map, and the fail-closed bar is UNCHANGED: no derivable extension
        # anywhere still returns None, never a silent "python".
        for field in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            value = task.get(field)
            items = value if isinstance(value, list) else []
            if isinstance(value, str):
                try:
                    import json as _json
                    parsed = _json.loads(value)
                    items = parsed if isinstance(parsed, list) else []
                except ValueError:
                    items = []
            for item in items:
                if not isinstance(item, str):
                    continue
                path = item.split("::", 1)[0]
                for suffix, language in _EXTENSION_LANGUAGE.items():
                    if path.lower().endswith(suffix):
                        counts[language] += 1
                        break
    if not counts:
        return None
    return sorted(counts, key=lambda language: (-counts[language], language))[0]


__all__ = ["derive_task_language", "normalize_language"]
