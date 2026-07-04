"""Host-only TRUE-gold resolver for DeepSWE tasks.

The measurement layer needs the *reference* fix to score localization hit@k. On
the run tapes only the MODEL's submitted patch is stored (``task_truth.patch_hygiene``)
— using it as gold makes the metric self-referential (it measures "did GT predict
where the agent edited", not "where the fix belongs"). This module resolves the
held-out reference solution instead.

Source (LOCAL, no fetch): ``deepswe-bench/tasks/<task>/`` in the Harbor task
format — ``solution/solution.patch`` is the reference fix (never shown to the
agent, never applied at grading), ``tests/test.patch`` is the verifier's test
additions (the LEAK surface — exposed here ONLY so the caller can assert the
brief never surfaced one), ``task.toml`` carries language / base commit / repo.

LEAK BOUNDARY: gold is loaded HOST-side into the metric layer only. It is NEVER
passed to ``generate_v1r_brief`` (that call gets ``gold_files=None``) and NEVER
written into an artifact the agent reads. That is the whole reason gold lives in
a separate module from the delivery path.

Deterministic: pure file reads + patch/TOML parsing, no network, no ordering
dependence (gold-file order follows the patch, which is stable).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

__all__ = [
    "task_id_from_tape_dir",
    "bench_root",
    "gold_patch_for_task",
    "gold_files_for_task",
    "test_patch_files_for_task",
    "task_meta",
]

# Default local bench; override for a different checkout via env.
_DEFAULT_BENCH = Path("D:/Groundtruth/deepswe-bench/tasks")
_TAPE_PREFIX = "deepswe-full-"


def bench_root() -> Path:
    return Path(os.environ.get("GT_DEEPSWE_BENCH_ROOT", str(_DEFAULT_BENCH)))


def task_id_from_tape_dir(name: str) -> str:
    """``deepswe-full-abs-module-cache-flags`` -> ``abs-module-cache-flags``.
    The tape dir carries the run prefix; the bench keys on the bare slug."""
    base = os.path.basename(name.rstrip("/\\"))
    return base[len(_TAPE_PREFIX):] if base.startswith(_TAPE_PREFIX) else base


def _task_dir(task_id: str, root: Path | None = None) -> Path | None:
    d = (root or bench_root()) / task_id_from_tape_dir(task_id)
    return d if d.is_dir() else None


def _files_from_patch(patch: str) -> list[str]:
    """``+++ b/<path>`` targets, minus ``/dev/null``, in patch order (dedup).
    Mirrors ``measure_v1r_localization.gold_files_from_patch`` — a NEW file has
    ``--- /dev/null`` / ``+++ b/<newpath>`` and the new path is kept (it is the
    stratum-D gold)."""
    files: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            p = line[6:].strip()
            if p and p != "/dev/null" and p not in files:
                files.append(p)
    return files


def gold_patch_for_task(task_id: str, root: Path | None = None) -> str | None:
    d = _task_dir(task_id, root)
    if d is None:
        return None
    p = d / "solution" / "solution.patch"
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def gold_files_for_task(task_id: str, root: Path | None = None) -> list[str]:
    """TRUE-gold changed files, or ``[]`` when the task is not resolvable locally
    (caller marks it ``gold=UNKNOWN`` and excludes it from hit@k — never a silent 0)."""
    patch = gold_patch_for_task(task_id, root)
    return _files_from_patch(patch) if patch else []


def test_patch_files_for_task(task_id: str, root: Path | None = None) -> list[str]:
    """Files touched by ``tests/test.patch`` — the VERIFIER additions. Returned
    ONLY so a caller can assert none of them ever appeared in the brief /
    localization output (a FAIL_TO_PASS leak). NEVER gold, NEVER delivered."""
    d = _task_dir(task_id, root)
    if d is None:
        return []
    p = d / "tests" / "test.patch"
    if not p.is_file():
        return []
    return _files_from_patch(p.read_text(encoding="utf-8", errors="replace"))


def task_meta(task_id: str, root: Path | None = None) -> dict:
    """``{language, base_commit, repository_url, task_id}`` from ``task.toml``,
    or ``{}`` when unresolvable. Used to cross-check the tape graph's language."""
    d = _task_dir(task_id, root)
    if d is None:
        return {}
    p = d / "task.toml"
    if not p.is_file():
        return {}
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    meta = data.get("metadata", {})
    return {
        "task_id": meta.get("task_id", task_id_from_tape_dir(task_id)),
        "language": meta.get("language"),
        "base_commit": meta.get("base_commit_hash"),
        "repository_url": meta.get("repository_url"),
    }
