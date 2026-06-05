"""Contract-drift transport helper shared by OpenHands (push) and mini-swe (pull).

Baseline = a FROZEN copy of the session-start graph.db (``<working>.orig``), the
contract callers were written against — taken before the agent edits anything, so
no hook ordering (the L6 reindex fires before the L3 hook) can corrupt it. After an
edit we reindex ONLY the edited file into the working graph.db (idempotent: a no-op
edit short-circuits on the file hash) and diff its post-edit contract against the
frozen original.

Zero test contact: callers are is_test=0 by call-graph construction and the
assertions table is never read; test/fixture files are skipped entirely. No
execution, deterministic, LLM-free, $0.
"""
from __future__ import annotations

import os
import shutil
import sqlite3

from groundtruth._binary import run_incremental_index
from groundtruth.pretask.contract_map import build_drift, snapshot_contract
from groundtruth.pretask.curation_map import _open_ro

# Source extensions GT reasons about; drift on anything else is meaningless.
_SOURCE_EXTS = (
    ".py", ".go", ".js", ".jsx", ".ts", ".tsx", ".rs", ".java",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".rb", ".php",
)
# Path fragments marking a test/fixture file — drift stays OUT of these (zero
# test contact; the agent should be fixing source, not tests).
_TEST_MARKERS = (
    "test_", "_test.", "/tests/", "/test/", "/__tests__/", "conftest.py",
    "/fixtures/", "/testdata/", "spec.",
)


def original_db_path(working_db: str) -> str:
    """The frozen-baseline path for a working graph.db. GT_ORIGINAL_DB overrides;
    default convention is ``<working_db>.orig`` so both scaffolds agree without
    extra env plumbing."""
    return os.environ.get("GT_ORIGINAL_DB", "") or (working_db + ".orig")


def freeze_original(working_db: str, *, force: bool = True) -> bool:
    """Snapshot the session-start graph.db as the drift baseline. Call ONCE at
    session start, BEFORE any edit. Returns True on success."""
    if not working_db or not os.path.exists(working_db):
        return False
    dst = original_db_path(working_db)
    if os.path.exists(dst) and not force:
        return True
    try:
        shutil.copyfile(working_db, dst)
        return True
    except OSError:
        return False


def _is_source_nontest(rel: str) -> bool:
    low = rel.lower()
    if not low.endswith(_SOURCE_EXTS):
        return False
    return not any(m in low for m in _TEST_MARKERS)


def _to_rel(root: str, path: str) -> str:
    """Normalize a (possibly absolute) edited path to the slash-relative form
    stored in nodes.file_path."""
    p = (path or "").replace("\\", "/")
    r = (root or "").replace("\\", "/").rstrip("/")
    if r and p.startswith(r + "/"):
        p = p[len(r) + 1:]
    return p.lstrip("/")


def _func_names_in_file(db: str, rel: str) -> list[str]:
    conn = _open_ro(db)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT name FROM nodes WHERE file_path = ? "
            "AND label IN ('Function','Method')",
            (rel,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [r[0] for r in rows if r and r[0]]


def drift_for_file(root: str, working_db: str, original_db: str, rel: str) -> str:
    """Reindex the edited file into the working db, then diff its post-edit
    contract against the frozen original. The function set is the ORIGINAL's
    (so a removed/renamed function callers depend on is detected)."""
    # Idempotent: if the wrapper already reindexed (OH push), the hash matches and
    # this short-circuits; for mini-swe (pull) it performs the reindex.
    run_incremental_index(root, rel, working_db)
    funcs = _func_names_in_file(original_db, rel)
    if not funcs:
        return ""
    pre = snapshot_contract(original_db, rel, funcs)
    if not pre:
        return ""
    return build_drift(working_db, rel, funcs, pre_snapshot=pre)


def drift_advisory(root: str, working_db: str, modified_files: list[str]) -> str:
    """Combined drift block for all edited source files, or "" (correct-or-quiet).

    Safe no-op when no frozen baseline exists (freeze_original was not called at
    session start) — drift has nothing to diff against. Never raises."""
    if not working_db or not os.path.exists(working_db):
        return ""
    original_db = original_db_path(working_db)
    if not os.path.exists(original_db):
        return ""
    blocks: list[str] = []
    seen: set[str] = set()
    for f in modified_files or []:
        rel = _to_rel(root, f)
        if not rel or rel in seen or not _is_source_nontest(rel):
            continue
        seen.add(rel)
        try:
            d = drift_for_file(root, working_db, original_db, rel)
        except Exception:
            d = ""
        if d:
            blocks.append(d)
    return "\n".join(blocks)
