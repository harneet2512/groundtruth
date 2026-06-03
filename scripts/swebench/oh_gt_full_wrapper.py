#!/usr/bin/env python3
"""OpenHands GT full-potential wrapper.

This module keeps the OpenHands-specific GT integration separate from the
SWE-agent track-4 path.  The top-level ``main`` patches the OpenHands SWE-bench
runner when that runner is importable, while the small functions below are
deliberately testable with fake runtimes and fake OpenHands action objects.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Add src to path for shared config imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
from groundtruth.config.evidence_markers import has_gt_evidence, L3_MARKERS
from groundtruth.config.evidence_markers import passes_relevance_gate, identifier_tokens

import cost_tracking  # noqa: F401

# Core module wiring (observation-only, no behavioral changes)
try:
    from groundtruth.runtime.sanitizer import is_hidden_line as _core_is_hidden_line
    from groundtruth.runtime.sanitizer import clip_balanced as _core_clip_balanced
    from groundtruth.runtime.sanitizer import sanitize_evidence_block as _core_sanitize_block
    from groundtruth.runtime.sanitizer import join_without_glue as _core_join_no_glue
    _SANITIZER_AVAILABLE = True
except ImportError:
    _SANITIZER_AVAILABLE = False

    def _core_clip_balanced(text, max_len=None):
        # Fallback: never emit an obviously-unterminated tail. Best-effort —
        # keep up to max_len chars and drop a trailing fragment after the last
        # whitespace if a quote/paren is left open.
        s = (text or "").strip()
        if max_len is not None and len(s) > max_len:
            s = s[:max_len].rsplit(" ", 1)[0]
        if s.count('"') % 2 or s.count("'") % 2 or s.count("(") != s.count(")"):
            s = s.rsplit(" ", 1)[0] if " " in s else ""
        return s.rstrip(" ,").rstrip()

    def _core_sanitize_block(text, max_chars=None):
        # Fallback Safe Renderer: drop hidden [GT_*] lines, cap at a line
        # boundary (never a raw byte slice), suppress if nothing fits.
        if not text or not str(text).strip():
            return ""
        lines = [ln for ln in str(text).split("\n") if not ln.strip().startswith("[GT_")]
        block = "\n".join(lines).rstrip()
        if max_chars is not None and len(block) > max_chars:
            out, n = [], 0
            for ln in block.split("\n"):
                if n + len(ln) + 1 > max_chars:
                    break
                out.append(ln)
                n += len(ln) + 1
            block = ("\n".join(out) + "\n…") if out else ""
        return block

    def _core_join_no_glue(left, right):
        left, right = str(left or ""), str(right or "")
        if not left:
            return right
        if not right:
            return left
        if left.endswith("\n") or right.startswith("\n"):
            return left + right
        return left + "\n" + right

try:
    from groundtruth.runtime.ledger import Ledger, SignalOutcome
    _LEDGER_AVAILABLE = True
except ImportError:
    _LEDGER_AVAILABLE = False


_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
_SRC_DIR = _REPO_ROOT / "src"
_DEFAULT_GT_INDEX = os.environ.get("GT_INDEX_BINARY", "/tmp/gt-index-linux")
_TOOL_ROOT = _REPO_ROOT / "tools" / "sweagent"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

WORKSPACE_ROOT = "/workspace"
GRAPH_DB = "/tmp/gt_index.db"
GT_TOOLS_DIR = "/tmp/gt_tools"


def _recall_edited_fn_names(diff_text: str) -> set[str]:
    """Extract edited function names from a unified-diff text.

    Mirrors the obligation-check regex (lines ~5046-5052): matches
    ``def <name>`` in @@ hunk headers and in added/removed lines so the
    [RECALL] gate can anchor on the function(s) the agent just edited.
    Pure / no I/O — safe to call at the emission site without a NameError.
    """
    fns: set[str] = set()
    for _dl in (diff_text or "").splitlines():
        _hm = re.match(r"^@@.*@@\s+(?:async\s+)?def\s+(\w+)", _dl)
        if _hm:
            fns.add(_hm.group(1))
        if _dl.startswith(("+", "-")) and not _dl.startswith(("+++", "---")):
            _dm = re.search(r"(?:async\s+)?def\s+(\w+)", _dl)
            if _dm:
                fns.add(_dm.group(1))
    return fns


def _recall_should_emit(
    cached_evidence: str,
    edited_fns: set[str] | None,
    issue_terms: set[str] | None,
) -> bool:
    """Decide whether a cached [RECALL] block is relevant enough to prepend.

    Correct-or-quiet with anti-over-suppression:

    * Build a relevance anchor = (issue terms) UNION (identifier tokens of every
      edited function name).
    * If we HAVE at least one anchor token, render only when the cached evidence
      overlaps it (``passes_relevance_gate``); otherwise suppress the stale,
      unrelated recall (the observed ``progress_write`` leak while ``set_fields``
      was edited).
    * If we have NO anchor at all (weak-signal task — no edited-fn name and no
      issue terms), we cannot judge relevance, so we SUPPRESS the cross-function
      recall to match ``passes_relevance_gate`` (no-anchor => drop, "stay silent
      rather than guess"). A stale per-file dump emitted against an unknown edit
      is unkeyed noise that can misdirect the agent (wrong front-loaded context
      lowers resolution — SWE-PRM, NeurIPS 2025); withholding it is the lesser
      harm (correct-or-quiet, .claude/CLAUDE.md pillar 3).

    Empty cached evidence never emits. Pure function — no I/O.
    """
    if not (cached_evidence or "").strip():
        return False
    anchor: set[str] = {t for t in (issue_terms or set()) if t}
    for _efn in edited_fns or set():
        anchor |= identifier_tokens(_efn)
    if not anchor:
        # No relevance anchor available — stay silent rather than guess
        # (parity with config.evidence_markers.passes_relevance_gate).
        return False
    return passes_relevance_gate(cached_evidence, anchor, None)


# L4a auto-query RETIRED (2026-05-28). L3b post-view now subsumes it:
# L3b delivers Contract pillar (always-fire) + verified categorical callers
# + ego on every first source read, issue-ranked. L4a's only non-overlapping
# value (issue-keyword symbol ranking) is already in L3b's Contract ordering.
# Running both duplicated the cross-file caller summary on every first read
# (context bloat — research: less is more). One hook owns the first read;
# it is the richer one (L3b). Flip to True to re-enable L4a (reversible).
_L4A_AUTO_QUERY_ENABLED = False

# Prefixes that must never appear in agent-visible observations.
# These are internal diagnostics — allowed in wrapper logs (stderr) only.
_HIDDEN_PREFIXES = ("[GT_META]", "[GT_STATUS]", "[GT_CONFIG]", "[GT_TRACE]", "[GT_DELIVERY]", "[GT_COST]", "[GT_PAYLOAD]", "[GT_LLM_CONFIG]", "[GT_CURATION]")


def _is_hidden_line(line: str) -> bool:
    """True if line starts with a hidden diagnostic prefix."""
    s = line.strip()
    return any(s.startswith(p) for p in _HIDDEN_PREFIXES)


def _emit_curation_log(config) -> None:
    """Consensus curation narrowing — shrink the candidate set per band and emit a
    ``[GT_CURATION]`` proof line so the shrink is reconstructable from output.jsonl.

    early (0-5) full prior -> mid (5-10) drop visited-and-not-edited -> late (10+)
    keep only edit-connected. Diagnostic only (hidden prefix, never shown to the
    agent). Wrapped so a tracking failure can never break the dispatch.
    """
    try:
        from groundtruth.router.curation import CurationTracker

        tr = getattr(config, "_curation_tracker", None)
        if tr is None:
            cands = set(getattr(config, "brief_candidates", None) or [])
            if not cands:
                return

            def _neigh(f, _cfg=config):
                try:
                    return {s["file"] for s in _detect_scope(f, _cfg, None)}
                except Exception:  # noqa: BLE001
                    return set()

            tr = CurationTracker(candidates=cands, neighbors_of=_neigh)
            config._curation_tracker = tr
        visited = set(getattr(config, "viewed_files", None) or [])
        edited = {
            f for f in (getattr(config, "edited_files", None) or [])
            if not _is_scaffolding_path(f)
        }
        tr.narrow(config.action_count, visited, edited)
        print(tr.log_line(config.action_count), flush=True)
    except Exception as _ce:  # noqa: BLE001
        print(f"[GT_META] curation_error: {type(_ce).__name__}: {_ce}", flush=True)


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards in a string for use with ESCAPE '\\\\'."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Grep Intercept: extract the search pattern from grep/rg commands.
_GREP_SYMBOL_RE = re.compile(
    r"""(?:grep|rg)\s+          # grep or rg command
    (?:-[^\s]*\s+)*             # skip flags like -r, -n, --include, etc.
    (?:--\s+)?                  # optional -- separator
    ['"]?                       # optional quote
    ([A-Za-z_][A-Za-z0-9_]*)   # capture: identifier-shaped pattern
    """,
    re.VERBOSE,
)


def _extract_grep_symbol(cmd_text: str) -> str | None:
    """Extract a symbol name from a grep/rg command.

    Returns the symbol if it looks like a valid identifier (function/class name).
    Returns None for regex patterns, file paths, or non-identifier searches.
    """
    m = _GREP_SYMBOL_RE.search(cmd_text)
    if m:
        sym = m.group(1)
        # Skip common grep patterns that aren't symbol names
        if len(sym) < 2 or sym in ("def", "class", "import", "from", "return", "if", "else", "for", "while", "try", "except", "with"):
            return None
        return sym
    return None


# A path-shaped token: at least one "/" or a known source extension, no shell metachars.
_GREP_PATH_RE = re.compile(r"[A-Za-z0-9_./\-]+")


class _GrepSilent(Exception):
    """Sentinel: a path-scoped grep had no in-file definition — stay silent."""


def _same_grep_scope(stored_path: str, grepped_path: str) -> bool:
    """True when a stored node file_path matches the agent's grepped file/dir scope.

    Suffix/containment match on normalized POSIX paths so `beets/importer.py`
    matches a stored `/testbed/beets/importer.py`, and a directory scope
    `beets/` matches files under it. Generalized: pure path comparison.
    """
    if not stored_path or not grepped_path:
        return False
    s = stored_path.replace("\\", "/")
    g = grepped_path.replace("\\", "/").lstrip("/")
    if g.endswith("/"):
        return ("/" + g) in ("/" + s) or s.startswith(g)
    return s == g or s.endswith("/" + g) or s.endswith(g)


def _extract_grep_file_scope(cmd_text: str) -> str | None:
    """Recover the file/dir the agent scoped its grep to (e.g. `grep set_fields importer.py`).

    The on-grep intercept must honor the agent's OWN search scope. When the agent
    greps a symbol inside a specific file, resolving that bare name repo-wide (and
    surfacing a most-called homonym from an unrelated file) is actively harmful —
    wrong file, often wrong arity, presented as authoritative.

    Returns the LAST path-shaped argument (grep convention: `grep PATTERN PATH...`)
    that looks like a real source path, or None when the grep is repo-wide / has no
    file argument. Generalized: keys off path shape (slash or source extension), not
    any repo name.
    """
    if not cmd_text:
        return None
    # Drop shell redirections / pipelines so we only look at this command's args.
    head = re.split(r"[|;&]|\d?>>?|2>", cmd_text, maxsplit=1)[0]
    toks = head.split()
    # The pattern symbol itself — never treat it as the path scope.
    sym = _extract_grep_symbol(cmd_text)
    candidates: list[str] = []
    for tok in toks:
        t = tok.strip("'\"")
        if not t or t.startswith("-"):
            continue
        if t in ("grep", "rg", "egrep", "fgrep", "ripgrep"):
            continue
        if sym and t == sym:
            continue
        if not _GREP_PATH_RE.fullmatch(t):
            continue
        # Path-shaped: has a directory separator OR ends in a source extension.
        if "/" in t or t.endswith(SOURCE_EXTS):
            candidates.append(t)
    # grep convention puts paths AFTER the pattern -> last path-shaped token wins.
    return candidates[-1] if candidates else None


def _grep_intercept_callers(
    db_path: str,
    symbol: str,
    *,
    file_scope: str | None,
    limit: int = 5,
    min_conf: float = 0.6,
) -> list[tuple[str, int]]:
    """Return cross-file callers of `symbol`, path-scoped to the grepped file.

    When `file_scope` is given, the symbol's DEFINITION node is constrained to that
    file (LIKE suffix match). If no definition of `symbol` exists in the grepped file,
    we stay SILENT (return []) rather than fall back to a repo-wide homonym — wrong
    info that misdirects the agent is worse than no info (correct-or-quiet).

    When `file_scope` is None (a genuinely repo-wide grep), the old unscoped behavior
    is preserved.

    Returns a list of (caller_file_path, source_line) tuples.
    """
    if not symbol or not db_path or not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return []
    try:
        conn.execute("PRAGMA busy_timeout=3000")
        if file_scope:
            # Constrain the TARGET (callee) definition to the grepped file. If the
            # symbol isn't defined there, the agent's scope has no match -> silence.
            suffix = "%" + file_scope.replace("\\", "/").lstrip("/")
            def_exists = conn.execute(
                "SELECT 1 FROM nodes WHERE name = ? AND file_path LIKE ? "
                "AND is_test = 0 LIMIT 1",
                (symbol, suffix),
            ).fetchone()
            if not def_exists:
                return []
            rows = conn.execute(
                "SELECT DISTINCT nsrc.file_path, e.source_line "
                "FROM edges e "
                "JOIN nodes nt ON e.target_id = nt.id "
                "JOIN nodes nsrc ON e.source_id = nsrc.id "
                "WHERE nt.name = ? AND nt.file_path LIKE ? AND e.type = 'CALLS' "
                "AND COALESCE(e.confidence, 0.5) >= ? "
                "AND nsrc.file_path != nt.file_path "
                "LIMIT ?",
                (symbol, suffix, min_conf, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT nsrc.file_path, e.source_line "
                "FROM edges e "
                "JOIN nodes nt ON e.target_id = nt.id "
                "JOIN nodes nsrc ON e.source_id = nsrc.id "
                "WHERE nt.name = ? AND e.type = 'CALLS' "
                "AND COALESCE(e.confidence, 0.5) >= ? "
                "AND nsrc.file_path != nt.file_path "
                "LIMIT ?",
                (symbol, min_conf, limit),
            ).fetchall()
        return [(r[0], r[1] or 0) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _build_grep_intercept_query(
    symbol: str,
    *,
    file_scope: str | None,
    min_conf: float = 0.6,
    limit: int = 5,
) -> tuple[str, list]:
    """Build the container-fallback grep-intercept query as PARAMETERIZED SQL.

    Returns ``(sql, params)`` where every value (symbol, file-scope LIKE pattern,
    confidence floor, limit) is a bound ``?`` parameter — NOTHING is interpolated
    into the SQL string. CLAUDE.md mandates parameterized SQLite; the container
    proxy (``_container_query``) executes ``c.execute(sql, params)``, so this path
    binds rather than hand-escapes. A repo file path can legitimately contain a
    single quote (``a'b/c.py``); binding makes that injection-safe and un-mangled
    instead of relying on manual quote-doubling.

    ``ESCAPE '\\'`` stays a literal in the LIKE clause (it escapes the LIKE
    wildcards in the pattern, which is data, not SQL); only the pattern value is
    bound. When ``file_scope`` is falsy the scope predicate is omitted entirely
    (repo-wide), yielding 3 params instead of 4.
    """
    sql = (
        "SELECT DISTINCT nsrc.file_path, e.source_line "
        "FROM edges e "
        "JOIN nodes nt ON e.target_id = nt.id "
        "JOIN nodes nsrc ON e.source_id = nsrc.id "
        "WHERE nt.name = ? AND e.type = 'CALLS' "
    )
    params: list = [symbol]
    if file_scope:
        sql += "AND nt.file_path LIKE ? ESCAPE '\\' "
        params.append("%" + _escape_like(file_scope.replace("\\", "/").lstrip("/")))
    sql += (
        "AND COALESCE(e.confidence, 0.5) >= ? "
        "AND nsrc.file_path != nt.file_path "
        "LIMIT ?"
    )
    params.extend([min_conf, limit])
    return sql, params


SOURCE_EXTS = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".java",
    ".rs",
    ".rb",
    ".php",
)
MUTATING_EDITOR_VERBS = {"create", "str_replace", "insert", "write"}
VIEW_EDITOR_VERBS = {"view"}

GT_ITER_METRICS = os.getenv("GT_ITER_METRICS", "/tmp/gt_iter_metrics.jsonl")


def _metrics_path(config: Any, name: str) -> str:
    """Return a per-task metrics file path to avoid cross-worker clobbering.

    Falls back to the global /tmp path when config has no task_id (single-worker
    mode or early init).
    """
    task_id = getattr(config, "_meta_instance_id", None) if config is not None else None
    if task_id and task_id != "global":
        # Sanitize task_id for filesystem (replace / with _)
        safe_id = task_id.replace("/", "_").replace("\\", "_")
        return f"/tmp/gt_{name}_{safe_id}.jsonl"
    return f"/tmp/gt_{name}.jsonl"


def _classify_edit_path(p: str) -> str:
    base = os.path.basename(p or "")
    if base.startswith(("reproduce_", "debug_", "test_")) or "/tests/" in (p or "") or "/test_" in (p or ""):
        return "scaffold"
    return "source"


def _record_edit_iter(config: Any, iter_num: int, path: str) -> None:
    if config._iter_state["iter_to_first_edit"] is None:
        config._iter_state["iter_to_first_edit"] = iter_num
    if _classify_edit_path(path) == "source":
        if config._iter_state["iter_to_first_source_edit"] is None:
            config._iter_state["iter_to_first_source_edit"] = iter_num
        config._last_source_edit_iter = iter_num


def _reset_iter_state(config: Any, task_id: str) -> None:
    config._iter_state.update({"task_id": task_id, "iter_to_first_edit": None, "iter_to_first_source_edit": None})


def _flush_iter_state(config: Any = None) -> None:
    state = config._iter_state if config is not None else {"task_id": None}
    path = _metrics_path(config, "iter_metrics") if config is not None else GT_ITER_METRICS
    with open(path, "a") as f:
        f.write(json.dumps(state) + "\n")


GT_DIFF_TIMELINE = os.getenv("GT_DIFF_TIMELINE", "/tmp/gt_diff_timeline.jsonl")
GT_TASK_METRICS = os.getenv("GT_TASK_METRICS", "/tmp/gt_task_metrics.jsonl")


def _record_diff_snapshot(orig_run_action: Any, config: Any, event_path: str, action_count: int) -> None:
    repo_root = _sh_single_quote(config.workspace_root)
    cmd = f"cd {repo_root} && git diff --shortstat 2>/dev/null; echo '---'; git diff --name-only 2>/dev/null | head -20"
    out = _run_internal(orig_run_action, cmd, 10)

    parts = (out or "").split("---")
    shortstat = parts[0].strip() if parts else ""
    name_list = [x.strip() for x in parts[1].strip().split("\n") if x.strip()] if len(parts) > 1 else []
    name_only_count = len(name_list)

    files_changed = insertions = deletions = 0
    m = re.search(r"(\d+) files? changed", shortstat)
    if m:
        files_changed = int(m.group(1))
    m = re.search(r"(\d+) insertion", shortstat)
    if m:
        insertions = int(m.group(1))
    m = re.search(r"(\d+) deletion", shortstat)
    if m:
        deletions = int(m.group(1))

    diff_nonzero = files_changed > 0 or name_only_count > 0

    if name_only_count < config._prev_name_only_count:
        config._new_files_deleted += config._prev_name_only_count - name_only_count
    if event_path and event_path not in config.edited_files and not event_path.startswith("["):
        config._new_files_created += 1
    config._prev_name_only_count = name_only_count

    record = {
        "iter": action_count,
        "event_path": event_path,
        "shortstat": shortstat,
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
        "changed_files": name_list,
        "diff_nonzero": diff_nonzero,
    }
    _diff_path = _metrics_path(config, "diff_timeline")
    with open(_diff_path, "a") as f:
        f.write(json.dumps(record) + "\n")

    if diff_nonzero:
        if not config._diff_ever_nonzero:
            config._diff_first_nonzero_iter = action_count
        config._diff_ever_nonzero = True
        config._diff_last_nonzero_iter = action_count
        config._diff_just_collapsed = False
    elif config._diff_ever_nonzero:
        config._diff_collapsed_count += 1
        config._diff_collapse_after_file = event_path
        config._diff_just_collapsed = True
        print(f"[GT_META] DIFF COLLAPSED TO ZERO at iter {action_count} after {event_path}", flush=True)


def _classify_behavior(config: Any) -> str:
    has_source = any(_is_real_source_edit(f, config) for f in config.edited_files)
    if has_source and config._diff_collapsed_count > 0:
        return "collapsed"
    if has_source:
        return "source_edit"
    if config.edited_files:
        return "non_source_edit_loop"
    return "read_run_stall"


def _flush_task_end_metrics(config: Any, phase: str = "finish") -> None:
    if getattr(config, '_metrics_flushed', False):
        return
    config._metrics_flushed = True

    # Close structured telemetry writer + compute run summary (Decision 34)
    writer = getattr(config, "_telemetry_writer", None)
    if writer is not None:
        try:
            if os.environ.get("GT_DEEP_LAYER_GROUNDED_METRICS", "0") == "1":
                from groundtruth.telemetry.metrics import compute_run_summary, print_summary
                summary = compute_run_summary(
                    writer.layer_events_path,
                    writer.agent_reactions_path,
                    writer.agent_events_path,
                    writer.belief_ledger_path,
                )
                writer.write_run_summary(summary)
                print_summary(summary)
        except Exception as mex:
            print(f"[GT_META] run summary computation failed: {mex}", flush=True)
        try:
            writer.close()
            print(f"[GT_META] Telemetry writer closed. Files: {writer.layer_events_path}", flush=True)
        except Exception:
            pass

    _flush_iter_state(config)

    if hasattr(config, '_task_end_orig_run_action') and config._task_end_orig_run_action:
        try:
            _record_diff_snapshot(config._task_end_orig_run_action, config, f"[{phase}]", config.action_count)
        except Exception:
            pass

    task_metrics = {
        "task_id": config._meta_instance_id,
        "condenser": os.environ.get("EVAL_CONDENSER", "none"),
        "action_count": config.action_count,
        "total_edits": len(config.edited_files),
        "l5_fired": config._l5_scaffold_fired,
        "l5_fire_count": config._l5_metrics.get("l5_fire_count", 0),
        "first_real_source_edit": any(_is_real_source_edit(f, config) for f in config.edited_files),
        "diff_ever_nonzero": config._diff_ever_nonzero,
        "first_nonzero_diff_iter": config._diff_first_nonzero_iter,
        "last_nonzero_diff_iter": config._diff_last_nonzero_iter,
        "diff_collapsed_count": config._diff_collapsed_count,
        "collapse_after_file": config._diff_collapse_after_file,
        "new_files_created": config._new_files_created,
        "new_files_deleted": config._new_files_deleted,
        "first_scaffold_iter": config._first_scaffold_iter,
        "additional_scratch_after_l5": config._l5_metrics.get("num_additional_scaffolds_after_l5", 0),
        "stuck_compat_skips": config._stuck_compat_skip_count,
        "behavior_class": _classify_behavior(config),
        "phase": phase,
    }
    _task_metrics_path = _metrics_path(config, "task_metrics")
    with open(_task_metrics_path, "a") as f:
        f.write(json.dumps(task_metrics) + "\n")
    print(f"[GT_META] Task metrics ({phase}): {json.dumps(task_metrics)}", flush=True)

    # Comprehensive session summary
    _l3_count = getattr(config, "_l3_fire_count", 0)
    _l3b_count = getattr(config, "_l3b_fire_count", 0)
    _l5_count = config._l5_metrics.get("l5_fire_count", 0)
    _auto_query_count = getattr(config, "_auto_query_count", 0)
    _dedup_count = sum(1 for k in config.evidence_sent if k.startswith("l3:") or k.startswith("l3b:"))
    _grep_intercept = getattr(config, "_grep_intercept_count", 0)
    print(
        f"[GT_SUMMARY] layers={{L3:{_l3_count}, L3b:{_l3b_count}, L4_auto:{_auto_query_count}, L5:{_l5_count}}} "
        f"dedup_keys={len(config.evidence_sent)} grep_intercepts={_grep_intercept} "
        f"edits={len(config.edited_files)} views={len(config.viewed_files)} "
        f"actions={config.action_count} phase={phase}",
        flush=True,
    )


INTERNAL_GT_MARKERS = (
    "/tmp/gt-index",
    "gt-index-linux",
    "/tmp/gt_tools/",
    "groundtruth.hooks.post_edit",
    "groundtruth.hooks.post_view",
    "gt_hook.py",
)
SCAFFOLDING_PREFIXES = (
    "reproduce_",
    "repro_",
    "debug_",
    "verify_fix",
    "verify_implementation",
    "test_fix",
    "scratch_",
    "temp_",
)
# Extensions and patterns that indicate a non-source file the agent created
# (backup copies, editor swap files, OH metadata). These pollute the git diff
# and can cause eval failure (weasyprint-2300: flex.py.bak in patch).
_JUNK_EXTENSIONS = (".bak", ".orig", ".swp", ".tmp", ".backup", ".pyc", ".pyo")
_JUNK_DIRS = (".openhands/", "__pycache__/", ".git/")
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]*$|(^|/)[^/]*_test\.[^/]*$"
)

_GT_VALIDATE_ARG_RE = re.compile(r"\bgt_validate\s+([^\s&|;<>]+)")


@dataclass(frozen=True)
class HookEvent:
    """Classified OpenHands tool event."""

    kind: str
    path: str = ""
    reason: str = ""


@dataclass
class GTRuntimeConfig:
    """Runtime paths and limits for the OH GT hook layer."""

    workspace_root: str = WORKSPACE_ROOT
    graph_db: str = GRAPH_DB
    gt_index_bin: str = _DEFAULT_GT_INDEX
    tools_dir: str = GT_TOOLS_DIR
    max_items: int = 3
    _node_count: int = 0
    _edge_count: int = 0
    _repo_scale: str = "medium"  # small (<500), medium (500-5000), large (>5000)
    source_exts: tuple[str, ...] = SOURCE_EXTS
    pending_checks: set[str] = field(default_factory=set)
    verified_checks: set[str] = field(default_factory=set)
    edited_files: set[str] = field(default_factory=set)
    viewed_files: set[str] = field(default_factory=set)
    pending_summaries: list[tuple[str, str]] = field(default_factory=list)
    last_visible_observation: Any = None
    telemetry: Any = None  # GTTelemetry, optional
    evidence_sent: dict[str, str] = field(default_factory=dict)  # file -> evidence hash for dedup
    evidence_cache: dict[str, str] = field(default_factory=dict)  # file -> top constraint for recall injection
    brief_candidates: set[str] = field(default_factory=set)
    action_count: int = 0  # PRF Iterative Checkpoints
    max_iter: int = 100
    scaffold_stripped: bool = False
    interaction_log: list[dict[str, Any]] = field(default_factory=list)
    instance_ref: Any = None
    _meta_instance_id: str = "global"
    _l5_scaffold_fired: bool = False
    _l5_last_scaffold_file: str = ""
    _l5_metrics: dict[str, Any] = field(default_factory=lambda: {
        "l5_scaffold_fired": False,
        "l5_fire_count": 0,
        "source_edit_after_l5": False,
        "num_additional_scaffolds_after_l5": 0,
        "touched_brief_candidate_after_l5": False,
    })
    _l5_edit_counts_per_file: dict[str, int] = field(default_factory=dict)
    _l3_fire_count: int = 0
    _l3b_fire_count: int = 0
    _gt_delivered_evidence_files: dict[str, int] = field(default_factory=dict)
    _consensus_fired: bool = False
    _consensus_turn: int = -1
    # Localizer multi-signal agreement tier, parsed from the brief's
    # <gt-localization confidence="high|medium|low"> tag. "high" == >=2 of
    # {grep, semantic, structural} signals agree on the localization. Gates the
    # CONFIDENT "primary target" consensus emission below; medium/low/absent
    # downgrades to the softer non-imperative "also in scope" form so a
    # single-signal mis-rank can no longer anoint the wrong file.
    _localization_confidence: str = ""
    _consensus_scope: list[str] = field(default_factory=list)
    _consensus_confirmed: set[str] = field(default_factory=set)
    _consensus_scope_edited: set[str] = field(default_factory=set)
    # Behavioral metrics for selective rescue governor
    _last_gt_action: int = 0
    _source_edit_actions: list[int] = field(default_factory=list)
    _test_actions: list[int] = field(default_factory=list)
    _read_history: list[str] = field(default_factory=list)
    _search_count_since_edit: int = 0
    _rescue_fired_count: int = 0
    _rescue_last_action: int = 0
    _grep_intercept_count: int = 0
    _diff_ever_nonzero: bool = False
    _diff_first_nonzero_iter: int = 0
    _diff_last_nonzero_iter: int = 0
    _diff_collapsed_count: int = 0
    _diff_collapse_after_file: str = ""
    _diff_just_collapsed: bool = False
    _new_files_created: int = 0
    _new_files_deleted: int = 0
    _first_scaffold_iter: int = 0
    _task_end_orig_run_action: Any = None
    _metrics_flushed: bool = False
    _prev_name_only_count: int = 0
    _telemetry_writer: Any = None  # GTTelemetryWriter, initialized when GT_STRUCTURED_EVENTS=1
    _pending_next_actions: list[dict[str, Any]] = field(default_factory=list)  # Online tracker for L5 ignored_next_action (legacy mirror)
    _agent_state: Any = None  # FINAL_ARCH_V2 Layer 2 canonical AgentState (lazy-initialized via _ensure_agent_state)
    # L6 pre-submit (verifiable consolidation, Option 2): fire diff-wide test
    # suggestions ONCE at the edit->review transition, while the agent can act.
    _presubmit_edited_files: set[str] = field(default_factory=set)  # source files edited this task
    _presubmit_last_edit_action: int = 0  # action_count at last source edit
    _presubmit_fired: bool = False
    _l5_governor: Any = None
    _edge_verifier: Any = None
    _host_graph_db: str = field(default_factory=lambda: os.environ.get("GT_PREBUILT_GRAPH_DB", ""))
    # C6 step-4 (RF-3): True only when an offline-promoted db was successfully
    # uploaded into the container as config.graph_db and the in-container build
    # was skipped. Stays False on the default eval path (no GT_PREBUILT_GRAPH_DB),
    # so every prebuilt-gated branch is dead code unless the flag is armed.
    _gt_prebuilt_active: bool = False
    _iter_state: dict[str, Any] = field(default_factory=lambda: {
        "task_id": None, "iter_to_first_edit": None, "iter_to_first_source_edit": None,
    })
    _stuck_compat_history: list[tuple[str, str]] = field(default_factory=list)
    _stuck_compat_skip_count: int = 0

    def __post_init__(self) -> None:
        if self._host_graph_db and os.path.exists(self._host_graph_db):
            os.environ.setdefault("GT_GRAPH_DB", self._host_graph_db)
            print(f"[GT_META] prebuilt_graph_db: {self._host_graph_db} ({os.path.getsize(self._host_graph_db)} bytes)", flush=True)


@dataclass
class GTTelemetry:
    """Per-run GT layer utilization (written to instance["gt_telemetry"] on finish)."""

    task_id: str
    layer_hits: dict[str, dict[str, int]] = field(default_factory=dict)

    def _bump(self, layer: str, key: str) -> None:
        bucket = self.layer_hits.setdefault(layer, {"ok": 0, "fail": 0, "skipped": 0})
        bucket[key] = bucket.get(key, 0) + 1

    def record_brief(self, ok: bool, l2_present: bool) -> None:
        self._bump("L1", "ok" if ok else "fail")
        if l2_present:
            self._bump("L2", "ok")
        else:
            self._bump("L2", "fail")

    def record_hook(self, layer: str, ok: bool, empty: bool = False) -> None:
        if empty:
            self._bump(layer, "skipped")
        else:
            self._bump(layer, "ok" if ok else "fail")

    def record_reindex(self, ok: bool) -> None:
        self._bump("L6", "ok" if ok else "fail")

    def record_gate(self, fired: bool) -> None:
        self._bump("L5", "ok" if fired else "skipped")

    def record_l4(self) -> None:
        self._bump("L4", "ok")

    def record_l4_prefetch(self, queries: int, lines: int) -> None:
        for _ in range(queries):
            self._bump("L4", "ok")
        if queries == 0:
            self._bump("L4", "skipped")

    def utilization(self) -> dict[str, float]:
        """Rough 0–1 utilization per layer (diagnostic only)."""

        def score(layer: str) -> float:
            b = self.layer_hits.get(layer, {})
            ok, fail, skipped = b.get("ok", 0), b.get("fail", 0), b.get("skipped", 0)
            total = ok + fail + skipped
            if total == 0:
                return 0.0
            if ok > 0:
                return 1.0
            if skipped > 0 and fail == 0:
                return 1.0
            return ok / (ok + fail) if (ok + fail) else 0.0

        return {f"L{i}": score(f"L{i}") for i in range(1, 7)}

    def finalize(self) -> dict[str, Any]:
        u = self.utilization()

        # L3b keyed as L3b in hits
        b3 = self.layer_hits.get("L3b", {})
        denom = b3.get("ok", 0) + b3.get("fail", 0)
        u["L3b"] = (b3.get("ok", 0) / denom) if denom else 0.0
        ow = {"L1": 0.2, "L2": 0.15, "L3": 0.2, "L3b": 0.1, "L4": 0.1, "L5": 0.15, "L6": 0.1}
        overall = sum(u.get(k, 0.0) * w for k, w in ow.items())
        return {
            "task_id": self.task_id,
            "layer_hits": dict(self.layer_hits),
            "utilization": u,
            "overall_utilization": round(overall, 4),
        }


def _compute_repo_scale(config: "GTRuntimeConfig") -> None:
    """Compute dynamic limits from actual graph density. Called once after graph.db is built.

    Density = edges/nodes. High density (>3) means tightly connected code —
    tighter limits prevent noise. Low density (<1) means sparse connections —
    looser limits capture everything available.
    """
    n = max(config._node_count, 1)
    e = config._edge_count
    density = e / n

    if density > 3:
        config._repo_scale = "dense"
        config.max_items = 2
    elif density < 1:
        config._repo_scale = "sparse"
        config.max_items = 5
    else:
        config._repo_scale = "normal"
        config.max_items = 3


def _dynamic_limit(config: "GTRuntimeConfig", base: int) -> int:
    """Scale a limit based on graph density, not hardcoded repo size."""
    if config._repo_scale == "sparse":
        return max(base, base * 2)
    elif config._repo_scale == "dense":
        return max(2, base * 2 // 3)
    return base


def _sh_single_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def _env_prefix(config: GTRuntimeConfig) -> str:
    tp = config.tools_dir.rstrip("/")
    return (
        f"export GT_GRAPH_DB={_sh_single_quote(config.graph_db)}; "
        f"export GT_REPO_ROOT={_sh_single_quote(config.workspace_root)}; "
        "export GT_PYTHON=python3; "
        "export PYTHONPATH=/tmp:${PYTHONPATH:-}; "
        f"export PATH={tp}/gt_query/bin:{tp}/gt_search/bin:"
        f"{tp}/gt_navigate/bin:{tp}/gt_validate/bin:${{PATH:-}}; "
    )


def _brief_max_tokens(text: str, max_tokens: int = 500) -> str:
    """Keep brief compact (~4 chars / token heuristic for English-ish code paths)."""

    if not text:
        return ""
    max_chars = max_tokens * 4
    lines = text.strip().split("\n")
    # STRUCTURE-PRESERVING (2026-05-29): the old code REORDERED lines —
    # `merged = ranked + rest` pulled every path-bearing line to the front and
    # pushed tag lines to the back. On the STRUCTURED v1r brief this shredded the
    # <gt-graph-map> block: its body lines contain file paths -> moved to the front,
    # while the <gt-graph-map>/</gt-graph-map> tag lines collapsed together at the
    # back -> an EMPTY <gt-graph-map></gt-graph-map> reached the agent (same length,
    # structure destroyed; proven on haystack-8525). Cap in ORIGINAL order so blocks
    # stay intact — a pure tail char-cap backstop, never a reorder.
    out: list[str] = []
    n = 0
    for ln in lines:
        if n + len(ln) + 1 > max_chars:
            break
        out.append(ln)
        n += len(ln) + 1
    body = "\n".join(out)
    if len(text.strip()) > max_chars:
        body += "\n[GT_BRIEF_TRUNCATED]"
    return body


def _derive_package_name(instance_id: str) -> str:
    if not instance_id:
        return ""
    tail = instance_id
    if "__" in instance_id:
        tail = instance_id.split("__", 1)[1]
    return re.sub(r"-\d+$", "", tail.strip())


def _rewrite_site_package_paths_in_brief(brief: str, instance_id: str, workspace_root: str) -> str:
    """Rewrite site-packages/dist-packages file paths to workspace checkout paths."""
    if not brief:
        return brief
    pkg = _derive_package_name(instance_id)
    if not pkg:
        return brief
    root = workspace_root.rstrip("/")
    file_exts = "py|go|js|ts|rs|c|cpp"
    pattern = re.compile(
        r"(?P<prefix>/[^\s\"'<>]+(?:site-packages|dist-packages)/"
        + re.escape(pkg)
        + r"/)(?P<rest>[^\s\"'<>]+\.(?:"
        + file_exts
        + r"))"
    )
    return pattern.sub(rf"{root}/{pkg}/\g<rest>", brief)


def _action_class(action: Any) -> str:
    return type(action).__name__


def _action_text(action: Any) -> str:
    return (
        getattr(action, "command", "")
        or getattr(action, "content", "")
        or getattr(action, "thought", "")
        or ""
    )


def _path_attr(action: Any) -> str:
    for attr in ("path", "file_path", "source", "target"):
        value = getattr(action, attr, "") or ""
        if value:
            return str(value)
    return ""


def _normalize_path(path: str) -> str:
    path = path.strip().strip("'\"")
    if path.startswith(WORKSPACE_ROOT + "/"):
        path = path[len(WORKSPACE_ROOT) + 1 :]
    return path.replace("\\", "/")


def _path_relative_to_workspace(path: str, config: GTRuntimeConfig) -> str:
    path = path.strip().strip("'\"").replace("\\", "/")
    root = config.workspace_root.rstrip("/")
    if root and path.startswith(root + "/"):
        return path[len(root) + 1 :]

    normalized = _normalize_path(path)
    workspace_name = root.rsplit("/", 1)[-1] if root else ""
    prefix = f"{workspace_name}/" if workspace_name else ""
    if prefix and normalized.startswith(prefix):
        return normalized[len(prefix) :]
    return normalized


def _is_source_path(path: str, source_exts: tuple[str, ...] = SOURCE_EXTS) -> bool:
    return _normalize_path(path).endswith(source_exts)


def _is_test_path(path: str) -> bool:
    return TEST_PATH_RE.search(_normalize_path(path)) is not None


def _is_scaffolding_path(path: str) -> bool:
    """Detect non-source files the agent creates that should NOT be in the patch.

    Generalized — not a list of known bad names, but a structural check:
    1. Name prefix match (reproduce_, debug_, scratch_, etc.)
    2. Junk extension (.bak, .orig, .swp, .tmp, .backup, .pyc, .pyo)
    3. Non-source directory (.openhands/, __pycache__/, .git/)
    4. Editor/tool artifacts (any file with a source extension PLUS another
       extension appended — file.py.bak, file.go.orig, file.rs.tmp)

    Works across all 5 languages — no language-specific patterns.
    """
    norm = _normalize_path(path).replace("\\", "/")
    base = Path(norm).name.lower()

    # 1. Known scaffold prefixes
    if base.startswith(SCAFFOLDING_PREFIXES):
        return True

    # 2. Junk extensions
    if any(base.endswith(ext) for ext in _JUNK_EXTENSIONS):
        return True

    # 3. Non-source directories
    if any(d in norm.lower() for d in _JUNK_DIRS):
        return True

    # 4. Double-extension pattern: file.py.bak, file.go.orig, file.rs.tmp
    # A source file with an extra extension appended = a backup copy
    parts = base.rsplit(".", 2)
    if len(parts) == 3:
        mid_ext = "." + parts[1]
        if mid_ext in SOURCE_EXTS:
            return True  # e.g., flex.py.bak → "py" is a source ext, so it's a backup

    return False


def _is_real_source_edit(path: str, config: GTRuntimeConfig) -> bool:
    """Source edit = not scaffold, not test, and either indexed in graph.db or a source extension."""
    if _is_scaffolding_path(path):
        return False
    rel = _normalize_rel_path(path, config) or path
    base = Path(rel).name.lower()
    if base.startswith("test_") or "/tests/" in rel.lower() or "/test/" in rel.lower():
        return False
    _src_edit_db = getattr(config, "_host_graph_db", "") or ""
    if _src_edit_db and os.path.exists(_src_edit_db):
        try:
            import sqlite3
            conn = sqlite3.connect(_src_edit_db)
            row = conn.execute(
                "SELECT 1 FROM nodes WHERE file_path LIKE ? ESCAPE '\\' AND is_test = 0 LIMIT 1",
                (f"%{_escape_like(rel)}",),
            ).fetchone()
            conn.close()
            if row is not None:
                return True
            # File not in graph.db — could be a new file the agent created.
            # Fall back to extension check: treat as source if it has a source extension.
            return _is_source_path(rel, config.source_exts)
        except Exception:
            pass
    return _is_source_path(path, config.source_exts)


def _render_scaffold_advisory(scaffold_path: str, config: GTRuntimeConfig) -> str:
    """Scaffold advisory — DIAGNOSTIC ONLY, no file prescription.

    Research basis (SWE-PRM, NeurIPS 2025, arXiv 2509.02360): mid-trajectory
    intervention helps resolution ONLY when diagnostic, never prescriptive.
    Action-prescriptive feedback ("edit these files: X, Y, Z") LOWERED
    success — it over-constrains the agent and anchors it (anchoring-bias
    arXiv 2412.06593; "Is Grep All You Need" arXiv 2605.15184 — a harness
    is a privileged tool output, so a wrong file suggestion anchors and
    compounds across planning steps).

    File candidates belong UPFRONT in L1 orientation (where localization's
    proven 15-17x / +12.8pp gains are realized), NOT in a late reminder.

    So this advisory states only the verifiable diagnostic fact about the
    trajectory and lets the agent self-correct. No file list. No directive.
    """
    scaffold_name = Path(scaffold_path).name
    _scratch_n = config._l5_metrics.get("l5_fire_count", 0) if hasattr(config, "_l5_metrics") else 0
    lines = [
        '<gt-advisory layer="L5" trigger="non_source_without_progress">',
        f"No tracked source file modified yet; last edit was a scratch/test "
        f"file ({scaffold_name}). Source-level resolution requires editing "
        f"tracked source.",
        "</gt-advisory>",
    ]
    return "\n".join(lines)


def _maybe_fire_l5(
    config: GTRuntimeConfig,
    path: str,
    obs: Any,
    act_text: str,
    tel_obj: Any,
    instance_ref: Any,
) -> Any:
    """Fire L5 on a non-source edit when there is no prior source progress."""
    if path == config._l5_last_scaffold_file:
        return obs
    config._l5_scaffold_fired = True
    config._l5_last_scaffold_file = path
    config._l5_metrics["l5_scaffold_fired"] = True
    config._l5_metrics["l5_fire_count"] = config._l5_metrics.get("l5_fire_count", 0) + 1
    advisory = _render_scaffold_advisory(path, config)
    if advisory:
        print(f"[GT_META] L5 non_source_edit fired for {path}", flush=True)
        _l5_ns_eid = _emit_structured_event(
            config, "L5", "non_source_edit",
            rendered_text=advisory, file_path=path,
        )
        _log_gt_interaction(config, "L5", f"non_source:{path}", "redirect", advisory,
            agent_action_before=act_text[:300], event_id=_l5_ns_eid or "")
        obs = append_observation(obs, "\n\n" + advisory + "\n")
        if tel_obj is not None:
            tel_obj.record_gate(True)
            _write_gt_telemetry(instance_ref, tel_obj)
    return obs


def _maybe_fire_presubmit_verify(config: GTRuntimeConfig, obs: Any, orig_run_action: Any) -> Any:
    """L6 pre-submit (Option 2) — VERIFIABLE diff-wide test consolidation.

    Fires ONCE per task at the edit→review transition: the agent has made
    >=1 source edit and then done >=3 actions without editing source (it has
    stopped editing and is reviewing/verifying). Delivered HERE — mid-
    trajectory, while the agent can still act — NOT in the finish handler
    (state=FINISHED there = dead write).

    VERIFIABLE ONLY (research: SWE-agent test guardrail +10.7pp NeurIPS 2024).
    Lists the tests (from the assertions table, target_node_id > 0 — real
    test→target links) that cover the files the agent edited, and suggests
    running them. NO semantic judgment ("patch incomplete"), NO caller-edit
    prescription — that is the mixed/harmful review_on_submit class.

    Goal test: more correct context (which tests cover your diff) at the
    helping moment (review phase, actionable), no wrong-direction risk
    (verifiable facts only), generalized (any repo with an assertions table;
    silent if none).
    """
    if config._presubmit_fired or not config._presubmit_edited_files:
        return obs
    # Edit→review transition: >=3 actions since the last source edit.
    if (config.action_count - config._presubmit_last_edit_action) < 3:
        return obs
    db = getattr(config, "_host_graph_db", "") or os.environ.get("GT_PREBUILT_GRAPH_DB", "") or ""
    if not db or not os.path.exists(db):
        db = config.graph_db if (config.graph_db and os.path.exists(config.graph_db)) else ""
    if not db:
        return obs

    tests: list[str] = []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            conn.execute("SELECT 1 FROM assertions LIMIT 1")  # table exists?
        except Exception:
            conn.close()
            config._presubmit_fired = True  # nothing to offer; don't retry
            return obs
        seen: set[str] = set()
        for _ef in list(config._presubmit_edited_files)[:10]:
            _norm = _ef.replace("\\", "/").lstrip("/")
            rows = conn.execute(
                "SELECT DISTINCT n.file_path, n.name FROM assertions a "
                "JOIN nodes n ON a.test_node_id = n.id "
                "JOIN nodes nt ON a.target_node_id = nt.id "
                "WHERE nt.file_path LIKE ? ESCAPE '\\' AND a.target_node_id > 0 "
                "LIMIT 5",
                (f"%{_escape_like(_norm)}",),
            ).fetchall()
            for _fp, _nm in rows:
                key = f"{_fp}::{_nm}"
                if key not in seen:
                    seen.add(key)
                    tests.append(f"  pytest {_fp}::{_nm}")
        conn.close()
    except Exception as _ps_exc:
        print(f"[GT_META] presubmit_verify_error: {_ps_exc}", flush=True)
        return obs

    config._presubmit_fired = True
    if not tests:
        # Under-confident: no verified test linkage. Stay silent (no guess).
        print("[GT_META] presubmit_verify: no verified tests for edited files — silent", flush=True)
        return obs

    text = (
        "[GT_VERIFY] Tests covering your changed files "
        f"({len(config._presubmit_edited_files)} edited) — run before finishing:\n"
        + "\n".join(tests[:8])
    )
    obs = append_observation(obs, "\n" + text)
    _emit_structured_event(config, "L6", "presubmit_verify", rendered_text=text)
    print(f"[GT_DELIVERY] presubmit_verify: tests={len(tests)} edited={len(config._presubmit_edited_files)}", flush=True)
    return obs


def _is_internal_gt_command(text: str) -> bool:
    blob = f" {text}"
    return any(marker in blob for marker in INTERNAL_GT_MARKERS)


def _parse_editor_command(command: str) -> tuple[str, str] | None:
    match = re.search(r"(?:str_replace_editor|file_editor)\s+(\S+)\s+(\S+)", command)
    if match:
        return match.group(1), _normalize_path(match.group(2))
    return None


def _parse_bash_edit_command(command: str) -> str:
    """Detect a mutating bash file-write and return the target path.

    Closes the L4b coverage gap: agents that edit via bash (`sed -i`,
    heredoc redirect, `tee`, `>`/`>>`) instead of the editor tool would
    otherwise route to skip — no L6 reindex, no L3 contract/verify/
    completeness. Conservative: only clear write patterns. Downstream
    `_is_source_path` / `_is_test_path` gates still apply in the caller.
    """
    patterns = [
        r"\bsed\s+-i\b[^>]*?\s(\S+)\s*$",          # sed -i ... FILE
        r"\btee\s+(?:-a\s+)?(\S+)",                 # tee FILE / tee -a FILE
        r">{1,2}\s*([^\s|&;<>]+\.\w+)",             # > FILE / >> FILE (ext'd)
        r"<<\s*['\"]?\w+['\"]?\s*>\s*([^\s|&;]+)",  # heredoc redirect target
    ]
    for pattern in patterns:
        match = re.search(pattern, command)
        if match:
            return _normalize_path(match.group(1))
    return ""


def _parse_read_command(command: str) -> str:
    patterns = [
        r"\bcat\s+(\S+)",
        r"\bsed\s+(?:-[^\s]+\s+)?(?:'[^']+'\s+|\"[^\"]+\"\s+)?(\S+)",
        r"\bhead\s+(?:-[^\s]+\s+)?(\S+)",
        r"\btail\s+(?:-[^\s]+\s+)?(\S+)",
        r"\bnl\s+(?:-[^\s]+\s+)?(\S+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, command)
        if match:
            return _normalize_path(match.group(1))
    return ""


def classify_tool_event(
    action: Any, *, source_exts: tuple[str, ...] = SOURCE_EXTS
) -> HookEvent:
    """Classify an OpenHands action into post-view, post-edit, finish, or skip."""

    cls = _action_class(action)
    text = _action_text(action)
    if _is_internal_gt_command(text):
        return HookEvent("skip", reason="internal_gt_command")

    if cls in {"AgentFinishAction", "FinishAction"} or text.strip() == "finish":
        return HookEvent("finish")

    if cls in {"FileReadAction", "FileViewAction"}:
        path = _normalize_path(_path_attr(action))
        if not path:
            return HookEvent("skip", reason="no_path")
        if not _is_source_path(path, source_exts):
            return HookEvent("skip", path=path, reason="non_source_ext")
        return HookEvent("post_view", path=path)

    if cls in {"FileEditAction", "FileWriteAction"}:
        path = _normalize_path(_path_attr(action))
        if not path:
            return HookEvent("skip", reason="no_path")
        if not _is_source_path(path, source_exts):
            return HookEvent("skip", path=path, reason="non_source_ext")
        return HookEvent("post_edit", path=path)

    if cls == "CmdRunAction":
        parsed = _parse_editor_command(text)
        if parsed:
            verb, path = parsed
            if verb in VIEW_EDITOR_VERBS:
                if not _is_source_path(path, source_exts):
                    return HookEvent("skip", path=path, reason="non_source_ext")
                return HookEvent("post_view", path=path)
            if verb not in MUTATING_EDITOR_VERBS:
                return HookEvent("skip", path=path, reason=f"non_mutating_verb:{verb}")
            if _is_test_path(path):
                return HookEvent("skip", path=path, reason="test_path")
            if not _is_source_path(path, source_exts):
                return HookEvent("skip", path=path, reason="non_source_ext")
            return HookEvent("post_edit", path=path)

        # Bash file-write (sed -i / heredoc / tee / redirection) → post_edit.
        # Closes the L4b coverage gap so L6 reindex + L3 contract/verify fire
        # on edits the agent makes via bash instead of the editor tool.
        bash_edit_path = _parse_bash_edit_command(text)
        if bash_edit_path:
            if _is_test_path(bash_edit_path):
                return HookEvent("skip", path=bash_edit_path, reason="test_path")
            if not _is_source_path(bash_edit_path, source_exts):
                return HookEvent("skip", path=bash_edit_path, reason="non_source_ext")
            return HookEvent("post_edit", path=bash_edit_path)

        read_path = _parse_read_command(text)
        if read_path:
            if not _is_source_path(read_path, source_exts):
                return HookEvent("skip", path=read_path, reason="non_source_ext")
            return HookEvent("post_view", path=read_path)

    return HookEvent("skip", reason="non_hook_action")


def make_reindex_command(path: str, config: GTRuntimeConfig) -> str:
    """Build the L6 command. Uses gt-index ``-file`` mode."""
    if not config.gt_index_bin:
        return ""
    rel = _path_relative_to_workspace(path, config)
    return (
        f"{config.gt_index_bin} -root={config.workspace_root} "
        f"-file={rel} -output={config.graph_db}"
    )


def make_view_hook_command(event: HookEvent, config: GTRuntimeConfig) -> str:
    rel_path = _path_relative_to_workspace(event.path, config)
    cmd = (
        _env_prefix(config)
        + "python3 -m groundtruth.hooks.post_view "
        + f"--root={config.workspace_root} --db={config.graph_db} --file={rel_path}"
    )
    if os.environ.get("GT_REBUILD_L3B", "0") == "1":
        ratio = config.action_count / max(config.max_iter, 1)
        cmd += f" --iteration-ratio={ratio:.2f}"
        cmd += f" --total-candidates={len(getattr(config, 'brief_candidates', set()))}"
    # Always emit structured output — needed for LSP verification + telemetry
    cmd += " --structured-output"
    return cmd


def make_edit_hook_command(event: HookEvent, config: GTRuntimeConfig) -> str:
    return make_edit_hook_command_with_artifacts(event, config)


def make_edit_hook_command_with_artifacts(
    event: HookEvent,
    config: GTRuntimeConfig,
    *,
    diff_path: str | None = None,
    old_content_path: str | None = None,
    mode: str = "post_edit",
    iteration_ratio: float = 0.0,
) -> str:
    rel_path = _path_relative_to_workspace(event.path, config)
    cmd = (
        _env_prefix(config)
        + "python3 -m groundtruth.hooks.post_edit "
        + f"--root={config.workspace_root} --db={config.graph_db} "
        + f"--file={rel_path} --quiet --max-items={config.max_items}"
    )
    if diff_path:
        cmd += f" --diff={diff_path}"
    if old_content_path:
        cmd += f" --old-content={old_content_path}"
    if os.environ.get("GT_REBUILD_L3", "0") == "1":
        cmd += f" --mode={mode}"
        cmd += f" --iteration-ratio={iteration_ratio:.2f}"
    # Always emit structured output — needed for LSP verification + telemetry
    cmd += " --structured-output"
    return cmd


def _extract_extras_from_observation(obs: Any) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    for attr in ("extras", "extra", "metadata"):
        value = getattr(obs, attr, None)
        if isinstance(value, dict):
            extras.update(value)
    return extras


def _deep_find_first(obj: Any, keys: set[str]) -> Any | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                return v
        for v in obj.values():
            found = _deep_find_first(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_first(v, keys)
            if found is not None:
                return found
    return None


def _extract_diff_and_old_content(obs: Any) -> tuple[str, str]:
    extras = _extract_extras_from_observation(obs)
    if not extras:
        return "", ""
    diff_val = _deep_find_first(extras, {"diff"})
    old_val = _deep_find_first(extras, {"old_content", "oldContent"})
    diff = diff_val if isinstance(diff_val, str) else ""
    old_content = old_val if isinstance(old_val, str) else ""
    return diff, old_content


def _write_text_to_container(
    orig_run_action: Callable[[Any], Any], content: str, target_path: str
) -> bool:
    if not content:
        return False
    try:
        payload = content.encode("utf-8", errors="replace")
    except Exception:
        return False
    chunks = _b64_chunks(payload)
    if not chunks:
        return False
    b64_path = f"{target_path}.b64"
    parent = Path(target_path).parent.as_posix()
    _run_internal(orig_run_action, f"mkdir -p {_sh_single_quote(parent)}", 15)
    _run_internal(
        orig_run_action,
        f"rm -f {_sh_single_quote(target_path)} {_sh_single_quote(b64_path)}",
        15,
    )
    for idx, chunk in enumerate(chunks):
        op = ">" if idx == 0 else ">>"
        _run_internal(
            orig_run_action,
            f"echo -n '{chunk}' {op} {_sh_single_quote(b64_path)}",
            15,
        )
    _run_internal(
        orig_run_action,
        f"base64 -d {_sh_single_quote(b64_path)} > {_sh_single_quote(target_path)} && rm -f {_sh_single_quote(b64_path)}",
        30,
    )
    return True


def render_l4_tool_footer(installed_tools: list[str] | None = None) -> str:
    return ""


def _format_l2_pretask_tag(telemetry: Any) -> str:
    """One-line, telemetry-backed L2 proof (hybrid fusion / sparse retrieval path)."""

    if telemetry is None:
        return ""
    m6 = getattr(telemetry, "module_6_hybrid", None) or {}
    if not isinstance(m6, dict) or not m6:
        return ""
    sc = m6.get("signal_counts") or {}
    parts: list[str] = []
    if isinstance(sc, dict):
        for k, v in sorted(sc.items()):
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n:
                parts.append(f"{k}={n}")
    fused = m6.get("fused_candidates") or []
    fused_n = len(fused) if isinstance(fused, list) else 0
    try:
        wall = int(m6.get("wall_ms") or 0)
    except (TypeError, ValueError):
        wall = 0
    attrs = [
        "layer=\"L2\"",
        "fusion=\"rrf\"",
        f"fused_candidates=\"{fused_n}\"",
        f"wall_ms=\"{wall}\"",
    ]
    if parts:
        attrs.append(f"signals=\"{' '.join(parts)}\"")
    return "<gt-pretask " + " ".join(attrs) + " />"


def _normalize_rel_path(path: str, config: GTRuntimeConfig) -> str:
    p = path.strip().strip("'\"")
    p = _path_relative_to_workspace(p, config)
    return _normalize_path(p)


def _same_repo_file(a: str, b: str, config: GTRuntimeConfig) -> bool:
    """True if two workspace-relative paths likely refer to the same file."""

    x = _normalize_rel_path(a, config)
    y = _normalize_rel_path(b, config)
    if x == y:
        return True
    if x.endswith("/" + y) or y.endswith("/" + x):
        return True
    bx, by = Path(x).name, Path(y).name
    return bx == by != "" and (x in y or y in x)


# Agent-friendly labels for internal edge resolution_method values.
# The agent understands structural relations (imports, calls, defines) but NOT
# indexer jargon (verified_unique, name_match, type_flow). This mapping is the
# ONLY place that translates indexer internals → agent-visible text; keeping it
# centralized prevents future leaks from ad-hoc f-strings.
_SCOPE_REASON_LABELS: dict[str, str] = {
    "same_file":                      "defined in same file",
    "import":                         "imported",
    "verified_unique":                "unique definition",
    "type_flow":                      "called via type inference",
    "name_match":                     "possible match (unverified)",
    "name_match_qualified_unresolved": "possible match (unverified)",
    "lsp":                            "verified by language server",
    "lsp_verified":                   "verified by language server",
    "route_match":                    "API route handler",
}


def _detect_scope(
    primary_file: str,
    config: GTRuntimeConfig,
    orig_run_action: Any,
) -> list[dict[str, str]]:
    """Detect multi-file scope from graph neighbors + same-directory siblings.

    Returns list of {file, reason, callers} for files connected to primary_file.
    Confidence-gated: edges >= 0.7. import/same_file edges are surfaced at full
    authority; name_match edges are relabeled to lower-confidence wording so a
    speculative name_match is never presented as a verified dependency.
    Same-dir siblings detected by matching method names in graph.db.
    """
    scope: list[dict[str, str]] = []
    seen: set[str] = set()
    primary_norm = _normalize_rel_path(primary_file, config)
    seen.add(primary_norm)

    _db = getattr(config, "_host_graph_db", "")
    if not _db or not os.path.exists(_db):
        # No host DB — try container query for basic scope detection
        if config.graph_db and orig_run_action:
            try:
                _scope_escaped = _escape_like(primary_norm).replace("'", "''")
                _scope_sql = (
                    f"SELECT DISTINCT nsrc.file_path FROM edges e "
                    f"JOIN nodes nt ON e.target_id = nt.id "
                    f"JOIN nodes nsrc ON e.source_id = nsrc.id "
                    f"WHERE nt.file_path LIKE '%{_scope_escaped}' ESCAPE '\\' "
                    f"AND e.type = 'CALLS' AND COALESCE(e.confidence, 0.5) >= 0.7 "
                    f"AND nsrc.file_path != nt.file_path LIMIT 10"
                )
                _scope_raw = _container_query(orig_run_action, config.graph_db, _scope_sql)
                if _scope_raw:
                    import json as _json_scope
                    try:
                        _scope_parsed = _json_scope.loads(_scope_raw) if isinstance(_scope_raw, str) else _scope_raw
                    except (ValueError, TypeError):
                        _scope_parsed = []
                    for _row in _scope_parsed:
                        _sf = _row[0] if isinstance(_row, (list, tuple)) else str(_row)
                        _sf_norm = _sf.replace("\\", "/").lstrip("./").lstrip("/")
                        if _sf_norm not in seen:
                            seen.add(_sf_norm)
                            scope.append({"file": _sf_norm, "reason": "calls functions here", "callers": "1+"})
                    print(f"[GT_META] detect_scope: container_query fallback found {len(scope)} neighbors", flush=True)
            except Exception as _sq_exc:
                print(f"[GT_META] detect_scope: container_query failed ({_sq_exc})", flush=True)
        return scope

    try:
        import sqlite3 as _sq
        conn = _sq.connect(_db)
        conn.row_factory = _sq.Row

        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        has_conf = "confidence" in cols
        has_res = "resolution_method" in cols

        # 1. Graph neighbors: files connected by confident edges (>= 0.7)
        conf_filter = "AND e.confidence >= 0.7" if has_conf else ""
        query = f"""
            SELECT DISTINCT n2.file_path, n2.name,
                   e.type{', e.confidence' if has_conf else ''}{', e.resolution_method' if has_res else ''}
            FROM nodes n1
            JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id)
            JOIN nodes n2 ON (n2.id = CASE WHEN e.source_id = n1.id THEN e.target_id ELSE e.source_id END)
            WHERE n1.file_path LIKE ? ESCAPE '\\' AND n2.file_path != n1.file_path
              AND n2.label IN ('Function', 'Method', 'Class')
              {conf_filter}
            ORDER BY {'e.confidence DESC,' if has_conf else ''} n2.file_path
            LIMIT 20
        """
        pnorm = primary_norm.replace("\\", "/").lstrip("/")
        rows = conn.execute(query, (f"%{_escape_like(pnorm)}",)).fetchall()
        for row in rows:
            fp = row["file_path"]
            fp_norm = fp.replace("\\", "/").lstrip("/")
            if fp_norm in seen or _is_test_path(fp_norm):
                continue
            seen.add(fp_norm)
            reason = "graph-connected"
            if has_res and row["resolution_method"]:
                _rmeth = row["resolution_method"]
                # Translate internal resolution_method to agent-friendly
                # language. The agent understands "imports", "calls", etc.
                # but NOT indexer jargon like "verified_unique" or
                # "name_match". Map every value to a structural relation
                # the agent can act on.
                reason = _SCOPE_REASON_LABELS.get(_rmeth, "graph-connected")
            scope.append({"file": fp_norm, "reason": reason, "callers": row["name"]})

        # 2. Same-directory siblings with matching method names
        primary_dir = str(Path(pnorm).parent)
        if primary_dir and primary_dir != ".":
            primary_methods = conn.execute(
                "SELECT DISTINCT name FROM nodes "
                "WHERE file_path LIKE ? ESCAPE '\\' AND label IN ('Function', 'Method')",
                (f"%{_escape_like(pnorm)}",),
            ).fetchall()
            method_names = {r["name"] for r in primary_methods}
            # Exclude very common names that would match everything
            method_names -= {"__init__", "__str__", "__repr__", "__eq__", "__hash__",
                             "setUp", "tearDown", "setup", "teardown", "main", "run"}
            if method_names:
                dir_files = conn.execute(
                    "SELECT DISTINCT file_path FROM nodes "
                    "WHERE file_path LIKE ? ESCAPE '\\' AND label IN ('Function', 'Method')",
                    (f"%{_escape_like(primary_dir)}/%",),
                ).fetchall()
                for df in dir_files:
                    dfp = df["file_path"].replace("\\", "/").lstrip("/")
                    if dfp in seen or _is_test_path(dfp) or dfp == pnorm:
                        continue
                    # Check if this file has any matching method names
                    file_methods = conn.execute(
                        "SELECT DISTINCT name FROM nodes "
                        "WHERE file_path = ? AND label IN ('Function', 'Method')",
                        (df["file_path"],),
                    ).fetchall()
                    file_method_names = {r["name"] for r in file_methods}
                    shared = method_names & file_method_names
                    if shared:
                        seen.add(dfp)
                        scope.append({
                            "file": dfp,
                            # A bare method-name collision is NOT a structural
                            # dependency (homonyms like extract/run/load recur
                            # across unrelated files). Agent-friendly: "shares
                            # <name>" tells the agent WHAT is shared without
                            # exposing indexer resolution terminology.
                            "reason": f"shares {', '.join(sorted(shared)[:3])}",
                            "callers": "",
                        })

        conn.close()
    except Exception as exc:
        print(f"[GT_META] scope detection error: {exc}", file=sys.stderr, flush=True)

    # Also check brief_candidates with suffix matching
    for cand in config.brief_candidates:
        cand_norm = _normalize_rel_path(cand, config) if hasattr(config, "workspace_root") else cand
        if cand_norm not in seen and not _is_test_path(cand_norm):
            if _same_repo_file(cand_norm, primary_norm, config):
                continue  # same as primary
            seen.add(cand_norm)

    _scope_cap = _dynamic_limit(config, 5)
    return scope[:_scope_cap]


def _classify_agent_state(config: GTRuntimeConfig) -> str:
    """Classify agent state from behavioral metrics.

    Returns one of:
      CONVERTED — agent is editing/testing, GT should stay quiet
      PRODUCTIVE_SILENT — exploring new ground, GT should stay quiet
      HARMFUL_SILENT — stuck, GT should rescue
    """
    ac = config.action_count
    last_edit = config._source_edit_actions[-1] if config._source_edit_actions else 0
    last_test = config._test_actions[-1] if config._test_actions else 0
    last_gt = config._last_gt_action
    has_edits = len(config._source_edit_actions) > 0

    # A: Converted — recent edit or testing after edit
    if has_edits and ac - last_edit < 15:
        return "CONVERTED"
    if has_edits and last_test > last_edit and ac - last_test < 10:
        return "CONVERTED"

    # B: Productive exploration — reading new files
    recent_reads = config._read_history[-10:] if config._read_history else []
    unique_recent = len(set(recent_reads)) if recent_reads else 0
    total_unique_reads = len(config.viewed_files)
    # Productive if still discovering new files relative to total
    if recent_reads and len(recent_reads) >= 3 and unique_recent / len(recent_reads) > 0.6:
        return "PRODUCTIVE_SILENT"

    # C: Harmful silence — dynamic, confidence-gated, hybrid (≥3 signals)
    #
    # 7 independent signals. Each fires on a different behavioral dimension.
    # The threshold (3) is the MINIMUM for the hybrid pillar. The signals
    # are WEIGHTED by confidence — a GT-delivered verified file that the
    # agent ignores counts double (confidence-gated escalation).
    stuck_signals = 0

    # Signal 1: No edits at all
    if not has_edits:
        stuck_signals += 1

    # Signal 2: Long gap since last GT evidence delivery
    if ac - last_gt > 20:
        stuck_signals += 1

    # Signal 3: Excessive searching without editing
    if config._search_count_since_edit > 10:
        stuck_signals += 1

    # Signal 4: High file-revisit ratio (going in circles)
    if recent_reads and unique_recent / len(recent_reads) < 0.4:
        stuck_signals += 1

    # Signal 5: No reads at all = blind grep/run loops
    if not recent_reads and ac > 20:
        stuck_signals += 1

    # Signal 6: Running tests without ever editing source
    if config._test_actions and not has_edits and len(config._test_actions) > 3:
        stuck_signals += 1

    # Signal 7 (DYNAMIC + CONFIDENCE-GATED): GT delivered VERIFIED evidence
    # for a file, agent viewed it, but hasn't edited ANY code file since.
    # This is the signal that detects "agent found gold but walked away."
    # Counts DOUBLE because it's backed by graph confidence, not just time.
    # The threshold adapts: fires earlier when GT confidence is higher.
    _gt_delivered_files = getattr(config, "_gt_delivered_evidence_files", {})
    if _gt_delivered_files and not has_edits:
        # How many turns since the agent viewed a GT-evidenced file?
        _best_delivered_action = max(_gt_delivered_files.values()) if _gt_delivered_files else 0
        _turns_since_evidence = ac - _best_delivered_action
        # Dynamic threshold: if GT delivered at high confidence (L3b with
        # markers), intervene after 10 turns. Standard: 20 turns.
        _evidence_threshold = 10 if config._l3b_fire_count > 0 else 20
        if _turns_since_evidence > _evidence_threshold:
            stuck_signals += 1  # confidence-gated (needs other signals too)

    # Signal 8 (DYNAMIC): past 30% of budget with 0 edits
    if ac > 0.3 * config.max_iter and not has_edits:
        stuck_signals += 1

    if stuck_signals >= 3:
        return "HARMFUL_SILENT"

    return "PRODUCTIVE_SILENT"


def _build_rescue_payload(config: GTRuntimeConfig, rescue_level: int = 0) -> str:
    """Build escalating rescue message from cached evidence.

    Level 0 (soft): confirmed file + evidence + nudge
    Level 1 (directed): specific function + test command
    Level 2 (final): smallest edit OR targeted test, do not edit unrelated
    """
    # Identify the confirmed file — prefer the most-recently-viewed
    # GT-evidenced file over a stale consensus. If the agent has been
    # reading OTHER files (not in consensus scope), the consensus may
    # point at the wrong file. The most-recently-viewed GT-evidenced
    # file reflects what the agent is ACTUALLY working on, which is
    # a stronger signal for rescue redirection.
    top_cand = ""
    top_base = ""
    # Check if agent has viewed files with GT evidence that are NOT
    # in the consensus scope — if so, prefer the latest of those.
    if config._gt_delivered_evidence_files:
        _consensus_set = set(config._consensus_scope) if config._consensus_scope else set()
        _non_consensus_evidence = {
            f: t for f, t in config._gt_delivered_evidence_files.items()
            if f not in _consensus_set
        }
        if _non_consensus_evidence:
            # Agent explored beyond consensus — use the most recent
            top_cand = max(_non_consensus_evidence,
                           key=_non_consensus_evidence.get)
            top_base = os.path.basename(top_cand)
    # Fall back to consensus scope if no divergent evidence
    if not top_base and config._consensus_scope:
        top_cand = config._consensus_scope[0]
        top_base = os.path.basename(top_cand)
    elif not top_base and config._consensus_confirmed:
        top_cand = next(iter(config._consensus_confirmed))
        top_base = os.path.basename(top_cand)
    elif not top_base and config._gt_delivered_evidence_files:
        top_cand = max(config._gt_delivered_evidence_files,
                       key=config._gt_delivered_evidence_files.get)
        top_base = os.path.basename(top_cand)

    if not top_base:
        return ""

    # Get cached evidence
    top_evidence = ""
    if config.evidence_cache:
        top_key = next(iter(config.evidence_cache))
        top_evidence = config.evidence_cache[top_key]

    # Scope files not yet edited
    unedited = []
    if config._consensus_scope:
        unedited = [
            os.path.basename(sf) for sf in config._consensus_scope
            if sf not in config._consensus_scope_edited
        ]

    # De-prescribed (C2; SWE-PRM NeurIPS 2025): the rescue states confirmation,
    # evidence, and scope as FACTS — no "Consider starting" / "Edit X" / "Run
    # tests" / "Make the smallest edit" / "Do not edit" commands. The agent decides.
    if rescue_level == 0:
        parts = [f"{top_base} was confirmed earlier."]
        if top_evidence:
            parts.append(f"Key evidence: {top_evidence}")
        if unedited:
            parts.append(f"Scope: {', '.join(unedited[:3])}")
        return "[GT] " + " ".join(parts) + "\n"

    elif rescue_level == 1:
        parts = [f"Highest-confidence target: {top_base}."]
        if top_evidence:
            parts.append(top_evidence)
        if unedited:
            parts.append(f"Also in scope: {', '.join(unedited[:2])}")
        return "[GT] " + " ".join(parts) + "\n"

    else:  # level 2 — final
        return (
            f"[GT] Highest-confidence target: {top_base}. Scope is limited to the "
            f"listed files; a targeted verification command is available.\n"
        )


def _deliver_or_trace(
    obs: Any,
    payload: str,
    config: "GTRuntimeConfig",
    layer: str,
    file_path: str,
    *,
    prepend: bool = True,
) -> Any:
    """Delivery invariant: evidence either reaches agent or gets explicit trace.

    Contract (Lost-in-the-Middle research: 30% accuracy gain for FRONT position):
    - payload has evidence markers → PREPEND and log agent_visible=true
    - payload empty → log ROUTER_EMIT_HOOK_EMPTY
    - payload lacks markers → log ROUTER_EMIT_MARKER_MISMATCH with first 300 chars
    - never silently return obs after router_emit=True
    """
    telemetry_layer = {
        "l3": "L3",
        "l3b": "L3b",
        "L4_auto_query": "L4",
        "l4_auto_query": "L4",
    }.get(layer, layer)
    if not payload or not payload.strip():
        _emit_structured_event(
            config, telemetry_layer, "delivery",
            emitted=False, suppressed=True, suppression_reason="empty_payload",
            file_path=file_path, trigger_event=layer, selected_path=file_path,
            producer_status="empty", delivery_status="not_delivered",
            payload_chars=0, agent_visible=False,
        )
        print(
            f"[GT_TRACE] {layer}_delivery status=ROUTER_EMIT_HOOK_EMPTY "
            f"file={file_path} ac={config.action_count}",
            flush=True,
        )
        return obs

    if not has_gt_evidence(payload, layer):
        _emit_structured_event(
            config, telemetry_layer, "delivery",
            emitted=False, suppressed=True, suppression_reason="marker_mismatch",
            rendered_text=payload[:2000], file_path=file_path,
            trigger_event=layer, selected_path=file_path,
            producer_status="produced", delivery_status="not_delivered",
            payload_chars=len(payload), agent_visible=False,
        )
        print(
            f"[GT_TRACE] {layer}_delivery status=ROUTER_EMIT_MARKER_MISMATCH "
            f"file={file_path} payload_len={len(payload)} "
            f"first_300={payload[:300]!r} ac={config.action_count}",
            flush=True,
        )
        return obs

    config._last_gt_action = config.action_count
    if prepend:
        obs = prepend_observation(obs, payload)
    else:
        obs = append_observation(obs, payload)

    _emit_structured_event(
        config, telemetry_layer, "delivery",
        emitted=True, suppressed=False, rendered_text=payload[:2000],
        file_path=file_path, trigger_event=layer, selected_path=file_path,
        producer_status="produced", delivery_status="delivered",
        payload_chars=len(payload), agent_facing_marker=layer,
        agent_visible=True,
    )

    print(
        f"[GT_TRACE] {layer}_delivery status=DELIVERED "
        f"file={file_path} payload_len={len(payload)} "
        f"agent_visible=true ac={config.action_count}",
        flush=True,
    )
    return obs


def _path_covered_by_validation(path: str, config: GTRuntimeConfig) -> bool:
    for v in config.verified_checks:
        if _same_repo_file(path, v, config):
            return True
    return False


def register_gt_validate_paths(command: str, config: GTRuntimeConfig) -> None:
    """Record paths from completed gt_validate shell lines (observation path)."""

    for m in _GT_VALIDATE_ARG_RE.finditer(command):
        raw = m.group(1).strip().strip("'\"")
        if not raw or raw.startswith("-"):
            continue
        rel = _normalize_rel_path(raw, config)
        if rel:
            config.verified_checks.add(rel)


def _l5_unresolved_paths(config: GTRuntimeConfig) -> list[str]:
    pending = sorted(config.pending_checks)
    return [p for p in pending if not _path_covered_by_validation(p, config)]


def render_l5_advisory(config: GTRuntimeConfig) -> str:
    edited = sorted(config.edited_files)
    pending = sorted(config.pending_checks)
    unresolved = _l5_unresolved_paths(config)
    explored_not_edited = sorted(config.viewed_files - config.edited_files)
    if not edited and not pending and not explored_not_edited:
        return ""

    # L5: Detect stuck patterns (independent of L1 candidates — never names specific files)
    redirect_msg = ""
    scaffold_creates = sum(1 for f in edited if any(
        Path(f).name.startswith(p) for p in ("reproduce_", "repro_", "debug_", "test_fix_", "scratch_", "temp_")
    ))
    edit_counts: dict[str, int] = {}
    for f in edited:
        edit_counts[f] = edit_counts.get(f, 0) + 1
    edit_loops = any(c >= 3 for c in edit_counts.values())

    if scaffold_creates >= 3:
        candidates = sorted(config.brief_candidates)[:3]
        if candidates:
            redirect_msg = (
                "\n[GT_ADVISORY] {} scaffolding files created; 0 durable source edits. "
                "Graph-ranked source candidates: {}".format(scaffold_creates, ", ".join(candidates))
            )
        else:
            redirect_msg = (
                "\n[GT_ADVISORY] {} scaffolding files created; 0 durable source edits. "
                "GT could not rank source candidates with confidence.".format(scaffold_creates)
            )
    elif edit_loops:
        looped = [f for f, c in edit_counts.items() if c >= 3]
        candidates = sorted(config.brief_candidates)[:3]
        if candidates:
            candidate_msg = " Graph-ranked source candidates: {}".format(", ".join(candidates))
        else:
            candidate_msg = " GT could not rank source candidates with confidence."
        redirect_msg = (
            "\n[GT_ADVISORY] {} edited {} times; diff not converging.{}".format(
                looped[0], edit_counts[looped[0]], candidate_msg
            )
        )

    lines = [
        "[GT_GATE] Pre-submit review:",
        f"  Files edited: {len(edited)}",
        f"  Pending checks: {len(pending)} ({len(unresolved)} unresolved)",
    ]
    if unresolved:
        unresolved_summaries: list[tuple[str, str]] = []
        seen = set()
        for p, s in config.pending_summaries:
            if p in unresolved and p not in seen:
                unresolved_summaries.append((p, s))
                seen.add(p)
        for path, summary in unresolved_summaries[:3]:
            lines.append(f"  WARNING {path}: {summary[:150]}")
    if explored_not_edited:
        lines.append(
            "  Files explored but not edited: " + ", ".join(explored_not_edited[:3])
        )
    body = "\n".join(lines) + redirect_msg
    return (
        f'<gt-advisory layer="L5" pending_count="{len(pending)}" unresolved_count="{len(unresolved)}">\n'
        + body
        + "\n</gt-advisory>"
    )


def _compute_has_real_evidence(layer: str, ev_type: str, gt_sent: str) -> bool:
    if not gt_sent or not gt_sent.strip():
        return False
    s = gt_sent.strip()
    if ev_type == "GT_OK":
        return False
    if layer == "L1":
        return "Traceback" not in s and "Error" not in s[:200] and len(s) > 50
    if layer == "L5":
        return "[GT_GATE]" in s or "[GT_ADVISORY]" in s
    if layer == "L6":
        return ev_type == "reindex_ok"
    return any(tag in s for tag in (
        "[GT_CHANGE]", "[GT_CONTRACT]", "[GT_PATTERN]",
        "[GT_STRUCTURAL]", "[GT_SEMANTIC]", "[GT_STATUS]",
        "[VERIFIED]", "[POSSIBLE]", "[GT_CALLER]",
        "Called by:", "Calls into:", "Imported by:",
    ))


def _log_gt_interaction(
    config: GTRuntimeConfig,
    layer: str,
    trigger: str,
    ev_type: str,
    gt_sent: str,
    agent_action_before: str = "",
    event_id: str = "",
    parent_event_id: str = "",
    next_action_type: str = "",
    next_action_file: str = "",
    next_action_command: str = "",
    next_action_test: str = "",
) -> None:
    """Record a GT→agent interaction for post-run analysis.

    Write-through: every call immediately appends one JSON line to a
    per-task file ``/tmp/gt_interactions_<task_id>.jsonl``.  The in-memory
    list + instance_ref flush are kept for backward compatibility but the
    file is the primary mechanism (survives max_iter timeout, finish-event
    misses, and instance_ref injection failures).

    Enhanced fields:
      - ``gt_sent``: full text GT injected (NOT truncated for L1 briefs)
      - ``agent_action_before``: what the agent was doing when GT fired
      - ``agent_action_after``: filled in on the NEXT action (see below)

    On each call, checks if the PREVIOUS entry in config.interaction_log is
    missing ``agent_action_after``.  If so, backfills it with the current
    trigger, creating a GT→agent→response chain for post-run analysis.
    """
    # Backfill agent_action_after on the PREVIOUS entry
    if config.interaction_log:
        prev = config.interaction_log[-1]
        if not prev.get("agent_action_after"):
            prev["agent_action_after"] = trigger
            # Also update the file — append a correction line
            try:
                correction = {
                    "type": "_backfill",
                    "target_iter": prev.get("iter"),
                    "target_layer": prev.get("layer"),
                    "agent_action_after": trigger,
                }
                _interactions_path = _metrics_path(config, "interactions")
                with open(_interactions_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(correction) + "\n")
            except Exception:
                pass

    entry = {
        "timestamp": time.time(),
        "iter": config.action_count,
        "layer": layer,
        "trigger": trigger,
        "type": ev_type,
        "gt_sent": gt_sent,
        "gt_sent_bytes": len(gt_sent.encode("utf-8", errors="replace")),
        "gt_sent_tokens": len(gt_sent.split()) if gt_sent else 0,
        "has_real_evidence": _compute_has_real_evidence(layer, ev_type, gt_sent),
        "agent_action_before": agent_action_before,
        "agent_action_after": "",
        "event_id": event_id,
        "parent_event_id": parent_event_id,
        "next_action_type": next_action_type,
        "next_action_file": next_action_file,
        "next_action_command": next_action_command,
        "next_action_test": next_action_test,
    }
    config.interaction_log.append(entry)

    # Write-through to file — primary persistence mechanism
    _instance_id = getattr(config, "_meta_instance_id", "global")
    _meta_path = f"/tmp/gt_meta_{_instance_id}.jsonl"
    try:
        with open(_meta_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
    # Also write to per-task interactions path
    try:
        _interactions_path = _metrics_path(config, "interactions")
        with open(_interactions_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    # Belt-and-suspenders: push to instance_ref if available
    ref = config.instance_ref
    if ref is not None:
        try:
            if isinstance(ref, dict):
                ref["gt_interactions"] = list(config.interaction_log)
            else:
                setattr(ref, "gt_interactions", list(config.interaction_log))
        except Exception:
            pass


def _emit_structured_event(
    config: GTRuntimeConfig,
    layer: str,
    event_type: str,
    *,
    emitted: bool = True,
    suppressed: bool = False,
    suppression_reason: str | None = None,
    rendered_text: str = "",
    evidence_items: list[dict] | None = None,
    next_action_type: str | None = None,
    next_action_file: str | None = None,
    next_action_test: str | None = None,
    file_path: str | None = None,
    parent_event_id: str | None = None,
    hook_output: str = "",
    verification_kind: str | None = None,
    event_bucket: str | None = None,
    file_kind: str | None = None,
    check_kind: str | None = None,
    verification_strength: str | None = None,
    confidence_level: str | None = None,
    confidence_score: float | None = None,
    confidence_basis: str | None = None,
    trigger_event: str | None = None,
    selected_path: str | None = None,
    producer_status: str | None = None,
    delivery_status: str | None = None,
    payload_chars: int | None = None,
    agent_facing_marker: str | None = None,
    agent_visible: bool | None = None,
) -> str | None:
    """Emit a GTLayerEvent if GT_STRUCTURED_EVENTS is enabled. Returns event_id or None."""
    writer = getattr(config, "_telemetry_writer", None)
    if writer is None:
        return None
    try:
        from groundtruth.telemetry.schemas import GTLayerEvent

        items = evidence_items or []
        if not items and "__GT_STRUCTURED__" in hook_output:
            parts = hook_output.split("__GT_STRUCTURED__", 1)
            if len(parts) == 2:
                try:
                    items = json.loads(parts[1].strip().splitlines()[0])
                except Exception:
                    pass

        event = GTLayerEvent(
            layer=layer,
            event_type=event_type,
            eligible=True,
            emitted=emitted,
            suppressed=suppressed,
            suppression_reason=suppression_reason,
            iter=config.action_count,
            max_iter=config.max_iter,
            rendered_text=rendered_text[:2000] if rendered_text else None,
            evidence_items=items,
            next_action_type=next_action_type,
            next_action_file=next_action_file,
            verification_kind=verification_kind,
            next_action_test=next_action_test,
            file_path=file_path,
            parent_event_id=parent_event_id,
            event_bucket=event_bucket,
            file_kind=file_kind,
            check_kind=check_kind,
            verification_strength=verification_strength,
            confidence_level=confidence_level,
            confidence_score=confidence_score,
            confidence_basis=confidence_basis,
            trigger_event=trigger_event,
            selected_path=selected_path,
            producer_status=producer_status,
            delivery_status=delivery_status,
            payload_chars=payload_chars if payload_chars is not None else (len(rendered_text) if rendered_text else None),
            agent_facing_marker=agent_facing_marker,
            agent_visible=agent_visible,
        )
        return writer.emit_layer_event(event)
    except Exception as exc:
        print(f"[GT_META] structured event emission failed: {exc}", flush=True)
        return None


def _get_edge_detail_in_container(
    orig_run_action: Any, graph_db: str, target_file: str, caller_file: str,
) -> tuple[str, int, float, str] | None:
    """Query graph.db INSIDE Docker for edge detail. Returns (symbol, line, confidence, method) or None.

    Runs a tiny Python script inside the container — same as hooks do.
    Avoids base64 download corruption.
    """
    target_norm = target_file.replace("\\", "/").lstrip("./")
    caller_norm = caller_file.replace("\\", "/").lstrip("./")
    script = (
        f"python3 -c \""
        f"import sqlite3,json; "
        f"c=sqlite3.connect('{graph_db}'); "
        f"r=c.execute("
        f"'SELECT nt.name,nt.start_line,e.confidence,e.resolution_method "
        f"FROM nodes nt JOIN edges e ON e.target_id=nt.id AND e.type=\\'CALLS\\' "
        f"JOIN nodes nsrc ON e.source_id=nsrc.id "
        f"WHERE nt.file_path=? AND nsrc.file_path=? "
        f"ORDER BY e.confidence DESC LIMIT 1',"
        f"('{target_norm}','{caller_norm}')).fetchone(); "
        f"print(json.dumps(list(r)) if r else 'null')"
        f"\""
    )
    try:
        obs = _run_internal(orig_run_action, script, timeout=10)
        if not obs or not obs.strip() or obs.strip() == "null":
            print(f"[GT_META] _get_edge_detail: NO EDGE {target_norm} <- {caller_norm}", flush=True)
            return None
        row = json.loads(obs.strip().splitlines()[-1])
        if row and len(row) >= 4:
            print(f"[GT_META] _get_edge_detail: {target_norm} <- {caller_norm} = {row[0]}:{row[1]} conf={row[2]} method={row[3]}", flush=True)
            return (row[0], row[1] or 0, row[2] or 0.5, row[3] or "unknown")
        return None
    except Exception as e:
        print(f"[GT_META] _get_edge_detail ERROR: {e}", flush=True)
        return None


def _emit_agent_event(config: GTRuntimeConfig, action: Any, event: Any, file_path: str = "") -> None:
    """Decision 34: Emit GTAgentEvent at every action boundary."""
    if os.environ.get("GT_DEEP_LAYER_GROUNDED_METRICS", "0") != "1":
        return
    writer = getattr(config, "_telemetry_writer", None)
    if writer is None:
        return
    try:
        from groundtruth.telemetry.schemas import GTAgentEvent
        from groundtruth.trajectory.event_classifier import classify_event_bucket, classify_file_kind

        act_cls = _action_class(action)
        command = ""
        if hasattr(action, "command"):
            command = str(action.command or "")[:200]

        is_finish = act_cls in ("AgentFinishAction", "FinishAction")
        bucket = classify_event_bucket(
            act_cls, command=command, is_finish=is_finish,
        )
        fk = classify_file_kind(file_path) if file_path else "UNKNOWN_FILE"

        import uuid
        agent_event = GTAgentEvent(
            agent_action_id=uuid.uuid4().hex[:16],
            iter=config.action_count,
            event_bucket=bucket,
            agent_event_type=act_cls,
            file_path=file_path or None,
            file_kind=fk if file_path else None,
            command=command or None,
            max_iter=config.max_iter,
        )
        writer.emit_agent_event(agent_event)
    except Exception:
        pass


def _feed_gt_next_action_to_l5(config: GTRuntimeConfig, next_action_type: str, next_action_file: str = "") -> None:
    """Decision 34: Feed L3/L3b next_action into L5 state for witness tracking."""
    if not next_action_type or next_action_type in ("NONE", "NONE_UNVERIFIABLE"):
        return
    _l5_gov = getattr(config, "_l5_governor", None)
    if _l5_gov is None:
        return
    try:
        _l5_gov.state.record_gt_next_action(next_action_type, next_action_file, config.action_count)
    except Exception:
        pass


def _emit_belief_event(
    config: GTRuntimeConfig,
    file_path: str,
    new_status: str,
    reason: str,
    source_event_id: str = "",
    previous_status: str | None = None,
    score: float | None = None,
) -> None:
    """Emit a GTBeliefEvent if writer is active."""
    writer = getattr(config, "_telemetry_writer", None)
    if writer is None:
        return
    try:
        from groundtruth.telemetry.schemas import GTBeliefEvent
        event = GTBeliefEvent(
            file_path=file_path,
            new_status=new_status,
            reason=reason,
            source_event_id=source_event_id or "",
            previous_status=previous_status,
            new_score=score,
            iter=config.action_count,
        )
        writer.emit_belief_event(event)
    except Exception:
        pass


def _ensure_agent_state(config: GTRuntimeConfig) -> Any:
    """Lazy-initialize the FINAL_ARCH_V2 Layer 2 AgentState for this task.

    Idempotent: subsequent calls return the same object. Failures are swallowed
    so the wrapper keeps working if the import path is missing in a partial
    install. The legacy ``config._pending_next_actions`` list is preserved and
    mirrored from here on.
    """
    if config._agent_state is not None:
        return config._agent_state
    try:
        from groundtruth.state.agent_state import AgentState
        repo_root = os.environ.get("GT_REPO_ROOT", WORKSPACE_ROOT)
        state = AgentState.load_or_create(
            task_id=config._meta_instance_id or "global",
            max_iterations=config.max_iter or 100,
            repo_root=repo_root,
        )
        # Sync the wrapper's already-populated fields into the state once.
        if config.brief_candidates and not state.brief_candidates:
            state.set_brief_candidates(config.brief_candidates)
        state.set_iteration(config.action_count, config.max_iter or 100)
        config._agent_state = state
        return state
    except Exception as exc:
        print(f"[GT_META] AgentState init failed: {type(exc).__name__}: {exc}", flush=True)
        return None


def _register_pending_next_action(config: GTRuntimeConfig, event_id: str, next_action_type: str, next_action_file: str = "") -> None:
    """Register a GT next_action for online tracking. Only tracks actionable types.

    Writes to the legacy ``config._pending_next_actions`` list AND mirrors into
    the canonical AgentState pending_suggestions (FINAL_ARCH_V2 Layer 2).
    """
    if not next_action_type or next_action_type in ("", "NONE", "NONE_UNVERIFIABLE", None):
        return
    if os.environ.get("GT_L5_STRUCTURAL_UNVERIFIED", "0") != "1":
        return
    config._pending_next_actions.append({
        "event_id": event_id or "",
        "next_action_type": next_action_type,
        "next_action_file": next_action_file,
        "iter_emitted": config.action_count,
        "checked_count": 0,
        "followed": False,
    })
    state = _ensure_agent_state(config)
    if state is not None:
        try:
            state.set_iteration(config.action_count, config.max_iter or 100)
            state.register_pending_suggestion(
                event_id=event_id or "",
                next_action_type=next_action_type,
                next_action_file=next_action_file,
                ttl_actions=3,
            )
        except Exception as exc:
            print(f"[GT_META] AgentState register failed: {type(exc).__name__}: {exc}", flush=True)


def _is_agent_stuck(config: Any) -> bool:
    """Proven-stuck signal from the obs-fingerprint repeat (STUCK_COMPAT) history.

    The action handler appends (action_fp, raw_obs_hash) pairs to
    ``config._stuck_compat_history``. The agent is "proven stuck" when it has been
    re-issuing substantially the same action — i.e. the most recent action
    fingerprint appears 2+ times in the recent window, OR a STUCK_COMPAT skip has
    already fired. This is the SAME fingerprint structure the handler uses to feed
    OpenHands' stuck detector, so the gate keys off a real runtime condition, not a
    hardcoded iteration count.
    """
    hist = getattr(config, "_stuck_compat_history", None) or []
    if getattr(config, "_stuck_compat_skip_count", 0) > 0:
        return True
    if len(hist) < 2:
        return False
    recent = hist[-8:]
    last_action_fp = recent[-1][0]
    repeats = sum(1 for pair in recent if pair[0] == last_action_fp)
    return repeats >= 2


def _resolve_unexamined_symbol(config: Any, naf: str) -> str | None:
    """Resolve the SPECIFIC symbol/relation connecting an edited file to ``naf``.

    Returns a concrete "caller() -> callee()" relation string when a verified CALLS
    edge links a symbol in ``naf`` to a symbol in one of the recently edited files,
    or None when no concrete relation can be named (then the caller stays silent —
    correct-or-quiet; never emit a vague file-only advisory).
    """
    if not naf:
        return None
    db = getattr(config, "_host_graph_db", "") or getattr(config, "graph_db", "") or ""
    if not db or not os.path.exists(db):
        return None
    edited = list(getattr(config, "edited_files", set()) or set())
    if not edited:
        return None
    naf_like = "%" + naf.replace("\\", "/").lstrip("/")
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    try:
        conn.execute("PRAGMA busy_timeout=3000")
        for ef in edited[-3:]:
            ef_like = "%" + str(ef).replace("\\", "/").lstrip("/")
            # A verified CALLS edge where the caller lives in naf and the callee
            # lives in the edited file (the unexamined caller→edited relation), or
            # vice versa (edited symbol calls into naf).
            row = conn.execute(
                "SELECT nsrc.name, ntgt.name "
                "FROM edges e "
                "JOIN nodes nsrc ON e.source_id = nsrc.id "
                "JOIN nodes ntgt ON e.target_id = ntgt.id "
                "WHERE e.type = 'CALLS' AND COALESCE(e.confidence, 0.5) >= 0.9 "
                "AND e.resolution_method IN ('same_file','import','lsp_verified') "
                "AND ((nsrc.file_path LIKE ? AND ntgt.file_path LIKE ?) "
                "  OR (nsrc.file_path LIKE ? AND ntgt.file_path LIKE ?)) "
                "LIMIT 1",
                (naf_like, ef_like, ef_like, naf_like),
            ).fetchone()
            if row and row[0] and row[1]:
                return f"{row[0]}() → {row[1]}()"
        return None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _l5_advisory_message(config: Any, naf: str) -> str | None:
    """Build the L5 "unexamined relation" advisory — or None to stay silent.

    Two hard gates (correct-or-quiet):
      1. The agent must be in a proven-stuck state (_is_agent_stuck). Firing after
         the agent has already self-localized is redundant noise.
      2. The advisory must name a SPECIFIC symbol/relation, not just a file. If no
         concrete relation resolves, stay silent.

    Diagnostic, not prescriptive (SWE-PRM NeurIPS 2025, arXiv 2509.02360):
    action-prescriptive feedback lowered resolution; state the verifiable
    observation and let the agent self-correct.
    """
    if not _is_agent_stuck(config):
        return None
    relation = _resolve_unexamined_symbol(config, naf)
    if not relation:
        return None
    return (
        f"[GT L5: Unexamined relation] A verified call relation {relation} "
        f"involving {naf} has not been examined. It may be relevant to the edit."
    )


def _check_pending_next_actions(config: GTRuntimeConfig, current_action_file: str = "", current_action_type: str = "", obs: Any = None) -> Any:
    """Check pending next_actions against agent's real action. Returns obs (possibly with L5b appended).

    Context budget rule (Decision 34 §12): when GT_L5_GOKU_EVENTS=1, this old tracker
    only logs structured events — it does NOT inject into agent context. Goku handles
    injections with its own band + confidence + cap gates. This prevents double-firing.
    """
    if not config._pending_next_actions:
        return obs
    goku_active = os.environ.get("GT_L5_GOKU_EVENTS", "1") == "1"
    # Mirror the agent action into the FINAL_ARCH_V2 Layer 2 AgentState before
    # the legacy list is walked, so the canonical state stays in sync. The
    # ``process_agent_action`` call drives status transitions there; the legacy
    # loop below still owns the rendering and structured-event emission paths.
    state = _ensure_agent_state(config)
    if state is not None:
        try:
            state.set_iteration(config.action_count, config.max_iter or 100)
            state.process_agent_action(action_file=current_action_file, action_type=current_action_type)
        except Exception as exc:
            print(f"[GT_META] AgentState process_action failed: {type(exc).__name__}: {exc}", flush=True)
    expired: list[int] = []
    for i, pending in enumerate(config._pending_next_actions):
        pending["checked_count"] += 1
        pf = pending.get("next_action_file", "")
        if pf and current_action_file and (pf in current_action_file or current_action_file in pf):
            pending["followed"] = True
        if pending["checked_count"] >= 3:
            if not pending["followed"]:
                # Always emit structured event (for telemetry)
                l5_eid = _emit_structured_event(
                    config, "L5", "ignored_next_action",
                    parent_event_id=pending["event_id"],
                    next_action_type=pending["next_action_type"],
                    next_action_file=pending.get("next_action_file"),
                    emitted=not goku_active,
                    suppressed=goku_active,
                    suppression_reason="goku_handles_injection" if goku_active else None,
                )
                nat = pending["next_action_type"]
                naf = pending.get("next_action_file", "")

                # L5B-INV-1: Cap at 2 L5b firings per task (noise control).
                _l5b_fire_count = getattr(config, "_l5b_injection_count", 0)
                if _l5b_fire_count >= 2:
                    expired.append(i)
                    continue
                # L5B-INV-2: Only suggest files in brief_candidates.
                _bc = getattr(config, "brief_candidates", set())
                if naf and _bc and not any(naf in c or c in naf for c in _bc):
                    expired.append(i)
                    continue
                # L5B-INV-3: Same file never suggested twice.
                _l5b_seen = getattr(config, "_l5b_suggested_files", set())
                if naf and naf in _l5b_seen:
                    expired.append(i)
                    continue

                # Inject into agent context if Goku is NOT active,
                # OR if agent has done N actions with zero source edits
                # (action-based gate instead of ratio cliff).
                _last_src_edit = getattr(config, "_last_source_edit_iter", 0)
                _first_src_edit = config._iter_state.get("iter_to_first_source_edit") or config.action_count
                _actions_since_edit = config.action_count - max(_first_src_edit, _last_src_edit)
                _no_edit_threshold = max(10, int((config.max_iter or 100) * 0.15))
                if not goku_active or _actions_since_edit >= _no_edit_threshold:
                    # TASK #46 gate: the advisory fires ONLY when the agent is in a
                    # proven-stuck state AND a SPECIFIC symbol/relation can be named
                    # (not just a file). Firing vaguely after the agent already
                    # self-localized was redundant noise. _l5_advisory_message
                    # returns None -> stay silent (correct-or-quiet).
                    msg = _l5_advisory_message(config, naf)
                    if msg is None:
                        _emit_structured_event(
                            config, "L5b", "suppressed_unexamined_relation",
                            parent_event_id=l5_eid, suppressed=True,
                            suppression_reason="not_stuck_or_no_specific_symbol",
                            next_action_type=nat, next_action_file=naf,
                        )
                        expired.append(i)
                        continue
                    try:
                        from groundtruth.trajectory.hooks import L5bSafetyChecker
                        ratio = config.action_count / max(config.max_iter, 1)
                        is_safe, reason = L5bSafetyChecker.validate(msg, ratio)
                    except Exception:
                        is_safe, reason = True, None
                    if is_safe and obs is not None:
                        l5b_eid = _emit_structured_event(
                            config, "L5b", "intervention_ignored_next_action",
                            parent_event_id=l5_eid, rendered_text=msg,
                            next_action_type=nat, next_action_file=naf,
                        )
                        obs = append_observation(obs, f"\n\n{msg}\n")
                        config._l5b_injection_count = getattr(config, "_l5b_injection_count", 0) + 1
                        if naf:
                            if not hasattr(config, "_l5b_suggested_files"):
                                config._l5b_suggested_files = set()
                            config._l5b_suggested_files.add(naf)
                        _log_gt_interaction(
                            config, "L5", "ignored_next_action", "advisory", msg,
                            event_id=l5b_eid or "", parent_event_id=l5_eid or "",
                            next_action_type=nat, next_action_file=naf,
                        )
                    elif not is_safe:
                        _l5b_blk_eid = _emit_structured_event(
                            config, "L5b", "blocked_by_safety",
                            parent_event_id=l5_eid, suppressed=True,
                            suppression_reason=reason,
                        )
                        _log_gt_interaction(config, "L5b", "ignored_next_action", "blocked", f"[blocked: {reason}]", event_id=_l5b_blk_eid or "")
            expired.append(i)
    for i in reversed(expired):
        config._pending_next_actions.pop(i)
    return obs


def _flush_interaction_log(config: GTRuntimeConfig, instance_ref: Any) -> None:
    """Write interaction log to instance for artifact pull."""
    if not config.interaction_log:
        return
    if instance_ref is not None:
        try:
            if isinstance(instance_ref, dict):
                instance_ref["gt_interactions"] = config.interaction_log
            else:
                setattr(instance_ref, "gt_interactions", config.interaction_log)
        except Exception:
            pass


def _is_scaffold_name(filename: str) -> bool:
    """Return True if filename (basename only) matches a known scaffold prefix."""
    base = filename.rsplit("/", 1)[-1]
    return base.startswith(SCAFFOLDING_PREFIXES)


# ---------------------------------------------------------------------------
# FINAL_ARCH_V2 GT_ROUTER_V2 path — three modes: off / shadow / live.
# ---------------------------------------------------------------------------

# Mode semantics:
#   off    — router never instantiated; legacy paths unchanged (default).
#   shadow — router runs in parallel; emissions logged to gt_interactions but
#            NOT appended to the agent observation. Legacy paths unchanged.
#            Used to compare router decisions vs legacy without any
#            agent-visible behaviour change.
#   live   — router emits into the agent observation AND the legacy
#            graph_navigation / generate_improved_evidence path is suppressed
#            for the same event. Router is the SOLE L3/L3b evidence source.
#            Telemetry records legacy_path_skipped=True so paired metrics can
#            distinguish "router substituted" from "router silent".
#
# Back-compat: GT_ROUTER_V2=1 is accepted and mapped to "shadow" so existing
# canary runbooks keep working. Anything other than {1, shadow, live} maps to
# off.


_ROUTER_V2_MODE_LOGGED = False


def _router_v2_mode() -> str:
    """Return one of {"off","shadow","live"}.

    Accepts legacy boolean values: "0" / unset → off; "1" → shadow.
    Logs the resolved mode ONCE per process so we can see what the wrapper
    actually saw at runtime (independent of what GHA's env block claims).
    """
    raw = (os.environ.get("GT_ROUTER_V2", "off") or "off").strip().lower()
    if raw in ("live",):
        mode = "live"
    elif raw in ("shadow", "1", "on", "true", "yes"):
        mode = "shadow"
    else:
        mode = "off"
    global _ROUTER_V2_MODE_LOGGED
    if not _ROUTER_V2_MODE_LOGGED:
        _ROUTER_V2_MODE_LOGGED = True
        print(
            f"[GT_META] router_v2 boot: env={os.environ.get('GT_ROUTER_V2', '<unset>')!r} "
            f"resolved={mode} pid={os.getpid()}",
            flush=True,
        )
    return mode


def _router_v2_enabled() -> bool:
    """True iff router runs (shadow or live). Kept for call-site compat."""
    return _router_v2_mode() != "off"


def _router_v2_live() -> bool:
    """True iff router is the agent-visible L3/L3b path."""
    return _router_v2_mode() == "live"


def _ensure_v2_router(config: GTRuntimeConfig) -> Any:
    """Lazy-build a CollaborationRouter on the wrapper's AgentState.

    Returns ``None`` if router_v2 is disabled, AgentState construction fails,
    or the imports aren't available (e.g., partial install). Always tolerant —
    never raises out into the agent loop.
    """
    if not _router_v2_enabled():
        return None
    existing = getattr(config, "_router_v2", None)
    if existing is not None:
        return existing
    try:
        state = _ensure_agent_state(config)
        if state is None:
            return None
        from groundtruth.router import CollaborationRouter
        repo_root = os.environ.get("GT_REPO_ROOT", WORKSPACE_ROOT)
        db_path = getattr(config, "_host_graph_db", "") or config.graph_db
        # FINAL_ARCH_V2 Track-A B-1: fail fast if a non-empty host-side DB
        # has the wrong schema. We DO NOT raise on missing DB here — the
        # router will simply emit NO_GRAPH_DB, which is the correct signal.
        # The skip is only for schema drift on an actually-present host DB.
        host_db = getattr(config, "_host_graph_db", "")
        if host_db and os.path.exists(host_db):
            try:
                from groundtruth.index.schema_version import (
                    SchemaMismatch, verify_graph_db_schema,
                )
                verify_graph_db_schema(host_db)
            except SchemaMismatch as sm:
                # Live mode: cannot proceed silently — the router would emit
                # without the trust_tier signals it expects.
                if _router_v2_live():
                    raise
                print(
                    f"[GT_META] router_v2 schema check WARNING (shadow mode "
                    f"continuing): {sm}",
                    flush=True,
                )
            except Exception as sv_exc:
                # Schema check itself failed (rare) — log, do not block.
                print(
                    f"[GT_META] router_v2 schema check error "
                    f"({type(sv_exc).__name__}): {sv_exc}",
                    flush=True,
                )
        router = CollaborationRouter(
            state=state,
            db_path=db_path or "",
            repo_root=repo_root,
            delegate_evidence=_router_v2_live(),
        )
        config._router_v2 = router  # type: ignore[attr-defined]
        return router
    except Exception as exc:
        # In live mode, schema mismatch must propagate so the run dies loud.
        if _router_v2_live() and exc.__class__.__name__ == "SchemaMismatch":
            print(
                f"[GT_FATAL] router_v2 live: {exc}",
                flush=True,
            )
            raise
        print(f"[GT_META] router_v2 init failed: {type(exc).__name__}: {exc}", flush=True)
        return None


def _router_v2_on_view(config: GTRuntimeConfig, observed_path: str) -> dict[str, Any] | None:
    """Call router.on_view. Returns a structured-event dict or None.

    Disk persistence: every call is fanned out to BOTH
    ``/tmp/gt_interactions_<task>.jsonl`` (interaction log file) AND
    ``gt_layer_events_<task>.jsonl`` (structured events file). Earlier
    versions only appended to the in-memory ``config.interaction_log`` and
    were lost on GHA artifact upload. See FINAL_ARCH_V2 Track-A B-6.
    """
    mode = _router_v2_mode()
    # Always count the entry attempt so end-of-task fail-fast can detect
    # "live mode set but router never hit".
    config._router_v2_call_count = getattr(config, "_router_v2_call_count", 0) + 1  # type: ignore[attr-defined]
    router = _ensure_v2_router(config)
    if router is None:
        print(
            f"[GT_META] router_v2 on_view SKIPPED router=None mode={mode} "
            f"path={observed_path!r}",
            flush=True,
        )
        return None
    if not observed_path:
        print(f"[GT_META] router_v2 on_view SKIPPED empty-path mode={mode}", flush=True)
        return None
    # Sync iteration so debounce logic sees the real action count
    router.state.iteration = config.action_count
    try:
        em = router.on_view(observed_path)
    except Exception as exc:
        print(f"[GT_META] router_v2 on_view failed: {type(exc).__name__}: {exc}", flush=True)
        return None
    sup = em.suppression_reason.value if em.suppression_reason else None
    print(
        f"[GT_META] router_v2 on_view mode={mode} path={observed_path} "
        f"kind={em.kind.value} emit={em.emit} sup={sup} text_len={len(em.evidence_text)}",
        flush=True,
    )
    event = {
        "layer": "L3_router_v2",
        "kind": em.kind.value,
        "trigger": "on_view",
        "emit": em.emit,
        "suppression_reason": sup,
        "suppression_detail": em.suppression_detail,
        "primary_edge_file": em.primary_edge_file,
        "next_action_type": em.next_action_type,
        "next_action_file": em.next_action_file,
        "iteration": em.iteration,
        "band": em.band,
        "provider_used": "graph_providers",
        "evidence_items": len(em.evidence_items),
        "evidence_text": em.evidence_text,
        "mode": mode,
        "path": observed_path,
    }
    if em.emit and em.next_action_type:
        state = _ensure_agent_state(config)
        if state is not None:
            event_id = f"router_v2_view::{config.action_count}::{em.primary_edge_file}"
            try:
                state.register_pending_suggestion(
                    event_id=event_id,
                    next_action_type=em.next_action_type,
                    next_action_file=em.next_action_file,
                    ttl_actions=3,
                )
                event["event_id"] = event_id
            except Exception as exc:
                print(f"[GT_META] router_v2 register_pending failed: {type(exc).__name__}: {exc}", flush=True)
    _persist_router_v2_event(config, event)
    return event


def _persist_router_v2_event(config: GTRuntimeConfig, event: dict[str, Any]) -> None:
    """Fan a router_v2 event out to in-memory log + the two on-disk telemetry
    files so GHA artifact upload sees it."""
    try:
        config.interaction_log.append({"router_v2": event})
    except Exception:
        pass
    # /tmp/gt_interactions_<task>.jsonl
    try:
        path = _metrics_path(config, "interactions")
        record = {"timestamp": time.time(), **event}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        print(f"[GT_META] router_v2 persist interactions failed: {exc}", flush=True)
    # gt_layer_events via the existing telemetry writer
    try:
        _emit_structured_event(
            config,
            "L3_router_v2",
            event["trigger"],
            emitted=bool(event.get("emit")),
            suppressed=not event.get("emit"),
            suppression_reason=event.get("suppression_reason"),
            rendered_text=event.get("evidence_text", "") or "",
            next_action_type=event.get("next_action_type") or None,
            next_action_file=event.get("next_action_file") or None,
            file_path=event.get("path") or event.get("primary_edge_file") or None,
        )
    except Exception as exc:
        print(f"[GT_META] router_v2 emit_structured_event failed: {exc}", flush=True)


def _write_router_v2_legacy_skip(
    config: GTRuntimeConfig,
    *,
    trigger: str,
    file_path: str,
    router_emitted: bool,
) -> None:
    """Live-mode bookkeeping: record that the legacy graph_navigation /
    generate_improved_evidence path was skipped for this event. Fans out to
    in-memory log AND /tmp/gt_interactions_<task>.jsonl so GHA post-run
    fail-fast can grep for it. Also prints a one-line trace."""
    rec = {
        "type": "router_v2_legacy_skip",
        "trigger": trigger,
        "file": file_path,
        "router_emitted": router_emitted,
        "timestamp": time.time(),
        "iter": getattr(config, "action_count", 0),
    }
    try:
        config.interaction_log.append({"router_v2_legacy_skip": rec})
    except Exception:
        pass
    try:
        path = _metrics_path(config, "interactions")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as exc:
        print(f"[GT_META] router_v2_legacy_skip persist failed: {exc}", flush=True)
    print(
        f"[GT_META] router_v2_legacy_skip trigger={trigger} file={file_path} "
        f"router_emitted={router_emitted}",
        flush=True,
    )


def _router_v2_on_edit(
    config: GTRuntimeConfig, edited_path: str, function_names: list[str],
) -> dict[str, Any] | None:
    """Call router.on_edit. Mirrors _router_v2_on_view + persistence + counter."""
    mode = _router_v2_mode()
    config._router_v2_call_count = getattr(config, "_router_v2_call_count", 0) + 1  # type: ignore[attr-defined]
    router = _ensure_v2_router(config)
    if router is None:
        print(
            f"[GT_META] router_v2 on_edit SKIPPED router=None mode={mode} "
            f"path={edited_path!r}",
            flush=True,
        )
        return None
    if not edited_path:
        print(f"[GT_META] router_v2 on_edit SKIPPED empty-path mode={mode}", flush=True)
        return None
    # Sync iteration so debounce logic sees the real action count
    router.state.iteration = config.action_count
    try:
        em = router.on_edit(edited_path, function_names or [])
    except Exception as exc:
        print(f"[GT_META] router_v2 on_edit failed: {type(exc).__name__}: {exc}", flush=True)
        return None
    sup = em.suppression_reason.value if em.suppression_reason else None
    print(
        f"[GT_META] router_v2 on_edit mode={mode} path={edited_path} "
        f"kind={em.kind.value} emit={em.emit} sup={sup} text_len={len(em.evidence_text)} "
        f"funcs={function_names}",
        flush=True,
    )
    event = {
        "layer": "L3_router_v2",
        "kind": em.kind.value,
        "trigger": "on_edit",
        "emit": em.emit,
        "suppression_reason": sup,
        "suppression_detail": em.suppression_detail,
        "primary_edge_file": em.primary_edge_file,
        "next_action_type": em.next_action_type,
        "next_action_file": em.next_action_file,
        "iteration": em.iteration,
        "band": em.band,
        "provider_used": "evidence_providers",
        "evidence_items": len(em.evidence_items),
        "evidence_text": em.evidence_text,
        "mode": mode,
        "path": edited_path,
        "function_names": list(function_names or []),
    }
    if em.emit and em.next_action_type:
        state = _ensure_agent_state(config)
        if state is not None:
            event_id = f"router_v2_edit::{config.action_count}::{em.primary_edge_file}"
            try:
                state.register_pending_suggestion(
                    event_id=event_id,
                    next_action_type=em.next_action_type,
                    next_action_file=em.next_action_file,
                    ttl_actions=3,
                )
                event["event_id"] = event_id
            except Exception as exc:
                print(f"[GT_META] router_v2 register_pending failed: {type(exc).__name__}: {exc}", flush=True)
    _persist_router_v2_event(config, event)
    return event


def _strip_scaffold_files(
    orig_run_action: Callable[[Any], Any],
    config: GTRuntimeConfig,
    instance_ref: Any,
) -> None:
    """Delete scaffold files created by the agent. Idempotent via config.scaffold_stripped.

    Only removes untracked files whose basename starts with a SCAFFOLDING_PREFIX
    (reproduce_*, debug_*, temp_*, etc.).  Files the agent may have created as
    part of a legitimate fix (new source modules, changelog entries, test data)
    are preserved — 19.3% of SWE-bench-Live Lite gold patches add new files.
    """
    if config.scaffold_stripped:
        return
    config.scaffold_stripped = True
    base_commit = ""
    if instance_ref is not None:
        if isinstance(instance_ref, dict):
            base_commit = instance_ref.get("base_commit", "")
        else:
            base_commit = getattr(instance_ref, "base_commit", "")
    if not base_commit:
        return
    ls_base = _run_internal(orig_run_action, f"git ls-tree -r --name-only {base_commit}", 30)
    base_files = {f.strip() for f in ls_base.splitlines() if f.strip()}
    ls_new = _run_internal(orig_run_action, "git ls-files --others --exclude-standard", 30)
    new_files = [f.strip() for f in ls_new.splitlines() if f.strip()]
    to_strip = [f for f in new_files if f not in base_files and _is_scaffold_name(f)]
    kept = [f for f in new_files if f not in base_files and not _is_scaffold_name(f)]
    if to_strip:
        print(f"GT_ENFORCE: Stripping {len(to_strip)} scaffold files.", flush=True)
        for f in sorted(to_strip):
            _run_internal(orig_run_action, f"rm -f {_sh_single_quote(f)}", 10)
    if kept:
        print(f"GT_ENFORCE: Kept {len(kept)} new non-scaffold files: {', '.join(sorted(kept)[:5])}", flush=True)

    _hyg_eid = _emit_structured_event(
        config, "HYGIENE", "scaffold_strip",
        emitted=bool(to_strip),
        suppressed=not to_strip,
        suppression_reason="no_scaffold_files" if not to_strip else None,
        evidence_items=[
            {"kind": "hygiene_strip", "file_path": f, "reason": "scaffold file removed"}
            for f in to_strip
        ],
    )
    _log_gt_interaction(
        config, "HYGIENE", "scaffold_strip",
        "strip_ok" if to_strip else "strip_noop",
        f"stripped={len(to_strip)} kept={len(kept)}",
        event_id=_hyg_eid or "",
    )


def append_observation(obs: Any, text: str) -> Any:
    current = getattr(obs, "content", "")
    if current is None:
        current = ""
    # C1 boundary (B1/B2/B3/B3b): semantic Safe Renderer — suppress malformed/
    # empty contract fields, drop hidden diagnostics, no truncated markers —
    # then a glue-free join so GT never fuses onto the prior observation.
    block = _core_sanitize_block(text)
    if not block.strip():
        return obs
    before_len = len(str(current))
    try:
        obs.content = _core_join_no_glue(str(current), block)
    except Exception as e:
        print(f"[GT_DELIVERY] append_observation FAILED: {type(e).__name__}: {e}", flush=True)
        return obs
    after_len = len(obs.content)
    if block.strip():
        print(f"[GT_DELIVERY] append_observation OK: +{len(block)} chars (obs {before_len}→{after_len}), obs_type={type(obs).__name__}, text_start={block.strip()[:80]!r}", flush=True)
    return obs


def prepend_observation(obs: Any, text: str) -> Any:
    """Prepend GT evidence before the observation content.

    Cap matches the evidence safe-truncation (2000 chars) so L3/L3b
    evidence is never double-truncated. The old 600-char cap silently
    clipped contract details, caller lists, and scope information.
    """
    current = getattr(obs, "content", "")
    if current is None:
        current = ""
    block = _core_sanitize_block(text, max_chars=2000)
    if not block.strip():
        return obs
    before_len = len(str(current))
    try:
        obs.content = _core_join_no_glue(block, str(current))
    except Exception as e:
        print(f"[GT_DELIVERY] prepend_observation FAILED: {type(e).__name__}: {e}", flush=True)
        return obs
    after_len = len(obs.content)
    if block.strip():
        print(f"[GT_DELIVERY] prepend_observation OK: +{len(block)} chars (obs {before_len}→{after_len}), obs_type={type(obs).__name__}, text_start={block.strip()[:80]!r}", flush=True)
    return obs


def _safe_truncate_evidence(text: str, max_chars: int) -> str:
    """Truncate an evidence body to ``max_chars`` at a SAFE boundary (B1).

    A fixed-byte slice (``text[:1997] + "..."``) can land mid-marker — cutting
    ``[CATCHES]`` to ``[CATCHE`` — or mid-word, and glue the omission glyph
    directly onto the fragment with no separator. That corrupted fragment then
    flows into the agent observation (the verified ``[CATCHE...`` / ``text wit#``
    defects). This truncator instead:
      - returns the text verbatim when it already fits (no spurious note);
      - clips to the last COMPLETE line that fits, never a raw byte slice;
      - if even the first line overflows, clause-safe-clips it via the shared
        clip_balanced authority (never mid-token, balanced quotes/brackets);
      - drops a trailing partial ``[MARKER`` opener so no truncated marker name
        survives;
      - appends an explicit ``[+N chars omitted]`` note on its OWN line, never
        glued to the preceding content.
    Language-agnostic (reasons about lines / quotes / brackets, not Python).
    """
    if not text or len(text) <= max_chars:
        return text or ""
    lines = text.split("\n")
    kept: list[str] = []
    n = 0
    for ln in lines:
        add = len(ln) + (1 if kept else 0)
        if n + add > max_chars:
            break
        kept.append(ln)
        n += add
    if kept:
        body = "\n".join(kept)
    else:
        # First line alone overflows — clause-safe clip it (never mid-token).
        body = _core_clip_balanced(lines[0], max_chars)
    # Strip any trailing partial "[MARKER" opener left by the clip so no
    # truncated marker name (e.g. "[CATCHE") ever reaches the agent.
    body = re.sub(r"\[[A-Z][A-Z _]*$", "", body).rstrip()
    if not body:
        return ""
    omitted = len(text) - len(body)
    # Omission note on its OWN line — a guaranteed newline delimiter so the note
    # is never glued onto the preceding word/marker.
    return f"{body}\n[+{omitted} chars omitted]"


def _cmd_action(command: str, timeout: int = 30) -> Any:
    try:
        from openhands.events.action import CmdRunAction  # type: ignore[import]

        action = CmdRunAction(command=command)
        if hasattr(action, "set_hard_timeout"):
            action.set_hard_timeout(timeout)
        return action
    except Exception:
        return _FallbackCmdRunAction(command=command, timeout=timeout)


class _FallbackCmdRunAction:
    def __init__(self, command: str, timeout: int = 30) -> None:
        self.command = command
        self.timeout = timeout

    def set_hard_timeout(self, timeout: int) -> None:
        self.timeout = timeout


def _run_internal(orig_run_action: Callable[[Any], Any], command: str, timeout: int = 30) -> str:
    obs = orig_run_action(_cmd_action(command, timeout))
    return getattr(obs, "content", "") or getattr(obs, "stdout", "") or ""


def _upload_bytes_b64(runtime: Any, payload: bytes, target_path: str, timeout: int = 120) -> None:
    b64_chunks = _b64_chunks(payload)
    b64_path = f"{target_path}.b64"
    runtime.run_action(_cmd_action(f"mkdir -p {Path(target_path).parent.as_posix()}", 30))
    for idx, chunk in enumerate(b64_chunks):
        op = ">" if idx == 0 else ">>"
        runtime.run_action(_cmd_action(f"echo -n '{chunk}' {op} {b64_path}", timeout))
    runtime.run_action(
        _cmd_action(f"base64 -d {b64_path} > {target_path} && rm -f {b64_path}", timeout)
    )


def _bundle_dir_payload(source_dir: Path, arcname: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(str(source_dir), arcname=arcname)
    return buf.getvalue()


def _container_query(run_action_fn: Any, graph_db_path: str, sql: str, params_json: str = "[]") -> str:
    """Execute a SQL query against graph.db inside the container and return results as JSON.

    This is the query proxy — avoids transferring the entire 11MB DB to the host.
    Each call is one bash command (~1 second) instead of 727 chunked transfers (~7.5 min).

    ``run_action_fn`` can be either:
    - a raw function (orig_run_action) — called directly
    - a runtime object with .run_action — called via .run_action()
    """
    import base64 as _b64_cq
    b64_sql = _b64_cq.b64encode(sql.encode()).decode()
    b64_params = _b64_cq.b64encode(params_json.encode()).decode()
    b64_db = _b64_cq.b64encode(graph_db_path.encode()).decode()
    cmd = (
        f"python3 -c \""
        f"import json,sqlite3,sys,base64;"
        f"db=base64.b64decode(sys.argv[1]).decode();"
        f"sql=base64.b64decode(sys.argv[2]).decode();"
        f"params=json.loads(base64.b64decode(sys.argv[3]).decode());"
        f"c=sqlite3.connect(db);"
        f"r=c.execute(sql,params).fetchall();"
        f"print(json.dumps(r))"
        f"\" {b64_db} {b64_sql} {b64_params}"
    )
    action = _cmd_action(cmd, 15)
    if callable(run_action_fn) and not hasattr(run_action_fn, "run_action"):
        obs = run_action_fn(action)
    else:
        obs = run_action_fn.run_action(action)
    text = getattr(obs, "content", "") or ""
    for line in text.strip().splitlines():
        line = line.strip()
        if line.startswith("["):
            return line
    return "[]"


def _download_graph_db_to_host(runtime: Any, graph_db_path: str) -> str:
    """Download graph.db from container to host.

    Strategy (ordered by reliability):
    1. runtime.copy_from() — OH native zip-based transfer (most reliable)
    2. Fallback: base64 via python3 in container (fragile, OH injects noise)
    """
    import hashlib
    import shutil
    import zipfile

    # --- Pre-transfer diagnostic: verify source exists in container ---
    try:
        _diag_cmd = f"ls -lah {graph_db_path} 2>/dev/null && sqlite3 {graph_db_path} 'SELECT count(*) FROM nodes; SELECT count(*) FROM edges;' 2>/dev/null || echo 'sqlite3 not available'"
        _diag_obs = runtime.run_action(_cmd_action(_diag_cmd, 10))
        _diag_text = getattr(_diag_obs, "content", "") or ""
        print(f"[GT_META] B-7 source_diagnostic: {_diag_text.strip()[:300]}", flush=True)
    except Exception as _diag_exc:
        print(f"[GT_META] B-7 source_diagnostic failed: {_diag_exc}", flush=True)

    # --- Strategy 1: OH native copy_from (zip-based, no observation noise) ---
    if hasattr(runtime, "copy_from"):
        try:
            zip_path = runtime.copy_from(graph_db_path)
            if zip_path and zip_path.exists():
                extract_dir = tempfile.mkdtemp(prefix="gt_graph_")
                with zipfile.ZipFile(str(zip_path), "r") as zf:
                    zf.extractall(extract_dir)
                zip_path.unlink()
                # Log zip contents for debugging
                _zip_contents = list(Path(extract_dir).rglob("*"))
                print(
                    f"[GT_META] B-7 copy_from: zip contents ({len(_zip_contents)} files): "
                    f"{[str(f.relative_to(extract_dir)) for f in _zip_contents[:10]]}",
                    flush=True,
                )
                db_name = Path(graph_db_path).name
                candidates = list(Path(extract_dir).rglob(db_name))
                if not candidates:
                    candidates = list(Path(extract_dir).rglob("*.db"))
                if candidates:
                    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
                    tmp.write(candidates[0].read_bytes())
                    tmp.flush()
                    tmp.close()
                    size = os.path.getsize(tmp.name)
                    md5 = hashlib.md5(Path(tmp.name).read_bytes()).hexdigest()
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    if size > 0:
                        # Verify sqlite integrity
                        try:
                            _vc = sqlite3.connect(tmp.name)
                            _nc = _vc.execute("SELECT count(*) FROM nodes").fetchone()[0]
                            _ec = _vc.execute("SELECT count(*) FROM edges").fetchone()[0]
                            _vc.close()
                            print(
                                f"[GT_META] B-7 copy_from: OK — {size}b md5={md5} "
                                f"nodes={_nc} edges={_ec} from {graph_db_path}",
                                flush=True,
                            )
                        except Exception as _vce:
                            print(
                                f"[GT_META] B-7 copy_from: file copied ({size}b) but "
                                f"sqlite verify failed: {_vce}",
                                flush=True,
                            )
                        return tmp.name
                    os.unlink(tmp.name)
                shutil.rmtree(extract_dir, ignore_errors=True)
                print(f"[GT_META] B-7 copy_from: zip extracted but no .db found", flush=True)
        except Exception as cf_exc:
            print(f"[GT_META] B-7 copy_from failed: {type(cf_exc).__name__}: {cf_exc}", flush=True)

    # --- Strategy 2: Fallback base64 via python3 (fragile) ---
    print(f"[GT_META] B-7 fallback: attempting base64 transfer for {graph_db_path}", flush=True)
    meta_cmd = (
        f"python3 -c \""
        f"import hashlib,os,sys;"
        f"p='{graph_db_path}';"
        f"d=open(p,'rb').read() if os.path.exists(p) else b'';"
        f"print(f'SIZE={{len(d)}} MD5={{hashlib.md5(d).hexdigest()}}')"
        f"\""
    )
    meta_obs = runtime.run_action(_cmd_action(meta_cmd, 30))
    meta_text = getattr(meta_obs, "content", "") or ""
    size_match = re.search(r"SIZE=(\d+)\s+MD5=([a-f0-9]{32})", meta_text)
    if not size_match:
        print(f"[GT_META] B-7 fallback: metadata probe failed: {meta_text[:200]}", flush=True)
        return ""
    expected_size = int(size_match.group(1))
    expected_md5 = size_match.group(2)
    if expected_size == 0:
        print(f"[GT_META] B-7 fallback: graph.db is 0 bytes", flush=True)
        return ""

    b64_cmd = (
        f"python3 -c \""
        f"import base64,sys;"
        f"sys.stdout.write(base64.b64encode(open('{graph_db_path}','rb').read()).decode())"
        f"\""
    )
    obs = runtime.run_action(_cmd_action(b64_cmd, 120))
    b64_content = getattr(obs, "content", "") or getattr(obs, "stdout", "") or ""
    tokens = re.findall(r"[A-Za-z0-9+/=]{128,}", b64_content)
    if not tokens:
        print(f"[GT_META] B-7 fallback: no base64 tokens ({len(b64_content)} chars)", flush=True)
        return ""
    best = "".join(t.strip() for t in tokens)
    best += "=" * ((4 - (len(best) % 4)) % 4)
    try:
        data = base64.b64decode(best)
    except Exception as dec_err:
        print(f"[GT_META] B-7 fallback: base64 decode failed: {dec_err}", flush=True)
        return ""

    actual_md5 = hashlib.md5(data).hexdigest()
    if len(data) != expected_size or actual_md5 != expected_md5:
        print(
            f"[GT_META] B-7 fallback: transfer mismatch — "
            f"expected {expected_size}b/{expected_md5}, "
            f"got {len(data)}b/{actual_md5}. Trying chunked.",
            flush=True,
        )
        # --- Strategy 3: Chunked base64 transfer (handles large DBs) ---
        CHUNK_BYTES = 20000  # ~27KB base64 per chunk, fits in OH 30K char output
        n_chunks = (expected_size + CHUNK_BYTES - 1) // CHUNK_BYTES
        print(f"[GT_META] B-7 chunked: {expected_size}b in {n_chunks} chunks of {CHUNK_BYTES}b", flush=True)
        all_data = b""
        for i in range(n_chunks):
            offset = i * CHUNK_BYTES
            chunk_cmd = (
                f"python3 -c \""
                f"import base64,sys;"
                f"f=open('{graph_db_path}','rb');"
                f"f.seek({offset});"
                f"sys.stdout.write(base64.b64encode(f.read({CHUNK_BYTES})).decode())"
                f"\""
            )
            chunk_obs = runtime.run_action(_cmd_action(chunk_cmd, 30))
            chunk_text = getattr(chunk_obs, "content", "") or ""
            chunk_tokens = re.findall(r"[A-Za-z0-9+/=]{8,}", chunk_text)
            if not chunk_tokens:
                print(f"[GT_META] B-7 chunked: chunk {i}/{n_chunks} empty", flush=True)
                break
            chunk_b64 = "".join(t.strip() for t in chunk_tokens)
            chunk_b64 += "=" * ((4 - (len(chunk_b64) % 4)) % 4)
            try:
                all_data += base64.b64decode(chunk_b64)
            except Exception:
                print(f"[GT_META] B-7 chunked: chunk {i} decode failed", flush=True)
                break
        if len(all_data) == expected_size:
            chunked_md5 = hashlib.md5(all_data).hexdigest()
            if chunked_md5 == expected_md5:
                data = all_data
                print(f"[GT_META] B-7 chunked: OK — {len(data)}b md5={chunked_md5}", flush=True)
            else:
                print(f"[GT_META] B-7 chunked: md5 mismatch {chunked_md5} vs {expected_md5}", flush=True)
                return ""
        else:
            print(f"[GT_META] B-7 chunked: size mismatch {len(all_data)} vs {expected_size}", flush=True)
            return ""

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.write(data)
    tmp.flush()
    tmp.close()
    try:
        _vc = sqlite3.connect(tmp.name)
        _integrity = _vc.execute("PRAGMA integrity_check").fetchone()[0]
        _nc = _vc.execute("SELECT count(*) FROM nodes").fetchone()[0]
        _ec = _vc.execute("SELECT count(*) FROM edges").fetchone()[0]
        _vc.close()
        if _integrity != "ok":
            raise sqlite3.DatabaseError(f"integrity_check={_integrity}")
        print(
            f"[GT_META] B-7 transfer sqlite: OK nodes={_nc} edges={_ec}",
            flush=True,
        )
    except Exception as _sqlite_exc:
        print(f"[GT_META] B-7 transfer sqlite validation failed: {_sqlite_exc}", flush=True)
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return ""
    print(f"[GT_META] B-7 transfer: OK — {len(data)}b md5={hashlib.md5(data).hexdigest()}", flush=True)
    return tmp.name


def _download_text_from_container(orig_run_action: Callable[[Any], Any], path: str) -> str:
    """Best-effort download of a small text file from the container."""
    obs = _run_internal(orig_run_action, f"base64 -w0 {_sh_single_quote(path)} 2>/dev/null", 30)
    body = (obs or "").strip()
    if not body:
        return ""
    tokens = re.findall(r"[A-Za-z0-9+/=]{8,}", body)
    payload = max(tokens, key=len).strip() if tokens else body
    if not payload:
        return ""
    payload += "=" * ((4 - (len(payload) % 4)) % 4)
    try:
        return base64.b64decode(payload).decode("utf-8", errors="replace")
    except Exception:
        return ""


def install_l4_tools(runtime: Any, config: GTRuntimeConfig) -> list[str]:
    """Upload GT tool bundles. Returns tool names that appear in ``command -v`` output."""

    tool_names = ("gt_query", "gt_search", "gt_navigate", "gt_validate")
    runtime.run_action(_cmd_action(f"mkdir -p {config.tools_dir}", 30))
    for name in tool_names:
        source_dir = _TOOL_ROOT / name
        if not source_dir.exists():
            continue
        payload = _bundle_dir_payload(source_dir, name)
        tar_target = f"{config.tools_dir}/{name}.tar.gz"
        _upload_bytes_b64(runtime, payload, tar_target)
        runtime.run_action(
            _cmd_action(
                f"cd {config.tools_dir} && tar xzf {name}.tar.gz && rm -f {name}.tar.gz",
                120,
            )
        )
    # Strip CRLF and ensure bin scripts are executable (Windows-packaged repos).
    runtime.run_action(
        _cmd_action(
            f"find {config.tools_dir} -type f \\( -path '*/bin/*' -o -name '*.sh' \\) "
            r"-exec sed -i 's/\r$//' {} \; 2>/dev/null; "
            f"find {config.tools_dir} -type f -path '*/bin/*' -exec chmod +x {{}} \\; 2>/dev/null",
            60,
        )
    )
    gdb_esc = config.graph_db.replace("'", "'\"'\"'")
    root_esc = config.workspace_root.replace("'", "'\"'\"'")
    runtime.run_action(
        _cmd_action(
            f"grep -q 'GT_GRAPH_DB=' ~/.bashrc || echo 'export GT_GRAPH_DB=\"{gdb_esc}\"' >> ~/.bashrc; "
            f"grep -q 'GT_REPO_ROOT=' ~/.bashrc || echo 'export GT_REPO_ROOT=\"{root_esc}\"' >> ~/.bashrc; "
            "grep -q 'GT_PYTHON=python3' ~/.bashrc || echo 'export GT_PYTHON=python3' >> ~/.bashrc; "
            f"grep -q '{config.tools_dir}/gt_query/bin' ~/.bashrc || echo "
            f"'export PATH={config.tools_dir}/gt_query/bin:{config.tools_dir}/gt_search/bin:"
            f"{config.tools_dir}/gt_navigate/bin:{config.tools_dir}/gt_validate/bin:$PATH' >> ~/.bashrc",
            30,
        )
    )
    check = _run_internal(
        runtime.run_action,
        _env_prefix(config) + "command -v gt_query gt_search gt_navigate gt_validate 2>/dev/null",
        30,
    )
    uniq: list[str] = []
    for ln in check.strip().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        base = Path(ln).name
        if base in tool_names and base not in uniq:
            uniq.append(base)

    runtime.run_action(
        _cmd_action(_env_prefix(config) + "python3 --version 2>&1 | head -1", 15)
    )

    return uniq


def _verify_graph_nonempty(runtime: Any, config: GTRuntimeConfig, orig_ra: Callable[[Any], Any]) -> tuple[int, int]:
    # Avoid relying on sqlite3 CLI availability inside evaluation containers.
    py = (
        "python3 - "
        + _sh_single_quote(config.graph_db)
        + " 2>&1 <<'PY'\n"
        + "import sqlite3\n"
        + "import sys\n"
        + "db = sys.argv[1]\n"
        + "n = e = 0\n"
        + "try:\n"
        + "    conn = sqlite3.connect(db)\n"
        + "    cur = conn.cursor()\n"
        + "    n = cur.execute('SELECT COUNT(*) FROM nodes').fetchone()[0]\n"
        + "    e = cur.execute('SELECT COUNT(*) FROM edges').fetchone()[0]\n"
        + "    conn.close()\n"
        + "except Exception:\n"
        + "    pass\n"
        + "print(f'{int(n)}|{int(e)}')\n"
        + "PY"
    )
    raw = _run_internal(orig_ra, _env_prefix(config) + py, 30).strip().split("\n")[0]
    raw = raw.strip()
    parts = [p.strip() for p in re.split(r"\s*\|\s*", raw, maxsplit=1)]
    nums: list[int] = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            continue
    if len(nums) >= 2:
        return nums[0], nums[1]
    return 0, 0


def _promoted_graph_db_path() -> str:
    """C6 step-4 (RF-3) single gate: return the offline-promoted graph.db path
    iff ``GT_PREBUILT_GRAPH_DB`` is set AND the file exists, else ``""``.

    This is THE one check that keeps the default eval path byte-identical: when
    the env var is unset (or points at a missing file) this returns ``""`` and
    every prebuilt-specific branch below is skipped, leaving the original
    in-container build + alt_root retry + L6 reindex flow untouched.
    """
    p = os.environ.get("GT_PREBUILT_GRAPH_DB", "")
    if p and os.path.exists(p):
        return p
    return ""


def _upload_promoted_db(
    runtime: Any,
    config: GTRuntimeConfig,
    orig_ra: Callable[[Any], Any],
    promoted_path: str,
) -> bool:
    """Upload an offline-promoted graph.db into the container at the EXACT
    ``config.graph_db`` path (C6 step-4 / RF-3 (a)+(b)).

    Validates the host db's schema via ``verify_graph_db_schema`` BEFORE trusting
    it (RF-3 (b)); on schema mismatch returns ``False`` so the caller falls back
    to the normal in-container build. Reuses the existing copy_to / _upload_bytes_b64
    transfer path used for the gt-index binary, then verifies the uploaded db is
    non-empty in-container. Returns ``True`` only when the promoted db is present
    and non-empty inside the container.
    """
    # RF-3 (b): schema gate on the HOST copy before we trust/upload it.
    try:
        from groundtruth.index.schema_version import (
            SchemaMismatch,
            verify_graph_db_schema,
        )
        try:
            verify_graph_db_schema(promoted_path)
        except SchemaMismatch as sm:
            print(
                f"[GT_META] prebuilt_db schema mismatch ({sm}); "
                "falling back to in-container gt-index build",
                flush=True,
            )
            return False
    except Exception as sv_exc:  # schema module import/probe failure — do not crash
        print(
            f"[GT_META] prebuilt_db schema check error "
            f"({type(sv_exc).__name__}: {sv_exc}); "
            "falling back to in-container gt-index build",
            flush=True,
        )
        return False

    # Upload into the container at the EXACT config.graph_db path (RF-3 (a)).
    # Reuse the binary-upload pattern: copy_to first, b64 fallback on failure.
    try:
        payload = Path(promoted_path).read_bytes()
    except Exception as read_exc:
        print(f"[GT_META] prebuilt_db read failed: {read_exc}", flush=True)
        return False

    uploaded = False
    try:
        runtime.copy_to(promoted_path, str(Path(config.graph_db).parent.as_posix()))
        # copy_to lands the file under <dir>/<basename>; rename to the exact
        # config.graph_db target so all consumers (--db={config.graph_db}) match.
        src_after_copy = (
            Path(config.graph_db).parent / Path(promoted_path).name
        ).as_posix()
        if src_after_copy != config.graph_db:
            _run_internal(
                orig_ra,
                f"mv -f {_sh_single_quote(src_after_copy)} {_sh_single_quote(config.graph_db)}",
                30,
            )
        uploaded = True
    except Exception as copy_exc:
        print(
            f"[GT_META] prebuilt_db copy_to failed ({str(copy_exc)[:160]}); "
            "trying b64 upload",
            flush=True,
        )
        try:
            _upload_bytes_b64(runtime, payload, config.graph_db)
            uploaded = True
        except Exception as b64_exc:
            print(f"[GT_META] prebuilt_db b64 upload failed: {b64_exc}", flush=True)
            return False

    if not uploaded:
        return False

    nc, ec = _verify_graph_nonempty(runtime, config, orig_ra)
    if nc == 0 and ec == 0:
        print(
            "[GT_META] prebuilt_db uploaded but verifies empty in-container "
            f"(db={config.graph_db}); falling back to in-container build",
            flush=True,
        )
        return False
    print(
        f"[GT_META] prebuilt_db uploaded OK to {config.graph_db} "
        f"(host={promoted_path}, nodes={nc} edges={ec}); in-container build skipped",
        flush=True,
    )
    return True


# C6 step-4 (RF-3 (d)) — L6-OVERWRITE RESOLUTION (the crux).
#
# Problem: when a prebuilt promoted db is uploaded, the L6 incremental reindex
# (gt-index -file=..., runIncremental in cmd/gt-index/main.go) does a file-keyed
# delete-and-replace of the EDITED file's edges. It re-resolves those edges with
# tree-sitter only (no LSP), so any edge that was promoted to
# resolution_method='lsp' (confidence=1.0, trust_tier=CERTIFIED) and whose
# source_file is the edited file reverts to a name_match guess — re-introducing
# un-promoted edges for exactly the file the agent is working on. (The rest of
# the db is untouched: -file mode only deletes this file's edges + incoming
# edges to its nodes, then re-resolves; it is NOT a full wipe.)
#
# CHOSEN FIX: RE-APPLY the promotion after reindex (NOT "preserve old rows").
# Rationale:
#   1. The reindex genuinely changed the file (line numbers, added/removed
#      functions). Preserving stale lsp rows would leave dangling/mislocated
#      edges pointing at deleted or moved nodes — worse than name_match.
#   2. resolve.py already has scoped re-promotion machinery (source_files filter
#      + per-language LSP); re-running it regenerates CORRECT lsp edges for the
#      NEW code, which is the actual goal.
#   3. Correct-or-quiet: if the LSP server is absent in-container we DO NOT touch
#      the db — the reindex output stands (name_match), and we log it. We never
#      silently destroy the promotion without at least attempting to restore it.
# This only runs when config._gt_prebuilt_active is True (a promoted db was
# uploaded). On the default path the function is a no-op.
def _repromote_after_reindex(
    runtime: Any,
    config: GTRuntimeConfig,
    orig_ra: Callable[[Any], Any],
    edited_rel_path: str,
) -> str:
    """Re-run scoped LSP promotion over the just-reindexed file (RF-3 (d)).

    Returns a short status string for logging. No-op (returns "skip:not_prebuilt")
    unless ``config._gt_prebuilt_active`` is set.
    """
    if not getattr(config, "_gt_prebuilt_active", False):
        return "skip:not_prebuilt"

    ext = Path(edited_rel_path).suffix.lstrip(".").lower()
    # Map file extension -> resolve.py --lang token (LSP-promotable languages).
    _ext_lang = {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "go": "go",
        "rs": "rust",
        "java": "java",
        "rb": "ruby",
        "kt": "kotlin",
    }
    lang = _ext_lang.get(ext, "")
    if not lang:
        return f"skip:lang_unsupported({ext or 'none'})"

    # The LSP server must exist IN-CONTAINER. If it is missing, do not touch the
    # db (correct-or-quiet): the name_match reindex result stands.
    _server_cmd = {
        "python": "pyright-langserver",
        "javascript": "typescript-language-server",
        "typescript": "typescript-language-server",
        "go": "gopls",
        "rust": "rust-analyzer",
        "java": "jdtls",
        "ruby": "solargraph",
        "kotlin": "kotlin-language-server",
    }.get(lang, "")
    if not _server_cmd:
        return f"skip:no_server_cmd({lang})"
    _have = _run_internal(
        orig_ra,
        f"command -v {_server_cmd} >/dev/null 2>&1 && echo GT_LSP_PRESENT || echo GT_LSP_ABSENT",
        15,
    )
    if "GT_LSP_PRESENT" not in _have:
        return f"skip:lsp_absent({_server_cmd})"

    # Re-promote, scoped to the edited file's source_file. resolve.py filters
    # ambiguous edges by language; --root anchors LSP at the repo root. We bound
    # with timeout + non-fatal so a slow/failed LSP can never break the turn.
    promote_cmd = (
        _env_prefix(config)
        + f"timeout 120 python3 -m groundtruth.resolve resolve "
        + f"--db={_sh_single_quote(config.graph_db)} "
        + f"--root={_sh_single_quote(config.workspace_root)} "
        + f"--lang={lang} --resolve 2>&1 || echo GT_REPROMOTE_WARN"
    )
    out = _run_internal(orig_ra, promote_cmd, 130)
    if "GT_REPROMOTE_WARN" in out:
        return f"warn:repromote_nonzero({lang})"
    return f"ok:repromoted({lang})"


def install_graph_and_hook(runtime: Any, config: GTRuntimeConfig) -> list[str]:
    """Install package, gt-index graph, PATH tools."""

    host_index = os.environ.get("GT_INDEX_BINARY", "")
    if not host_index or not Path(host_index).exists():
        cand = _REPO_ROOT / "tools" / "sweagent" / "gt_edit" / "bin" / "gt-index"
        if cand.exists():
            host_index = str(cand)

    orig_ra = runtime.run_action

    copy_to_ok = False
    b64_ok = False
    copy_exc_msg = ""
    b64_exc_msg = ""
    if host_index and Path(host_index).exists():
        container_bin = "/tmp/" + Path(host_index).name
        try:
            runtime.copy_to(host_index, "/tmp/")
            copy_to_ok = True
        except Exception as copy_exc:
            copy_exc_msg = str(copy_exc)[:200]
            try:
                _upload_bytes_b64(runtime, Path(host_index).read_bytes(), container_bin)
                b64_ok = True
            except Exception as b64_exc:
                b64_exc_msg = str(b64_exc)[:200]
            else:
                config.gt_index_bin = container_bin
        else:
            config.gt_index_bin = container_bin
        runtime.run_action(_cmd_action(f"chmod +x {config.gt_index_bin}", 30))

    verify_out = _run_internal(orig_ra, f"test -x {config.gt_index_bin} && echo GT_BIN_OK", 10).strip()
    if "GT_BIN_OK" not in verify_out:
        path_bin = _run_internal(orig_ra, "command -v gt-index 2>/dev/null || true", 10).strip().split("\n", 1)[0].strip()
        if path_bin:
            print(f"[GT_META] gt-index uploaded binary unusable, falling back to PATH: {path_bin}", flush=True)
            config.gt_index_bin = path_bin
        else:
            host_exists = Path(host_index).exists() if host_index else False
            host_executable = os.access(host_index, os.X_OK) if host_index and host_exists else False
            container_dir = _run_internal(orig_ra, f"ls -la /tmp/gt-index* 2>&1 || echo 'no gt-index files'", 5).strip()
            print(
                f"[GT_META] L6 BINARY UPLOAD FAILED — diagnostics:\n"
                f"  host_path={host_index}\n"
                f"  host_exists={host_exists}\n"
                f"  host_executable={host_executable}\n"
                f"  container_target={config.gt_index_bin}\n"
                f"  container_listing={container_dir[:300]}\n"
                f"  copy_to_ok={copy_to_ok} exc={copy_exc_msg}\n"
                f"  b64_ok={b64_ok} exc={b64_exc_msg}",
                flush=True,
            )
            config.gt_index_bin = ""

    groundtruth_pkg = _SRC_DIR / "groundtruth"
    if groundtruth_pkg.exists():
        payload = _bundle_dir_payload(groundtruth_pkg, "groundtruth")
        _upload_bytes_b64(runtime, payload, "/tmp/gt_src.tar.gz")
        runtime.run_action(
            _cmd_action(
                "cd /tmp && tar xzf gt_src.tar.gz && rm -f gt_src.tar.gz; "
                r"grep -q 'PYTHONPATH=/tmp:${PYTHONPATH:-}' ~/.bashrc || "
                r"echo 'export PYTHONPATH=/tmp:${PYTHONPATH:-}' >> ~/.bashrc; "
                r"export PYTHONPATH=/tmp:${PYTHONPATH:-}",
                120,
            )
        )

    chk = _run_internal(
        orig_ra,
        _env_prefix(config)
        + 'python3 -c "from groundtruth.hooks.post_edit import main; print(\\"GT_PKG_OK\\")" 2>&1',
        45,
    )
    if "GT_PKG_OK" not in chk:
        print(f"WARNING: GT package import verification failed: {chk[:500]}", flush=True)

    pyver = _run_internal(
        orig_ra,
        _env_prefix(config)
        + (
            'python3 -c "import sys; v=sys.version_info; '
            'assert v.major>=3 and v.minor>=10; print(sys.version.split()[0])" 2>&1'
        ),
        20,
    )
    if pyver.startswith("Traceback") or "AssertionError" in pyver:
        print(f"WARNING: container Python >=3.10 recommended for hooks: {pyver[:240]}", flush=True)

    # C6 step-4 (RF-3): if an offline-promoted graph.db exists, upload it into
    # the container at the EXACT config.graph_db path and SKIP the in-container
    # gt-index build (and its alt_root retry). The single gate is
    # _promoted_graph_db_path(); when GT_PREBUILT_GRAPH_DB is unset this returns
    # "" and the entire block is skipped, leaving the default build path
    # byte-identical to prior behavior.
    index_out = ""
    nc = ec = 0
    _promoted = _promoted_graph_db_path()
    _prebuilt_used = False
    if _promoted:
        _prebuilt_used = _upload_promoted_db(runtime, config, orig_ra, _promoted)
        if _prebuilt_used:
            # Mark the run so the L6 reindex re-applies the promotion (RF-3 (d)).
            config._gt_prebuilt_active = True
            nc, ec = _verify_graph_nonempty(runtime, config, orig_ra)
            index_out = "(in-container build skipped: prebuilt promoted db uploaded)"

    if not _prebuilt_used:
        # Default path: in-container gt-index build (+ alt_root retry).
        index_cmd = (
            f"{config.gt_index_bin} -root={_sh_single_quote(config.workspace_root)} "
            f"-output={_sh_single_quote(config.graph_db)} 2>&1"
        )
        index_out = _run_internal(orig_ra, index_cmd, 180)

        nc, ec = _verify_graph_nonempty(runtime, config, orig_ra)
        if nc == 0 and ec == 0:
            alt_root = _run_internal(
                orig_ra,
                f"git -C {_sh_single_quote(config.workspace_root)} rev-parse --show-toplevel 2>/dev/null || true",
                20,
            ).strip()
            alt_root = alt_root.split("\n", 1)[0].strip().strip("'\"")
            if alt_root and alt_root != config.workspace_root:
                retry_cmd = (
                    f"{config.gt_index_bin} -root={_sh_single_quote(alt_root)} "
                    f"-output={_sh_single_quote(config.graph_db)} 2>&1"
                )
                retry_out = _run_internal(orig_ra, retry_cmd, 180)
                nc, ec = _verify_graph_nonempty(runtime, config, orig_ra)
                if nc > 0 or ec > 0:
                    print(
                        f"WARNING: gt-index root adjusted from {config.workspace_root} to {alt_root}",
                        flush=True,
                    )
                    config.workspace_root = alt_root
                elif retry_out.strip():
                    print(f"WARNING: gt-index retry output: {retry_out[:400]}", flush=True)
            print(
                "WARNING: graph.db has zero nodes/edges after build "
                f"(root={config.workspace_root}, db={config.graph_db}); "
                + f"index_output={index_out[:400]!r}",
                flush=True,
            )
        else:
            print(f"GT graph sanity OK: nodes={nc} edges={ec}", flush=True)

    # Set dynamic limits based on repo size
    config._node_count = nc
    config._edge_count = ec
    _compute_repo_scale(config)
    print(f"[GT_META] repo_scale={config._repo_scale} nodes={nc} edges={ec} max_items={config.max_items}", flush=True)

    # --- Always attempt graph.db download to host after indexing ---
    # Default "proxy" mode never downloads graph.db, leaving host-side features dead
    # (grep intercept, L6 pre-submit, scope detection, scaffold advisory, anchor extraction).
    # Download eagerly here so host-side features work regardless of transfer mode.
    if nc > 0 and not config._host_graph_db:
        try:
            _downloaded_path = _download_graph_db_to_host(runtime, config.graph_db)
            if _downloaded_path:
                config._host_graph_db = _downloaded_path
                os.environ["GT_GRAPH_DB"] = _downloaded_path
                print(
                    f"[GT_META] host_graph_db: OK ({os.path.getsize(_downloaded_path)} bytes) "
                    f"path={_downloaded_path}",
                    flush=True,
                )
            else:
                print(
                    "[GT_META] host_graph_db: download returned empty "
                    "(host-side features will use _container_query fallback)",
                    flush=True,
                )
        except Exception as _dl_exc:
            print(
                f"[GT_META] host_graph_db: download failed ({_dl_exc}), "
                "host-side features will use _container_query fallback",
                flush=True,
            )

    return install_l4_tools(runtime, config)


def _write_gt_telemetry(instance: Any, tel: GTTelemetry | None) -> None:
    if tel is None or instance is None:
        return
    blob = tel.finalize()
    try:
        instance["gt_telemetry"] = blob
    except Exception:
        try:
            setattr(instance, "gt_telemetry", blob)
        except Exception:
            pass


def _pull_hook_logs(orig_run_action: Callable[[Any], Any], instance_ref: Any) -> None:
    """Download hook logs from container into instance record."""
    if instance_ref is None:
        return
    for key, path in (
        ("gt_hook_log", os.environ.get("GT_HOOK_LOG", "/tmp/gt_hooks.log")),
        ("gt_hook_log_jsonl", "/tmp/gt_hook_log.jsonl"),
        ("gt_interactions_jsonl", "/tmp/gt_interactions.jsonl"),
    ):
        content = _download_text_from_container(orig_run_action, path)
        if content:
            try:
                instance_ref[key] = content
            except Exception:
                try:
                    setattr(instance_ref, key, content)
                except Exception:
                    pass


def _pull_graph_db_artifact(config: GTRuntimeConfig) -> str:
    """Stage the task's graph.db next to ``output.jsonl`` for offline replay.

    FINAL_ARCH_V2 §3 Layer 0 + the shadow-replay contract require a per-task
    graph.db to land in the eval artifact dir so that downstream shadow
    replays can resolve a matched (output.jsonl, graph.db) pair.

    This helper is best-effort: it reads the host-side graph.db copy that the
    wrapper already populates at ``config._host_graph_db`` (line ~2516) and
    copies it to ``$GT_ARTIFACT_DIR/graph.db`` (or, when ``GT_ARTIFACT_DIR`` is
    unset, to the metadata-derived eval_output_dir under
    ``$EVAL_OUTPUT_DIR``). Returns the destination path or ``""``.
    """
    src = getattr(config, "_host_graph_db", "") or ""
    if not src or not os.path.isfile(src):
        return ""
    dest_dir = (
        os.environ.get("GT_ARTIFACT_DIR")
        or os.environ.get("EVAL_OUTPUT_DIR")
        or os.environ.get("OUT_ROOT")
        or ""
    )
    if not dest_dir:
        return ""
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "graph.db")
        import shutil as _shutil
        _shutil.copy2(src, dest)
        print(f"[GT_ARTIFACT] graph.db -> {dest} ({os.path.getsize(dest)} bytes)", flush=True)
        return dest
    except Exception as exc:
        print(f"[GT_ARTIFACT] graph.db copy failed: {type(exc).__name__}: {exc}", flush=True)
        return ""


GT_PHASE = os.environ.get("GT_PHASE", "full").lower()

# ---------------------------------------------------------------------------
# L4b Tool-as-Hooks Architecture
#
# The 7 MCP tool capabilities are delivered passively via existing hooks:
#   investigate  -> L3 (callers + contracts) + L3b (navigation) + L4a (symbols)
#   orient_v2    -> L1 (brief) + consensus (scope detection)
#   check_v2     -> L3 (post-edit contracts) + L6 (pre-submit validation)
#   status_v2    -> L5 (scaffold governor) + observability logging
#   gt_plan      -> L1+ (edit plan + key contracts)
#   gt_run_tests -> L3 (verify suggestions) + L6 (test recommendations)
#   gt_contract  -> L3 (behavioral contract from properties table)
#
# No separate "tool-as-hook" code is needed -- the hooks ARE the tools.
# Each agent action (task start, file read, file edit, submit) triggers the
# equivalent of an MCP tool invocation through the layer that fires on that
# event type.  This design avoids MCP adoption dependency (agents ignore tools
# at 0% call rate) while still delivering the same intelligence passively.
# ---------------------------------------------------------------------------


def wrap_runtime_run_action(runtime: Any, config: GTRuntimeConfig | None = None) -> Any:
    """Append GT evidence to agent-visible observations for eligible events.

    GT_PHASE env controls which layers are active:
      "B"    — L1 brief only, patched_run_action is pass-through
      "C"    — L1 + pacing ([GT_OK] placeholders, no real hooks)
      "D"    — L1 + pacing + real L3/L3b evidence
      "E"    — L1 + pacing + evidence + L5 redirect
      "full" — all layers (L1 + L3 + L3b + L5 + L6 + scaffold strip)
    """

    config = config or GTRuntimeConfig()
    if getattr(runtime, "_gt_full_wrapped", False):
        return runtime
    orig_run_action = runtime.run_action
    config._task_end_orig_run_action = orig_run_action

    def patched_run_action(action: Any) -> Any:
        # Phase B: brief-only, no run_action hooks at all
        if GT_PHASE == "b":
            return orig_run_action(action)

        act_text = _action_text(action)
        act_cls = _action_class(action)
        tel_obj = getattr(config, "telemetry", None)
        l4_recorded = False

        # Online tracker: check pending next_actions against this real agent action
        _action_file = ""
        if hasattr(action, "path"):
            _action_file = str(action.path or "")
        obs_placeholder = getattr(action, "_obs_ref", None)  # will be set after orig_run_action
        # We check pending BEFORE GT logic runs — this is the agent's real action
        # Note: obs is not available yet here; we'll check again after obs is obtained

        # Backfill agent_action_after on previous log entry for every action,
        # not just GT-triggered ones.  This creates a complete GT->agent->response
        # chain even when the next action doesn't trigger GT.
        if config.interaction_log:
            prev = config.interaction_log[-1]
            if not prev.get("agent_action_after"):
                summary = f"{act_cls}:{act_text[:200]}"
                prev["agent_action_after"] = summary

        # L6 pre-submit: OH controller sets state=FINISHED before calling
        # runtime.run_action, so returning an early observation here cannot
        # prevent the finish — the agent never steps again.  The actual L6
        # review runs in the finish handler below (~line 4600) where it
        # appends to the observation for telemetry/artifact purposes.
        _is_finish_action = act_cls in ("AgentFinishAction", "FinishAction")

        # L5b pre-finish: OH sets state=FINISHED before run_action, so returning
        # early here cannot prevent the finish. L5b governance fires in the
        # post-finish handler (~line 4540) where it appends to the observation
        # for telemetry purposes. Pre-finish intercept was attempted but removed
        # because the agent never steps again after state=FINISHED.

        if tel_obj is not None and _action_class(action) == "CmdRunAction":
            if re.search(r"\bgt_(query|search|navigate|validate)\b", act_text):
                tel_obj.record_l4()
                l4_recorded = True

        try:
            obs = orig_run_action(action)
        except AttributeError as ae:
            if "TaskTracking" in _action_class(action) or "'str' object" in str(ae):
                from openhands.events.observation import NullObservation  # type: ignore[import]
                obs = NullObservation("Task tracking skipped (format mismatch)")
                print(f"[GT_META] TaskTrackingAction crash intercepted: {ae}", flush=True)
            else:
                raise

        # ---------------------------------------------------------------
        # Stuck detector compatibility (OH issue #7183/#5480).
        #
        # OH's stuck detector compares 4+ consecutive identical
        # (action, observation) pairs.  GT modifies observation content
        # on every invocation, making each pair unique → detector never
        # fires → agent loops forever.
        #
        # Fix: fingerprint the RAW observation BEFORE GT touches it.
        # When the same (action, raw_observation) pair repeats, skip
        # ALL GT injection so the detector sees identical entries.
        #
        # Evidence from the 1st occurrence is already in the agent's
        # context window.  Repeating it on a stuck loop adds noise,
        # not value.  Letting the detector fire is strictly better.
        # ---------------------------------------------------------------
        _raw_content = getattr(obs, "content", "") or ""
        _raw_hash = hashlib.md5(
            _raw_content[:8000].encode("utf-8", errors="replace")
        ).hexdigest()
        _obs_pair = (f"{act_cls}:{act_text[:300]}", _raw_hash)
        _is_repeated_obs = _obs_pair in config._stuck_compat_history[-8:]
        config._stuck_compat_history.append(_obs_pair)
        if len(config._stuck_compat_history) > 24:
            config._stuck_compat_history = config._stuck_compat_history[-24:]

        if _is_repeated_obs and not _GT_BASELINE and not _is_finish_action:
            config.action_count += 1
            if act_cls == "CmdRunAction":
                config._cmd_action_count = getattr(config, "_cmd_action_count", 0) + 1
            event = classify_tool_event(action, source_exts=config.source_exts)
            if event.kind == "post_view":
                _rv = _normalize_rel_path(event.path, config)
                if _rv:
                    config.viewed_files.add(_rv)
                    config._read_history.append(_rv)
            elif event.kind == "post_edit":
                _rp = _normalize_rel_path(event.path, config)
                if _rp:
                    config.edited_files.add(_rp)
            config.last_visible_observation = obs
            config._stuck_compat_skip_count += 1
            _emit_agent_event(config, action, event, _action_file)
            print(
                f"[GT_META] STUCK_COMPAT: skip GT injection — repeated "
                f"action-obs pair (cls={act_cls} hash={_raw_hash[:8]} "
                f"ac={config.action_count} total_skips="
                f"{config._stuck_compat_skip_count})",
                flush=True,
            )
            return obs

        # Online tracker: check pending next_actions after obs is available
        obs = _check_pending_next_actions(config, current_action_file=_action_file, obs=obs)
        event = classify_tool_event(action, source_exts=config.source_exts)
        instance_ref = getattr(runtime, "_gt_instance", None)

        _aclass = _action_class(action)
        config.action_count += 1
        # Consensus curation narrowing: shrink the candidate set per band and emit
        # the [GT_CURATION] proof line on every action (not _GT_BASELINE).
        if not _GT_BASELINE:
            _emit_curation_log(config)
        # L6 pre-submit (Option 2): verifiable diff-wide test consolidation at
        # the edit→review transition, while the agent can still act. Fires once.
        if not _GT_BASELINE and not _is_finish_action:
            obs = _maybe_fire_presubmit_verify(config, obs, orig_run_action)
        if _aclass == "CmdRunAction":
            config._cmd_action_count = getattr(config, "_cmd_action_count", 0) + 1
            # Behavioral trace: track searches for rescue governor
            if re.search(r"\bgrep\b|\bfind\b|\brg\b", act_text):
                config._search_count_since_edit += 1
            if re.search(r"\bpytest\b|python -m pytest\b|python.*test", act_text):
                config._test_actions.append(config.action_count)
            if config._cmd_action_count > config.max_iter and not config.scaffold_stripped:
                _strip_scaffold_files(orig_run_action, config, instance_ref)
                _flush_interaction_log(config, instance_ref)
                _flush_task_end_metrics(config, "max_iter")
                _pull_graph_db_artifact(config)
            if "gt_validate" in act_text:
                register_gt_validate_paths(act_text, config)

            # L5 governor: detect test commands (edits handled below after event classification)
            _l5_gov = getattr(config, "_l5_governor", None)
            if _l5_gov is not None and not _GT_BASELINE and event.kind == "skip":
                try:
                    _l5d = _l5_gov.after_interaction(
                        action, obs, config.action_count, config.max_iter,
                        edited_files=config.edited_files,
                        brief_candidates=config.brief_candidates,
                        viewed_files=config.viewed_files,
                        graph_db=config.graph_db,
                        workspace_root=config.workspace_root,
                    )
                    if _l5d.fired:
                        _l5_eid = _emit_structured_event(
                            config, "L5", _l5d.hook_name,
                            evidence_items=_l5d.evidence_items,
                            verification_kind=_l5d.verification_kind,
                        )
                        if _l5d.message:
                            _l5b_eid = _emit_structured_event(
                                config, "L5b", f"intervention_{_l5d.hook_name}",
                                parent_event_id=_l5_eid,
                                rendered_text=_l5d.message,
                                next_action_type=_l5d.next_action_type,
                                next_action_file=_l5d.next_action_file,
                                next_action_test=_l5d.next_action_test,
                            )
                            obs = append_observation(obs, f"\n\n{_l5d.message}\n")
                            _log_gt_interaction(
                                config, "L5", "governor_cmd", "advisory", _l5d.message,
                                agent_action_before=act_text[:300],
                                event_id=_l5b_eid or "",
                                parent_event_id=_l5_eid or "",
                                next_action_type=_l5d.next_action_type or "",
                                next_action_file=_l5d.next_action_file or "",
                                next_action_test=_l5d.next_action_test or "",
                            )
                            _register_pending_next_action(config, _l5b_eid or "", _l5d.next_action_type or "", _l5d.next_action_file or "")
                        elif _l5d.suppressed:
                            _l5b_blk = _emit_structured_event(
                                config, "L5b", "blocked_by_safety",
                                parent_event_id=_l5_eid,
                                suppressed=True,
                                suppression_reason=_l5d.suppression_reason,
                            )
                            _log_gt_interaction(config, "L5b", "governor_cmd", "blocked", f"[blocked: {_l5d.suppression_reason}]", event_id=_l5b_blk or "")
                except Exception as l5_exc:
                    print(f"[GT_META] L5 governor error on CmdRunAction: {l5_exc}", flush=True)

            # Decision 34: Goku event-driven L5 check (runs alongside old after_interaction)
            if _l5_gov is not None and not _GT_BASELINE and os.environ.get("GT_L5_GOKU_EVENTS", "1") == "1":
                try:
                    _goku_d = _l5_gov.goku_check(
                        action, obs, config.action_count, config.max_iter,
                        file_path=_action_file or None,
                    )
                    if _goku_d.fired:
                        _goku_eid = _emit_structured_event(
                            config, "L5", _goku_d.hook_name,
                            emitted=not _goku_d.suppressed,
                            suppressed=_goku_d.suppressed,
                            suppression_reason=_goku_d.suppression_reason,
                        )
                        if _goku_d.message and not _goku_d.suppressed:
                            _goku_l5b_eid = _emit_structured_event(
                                config, "L5b", f"intervention_{_goku_d.hook_name}",
                                parent_event_id=_goku_eid,
                                rendered_text=_goku_d.message,
                                next_action_type=_goku_d.next_action_type,
                                next_action_file=_goku_d.next_action_file,
                            )
                            obs = append_observation(obs, f"\n\n{_goku_d.message}\n")
                            _log_gt_interaction(
                                config, "L5", "goku_cmd", "advisory", _goku_d.message,
                                agent_action_before=act_text[:300],
                                event_id=_goku_l5b_eid or "",
                                next_action_type=_goku_d.next_action_type or "",
                                next_action_file=_goku_d.next_action_file or "",
                            )
                            _register_pending_next_action(config, _goku_l5b_eid or "", _goku_d.next_action_type or "", _goku_d.next_action_file or "")
                except Exception as gk_exc:
                    print(f"[GT_META] L5 goku error on CmdRunAction: {gk_exc}", flush=True)

            # Grep Intercept: agent searched for a symbol — append its callers.
            # This is CONTEXTUAL augmentation (agent's own search focus), not unsolicited.
            # Decay detail level instead of hard stop: full callers+code -> file-only -> count-only
            if (
                not _GT_BASELINE
                and re.search(r"\b(grep|rg)\b", act_text)
            ):
                _gi_count = config._grep_intercept_count
                if _gi_count < 5:
                    # Full callers with code snippets
                    _gi_limit = 5
                    _gi_detail = "full"
                elif _gi_count < 10:
                    # File names only, no code
                    _gi_limit = 3
                    _gi_detail = "files_only"
                else:
                    # Count only
                    _gi_limit = 1
                    _gi_detail = "count_only"
                _grep_sym = _extract_grep_symbol(act_text)
                # Honor the agent's OWN search scope: when it grepped a symbol inside
                # a specific file, resolving the bare name repo-wide (and surfacing a
                # most-called homonym from an unrelated file) is harmful. Path-scope
                # the lookup; if the symbol isn't defined in the grepped file, stay
                # silent rather than emit a wrong-file homonym (correct-or-quiet).
                _grep_scope = _extract_grep_file_scope(act_text)
                _grep_db = getattr(config, "_host_graph_db", "") or ""
                if _grep_sym and (_grep_db and os.path.exists(_grep_db)):
                    # Host-side graph.db — ego-graph for full detail, flat for lighter modes
                    # RepoGraph ICLR 2025: agent searches → gets ego-graph as response
                    if _gi_detail == "full":
                        try:
                            from groundtruth.graph.ego import ego_graph as _ego_grep
                            # Pass the grepped file so the ego center binds to the
                            # symbol IN that file, not a repo-wide homonym.
                            _eg = _ego_grep(
                                _grep_db, _grep_sym,
                                file_path=_grep_scope or "",
                                k=1, min_confidence=0.9,
                            )
                            # If the agent scoped to a file but the center landed in a
                            # different file, the grepped file has no such symbol —
                            # stay silent instead of surfacing the homonym.
                            _ego_offscope = bool(
                                _grep_scope and _eg.center
                                and not _same_grep_scope(_eg.center.file_path, _grep_scope)
                            )
                            if _eg.center and len(_eg.callers) > 0 and not _ego_offscope:
                                _ego_text = _eg.render(max_tokens=150)
                                _grep_evidence = f"\n[GT] {_grep_sym}:\n{_ego_text}"
                                obs = append_observation(obs, _grep_evidence)
                                config._grep_intercept_count += 1
                                print(f"[GT_DELIVERY] grep_intercept_ego: symbol={_grep_sym} scope={_grep_scope or '-'} callers={len(_eg.callers)} fire={config._grep_intercept_count}", flush=True)
                                # Skip flat caller path below
                                _grep_sym = None
                            elif _ego_offscope:
                                # Scoped grep, no in-file definition -> silence.
                                print(f"[GT_META] grep_intercept_ego: symbol={_grep_sym} off-scope (scope={_grep_scope}) — silent", flush=True)
                                _grep_sym = None
                        except Exception as _ego_grep_exc:
                            print(f"[GT_META] grep_ego_fallback: {_ego_grep_exc}", flush=True)
                    if _grep_sym:
                        try:
                            # Path-scoped flat fallback. When the agent grepped a
                            # specific file, _grep_intercept_callers constrains the
                            # callee definition to that file and returns [] (silence)
                            # if the symbol isn't defined there — never a homonym.
                            _grep_callers = _grep_intercept_callers(
                                _grep_db, _grep_sym,
                                file_scope=_grep_scope,
                                limit=_gi_limit, min_conf=0.6,
                            )
                            if _grep_scope and not _grep_callers:
                                # Scoped grep with no in-file match -> stay silent.
                                print(f"[GT_META] grep_intercept: symbol={_grep_sym} scope={_grep_scope} — no in-file def, silent", flush=True)
                                raise _GrepSilent()
                            _grep_total = len(_grep_callers)
                            if _grep_callers:
                                _caller_line_parts: list[str] = []
                                if _gi_detail == "count_only":
                                    _caller_line_parts.append(f"  {_grep_total} caller(s) across codebase")
                                else:
                                    for _cfile, _cline in _grep_callers:
                                        if _gi_detail == "full":
                                            _code = ""
                                            try:
                                                _src_path = os.path.join(config.workspace_root or "/workspace", _cfile)
                                                with open(_src_path, encoding="utf-8", errors="ignore") as _sf:
                                                    for _li, _ln in enumerate(_sf, 1):
                                                        if _li == _cline:
                                                            _code = _ln.strip()[:80]
                                                            break
                                            except OSError:
                                                pass
                                            _caller_line_parts.append(
                                                f"  {_cfile}:{_cline}" + (f" `{_code}`" if _code else "")
                                            )
                                        else:
                                            _caller_line_parts.append(f"  {_cfile}:{_cline}")
                                _caller_lines = "\n".join(_caller_line_parts)
                                _scope_tag = f" in {_grep_scope}" if _grep_scope else ""
                                _grep_evidence = f"\n[GT] Callers of '{_grep_sym}'{_scope_tag}:\n{_caller_lines}"
                                obs = append_observation(obs, _grep_evidence)
                                config._grep_intercept_count += 1
                                print(
                                    f"[GT_DELIVERY] grep_intercept: symbol={_grep_sym} "
                                    f"scope={_grep_scope or '-'} callers={len(_grep_callers)} "
                                    f"detail={_gi_detail} fire={config._grep_intercept_count}",
                                    flush=True,
                                )
                            else:
                                print(f"[GT_META] grep_intercept: symbol={_grep_sym} callers=0 (no high-confidence edges)", flush=True)
                        except _GrepSilent:
                            pass
                        except Exception as _grep_exc:
                            print(f"[GT_META] grep_intercept_error: {_grep_exc}", flush=True)
                elif _grep_sym and config.graph_db:
                    # Fallback: query inside container via _container_query
                    try:
                        import json as _j_grep
                        # Parameterized (CLAUDE.md: no f-string SQL). Path-scope the
                        # callee definition to the grepped file so a repo-wide homonym
                        # is never surfaced for a file-scoped grep; the scope/symbol
                        # values flow as BOUND params (a path with a single quote like
                        # a'b/c.py is injection-safe, not hand-escaped).
                        _grep_sql, _grep_params = _build_grep_intercept_query(
                            _grep_sym,
                            file_scope=_grep_scope or None,
                            min_conf=0.6,
                            limit=_gi_limit,
                        )
                        _grep_raw = _container_query(
                            orig_run_action, config.graph_db, _grep_sql,
                            params_json=_j_grep.dumps(_grep_params),
                        )
                        _grep_rows = _j_grep.loads(_grep_raw)
                        if _grep_rows:
                            _caller_line_parts_cq: list[str] = []
                            if _gi_detail == "count_only":
                                _caller_line_parts_cq.append(f"  {len(_grep_rows)}+ caller(s) across codebase")
                            else:
                                for _row in _grep_rows:
                                    _fp = _row[0] if isinstance(_row, (list, tuple)) else ""
                                    _sl = _row[1] if isinstance(_row, (list, tuple)) and len(_row) > 1 else 0
                                    _caller_line_parts_cq.append(f"  {_fp}:{_sl}")
                            _caller_lines_cq = "\n".join(_caller_line_parts_cq)
                            _scope_tag_cq = f" in {_grep_scope}" if _grep_scope else ""
                            _grep_evidence = f"\n[GT] Callers of '{_grep_sym}'{_scope_tag_cq}:\n{_caller_lines_cq}"
                            obs = append_observation(obs, _grep_evidence)
                            config._grep_intercept_count += 1
                            print(
                                f"[GT_DELIVERY] grep_intercept(container): symbol={_grep_sym} "
                                f"callers={len(_grep_rows)} detail={_gi_detail} fire={config._grep_intercept_count}",
                                flush=True,
                            )
                        else:
                            print(f"[GT_META] grep_intercept(container): symbol={_grep_sym} callers=0", flush=True)
                    except Exception as _grep_cq_exc:
                        print(f"[GT_META] grep_intercept_container_error: {_grep_cq_exc}", flush=True)

        # Decision 34: Emit GTAgentEvent at action boundary
        _emit_agent_event(config, action, event, _action_file)

        if event.kind != "finish":
            config.last_visible_observation = obs
        if l4_recorded and tel_obj is not None:
            _write_gt_telemetry(instance_ref, tel_obj)

        # Selective rescue governor: detect stuck agent and intervene
        # Escalating: level 0 (soft), level 1 (directed), level 2 (final)
        _rescue_eligible = (
            not _GT_BASELINE
            and config.action_count > 0
            and config.action_count % 5 == 0
            and config._rescue_fired_count < 3  # max 3 rescues
            and config.action_count - config._rescue_last_action > 10  # 10-action cooldown
        )
        if _rescue_eligible:
            _agent_state = _classify_agent_state(config)
            _rescue_decision = "suppress"
            _rescue_reason = ""
            if _agent_state == "HARMFUL_SILENT":
                _rescue_msg = _build_rescue_payload(config, rescue_level=config._rescue_fired_count)
                if _rescue_msg:
                    config._rescue_fired_count += 1
                    config._rescue_last_action = config.action_count
                    config._last_gt_action = config.action_count
                    _rescue_decision = "emit"
                    obs = append_observation(obs, f"\n{_rescue_msg}")
                    _log_gt_interaction(
                        config, "L5", f"rescue:{config.action_count}", "rescue",
                        _rescue_msg, agent_action_before=act_text[:300],
                    )
                else:
                    _rescue_reason = "empty_payload"
            else:
                _rescue_reason = f"state={_agent_state}"
            print(
                f"[GT_TRACE] rescue_check ac={config.action_count} state={_agent_state} "
                f"decision={_rescue_decision} reason={_rescue_reason} "
                f"level={config._rescue_fired_count} edits={len(config._source_edit_actions)} "
                f"reads={len(config._read_history)} searches={config._search_count_since_edit} "
                f"last_gt={config._last_gt_action} tests={len(config._test_actions)}",
                flush=True,
            )

        def _hook_fatal(blob: str) -> bool:
            return "[GT_STATUS] error" in blob

        if event.kind == "post_view":
            rel_view = _normalize_rel_path(event.path, config)
            if rel_view:
                config.viewed_files.add(rel_view)
                config._read_history.append(rel_view)

            # Auto-query: on first read of a source file, auto-run gt_query
            # on the top symbol to give the agent graph context without asking.
            # Budget: max 2 auto-queries per task. Non-test source files only.
            _auto_budget = getattr(config, "_auto_query_count", 0)
            _auto_seen = getattr(config, "_auto_query_seen", set())
            if not hasattr(config, "_auto_query_seen"):
                config._auto_query_seen = set()
                config._auto_query_count = 0
            _vp = rel_view or event.path
            print(f"[GT_META] auto_query_gate: file={_vp} count={config._auto_query_count} seen={_vp in config._auto_query_seen} scaffold={_is_scaffolding_path(_vp)} test={_vp.startswith('test') or '/test' in _vp} graph_db={bool(config.graph_db)} baseline={_GT_BASELINE}", flush=True)
            # Rule 2 (R2/R3): Suppress L4a when L3b already fired for
            # this file — avoids duplicate graph summary.
            _l3b_already_fired = f"l3b_file:{_vp}" in config.evidence_sent
            if (_L4A_AUTO_QUERY_ENABLED
                and config._auto_query_count < 2
                and _vp not in config._auto_query_seen
                and not _l3b_already_fired
                and not _is_scaffolding_path(_vp)
                and not _vp.startswith("test")
                and not "/test" in _vp
                and config.graph_db
                and not _GT_BASELINE):
                try:
                    import json as _j_aq
                    _norm_vp = _vp.replace("\\", "/").lstrip("./").lstrip("/")
                    _safe_vp = _escape_like(_norm_vp).replace("'", "''")
                    # Layer 2.4: categorical edge filter (verified-only) shared
                    # with L3/L3b. L4a's unique value is verified cross-file
                    # callers the agent can't grep — NOT name_match noise.
                    # Resolve the clause from a host db copy when available.
                    _aq_host_db = getattr(config, "_host_graph_db", "") or ""
                    if not _aq_host_db or not os.path.exists(_aq_host_db):
                        _aq_host_db = config.graph_db if os.path.exists(config.graph_db) else ""
                    try:
                        from groundtruth.hooks.post_edit import _edge_filter_for_db
                        _aq_ef = _edge_filter_for_db(_aq_host_db) if _aq_host_db else "COALESCE(e.confidence,0.5) >= 0.7"
                    except Exception:
                        _aq_ef = "COALESCE(e.confidence,0.5) >= 0.7"
                    # A1 fix: also select signature for fallback when 0 callers.
                    # Rank by VERIFIED in-degree (categorical filter) so hubs of
                    # name_match noise don't dominate the "top symbols".
                    # Fetch a WIDER candidate set (LIMIT 8) so the issue-keyword
                    # boost below can rank an issue-relevant symbol that isn't in
                    # the top-2-by-verified-callers. Hub bias fix: caller count
                    # is the prior, issue relevance is a real ranking signal —
                    # not a tiebreak on 2 survivors.
                    _top_syms = _j_aq.loads(_container_query(
                        orig_run_action, config.graph_db,
                        f"SELECT n.name, n.signature FROM nodes n "
                        f"LEFT JOIN edges e ON e.target_id = n.id AND e.type='CALLS' "
                        f"AND {_aq_ef} "
                        f"WHERE n.file_path LIKE '%{_safe_vp}' ESCAPE '\\' "
                        f"AND n.label IN ('Function','Method') AND n.is_test=0 "
                        f"GROUP BY n.id ORDER BY COUNT(e.id) DESC LIMIT 8",
                    ))
                    if _top_syms:
                        _sym_names = [s[0] for s in _top_syms if s]
                        _sym_sigs = {s[0]: (s[1] or "") for s in _top_syms if s}
                        # L4b-3: Issue-keyword boost (SweRank ICLR 2025)
                        # Boost symbols whose names match issue terms
                        _issue_terms_path = "/tmp/gt_issue_terms.txt"
                        _issue_kws = set()
                        try:
                            _ikt = _run_internal(orig_run_action, f"cat {_issue_terms_path} 2>/dev/null", 5)
                            _issue_kws = {w.strip().lower() for w in _ikt.splitlines() if len(w.strip()) > 3}
                        except Exception:
                            pass
                        if _issue_kws:
                            def _kw_boost(name: str) -> int:
                                parts = set(p.lower() for p in re.split(r'[_]|(?<=[a-z])(?=[A-Z])', name) if p)
                                return len(parts & _issue_kws)
                            _sym_names.sort(key=lambda n: _kw_boost(n), reverse=True)
                        _aq_lines = []
                        for _sn in _sym_names[:2]:
                            _safe_sn = _sn.replace("'", "''")
                            _callers = _j_aq.loads(_container_query(
                                orig_run_action, config.graph_db,
                                f"SELECT nsrc.file_path, e.source_line FROM nodes nt "
                                f"JOIN edges e ON e.target_id = nt.id AND e.type='CALLS' "
                                f"AND {_aq_ef} "
                                f"JOIN nodes nsrc ON e.source_id = nsrc.id "
                                f"WHERE nt.name='{_safe_sn}' AND nt.file_path LIKE '%{_safe_vp}' ESCAPE '\\' "
                                f"AND nsrc.file_path NOT LIKE '%{_safe_vp}' ESCAPE '\\' LIMIT 3",
                            ))
                            if _callers:
                                _caller_str = ", ".join(f"{c[0]}:{c[1]}" for c in _callers[:3])
                                _aq_lines.append(f"  {_sn}() called by: {_caller_str}")
                            elif _sym_sigs.get(_sn):
                                _aq_lines.append(f"  {_sn}({_sym_sigs[_sn][:80]})")
                        if _aq_lines:
                            _aq_text = f"[GT_AUTO] Key symbols in {os.path.basename(_vp)}:\n" + "\n".join(_aq_lines)
                            obs = _deliver_or_trace(obs, _aq_text, config, "L4_auto_query", _vp, prepend=True)
                            config._auto_query_count += 1
                            config._auto_query_seen.add(_vp)
                            print(f"[GT_META] auto_query: file={_vp} symbols={_sym_names} callers={len(_aq_lines)}", flush=True)
                        else:
                            print(f"[GT_META] auto_query_no_output: file={_vp} reason=no_actionable_lines symbols_found={len(_sym_names)}", flush=True)
                            _aq_no_lines_eid = _emit_structured_event(
                                config, "L4", "auto_query_no_output",
                                emitted=False, suppressed=True,
                                suppression_reason="no_actionable_lines",
                                file_path=_vp,
                            )
                            _log_gt_interaction(config, "L4", f"auto_query:{_vp}", "no_output", "no_actionable_lines", agent_action_before=act_text[:300], event_id=_aq_no_lines_eid or "")
                    else:
                        print(f"[GT_META] auto_query_no_output: file={_vp} reason=no_symbols symbols_found=0", flush=True)
                        config._auto_query_seen.add(_vp)
                        _aq_no_symbols_eid = _emit_structured_event(
                            config, "L4", "auto_query_no_output",
                            emitted=False, suppressed=True,
                            suppression_reason="no_symbols",
                            file_path=_vp,
                        )
                        _log_gt_interaction(config, "L4", f"auto_query:{_vp}", "no_output", "no_symbols", agent_action_before=act_text[:300], event_id=_aq_no_symbols_eid or "")
                except Exception as _aq_exc:
                    print(f"[GT_META] auto_query_error: {_aq_exc}", flush=True)
                    _aq_error_eid = _emit_structured_event(
                        config, "L4", "auto_query_no_output",
                        emitted=False, suppressed=True,
                        suppression_reason="query_error",
                        file_path=_vp,
                    )
                    _log_gt_interaction(config, "L4", f"auto_query:{_vp}", "no_output", "query_error", agent_action_before=act_text[:300], event_id=_aq_error_eid or "")

            # Consensus: agent views a GT brief candidate.
            # Layer A: first candidate → scope-aware consensus (fires once, full scope)
            # Layer B: subsequent candidates → lightweight progressive confirmation
            # Runs before mode branching — independent of L3b evidence quality.
            _view_path = rel_view or event.path
            _is_candidate_cv = any(
                _same_repo_file(_view_path, c, config) for c in config.brief_candidates
            ) if config.brief_candidates else False
            _has_source_edit_cv = any(
                not _is_scaffolding_path(f) for f in config.edited_files
            ) if hasattr(config, "edited_files") and config.edited_files else False

            if not _GT_BASELINE and _is_candidate_cv and not _has_source_edit_cv:
                _view_base = os.path.basename(_view_path)
                _view_norm = _normalize_rel_path(_view_path, config)

                if not config._consensus_fired:
                    # Layer A: First consensus — detect scope and deliver full context
                    config._consensus_fired = True
                    config._consensus_turn = config.action_count
                    config._consensus_confirmed.add(_view_norm)

                    _scope = _detect_scope(_view_path, config, orig_run_action)
                    config._consensus_scope = [_view_norm] + [s["file"] for s in _scope]

                    if _scope:
                        # parent/basename so beets/util/__init__.py and
                        # beets/autotag/__init__.py do not both collapse to a
                        # bare "__init__.py" (indistinguishable scope entries).
                        def _scope_short(_p: str) -> str:
                            _r = (_p or "").replace("\\", "/")
                            return "/".join(_r.split("/")[-2:]) if "/" in _r else _r
                        _scope_lines = []
                        # Multi-signal gate: only anoint "primary target" when the
                        # localizer reports high confidence (>=2 of grep/semantic/
                        # structural agree). Otherwise a single-signal mis-rank that
                        # the agent happens to open would be falsely confirmed.
                        if config._localization_confidence == "high":
                            _scope_lines.append(f"1. {_scope_short(_view_path)} — primary target")
                        else:
                            _scope_lines.append(f"1. {_scope_short(_view_path)} — in scope (you are viewing this)")
                        for idx, s in enumerate(_scope[:4], 2):
                            _sbase = _scope_short(s["file"])
                            _scope_lines.append(f"{idx}. {_sbase} — {s['reason']}")
                        _consensus_msg = (
                            f'\n<gt-scope files="{len(_scope) + 1}">\n'
                            + "\n".join(_scope_lines)
                            + (
                                f"\nYou do not need to modify every file listed.\n"
                                if config._localization_confidence == "high"
                                else f"\nThese files are related in scope; GT has not "
                                     f"confirmed a single primary target — confirm the "
                                     f"edit target with grep.\n"
                            )
                            + f"</gt-scope>\n"
                        )
                    else:
                        _consensus_msg = (
                            f'\n<gt-scope files="1">\n'
                            f"{_view_base} is the file you're viewing; GT could not "
                            f"expand scope from the graph — confirm the edit target with grep.\n"
                            f"</gt-scope>\n"
                        )

                    print(
                        f"[GT_DELIVERY] CONSENSUS at action={config.action_count} "
                        f"file={_view_path} scope={len(config._consensus_scope)}",
                        flush=True,
                    )
                    obs = _deliver_or_trace(obs, _consensus_msg, config, "l3b", _view_path, prepend=True)
                    _log_gt_interaction(
                        config, "L2", f"consensus:{_view_path}", "confirmed",
                        _consensus_msg, agent_action_before=act_text[:300],
                    )

                elif _view_norm not in config._consensus_confirmed:
                    # Layer B: Progressive confirmation — lightweight "also in scope"
                    config._consensus_confirmed.add(_view_norm)
                    _in_scope = any(
                        _same_repo_file(_view_norm, sf, config) for sf in config._consensus_scope
                    )
                    if _in_scope:
                        _prog_msg = f"\n[GT] {_view_base}: also in scope.\n"
                        print(
                            f"[GT_DELIVERY] CONSENSUS_PROGRESSIVE action={config.action_count} file={_view_path}",
                            flush=True,
                        )
                        obs = _deliver_or_trace(obs, _prog_msg, config, "l3b", _view_path)
                        _log_gt_interaction(
                            config, "L2", f"consensus_prog:{_view_path}", "confirmed",
                            _prog_msg, agent_action_before=act_text[:300],
                        )

            # FINAL_ARCH_V2 router. Modes: off / shadow / live.
            #   shadow → run alongside legacy path; no observation mutation.
            #   live   → when the router EMITS it runs the legacy hook in-container
            #            and delivers; when the router stays silent, execution FALLS
            #            THROUGH to the legacy L3b path below (see the not-emit branch).
            #            The router selects WHICH path delivers — it NEVER suppresses
            #            L3b delivery. (Invariant guarding the prior "router killed L3" bug.)
            _v2_mode_pv = _router_v2_mode()
            try:
                _v2_event_pv = _router_v2_on_view(config, event.path)
            except Exception as _rv2_exc:
                print(f"[GT_META] router_v2 on_view CRASHED: {type(_rv2_exc).__name__}: {_rv2_exc}", flush=True)
                _v2_event_pv = None
            # BASELINE: suppress all L3b injection (track views for telemetry only)
            if _GT_BASELINE:
                return obs
            _router_v2_pv_emit = bool(
                _v2_mode_pv == "live"
                and _v2_event_pv
                and _v2_event_pv.get("emit")
            )
            if _v2_mode_pv == "live" and not _router_v2_pv_emit:
                # Router suppressed — fall through to legacy L3b path.
                # Do NOT skip evidence delivery just because the router said no.
                _write_router_v2_legacy_skip(
                    config,
                    trigger="on_view",
                    file_path=rel_view or event.path,
                    router_emitted=False,
                )
                print(f"[GT_TRACE] l3b router_suppressed, falling through to legacy path file={rel_view or event.path}", flush=True)
            if _router_v2_pv_emit:
                # Router approved emission — run the legacy hook in-container
                # to get the actual evidence text (graph.db is there).
                _write_router_v2_legacy_skip(
                    config,
                    trigger="on_view",
                    file_path=rel_view or event.path,
                    router_emitted=True,
                )
                if config.viewed_files:
                    _write_text_to_container(
                        orig_run_action,
                        "\n".join(sorted(config.viewed_files)) + "\n",
                        "/tmp/gt_viewed.txt",
                    )
                hook_out = _run_internal(orig_run_action, make_view_hook_command(event, config), 30)
                # F2: re-emit container GT_META lines to host stdout for GHA visibility
                for _meta_ln in hook_out.strip().splitlines():
                    if _meta_ln.strip().startswith("[GT_META]"):
                        print(_meta_ln.strip(), flush=True)
                # HOST-SIDE FALLBACK for L3b inside router_v2 path:
                # Non-Python containers lack pydantic → hook returns empty/status-only.
                # Run graph_navigation HOST-SIDE using the host graph.db copy.
                _l3b_has_ev_r2 = any(m in hook_out for m in ("[CONTRACT]", "[SIGNATURE]", "Called by:", "Calls into:", "[CATCHES]", "[COMPLETENESS]", "Imported by:", "[RAISES]", "PRESERVE:", "[TEST]"))
                _l3b_fb_db = getattr(config, "_host_graph_db", "") or os.environ.get("GT_PREBUILT_GRAPH_DB", "")
                if (not hook_out.strip() or _hook_fatal(hook_out) or not _l3b_has_ev_r2) and _l3b_fb_db and os.path.exists(_l3b_fb_db):
                    try:
                        from groundtruth.hooks.post_view import graph_navigation as _host_gn_r2
                        _l3b_r2_file = (rel_view or event.path).replace("\\", "/")
                        _l3b_r2_lines, _ = _host_gn_r2(
                            _l3b_r2_file, _l3b_fb_db,
                            limit=config.max_items,
                        )
                        if _l3b_r2_lines:
                            hook_out = "\n".join(_l3b_r2_lines)
                            print(
                                f"[GT_META] L3b host-side fallback OK (router_v2) for {_l3b_r2_file} "
                                f"({len(hook_out)} chars)",
                                flush=True,
                            )
                    except Exception as _l3b_r2_exc:
                        print(f"[GT_META] L3b host-side fallback failed (router_v2): {_l3b_r2_exc}", flush=True)
                hook_body = "\n".join(
                    ln for ln in hook_out.strip().splitlines()
                    if not _is_hidden_line(ln)
                )
                # Strip __GT_STRUCTURED__ JSON from agent-visible text
                if "__GT_STRUCTURED__" in hook_body:
                    hook_body = hook_body.split("__GT_STRUCTURED__")[0].strip()
                # Extract next-action from evidence
                _next_file = ""
                for _eline in hook_body.splitlines():
                    _eline_s = _eline.strip()
                    if "Called by:" in _eline_s or "Calls into:" in _eline_s:
                        import re as _re_next
                        _fm = _re_next.search(r"(\S+\.(?:py|go|js|ts|rs|java|rb))", _eline_s)
                        if _fm:
                            _next_file = _fm.group(1)
                            break
                _viewed_basename = os.path.basename(rel_view or event.path).rsplit(".", 1)[0]
                # Repair directive removed — was wrong 4/4 times in canary.
                # Fired on any file view matching brief_candidates, not just
                # the actual edit target. Noise that derails the agent.
                _formatted = f"[GT] {_viewed_basename}:\n{hook_body}\n"
                # Delivery invariant: uses shared marker contract
                obs = _deliver_or_trace(obs, _formatted, config, "l3b", rel_view or event.path, prepend=True)
                if has_gt_evidence(_formatted, "l3b"):
                    _persist_router_v2_event(config, {
                        **(_v2_event_pv or {}),
                        "evidence_source": "in_container_hook",
                        "evidence_text": hook_body[:500],
                        "next_action_file": _next_file,
                    })
                    _cache_key = rel_view or event.path
                    if hook_body and _cache_key:
                        _first_line = hook_body.strip().split("\n")[0][:120]
                        config.evidence_cache[_cache_key] = _first_line
                    config._l3b_fire_count += 1
                    config._gt_delivered_evidence_files[_cache_key] = config.action_count
                return obs
            # Budget caps removed — dedup is the sole gate.
            # Fire count still tracked for telemetry.
            # Write trajectory files for L3b (post_view hook reads these)
            if config.viewed_files:
                _write_text_to_container(
                    orig_run_action,
                    "\n".join(sorted(config.viewed_files)) + "\n",
                    "/tmp/gt_viewed.txt",
                )
            # Pre-gen recovery: do NOT write brief_candidates to container.
            # G6 gate in post_edit.py reads this file — if it exists, G6 fires.
            # Pre-gen baseline had this file NEVER written (Decision 29 line 702).
            hook_out = _run_internal(orig_run_action, make_view_hook_command(event, config), 30)
            if _hook_fatal(hook_out):
                print(f"GT HOOK ERROR (post_view:{event.path}): {hook_out[:400]}", flush=True)
            # HOST-SIDE FALLBACK for L3b: non-Python containers lack pydantic →
            # post_view crashes → empty/fatal output. Run graph_navigation HOST-SIDE
            # using the host graph.db copy. Same evidence, all 5 languages.
            _l3b_has_evidence = any(m in hook_out for m in ("[CONTRACT]", "[SIGNATURE]", "Called by:", "Calls into:", "[CATCHES]", "[COMPLETENESS]", "Imported by:", "[RAISES]", "PRESERVE:", "[TEST]"))
            _l3b_fallback_db = getattr(config, "_host_graph_db", "") or os.environ.get("GT_PREBUILT_GRAPH_DB", "")
            if (_hook_fatal(hook_out) or not hook_out.strip() or not _l3b_has_evidence) and _l3b_fallback_db and os.path.exists(_l3b_fallback_db):
                try:
                    from groundtruth.hooks.post_view import graph_navigation as _host_gn
                    _l3b_file = (rel_view or event.path).replace("\\", "/")
                    _l3b_lines, _ = _host_gn(
                        _l3b_file, _l3b_fallback_db,
                        limit=config.max_items,
                    )
                    if _l3b_lines:
                        hook_out = "\n".join(_l3b_lines)
                        print(
                            f"[GT_META] L3b host-side fallback OK for {_l3b_file} "
                            f"({len(hook_out)} chars)",
                            flush=True,
                        )
                except Exception as _l3b_exc:
                    print(f"[GT_META] L3b host-side fallback failed: {_l3b_exc}", flush=True)
            if tel_obj is not None:
                fatal = _hook_fatal(hook_out)
                empty_ev = any(
                    marker in hook_out
                    for marker in (
                        "[GT_STATUS] empty",
                        "[GT_STATUS] no_evidence:",
                        "[GT_STATUS] skipped:",
                    )
                )
                ok_ev = has_gt_evidence(hook_out, "l3b")
                tel_obj.record_hook("L3b", ok_ev and not fatal, empty=empty_ev or (not hook_out.strip()))
                _write_gt_telemetry(instance_ref, tel_obj)
            hook_body = "\n".join(
                ln for ln in hook_out.strip().splitlines()
                if not _is_hidden_line(ln)
            )
            if not has_gt_evidence(hook_body, "l3b"):
                _l3b_ok_eid = _emit_structured_event(config, "L3b", "navigation_suppressed", emitted=False, suppressed=True, suppression_reason="no_evidence", file_path=rel_view or event.path)
                _log_gt_interaction(config, "L3b", f"post_view:{rel_view or event.path}", "GT_OK", "[GT_OK] No concerns.", agent_action_before=act_text[:300], event_id=_l3b_ok_eid or "")
                return obs

            # HARD CAP: max 2 L3b deliveries per file, regardless of reindex
            # or recency. After 2 deliveries the agent has seen the evidence
            # twice — more is noise. This prevents the L6 reindex → re-view
            # cycle from delivering 7x for the same file.
            _l3b_count_key = f"l3b_count:{rel_view or event.path}"
            _l3b_delivery_count = config.evidence_sent.get(_l3b_count_key, 0)
            if _l3b_delivery_count >= 2:
                print(f"[GT_META] l3b_max_deliveries: suppressed for {rel_view or event.path} (already delivered {_l3b_delivery_count}x)", flush=True)
                _l3b_max_eid = _emit_structured_event(config, "L3b", "max_delivery_cap", emitted=False, suppressed=True, suppression_reason=f"max_2_deliveries_reached:{_l3b_delivery_count}", file_path=rel_view or event.path)
                _log_gt_interaction(config, "L3b", f"post_view:{rel_view or event.path}", "max_cap", f"[max_cap:{_l3b_delivery_count}]", agent_action_before=act_text[:300], event_id=_l3b_max_eid or "")
                return obs

            # DEDUP-INV-1 (hybrid): Per-file-once gate blocks pure re-reads.
            # Reset after L6 reindex (graph changed → callers may differ).
            # Hash-based dedup remains as safety net for post-reindex re-reads
            # where content didn't actually change.
            # Research: Du et al. EMNLP 2025, OCD/SWEzze 2026, Lost in Middle.
            #
            # RECENCY EXCEPTION: if the last L3b delivery for this file was
            # more than 50 actions ago, allow re-delivery. The agent's context
            # window has moved on; the evidence is effectively lost. Research:
            # Lost in the Middle (Liu+ NeurIPS 2024) — information placed far
            # back in context is effectively invisible to the model. On ANY
            # task, an agent that re-reads a file 50+ actions later needs the
            # evidence refreshed.
            _l3b_file_key = f"l3b_file:{rel_view or event.path}"
            _l3b_last_delivery = config.evidence_sent.get(_l3b_file_key)
            if _l3b_last_delivery is not None:
                _actions_since_l3b = config.action_count - _l3b_last_delivery
                if _actions_since_l3b < 50:
                    print(f"[GT_META] l3b_per_file_once: suppressed re-read of {rel_view or event.path} (delivered {_actions_since_l3b} actions ago)", flush=True)
                    _l3b_pfo_eid = _emit_structured_event(config, "L3b", "per_file_once_gate", emitted=False, suppressed=True, suppression_reason="already_delivered_no_reindex", file_path=rel_view or event.path)
                    _log_gt_interaction(config, "L3b", f"post_view:{rel_view or event.path}", "per_file_once", "[per_file_once]", agent_action_before=act_text[:300], event_id=_l3b_pfo_eid or "")
                    return obs
                else:
                    print(f"[GT_META] l3b_recency_exception: re-delivering for {rel_view or event.path} ({_actions_since_l3b} actions since last delivery)", flush=True)
            config.evidence_sent[_l3b_file_key] = config.action_count
            config.evidence_sent[_l3b_count_key] = _l3b_delivery_count + 1

            # Hash-based dedup: safety net for post-reindex re-reads where
            # graph changed but callers didn't. Strip visited_files-variant
            # content before hashing to avoid false negatives.
            _dedup_body_view = hook_body.split("__GT_STRUCTURED__")[0].strip() if "__GT_STRUCTURED__" in hook_body else hook_body
            if _dedup_body_view.startswith("[RECALL]"):
                _recall_end = _dedup_body_view.find("\n")
                if _recall_end > 0:
                    _dedup_body_view = _dedup_body_view[_recall_end + 1:]
            _dedup_hash_view = hashlib.md5(_dedup_body_view.strip().encode("utf-8", errors="replace")).hexdigest()
            _dedup_key_view = f"l3b:{rel_view or event.path}:{_dedup_hash_view}"
            if _dedup_key_view in config.evidence_sent:
                print(f"[GT_META] dedup_suppressed: layer=l3b file={rel_view or event.path} reason=hash_match_after_reindex", flush=True)
                _l3b_dd_eid = _emit_structured_event(config, "L3b", "navigation_dedup", emitted=False, suppressed=True, suppression_reason="hash_duplicate", file_path=rel_view or event.path)
                _log_gt_interaction(config, "L3b", f"post_view:{rel_view or event.path}", "dedup", "[dedup]", agent_action_before=act_text[:300], event_id=_l3b_dd_eid or "")
                return obs
            config.evidence_sent[_dedup_key_view] = True
            suggestion = ""
            if "[GT_STATUS] no_evidence:" in hook_out:
                stem = Path(rel_view or event.path).stem or "symbol"
                suggestion = f"\nNo coupling data. Try: gt_search function {stem}"
            print(f"[GT_META] L3b post_view evidence for {rel_view or event.path} ({len(hook_body)} chars)", flush=True)

            # CURATION GATE: L3b only injects into agent context when it helps
            # focus, not when it causes exploration spiral.
            # Rule: inject ONLY if agent has not yet made a durable source edit
            # OR if this file is a brief candidate (agent is deepening, not wandering).
            # After first source edit: structured telemetry only, zero agent injection.
            _has_source_edit = any(
                not _is_scaffolding_path(f) for f in config.edited_files
            ) if hasattr(config, "edited_files") and config.edited_files else False
            _is_candidate = (rel_view or event.path) in config.brief_candidates if hasattr(config, "brief_candidates") else False

            _l3b_should_inject = (not _has_source_edit) or _is_candidate
            if not _l3b_should_inject:
                _l3b_suppress_eid = _emit_structured_event(
                    config, "L3b", "navigation_suppressed_post_edit",
                    emitted=False, suppressed=True,
                    suppression_reason="agent_has_source_edit_and_file_not_candidate",
                    file_path=rel_view or event.path,
                )
                _log_gt_interaction(
                    config, "L3b", f"post_view:{rel_view or event.path}", "suppressed",
                    hook_body, agent_action_before=act_text[:300],
                    event_id=_l3b_suppress_eid or "",
                )
                return obs
            # Extract primary-edge next_action from structured data + LSP verification
            _l3b_nat = ""
            _l3b_naf = ""
            _l3b_verified_method = "not_verified"
            if os.environ.get("GT_L3B_PRIMARY_EDGE", "0") == "1" and hook_out and "__GT_STRUCTURED__" in hook_out:
                try:
                    _sp = hook_out.split("__GT_STRUCTURED__", 1)[1].strip().splitlines()[0]
                    _si_list = json.loads(_sp)
                    _edge_candidates = [si for si in _si_list if si.get("file_path") and si.get("kind") in ("l3b_caller_edge", "l3b_callee_edge", "l3b_importer_edge")]
                    _verifier = getattr(config, "_edge_verifier", None)

                    _lsp_flag = os.environ.get("GT_LSP_VERIFY", "1")
                    if _verifier and _lsp_flag == "1":
                        from groundtruth.lsp.edge_verifier import verify_edge_sync
                        for _cand in _edge_candidates[:3]:
                            _detail = _get_edge_detail_in_container(orig_run_action, config.graph_db, rel_view or event.path, _cand["file_path"])
                            if _detail:
                                _vedge = verify_edge_sync(
                                    config.workspace_root,
                                    target_file=rel_view or event.path,
                                    target_symbol=_detail[0],
                                    target_line=_detail[1],
                                    caller_file=_cand["file_path"],
                                    original_confidence=_detail[2],
                                    timeout=5.0,
                                )
                                if _vedge.verified:
                                    _l3b_nat = "READ_CALLER_CONTRACT" if _cand["kind"] == "l3b_caller_edge" else "READ_CONSUMER"
                                    _l3b_naf = _cand["file_path"]
                                    _l3b_verified_method = _vedge.method
                                    print(f"[GT_META] L3b edge VERIFIED: {_cand['file_path']} calls {rel_view} ({_vedge.latency_ms}ms)", flush=True)
                                    break
                                else:
                                    print(f"[GT_META] L3b edge REJECTED: {_cand['file_path']} (false positive)", flush=True)
                            else:
                                if _cand.get("primary_edge"):
                                    _l3b_nat = "READ_CALLER_CONTRACT" if _cand["kind"] == "l3b_caller_edge" else "READ_CONSUMER"
                                    _l3b_naf = _cand["file_path"]
                                    _l3b_verified_method = "fallback_no_edge_detail"
                                    break
                    else:
                        for _si in _edge_candidates:
                            if _si.get("primary_edge"):
                                _l3b_nat = "READ_CALLER_CONTRACT" if _si["kind"] == "l3b_caller_edge" else "READ_CONSUMER"
                                _l3b_naf = _si["file_path"]
                                _l3b_verified_method = "no_verifier"
                                break
                except Exception as _l3b_exc:
                    print(f"[GT_META] L3b structured parse error: {_l3b_exc}", flush=True)
            # Strands-style: agent sees compact navigation, full detail → JSONL only
            agent_body = hook_body.split("__GT_STRUCTURED__")[0].strip() if "__GT_STRUCTURED__" in hook_body else hook_body
            directive_lines = [
                ln.strip() for ln in agent_body.splitlines()
                if ln.strip()
                and not ln.strip().startswith("[GT_STATUS]")
                and not _is_hidden_line(ln)
                and not ln.strip().startswith("<")
                and not ln.strip().startswith("</")
            ]
            # Line budget removed — dedup is the sole gate. All directive lines pass through.
            nav_lines = directive_lines
            nav_text = "\n".join(nav_lines)
            # Temporal correctness: suppress "Next: read X" if agent already viewed X
            _l3b_naf_stale = False
            if _l3b_naf:
                _naf_norm = _normalize_rel_path(_l3b_naf, config)
                _l3b_naf_stale = (_naf_norm in config.viewed_files) or (_l3b_naf in config.viewed_files)
            if nav_text:
                _view_base = os.path.basename(rel_view or event.path)
                # Repair directive removed — was wrong 4/4 times in canary.
                # Fired on any viewed file, not just the edit target.
                evidence = f'\n\n<gt-context file="{_view_base}">\n{nav_text}</gt-context>\n'
            else:
                evidence = ""
            _l3b_eid = _emit_structured_event(
                config, "L3b", "navigation",
                rendered_text=hook_body,
                file_path=rel_view or event.path,
                hook_output=hook_out or "",
                next_action_type=_l3b_nat or None,
                next_action_file=_l3b_naf or None,
            )
            _log_gt_interaction(
                config, "L3b", f"post_view:{rel_view or event.path}", "evidence",
                hook_body, agent_action_before=act_text[:300],
                event_id=_l3b_eid or "",
                next_action_type=_l3b_nat,
                next_action_file=_l3b_naf,
            )
            _register_pending_next_action(config, _l3b_eid or "", _l3b_nat, _l3b_naf)
            _feed_gt_next_action_to_l5(config, _l3b_nat, _l3b_naf)
            # Char cap removed — dedup is the sole gate.
            print(f"[GT_DELIVERY] L3b post_view: evidence_len={len(evidence)} file={rel_view or event.path} fire={config._l3b_fire_count+1}", flush=True)
            if not evidence.strip():
                print(f"[GT_DELIVERY] L3b EMPTY EVIDENCE! nav_lines={nav_lines!r}", flush=True)
            config._l3b_fire_count += 1
            _ev_key = rel_view or event.path
            if _ev_key:
                config._gt_delivered_evidence_files[_ev_key] = config.action_count
            return _deliver_or_trace(obs, evidence, config, "l3b", rel_view or event.path)

        if event.kind == "post_edit":
            # --- Phase 1: Record edit state BEFORE any L5 checks (Decision 30, Bug 5 fix) ---
            rel_p = _normalize_rel_path(event.path, config)
            if rel_p:
                config.edited_files.add(rel_p)
                if not _is_scaffolding_path(rel_p) and not _is_test_path(rel_p):
                    config._source_edit_actions.append(config.action_count)
                    config._search_count_since_edit = 0
                    # L6 pre-submit (Option 2): track source files edited this
                    # task + reset the review-phase clock on each new edit.
                    config._presubmit_edited_files.add(rel_p)
                    config._presubmit_last_edit_action = config.action_count
                _emit_belief_event(config, rel_p, "unverified", "agent edited source file")
            # FINAL_ARCH_V2 router. Modes: off / shadow / live.
            #   shadow → run alongside legacy; no observation mutation.
            #   live   → when the router EMITS it runs the legacy edit hook
            #            (generate_improved_evidence / make_edit_hook_command) and
            #            delivers; when the router stays silent, execution FALLS
            #            THROUGH to that legacy L3 block below. The router selects
            #            WHICH path delivers — it NEVER suppresses L3 delivery.
            #            (Invariant guarding the prior "router killed L3" bug.)
            _v2_mode_pe = _router_v2_mode()
            try:
                _v2_event_pe = _router_v2_on_edit(config, event.path, [])
            except Exception as _rv2_exc:
                print(f"[GT_META] router_v2 on_edit CRASHED: {type(_rv2_exc).__name__}: {_rv2_exc}", flush=True)
                _v2_event_pe = None
            _record_edit_iter(config, config.action_count, event.path)
            _record_diff_snapshot(orig_run_action, config, event.path, config.action_count)

            # Track per-file edit counts for edit loop detection
            edit_key = rel_p or event.path
            config._l5_edit_counts_per_file[edit_key] = config._l5_edit_counts_per_file.get(edit_key, 0) + 1

            # L5 governor: notify about source edit so it can track for Hypothesis Falsified
            _l5_gov = getattr(config, "_l5_governor", None)
            if _l5_gov is not None and not _GT_BASELINE:
                try:
                    _l5_gov.state.update_iter(config.action_count, config.max_iter)
                    if _is_real_source_edit(event.path, config):
                        _l5_gov.state.record_source_edit(rel_p or event.path)
                        print(f"[GT_META] L5 governor: tracked source edit {rel_p or event.path}", flush=True)
                    _l5_gov.state.save()
                except Exception as l5_exc:
                    print(f"[GT_META] L5 governor edit tracking error: {l5_exc}", flush=True)

            # Old L5 triggers (33/66 checkpoints, non-source, diff-collapsed, edit-loop) REMOVED.
            # All L5 logic now goes through the governor (Decision 31).

            # Pre-gen recovery: do NOT write brief_candidates to container (Decision 29 line 702).
            if config.edited_files:
                _write_text_to_container(
                    orig_run_action,
                    "\n".join(sorted(config.edited_files)) + "\n",
                    "/tmp/gt_edited_files.txt",
                )

            # --- Phase 3: Scaffolding early-exit (reindex + skip L3) ---
            if _is_scaffolding_path(event.path):
                if config._first_scaffold_iter == 0:
                    config._first_scaffold_iter = config.action_count
                reindex_cmd = make_reindex_command(event.path, config)
                reindex_out = _run_internal(orig_run_action, reindex_cmd, 120)
                if tel_obj is not None:
                    r_ok = bool(reindex_out.strip()) and not any(
                        w in reindex_out.lower() for w in ("panic", "fatal")
                    )
                    tel_obj.record_reindex(r_ok)
                    tel_obj.record_hook("L3", ok=False, empty=True)
                    _write_gt_telemetry(instance_ref, tel_obj)
                if config._l5_scaffold_fired:
                    config._l5_metrics["num_additional_scaffolds_after_l5"] += 1

                _scaffold_eid = _emit_structured_event(
                    config,
                    "L3",
                    "post_edit_scaffold_skip",
                    emitted=False,
                    suppressed=True,
                    suppression_reason="scaffolding_file",
                    file_path=event.path,
                )
                _log_gt_interaction(config, "L3", f"post_edit:{event.path}", "scaffold_skip", "skipped:scaffolding_file", agent_action_before=act_text[:300], event_id=_scaffold_eid or "")
                return obs

            # --- Phase 4: L6 reindex BEFORE L3 post_edit hook (sequential ordering is load-bearing) ---
            reindex_cmd = make_reindex_command(event.path, config)
            if not reindex_cmd:
                if tel_obj is not None:
                    tel_obj.record_reindex(False)
                print(f"[GT_META] L6 reindex SKIPPED (binary unavailable) for {event.path}", flush=True)
                _l6_skip_eid = _emit_structured_event(config, "L6", "reindex_skip", emitted=False, suppressed=True, suppression_reason="binary_unavailable", file_path=event.path)
                _log_gt_interaction(config, "L6", f"reindex:{event.path}", "reindex_skip", "binary unavailable", agent_action_before=act_text[:300], event_id=_l6_skip_eid or "")
            else:
                mtime_before_raw = _run_internal(
                    orig_run_action,
                    f"stat -c %Y {config.graph_db} 2>/dev/null || echo 0",
                    5,
                ).strip()
                mtime_before = int(mtime_before_raw or "0")
                _reindex_start = time.time()

                reindex_out = _run_internal(orig_run_action, reindex_cmd + "; echo __EXIT__$?", 120)

                exit_code = 1
                if "__EXIT__" in reindex_out:
                    parts = reindex_out.rsplit("__EXIT__", 1)
                    reindex_out = parts[0]
                    try:
                        exit_code = int(parts[1].strip())
                    except ValueError:
                        exit_code = 1

                mtime_after_raw = _run_internal(
                    orig_run_action,
                    f"stat -c %Y {config.graph_db} 2>/dev/null || echo 0",
                    5,
                ).strip()
                mtime_after = int(mtime_after_raw or "0")

                r_ok = (exit_code == 0) and (mtime_after > mtime_before)

                if r_ok and any(w in reindex_out.lower() for w in (
                    "panic", "fatal", "no such file", "not found", "cannot execute", "permission denied"
                )):
                    r_ok = False

                if tel_obj is not None:
                    tel_obj.record_reindex(r_ok)
                # DEDUP-INV-1 hybrid: reset per-file-once gate for the EDITED
                # file only (not all files). After reindex, graph data for
                # this file changed — L3b should re-deliver if agent re-reads.
                # Other files' gates stay intact to prevent duplication.
                if r_ok:
                    _edited_rel = _normalize_rel_path(event.path, config) or event.path
                    _pfo_key = f"l3b_file:{_edited_rel}"
                    if _pfo_key in config.evidence_sent:
                        del config.evidence_sent[_pfo_key]
                # C6 step-4 (RF-3 (d)): the reindex re-resolved the edited file's
                # edges WITHOUT LSP, demoting any promoted lsp edges for this file
                # back to name_match. Re-apply the offline promotion scoped to the
                # edited file so the in-container hooks keep reading verified edges.
                # Gated on _gt_prebuilt_active — a pure no-op on the default path.
                if r_ok and getattr(config, "_gt_prebuilt_active", False):
                    _edited_rel_rp = _normalize_rel_path(event.path, config) or event.path
                    try:
                        _repromote_status = _repromote_after_reindex(
                            runtime, config, orig_run_action, _edited_rel_rp
                        )
                    except Exception as _rp_exc:
                        _repromote_status = f"error:{type(_rp_exc).__name__}"
                    print(
                        f"[GT_META] L6 re-promotion after reindex for {_edited_rel_rp}: "
                        f"{_repromote_status}",
                        flush=True,
                    )
                print(f"[GT_META] L6 reindex {'OK' if r_ok else 'FAIL'} for {event.path} (exit={exit_code}, mtime_delta={mtime_after - mtime_before}, l3b_gates_reset={r_ok})", flush=True)
                _reindex_latency = int((time.time() - _reindex_start) * 1000)
                _l6_eid = _emit_structured_event(
                    config, "L6", "reindex",
                    emitted=True, suppressed=False,
                    file_path=event.path,
                    evidence_items=[{
                        "kind": "l6_reindex",
                        "file_path": event.path,
                        "reason": f"reindex {'success' if r_ok else 'failed'}: exit={exit_code}",
                        "text": f"latency_ms={_reindex_latency} mtime_delta={mtime_after - mtime_before}",
                    }],
                )
                _log_gt_interaction(
                    config, "L6", f"reindex:{event.path}",
                    "reindex_ok" if r_ok else "reindex_fail",
                    reindex_out[:200], agent_action_before=act_text[:300],
                    event_id=_l6_eid or "",
                )
                # [7] L6 auto-consumer: DISABLED (15.6s overhead, 0 agent-visible impact)
                # Was: query caller count per-reindex. Never injected into agent observation.
                # Reindex itself is kept (refreshes graph.db for L3/L4).

            # Download graph.db to host after successful reindex — but respect
            # proxy mode (A4 fix: post-reindex was ignoring GT_GRAPH_DB_TRANSFER).
            if locals().get("r_ok"):
                _post_reindex_mode = os.environ.get("GT_GRAPH_DB_TRANSFER", "proxy").lower()
                if _post_reindex_mode == "proxy":
                    try:
                        import json as _j_pr
                        _nc_raw = _container_query(runtime, config.graph_db, "SELECT COUNT(*) FROM nodes")
                        _nc = _j_pr.loads(_nc_raw)
                        _node_count_pr = _nc[0][0] if _nc else 0
                        _l5g_pr = getattr(config, "_l5_governor", None)
                        if _l5g_pr and _node_count_pr > 0:
                            if _node_count_pr > 5000:
                                _l5g_pr._cached_scaffold_threshold = 35
                            elif _node_count_pr > 1000:
                                _l5g_pr._cached_scaffold_threshold = 25
                            else:
                                _l5g_pr._cached_scaffold_threshold = 20
                        if hasattr(config, "_router_v2"):
                            config._router_v2 = None  # type: ignore[attr-defined]
                        print(f"[GT_META] graph.db post-reindex refresh (proxy mode): node_count={_node_count_pr} router_v2_reset=True", flush=True)
                    except Exception as _pr_exc:
                        print(f"[GT_META] graph.db proxy refresh error after L6: {_pr_exc}", flush=True)
                else:
                    try:
                        _local_db = _download_graph_db_to_host(runtime, config.graph_db)
                        if _local_db:
                            _prev_host_db = config._host_graph_db
                            config._host_graph_db = _local_db
                            if hasattr(config, "_router_v2"):
                                config._router_v2 = None  # type: ignore[attr-defined]
                            print(
                                f"[GT_META] graph.db refreshed to host after L6 reindex: "
                                f"{_local_db} (prev={_prev_host_db or 'none'}) "
                                f"router_v2_reset=True",
                                flush=True,
                            )
                        else:
                            print("[GT_META] graph.db download failed after L6 reindex", flush=True)
                    except Exception as dl_exc:
                        print(f"[GT_META] graph.db download error after L6: {dl_exc}", flush=True)

            # --- Phase 5: L3 post_edit hook ---
            # BASELINE: suppress L3 evidence injection entirely
            if _GT_BASELINE:
                return obs
            # LIVE router-v2: when the router approves emission, use the
            # router-approved path. When it suppresses, fall through to the
            # legacy L3 path so evidence is still delivered.
            _router_v2_pe_emit = bool(
                _v2_mode_pe == "live"
                and _v2_event_pe
                and _v2_event_pe.get("emit")
            )
            if _v2_mode_pe == "live" and not _router_v2_pe_emit:
                # Router suppressed — fall through to legacy L3 path.
                _write_router_v2_legacy_skip(
                    config,
                    trigger="on_edit",
                    file_path=rel_p or event.path,
                    router_emitted=False,
                )
                print(f"[GT_TRACE] l3 router_suppressed, falling through to legacy path file={rel_p or event.path}", flush=True)
            if _router_v2_pe_emit:
                _write_router_v2_legacy_skip(
                    config,
                    trigger="on_edit",
                    file_path=rel_p or event.path,
                    router_emitted=True,
                )
                # Router approved — run legacy hook in-container for evidence.
                diff_text_live, old_content_live = _extract_diff_and_old_content(obs)
                diff_path_live = ""
                old_content_path_live = ""
                if diff_text_live:
                    diff_path_live = "/tmp/gt_diff.txt"
                    _write_text_to_container(orig_run_action, diff_text_live, diff_path_live)
                if old_content_live:
                    old_content_path_live = "/tmp/gt_old.txt"
                    _write_text_to_container(orig_run_action, old_content_live, old_content_path_live)
                _l3_ratio_live = config.action_count / max(config.max_iter, 1)
                hook_out = _run_internal(
                    orig_run_action,
                    make_edit_hook_command_with_artifacts(
                        event, config,
                        diff_path=diff_path_live or None,
                        old_content_path=old_content_path_live or None,
                        mode="post_edit",
                        iteration_ratio=_l3_ratio_live,
                    ),
                    45,
                )
                hook_body = "\n".join(
                    ln for ln in hook_out.strip().splitlines()
                    if not _is_hidden_line(ln)
                )
                # HOST-SIDE FALLBACK: if the container-side hook returned empty
                # (non-Python containers lack pydantic → import error → no evidence),
                # run post_edit HOST-SIDE using the host graph.db copy. The host has
                # the full GT package installed. This makes L3 post-edit work on ALL
                # 5 languages, not just Python. One product, one surface.
                _fallback_db = getattr(config, "_host_graph_db", "") or os.environ.get("GT_PREBUILT_GRAPH_DB", "")
                _l3_has_evidence = any(m in hook_body for m in ("[SIGNATURE]", "[BEHAVIORAL CONTRACT]", "[CALLERS]", "PRESERVE:", "[TEST]", "[TWIN]", "[SIMILAR]", "[OVERRIDE]", "Calls into:", "[COMPLETENESS]", "[CATCHES]", "[RAISES]"))
                if (not hook_body.strip() or not _l3_has_evidence) and _fallback_db and os.path.exists(_fallback_db):
                    try:
                        import io as _pe_io
                        _pe_file = (rel_p or event.path).replace("\\", "/")
                        _pe_host_root = "/tmp/testbed_src"
                        _pe_argv = [
                            "post_edit",
                            "--db", _fallback_db,
                            "--root", _pe_host_root,
                            "--file", _pe_file,
                            "--mode", "post_edit",
                            "--iteration-ratio", str(_l3_ratio_live),
                        ]
                        _pe_old_argv = sys.argv
                        _pe_old_stdout = sys.stdout
                        _pe_capture = _pe_io.StringIO()
                        sys.argv = _pe_argv
                        sys.stdout = _pe_capture
                        try:
                            from groundtruth.hooks.post_edit import main as _pe_main
                            _pe_main()
                        finally:
                            sys.argv = _pe_old_argv
                            sys.stdout = _pe_old_stdout
                        _pe_raw = _pe_capture.getvalue()
                        hook_body = "\n".join(
                            ln for ln in _pe_raw.strip().splitlines()
                            if not _is_hidden_line(ln)
                        )
                        if hook_body.strip():
                            print(
                                f"[GT_META] L3 host-side fallback OK for {_pe_file} "
                                f"({len(hook_body)} chars)",
                                flush=True,
                            )
                    except Exception as _pe_exc:
                        print(
                            f"[GT_META] L3 host-side fallback failed: {_pe_exc}",
                            flush=True,
                        )
                has_evidence = has_gt_evidence(hook_body, "l3")
                _matched = [t for t in L3_MARKERS if t in hook_body]
                _gdb_exists = bool(config.graph_db)  # graph.db exists in container; host copy may or may not be available
                _turns_left = max(0, config.max_iter - config.action_count)
                if _matched:
                    print(
                        f"[GT_TRACE] mech=L3_post_edit layer=L3 event=post_edit step={config.action_count} "
                        f"graph_db={_gdb_exists} evidence={len(_matched)} action=emit "
                        f"visible=True surface=append_observation tokens={len(hook_body)//4} "
                        f"turns_left={_turns_left} markers={_matched} file={rel_p}",
                        flush=True,
                    )
                elif hook_body:
                    print(
                        f"[GT_TRACE] mech=L3_post_edit layer=L3 event=post_edit step={config.action_count} "
                        f"graph_db={_gdb_exists} evidence=0 action=suppress "
                        f"reason=GATE_MISMATCH visible=False surface=none "
                        f"turns_left={_turns_left} body_len={len(hook_body)} first_80={hook_body[:80]!r} file={rel_p}",
                        flush=True,
                    )
                else:
                    print(
                        f"[GT_TRACE] mech=L3_post_edit layer=L3 event=post_edit step={config.action_count} "
                        f"graph_db={_gdb_exists} evidence=0 action=suppress "
                        f"reason=NO_EVIDENCE visible=False surface=none "
                        f"turns_left={_turns_left} file={rel_p}",
                        flush=True,
                    )
                # [9] Semantic check + [3] Behavioral contract — ALWAYS run on post-edit
                # These analyze the FUNCTION BODY (not graph edges). Independent of
                # whether the hook found callers/signatures.
                _sem_file = rel_p or event.path
                if "/" in _sem_file and _sem_file.count("/") >= 2:
                    _parts = _sem_file.split("/", 1)
                    if "__" in _parts[0]:
                        _sem_file = _parts[1]
                _sem_cmd = (
                    _env_prefix(config)
                    + f"python3 -m groundtruth.hooks.semantic_check "
                    f"--file={_sem_file} --workspace={config.workspace_root}"
                )
                try:
                    _sem_out = _run_internal(orig_run_action, _sem_cmd, 8).strip()
                    if _sem_out:
                        _sem_lines = []
                        for _sl in _sem_out.splitlines():
                            if _sl.startswith("GUARD_ADDED:"):
                                _sem_lines.append(f"SEMANTIC WARNING: New guard: {_core_clip_balanced(_sl[12:])}")
                            elif _sl.startswith("GUARD_REMOVED:"):
                                _sem_lines.append(f"SEMANTIC WARNING: Guard removed: {_core_clip_balanced(_sl[14:])}")
                            elif _sl.startswith("RETURN_PATH:"):
                                _sem_lines.append(f"  {_sl[12:]}")
                        if _sem_lines:
                            _sem_block = "\n".join(_sem_lines)
                            hook_body = _sem_block + "\n" + hook_body
                            has_evidence = True
                            print(
                                f"[GT_TRACE] mech=semantic_check layer=L3 event=post_edit step={config.action_count} "
                                f"graph_db=False evidence={len(_sem_lines)} action=emit "
                                f"visible=True surface=append_observation tokens={len(_sem_block)//4} "
                                f"turns_left={_turns_left} file={rel_p}",
                                flush=True,
                            )
                        else:
                            print(
                                f"[GT_TRACE] mech=semantic_check layer=L3 event=post_edit step={config.action_count} "
                                f"graph_db=False evidence=0 action=suppress reason=GATE_MISMATCH "
                                f"visible=False surface=none turns_left={_turns_left} "
                                f"raw_out={_sem_out[:200]!r} file={rel_p}",
                                flush=True,
                            )
                    else:
                        print(
                            f"[GT_TRACE] mech=semantic_check layer=L3 event=post_edit step={config.action_count} "
                            f"graph_db=False evidence=0 action=suppress reason=NO_EVIDENCE "
                            f"visible=False surface=none turns_left={_turns_left} file={rel_p}",
                            flush=True,
                        )
                except Exception as _sem_exc:
                    print(
                        f"[GT_TRACE] mech=semantic_check layer=L3 event=post_edit step={config.action_count} "
                        f"graph_db=False evidence=0 action=suppress reason=SNIPPET_ERROR "
                        f"visible=False surface=none turns_left={_turns_left} "
                        f"error={type(_sem_exc).__name__}:{_sem_exc} file={rel_p}",
                        flush=True,
                    )
                # Obligation check (router live path)
                # Fix: run git diff inside container to get function-aware hunk
                # headers. OpenHands obs.extras.diff is often empty; difflib
                # diffs lack function context. git diff includes "def func()"
                # in hunk headers by default.
                if _sem_file.endswith(".py"):
                    try:
                        _oblig_edited_fns: set[str] = set()
                        # Primary: git diff in container (has function context)
                        _git_diff_cmd = f"cd {config.workspace_root} && git diff -U0 HEAD -- {_sem_file} 2>/dev/null"
                        _git_diff_out = _run_internal(orig_run_action, _git_diff_cmd, 10)
                        if _git_diff_out:
                            for _dl in _git_diff_out.splitlines():
                                _hm = re.match(r"^@@.*@@\s+(?:async\s+)?def\s+(\w+)", _dl)
                                if _hm:
                                    _oblig_edited_fns.add(_hm.group(1))
                                if _dl.startswith(("+", "-")) and not _dl.startswith(("+++", "---")):
                                    _dm = re.search(r"(?:async\s+)?def\s+(\w+)", _dl)
                                    if _dm:
                                        _oblig_edited_fns.add(_dm.group(1))
                        # Fallback: observation diff (if git diff failed)
                        if not _oblig_edited_fns and diff_text_live:
                            for _dl in diff_text_live.splitlines():
                                _hm = re.match(r"^@@.*@@\s+(?:async\s+)?def\s+(\w+)", _dl)
                                if _hm:
                                    _oblig_edited_fns.add(_hm.group(1))
                                if _dl.startswith(("+", "-")) and not _dl.startswith(("+++", "---")):
                                    _dm = re.search(r"(?:async\s+)?def\s+(\w+)", _dl)
                                    if _dm:
                                        _oblig_edited_fns.add(_dm.group(1))
                        # PRIOR-004 fix: when diff hunk headers show class not function,
                        # extract changed line numbers and query graph.db for enclosing function
                        if not _oblig_edited_fns and diff_text_live:
                            _changed_lines: list[int] = []
                            for _dl in diff_text_live.splitlines():
                                _lm = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", _dl)
                                if _lm:
                                    _start = int(_lm.group(1))
                                    _count = int(_lm.group(2) or "1")
                                    _changed_lines.extend(range(_start, _start + _count))
                            if _changed_lines:
                                _oblig_db = getattr(config, "_host_graph_db", "") or ""
                                if _oblig_db and os.path.exists(_oblig_db):
                                    try:
                                        _oblig_conn = sqlite3.connect(_oblig_db)
                                        _sem_norm = (_sem_file or "").replace("\\", "/").lstrip("/")
                                        for _cl in sorted(set(_changed_lines))[:5]:
                                            _fn_row = _oblig_conn.execute(
                                                "SELECT name FROM nodes WHERE file_path LIKE ? ESCAPE '\\' "
                                                "AND label IN ('Function','Method') AND start_line <= ? AND end_line >= ? "
                                                "ORDER BY start_line DESC LIMIT 1",
                                                (f"%{_escape_like(_sem_norm)}", _cl, _cl),
                                            ).fetchone()
                                            if _fn_row:
                                                _oblig_edited_fns.add(_fn_row[0])
                                        _oblig_conn.close()
                                    except Exception:
                                        pass
                        # PRIOR-004 fix: if we couldn't identify edited functions,
                        # suppress completeness entirely rather than class-wide all-pairs noise
                        if not _oblig_edited_fns:
                            print(f"[GT_META] obligation_check SUPPRESSED_NO_EDITED_FUNCTION for {_sem_file}", flush=True)
                            _oblig_out_r = ""
                        else:
                            _oblig_ef_arg = f" --edited-functions={','.join(sorted(_oblig_edited_fns))}"
                            _oblig_cmd_r = (
                                _env_prefix(config)
                                + "python3 -m groundtruth.hooks.obligation_check "
                                + f"--file={_sem_file} --workspace={config.workspace_root}"
                                + _oblig_ef_arg
                            )
                            _oblig_out_r = _run_internal(orig_run_action, _oblig_cmd_r, 5).strip()
                        if _oblig_out_r:
                            for _ol_r in _oblig_out_r.splitlines():
                                if _ol_r.startswith("OBLIGATION:"):
                                    hook_body = f"[COMPLETENESS] {_ol_r[12:]}\n" + hook_body
                                    has_evidence = True
                    except Exception as _oblig_exc_r:
                        print(f"[GT_META] obligation_check_error(router): {_oblig_exc_r}", flush=True)

                if has_evidence:
                    # Track last GT action for rescue governor stuck detection
                    config._last_gt_action = config.action_count
                    # Strip __GT_STRUCTURED__ JSON from agent-visible text (telemetry only)
                    if "__GT_STRUCTURED__" in hook_body:
                        hook_body = hook_body.split("__GT_STRUCTURED__")[0].strip()
                    if len(hook_body) > 2000:
                        # B1: safe-boundary truncation — never cut a marker/word
                        # mid-token and never glue the omission note onto the
                        # cut (the verified "[CATCHE..." corruption).
                        hook_body = _safe_truncate_evidence(hook_body, 2000)
                    # Recall injection — relevance-gated (TASK #47/#55).
                    # A cached per-file evidence dump can be stale and unrelated
                    # to the function the agent just edited (observed: a [RECALL]
                    # of progress_write while set_fields was edited). Anchor on
                    # the edited function's identifier tokens (derived locally
                    # from the live observation diff, so no NameError) and emit
                    # only when the cached text overlaps that anchor. When no
                    # anchor is available we keep prior behavior (do not
                    # over-suppress legitimate recall). See _recall_should_emit.
                    _recall_key = rel_p or event.path
                    _cached_evidence = config.evidence_cache.get(_recall_key, "")
                    _recall_prefix = ""
                    if _cached_evidence:
                        _recall_edited_fns = _recall_edited_fn_names(diff_text_live)
                        # Reuse the richer edited-fn set already resolved above for
                        # this edit (git -U0 hunk headers + graph.db line->enclosing-
                        # function fallback) so in-body edits, where bare `def <name>`
                        # never appears on +/- lines, still yield a recall anchor.
                        # Guarded: that set only exists on the .py obligation path.
                        _recall_edited_fns |= locals().get("_oblig_edited_fns", set()) or set()
                        if _recall_should_emit(_cached_evidence, _recall_edited_fns, None):
                            _recall_prefix = f"[RECALL] from earlier: {_cached_evidence}\n"
                        else:
                            print(
                                f"[GT_TRACE] mech=recall_gate layer=L3 event=post_edit "
                                f"step={config.action_count} action=suppress reason=RECALL_OFF_ANCHOR "
                                f"edited_fns={sorted(_recall_edited_fns)} file={rel_p or event.path}",
                                flush=True,
                            )
                    hook_body = _recall_prefix + hook_body
                    _edit_basename = os.path.basename(rel_p or event.path)
                    _formatted_pe = f"[GT] Post-edit: {_edit_basename}\n{hook_body}\n"
                    print(
                        f"[GT_DELIVERY] L3 LIVE post_edit: evidence_len={len(hook_body)} "
                        f"file={rel_p or event.path}",
                        flush=True,
                    )
                    # [10] Multi-file scope check — fires ON EDIT, not at submit
                    # If this file has callers in OTHER files agent hasn't edited, warn NOW
                    _scope_warning = ""
                    try:
                        _edited_set = config.edited_files
                        _edit_file = rel_p or event.path
                        _ef_py = _edit_file.replace("\\", "\\\\").replace("'", "\\'")
                        _mf_cmd = (
                            _env_prefix(config)
                            + f"python3 -c \""
                            f"import sqlite3; f='{_ef_py}'; c=sqlite3.connect('{config.graph_db}'); "
                            f"rows=c.execute("
                            f"'SELECT DISTINCT nsrc.file_path, COUNT(*) as cnt "
                            f"FROM nodes nt JOIN edges e ON e.target_id=nt.id AND e.type=\\'CALLS\\' "
                            f"AND COALESCE(e.confidence,0.5)>=0.7 "
                            f"JOIN nodes nsrc ON e.source_id=nsrc.id "
                            f"WHERE nt.file_path=? AND nsrc.file_path!=? "
                            f"AND nsrc.is_test=0 GROUP BY nsrc.file_path HAVING cnt>=1 "
                            f"ORDER BY cnt DESC LIMIT 3',(f,f)).fetchall(); "
                            f"[print(f'{{r[0]}} ({{r[1]}}x)') for r in rows]\""
                        )
                        _mf_out = _run_internal(orig_run_action, _mf_cmd, 5).strip()
                        if _mf_out:
                            _unedited = [ln for ln in _mf_out.splitlines() if ln.strip() and not any(ef in ln for ef in _edited_set)]
                            if _unedited:
                                _scope_warning = f"\nSCOPE: callers in unedited files: {'; '.join(_unedited[:2])}\n"
                    except Exception:
                        pass
                    if _scope_warning:
                        _formatted_pe = _formatted_pe.rstrip() + _scope_warning
                    # Layer C: Scope-aware progress tracking
                    _edit_norm = _normalize_rel_path(rel_p or event.path, config)
                    if config._consensus_scope:
                        _matched_scope = [
                            sf for sf in config._consensus_scope
                            if _same_repo_file(_edit_norm, sf, config)
                        ]
                        if _matched_scope:
                            config._consensus_scope_edited.add(_matched_scope[0])
                        _remaining = [
                            sf for sf in config._consensus_scope
                            if sf not in config._consensus_scope_edited
                        ]
                        _total = len(config._consensus_scope)
                        _done = _total - len(_remaining)
                        if _remaining and _done > 0:
                            # Bug 7 fix: show parent/basename to disambiguate __init__.py
                            def _short_path(r):
                                _r = r.replace("\\", "/")
                                return "/".join(_r.split("/")[-2:]) if "/" in _r else _r
                            # Only nudge "remaining" for VERIFIED co-change files (a >=0.7
                            # graph edge to an already-edited file). Listing name_match /
                            # lexical scope members as "Remaining" is an over-edit nudge that
                            # pushes the agent to edit files that are NOT the fix — GT curates,
                            # it does not expand exploration (correct-or-quiet; live beets-5495
                            # defect: autotag/* name_match files were nudged as remaining).
                            _conn_rem: list = []
                            if config._host_graph_db and os.path.exists(config._host_graph_db) and config._consensus_scope_edited:
                                try:
                                    import sqlite3 as _sq_sf
                                    _sfc = _sq_sf.connect(config._host_graph_db)
                                    _ed_pats = [f"%{_escape_like(ef.replace(chr(92), '/').lstrip('/'))}" for ef in config._consensus_scope_edited]
                                    for _rf in _remaining:
                                        _rf_pat = f"%{_escape_like(_rf.replace(chr(92), '/').lstrip('/'))}"
                                        for _ep in _ed_pats:
                                            if _sfc.execute(
                                                "SELECT COUNT(*) FROM edges e "
                                                "JOIN nodes n1 ON e.source_id = n1.id "
                                                "JOIN nodes n2 ON e.target_id = n2.id "
                                                "WHERE COALESCE(e.confidence, 0.5) >= 0.7 "
                                                "AND ((n1.file_path LIKE ? ESCAPE '\\' AND n2.file_path LIKE ? ESCAPE '\\') "
                                                " OR (n1.file_path LIKE ? ESCAPE '\\' AND n2.file_path LIKE ? ESCAPE '\\'))",
                                                (_ep, _rf_pat, _rf_pat, _ep),
                                            ).fetchone()[0] > 0:
                                                _conn_rem.append(_rf)
                                                break
                                    _sfc.close()
                                except Exception:
                                    pass
                            if _conn_rem:
                                _rem_names = ", ".join(_short_path(r) for r in _conn_rem[:3])
                                _formatted_pe = _formatted_pe.rstrip() + f"\n[GT] {_done}/{_total} scope files edited. {len(_conn_rem)} verified co-change file(s) remain: {_rem_names}\n"
                            # else: no verified co-change -> no nudge (do not push over-edit)
                        elif not _remaining and _done == _total and _total > 1:
                            _formatted_pe = _formatted_pe.rstrip() + f"\n[GT] All {_total} scope files covered. Verify your changes.\n"
                    _persist_router_v2_event(config, {
                        **(_v2_event_pe or {}),
                        "evidence_source": "in_container_hook",
                        "evidence_text": hook_body[:500],
                        "next_action_type": "gt_check",
                        "next_action_file": rel_p or event.path,
                    })
                    # L6 early review removed (correct-or-quiet): the "PRESERVE: ...
                    # callers depend on it" output was a caller-EDIT prescription
                    # (SWE-PRM NeurIPS 2025: action-prescriptive feedback lowers
                    # resolution). The verifiable test-coverage suggestions it also
                    # built are already delivered by _maybe_fire_presubmit_verify()
                    # at the edit→review transition (same assertions-table query,
                    # target_node_id > 0). No double-delivery, no prescription.
                    # Cross-turn content dedup (live beets-5495: identical post-edit
                    # evidence delivered twice after two edits of the same file). The
                    # legacy L3 path has this guard (l3:file:hash); the router path did
                    # not — add it here so an unchanged body is not re-emitted.
                    import hashlib as _hl_pe
                    _pe_key = (
                        f"l3:{rel_p or event.path}:"
                        f"{_hl_pe.md5(hook_body.strip().encode('utf-8', 'replace')).hexdigest()[:12]}"
                    )
                    if _pe_key in config.evidence_sent:
                        return obs
                    config.evidence_sent[_pe_key] = True
                    return append_observation(obs, f"\n\n{_formatted_pe}")
                return obs
            # Budget caps removed — dedup is the sole gate.
            # Fire count still tracked for telemetry.
            diff_text, old_content_text = _extract_diff_and_old_content(obs)
            diff_path = ""
            old_content_path = ""
            if diff_text:
                diff_path = "/tmp/gt_diff.txt"
                _write_text_to_container(orig_run_action, diff_text, diff_path)
            if old_content_text:
                old_content_path = "/tmp/gt_old.txt"
                _write_text_to_container(orig_run_action, old_content_text, old_content_path)
            # Compute L3 mode from L5 governor state (Change 5a)
            _l3_mode = "post_edit"
            _l3_ratio = config.action_count / max(config.max_iter, 1)
            if os.environ.get("GT_REBUILD_L3", "0") == "1":
                _l5_gov = getattr(config, "_l5_governor", None)
                if _l5_gov is not None and _l5_gov.state.has_unresolved_failure():
                    _l3_mode = "post_failure"

            hook_out = _run_internal(
                orig_run_action,
                make_edit_hook_command_with_artifacts(
                    event,
                    config,
                    diff_path=diff_path or None,
                    old_content_path=old_content_path or None,
                    mode=_l3_mode,
                    iteration_ratio=_l3_ratio,
                ),
                45,
            )
            if _hook_fatal(hook_out):
                print(f"GT HOOK ERROR (post_edit:{event.path}): {hook_out[:400]}", flush=True)

            # L5 metrics: track post-advisory source edits
            if config._l5_scaffold_fired and _is_real_source_edit(event.path, config):
                config._l5_metrics["source_edit_after_l5"] = True
                if rel_p and rel_p in config.brief_candidates:
                    config._l5_metrics["touched_brief_candidate_after_l5"] = True

            # L3 fully decoupled from L1 (Decision 22 Fix 5) — no candidate labeling
            framing = ""

            if rel_p and hook_out and not _hook_fatal(hook_out):
                low = hook_out.lower()
                first_line = ""
                for line in hook_out.splitlines():
                    if line.strip().startswith("[GT_"):
                        first_line = line.strip()
                        break
                needs_check = (
                    "[GT_CONTRACT]" in hook_out
                    or "[GT_CALLER]" in hook_out
                    or "likely_invalid" in low
                )
                if needs_check:
                    config.pending_checks.add(rel_p)
                    if first_line:
                        config.pending_summaries.append((rel_p, first_line))

            if tel_obj is not None:
                fatal = _hook_fatal(hook_out)
                empty_ev = any(
                    marker in hook_out
                    for marker in (
                        "[GT_STATUS] empty",
                        "[GT_STATUS] no_evidence:",
                        "[GT_STATUS] skipped:",
                    )
                )
                ok_ev = has_gt_evidence(hook_out, "l3")
                tel_obj.record_hook("L3", ok_ev and not fatal, empty=empty_ev or (not hook_out.strip()))
                _write_gt_telemetry(instance_ref, tel_obj)

            # F2: re-emit container GT_META lines to host stdout for GHA visibility
            for _meta_ln in hook_out.strip().splitlines():
                if _meta_ln.strip().startswith("[GT_META]"):
                    print(_meta_ln.strip(), flush=True)
            hook_body_edit = "\n".join(
                ln for ln in hook_out.strip().splitlines()
                if not _is_hidden_line(ln)
            )
            # Semantic check: runs on every post-edit regardless of router mode
            _sem_file_leg = rel_p or event.path
            if "/" in _sem_file_leg and _sem_file_leg.count("/") >= 2:
                _parts_leg = _sem_file_leg.split("/", 1)
                if "__" in _parts_leg[0]:
                    _sem_file_leg = _parts_leg[1]
            try:
                _sem_cmd_leg = (
                    _env_prefix(config)
                    + f"python3 -m groundtruth.hooks.semantic_check "
                    f"--file={_sem_file_leg} --workspace={config.workspace_root}"
                )
                _sem_out_leg = _run_internal(orig_run_action, _sem_cmd_leg, 8).strip()
                if _sem_out_leg:
                    for _sl in _sem_out_leg.splitlines():
                        if _sl.startswith("GUARD_ADDED:") or _sl.startswith("GUARD_REMOVED:"):
                            hook_body_edit = _sl + "\n" + hook_body_edit
            except Exception as _sem_exc:
                print(f"[GT_META] semantic_check_error: {type(_sem_exc).__name__}: {_sem_exc}", flush=True)

            # Obligation check (check_v2 AST logic): find methods sharing
            # self.attrs with the edited function that weren't also edited.
            # CLAUDE.md item 2+4: "Consistency + Completeness — must fire on
            # EVERY edit regardless of graph quality."
            if _sem_file_leg.endswith(".py"):
                try:
                    # Fix: run git diff in container for function-aware hunk headers
                    _oblig_edited_fns_leg: set[str] = set()
                    _git_diff_cmd_leg = f"cd {config.workspace_root} && git diff -U0 HEAD -- {_sem_file_leg} 2>/dev/null"
                    _git_diff_out_leg = _run_internal(orig_run_action, _git_diff_cmd_leg, 10)
                    if _git_diff_out_leg:
                        for _dl in _git_diff_out_leg.splitlines():
                            _hm = re.match(r"^@@.*@@\s+(?:async\s+)?def\s+(\w+)", _dl)
                            if _hm:
                                _oblig_edited_fns_leg.add(_hm.group(1))
                            if _dl.startswith(("+", "-")) and not _dl.startswith(("+++", "---")):
                                _dm = re.search(r"(?:async\s+)?def\s+(\w+)", _dl)
                                if _dm:
                                    _oblig_edited_fns_leg.add(_dm.group(1))
                    # Fallback: observation diff
                    if not _oblig_edited_fns_leg and diff_text:
                        for _dl in diff_text.splitlines():
                            _hm = re.match(r"^@@.*@@\s+(?:async\s+)?def\s+(\w+)", _dl)
                            if _hm:
                                _oblig_edited_fns_leg.add(_hm.group(1))
                            if _dl.startswith(("+", "-")) and not _dl.startswith(("+++", "---")):
                                _dm = re.search(r"(?:async\s+)?def\s+(\w+)", _dl)
                                if _dm:
                                    _oblig_edited_fns_leg.add(_dm.group(1))
                    # PRIOR-004 fix: graph.db fallback for function name extraction
                    if not _oblig_edited_fns_leg and diff_text:
                        _changed_lines_leg: list[int] = []
                        for _dl in diff_text.splitlines():
                            _lm = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", _dl)
                            if _lm:
                                _start = int(_lm.group(1))
                                _count = int(_lm.group(2) or "1")
                                _changed_lines_leg.extend(range(_start, _start + _count))
                        if _changed_lines_leg:
                            _oblig_db_leg = getattr(config, "_host_graph_db", "") or ""
                            if _oblig_db_leg and os.path.exists(_oblig_db_leg):
                                try:
                                    _oconn = sqlite3.connect(_oblig_db_leg)
                                    _sem_norm_leg = (_sem_file_leg or "").replace("\\", "/").lstrip("/")
                                    for _cl in sorted(set(_changed_lines_leg))[:5]:
                                        _fn_row = _oconn.execute(
                                            "SELECT name FROM nodes WHERE file_path LIKE ? ESCAPE '\\' "
                                            "AND label IN ('Function','Method') AND start_line <= ? AND end_line >= ? "
                                            "ORDER BY start_line DESC LIMIT 1",
                                            (f"%{_escape_like(_sem_norm_leg)}", _cl, _cl),
                                        ).fetchone()
                                        if _fn_row:
                                            _oblig_edited_fns_leg.add(_fn_row[0])
                                    _oconn.close()
                                except Exception:
                                    pass
                    # PRIOR-004 fix: suppress when edited functions unknown
                    if not _oblig_edited_fns_leg:
                        print(f"[GT_META] obligation_check SUPPRESSED_NO_EDITED_FUNCTION for {_sem_file_leg}", flush=True)
                        _oblig_out = ""
                    else:
                        _oblig_ef_arg_leg = f" --edited-functions={','.join(sorted(_oblig_edited_fns_leg))}"
                        _oblig_cmd = (
                            _env_prefix(config)
                            + "python3 -m groundtruth.hooks.obligation_check "
                            + f"--file={_sem_file_leg} --workspace={config.workspace_root}"
                            + _oblig_ef_arg_leg
                        )
                        _oblig_out = _run_internal(orig_run_action, _oblig_cmd, 5).strip()
                    if _oblig_out:
                        for _ol in _oblig_out.splitlines():
                            if _ol.startswith("OBLIGATION:"):
                                hook_body_edit = f"[COMPLETENESS] {_ol[12:]}\n" + hook_body_edit
                except Exception as _oblig_exc:
                    print(f"[GT_META] obligation_check_error: {type(_oblig_exc).__name__}: {_oblig_exc}", flush=True)

            if not has_gt_evidence(hook_body_edit, "l3"):
                _l3_ok_eid = _emit_structured_event(config, "L3", "post_edit_suppressed", emitted=False, suppressed=True, suppression_reason="no_evidence", file_path=rel_p or event.path)
                _log_gt_interaction(config, "L3", f"post_edit:{rel_p or event.path}", "GT_OK", "[GT_OK] No concerns.", agent_action_before=act_text[:300], event_id=_l3_ok_eid or "")
                return obs

            _dedup_body = hook_body_edit.split("__GT_STRUCTURED__")[0].strip() if "__GT_STRUCTURED__" in hook_body_edit else hook_body_edit
            # Bug 6 fix: strip [RECALL] prefix before hashing — RECALL
            # content changes across invocations, defeating dedup.
            if _dedup_body.startswith("[RECALL]"):
                _recall_end = _dedup_body.find("\n")
                if _recall_end > 0:
                    _dedup_body = _dedup_body[_recall_end + 1:]
            _dedup_hash_edit = hashlib.md5(_dedup_body.strip().encode("utf-8", errors="replace")).hexdigest()
            _dedup_sorted_hash_edit = hashlib.md5("\n".join(sorted(_dedup_body.strip().splitlines())).encode("utf-8", errors="replace")).hexdigest()
            _dedup_key_edit = f"l3:{rel_p or event.path}:{_dedup_hash_edit}"
            _dedup_sorted_key_edit = f"l3s:{rel_p or event.path}:{_dedup_sorted_hash_edit}"
            if _dedup_key_edit in config.evidence_sent or _dedup_sorted_key_edit in config.evidence_sent:
                _l3_dedup_reason = "exact_match" if _dedup_key_edit in config.evidence_sent else "order_variant"
                print(f"[GT_META] dedup_suppressed: layer=l3 file={rel_p or event.path} reason={_l3_dedup_reason}", flush=True)
                _l3_dd_eid = _emit_structured_event(config, "L3", "post_edit_dedup", emitted=False, suppressed=True, suppression_reason="duplicate", file_path=rel_p or event.path)
                _log_gt_interaction(config, "L3", f"post_edit:{rel_p or event.path}", "dedup", "[dedup]", agent_action_before=act_text[:300], event_id=_l3_dd_eid or "")
                return obs
            else:
                config.evidence_sent[_dedup_key_edit] = True
                config.evidence_sent[_dedup_sorted_key_edit] = True
                # Finding 7: per-file-per-layer evolution safety valve
                _l3_file_prefix = f"l3:{rel_p or event.path}:"
                _l3_file_fire_count = sum(1 for k in config.evidence_sent if k.startswith(_l3_file_prefix))
                if _l3_file_fire_count > 5:  # >5 unique evidence injections for same file+layer
                    _l3_evol_eid = _emit_structured_event(config, "L3", "post_edit_evolution_cap", emitted=False, suppressed=True, suppression_reason="evidence_evolving_rapidly", file_path=rel_p or event.path)
                    _log_gt_interaction(config, "L3", f"post_edit:{rel_p or event.path}", "evolution_cap", "[evolution_cap] evidence evolving rapidly — showing latest only", agent_action_before=act_text[:300], event_id=_l3_evol_eid or "")
                    # Remove all previous L3 entries for this file, keep only the latest
                    _stale_l3_keys = [k for k in config.evidence_sent if k.startswith(_l3_file_prefix) and k != _dedup_key_edit]
                    for _sk in _stale_l3_keys:
                        del config.evidence_sent[_sk]
                    # Also clean sorted keys
                    _l3s_file_prefix = f"l3s:{rel_p or event.path}:"
                    _stale_l3s_keys = [k for k in config.evidence_sent if k.startswith(_l3s_file_prefix) and k != _dedup_sorted_key_edit]
                    for _sk in _stale_l3s_keys:
                        del config.evidence_sent[_sk]
                print(f"[GT_META] L3 post_edit evidence for {rel_p or event.path} ({len(hook_body_edit)} chars)", flush=True)
                # Structural next_action hierarchy (Decision 32)
                _l3_next_action_type = ""
                _l3_next_action_file = ""
                _l3_next_action_test = ""
                if os.environ.get("GT_STRUCTURAL_NEXT_ACTION", "0") == "1" and hook_out and "__GT_STRUCTURED__" in hook_out:
                    try:
                        _struct_part = hook_out.split("__GT_STRUCTURED__", 1)[1].strip().splitlines()[0]
                        _struct_items = json.loads(_struct_part)
                        # Priority 1: caller code (with LSP verification if available)
                        _l3_verifier = getattr(config, "_edge_verifier", None)
                        _l3_caller_candidates = [si for si in _struct_items if si.get("kind") == "l3_caller_code" and si.get("file_path")]
                        if _l3_verifier and os.environ.get("GT_LSP_VERIFY", "1") == "1" and _l3_caller_candidates:
                            from groundtruth.lsp.edge_verifier import verify_edge_sync
                            for _cc in _l3_caller_candidates[:3]:
                                _cd = _get_edge_detail_in_container(orig_run_action, config.graph_db, rel_p or event.path, _cc["file_path"])
                                if _cd:
                                    _ve = verify_edge_sync(
                                        config.workspace_root,
                                        target_file=rel_p or event.path,
                                        target_symbol=_cd[0], target_line=_cd[1],
                                        caller_file=_cc["file_path"],
                                        original_confidence=_cd[2],
                                    )
                                    if _ve.verified:
                                        _l3_next_action_type = "READ_CALLER_CONTRACT"
                                        _l3_next_action_file = _cc["file_path"]
                                        print(f"[GT_META] L3 caller VERIFIED: {_cc['file_path']} ({_ve.latency_ms}ms)", flush=True)
                                        break
                                    else:
                                        print(f"[GT_META] L3 caller REJECTED: {_cc['file_path']}", flush=True)
                        else:
                            for _si in _l3_caller_candidates:
                                _l3_next_action_type = "READ_CALLER_CONTRACT"
                                _l3_next_action_file = _si["file_path"]
                                break
                        # Priority 2: consumer/importer
                        if not _l3_next_action_type:
                            for _si in _struct_items:
                                if _si.get("kind") in ("l3b_importer_edge", "l3b_callee_edge") and _si.get("file_path"):
                                    _l3_next_action_type = "READ_CONSUMER"
                                    _l3_next_action_file = _si["file_path"]
                                    break
                        # Priority 3: signature — only if we have a file to check
                        if not _l3_next_action_type:
                            for _si in _struct_items:
                                if _si.get("kind") == "l3_signature" and _si.get("file_path"):
                                    _l3_next_action_type = "CHECK_SIGNATURE"
                                    _l3_next_action_file = _si["file_path"]
                                    break
                        # Priority 4: targeted test (no structural witness)
                        if not _l3_next_action_type:
                            for _si in _struct_items:
                                if _si.get("kind") == "l3_targeted_verification":
                                    _l3_next_action_type = "RUN_TARGETED_TEST"
                                    _l3_next_action_test = _si.get("text", "")
                                    break
                        # Priority 5/6: static sanity or none
                        if not _l3_next_action_type:
                            _l3_next_action_type = "NONE_UNVERIFIABLE"
                    except Exception:
                        pass
                _l3_eid = _emit_structured_event(
                    config, "L3", "post_edit_contract",
                    rendered_text=framing + hook_body_edit,
                    file_path=rel_p or event.path,
                    hook_output=hook_out or "",
                    next_action_type=_l3_next_action_type or None,
                    next_action_file=_l3_next_action_file or None,
                    next_action_test=_l3_next_action_test or None,
                )
                _log_gt_interaction(
                    config, "L3", f"post_edit:{rel_p or event.path}", "evidence",
                    framing + hook_body_edit, agent_action_before=act_text[:300],
                    event_id=_l3_eid or "",
                    next_action_type=_l3_next_action_type,
                    next_action_file=_l3_next_action_file,
                    next_action_test=_l3_next_action_test,
                )
                _register_pending_next_action(config, _l3_eid or "", _l3_next_action_type, _l3_next_action_file)
                _feed_gt_next_action_to_l5(config, _l3_next_action_type, _l3_next_action_file)
                # Strands-style: agent sees compact evidence, full detail → JSONL only
                agent_edit_body = hook_body_edit.split("__GT_STRUCTURED__")[0].strip() if "__GT_STRUCTURED__" in hook_body_edit else hook_body_edit
                # Extract content lines (skip XML wrappers, status lines, underscores)
                directive_lines = [
                    ln.strip() for ln in agent_edit_body.splitlines()
                    if ln.strip()
                    and not ln.strip().startswith("[GT_STATUS]")
                    and not _is_hidden_line(ln)
                    and not ln.strip().startswith("__")
                    and not ln.strip().startswith("<")
                    and not ln.strip().startswith("</")
                ]
                # B1: safe-boundary truncation (never mid-marker/mid-word, omission
                # note on its own line) instead of a raw [:2000] byte slice.
                evidence_text = _safe_truncate_evidence("\n".join(directive_lines), 2000)
                _edit_base = os.path.basename(rel_p or event.path)
                if evidence_text:
                    evidence = f'\n\n<gt-post-edit file="{_edit_base}">\n{evidence_text}\n</gt-post-edit>\n'
                else:
                    evidence = ""
                print(f"[GT_DELIVERY] L3 post_edit: agent_edit_body_lines={len(agent_edit_body.splitlines())} directive_lines={len(directive_lines)} evidence_len={len(evidence)} file={rel_p} fire={config._l3_fire_count+1}", flush=True)
                if not evidence.strip():
                    print(f"[GT_DELIVERY] L3 EMPTY EVIDENCE! agent_edit_body first 200: {agent_edit_body[:200]!r}", flush=True)
                if evidence.strip():
                    config._l3_fire_count += 1
                    # Track last GT action for rescue governor stuck detection
                    config._last_gt_action = config.action_count
                    # Layer C (legacy path): Scope-aware progress tracking
                    _edit_norm_leg = _normalize_rel_path(rel_p or event.path, config)
                    if config._consensus_scope:
                        _matched_leg = [
                            sf for sf in config._consensus_scope
                            if _same_repo_file(_edit_norm_leg, sf, config)
                        ]
                        if _matched_leg:
                            config._consensus_scope_edited.add(_matched_leg[0])
                        _rem_leg = [
                            sf for sf in config._consensus_scope
                            if sf not in config._consensus_scope_edited
                        ]
                        _total_leg = len(config._consensus_scope)
                        _done_leg = _total_leg - len(_rem_leg)
                        if _rem_leg and _done_leg > 0:
                            # Bug 7 fix: show parent/basename to disambiguate __init__.py
                            def _short_path_leg(r):
                                _r = r.replace("\\", "/")
                                return "/".join(_r.split("/")[-2:]) if "/" in _r else _r
                            _rnames = ", ".join(_short_path_leg(r) for r in _rem_leg[:3])
                            _conn_rem_leg: list = []
                            # Only nudge for VERIFIED co-change (correct-or-quiet)
                            if getattr(config, "_host_graph_db", "") and os.path.exists(config._host_graph_db) and config._consensus_scope_edited:
                                try:
                                    import sqlite3 as _sq_sf_leg
                                    _sfc_leg = _sq_sf_leg.connect(config._host_graph_db)
                                    _ed_pats_leg = [f"%{_escape_like(ef.replace(chr(92), '/').lstrip('/'))}" for ef in config._consensus_scope_edited]
                                    _conn_rem_leg = []
                                    for _rf in _rem_leg:
                                        _rf_pat = f"%{_escape_like(_rf.replace(chr(92), '/').lstrip('/'))}"
                                        for _ep in _ed_pats_leg:
                                            if _sfc_leg.execute(
                                                "SELECT COUNT(*) FROM edges e "
                                                "JOIN nodes n1 ON e.source_id = n1.id "
                                                "JOIN nodes n2 ON e.target_id = n2.id "
                                                "WHERE COALESCE(e.confidence, 0.5) >= 0.7 "
                                                "AND ((n1.file_path LIKE ? ESCAPE '\\' AND n2.file_path LIKE ? ESCAPE '\\') "
                                                " OR (n1.file_path LIKE ? ESCAPE '\\' AND n2.file_path LIKE ? ESCAPE '\\'))",
                                                (_ep, _rf_pat, _rf_pat, _ep),
                                            ).fetchone()[0] > 0:
                                                _conn_rem_leg.append(_rf)
                                                break
                                    _sfc_leg.close()
                                    if _conn_rem_leg:
                                        _rem_leg = _conn_rem_leg
                                        _rnames = ", ".join(_short_path_leg(r) for r in _rem_leg[:3])
                                except Exception:
                                    pass
                            if _conn_rem_leg:
                                _rnames = ", ".join(_short_path_leg(r) for r in _conn_rem_leg[:3])
                                evidence = evidence.rstrip() + f"\n[GT] {_done_leg}/{_total_leg} scope files edited. {len(_conn_rem_leg)} verified co-change file(s) remain: {_rnames}\n"
                        elif not _rem_leg and _done_leg == _total_leg and _total_leg > 1:
                            evidence = evidence.rstrip() + f"\n[GT] All {_total_leg} scope files covered. Verify your changes.\n"
                    # Graph-based scope check: if callers span multiple files
                    # but agent has only edited one, warn early
                    # Uses query proxy (1 bash call) instead of requiring host graph.db
                    if config.graph_db and len(config.edited_files) == 1 and evidence.strip():
                        try:
                            _enorm = _edit_norm_leg or (rel_p or "").replace("\\", "/").lstrip("./").lstrip("/")
                            _host_db = getattr(config, "_host_graph_db", "")
                            if _host_db and os.path.exists(_host_db):
                                import sqlite3 as _sq_scope
                                _sc = _sq_scope.connect(_host_db)
                                _caller_files = _sc.execute(
                                    "SELECT DISTINCT nsrc.file_path FROM nodes nt "
                                    "JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS' "
                                    "JOIN nodes nsrc ON e.source_id = nsrc.id "
                                    "WHERE nt.file_path LIKE ? ESCAPE '\\' AND nsrc.file_path NOT LIKE ? ESCAPE '\\' "
                                    "AND COALESCE(e.confidence, 0.5) >= 0.5 LIMIT 5",
                                    (f"%{_escape_like(_enorm)}", f"%{_escape_like(_enorm)}"),
                                ).fetchall()
                                _sc.close()
                            else:
                                import json as _j_scope
                                _enorm_esc = _escape_like(_enorm).replace("'", "''")
                                _raw = _container_query(
                                    config._task_end_orig_run_action, config.graph_db,
                                    f"SELECT DISTINCT nsrc.file_path FROM nodes nt "
                                    f"JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS' "
                                    f"JOIN nodes nsrc ON e.source_id = nsrc.id "
                                    f"WHERE nt.file_path LIKE '%{_enorm_esc}' ESCAPE '\\' AND nsrc.file_path NOT LIKE '%{_enorm_esc}' ESCAPE '\\' "
                                    f"AND COALESCE(e.confidence, 0.5) >= 0.5 LIMIT 5",
                                )
                                _caller_files = _j_scope.loads(_raw)
                            if len(_caller_files) >= 2:
                                _cnames = ", ".join(os.path.basename(f[0] if isinstance(f, (list, tuple)) else f) for f in _caller_files[:3])
                                evidence = evidence.rstrip() + f"\n[SCOPE] Callers in {len(_caller_files)} files ({_cnames}); you've edited 1 file so far.\n"
                        except Exception as _scope_exc:
                            print(f"[GT_META] scope_check_error: {type(_scope_exc).__name__}: {_scope_exc}", flush=True)
            # L6 early review (legacy path) removed (correct-or-quiet): this block
            # only emitted "PRESERVE: ... callers depend on it" — a caller-EDIT
            # prescription (SWE-PRM NeurIPS 2025: action-prescriptive feedback
            # lowers resolution). It carried no verifiable-only output, so it is
            # deleted outright. Verifiable test coverage is delivered by
            # _maybe_fire_presubmit_verify() at the edit→review transition.
            return _deliver_or_trace(obs, evidence, config, "l3", rel_p or event.path)

        if event.kind == "finish":
            # L5 governor: unsafe finish check
            _l5_gov = getattr(config, "_l5_governor", None)
            if _l5_gov is not None and not _GT_BASELINE:
                try:
                    _l5d = _l5_gov.after_interaction(
                        action, obs, config.action_count, config.max_iter,
                        edited_files=config.edited_files,
                    )
                    if _l5d.fired:
                        # BUG-001 fix: finish handler runs after FINISHED — agent never sees this
                        _l5_eid = _emit_structured_event(
                            config, "L5", _l5d.hook_name,
                            emitted=False, suppressed=True,
                            suppression_reason="finish_handler_dead_write",
                        )
                        if _l5d.message:
                            _l5b_eid = _emit_structured_event(
                                config, "L5b", f"intervention_{_l5d.hook_name}",
                                parent_event_id=_l5_eid,
                                rendered_text=_l5d.message,
                                next_action_type=_l5d.next_action_type,
                                next_action_file=_l5d.next_action_file,
                                next_action_test=_l5d.next_action_test,
                                emitted=False, suppressed=True,
                                suppression_reason="finish_handler_dead_write",
                            )
                            obs = append_observation(obs, f"\n\n{_l5d.message}\n")
                            _log_gt_interaction(
                                config, "L5", "governor_finish", "advisory", _l5d.message,
                                agent_action_before=act_text[:300],
                                event_id=_l5b_eid or "",
                                parent_event_id=_l5_eid or "",
                                next_action_type=_l5d.next_action_type or "",
                            )
                            _register_pending_next_action(config, _l5b_eid or "", _l5d.next_action_type or "", _l5d.next_action_file or "")
                        elif _l5d.suppressed:
                            _l5b_blk = _emit_structured_event(
                                config, "L5b", "blocked_by_safety",
                                parent_event_id=_l5_eid, suppressed=True,
                                suppression_reason=_l5d.suppression_reason,
                            )
                            _log_gt_interaction(config, "L5b", "governor_finish", "blocked", f"[blocked: {_l5d.suppression_reason}]", event_id=_l5b_blk or "")
                except Exception as l5_exc:
                    print(f"[GT_META] L5 governor error on finish: {l5_exc}", flush=True)

            # Decision 34: Goku L5 check on finish
            if _l5_gov is not None and not _GT_BASELINE and os.environ.get("GT_L5_GOKU_EVENTS", "1") == "1":
                try:
                    _goku_d = _l5_gov.goku_check(
                        action, obs, config.action_count, config.max_iter,
                        file_path=None,
                    )
                    if _goku_d.fired and _goku_d.message and not _goku_d.suppressed:
                        # BUG-001 fix: finish handler — agent never sees this
                        _goku_eid = _emit_structured_event(
                            config, "L5", _goku_d.hook_name,
                            emitted=False, suppressed=True,
                            suppression_reason="finish_handler_dead_write",
                        )
                        _goku_l5b_eid = _emit_structured_event(
                            config, "L5b", f"intervention_{_goku_d.hook_name}",
                            parent_event_id=_goku_eid,
                            rendered_text=_goku_d.message,
                            next_action_type=_goku_d.next_action_type,
                            next_action_file=_goku_d.next_action_file,
                            emitted=False, suppressed=True,
                            suppression_reason="finish_handler_dead_write",
                        )
                        obs = append_observation(obs, f"\n\n{_goku_d.message}\n")
                        _log_gt_interaction(
                            config, "L5", "goku_finish", "advisory", _goku_d.message,
                            agent_action_before=act_text[:300],
                            event_id=_goku_l5b_eid or "",
                        )
                except Exception as gk_exc:
                    print(f"[GT_META] L5 goku error on finish: {gk_exc}", flush=True)

            # L6 Pre-Submit Review: REMOVED (2026-05-28).
            # OH sets state=FINISHED before calling runtime.run_action, so any
            # observation appended in the finish handler is a DEAD WRITE — the
            # agent never steps again and never reads it. The prior block ran a
            # full `git diff HEAD` + per-export caller-count graph queries + an
            # assertions sweep, then appended a [PRE-SUBMIT REVIEW] the agent
            # could never see. That is pure cost for zero delivery — it fails
            # the goal test (no correct context reaches the agent).
            #
            # The verify-before-finish VALUE is already delivered upstream by
            # L3 post-edit's verification suggestion ([GT_VERIFY] / gt_run_tests
            # hook), which fires after each edit and DOES reach the agent
            # (SWE-agent guardrail class, +10.7pp, NeurIPS 2024).
            #
            # A real diff-wide pre-submit gate would need a pre-FINISHED hook
            # (OH does not expose one on the run_action path); deferred, and
            # the research on semantic pre-submit review is mixed anyway
            # (SWE-agent review_on_submit rejected correct patches).
            if not _GT_BASELINE:
                print("[GT_META] l6_pre_submit: skipped (finish-handler dead write removed)", flush=True)

            # Kill any stuck bash process so complete_runtime can cd into the workspace.
            try:
                orig_run_action(_cmd_action("kill %1 2>/dev/null; wait 2>/dev/null; true", timeout=5))
            except Exception:
                pass
            _strip_scaffold_files(orig_run_action, config, instance_ref)
            _flush_interaction_log(config, instance_ref)
            _pull_graph_db_artifact(config)

            advisory = render_l5_advisory(config)
            unresolved = _l5_unresolved_paths(config)
            # Fix 6: Keep advisory for state/telemetry but remove agent-visible injection

            instance_ref = getattr(runtime, "_gt_instance", None)
            if advisory and instance_ref is not None:
                try:
                    instance_ref["gt_advisory"] = advisory
                except Exception:
                    try:
                        setattr(instance_ref, "gt_advisory", advisory)
                    except Exception:
                        pass
            _pull_hook_logs(orig_run_action, instance_ref)

            tel_fin = getattr(config, "telemetry", None)
            if tel_fin is not None:
                tel_fin.record_gate(bool(unresolved))
                _write_gt_telemetry(instance_ref, tel_fin)
                fin = tel_fin.finalize()
                print(f"[GT_META] === TASK COMPLETE: {tel_fin.task_id} ===", flush=True)
                print(f"[GT_META] Layer hits: {json.dumps(fin.get('layer_hits', {}))}", flush=True)
                print(f"[GT_META] Utilization: {json.dumps(fin.get('utilization', {}))}", flush=True)
                print(f"[GT_META] Overall: {fin.get('overall_utilization', 0)}", flush=True)
                print(f"[GT_META] Actions: {config.action_count}, Edits: {len(config.edited_files)}, Views: {len(config.viewed_files)}", flush=True)
                print(f"[GT_META] L5 metrics: {json.dumps(config._l5_metrics)}", flush=True)
                # FINAL_ARCH_V2 fail-fast: live mode requires non-zero router events.
                _v2m = _router_v2_mode()
                _v2_calls = getattr(config, "_router_v2_call_count", 0)
                print(
                    f"[GT_META] router_v2 final: mode={_v2m} calls={_v2_calls} "
                    f"events_persisted={len([e for e in config.interaction_log if 'router_v2' in e or 'router_v2_legacy_skip' in e])}",
                    flush=True,
                )
                _had_io_events = (len(config.viewed_files) + len(config.edited_files)) > 0
                if _v2m in ("shadow", "live") and _had_io_events and _v2_calls == 0:
                    print(
                        f"[GT_FATAL] GT_ROUTER_V2={_v2m} but 0 router calls with "
                        f"{len(config.viewed_files)} views + {len(config.edited_files)} edits — "
                        "wrapper call site silently bypassed router",
                        flush=True,
                    )
            _flush_task_end_metrics(config, "finish")

        return obs

    runtime.run_action = patched_run_action
    runtime._gt_full_wrapped = True
    runtime._gt_full_config = config
    return runtime


_ORIG_INITIALIZE_RUNTIME = None
_ORIG_GET_INSTRUCTION = None

L4_PREFETCH_MAX_QUERIES = 3
L4_PREFETCH_MAX_LINES_PER_QUERY = 4
L4_PREFETCH_MAX_CHARS = 600
L4_PREFETCH_WALL_TIMEOUT = 30
L4_NOISE_PATTERNS = ("body spans", "sibling:")

# A path token is a run of path-legal characters only. The old greedy ``\S+``
# swallowed leading/embedded grouping punctuation (e.g. a ``(`` from a caller-code
# snippet ``x = (foo/bar.py``), polluting brief_candidates with ``(file`` entries.
# Restrict to path-legal chars [alnum _ . / \ ~ + @ -] so grouping/quote/separator
# punctuation ``( ) [ ] { } , : ; ' "`` and whitespace terminate the token. This is
# a structural property of file paths in general (POSIX/relative/scoped), not a
# benchmark-shaped rule.
_FILE_PATH_RE = re.compile(r"([A-Za-z0-9_./\\~+@-]+\.(?:py|go|js|ts|rs|java|rb|php))\b")


_RANK_LINE_RE = re.compile(r"^\s*\d+\.\s+")


def _extract_candidate_files(brief: str) -> list[str]:
    """Extract the RANKED edit-target file paths from the brief.

    Flow-audit fix (brief-to-candidates over-harvest -> consensus over-fire):
    harvest ONLY the numbered rank lines ("1. beets/importer.py (...)"), NOT the
    Callers:/Tests:/Calls:/EDIT-TARGET/graph-map sub-lines. Those sub-lines carry
    caller/test/callee paths and bare basenames (e.g. "db.py") which, via the
    consensus gate's basename fallback, made consensus fire on unrelated
    same-named files in other directories. Rank-line order is preserved (rank-1
    first) so the list reflects the localizer's ranking for any order-dependent
    consumer. Gold (the #1 rank line) is unaffected — it was always captured.
    """
    files: list[str] = []
    seen: set[str] = set()
    for line in brief.splitlines():
        if not _RANK_LINE_RE.match(line):
            continue  # skip Callers:/Tests:/Calls:/EDIT-TARGET/graph-map sub-lines
        m = _FILE_PATH_RE.search(line)
        if not m:
            continue
        fp = m.group(1)
        if fp not in seen:
            seen.add(fp)
            files.append(fp)
    return files


def _select_issue_seeded_symbols(
    orig_run_action: Callable[[Any], Any],
    config: GTRuntimeConfig,
    issue_text: str,
    candidate_files: list[str],
    max_symbols: int = 3,
) -> list[str]:
    """Issue-text-seeded symbol selection (SweRank §3.2 deterministic pattern).

    1. Extract potential identifiers from issue text
    2. Deterministically filter tokens against graph.db (Agnostic Soft-Filter)
    3. Rerank based on node degree (centrality) within candidate files
    """
    if not candidate_files:
        return []

    # Extract likely function/method identifiers from issue text. NO blocklist: the
    # graph-membership intersection below already drops any token that is not a
    # DEFINED symbol in this repo (English words, builtins), and a data-derived
    # HOMONYM filter (repo P95 def-count, computed in the in-container script) drops
    # the generic-hub half the old _BUILTIN_NOISE list hand-encoded. That static list
    # also dropped REAL symbols (get/run/open) — the exact false-negative poison.
    # Keep only the cheap lexical pre-trim (snake/camel first) to order candidates.
    # (Aider def-frequency genericness; Agentless arXiv 2407.01489: issue keywords
    # ARE the signal, hub-centrality fallback selects irrelevant symbols.)
    issue_idents: list[str] = []
    seen_toks: set[str] = set()
    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b", issue_text or ""):
        tok = m.group(1)
        if tok in seen_toks:
            continue
        # Prefer snake_case or camelCase (likely function/method names)
        if "_" in tok or (tok[0].islower() and any(c.isupper() for c in tok[1:])):
            issue_idents.insert(0, tok)
        else:
            issue_idents.append(tok)
        seen_toks.add(tok)

    if not issue_idents:
        return []

    # Batch check tokens against the graph nodes - this is the "Soft-Filter"
    # A token is only kept if it actually exists as a named entity in this repo's graph.
    file_likes = " OR ".join(
        f"n.file_path LIKE '%{_escape_like(f).replace(chr(39), '')}' ESCAPE '\\'" for f in candidate_files[:5]
    )
    # We limit to first 100 tokens to keep the SQL query size reasonable
    issue_names_sql = ",".join(f"'{n.replace(chr(39), '')}'" for n in issue_idents[:100])

    py_script = (
        "import sqlite3\n"
        f"c = sqlite3.connect('{config.graph_db}')\n"
        # repo P95 definition-count (homonymy ceiling) — DATA-DERIVED, no magic
        # threshold; >=20-sample guard (a P95 below 1/(1-0.95)=20 points == the max).
        "dc = [r[0] for r in c.execute(\"SELECT COUNT(DISTINCT file_path) cc FROM nodes "
        "WHERE label IN ('Function','Method','Class') AND name IS NOT NULL "
        "GROUP BY LOWER(name) ORDER BY cc\").fetchall()]\n"
        "d95 = dc[int(len(dc)*0.95)] if len(dc) >= 20 else None\n"
        "def homonym(nm):\n"
        "    if d95 is None: return False\n"
        "    n = c.execute(\"SELECT COUNT(DISTINCT file_path) FROM nodes WHERE LOWER(name)=LOWER(?) "
        "AND label IN ('Function','Method','Class')\", (nm,)).fetchone()[0] or 0\n"
        "    return n > d95\n"
        "results = []\n"
    )
    # SweRank-style: intersection of issue tokens and graph nodes IN candidate files,
    # minus repo HOMONYMS (the data-derived replacement for the old _BUILTIN_NOISE
    # blocklist). Membership drops English words for free; homonym() drops the
    # generic-hub half by per-repo definition-frequency (Aider; Step-2 finding #1).
    py_script += (
        f"for r in c.execute(\n"
        f"    \"SELECT DISTINCT n.name FROM nodes n \"\n"
        f"    \"WHERE n.name IN ({issue_names_sql}) \"\n"
        f"    \"AND ({file_likes}) \"\n"
        f"    \"AND n.label IN ('Function','Method','Class') LIMIT 50\"\n"
        f").fetchall():\n"
        f"    if not homonym(r[0]) and r[0] not in results:\n"
        f"        results.append(r[0])\n"
        f"    if len(results) >= {max_symbols}: break\n"
    )
    # Fallback: widen to ALL graph files (issue keywords ARE the signal — Agentless
    # arXiv 2407.01489; hub-centrality fallback selects irrelevant symbols).
    py_script += (
        f"if len(results) < {max_symbols}:\n"
        f"    for r in c.execute(\n"
        f"        \"SELECT DISTINCT n.name FROM nodes n \"\n"
        f"        \"WHERE n.name IN ({issue_names_sql}) \"\n"
        f"        \"AND n.label IN ('Function','Method','Class') LIMIT 50\"\n"
        f"    ).fetchall():\n"
        f"        if not homonym(r[0]) and r[0] not in results:\n"
        f"            results.append(r[0])\n"
        f"        if len(results) >= {max_symbols}: break\n"
        "for name in results:\n"
        "    print(name)\n"
        "c.close()\n"
    )
    script_path = "/tmp/gt_symbol_query.py"
    payload = py_script.encode("utf-8")
    b64 = base64.b64encode(payload).decode("ascii")
    _run_internal(
        orig_run_action,
        f"echo -n '{b64}' | base64 -d > {script_path}",
        10,
    )
    raw = _run_internal(
        orig_run_action,
        _env_prefix(config) + f"python3 {script_path} 2>/dev/null",
        10,
    ).strip()
    symbols = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.startswith("Traceback")]
    return symbols[:max_symbols]


def _run_l4_prefetch(
    orig_run_action: Callable[[Any], Any],
    config: GTRuntimeConfig,
    brief: str,
    issue_text: str,
    tel: Any,
) -> str:
    """Pre-fetch gt_query evidence for issue-relevant symbols. Returns formatted block."""
    candidate_files = _extract_candidate_files(brief)
    symbols = _select_issue_seeded_symbols(
        orig_run_action, config, issue_text, candidate_files, L4_PREFETCH_MAX_QUERIES,
    )
    if not symbols:
        if tel is not None:
            tel.record_l4_prefetch(0, 0)
        return ""

    import time as _time
    t0 = _time.monotonic()
    blocks: list[str] = []
    queries_run = 0
    total_lines = 0

    for sym in symbols:
        elapsed = _time.monotonic() - t0
        if elapsed >= L4_PREFETCH_WALL_TIMEOUT:
            print(f"L4_PREFETCH: wall timeout after {queries_run} queries ({elapsed:.1f}s)", flush=True)
            break
        cmd = (
            _env_prefix(config)
            + f"python3 ${{GT_TOOLS_DIR:-/tmp/gt_tools}}/gt_query/lib/gt_query.py {_sh_single_quote(sym)} 2>&1"
        )
        raw = _run_internal(orig_run_action, cmd, 10).strip()
        queries_run += 1
        if not raw or len(raw) < 10:
            continue
        lines = raw.splitlines()
        kept: list[str] = []
        for ln in lines[:L4_PREFETCH_MAX_LINES_PER_QUERY * 2]:
            if any(noise in ln for noise in L4_NOISE_PATTERNS):
                continue
            if "[VERIFIED]" in ln or "[POSSIBLE]" in ln or ln.startswith("# gt_query:"):
                kept.append(ln)
            if len(kept) >= L4_PREFETCH_MAX_LINES_PER_QUERY:
                break
        if not kept:
            continue
        total_lines += len(kept)
        blocks.append("\n".join(kept))

    # Git precedent: last commit for top 2 candidate files
    for cfile in candidate_files[:2]:
        elapsed = _time.monotonic() - t0
        if elapsed >= L4_PREFETCH_WALL_TIMEOUT:
            break
        git_cmd = (
            f"cd {_sh_single_quote(config.workspace_root)} && "
            f"git log --oneline -1 --follow -- {_sh_single_quote(cfile)} 2>/dev/null"
        )
        git_out = _run_internal(orig_run_action, git_cmd, 5).strip()
        if git_out and len(git_out) > 5:
            blocks.append(f"# {cfile}: last commit: {git_out[:80]}")
            total_lines += 1

    wall_ms = int((_time.monotonic() - t0) * 1000)
    print(
        f"L4_PREFETCH: queries={queries_run} symbols={symbols} "
        f"lines={total_lines} wall_ms={wall_ms}",
        flush=True,
    )

    if tel is not None:
        tel.record_l4_prefetch(queries_run, total_lines)

    if not blocks:
        return ""

    evidence = "\n".join(blocks)
    if len(evidence) > L4_PREFETCH_MAX_CHARS:
        evidence = evidence[:L4_PREFETCH_MAX_CHARS] + "\n[L4_PREFETCH_TRUNCATED]"

    return (
        f"\n<gt-prefetch layer=\"L4\" queries=\"{queries_run}\" "
        f"symbols=\"{','.join(symbols)}\" wall_ms=\"{wall_ms}\">\n"
        + evidence
        + "\n</gt-prefetch>"
    )


def _b64_chunks(payload: bytes, chunk_size: int = 8000) -> list[str]:
    encoded = base64.b64encode(payload).decode("ascii")
    return [encoded[i : i + chunk_size] for i in range(0, len(encoded), chunk_size)]


def patched_initialize_runtime(runtime: Any, instance: Any, metadata: Any) -> None:
    if _ORIG_INITIALIZE_RUNTIME is not None:
        try:
            _ORIG_INITIALIZE_RUNTIME(runtime, instance, metadata)
        except Exception as _init_exc:
            # OH's initialize_runtime asserts `which python` finds a testbed
            # interpreter. Non-Python repos (Go, Rust, TS, JS) don't have one.
            # GT works on all 5 languages — catch the assertion and continue so
            # the agent can still run on non-Python tasks. The python check is
            # OH-specific, not a GT requirement.
            print(
                f"[GT_META] initialize_runtime raised {type(_init_exc).__name__}: "
                f"{_init_exc} — continuing (non-Python repo)",
                flush=True,
            )
    workspace_name = (
        getattr(instance, "instance_id", "")
        or (instance.get("instance_id", "") if isinstance(instance, dict) else "")
    )
    workspace_name = (workspace_name or "").strip()

    tentative = f"{WORKSPACE_ROOT}/{workspace_name}" if workspace_name else WORKSPACE_ROOT

    qr = _sh_single_quote(tentative + "/.git")
    wr = _sh_single_quote(WORKSPACE_ROOT + "/.git")

    probe_cmd = (
        f"if [ -d {qr} ]; then echo {_sh_single_quote(tentative)}; "
        f"elif [ -d {wr} ]; then echo {_sh_single_quote(WORKSPACE_ROOT)}; "
        f"else GITDIR=$(find {_sh_single_quote(WORKSPACE_ROOT)} -maxdepth 3 -type d -name .git "
        "-print -quit 2>/dev/null); "
        '[ -n "$GITDIR" ] && dirname "$GITDIR"; fi'
    )

    probed_root = (
        _run_internal(runtime.run_action, probe_cmd, 45).strip().split("\n")[0].strip().strip("'\"")
    )
    workspace_root = probed_root if probed_root else tentative

    tel = GTTelemetry(workspace_name or "unknown")
    _max_iter = int(os.environ.get("GT_MAX_ITER", str(getattr(metadata, "max_iterations", 100))))
    config = GTRuntimeConfig(
        workspace_root=workspace_root,
        telemetry=tel,
        max_iter=_max_iter,
        instance_ref=instance,
    )
    config._meta_instance_id = workspace_name or "unknown"

    # L5 trajectory governor (Decision 30 + test-failure hooks)
    try:
        from groundtruth.trajectory.governor import L5Governor
        config._l5_governor = L5Governor(
            instance_id=workspace_name or "unknown",
            max_iter=_max_iter,
        )
        print(f"[GT_META] L5 governor initialized for {workspace_name}", flush=True)
    except Exception as exc:
        config._l5_governor = None  # type: ignore[attr-defined]
        print(f"[GT_META] L5 governor init failed: {exc}", flush=True)

    # Initialize lazy edge verifier (jedi__branch: LSP-verified edges)
    if os.environ.get("GT_LSP_VERIFY", "1") == "1":
        try:
            from groundtruth.lsp.edge_verifier import LazyEdgeVerifier
            config._edge_verifier = LazyEdgeVerifier(
                workspace_root=config.workspace_root,
                graph_db=config.graph_db,
            )
            import asyncio
            asyncio.get_event_loop().run_until_complete(config._edge_verifier.start())
            print(f"[GT_META] Edge verifier (LSP) initialized for {workspace_name}", flush=True)
            # graph.db download happens after first L6 reindex (not here — db doesn't exist yet)
        except Exception as exc:
            config._edge_verifier = None
            print(f"[GT_META] Edge verifier init failed (falling back to gt-index only): {exc}", flush=True)

    # Initialize structured telemetry writer (GT_STRUCTURED_EVENTS=1)
    if os.environ.get("GT_STRUCTURED_EVENTS", "0") == "1":
        try:
            from groundtruth.telemetry.writer import GTTelemetryWriter
            _run_id = os.environ.get("GITHUB_RUN_ID", "local")
            config._telemetry_writer = GTTelemetryWriter(
                run_id=_run_id,
                task_id=workspace_name or "unknown",
                output_dir=os.environ.get("GT_DEBUG_DIR", "/tmp"),
            )
            print(f"[GT_META] Telemetry writer initialized for {workspace_name}", flush=True)
        except Exception as exc:
            config._telemetry_writer = None
            print(f"[GT_META] Telemetry writer init failed: {exc}", flush=True)

    runtime._gt_instance = instance
    try:
        if isinstance(instance, dict):
            instance["_gt_runtime"] = runtime
        else:
            setattr(instance, "_gt_runtime", runtime)
    except Exception:
        pass

    l4_ok = install_graph_and_hook(runtime, config)

    # B-7: graph.db access mode.
    # "proxy" (default): one query to get node count for L5 threshold (~1 sec)
    # "always": full 11MB chunked transfer (~7.5 min) for host-side graph.db
    _transfer_mode = os.environ.get("GT_GRAPH_DB_TRANSFER", "proxy").lower()
    if config.graph_db and _transfer_mode == "proxy":
        try:
            import json as _j_proxy
            _nc_raw = _container_query(
                runtime, config.graph_db,
                "SELECT COUNT(*) FROM nodes",
            )
            _nc = _j_proxy.loads(_nc_raw)
            _node_count = _nc[0][0] if _nc else 0
            _l5g = getattr(config, "_l5_governor", None)
            if _l5g and _node_count > 0:
                if _node_count > 5000:
                    _l5g._cached_scaffold_threshold = 35
                elif _node_count > 1000:
                    _l5g._cached_scaffold_threshold = 25
                else:
                    _l5g._cached_scaffold_threshold = 20
                print(f"[GT_META] B-7 proxy: node_count={_node_count} L5_threshold={_l5g._cached_scaffold_threshold} (1 query, ~1 sec)", flush=True)
            else:
                print(f"[GT_META] B-7 proxy: node_count={_node_count} (no L5 governor)", flush=True)
        except Exception as _proxy_exc:
            print(f"[GT_META] B-7 proxy failed: {_proxy_exc}", flush=True)
    elif config.graph_db and _transfer_mode == "always":
        try:
            _local_db = _download_graph_db_to_host(runtime, config.graph_db)
            if _local_db:
                config._host_graph_db = _local_db
                # Set GT_GRAPH_DB env on host so L5 governor + other host-side
                # code can access graph.db (they read os.environ, not config)
                os.environ["GT_GRAPH_DB"] = _local_db
                # Invalidate governor's cached threshold so it re-reads from the downloaded graph
                _l5g = getattr(config, "_l5_governor", None)
                if _l5g:
                    if hasattr(_l5g, "_cached_scaffold_threshold"):
                        delattr(_l5g, "_cached_scaffold_threshold")
                    _l5g._threshold_needs_refresh = True
                    print(f"[GT_META] L5 governor threshold cache invalidated (will re-read from {_local_db})", flush=True)
                print(
                    f"[GT_META] B-7 pre-fetch: graph.db downloaded to host at "
                    f"{_local_db} (graph_db={config.graph_db})",
                    flush=True,
                )
            else:
                print(
                    f"[GT_META] B-7 pre-fetch: graph.db download returned empty "
                    f"(graph_db={config.graph_db})",
                    flush=True,
                )
                if _router_v2_live():
                    print(
                        "[GT_META] L6_REINDEX_SYNC_FAILED_NONFATAL "
                        "graph.db pre-fetch to host failed — router uses cached/stale graph. "
                        "L3/L3b hooks still run in-container where graph.db exists.",
                        flush=True,
                    )
        except Exception as pf_exc:
            print(
                f"[GT_META] B-7 pre-fetch failed: {type(pf_exc).__name__}: {pf_exc}",
                flush=True,
            )

    try:
        instance["gt_l4_tools"] = l4_ok
    except Exception:
        try:
            setattr(instance, "gt_l4_tools", l4_ok)
        except Exception:
            pass

    issue_text = getattr(instance, "problem_statement", "") or ""
    if not issue_text and isinstance(instance, dict):
        issue_text = str(instance.get("problem_statement", "") or "")
    issue_text = str(issue_text or "")

    brief = ""
    l2_tag = ""
    task_id = workspace_name or "unknown"
    _reset_iter_state(config, task_id)
    print(f"[GT_META] EVAL_CONDENSER={os.environ.get('EVAL_CONDENSER', 'NOT SET')}", flush=True)
    try:
        _record_diff_snapshot(runtime.run_action, config, "[task_start]", 0)
    except Exception:
        pass

    brief_runner = (
        "import json, sys\n"
        "import inspect\n"
        "meta_path, issue_path = sys.argv[1], sys.argv[2]\n"
        'with open(meta_path, encoding="utf-8") as f:\n'
        "    meta = json.load(f)\n"
        'with open(issue_path, encoding="utf-8", errors="replace") as f:\n'
        "    issue = f.read()\n"
        "try:\n"
        "    from groundtruth.pretask.v1r_brief import generate_v1r_brief\n"
        "    import os, sqlite3 as _sq\n"
        "    _db = meta['graph_db']\n"
        "    _rr = meta['repo_root']\n"
        "    _db_exists = os.path.exists(_db)\n"
        "    _nc = 0\n"
        "    if _db_exists:\n"
        "        _nc = _sq.connect(_db).execute('SELECT COUNT(DISTINCT file_path) FROM nodes WHERE is_test=0').fetchone()[0]\n"
        "    _sample = ''\n"
        "    if _db_exists:\n"
        "        _sample = str(_sq.connect(_db).execute('SELECT file_path FROM nodes LIMIT 3').fetchall())\n"
        "    _rr_exists = os.path.isdir(_rr)\n"
        "    _rr_list = str(os.listdir(_rr)[:5]) if _rr_exists else 'DIR_MISSING'\n"
        '    print(f"[GT_BRIEF_DIAG] db={_db} exists={_db_exists} files={_nc} sample={_sample}")\n'
        '    print(f"[GT_BRIEF_DIAG] repo_root={_rr} exists={_rr_exists} ls={_rr_list}")\n'
        "    out = generate_v1r_brief(\n"
        "        issue_text=issue,\n"
        "        repo_root=meta['repo_root'],\n"
        "        graph_db=meta['graph_db'],\n"
        "        bug_id=meta['task_id'],\n"
        "    )\n"
        "    brief = out.brief_text or ''\n"
        '    print(f"[GT_BRIEF_DIAG] brief_len={len(brief)} files={len(out.files)}")\n'
        "    print(brief.strip())\n"
        "    v74 = out.v74_result\n"
        "    m6 = {}\n"
        "    if v74 is not None:\n"
        "        m6 = {'focus_set': v74.focus_set, 'ranked_count': len(v74.ranked_full), 'ranked_full': [{'path': r.get('path'), 'score': round(float(r.get('score') or 0), 4), 'components': r.get('components', {})} for r in v74.ranked_full[:20]]}\n"
        '        print(f"[GT_BRIEF_DIAG] ranked_count={len(v74.ranked_full)} focus={v74.focus_set}")\n'
        "        for _ri, _rr in enumerate(v74.ranked_full[:12], 1):\n"
        "            _rc = _rr.get('components', {})\n"
        "            print(f\"[GT_RANK_DIAG] #{_ri} score={round(float(_rr.get('score') or 0), 4)} path_comp={round(float(_rc.get('path', 0) or 0), 3)} reach={round(float(_rc.get('reach', 0) or 0), 3)} lex={round(float(_rc.get('lex', 0) or 0), 3)} {_rr.get('path')}\")\n"
        '    print("\\n---GT_L2_JSON---")\n'
        '    print(json.dumps(m6))\n'
        "except Exception as exc:\n"
        "    import traceback\n"
        '    print(f"[GT_BRIEF_FAILED] {type(exc).__name__}: {exc}")\n'
        '    print(f"[GT_BRIEF_TRACEBACK] {traceback.format_exc()[-500:]}")\n'
    )

    if issue_text.strip():
        meta = {
            "repo_root": config.workspace_root,
            "graph_db": config.graph_db,
            "task_id": task_id,
        }
        _upload_bytes_b64(runtime, brief_runner.encode("utf-8"), "/tmp/gt_brief_runner.py")
        _upload_bytes_b64(runtime, json.dumps(meta).encode("utf-8"), "/tmp/gt_brief_meta.json")
        _upload_bytes_b64(runtime, issue_text.encode("utf-8"), "/tmp/gt_issue.txt")
        issue_terms = sorted(set(
            w.lower() for w in re.findall(r"[A-Za-z_]\w{2,}", issue_text)
            if len(w) > 3
        ))
        _upload_bytes_b64(
            runtime,
            "\n".join(issue_terms).encode("utf-8"),
            "/tmp/gt_issue_terms.txt",
        )
        # SINGLE-SOURCE ANCHORS (remediation §3.1, flow-audit risk #1): issue anchors
        # are extracted ONCE, in-container, by generate_v1r_brief against the SAME
        # graph_db that localize ranks with — it writes the canonical
        # /tmp/gt_issue_anchors.json at v1r_brief.py:1347, which runs AFTER this point
        # in the flow (the brief_runner invocation just below). A host-side extraction
        # here used config._host_graph_db, frequently ABSENT on the default path, so it
        # produced an UN-cross-checked upload that either raced the authoritative
        # in-container write or survived as STALE anchors on any path where that write
        # failed. The in-container consumers (post_view._contract_pillar /
        # _score_by_issue_relevance, post_edit) read that one file and already tolerate
        # its absence (return empty anchors), so removing the duplicate host extraction
        # eliminates the split without a new failure mode. DO NOT re-add a host-side
        # extract_issue_anchors call here.
        raw_br = (
            _run_internal(
                runtime.run_action,
                _env_prefix(config)
                + "python3 /tmp/gt_brief_runner.py /tmp/gt_brief_meta.json /tmp/gt_issue.txt 2>/tmp/gt_brief_stderr.log",
                180,
            ).strip()
        )
        # HOST-SIDE FALLBACK: if the in-container brief crashed (non-Python
        # containers lack numpy/pydantic), run generate_v1r_brief HOST-SIDE
        # where the GT package is fully installed. Uses the host graph.db copy.
        # One product, all 5 languages.
        _brief_fallback_db = getattr(config, "_host_graph_db", "") or os.environ.get("GT_PREBUILT_GRAPH_DB", "")
        if "GT_BRIEF_FAILED" in raw_br and _brief_fallback_db and os.path.exists(_brief_fallback_db):
            try:
                from groundtruth.pretask.v1r_brief import generate_v1r_brief as _host_brief
                _hb_out = _host_brief(
                    issue_text=issue_text,
                    repo_root="/tmp/testbed_src",
                    graph_db=_brief_fallback_db,
                    bug_id=task_id,
                )
                if _hb_out and _hb_out.brief_text:
                    raw_br = _hb_out.brief_text.strip()
                    print(f"[GT_META] L1 host-side brief OK ({len(raw_br)} chars)", flush=True)
            except Exception as _hb_exc:
                print(f"[GT_META] L1 host-side brief failed: {_hb_exc}", flush=True)
        # Diagnostic: capture stderr from brief runner
        _brief_stderr = _run_internal(
            runtime.run_action,
            "cat /tmp/gt_brief_stderr.log 2>/dev/null || echo 'no stderr'",
            10,
        ).strip()
        if _brief_stderr and _brief_stderr != "no stderr":
            print(f"[GT_META] Brief runner stderr: {_brief_stderr[:500]}", flush=True)
        print(f"[GT_META] Brief runner raw output ({len(raw_br)} chars): {raw_br[:300]}", flush=True)
        segments = raw_br.split("---GT_L2_JSON---")
        # Strip diagnostic stdout prefixes through the SINGLE central authority
        # (sanitizer._HIDDEN_PREFIXES — now includes [GT_RANK_DIAG]/[GT_BRIEF_DIAG]).
        # A local two-marker list here previously could re-leak any OTHER [GT_*]
        # marker that reached the brief; routing through is_hidden_line means every
        # strip site shares one list. Rank data is preserved in the GT_L2_JSON blob.
        def _brief_line_hidden(_ln: str) -> bool:
            if _SANITIZER_AVAILABLE:
                return _core_is_hidden_line(_ln)
            return _ln.lstrip().startswith(("[GT_BRIEF_DIAG]", "[GT_RANK_DIAG]", "[GT_META]"))

        brief = "\n".join(
            line for line in segments[0].strip().splitlines()
            if not _brief_line_hidden(line)
        ).strip()
        l2_blob: dict[str, Any] = {}
        if len(segments) > 1:
            try:
                l2_blob = json.loads(segments[1].strip())
            except Exception:
                l2_blob = {}
        fused_candidates = l2_blob.get("fused_candidates") if isinstance(l2_blob, dict) else None
        fused_n = len(fused_candidates) if isinstance(fused_candidates, list) else 0
        # Also check ranked_count from v7.4 scorer (fused_candidates is legacy)
        _ranked_count = l2_blob.get("ranked_count", 0) if isinstance(l2_blob, dict) else 0
        if _ranked_count > 0:
            fused_n = _ranked_count

        class _TelNS:
            def __init__(self, d: dict[str, Any]) -> None:
                self.module_6_hybrid = d

        l2_tag = _format_l2_pretask_tag(_TelNS(l2_blob))
        low_signal_brief = (
            not brief
            or "could not deterministically localize" in brief.lower()
            or "could not localize" in brief.lower()
        )
        if fused_n == 0 and low_signal_brief:
            keyword = "issue"
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", issue_text):
                low = token.lower()
                if low not in {"issue", "with", "from", "that", "this", "when", "then", "have"}:
                    keyword = token
                    break
            brief = (
                "GT could not rank files with confidence (0 candidates from graph).\n"
                f"Relevant symbol keyword from issue text: {keyword}"
            )

        if (
            (not brief)
            or ("GT graph built" in brief)
            or ("[GT_BRIEF_FAILED]" in brief)
            or (
                len(brief) < 100
                and "GT could not rank files with confidence" not in brief
            )
        ):
            brief = (
                f"[GT_BRIEF_FAILED] Brief generation produced no real content "
                f"(len={len(brief)}).\nRAW:\n{raw_br[:520]}"
            )
            tel.record_brief(False, bool(l2_tag))
        else:
            tel.record_brief(True, bool(l2_tag))
    else:
        tel.record_brief(False, False)

    brief = _rewrite_site_package_paths_in_brief(brief, workspace_name, config.workspace_root)
    brief_full = brief  # Keep full text for L1 logging (NOT truncated)
    # DELIVERY BACKSTOP (2026-05-29, root-caused): generate_v1r_brief is the PRIMARY
    # budget authority (its self-budget loop drops WHOLE entries), but leaves a single
    # fat entry unbounded — hence a generous hard backstop here. The real defect was NOT
    # truncation: _brief_max_tokens used to REORDER (path-lines first), which shredded the
    # structured v1r brief and emptied the <gt-graph-map> block before the agent saw it
    # (proven on haystack-8525: gt_brief map EMPTY, gt_brief_full POPULATED, same length).
    # _brief_max_tokens is now structure-preserving (in-order cap, no reorder), so a normal
    # ~2000-3000-char brief passes through untouched with blocks intact.
    brief = _brief_max_tokens(brief, max_tokens=2000)  # structure-preserving ~8000-char backstop

    if brief.strip():
        _raw_candidates = _extract_candidate_files(brief)
        # Store both raw paths AND instance-prefixed paths so matching works
        # regardless of whether viewed_files has the prefix or not.
        _prefixed = set()
        for c in _raw_candidates:
            _prefixed.add(c)  # raw: "sh.py"
            if workspace_name and not c.startswith(workspace_name):
                _prefixed.add(f"{workspace_name}/{c}")  # prefixed: "amoffat__sh-744/sh.py"
        config.brief_candidates = _prefixed
        print(f"[GT_META] brief_candidates={sorted(_prefixed)}", file=sys.stderr, flush=True)
        # Parse the localizer's multi-signal agreement tier from the brief. The
        # localizer emits <gt-localization confidence="high|medium|low"> where the
        # tier reflects how many of {grep, semantic, structural} signals agree.
        # Only "high" (>=2 signals) earns the CONFIDENT "primary target" consensus
        # emission. Absent/unparseable -> "" -> softer scope (correct-or-quiet).
        _loc_m = re.search(
            r"<gt-localization[^>]*confidence\s*=\s*[\x27\x22]([A-Za-z]+)[\x27\x22]",
            brief,
        )
        config._localization_confidence = (_loc_m.group(1).lower() if _loc_m else "")
        print(f"[GT_META] localization_confidence={config._localization_confidence!r}", file=sys.stderr, flush=True)

    if not brief.strip():
        brief = ""  # Fix 8: inject nothing if brief generation fails

    prefetch_block = _run_l4_prefetch(runtime.run_action, config, brief, issue_text, tel)
    if prefetch_block:
        brief = brief + "\n" + prefetch_block
        _l4_eid = _emit_structured_event(
            config, "L4", "prefetch",
            rendered_text=prefetch_block[:1200],
            evidence_items=[{"kind": "l4_constraint", "text": prefetch_block[:500], "source": "graph_db"}],
        )
        _log_gt_interaction(config, "L4", "prefetch", "prefetch_ok", prefetch_block[:500], event_id=_l4_eid or "")
    else:
        _l4_skip_eid = _emit_structured_event(
            config, "L4", "prefetch",
            emitted=False, suppressed=True,
            suppression_reason="no_prefetch_results",
        )
        _log_gt_interaction(config, "L4", "prefetch", "prefetch_skip", "no_results", event_id=_l4_skip_eid or "")

    try:
        instance["gt_brief"] = brief
        instance["gt_brief_full"] = brief_full  # Full untruncated brief for logging
    except Exception:
        try:
            setattr(instance, "gt_brief", brief)
            setattr(instance, "gt_brief_full", brief_full)
        except Exception:
            pass
    wrap_runtime_run_action(runtime, config)


def generate_task_brief(instance: Any) -> str:
    """Generate or retrieve the GT pre-task brief for the first user turn."""

    injected = getattr(instance, "gt_brief", "") or os.environ.get("GT_STATIC_BRIEF", "")
    if not injected and hasattr(instance, "get"):
        try:
            injected = instance.get("gt_brief", "")
        except Exception:
            injected = ""
    if injected.strip():
        return injected.strip()

    # Correct-or-quiet: the live canary brief is generated once via v1r during
    # install_graph_and_hook(). If that producer did not populate gt_brief, do
    # not fall back to retired brief paths.
    return ""


_GT_BASELINE = os.environ.get("GT_BASELINE", "0") == "1"

def patched_get_instruction(instance: Any, metadata: Any) -> Any:
    if _ORIG_GET_INSTRUCTION is None:
        raise RuntimeError("OpenHands get_instruction patch was not initialized")
    msg = _ORIG_GET_INSTRUCTION(instance, metadata)
    if _GT_BASELINE:
        print("[GT_META] BASELINE MODE — no GT layers", flush=True)
        return msg
    content = getattr(msg, "content", "") or ""
    # A5 fix: tool instruction decoupled from brief gate — always inject when
    # GT tools are active, so agent knows WHEN to use them even without a brief.
    # GT tools: 0% autonomous adoption across 12 trajectories. Agent never
    # calls gt_query/gt_validate/gt_search/gt_navigate — same info delivered
    # passively via hooks. These ~300 tokens of instructions are static context
    # that degrades performance per Du et al. EMNLP 2025 and ETH Zurich
    # AGENTS.md eval 2026. Suppressed for benchmark runs. Tools remain active
    # for human use via Claude Code / Cursor (GT_NATIVE_TOOLS=1 still registers
    # them in the MCP server).
    tools_hint = ""
    brief = generate_task_brief(instance)
    # L1+ Enhancement: Agentless-style edit targeting
    # Issue text needed for keyword → function matching
    _l1_issue_text = getattr(instance, "problem_statement", "") or ""
    if not _l1_issue_text and isinstance(instance, dict):
        _l1_issue_text = str(instance.get("problem_statement", "") or "")
    if brief and not _GT_BASELINE:
        _l1_graph_db = ""
        try:
            # Strategy 0: GT_PREBUILT_GRAPH_DB from GHA pre-index step (zero cost)
            _prebuilt = os.environ.get("GT_PREBUILT_GRAPH_DB", "")
            if _prebuilt and os.path.exists(_prebuilt):
                _l1_graph_db = _prebuilt
            # Strategy 1: pre-built indexes on host
            _l1_indexes_root = os.environ.get("GT_PREBUILT_INDEXES_ROOT", "")
            _l1_instance_id = getattr(instance, "instance_id", "") or getattr(instance, "id", "") or ""
            if _l1_indexes_root and _l1_instance_id:
                _l1_db_path = str(Path(_l1_indexes_root) / _l1_instance_id / "graph.db")
                if os.path.exists(_l1_db_path):
                    _l1_graph_db = _l1_db_path
            # Strategy 2: host graph.db downloaded during setup
            if not _l1_graph_db:
                _runtime = (
                    getattr(instance, "_gt_runtime", None)
                    or (instance.get("_gt_runtime") if isinstance(instance, dict) else None)
                )
                if _runtime:
                    _cfg = getattr(_runtime, "_gt_full_config", None)
                    if _cfg:
                        _host_db = getattr(_cfg, "_host_graph_db", "")
                        if _host_db and os.path.exists(_host_db):
                            _l1_graph_db = _host_db
        except Exception:
            pass

        if _l1_graph_db:
            try:
                _l1_conn = sqlite3.connect(f"file:{_l1_graph_db}?mode=ro", uri=True)
                _l1_conn.row_factory = sqlite3.Row
                try:
                    # Derive brief candidates from the brief text (file paths)
                    _l1_brief_files: list[str] = []
                    _FILE_PATTERN = re.compile(r"(\S+\.(?:py|go|js|ts|rs|java|rb|php))")
                    for _bl in brief.splitlines():
                        _bm = _FILE_PATTERN.search(_bl.strip())
                        if _bm:
                            _l1_brief_files.append(_bm.group(1))

                    # Issue-keyword → function matching (Agentless stage 2)
                    # Match issue terms to function names to find edit target
                    _issue_kws = set()
                    if _l1_issue_text:
                        _issue_kws = {
                            w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", _l1_issue_text)
                            if len(w) > 3 and w.lower() not in {
                                "that", "this", "with", "from", "have", "been",
                                "when", "then", "should", "would", "could",
                                "file", "line", "code", "test", "error", "issue",
                                "none", "true", "false", "self", "class",
                                "return", "function", "method", "import", "raise",
                                "except", "print", "string", "object", "value",
                                "result", "data", "list", "dict", "type", "name",
                                "path", "args", "kwargs", "super", "init", "call",
                                "make", "using", "does", "work", "need", "want",
                                "like", "also", "just", "some", "only", "more",
                            }
                        }

                    _edit_target = None  # (file, func_name, signature, callers, constraints)
                    _plan_lines: list[str] = []

                    # BUG-003 fix: evaluate ALL candidates, score, pick best.
                    # Invariant 3: issue-named function beats high-caller functions.
                    _COMMON_FN_PARTS = {
                        "get", "set", "add", "remove", "update", "create",
                        "delete", "find", "make", "check", "is", "has",
                        "do", "run", "to", "from", "on", "in", "of", "by",
                    }
                    _all_candidates: list[dict] = []
                    # L1-INV-1: Expand search space with issue-symbol-matched files.
                    # If issue text names a function in graph.db, that function's file
                    # MUST be searched, even if v7.4 didn't rank it in the brief.
                    _issue_symbol_files: set[str] = set()
                    if _issue_kws:
                        try:
                            _sym_rows = _l1_conn.execute(
                                "SELECT DISTINCT name, file_path FROM nodes WHERE is_test = 0"
                            ).fetchall()
                            for _sr in _sym_rows:
                                _sn = _sr["name"] or ""
                                _sf = _sr["file_path"] or ""
                                if _sn and _sf and _sn.lower() in _issue_kws:
                                    _issue_symbol_files.add(_sf)
                        except Exception:
                            pass
                    if _issue_symbol_files:
                        for _isf in _issue_symbol_files:
                            if _isf not in _l1_brief_files:
                                _l1_brief_files.append(_isf)
                        print(f"[GT_META] l1_issue_symbol_files: {sorted(_issue_symbol_files)}", flush=True)
                    for _bf in _l1_brief_files[:8]:
                        _bf_norm = _bf.replace("\\", "/").lstrip("/")
                        _key_funcs = _l1_conn.execute(
                            "SELECT id, name, label, signature, start_line, end_line FROM nodes "
                            "WHERE file_path LIKE ? ESCAPE '\\' AND is_exported = 1 AND is_test = 0 "
                            "ORDER BY (SELECT COUNT(*) FROM edges WHERE target_id = nodes.id AND type='CALLS') DESC LIMIT 5",
                            (f"%{_escape_like(_bf_norm)}",)
                        ).fetchall()
                        for _kf in _key_funcs:
                            _fn_parts = set(re.split(r"[_]|(?<=[a-z])(?=[A-Z])", _kf["name"]))
                            _fn_parts = {p.lower() for p in _fn_parts if p and p.lower() not in _COMMON_FN_PARTS}
                            _kw_overlap = len(_fn_parts & _issue_kws)
                            _direct = _kf["name"].lower() in _l1_issue_text.lower() if _l1_issue_text else False

                            # Score: direct mention dominates, keyword overlap secondary, callers tiebreak.
                            # Classes/interfaces mentioned in issue text are usually context (setup),
                            # not bug targets. Functions/methods mentioned are the complaint.
                            _score = 0
                            _is_class = _kf["label"] in ("Class", "Interface", "Struct")
                            if _direct:
                                if _is_class:
                                    _score += 200  # class mentioned in issue = context, not target
                                else:
                                    _score += 1000  # function/method mentioned = likely the bug
                            _score += _kw_overlap * 10
                            # Align with L1 brief threshold (0.7) — only count
                            # callers the agent will actually see in the brief.
                            _caller_count = _l1_conn.execute(
                                "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS' "
                                "AND COALESCE(confidence, 0.5) >= 0.7",
                                (_kf["id"],),
                            ).fetchone()[0]
                            _score += min(_caller_count, 5)  # callers capped as tiebreak
                            # SLOC + fan_out for role discount (Herbold PeerJ 2019)
                            _fn_sloc = max(0, (_kf["end_line"] or 0) - (_kf["start_line"] or 0))
                            _fn_fan_out = _l1_conn.execute(
                                "SELECT COUNT(*) FROM edges WHERE source_id = ?",
                                (_kf["id"],),
                            ).fetchone()[0]

                            # BUG-3 fix: admit ALL candidates to composite scorer.
                            # The composite (5-signal hybrid + sigma-based dynamic
                            # tiers) decides tier honestly; the OLD integer-_score
                            # is no longer a gate.
                            _constraints = []
                            _has_props_table = _l1_conn.execute(
                                "SELECT name FROM sqlite_master WHERE type='table' AND name='properties'"
                            ).fetchone()
                            if _has_props_table:
                                _props = _l1_conn.execute(
                                    "SELECT kind, value FROM properties WHERE node_id = ? "
                                    "AND kind IN ('guard_clause','conditional_return','exception_handler','side_effect') LIMIT 3",
                                    (_kf["id"],),
                                ).fetchall()
                                # Balance-aware clip: a blind p['value'][:60]
                                # can split mid-string-literal / mid-expression
                                # and ship the agent an unterminated guard
                                # (correct-or-quiet violation). clip_balanced
                                # returns a well-formed prefix or drops it.
                                _constraints = []
                                for p in _props:
                                    _cv = _core_clip_balanced(p["value"], 60)
                                    if _cv:
                                        _constraints.append(f"{p['kind']}: {_cv}")

                            _all_candidates.append({
                                "file": _bf,
                                "func": _kf["name"],
                                "label": _kf["label"],  # BUG-4 fix: pass real graph label
                                "sig": _kf["signature"] or "",
                                "line": _kf["start_line"] or 0,
                                "callers": _caller_count,
                                "sloc": _fn_sloc,
                                "fan_out": _fn_fan_out,
                                "constraints": _constraints,
                                "tier": "high" if _direct else ("medium" if _kw_overlap >= 2 else "low"),
                                "score": _score,
                            })

                    # Direct-name rescue: query ALL issue-named functions directly.
                    # Don't skip functions already in candidates — they may have been
                    # scored without _direct=True if they came from the per-file loop
                    # for a different file. The rescue ensures +1000 scoring.
                    if _issue_kws and _l1_issue_text:
                        _direct_names = [
                            w for w in re.findall(r"[A-Za-z_]\w{3,}", _l1_issue_text)
                            if w.lower() in _issue_kws
                            and w.lower() not in {"that","this","with","from","have","been","when","then","should","would","could","file","line","code","test","error","issue","none","true","false"}
                        ]
                        for _dn in _direct_names[:5]:
                            try:
                                _dn_rows = _l1_conn.execute(
                                    "SELECT id, name, label, file_path, signature, start_line, end_line FROM nodes "
                                    "WHERE name = ? AND is_test = 0 LIMIT 3",
                                    (_dn,),
                                ).fetchall()
                                for _dr in _dn_rows:
                                    _is_cls = _dr["label"] in ("Class", "Interface", "Struct")
                                    _dr_callers = _l1_conn.execute(
                                        "SELECT COUNT(*) FROM edges WHERE target_id = ? AND type = 'CALLS'",
                                        (_dr["id"],),
                                    ).fetchone()[0]
                                    _dr_sloc = max(0, (_dr["end_line"] or 0) - (_dr["start_line"] or 0))
                                    _dr_fan_out = _l1_conn.execute(
                                        "SELECT COUNT(*) FROM edges WHERE source_id = ?",
                                        (_dr["id"],),
                                    ).fetchone()[0]
                                    _all_candidates.append({
                                        "file": _dr["file_path"],
                                        "func": _dr["name"],
                                        "label": _dr["label"],
                                        "sig": _dr["signature"] or "",
                                        "line": _dr["start_line"] or 0,
                                        "callers": _dr_callers,
                                        "sloc": _dr_sloc,
                                        "fan_out": _dr_fan_out,
                                        "constraints": [],
                                        "tier": "high",
                                        "score": 200 if _is_cls else 1000 + min(_dr_callers, 5),
                                    })
                            except Exception:
                                pass

                    if _all_candidates:
                        _all_candidates.sort(key=lambda c: c["score"], reverse=True)
                        _edit_target = _all_candidates[0]
                        # Debug: show top 3 candidates with scores
                        for _dbg_i, _dbg_c in enumerate(_all_candidates[:3]):
                            print(f"[GT_META] edit_target_candidate_{_dbg_i}: func={_dbg_c['func']} score={_dbg_c['score']} file={_dbg_c['file']} callers={_dbg_c['callers']} tier={_dbg_c['tier']}", flush=True)

                    # Orientation: dynamic + hybrid + confidence-gated per
                    # .claude/CLAUDE.md "Three Mandatory Properties". Uses
                    # composite scoring (5 signals) + sigma-based dynamic
                    # tier boundaries + confidence-gated rendering.
                    _orientation_lines: list[str] = []
                    _contract_lines: list[str] = []
                    _orient_counts: dict[str, int] = {"verified": 0, "warning": 0, "info_suppressed": 0}

                    try:
                        from groundtruth.orientation.composite import (
                            composite_score as _comp_score,
                            signal_decomposition_tiers as _decomp_tiers,
                            render_orientation as _render_orient,
                        )

                        _scored: list[dict] = []
                        for _c in _all_candidates:
                            # BUG-4 fix: use real graph label (not score arithmetic)
                            _label = _c.get("label") or "Function"
                            _props_for_score = [
                                {"value": _ct} for _ct in (_c.get("constraints") or [])
                            ]
                            _s, _signals = _comp_score(
                                name=_c["func"],
                                label=_label,
                                file_path=_c.get("file", ""),
                                caller_count=int(_c.get("callers", 0) or 0),
                                properties=_props_for_score,
                                issue_text=_l1_issue_text or "",
                                issue_kws=_issue_kws,
                                function_sloc=int(_c.get("sloc", -1)),
                                function_fan_out=int(_c.get("fan_out", -1)),
                            )
                            _scored.append({
                                "func": _c["func"],
                                "file": _c.get("file", ""),
                                "callers": int(_c.get("callers", 0) or 0),
                                "label": _label,
                                "constraints": _c.get("constraints", []),
                                "composite": _s,
                                "signals": _signals,
                            })

                        # BUG-5 fix: sort by composite FIRST, then dedup by
                        # (func, file_path). The composite ordering picks the
                        # best variant of duplicated names instead of inheriting
                        # the OLD-score ordering.
                        _scored.sort(key=lambda x: x["composite"], reverse=True)
                        _seen_keys: set[tuple[str, str]] = set()
                        _scored_dedup: list[dict] = []
                        for _sc in _scored:
                            _key = (_sc["func"], _sc["file"])
                            if _key in _seen_keys:
                                continue
                            _seen_keys.add(_key)
                            _scored_dedup.append(_sc)

                        _scored_top = _scored_dedup[:10]
                        # Option B: signal-decomposition tiering. Tier comes
                        # from WHICH categorical signals fired per candidate,
                        # not from numeric composite total.
                        _tiers = _decomp_tiers([x["signals"] for x in _scored_top])
                        _orientation_lines, _orient_counts = _render_orient(_scored_top, _tiers)

                        # Edit target = top-scored candidate (for logging only)
                        if _scored_dedup:
                            _edit_target = {
                                "func": _scored_dedup[0]["func"],
                                "file": _scored_dedup[0]["file"],
                                "callers": _scored_dedup[0]["callers"],
                                "score": _scored_dedup[0]["composite"],
                                "tier": _tiers[0] if _tiers else "[INFO]",
                            }

                        # Per-task telemetry for verification
                        for _dbg_i, (_dbg_c, _dbg_t) in enumerate(zip(_scored_top[:5], _tiers[:5])):
                            print(
                                f"[GT_META] orient_candidate_{_dbg_i}: "
                                f"func={_dbg_c['func']} composite={_dbg_c['composite']:.3f} "
                                f"tier={_dbg_t} signals={_dbg_c['signals']} "
                                f"callers={_dbg_c['callers']}",
                                flush=True,
                            )
                        print(
                            f"[GT_META] orient_tiers: verified={_orient_counts['verified']} "
                            f"warning={_orient_counts['warning']} "
                            f"info_suppressed={_orient_counts['info_suppressed']}",
                            flush=True,
                        )

                        # Collect contracts from highest-tier candidate (VERIFIED or WARNING)
                        for _sc, _tier in zip(_scored_top, _tiers):
                            if _tier in ("[VERIFIED]", "[WARNING]") and _sc.get("constraints"):
                                for _con in _sc["constraints"][:2]:
                                    _contract_lines.append(f"  Preserve: {_con}")
                                break
                    except Exception as _orient_exc:
                        print(f"[GT_META] orient_composite_error: {_orient_exc}", flush=True)
                        # Fallback to honest note rather than misleading caller-count ranking
                        _orientation_lines = [
                            "Note: GT could not compute orientation. Use grep on "
                            "issue keywords to localize."
                        ]

                    # BUG-1 fix: empty _all_candidates path. When the brief
                    # files don't yield any candidates, emit honest fallback
                    # with the brief file paths so the agent still gets something.
                    if not _orientation_lines:
                        if _l1_brief_files:
                            _orientation_lines.append(
                                "Note: GT could not localize via symbol graph. "
                                "Brief files of interest: "
                                + ", ".join(_l1_brief_files[:3])
                            )
                        else:
                            _orientation_lines.append(
                                "Note: GT could not anchor any candidates. Use grep "
                                "on issue keywords to localize."
                            )
                finally:
                    _l1_conn.close()

                _l1_extra = ""
                if _orientation_lines:
                    _l1_extra = (
                        f"\n<gt-orientation>\n"
                        + "\n".join(_orientation_lines)
                        + f"\n</gt-orientation>"
                    )

                # BUG-002 fix: emit [GT KEY CONTRACTS] marker when contracts exist
                if _contract_lines:
                    _l1_extra += "\n[GT KEY CONTRACTS]\n" + "\n".join(_contract_lines)

                if _l1_extra:
                    brief = brief + _l1_extra
                    # BUG-2 fix: report orient_counts instead of dead _plan_lines
                    _et_log = (
                        f"edit_target={_edit_target['func']} tier={_edit_target.get('tier','?')}"
                        if _edit_target
                        else f"verified={_orient_counts['verified']} warning={_orient_counts['warning']} suppressed={_orient_counts['info_suppressed']}"
                    )
                    print(f"[GT_META] l1_enhanced: {_et_log} contracts={len(_contract_lines)}", flush=True)
                # L1-INV-1 consensus bridge: add issue-symbol files to
                # brief_candidates so consensus recognizes them when viewed.
                _rt = (
                    getattr(instance, "_gt_runtime", None)
                    or (instance.get("_gt_runtime") if isinstance(instance, dict) else None)
                )
                _cfg = getattr(_rt, "_gt_full_config", None) if _rt else None
                _ws = getattr(instance, "instance_id", "") or getattr(instance, "id", "") or ""
                if _issue_symbol_files and _cfg and hasattr(_cfg, 'brief_candidates'):
                    for _isf in _issue_symbol_files:
                        _cfg.brief_candidates.add(_isf)
                        if _ws and not _isf.startswith(_ws):
                            _cfg.brief_candidates.add(f"{_ws}/{_isf}")
            except Exception as _l1_exc:
                print(f"[GT_META] l1_enhance_error: {_l1_exc}", flush=True)

    if brief:
        # Demo injection: show one gt_query example from the L4 prefetch output
        _demo = ""
        _prefetch = getattr(instance, "gt_brief", "") or ""
        if "gt_query:" in _prefetch or "# gt_query:" in _prefetch:
            import re as _re_demo
            _dm = _re_demo.search(r"(# gt_query:.*?)(?=\n# gt_query:|\Z)", _prefetch, _re_demo.DOTALL)
            if _dm:
                _demo_text = _dm.group(1).strip()[:300]
                _demo = (
                    "\n<gt-demo>\n"
                    "Example: running `gt_query` from bash produces output like this:\n"
                    f"$ gt_query {_demo_text.split('@')[0].split(':')[-1].strip()[:30] if '@' in _demo_text else 'symbol'}\n"
                    f"{_demo_text}\n"
                    "</gt-demo>\n"
                )
        # Exactly ONE <gt-task-brief> wrapper. generate_task_brief already returns the
        # v1r brief wrapped in <gt-task-brief>...</gt-task-brief> (+ sibling <gt-graph-map>
        # / <gt-orientation>); wrapping it again produced NESTED <gt-task-brief> tags in
        # the agent's instruction (2026-05-29, haystack-8525). Wrap only if not already.
        # C1 boundary (B3/B3b): route the agent-facing brief through the Safe
        # Renderer so semantic-nonsense / empty contract fields never reach the
        # agent. Structure (<gt-task-brief>/<gt-graph-map>/...) is preserved.
        _wrapped = _core_sanitize_block(brief.strip())
        if _wrapped:
            if _wrapped.startswith("<gt-task-brief>"):
                content = f"{_wrapped}\n\n{tools_hint}\n{_demo}\n" + content
            else:
                content = f"<gt-task-brief>\n{_wrapped}\n</gt-task-brief>\n\n{tools_hint}\n{_demo}\n" + content
        # Log L1 brief injection — use full untruncated brief for logging
        brief_full_for_log = (
            getattr(instance, "gt_brief_full", "")
            or (instance.get("gt_brief_full", "") if isinstance(instance, dict) else "")
            or brief
        )
        runtime = (
            getattr(instance, "_gt_runtime", None)
            or (instance.get("_gt_runtime") if isinstance(instance, dict) else None)
        )
        config = getattr(runtime, "_gt_full_config", None) if runtime else None
        if config:
            print(f"[GT_META] L1 brief injected ({len(brief_full_for_log)} chars)", flush=True)

            # Structured L1 event (emit first to get event_id)
            # Parse L1 candidates from rendered brief text (container-safe — no file boundary)
            l1_items: list[dict] = []
            try:
                _FILE_RE = re.compile(r"^\d+\.\s+(\S+\.(?:py|go|js|ts|rs|java|rb|php))")
                for _bline in brief_full_for_log.splitlines():
                    _fm = _FILE_RE.match(_bline.strip())
                    if _fm:
                        l1_items.append({
                            "kind": "l1_candidate",
                            "file_path": _fm.group(1),
                            "confidence": 0.0,
                            "source": "graph_db",
                            "reason": "V1R candidate",
                        })
            except Exception:
                pass
            l1_eid = _emit_structured_event(
                config, "L1", "localization_brief",
                rendered_text=brief_full_for_log,
                evidence_items=l1_items,
            )
            _log_gt_interaction(
                config, "L1", "brief", "brief_injection", brief_full_for_log,
                agent_action_before="", event_id=l1_eid or "",
            )
            # Belief events for each L1 candidate
            for item in l1_items:
                _emit_belief_event(
                    config,
                    file_path=item.get("file_path", ""),
                    new_status="candidate",
                    reason=f"L1 candidate: {item.get('reason', '')}",
                    source_event_id=l1_eid or "",
                    score=item.get("confidence"),
                )
    elif tools_hint:
        content = f"{tools_hint}\n" + content
    tools_installed = getattr(instance, "gt_l4_tools", None)
    if tools_installed is None and isinstance(instance, dict):
        tools_installed = instance.get("gt_l4_tools")
    try:
        msg.content = content + render_l4_tool_footer(list(tools_installed or []))
    except Exception:
        pass
    return msg


def _parse_condenser_config(
    condenser_name: str | None,
    get_condenser_config_arg: Any,
    NoOpCondenserConfig: Any,
) -> Any:
    """Parse EVAL_CONDENSER env var into a condenser config object.

    Supports extended format: ``recent_events:keep_first=5,max_events=15``
    which OH's ``get_condenser_config_arg`` may not handle natively.
    Falls back to ``get_condenser_config_arg`` for simple formats.
    """
    if not condenser_name:
        return NoOpCondenserConfig() if NoOpCondenserConfig else None

    # Extended format: "recent_events:key=val,key=val"
    if ":" in condenser_name and "=" in condenser_name:
        ctype, params_str = condenser_name.split(":", 1)
        params: dict[str, Any] = {}
        for part in params_str.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                try:
                    params[k.strip()] = int(v.strip())
                except ValueError:
                    params[k.strip()] = v.strip()
        try:
            if ctype == "recent_events":
                from openhands.core.config.condenser_config import RecentEventsCondenserConfig
                return RecentEventsCondenserConfig(**params)
        except (ImportError, TypeError) as exc:
            print(f"[GT_META] condenser extended parse failed ({exc}), falling back", flush=True)

    # Simple format: "recent_events:5" or "noop"
    if get_condenser_config_arg:
        return get_condenser_config_arg(condenser_name)
    return NoOpCondenserConfig() if NoOpCondenserConfig else None


def patch_run_infer(ri_module: Any) -> None:
    """Patch the OpenHands SWE-bench run_infer module."""

    global _ORIG_INITIALIZE_RUNTIME, _ORIG_GET_INSTRUCTION
    _ORIG_INITIALIZE_RUNTIME = ri_module.initialize_runtime
    _ORIG_GET_INSTRUCTION = ri_module.get_instruction
    ri_module.initialize_runtime = patched_initialize_runtime
    ri_module.get_instruction = patched_get_instruction


def run_openhands_fork_main(ri_module: Any, argv: list[str]) -> None:
    """Run v0.54-style OpenHands SWE-bench module after monkey-patching."""

    sys.argv = ["run_infer.py"] + argv

    import openhands.agenthub  # noqa: F401
    from datasets import load_dataset
    from evaluation.utils.shared import make_metadata, prepare_dataset, run_evaluation
    from openhands.core.config import get_llm_config_arg
    try:
        from openhands.core.config import get_evaluation_parser
    except ImportError:
        from openhands.core.config import get_parser as get_evaluation_parser
    try:
        from openhands.core.config.condenser_config import NoOpCondenserConfig
    except ImportError:
        NoOpCondenserConfig = None
    try:
        from openhands.core.config.utils import get_condenser_config_arg
    except ImportError:
        get_condenser_config_arg = None

    parser = get_evaluation_parser()
    parser.add_argument("--dataset", type=str, default="princeton-nlp/SWE-bench")
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--mode", type=str, default="swe", choices=["swe", "swt", "swt-ci"])
    args, _ = parser.parse_known_args()
    print(f"OH_GT_FULL_ARGS argv={argv!r} eval_output_dir={args.eval_output_dir!r}", flush=True)
    if args.eval_output_dir is None and os.environ.get("OUT_ROOT"):
        args.eval_output_dir = os.environ["OUT_ROOT"]
        print(f"OH_GT_FULL_ARGS fallback eval_output_dir={args.eval_output_dir!r}", flush=True)
    if getattr(args, "agent_cls", None) is None:
        args.agent_cls = "CodeActAgent"
        print("OH_GT_FULL_ARGS fallback agent_cls='CodeActAgent'", flush=True)

    dataset = load_dataset(args.dataset, split=args.split)
    ri_module.set_dataset_type(args.dataset)
    tests = ri_module.filter_dataset(dataset.to_pandas(), "instance_id")

    llm_config = get_llm_config_arg(args.llm_config) if args.llm_config else None
    if llm_config is None:
        # OH 0.54 compat: get_llm_config_arg returns None when AppConfig
        # isn't pre-loaded (ri.main() missing). Fall back to reading the
        # TOML directly — same config the GHA workflow writes.
        import toml as _toml_cfg
        from openhands.core.config import LLMConfig
        _eval_cfg_path = str(Path(ri_module.__file__).resolve().parent / "config.toml")
        for _cfg_candidate in ["/tmp/config.toml", _eval_cfg_path]:
            if os.path.exists(_cfg_candidate):
                _raw = _toml_cfg.load(_cfg_candidate)
                _sec = _raw.get("llm", {}).get(args.llm_config or "eval", {})
                if _sec.get("model"):
                    _model = _sec.pop("model")
                    _skip = {"vertex_project", "vertex_location"}
                    llm_config = LLMConfig(
                        model=_model,
                        **{k: v for k, v in _sec.items() if k not in _skip},
                    )
                    print(f"[GT_META] LLM config from {_cfg_candidate}: model={_model}", flush=True)
                    break
        if llm_config is None:
            raise ValueError(f"Missing or unknown llm_config: {args.llm_config}")
    llm_config.log_completions = True
    llm_config.modify_params = False
    if hasattr(llm_config, "reasoning_effort"):
        llm_config.reasoning_effort = None
    if hasattr(llm_config, "enable_thinking"):
        llm_config.enable_thinking = False

    condenser_name = os.environ.get("EVAL_CONDENSER")
    condenser_config = _parse_condenser_config(condenser_name, get_condenser_config_arg, NoOpCondenserConfig)
    dataset_description = args.dataset.replace("/", "__") + "-" + args.split.replace("/", "__")
    metadata = make_metadata(
        llm_config,
        dataset_description,
        args.agent_cls,
        args.max_iterations,
        args.eval_note,
        args.eval_output_dir,
        details={"mode": args.mode},
        condenser_config=condenser_config,
    )
    output_file = os.path.join(metadata.eval_output_dir, "output.jsonl")
    instances = prepare_dataset(tests, output_file, args.eval_n_limit)
    run_evaluation(
        instances,
        metadata,
        output_file,
        args.eval_num_workers,
        ri_module.process_instance,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenHands GT full-potential wrapper")
    parser.add_argument("--instance-ids", default="")
    args, remainder = parser.parse_known_args()

    try:
        from evaluation.benchmarks.swe_bench import run_infer as ri  # type: ignore[import]
    except ImportError as exc:
        print(f"FATAL: cannot import OpenHands run_infer: {exc}", file=sys.stderr)
        sys.exit(1)

    patch_run_infer(ri)

    # DIAGNOSTIC: log OH LLMConfig after __post_init__ to capture reasoning_effort etc.
    try:
        from openhands.llm.llm import LLM
        _orig_llm_init = LLM.__init__
        def _logged_llm_init(self, config, *a, **kw):
            _orig_llm_init(self, config, *a, **kw)
            cfg = self.config
            print(f"[GT_LLM_CONFIG] model={cfg.model} base_url={cfg.base_url} "
                  f"custom_llm_provider={getattr(cfg, 'custom_llm_provider', 'NONE')} "
                  f"temperature={cfg.temperature} top_p={getattr(cfg, 'top_p', 'NONE')} "
                  f"max_output_tokens={cfg.max_output_tokens} "
                  f"drop_params={getattr(cfg, 'drop_params', 'NONE')} "
                  f"modify_params={getattr(cfg, 'modify_params', 'NONE')} "
                  f"reasoning_effort={getattr(cfg, 'reasoning_effort', 'NONE')} "
                  f"enable_thinking={getattr(cfg, 'enable_thinking', 'NONE')} "
                  f"caching_prompt={getattr(cfg, 'caching_prompt', 'NONE')}", flush=True)
            # SAFETY: warn if caching_prompt=true with DeepSeek (corrupts completions)
            _cp = getattr(cfg, 'caching_prompt', None)
            _mn = getattr(cfg, 'model', '') or ''
            if "deepseek" in _mn.lower() and _cp:
                print("[GT_META] WARNING: caching_prompt=true with DeepSeek — this corrupts completions. Set caching_prompt=false.", flush=True)
        LLM.__init__ = _logged_llm_init
    except Exception as e:
        print(f"[GT_LLM_CONFIG] patch failed: {e}", flush=True)

    if args.instance_ids:
        ids = [s.strip() for s in args.instance_ids.split(",") if s.strip()]
        config_path = Path(ri.__file__).resolve().parent / "config.toml"
        # Merge: preserve [llm.*] sections from /tmp/config.toml + add selected_ids
        toml_content = ""
        for candidate in ["/tmp/config.toml", str(config_path)]:
            if os.path.exists(candidate):
                toml_content = Path(candidate).read_text(encoding="utf-8")
                break
        import re as _re_toml
        # Remove any existing selected_ids line
        toml_content = _re_toml.sub(r"selected_ids\s*=.*\n?", "", toml_content)
        # PREPEND selected_ids before any [section] header so TOML parser
        # puts it in the root namespace (not under [sandbox] or [llm])
        selected_line = "selected_ids = " + repr(ids) + "\n\n"
        toml_content = selected_line + toml_content
        config_path.write_text(toml_content, encoding="utf-8")
        print(f"[GT_META] selected_ids={ids} prepended to {config_path}", flush=True)

    sys.argv = ["run_infer.py"] + remainder
    if hasattr(ri, "main"):
        ri.main()
        return
    run_openhands_fork_main(ri, remainder)


if __name__ == "__main__":
    main()
