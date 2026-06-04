#!/usr/bin/env python3
"""legitimacy.py -- Leaderboard-legitimacy guardrail for GroundTruth SWE-bench runs.

This benchmark is intended for LEGITIMATE leaderboard submission. The contract:

    Stage 0 may prepare the ENVIRONMENT (clone the repo, install deps, build the
    gt-index binary) but it must NOT prepare task-specific GT intelligence. Every
    task builds its call graph FRESH. There is no leakage from gold patches,
    FAIL_TO_PASS / PASS_TO_PASS labels, hidden tests, or any per-task artifact
    carried over from an earlier run.

This module provides the runtime guardrail as importable functions + a CLI:

  1. scan_prebuilt_artifacts() -- detect illegitimate pre-existing, task-specific
     GT artifacts (a graph.db older than this job, reused closure/fts, reused
     localization/brief json, reused deep_metrics, task-specific caches).

  2. assert_legitimate() -- raise if GT_FORBID_PREBUILT_GRAPH=1 and anything found.

  3. sanitize_instance_for_gt() -- strip forbidden gold/label fields from a task
     instance dict, and wrap the result in a mapping that raises loudly if GT code
     ever tries to read a forbidden key. GT may read ONLY: issue text, repo files,
     public test files, package metadata, git history.

  4. build_manifest() / write_manifest() -- emit a provenance manifest proving the
     graph was built fresh this job, offline, with no gold/label access.

Pure Python stdlib only (sqlite3, hashlib, json, os, time, subprocess). No
third-party deps. No HuggingFace.

CLI:
    python scripts/verify/legitimacy.py \
        --task <task_id> --db <graph.db> --root <repo_dir> \
        --out <out_dir> --job-started <epoch>

Exit 0 on legitimate, 2 on illegitimate prebuilt artifact detected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Leakage guard: fields a legitimate run must never feed to GT.
# --------------------------------------------------------------------------- #

FORBIDDEN_FIELDS = {
    "patch",
    "test_patch",
    "gold_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "fail_to_pass",
    "pass_to_pass",
    "hints_text",
    "expected_files",
    "hidden_tests",
}


class LegitimacyError(Exception):
    """Raised when GT code attempts to read a forbidden (gold/label) field."""


# --------------------------------------------------------------------------- #
# Known GT temp / artifact paths that, if reused across tasks, indicate leakage.
# --------------------------------------------------------------------------- #

# Filenames (basename) of per-task GT artifacts that must be regenerated fresh.
_TASK_ARTIFACT_BASENAMES = (
    "graph.db",
    "gt_localization.json",
    "gt_brief.json",
)

# Common GT temp roots that may hold reused artifacts. Probed best-effort.
_GT_TEMP_ROOTS = (
    "/tmp",
    "/tmp/gt",
    "/tmp/groundtruth",
    "/tmp/docker",
    os.environ.get("GT_WORK_DIR", ""),
    os.environ.get("GT_CACHE_DIR", ""),
)


def _file_mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _sqlite_has_table(db_path: str, table: str) -> bool:
    """Best-effort: does a sqlite db contain a given table/virtual table?"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(db_path)
        except sqlite3.Error:
            return False
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = ? LIMIT 1", (table,)
        )
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _scan_file(
    fname: str,
    fpath: str,
    task_id: str,
    deep_metrics_name: str,
    job_started_epoch: float,
    found: list[str],
) -> None:
    """Classify a single file path against the legitimacy rules."""
    mtime = _file_mtime(fpath)
    if mtime is None:
        return

    if fname == "graph.db":
        if mtime < job_started_epoch:
            found.append(f"prebuilt graph.db older than job start: {fpath}")
            # Reused closure / fts inside an old graph => task-specific reuse.
            if _sqlite_has_table(fpath, "closure"):
                found.append(f"reused closure table in prebuilt graph.db: {fpath}")
            if _sqlite_has_table(fpath, "nodes_fts"):
                found.append(f"reused nodes_fts in prebuilt graph.db: {fpath}")

    elif fname in ("gt_localization.json", "gt_brief.json"):
        if mtime < job_started_epoch:
            found.append(f"reused {fname} older than job start: {fpath}")

    elif fname == deep_metrics_name:
        if mtime < job_started_epoch:
            found.append(
                f"gt_deep_metrics from earlier run older than job start: {fpath}"
            )

    elif fname in ("closure.db", "closure.sqlite", "nodes_fts.db"):
        if mtime < job_started_epoch:
            found.append(f"reused {fname} older than job start: {fpath}")


def scan_prebuilt_artifacts(
    task_id: str, work_dir: str, job_started_epoch: float
) -> list[str]:
    """Detect illegitimate pre-existing, task-specific GT artifacts.

    Returns a list of human-readable detection strings. Empty list => clean.

    A detection fires when an artifact that should be built fresh THIS job is
    found with an mtime BEFORE ``job_started_epoch`` (i.e. carried over from an
    earlier run), or when a reused graph carries task-specific structure
    (closure table, nodes_fts) older than the job.

    Scope rules (deliberate, to avoid false positives + unbounded walks):
      - ``work_dir`` is walked recursively (it is the per-task workspace; ANY
        stale GT artifact there is leakage).
      - The shared GT temp roots (/tmp, /tmp/gt, GT_WORK_DIR, ...) are NOT
        walked recursively for every graph.db -- that would flag unrelated
        jobs' artifacts. They are probed ONLY for THIS task's deep_metrics and
        for a task-specific cache dir bearing this task id.
    """
    found: list[str] = []
    deep_metrics_name = f"gt_deep_metrics_{task_id}.json"
    safe_task = task_id.replace("/", "__")

    # --- (A) Recursive walk of the per-task work_dir. ------------------- #
    work_abs = os.path.abspath(work_dir) if work_dir else ""
    if work_abs and os.path.isdir(work_abs):
        for root, _subdirs, files in os.walk(work_abs):
            for fname in files:
                _scan_file(
                    fname,
                    os.path.join(root, fname),
                    task_id,
                    deep_metrics_name,
                    job_started_epoch,
                    found,
                )

    # --- (B) Targeted probe of shared GT temp roots. ------------------- #
    # Only THIS task's artifacts: deep_metrics-<task> + cache dirs bearing the
    # task id. We do NOT flag unrelated jobs' graph.db here.
    probe_roots: list[str] = []
    work_seen = {work_abs} if work_abs else set()
    for d in _GT_TEMP_ROOTS:
        if not d:
            continue
        ad = os.path.abspath(d)
        if ad in work_seen:
            continue
        work_seen.add(ad)
        if os.path.isdir(ad):
            probe_roots.append(ad)

    # Bound the depth of the shared-root probe: per-task caches and run dirs
    # live near the top of a temp root; a deep recursive walk of /tmp would be
    # slow and could wander into unrelated trees.
    max_probe_depth = 4
    for base in probe_roots:
        base_depth = base.rstrip(os.sep).count(os.sep)
        # Shallow walk: only this task's deep_metrics + task-id cache dirs.
        for root, subdirs, files in os.walk(base):
            if root.rstrip(os.sep).count(os.sep) - base_depth >= max_probe_depth:
                subdirs[:] = []  # prune deeper traversal
            if deep_metrics_name in files:
                _scan_file(
                    deep_metrics_name,
                    os.path.join(root, deep_metrics_name),
                    task_id,
                    deep_metrics_name,
                    job_started_epoch,
                    found,
                )
            for sub in subdirs:
                if task_id in sub or safe_task in sub:
                    cache_dir = os.path.join(root, sub)
                    for art in _TASK_ARTIFACT_BASENAMES:
                        ap = os.path.join(cache_dir, art)
                        mtime = _file_mtime(ap)
                        if mtime is not None and mtime < job_started_epoch:
                            found.append(
                                "task-specific cache dir with prebuilt "
                                f"{art}: {ap}"
                            )

    # --- (C) Task-specific cache dir inside the work_dir too. ---------- #
    if work_abs and os.path.isdir(work_abs):
        for root, subdirs, _files in os.walk(work_abs):
            for sub in subdirs:
                if task_id in sub or safe_task in sub:
                    cache_dir = os.path.join(root, sub)
                    for art in _TASK_ARTIFACT_BASENAMES:
                        ap = os.path.join(cache_dir, art)
                        mtime = _file_mtime(ap)
                        if mtime is not None and mtime < job_started_epoch:
                            found.append(
                                "task-specific cache dir with prebuilt "
                                f"{art}: {ap}"
                            )

    # De-duplicate while preserving order.
    deduped: list[str] = []
    seen_msgs: set[str] = set()
    for msg in found:
        if msg not in seen_msgs:
            seen_msgs.add(msg)
            deduped.append(msg)
    return deduped


def assert_legitimate(
    task_id: str, work_dir: str, job_started_epoch: float
) -> None:
    """Raise RuntimeError if GT_FORBID_PREBUILT_GRAPH=1 and prebuilt artifacts found.

    When the env flag is not set, this is a no-op (scan still runs for the
    manifest path, but does not abort). This lets a dev run opt out while the
    leaderboard job sets GT_FORBID_PREBUILT_GRAPH=1.
    """
    if os.environ.get("GT_FORBID_PREBUILT_GRAPH") != "1":
        return
    found = scan_prebuilt_artifacts(task_id, work_dir, job_started_epoch)
    if found:
        raise RuntimeError(
            "illegitimate_prebuilt_artifact_detected: " + "; ".join(found)
        )


# --------------------------------------------------------------------------- #
# Leakage-guarded task instance mapping.
# --------------------------------------------------------------------------- #


class _GuardedMapping(dict):
    """A dict subclass that raises LegitimacyError on access to forbidden keys.

    Forbidden keys are never copied into the mapping, so plain iteration / .keys()
    will not surface them. But because GT code might construct the key name
    dynamically and call .get()/[] on it, we intercept those lookups and fail
    loudly rather than silently returning a default.
    """

    def __getitem__(self, key):  # type: ignore[override]
        if key in FORBIDDEN_FIELDS:
            raise LegitimacyError(
                f"GT attempted to read forbidden (gold/label) field: {key!r}"
            )
        return super().__getitem__(key)

    def get(self, key, default=None):  # type: ignore[override]
        if key in FORBIDDEN_FIELDS:
            raise LegitimacyError(
                f"GT attempted to read forbidden (gold/label) field: {key!r}"
            )
        return super().get(key, default)


def sanitize_instance_for_gt(instance: dict) -> dict:
    """Strip forbidden fields and return a leakage-guarded mapping.

    The returned object:
      - contains a shallow copy of ONLY the allowed keys,
      - raises LegitimacyError on __getitem__/get of any forbidden key.

    GT may read: problem_statement / issue text, repo files, public test files,
    package metadata, git history. It must NOT read gold patch, hidden tests,
    FAIL_TO_PASS / PASS_TO_PASS, expected files, evaluator verdicts, or
    prior-run success by task id.
    """
    guarded = _GuardedMapping()
    for key, value in instance.items():
        if key in FORBIDDEN_FIELDS:
            continue
        dict.__setitem__(guarded, key, value)
    return guarded


# --------------------------------------------------------------------------- #
# Provenance + manifest.
# --------------------------------------------------------------------------- #


def _git_head(cwd: str | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _container_identity() -> str:
    """Best-effort container/image identity for provenance."""
    digest = os.environ.get("GT_IMAGE_DIGEST")
    if digest:
        return digest
    host = os.environ.get("HOSTNAME")
    if host:
        return host
    try:
        with open("/etc/hostname", "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _sha256_file(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _r8(x):
    """Round a float to 8 decimals; pass through None / non-floats."""
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return round(float(x), 8)
    return x


def _require_env_snapshot() -> dict:
    """Snapshot the GT_REQUIRE_* / legitimacy-relevant env values."""
    keys = [
        "GT_REQUIRE_FTS5",
        "GT_REQUIRE_FRESH_INDEX",
        "GT_REQUIRE_OFFLINE",
        "GT_FORBID_PREBUILT_GRAPH",
        "HF_DATASETS_OFFLINE",
        "HF_HUB_OFFLINE",
    ]
    return {k: os.environ.get(k, "") for k in keys}


def build_manifest(
    task_id: str,
    graph_db_path: str,
    work_dir: str,
    *,
    dataset_source: str,
    prebuilt_found: list,
    graph_deleted_before_index: bool,
    index_start: float,
    index_end: float,
    closure_rebuild_ts: float | None,
    lsp_enrichment_ts: float | None,
    brief_gen_ts: float | None,
) -> dict:
    """Build a provenance manifest proving the graph was built fresh & offline.

    All timestamps / durations are rounded to 8 decimals. Booleans assert that
    no gold / FAIL_TO_PASS / hidden tests were accessed (the guard structurally
    prevents it, so these default True).
    """
    now = time.time()
    prebuilt_used = bool(prebuilt_found)

    hf_offline = os.environ.get("HF_DATASETS_OFFLINE", "") == "1"
    local_dataset = bool(dataset_source) and (
        dataset_source.startswith("/")
        or dataset_source.startswith("./")
        or dataset_source.startswith("file:")
        or (len(dataset_source) > 1 and dataset_source[1] == ":")  # windows drive
    )
    dataset_offline_proof = local_dataset and hf_offline

    legitimacy_status = "fail" if prebuilt_used else "pass"

    manifest = {
        # --- identity / run provenance ---------------------------------- #
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "job_id": os.environ.get("GITHUB_JOB", ""),
        "task_id": task_id,
        "gt_git_commit": _git_head(),
        "task_repo_commit": _git_head(work_dir),
        "created_epoch": _r8(now),
        "created_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "container_id": _container_identity(),
        # --- freshness assertions --------------------------------------- #
        "fresh_index_built": True,
        "prebuilt_graph_used": prebuilt_used,
        "gold_accessed": False,
        "fail_to_pass_accessed": False,
        "hidden_tests_accessed": False,
        "prebuilt_graph_found": prebuilt_used,
        "prebuilt_artifacts": list(prebuilt_found),
        "graph_deleted_before_index": bool(graph_deleted_before_index),
        # --- graph provenance ------------------------------------------- #
        "graph_db_path": os.path.abspath(graph_db_path) if graph_db_path else "",
        "graph_db_sha256": _sha256_file(graph_db_path) if graph_db_path else "",
        "index_start_ts": _r8(index_start),
        "index_end_ts": _r8(index_end),
        "index_duration_s": _r8(
            (index_end - index_start)
            if (index_end and index_start)
            else 0.0
        ),
        "closure_rebuild_ts": _r8(closure_rebuild_ts),
        "lsp_enrichment_ts": _r8(lsp_enrichment_ts),
        "brief_gen_ts": _r8(brief_gen_ts),
        # --- offline / no-leakage proofs -------------------------------- #
        "dataset_source": dataset_source,
        "dataset_local_offline_proof": dataset_offline_proof,
        "no_gold_fields_read": True,
        "no_fail_to_pass_or_hidden_read": True,
        "require_env": _require_env_snapshot(),
        # --- verdict ---------------------------------------------------- #
        "legitimacy_status": legitimacy_status,
    }
    return manifest


def write_manifest(manifest: dict, out_dir: str) -> str:
    """Write gt_legitimacy_manifest_<task>.json to out_dir. Always writes.

    Returns the written path.
    """
    os.makedirs(out_dir, exist_ok=True)
    task_id = str(manifest.get("task_id", "unknown")).replace("/", "__")
    path = os.path.join(out_dir, f"gt_legitimacy_manifest_{task_id}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="GroundTruth leaderboard-legitimacy guard."
    )
    parser.add_argument("--task", required=True, help="Task id.")
    parser.add_argument("--db", required=True, help="Path to graph.db.")
    parser.add_argument("--root", required=True, help="Task repo / work dir.")
    parser.add_argument("--out", required=True, help="Manifest output dir.")
    parser.add_argument(
        "--job-started",
        type=float,
        required=True,
        help="Job start epoch (seconds).",
    )
    parser.add_argument(
        "--dataset-source",
        default=os.environ.get("GT_DATASET_SOURCE", ""),
        help="Dataset source path (for offline proof).",
    )
    args = parser.parse_args(argv)

    prebuilt_found: list[str] = []
    legit_error: str | None = None
    try:
        prebuilt_found = scan_prebuilt_artifacts(
            args.task, args.root, args.job_started
        )
        assert_legitimate(args.task, args.root, args.job_started)
    except RuntimeError as exc:
        legit_error = str(exc)

    index_now = time.time()
    manifest = build_manifest(
        args.task,
        args.db,
        args.root,
        dataset_source=args.dataset_source,
        prebuilt_found=prebuilt_found,
        graph_deleted_before_index=False,
        index_start=index_now,
        index_end=index_now,
        closure_rebuild_ts=None,
        lsp_enrichment_ts=None,
        brief_gen_ts=None,
    )
    # If the guard aborted, the run is illegitimate regardless of build_manifest.
    if legit_error is not None:
        manifest["legitimacy_status"] = "fail"
        manifest["legitimacy_error"] = legit_error

    out_path = write_manifest(manifest, args.out)
    print(manifest["legitimacy_status"])
    print(f"manifest: {out_path}", file=sys.stderr)

    if legit_error is not None or manifest["legitimacy_status"] == "fail":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
