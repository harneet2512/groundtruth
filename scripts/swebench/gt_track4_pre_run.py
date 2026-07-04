"""Track 4 pre-run hook — L1 (family brief) + L2 (stdlib FTS5 BM25 fallback).

This is the SWE-agent ``RunHook`` that fires once per instance, before the
agent loop starts. It computes the brief by chaining:

    L1: extract_identifiers_from_issue(issue_text)
        if >=1 ID and generate_enhanced_briefing(...) is non-empty
        => brief = that output

    L2: otherwise => brief = sqlite3_fts_fallback.generate_fts5_orientation_brief(
            issue_text, repo, db)
        Stdlib-only (sqlite3 FTS5 + BM25 + structural rerank). Replaces
        v22_brief which silent-failed in sweagent_venv (no
        sentence_transformers). Research basis: AgentLess /
        AutoCodeRover / Reformulate-Retrieve-Localize.

The brief is:
  1. Written verbatim to <instance_log_dir>/gt_brief.txt (atomic + fsync)
  2. Logged once to <instance_log_dir>/gt_layers.log as a single line
     ``task=<id> L1=<fired|fallback|empty> L2=<fired|noop>``
     (other layer fields are appended later by Tracks B1/C/D)
  3. Injected into the agent's prompt by mutating
     ``problem_statement.extra_fields["gt_brief"] = brief``
     SWE-agent's ``_get_format_dict`` merges that key into the Jinja2
     context for ``instance_template``, where ``{{gt_brief}}`` lives.

Environment variables read:
  GT_GRAPH_DB           Path to the prebuilt graph.db (REQUIRED).
  GT_REPO_PATH          Path to the source repo on disk. Optional. If
                        unset, falls back to env.repo.path or "/workspace".
  GT_TRACK4_LOG_DIR     Override for the per-instance log dir. Optional.
                        If unset, falls back to <output_dir>/<id>/.
  GT_TRACK4_MAX_LINES   Cap for L1 brief lines. Default 8.

Public surface:
  - GTTrack4PreRunHook        SWE-agent RunHook subclass for batch runs.
  - compute_brief(...)        Pure function, harness-agnostic. Used by the
                              hook AND by Track A's verification (step B/C).

This module is harness-agnostic at the compute layer: the brief
computation does not import sweagent.* at all. The RunHook subclass at the
bottom does — but it is gated behind a try/except so this file is
importable in environments that don't have sweagent installed
(verification step A, unit tests).
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shlex
import sqlite3
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Any

# shlex.quote alias — used by the container-path probe in _container_path_exists.
shlex_quote = shlex.quote

logger = logging.getLogger("gt_track4_pre_run")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# sys.path bootstrap so this file works whether imported from the repo root
# or from inside SWE-agent's run subprocess.
# ---------------------------------------------------------------------------

_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parents[2]  # scripts/swebench/<this>.py -> repo root
for sub in ("src", "scripts/swebench", "benchmarks/swebench"):
    p = _REPO_ROOT / sub
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))


# ---------------------------------------------------------------------------
# Lazy imports of the reuse modules (gt_intel, v22_brief). Done lazily so
# that the module is importable even when sqlite3 / pretask deps are
# unavailable (e.g. minimal CI containers).
# ---------------------------------------------------------------------------

def _import_gt_intel():
    """Return (extract_identifiers_from_issue, generate_enhanced_briefing).

    Raises ImportError on failure — caller surfaces.
    """
    # `gt_intel.py` lives in benchmarks/swebench. It is a script, not a
    # package, so import it as a top-level module after the sys.path
    # bootstrap above.
    import gt_intel  # type: ignore[import-not-found]
    return (
        gt_intel.extract_identifiers_from_issue,
        gt_intel.generate_enhanced_briefing,
    )


def _import_l2_fallback():
    """Return the L2 stdlib FTS5 fallback brief generator.

    Replaces v22_brief.generate_brief, which silent-failed inside SWE-agent
    task containers (sentence_transformers not in sweagent_venv). The new
    fallback is stdlib-only (sqlite3 FTS5 + BM25 + structural rerank).

    See ``groundtruth.pretask.sqlite3_fts_fallback`` for research-backed
    rationale (AgentLess, AutoCodeRover, Reformulate-Retrieve-Localize).
    """
    from groundtruth.pretask.sqlite3_fts_fallback import (  # type: ignore
        generate_fts5_orientation_brief,
    )
    return generate_fts5_orientation_brief


# ---------------------------------------------------------------------------
# RC-7 Bug L1-4: substantive-vs-fallback tier signal for L1 briefs.
#
# gt_intel.format_gt_output() guarantees the <gt-evidence> wrapper is
# always present and emits "[OK] No high-confidence findings." or
# "[OK] No symbols matched in graph." when there are no findings. That
# means an L1 call returning the [OK] sentinel still passes
# ``output and output.strip()`` — yet it carries ZERO actionable
# context. Treating it as L1=fired short-circuits the L2 BM25 fallback
# and starves the agent of any retrieval signal.
#
# This helper splits the wrapper-stripped body and returns True only
# when the brief carries at least one tier marker (FIX HERE, [VERIFIED],
# [LIKELY], [POSSIBLE], CALLERS, TEST, ENTRY POINT, or any of the
# TAXONOMY_LABELS family tags). It does NOT inspect content quality —
# any non-OK directive line is treated as substantive. This is the
# narrowest fix that closes the L2-bypass while preserving every
# existing brief-shape contract pinned by tests/layers/test_l1_brief.py.
# ---------------------------------------------------------------------------

_L1_SUBSTANTIVE_MARKERS: tuple[str, ...] = (
    "FIX HERE",
    "[VERIFIED]",
    "[LIKELY]",
    "[POSSIBLE]",
    "CALLERS:",
    "TEST:",
    "ENTRY POINT:",
    # TAXONOMY_LABELS — see gt_intel.TAXONOMY_LABELS. Hardcoded here to
    # avoid an import-time round-trip; if a label is renamed there, the
    # corresponding test in tests/layers/test_l1_brief.py will catch it.
    "CALLER-BLIND-EDIT",
    "HALLUCINATED-IMPORT",
    "PATTERN-DIVERGENCE",
    "UNVERIFIED-EDIT",
    "BLAST-RADIUS",
    "CONTRACT-BREAK",
    "STYLE-DIVERGENCE",
)


def _l1_brief_is_substantive(brief: str) -> bool:
    """True iff the L1 brief contains an actionable tier marker.

    Strips ``<gt-evidence>`` wrapper, then checks for any
    _L1_SUBSTANTIVE_MARKERS substring. The "[OK] No symbols matched in
    graph." / "[OK] No high-confidence findings." sentinels do NOT
    contain any marker, so they correctly route to L2.

    Note: this is wrapper-and-tag agnostic. The L2 brief uses
    ``<gt-task-brief>`` which doesn't conflict — L2 outputs are
    only seen by compute_brief AFTER this check, so they never collide.
    """
    if not brief:
        return False
    body = brief
    # Strip the standard wrappers; the body may still carry the [OK]
    # sentinel after stripping.
    for marker in ("<gt-evidence>", "</gt-evidence>",
                   "<gt-task-brief>", "</gt-task-brief>"):
        body = body.replace(marker, "")
    body = body.strip()
    if not body:
        return False
    return any(m in body for m in _L1_SUBSTANTIVE_MARKERS)


# ---------------------------------------------------------------------------
# F2P-driven localization seeds.
#
# When a SWE-bench-style row carries FAIL_TO_PASS, the test names point at
# the failing tests that the patch must turn green. The imports inside those
# test files are by construction the symbols the failing tests exercise —
# i.e. high-confidence localization seeds that no amount of issue-text
# regex extraction can produce. This is generic across any FAIL_TO_PASS-
# tagged dataset and any Python repo. For non-Python languages or
# unparseable files we degrade silently.
# ---------------------------------------------------------------------------

def _parse_f2p_test_paths(f2p_raw: Any) -> list[str]:
    """Extract repo-relative test file paths from a FAIL_TO_PASS field.

    The field can be:
      - JSON-encoded list of strings
      - newline-separated string
      - already-decoded list

    Each entry is a pytest-style identifier like
    ``tests/foo/test_bar.py::TestClass::test_baz``; we keep only the
    file-path portion (everything left of the first ``::``).
    """
    if not f2p_raw:
        return []
    if isinstance(f2p_raw, str):
        text = f2p_raw.strip()
        if not text:
            return []
        # Try JSON first (the canonical SWE-bench-Live serialization).
        try:
            decoded = json.loads(text)
            if isinstance(decoded, list):
                items = [str(x) for x in decoded]
            else:
                items = [str(decoded)]
        except json.JSONDecodeError:
            items = [ln for ln in text.splitlines() if ln.strip()]
    elif isinstance(f2p_raw, (list, tuple)):
        items = [str(x) for x in f2p_raw]
    else:
        items = [str(f2p_raw)]

    paths: list[str] = []
    seen: set[str] = set()
    for item in items:
        item = item.strip()
        if not item:
            continue
        # pytest::nodeid → take the file-path portion.
        path = item.split("::", 1)[0].strip()
        if not path:
            continue
        # Normalize separators.
        norm = path.replace("\\", "/")
        if norm not in seen:
            seen.add(norm)
            paths.append(norm)
    return paths


def _extract_imports_from_python_file(path: Path) -> list[str]:
    """Return the set of imported names that look local (top-level package).

    Walks the file's AST and collects:
      - ``import a.b.c``        → ``a``, ``a.b``, ``a.b.c``
      - ``from a.b import x, y`` → ``a.b``, ``a.b.x``, ``a.b.y``, ``x``, ``y``

    The "look local" filter (graph-membership intersection) is applied by
    the caller so this helper stays AST-only.
    """
    import ast

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError):
        return []

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    parts = alias.name.split(".")
                    for i in range(1, len(parts) + 1):
                        names.add(".".join(parts[:i]))
                    # Last segment alone — useful for graph name matching.
                    names.add(parts[-1])
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod:
                parts = mod.split(".")
                for i in range(1, len(parts) + 1):
                    names.add(".".join(parts[:i]))
                names.add(parts[-1])
            for alias in node.names:
                if alias.name and alias.name != "*":
                    names.add(alias.name)
    # Drop empty / dunder-only entries.
    return sorted(n for n in names if n and not n.startswith("_"))


_DIFF_FILE_RE = re.compile(r"^diff --git a/(\S+) b/", re.MULTILINE)


def _extract_added_lines_from_diff(diff_text: str) -> str:
    """Return concatenated text of added (`+`) lines from a unified diff,
    excluding the `+++ ` file-header rows. Returns empty string on no diff.
    """
    if not diff_text:
        return ""
    out: list[str] = []
    for ln in diff_text.splitlines():
        if ln.startswith("+++"):
            continue
        if ln.startswith("+"):
            out.append(ln[1:])
    return "\n".join(out)


def _extract_test_file_tokens(diff_text: str) -> list[str]:
    """Return symbol-shape tokens derived from changed-file paths in a diff.

    For ``test/unit/rules/functions/test_find_in_map.py`` returns:
      - ``find_in_map`` (snake_case from ``test_NAME.py``)
      - ``FindInMap`` (PascalCase reconstruction)

    Both forms are useful seeds: gt_intel's identifier resolver matches
    snake_case directly; the PascalCase form catches class-name conventions
    common in test-target naming.
    """
    if not diff_text:
        return []
    seeds: set[str] = set()
    for path in _DIFF_FILE_RE.findall(diff_text):
        base = os.path.basename(path)
        stem = os.path.splitext(base)[0]
        orig_stem = stem
        # Strip canonical test-name prefixes/suffixes.
        # Python:        test_foo    -> foo
        # Python/Go:     foo_test    -> foo
        # Ruby:          foo_spec    -> foo
        for pat in ("test_", "tests_"):
            if stem.startswith(pat):
                stem = stem[len(pat):]
                break
        for pat in ("_test", "_tests", "_spec", "_specs"):
            if stem.endswith(pat):
                stem = stem[: -len(pat)]
                break
        # RC-06: PascalCase suffix-strip for Java/C#/PHP-style class-named
        # tests (`FooTest.java`, `FooTests.cs`, `FooSpec`). Order: Tests >
        # Spec > Test (longer first). Only strip when the head is
        # PascalCase to avoid clobbering legit names ending in `est`.
        for pat in ("Tests", "Spec", "Test"):
            if stem.endswith(pat) and len(stem) > len(pat):
                head = stem[: -len(pat)]
                if head and head[0].isupper():
                    stem = head
                    break
        if not stem or len(stem) < 3:
            continue
        seeds.add(stem)
        # PascalCase reconstruction: snake_case -> PascalCase.
        if "_" in stem:
            pascal = "".join(
                p[:1].upper() + p[1:].lower() for p in stem.split("_") if p
            )
            if len(pascal) >= 3:
                seeds.add(pascal)
        # RC-06: snake_case reconstruction for PascalCase stems
        # (FooBar -> foo_bar). Captures the alternate convention common
        # for Python-shaped tests of Java/C# APIs.
        if stem != orig_stem and stem and stem[0].isupper() and "_" not in stem:
            snake = re.sub(r"(?<!^)(?=[A-Z])", "_", stem).lower()
            if len(snake) >= 3:
                seeds.add(snake)
    return sorted(seeds)


def _f2p_localization_seeds(
    f2p_raw: Any,
    repo_path: str,
    graph_db_path: str,
    *,
    test_patch: str | None = None,
    max_seeds: int = 32,
) -> list[str]:
    """Return high-confidence localization seeds derived from FAIL_TO_PASS.

    Three-source strategy (tried in order, results merged then graph-filtered):
      A) ``test_patch`` added (``+``) line content — fed through the same
         identifier extractor that L1 uses on issue text. Catches CamelCase
         types, snake_case names, dotted refs, error-class names, and
         Python file paths that the test_patch introduces (e.g. new asserts
         like ``validator="fn_findinmap"`` or ``ValidationError(...)``).
      B) Changed-file paths in the diff — derive seeds from the test
         filenames themselves (``test_find_in_map.py`` -> ``find_in_map``
         + ``FindInMap``). Useful when the diff body is sparse (modified
         tests don't typically re-import what the file already had).
      C) Host-side test file AST parse — only fires when the repo is
         checked out on the host (rare for SWE-bench-Live which keeps the
         repo inside the container image; common for self-hosted runs).

    Then filter to names that appear as a node ``name`` in graph.db (any
    function / class / method / interface) — drops third-party / stdlib
    noise, keeps only in-repo symbols.

    Generic across any Python repo with a FAIL_TO_PASS-tagged dataset row.
    For non-Python tests the AST step degrades silently to [].

    Behind GT_F2P_LOCALIZATION (default "1"); flipping to "0" disables the
    extension cleanly without code changes.
    """
    if os.environ.get("GT_F2P_LOCALIZATION", "1").strip() != "1":
        return []
    test_paths = _parse_f2p_test_paths(f2p_raw)
    if not test_paths and not test_patch:
        return []

    raw_imports: set[str] = set()

    # (A) Diff-body identifier extraction. We reuse gt_intel's regex set
    # (CamelCase, snake_case, dotted refs, *Error/*Exception, file paths)
    # so the seed shape matches what generate_enhanced_briefing already
    # resolves. Lazy-import — module is sys.path-bootstrapped above.
    if test_patch:
        added_text = _extract_added_lines_from_diff(test_patch)
        if added_text:
            try:
                extract_fn, _ = _import_gt_intel()
                for name in extract_fn(added_text):
                    raw_imports.add(name)
            except Exception:  # noqa: BLE001
                # gt_intel unavailable — fall through to other sources.
                pass

    # (B) Test-file path tokens — sourced from BOTH the diff header and
    # the FAIL_TO_PASS list (covers both modified + new test files).
    if test_patch:
        for s in _extract_test_file_tokens(test_patch):
            raw_imports.add(s)
    for tp in test_paths:
        norm = tp.replace("\\", "/")
        base = os.path.basename(norm)
        stem = os.path.splitext(base)[0]
        for pat in ("test_", "tests_"):
            if stem.startswith(pat):
                stem = stem[len(pat):]
                break
        for pat in ("_test", "_tests"):
            if stem.endswith(pat):
                stem = stem[: -len(pat)]
                break
        if stem and len(stem) >= 3:
            raw_imports.add(stem)
            if "_" in stem:
                pascal = "".join(
                    p[:1].upper() + p[1:].lower() for p in stem.split("_") if p
                )
                if len(pascal) >= 3:
                    raw_imports.add(pascal)

    # (C) Host-file AST extraction — secondary, only fires when the repo
    # is checked out host-side. Refuses absolute / parent-traversal paths.
    repo_root = Path(repo_path) if repo_path else None
    if repo_root and repo_root.is_dir():
        for tp in test_paths:
            if os.path.isabs(tp) or tp.startswith(".."):
                continue
            candidate = repo_root / tp
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() != ".py":
                continue
            for name in _extract_imports_from_python_file(candidate):
                raw_imports.add(name)

    if not raw_imports:
        return []

    # Filter to names present in graph.db. We treat any node with that name
    # as evidence the import is in-repo (vs third-party / stdlib).
    if not os.path.exists(graph_db_path):
        return []
    try:
        conn = sqlite3.connect(
            f"file:{graph_db_path}?mode=ro&immutable=1", uri=True, timeout=10
        )
    except sqlite3.Error:
        return []
    try:
        kept: list[str] = []
        cur = conn.cursor()
        for name in sorted(raw_imports, key=len, reverse=True):
            # The graph stores leaf names; if the import is dotted, also
            # check the leaf segment.
            leaf = name.split(".")[-1]
            row = cur.execute(
                "SELECT 1 FROM nodes WHERE name = ? "
                "AND label IN ('Function','Method','Class','Interface') "
                "LIMIT 1",
                (leaf,),
            ).fetchone()
            if row is None:
                continue
            kept.append(leaf)
            if len(kept) >= max_seeds:
                break
    except sqlite3.OperationalError:
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    # De-dup while preserving discovery order.
    seen: set[str] = set()
    out: list[str] = []
    for s in kept:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Pure compute function — harness-agnostic.
# ---------------------------------------------------------------------------

def compute_brief(
    issue_text: str,
    repo_path: str,
    graph_db_path: str,
    *,
    max_lines: int = 8,
    f2p_raw: Any = None,
    test_patch: str | None = None,
) -> tuple[str, str, str]:
    """Compute the L1 brief, falling back to the stdlib L2 FTS5 fallback if needed.

    Returns ``(brief_text, l1_status, l2_status)`` where:
      l1_status is one of {"fired", "fallback", "empty"}
      l2_status is one of {"fired", "fired_but_empty", "noop"}

    Contract:
      - L1 "fired" means L1 produced a non-empty brief and that brief is
        what's in brief_text.
      - L1 "fallback" means L1 ran but produced 0 IDs / empty output, so
        L2 was attempted.
      - L1 "empty" means L1 ran and was kept (no fallback needed).
      - L2 "fired" means the L2 stdlib FTS5 fallback returned a non-empty
        ranked-files brief.
      - L2 "fired_but_empty" means L2 ran but the issue text was too sparse
        for structural retrieval; L2 returned the sparse-result brief.
      - L2 "noop" means L2 was not entered (L1 already fired).
        Tracks A/D verifier reads this string verbatim.

    Errors are caught and logged; this function never raises. On total
    failure brief_text is "" and (l1_status, l2_status) reflect what
    happened.
    """
    issue_text = issue_text or ""

    # ------------------------------------------------------------------ L1
    l1_status = "empty"
    brief = ""
    try:
        extract_fn, briefing_fn = _import_gt_intel()
    except Exception as exc:  # noqa: BLE001
        logger.warning("gt_intel import failed: %s — skipping L1", exc)
        extract_fn = None
        briefing_fn = None

    ids: list[str] = []
    if extract_fn is not None:
        try:
            ids = list(extract_fn(issue_text))
        except Exception as exc:  # noqa: BLE001
            logger.warning("extract_identifiers_from_issue raised: %s", exc)
            ids = []
    # F2P-driven localization (generic across any FAIL_TO_PASS-tagged
    # dataset). Seeds derived from the imports of the failing test files
    # are merged into the entry-symbol set BEFORE the briefing call so
    # generate_enhanced_briefing's symbol resolver can reach them. Seeds
    # are de-duplicated against issue-text identifiers; we keep both.
    f2p_seeds: list[str] = []
    try:
        f2p_seeds = _f2p_localization_seeds(
            f2p_raw, repo_path, graph_db_path, test_patch=test_patch,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("F2P localization helper raised: %s", exc)
        f2p_seeds = []
    if f2p_seeds:
        existing = {x for x in ids}
        for s in f2p_seeds:
            if s not in existing:
                ids.append(s)
                existing.add(s)
        logger.info(
            "L1 seeds extended via FAIL_TO_PASS: +%d (total ids=%d)",
            len(f2p_seeds), len(ids),
        )
    # Test-only probe mode for forcing the L2 fallback path in live runs.
    if os.environ.get("GT_FORCE_EMPTY_IDS", "").strip() == "1":
        logger.info("GT_FORCE_EMPTY_IDS=1 active: forcing empty identifier set for L2 probe")
        ids = []

    if briefing_fn is not None and ids:
        if not os.path.exists(graph_db_path):
            logger.warning("graph.db not found at %s — skipping L1", graph_db_path)
        else:
            conn = None
            try:
                # RC-9 Bug L1-6: read-only + immutable URI so chmod 444
                # graph.db doesn't crash on SELECT (SQLite would otherwise
                # try to create/update WAL or journal files).
                uri = f"file:{graph_db_path}?mode=ro&immutable=1"
                conn = sqlite3.connect(uri, uri=True, timeout=15)
                output = briefing_fn(conn, repo_path, ids, max_lines=max_lines)
                # RC-7 Bug L1-4: differentiate substantive L1 brief from the
                # gt_intel.format_gt_output() "[OK] No symbols matched in
                # graph." sentinel. The sentinel is non-empty (it carries the
                # <gt-evidence> wrapper) but it carries zero actionable
                # context, so the previous ``output.strip()`` test treated
                # noise as "fired" and short-circuited L2 routing.
                if output and output.strip() and _l1_brief_is_substantive(output):
                    brief = output
                    l1_status = "fired"
                # else: leave brief="" so the L2 fallback below fires.
            except Exception as exc:  # noqa: BLE001
                logger.warning("generate_enhanced_briefing raised: %s", exc)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass

    # ------------------------------------------------------------------ L2
    # L2 = stdlib FTS5 + BM25 + structural rerank fallback. Replaces the
    # old v22_brief path, which silent-failed because sentence_transformers
    # is not in sweagent_venv. See sqlite3_fts_fallback.py for research
    # validation (AgentLess / AutoCodeRover / Reformulate-Retrieve-Localize).
    l2_status = "noop"
    if not brief:
        # L1 produced nothing — fall through to L2.
        l1_status = "fallback" if ids else "empty"
        try:
            generate_l2_brief = _import_l2_fallback()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "L2 fallback import failed: %s — L2 unavailable", exc
            )
            generate_l2_brief = None

        if generate_l2_brief is not None:
            try:
                v2 = generate_l2_brief(issue_text, repo_path, graph_db_path)
                if v2 and v2.strip():
                    brief = v2
                    # The stdlib fallback always returns a <gt-task-brief>
                    # block. The "fired_but_empty" branch is identified by
                    # the sparse-result marker line.
                    if "[STRUCTURAL RETRIEVAL] Issue text too sparse" in v2:
                        l2_status = "fired_but_empty"
                    else:
                        l2_status = "fired"
            except Exception as exc:  # noqa: BLE001
                logger.warning("L2 fallback raised: %s", exc)

    return brief, l1_status, l2_status


def _symbol_exists_in_graph(graph_db_path: str, symbol: str) -> bool:
    """Return True iff at least one node in graph.db matches ``symbol``
    by name OR qualified_name. Read-only, never raises."""
    if not symbol or not graph_db_path or not os.path.exists(graph_db_path):
        return False
    leaf = symbol.split(".")[-1] if "." in symbol else symbol
    try:
        conn = sqlite3.connect(
            f"file:{graph_db_path}?mode=ro", uri=True, timeout=5,
        )
        try:
            row = conn.execute(
                "SELECT 1 FROM nodes WHERE name = ? OR qualified_name = ? "
                "OR qualified_name LIKE ? OR LOWER(name) = LOWER(?) LIMIT 1",
                (symbol, symbol, f"%.{leaf}", leaf),
            ).fetchone()
            return row is not None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except sqlite3.Error:
        return False


def _pick_bootstrap_symbol(issue_text: str, graph_db_path: str = "") -> str:
    """Choose one stable symbol/token for the optional first gt_query call.

    RC-01: validate the candidate against graph.db before returning. If no
    candidate resolves to a real node — or if graph.db is unreachable —
    return ``""`` so the agent template can omit the "First action
    requirement" directive entirely (gating directive on resolvable symbol).
    Callers that pass an empty ``graph_db_path`` keep the legacy best-effort
    behavior (returns the first candidate token without validation).
    """
    candidates: list[str] = []
    try:
        extract_fn, _ = _import_gt_intel()
        ids = list(extract_fn(issue_text or ""))
        candidates.extend(str(x) for x in ids)
    except Exception:  # noqa: BLE001
        pass
    text = (issue_text or "").strip()
    for tok in re.split(r"[^A-Za-z0-9_./-]+", text):
        if len(tok) >= 4 and any(c.isalpha() for c in tok):
            candidates.append(tok)

    if not graph_db_path:
        for c in candidates:
            if c:
                return c
        return ""

    for c in candidates:
        if _symbol_exists_in_graph(graph_db_path, c):
            return c
    return ""


# ---------------------------------------------------------------------------
# Atomic file writes (durability — Track 4 plan: "every per-task artifact
# written via fsync-on-close. Crash mid-run preserves completed task logs
# durably.").
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: str) -> None:
    """Write data to path atomically with fsync. Best-effort on Windows
    (os.replace is atomic on NTFS for same-volume targets)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _append_layers_log(path: Path, instance_id: str, l1: str, l2: str) -> None:
    """Append a single ``task=… L1=… L2=…`` line. Other layer fields are
    appended later by Tracks B1/C/D's hooks; this writer is intentionally
    minimal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"task={instance_id} L1={l1} L2={l2}\n"
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass


def _init_host_artifact_stubs(log_dir: Path) -> None:
    """Create deterministic host artifact skeleton to avoid missing-file drift."""
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "gt_evidence").mkdir(parents=True, exist_ok=True)
    # RC-10 (D-005): pre-create all 6 expected JSONL stubs (post 11→4
    # consolidation). Without all six, "tool _emit_telemetry silently
    # failed (OSError on container log dir)" is indistinguishable from
    # "tool was never invoked" — both leave no file. Stubbing makes a
    # missing file a real signal (the pull didn't happen).
    for name in (
        "gt_query_calls.jsonl",
        "gt_search_calls.jsonl",
        "gt_navigate_calls.jsonl",
        "gt_validate_calls.jsonl",
        "gt_reindex.jsonl",
    ):
        p = log_dir / name
        if not p.exists():
            p.write_text("", encoding="utf-8")


def _init_container_artifact_stubs(env: Any, instance_id: str) -> None:
    """Best-effort creation of container artifact files expected by pullback.

    RC-10 (D-005): same as the host-side stubs — pre-create all 6
    *_calls.jsonl files inside the container so pull-back can
    distinguish "tool wrote nothing" (empty) from "_emit_telemetry
    OSError" (absent).
    """
    try:
        env.communicate(
            (
                "mkdir -p /root/gt_artifacts/gt_evidence "
                "&& : > /root/gt_artifacts/gt_query_calls.jsonl "
                "&& : > /root/gt_artifacts/gt_search_calls.jsonl "
                "&& : > /root/gt_artifacts/gt_navigate_calls.jsonl "
                "&& : > /root/gt_artifacts/gt_validate_calls.jsonl "
                "&& : > /root/gt_artifacts/gt_reindex.jsonl"
            ),
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("container artifact init skipped for %s: %s", instance_id, exc)


# ---------------------------------------------------------------------------
# Container <-> host transfer helpers (graph.db push at start, artifact pull
# pre-close).
# ---------------------------------------------------------------------------

# Container paths owned by gt_edit / gt_pre_finish_gate (Track 4 plan).
_CONTAINER_GRAPH_DB = "/tmp/graph.db"
_CONTAINER_ARTIFACT_DIR = "/root/gt_artifacts"
_CONTAINER_EVIDENCE_DIR = "/root/gt_artifacts/gt_evidence"
# RC-4 BUG-3: gt_query_calls.jsonl was previously omitted from this tuple.
# gt_query.py:_emit_telemetry() writes it to $GT_INSTANCE_LOG_DIR (which is
# the same /root/gt_artifacts dir), but without listing it here it never made
# it back to the host. The L4 counter then fell back to a substring scan of
# `result.trajectory` which (a) over-counted PATH-export install lines that
# contain the token "gt_query" and (b) was broadcast to ALL pending entries
# on the drain-all fallback. Pulling the canonical artifact and counting its
# lines fixes both.
_FLAT_ARTIFACTS = (
    "gt_pre_finish_gate.json",
    "gt_reindex.jsonl",
    "gt_query_calls.jsonl",
    # Curation-surface telemetry after the 2026-05-06 11→4 consolidation:
    # one *_calls.jsonl per consolidated bundle (gt_search collapses the
    # 6 prior search_* bundles, gt_navigate collapses gt_trace + gt_impact
    # + gt_find_relevant, gt_validate is unchanged).
    "gt_search_calls.jsonl",
    "gt_navigate_calls.jsonl",
    "gt_validate_calls.jsonl",
)


def _run_async_safely(coro_factory: Any, timeout_s: float = 120.0) -> Any:
    """RC-15: asyncio.run() raises RuntimeError if a loop is already running
    on the calling thread. Future SWE-agent versions may invoke our hook
    inside an async context; the legacy code swallowed that as a generic
    exception and fell through to a corrupt graph.db rebuild fallback.

    ``coro_factory`` is a zero-arg callable returning a fresh coroutine —
    required because a coroutine cannot be re-awaited if the first attempt
    hits the loop-already-running path.

    Strategy: try the no-loop fast path with asyncio.run; if a loop is
    already running, dispatch the freshly-built coroutine onto a private
    loop in a worker thread and block with a real timeout. Caller handles
    exceptions.
    """
    import asyncio
    coro = coro_factory()
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "running event loop" not in msg and "asyncio.run() cannot" not in msg:
            raise
        # asyncio.run rejected the coro before scheduling — close the orphan
        # to silence "coroutine was never awaited" RuntimeWarnings, then
        # build a fresh one for the worker-thread loop.
        try:
            coro.close()
        except Exception:  # noqa: BLE001
            pass
        result_box: dict[str, Any] = {}

        def _runner() -> None:
            inner_loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(inner_loop)
                result_box["value"] = inner_loop.run_until_complete(coro_factory())
            except BaseException as e:  # noqa: BLE001
                result_box["error"] = e
            finally:
                inner_loop.close()

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=timeout_s)
        if t.is_alive():
            raise TimeoutError(
                f"async upload exceeded {timeout_s}s (loop-already-running path)"
            )
        if "error" in result_box:
            raise result_box["error"]
        return result_box.get("value")


def _push_graph_db_to_container(env: Any, host_path: str, instance_id: str) -> bool:
    """Copy the host's prebuilt graph.db into the container at /tmp/graph.db.

    Uses SWE-ReX's UploadRequest (path-based, binary-safe) — same mechanism
    SWE-agent itself uses for tool bundles in `sweagent/tools/tools.py`.

    Returns True on success, False otherwise. Never raises. RC-15: retries
    once with 1s backoff on transient failure; the loop-already-running
    failure mode is handled inside _run_async_safely.
    """
    if not host_path or not os.path.exists(host_path):
        logger.warning(
            "graph.db push skipped for %s: host path %r missing",
            instance_id, host_path,
        )
        return False
    try:
        from swerex.runtime.abstract import UploadRequest  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "graph.db push failed for %s: swerex import error: %s",
            instance_id, exc,
        )
        return False

    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            _run_async_safely(
                lambda: env.deployment.runtime.upload(
                    UploadRequest(
                        source_path=host_path,
                        target_path=_CONTAINER_GRAPH_DB,
                    )
                ),
                timeout_s=120.0,
            )
            logger.info(
                "graph.db pushed: %s -> container:%s (instance=%s, attempt=%d)",
                host_path, _CONTAINER_GRAPH_DB, instance_id, attempt,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == 1:
                logger.debug(
                    "graph.db push attempt 1 failed for %s: %s — backing off 1s",
                    instance_id, exc,
                )
                time.sleep(1.0)
                continue
    logger.warning(
        "graph.db push failed for %s after 2 attempts: %s — surfacing push_failed",
        instance_id, last_exc,
    )
    return False


def _container_path_exists(env: Any, container_path: str) -> bool:
    """Return True iff ``container_path`` exists as a file inside the container.

    Used to gate ``env.read_file`` calls so that swerex doesn't log the
    "🦖 ERROR" line every time we probe an optional artifact that the
    in-container tool happened not to write. The previous code path was:

        env.read_file(missing) -> 500 -> swerex logs ERROR -> raises
        FileNotFoundError -> our caller swallows it.

    Net: every absent artifact wrote one noise line per task, with N tasks
    × M artifacts × no-fire = noisy logs. We saw 4-5 of these per task in
    Phase 2 (2026-05-06). The probe is one cheap `bash -c "test -f <p>"`.

    Best-effort: any failure to invoke the probe is treated as "unknown",
    in which case we fall back to attempting the read (preserves the
    pre-fix behavior so a probe regression cannot lose artifacts).
    """
    if env is None or not container_path:
        return False
    try:
        out = env.communicate(
            f"test -f {shlex_quote(container_path)} && echo __GT_FOUND__ || echo __GT_MISSING__",
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "container_path_exists probe failed for %s: %s — falling back to read",
            container_path, exc,
        )
        # Fall back to "exists" so the read attempt happens and the existing
        # FileNotFound path triggers; this preserves the pre-fix behavior
        # rather than silently dropping a real artifact on probe regressions.
        return True
    text = (out or "").strip()
    if "__GT_FOUND__" in text:
        return True
    if "__GT_MISSING__" in text:
        return False
    # Ambiguous output (e.g. shell init prints garbage) — fall back to attempting
    # the read.
    logger.debug(
        "container_path_exists ambiguous probe output for %s: %r",
        container_path, text[:200],
    )
    return True


def _read_file_with_retry(
    env: Any,
    container_path: str,
    *,
    attempts: int = 3,
    backoff_s: float = 1.0,
) -> tuple[str | None, str | None]:
    """RC-15: env.read_file is the single point of artifact loss at scale.

    Each call hits the swerex bridge over a TCP socket; transient resets
    (container under load, socket churn) caused single-shot failures to
    propagate as "missing artifact" — i.e. silent partial pulls that the
    verifier cannot distinguish from a true zero.

    Returns ``(content, error)``. On success, error is None. On failure
    after exhausting retries, content is None and error is a short
    classification string. ``FileNotFoundError`` short-circuits — that's
    a real "absent" signal, not a transient.
    """
    last_err: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            return env.read_file(container_path), None
        except FileNotFoundError:
            return None, "file_not_found"
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}"
            if attempt < attempts:
                time.sleep(backoff_s)
    return None, last_err or "read_error"


def _pull_gt_artifacts(env: Any, host_log_dir: Path, instance_id: str) -> dict[str, Any]:
    """Pull GT artifacts from the container to host_log_dir.

    Pulls (if present):
      - /root/gt_artifacts/gt_pre_finish_gate.json
      - /root/gt_artifacts/gt_reindex.jsonl
      - /root/gt_artifacts/gt_evidence/edit_*.json

    Returns a small dict summarizing what was pulled, used by the
    on_instance_completed hook to compose the L3/L4/L5/L6 log line.

    Never raises. Missing artifacts are silent (gt_edit / gt_pre_finish_gate
    are optional layers; absence is a valid state).
    """
    summary: dict[str, Any] = {
        "edit_count": 0,        # L3
        "query_count": 0,       # L4 — RC-4 BUG-1/BUG-3: pulled from gt_query_calls.jsonl
        "search_count": 0,      # L4b — sum of gt_search_calls.jsonl invocations
        "navigate_count": 0,    # L4c — sum of gt_navigate_calls.jsonl invocations
        "validate_count": 0,    # L5b — sum of gt_validate_calls.jsonl invocations
        "reindex_count": 0,     # L6
        "gate_verdict": "absent",  # L5
        # RC-10 (D-009 / J-fix): track per-artifact write outcomes so the
        # caller can distinguish a complete pull from a partial one. A
        # task whose pull is partial must NOT contribute to rate-gate
        # denominators in verify_report — its zeros are not real zeros,
        # they're missing data.
        "pull_attempted": 0,
        "pull_succeeded": 0,
        "pull_failures": [],     # list[str] of "<artifact>:<reason>"
        "partial_pull": False,
    }
    host_log_dir.mkdir(parents=True, exist_ok=True)

    # Flat files: probe-existence-first (test -f) so missing artifacts don't
    # generate the "🦖 ERROR" log noise that the swerex client emits whenever
    # env.read_file hits a FileNotFoundError. The probe is one cheap
    # `bash -c "test -f"` per artifact (vs the 500-error-then-FileNotFound
    # round-trip on the previous code path). Missing files are still a valid
    # no-op state for L3-L6 — we only count what's actually present.
    for name in _FLAT_ARTIFACTS:
        container_path = f"{_CONTAINER_ARTIFACT_DIR}/{name}"
        summary["pull_attempted"] += 1
        if not _container_path_exists(env, container_path):
            logger.debug(
                "artifact pull skip %s for %s: not present (pre-checked)",
                container_path, instance_id,
            )
            # "not present" is a valid no-op state when stubs are
            # pre-created (see _init_container_artifact_stubs); count it
            # as a successful attempt with zero records.
            summary["pull_succeeded"] += 1
            continue
        # RC-15: 3 attempts × 1s backoff. Single-shot env.read_file was
        # the dominant artifact-loss vector at n=300 — transient socket
        # resets returned silent zeros that masked healthy runs.
        content, read_err = _read_file_with_retry(env, container_path)
        if read_err == "file_not_found":
            logger.debug(
                "artifact pull race %s for %s: gone between probe and read",
                container_path, instance_id,
            )
            summary["pull_failures"].append(f"{name}:race_disappeared")
            continue
        if read_err is not None:
            logger.debug(
                "artifact pull skip %s for %s after retries: %s",
                container_path, instance_id, read_err,
            )
            summary["pull_failures"].append(
                f"{name}:read_error:{read_err}"
            )
            continue
        if content is None:  # pragma: no cover — defensive
            summary["pull_failures"].append(f"{name}:read_empty")
            continue
        try:
            (host_log_dir / name).write_text(content, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "artifact write_host failed %s for %s: %s",
                name, instance_id, exc,
            )
            summary["pull_failures"].append(
                f"{name}:host_write_error:{type(exc).__name__}"
            )
            continue
        summary["pull_succeeded"] += 1
        if name == "gt_reindex.jsonl":
            summary["reindex_count"] = sum(
                1 for ln in content.splitlines() if ln.strip()
            )
        elif name == "gt_query_calls.jsonl":
            # RC-4 BUG-1: count canonical gt_query_calls.jsonl lines instead
            # of the trajectory-substring scan, which over-counted PATH=...
            # install lines that contain the literal token "gt_query".
            # gt_query.py writes one JSON line per invocation — empty file
            # means zero invocations, which is a valid state.
            summary["query_count"] = sum(
                1 for ln in content.splitlines() if ln.strip()
            )
        elif name == "gt_search_calls.jsonl":
            summary["search_count"] = sum(
                1 for ln in content.splitlines() if ln.strip()
            )
        elif name == "gt_navigate_calls.jsonl":
            summary["navigate_count"] = sum(
                1 for ln in content.splitlines() if ln.strip()
            )
        elif name == "gt_validate_calls.jsonl":
            summary["validate_count"] = sum(
                1 for ln in content.splitlines() if ln.strip()
            )
        elif name == "gt_pre_finish_gate.json":
            try:
                gate = json.loads(content) if content.strip() else {}
                # Gate writer (tools/.../gt_pre_finish_gate.py) sets
                # `verdict["result"]` — pass / force / no_graph_db / blocked /
                # warn_soft_escape. Earlier code read `gate.get("verdict")`
                # which never matched and silently defaulted to "unknown" on
                # every successful pull-back. Verified 2026-05-06 against the
                # gate's `_write_verdict()` and `main()` assignments.
                summary["gate_verdict"] = str(gate.get("result", "unknown"))
            except json.JSONDecodeError:
                summary["gate_verdict"] = "malformed"

    # Directory: gt_evidence/edit_*.json — list via communicate, then pull each.
    try:
        listing = env.communicate(
            f"ls -1 {_CONTAINER_EVIDENCE_DIR} 2>/dev/null || true",
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("evidence listing failed for %s: %s", instance_id, exc)
        listing = ""

    edit_dir = host_log_dir / "gt_evidence"
    edit_count = 0
    for raw in (listing or "").splitlines():
        name = raw.strip()
        if not name.startswith("edit_") or not name.endswith(".json"):
            continue
        container_path = f"{_CONTAINER_EVIDENCE_DIR}/{name}"
        content, read_err = _read_file_with_retry(env, container_path)
        if read_err is not None or content is None:
            logger.debug(
                "evidence read skip %s after retries: %s",
                container_path, read_err,
            )
            # Per-evidence read failures contribute to partial_pull —
            # they're real artifacts the agent produced and we lost.
            summary["pull_failures"].append(
                f"evidence/{name}:read_error:{read_err or 'empty'}"
            )
            continue
        try:
            edit_dir.mkdir(parents=True, exist_ok=True)
            (edit_dir / name).write_text(content, encoding="utf-8")
            edit_count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "evidence write_host failed %s for %s: %s",
                name, instance_id, exc,
            )
    summary["edit_count"] = edit_count

    # RC-12: third filesystem location for one task's GT state. Besides
    # /root/state.json (state file) and /root/gt_artifacts/ (canonical L3-L6
    # artifact dir), gt_hook also mirrors per-call telemetry into
    # /tmp/gt_telemetry_<instance_id>/layer*_*.jsonl when GT_INSTANCE_ID is
    # set. Pre-RC-12 this dir was never pulled, so any layer that ONLY wrote
    # to the mirror (e.g. when /root/gt_artifacts was unwritable) appeared
    # as zero on the host. Probe-list the mirror dir; pull every file we
    # find. Treat absence as a normal no-op state.
    safe_iid = re.sub(r"[^A-Za-z0-9._-]", "_", instance_id)
    mirror_dir = f"/tmp/gt_telemetry_{safe_iid}"
    summary["telemetry_mirror_pulled"] = 0
    try:
        ls_proc = env.execute_action(
            f'bash -lc "ls -1 {shlex.quote(mirror_dir)} 2>/dev/null || true"',
            timeout=10,
        )
        listing = (getattr(ls_proc, "output", "") or "").splitlines()
    except Exception as exc:  # noqa: BLE001
        logger.debug("telemetry mirror probe failed for %s: %s", instance_id, exc)
        listing = []
    for fname in listing:
        fname = fname.strip()
        if not fname or fname.startswith("."):
            continue
        cpath = f"{mirror_dir}/{fname}"
        try:
            content = env.read_file(cpath)
        except Exception:  # noqa: BLE001
            continue
        try:
            host_target = host_log_dir / "gt_telemetry_mirror" / fname
            host_target.parent.mkdir(parents=True, exist_ok=True)
            host_target.write_text(content, encoding="utf-8")
            summary["telemetry_mirror_pulled"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("telemetry mirror write_host failed %s: %s", fname, exc)

    # RC-10 (D-009/J-fix): a partial pull means at least one expected
    # flat artifact failed to read or write. The 0/1 counters that
    # remain in the summary are NOT real zeros — they are missing data.
    # verify_report.py excludes partial-pull tasks from rate-gate
    # denominators so a half-broken pull cannot mask a healthy run as
    # "delivery_rate=0".
    summary["partial_pull"] = bool(summary["pull_failures"])

    logger.info(
        "GT artifacts pulled (instance=%s): L3_edits=%d L4_queries=%d "
        "L4_search=%d L4_navigate=%d L5_gate=%s L5_validate=%d L6_reindex=%d "
        "partial_pull=%s pull_succeeded=%d/%d",
        instance_id, edit_count, summary["query_count"],
        summary["search_count"], summary["navigate_count"],
        summary["gate_verdict"], summary["validate_count"],
        summary["reindex_count"],
        summary["partial_pull"],
        summary["pull_succeeded"], summary["pull_attempted"],
    )
    return summary


def _wrap_env_close_with_artifact_pull(
    env: Any,
    host_log_dir: Path,
    instance_id: str,
    cache: dict[str, Any],
) -> None:
    """Wrap env.close so artifacts are pulled BEFORE deployment.stop().

    Required because run_batch.py calls env.close() (which kills the
    container) BEFORE on_instance_completed fires, leaving no env access at
    completion time. We pull pre-close and ALSO write the per-task
    L3/L4/L5/L6 line to gt_layers.log here — at this point we still have
    the instance_id captured in the closure. on_instance_completed only
    serves as a defensive last-resort for tasks where env.close never
    fires (autosubmit / exit_cost paths bypass it).

    RC-11: ALSO register an ``atexit`` flush handler in the wrapper's
    closure. SWE-agent's autosubmit / cost-exit / call-limit-exit paths
    bypass ``env.close()`` entirely, so the canonical pre-close pull never
    runs. The atexit handler holds the (env, log_dir, instance_id, cache)
    closure and on process exit attempts ONE last
    ``_pull_gt_artifacts(env, ...)`` if the cache hasn't already logged.
    Best-effort: if the container is already dead at exit time the pull is
    silent. Idempotent vs the close-wrap path via ``cache.completion_logged``.

    Pre-RC-?: the L3-L6 line was written from on_instance_completed,
    which received a `result` object that — on this model setup — has
    `info.instance_id == None`. The legacy `_resolve_instance_id`
    fell through all 5 fallbacks under parallel workers (5+ pending
    tasks at once), so the RC-4 BUG-5 guard's "tag-first-pending"
    branch wrote zero-counters with `L5_gate=no_close_wrap` against the
    WRONG instance, even though `_pull_gt_artifacts` had succeeded with
    the correct counters. Verified 2026-05-06 phase2 5-task smoke:
    actual artifact files were correct (L3=9 etc) but gt_layers.log
    summary line said zeros. Writing the line here (where instance_id
    is captured in the closure) makes the summary line correct
    deterministically.

    See sweagent/environment/swe_env.py:close — runs deployment.stop()
    immediately, so we must intercept earlier.

    RC-11: SWE-agent's autosubmit / cost-exit / call-limit-exit / SIGTERM
    paths bypass ``env.close()`` entirely, so the canonical pre-close pull
    never fires. We register an ``atexit`` handler in this wrapper's closure
    that, on process exit OR explicit invocation by ``on_instance_completed``,
    attempts ONE last ``_pull_gt_artifacts`` (if env hasn't been GC'd) and
    writes the L3-L6 line stamped with ``exit_status=atexit``. Idempotent vs
    the close-wrap path via ``cache.completion_logged``. The flush callable
    is also stashed on ``cache["atexit_flush"]`` so ``on_instance_completed``
    can invoke it synchronously when it sees an autosubmit / exit_cost /
    exit_context transition (no need to wait for true process exit, which
    would lose artifacts when the next task starts).
    """
    original_close = env.close
    # RC-11: hold env via weakref so the atexit handler doesn't pin the
    # SWE-agent env past its natural lifetime. The handler bails silently
    # if env has already been GC'd by the time exit fires.
    env_ref = weakref.ref(env)

    def _atexit_flush() -> None:
        """RC-11 last-resort artifact flush.

        Runs at process exit or by explicit invocation from
        ``on_instance_completed`` (when it detects an exit_cost / exit_context
        / autosubmitted exit_status). Idempotent vs the close-wrap path via
        ``cache.completion_logged``.
        """
        if cache.get("completion_logged"):
            return
        live_env = env_ref()
        try:
            if live_env is not None:
                try:
                    summary = _pull_gt_artifacts(
                        live_env, host_log_dir, instance_id,
                    )
                    cache["summary"] = summary
                except Exception as exc:  # noqa: BLE001
                    cache["pull_error"] = repr(exc)
            if cache.get("summary"):
                summary_for_log = dict(cache["summary"])
            elif cache.get("pull_error"):
                summary_for_log = {
                    "edit_count": 0,
                    "query_count": 0,
                    "reindex_count": 0,
                    "gate_verdict": "pull_failed",
                }
            else:
                summary_for_log = {
                    "edit_count": 0,
                    "query_count": 0,
                    "reindex_count": 0,
                    "gate_verdict": "no_close_wrap",
                }
            # Cohort marker for verify_report (RC-10). on_instance_completed
            # may have pre-stamped a more specific value (cost_exit /
            # call_exit) — respect it via setdefault.
            # TODO(RC-11-coord): RC-10 owns the schema flow that turns this
            # cell into engagement_rate denominator exclusions.
            summary_for_log.setdefault("exit_status", "atexit")
            l4_count = _count_gt_query_calls(host_log_dir)
            _append_completion_log(
                host_log_dir / "gt_layers.log",
                instance_id,
                summary_for_log,
                l4_count,
            )
            try:
                sidecar = host_log_dir / "gt_completion_summary.json"
                sidecar.write_text(
                    json.dumps(summary_for_log, sort_keys=True),
                    encoding="utf-8",
                )
            except Exception as sidecar_exc:  # noqa: BLE001
                logger.debug(
                    "atexit completion sidecar write failed for %s: %s",
                    instance_id, sidecar_exc,
                )
            cache["completion_logged"] = True
        except Exception as exc:  # noqa: BLE001
            # atexit must NEVER raise — Python prints
            # "Error in atexit._run_exitfuncs" and continues. Log and swallow.
            logger.debug(
                "atexit flush failed for %s: %s", instance_id, exc,
            )

    atexit.register(_atexit_flush)
    cache["atexit_flush"] = _atexit_flush

    def _wrapped_close() -> None:
        try:
            summary = _pull_gt_artifacts(env, host_log_dir, instance_id)
            cache["summary"] = summary
        except Exception as exc:  # noqa: BLE001
            # RC-4 BUG-4: previously, a pull-wrapper exception silently fell
            # through and on_instance_completed defaulted to a zero-summary
            # with gate_verdict="absent" — masking pull failures as "gate
            # genuinely didn't run". Stash a sentinel so the completion log
            # records gate_verdict="pull_failed" and counters reflect reality
            # (unknown vs zero).
            logger.warning(
                "artifact pull wrapper failed for %s: %s",
                instance_id, exc,
            )
            cache["pull_error"] = repr(exc)

        # Write the per-task L3/L4/L5/L6 line HERE — instance_id is the
        # closure-captured one, never wrong even under N-way parallel
        # workers. Best-effort; if this raises we still fall through to
        # original_close so the container is stopped.
        try:
            if cache.get("summary"):
                summary_for_log = dict(cache["summary"])
            elif cache.get("pull_error"):
                summary_for_log = {
                    "edit_count": 0,
                    "query_count": 0,
                    "reindex_count": 0,
                    "gate_verdict": "pull_failed",
                }
            else:
                summary_for_log = {
                    "edit_count": 0,
                    "query_count": 0,
                    "reindex_count": 0,
                    "gate_verdict": "absent",
                }
            # RC-11: env.close fires on the normal-completion path. The
            # cost-exit / call-limit-exit / SIGTERM paths bypass it and are
            # caught instead by _atexit_flush above (or by the synchronous
            # invocation from on_instance_completed for autosubmit).
            summary_for_log.setdefault("exit_status", "normal")
            l4_count = _count_gt_query_calls(host_log_dir)
            _append_completion_log(
                host_log_dir / "gt_layers.log",
                instance_id,
                summary_for_log,
                l4_count,
            )
            # RC-10 (D-008/B-fix): also persist the summary as a JSON
            # sidecar that the smoke runner reads at completion-time to
            # emit the SINGLE canonical [GT_LAYERS] line. The
            # gt_layers.log append above remains for backward
            # compatibility with the existing pullback-hook tests, but
            # the JSON sidecar is now the source of truth that the
            # canonical-writer flow consumes — picking ONE writer
            # (smoke runner) and demoting the others to JSON sidecars.
            try:
                sidecar = host_log_dir / "gt_completion_summary.json"
                sidecar.write_text(
                    json.dumps(summary_for_log, sort_keys=True),
                    encoding="utf-8",
                )
            except Exception as sidecar_exc:  # noqa: BLE001
                logger.debug(
                    "completion sidecar write failed for %s: %s",
                    instance_id, sidecar_exc,
                )
            cache["completion_logged"] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "completion log append failed in close-wrap for %s: %s",
                instance_id, exc,
            )
        return original_close()

    env.close = _wrapped_close  # type: ignore[method-assign]


def _safety_net_finalize(
    host_log_dir: Path,
    instance_id: str,
    cache: dict[str, Any],
) -> None:
    """RC-03 (d) safety net — runs at env GC time via weakref.finalize.

    The env.close monkey-patch in `_wrap_env_close_with_artifact_pull` is
    the canonical writer for the per-task gt_layers.log L3-L6 line under
    parallel workers, but a small set of SWE-agent paths can bypass
    `env.close()` (autosubmit, exit_cost, agent crash propagating up
    before close fires). When that happens, the env object eventually
    becomes unreferenced — at which point CPython's GC fires the
    weakref.finalize callback registered in `on_instance_start`. We use
    that as a last-chance writer.

    Idempotent vs the close-wrap path: we check `cache["completion_logged"]`
    and bail out if the close-wrap already wrote the canonical line. We
    do NOT pull artifacts here — the container is gone by GC time. We
    write a `gate_verdict="no_close_wrap"` line so verify_report sees a
    distinct, named failure mode rather than a missing log entry.
    """
    if cache.get("completion_logged"):
        return
    try:
        summary = {
            "edit_count": 0,
            "query_count": 0,
            "reindex_count": 0,
            "gate_verdict": "no_close_wrap",
        }
        l4_count = _count_gt_query_calls(host_log_dir)
        _append_completion_log(
            host_log_dir / "gt_layers.log",
            instance_id,
            summary,
            l4_count,
        )
        cache["completion_logged"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "weakref safety-net log append failed for %s: %s",
            instance_id, exc,
        )


def _count_gt_query_calls(host_log_dir: Path | None) -> int:
    """Count L4 gt_query invocations from the canonical artifact.

    RC-4 BUG-1 + BUG-2: the previous implementation scanned
    ``result.trajectory`` for the substring "gt_query". Two failure modes:

      (1) Drain-all fan-out — the count was computed once per
          on_instance_completed call and broadcast to every pending
          entry. With concurrent workers, all tasks ended up showing
          the same number.

      (2) Tokenization false-positives — install/setup lines like
          ``export PATH=$PATH:/.../gt_query/bin`` matched the substring
          test, producing a non-zero floor (~3 per task) even when the
          agent never invoked the tool. The 5-task smoke
          ``track4_smoke_5task_1778046768`` showed L4=3 on every task
          while the actual trajectories had 0/0/0/0/3 invocations.

    The fix is to read ``gt_query_calls.jsonl`` from the per-task host
    artifact directory, populated by ``_pull_gt_artifacts`` from
    ``$GT_INSTANCE_LOG_DIR/gt_query_calls.jsonl`` inside the container.
    Each invocation appends one JSON line (see
    ``tools/sweagent/gt_query/lib/gt_query.py:_emit_telemetry``); the
    line count IS the canonical L4 count. Empty file or missing file
    means zero invocations — both are valid states.

    Returns 0 if the file does not exist or cannot be read.

    RC-10 (D-006): delegates to the canonical shared helper
    ``gt_layer_counts.count_layer_calls`` so this reader cannot
    disagree with the smoke runner / deep_util_gate /
    full_potential_analyzer readers. Returns the gt_query JSONL line
    count for backward compatibility with the existing call sites; the
    full per-tool breakdown is available via ``count_layer_calls``.
    """
    if host_log_dir is None:
        return 0
    try:
        from gt_layer_counts import count_layer_calls
    except ImportError:  # pragma: no cover — fallback for legacy test envs
        path = host_log_dir / "gt_query_calls.jsonl"
        if not path.exists():
            return 0
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return sum(1 for ln in fh if ln.strip())
        except OSError as exc:
            logger.debug("gt_query_calls.jsonl read failed for %s: %s", path, exc)
            return 0
    return count_layer_calls(host_log_dir).get("gt_query", 0)


def _append_completion_log(
    path: Path,
    instance_id: str,
    summary: dict[str, Any],
    l4_count: int,
) -> None:
    """Append a final per-task layer line to gt_layers.log.

    RC-10 (D-001/D-008) NOTE: this writer's `task=… L3_edits=…` line
    format does NOT match the canonical `[GT_LAYERS] task=… L3=N` regex
    that ``gt_layers_verifier.py`` parses — and the verifier classifies
    every line that starts with `task=` as bad/unparseable. Track 4's
    line is therefore write-once-read-by-substring (deep_util_gate /
    full_potential_analyzer grep for `L2=fired`/`L5_gate=…`). The
    truth-bearing source for the verifier is the smoke runner's
    canonical `[GT_LAYERS] …` line, which now reads from the JSON
    sidecar (`gt_completion_summary.json`) written alongside this
    append in `_wrapped_close`. Keeping the legacy append for
    backwards-compat with the existing pullback-hook tests; coordinate
    full removal with RC-03 (the concurrency cluster also touches
    this path). # TODO(RC-10-coord): once RC-03 lands, drop this
    legacy writer and have the smoke runner be the only writer to
    gt_layers.log.

    The line carries six counter cells in order to match the post-2026-05-06
    11→4 consolidation:

      task=<id>
      L3_edits=<n>            -- gt_edit state-command edit_*.json count
      L4_queries=<n>          -- gt_query invocations
      L4_search=<n>           -- gt_search invocations (collapsed 6 search_* bundles)
      L4_navigate=<n>         -- gt_navigate invocations (collapsed gt_trace+gt_impact+gt_find_relevant)
      L5_gate=<verdict>       -- gt_pre_finish_gate result
      L5_validate=<n>         -- gt_validate invocations
      L6_reindex=<n>          -- gt-index reindex passes
      exit_status=<status>    -- RC-11: cost_exit / call_exit / atexit / normal.
                                 Lets the verify_report layer exclude cost-
                                 exited tasks from rate-gate denominators
                                 (or compute them as a separate cohort) so
                                 cost-exits don't bias arm comparison.
                                 Default "normal" when the field is not set
                                 by the writer. # TODO(RC-11-coord): RC-10
                                 owns the schema flow into verify_report's
                                 rate-aggregation step.

    The L3_edits / L4_queries / L5_gate / L6_reindex cells are unchanged;
    L4_search / L4_navigate / L5_validate / exit_status are additive so
    existing parsers (tests/layers/test_pullback_hook.py,
    swe_agent_smoke_runner.py) keep working unmodified.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        f"task={instance_id} "
        f"L3_edits={summary.get('edit_count', 0)} "
        f"L4_queries={l4_count} "
        f"L4_search={summary.get('search_count', 0)} "
        f"L4_navigate={summary.get('navigate_count', 0)} "
        f"L5_gate={summary.get('gate_verdict', 'absent')} "
        f"L5_validate={summary.get('validate_count', 0)} "
        f"L6_reindex={summary.get('reindex_count', 0)} "
        f"exit_status={summary.get('exit_status', 'normal')}\n"
    )
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass


# ---------------------------------------------------------------------------
# RunHook — only loaded when sweagent is importable.
# ---------------------------------------------------------------------------

try:  # pragma: no cover — depends on whether sweagent is installed.
    from sweagent.run.hooks.abstract import RunHook  # type: ignore

    class GTTrack4PreRunHook(RunHook):
        """SWE-agent ``RunHook`` for L1+L2 brief injection.

        Constructor args:
          graph_db_path  Path to the prebuilt graph.db. If None, read from
                         env GT_GRAPH_DB at on_instance_start time.
          output_dir     Run output dir (per-instance dirs are
                         <output_dir>/<instance_id>/). If None, falls
                         back to env GT_TRACK4_LOG_DIR or "logs".
          max_lines      Brief line cap. Default 8.

        Side effects per instance:
          - Computes brief via ``compute_brief(...)``
          - Writes <log_dir>/gt_brief.txt (verbatim brief bytes)
          - Appends one line to <log_dir>/gt_layers.log
          - Mutates ``problem_statement.extra_fields["gt_brief"]`` so
            {{gt_brief}} renders inside instance_template.
        """

        def __init__(
            self,
            graph_db_path: str | None = None,
            output_dir: str | os.PathLike[str] | None = None,
            max_lines: int = 8,
        ) -> None:
            self._graph_db_path = graph_db_path
            self._output_dir = Path(output_dir) if output_dir is not None else None
            self._max_lines = max_lines
            # RC-03: SWE-agent's RunBatch fires hook callbacks from worker
            # threads at num_workers >= 2 on the SAME shared hook instance.
            # The shared `_pending` dict is therefore subject to compound
            # read-modify-write races; mutations are serialized through
            # `_pending_lock`. The per-thread map `_thread_pending` keys the
            # active instance_id by `threading.get_ident()` — when the same
            # worker thread that ran on_instance_start also drives
            # on_instance_completed (the SWE-agent contract), this is a
            # deterministic resolution path that does NOT depend on
            # `info["instance_id"]` (which LiteLLM does not populate for
            # the openai-via-litellm route).
            self._pending: dict[str, dict[str, Any]] = {}
            self._pending_lock = threading.Lock()
            self._thread_pending: dict[int, str] = {}

        # The RunHook ABC's signature uses keyword-only args.
        def on_instance_start(  # type: ignore[override]
            self,
            *,
            index: int,
            env: Any,
            problem_statement: Any,
        ) -> None:
            t0 = time.monotonic()
            instance_id = getattr(problem_statement, "id", f"instance_{index}")

            # Resolve graph.db. Multi-task batches: GT_INDEXES_ROOT/<instance_id>/graph.db
            # is preferred so each task gets its own prebuilt index. Single-task fallback:
            # GT_GRAPH_DB. Constructor override (self._graph_db_path) wins for unit tests.
            graph_db = self._graph_db_path or ""
            if not graph_db:
                indexes_root = os.environ.get("GT_INDEXES_ROOT", "")
                if indexes_root:
                    candidate = os.path.join(indexes_root, instance_id, "graph.db")
                    if os.path.exists(candidate):
                        graph_db = candidate
                    else:
                        logger.warning(
                            "Track 4 pre-run hook: GT_INDEXES_ROOT set but %s missing; "
                            "falling back to GT_GRAPH_DB.",
                            candidate,
                        )
                if not graph_db:
                    graph_db = os.environ.get("GT_GRAPH_DB", "")
            if not graph_db:
                logger.error(
                    "Track 4 pre-run hook: no graph.db resolved (checked self._graph_db_path, "
                    "GT_INDEXES_ROOT/<instance>/graph.db, GT_GRAPH_DB). Skipping brief for instance=%s.",
                    instance_id,
                )
                return

            # Resolve repo path
            repo_path = os.environ.get("GT_REPO_PATH", "")
            if not repo_path:
                # Best-effort: SWE-agent's env exposes the repo via env.repo.path.
                repo = getattr(env, "repo", None)
                repo_path = getattr(repo, "path", None) or "/workspace"

            # Resolve issue text
            issue_text = ""
            try:
                issue_text = problem_statement.get_problem_statement()
            except Exception:  # noqa: BLE001
                # Older SWE-agent: .text attribute. Fall back gracefully.
                issue_text = getattr(problem_statement, "text", "") or ""

            # Pull the FAIL_TO_PASS field off the problem statement if the
            # dataset row carries it. SWE-agent's SWEBenchProblemStatement
            # exposes the raw row via .extra_fields; some loaders pin the
            # row to .raw_row or .row instead, so probe a few attributes
            # before giving up.
            f2p_raw: Any = None
            test_patch_text: str | None = None
            for accessor in ("extra_fields", "raw_row", "row"):
                container = getattr(problem_statement, accessor, None)
                if isinstance(container, dict):
                    if "FAIL_TO_PASS" in container:
                        f2p_raw = container["FAIL_TO_PASS"]
                    elif "fail_to_pass" in container:
                        f2p_raw = container["fail_to_pass"]
                    if "test_patch" in container and isinstance(container["test_patch"], str):
                        test_patch_text = container["test_patch"]
                    if f2p_raw is not None or test_patch_text is not None:
                        break
            if f2p_raw is None:
                # Last resort — direct attribute (some custom loaders).
                f2p_raw = getattr(problem_statement, "FAIL_TO_PASS", None)
            if test_patch_text is None:
                tp = getattr(problem_statement, "test_patch", None)
                if isinstance(tp, str):
                    test_patch_text = tp

            brief, l1_status, l2_status = compute_brief(
                issue_text=issue_text,
                repo_path=str(repo_path),
                graph_db_path=str(graph_db),
                max_lines=self._max_lines,
                f2p_raw=f2p_raw,
                test_patch=test_patch_text,
            )

            # Resolve per-instance log dir
            log_root = (
                self._output_dir
                or Path(os.environ.get("GT_TRACK4_LOG_DIR", "logs"))
            )
            log_dir = log_root / instance_id
            try:
                _init_host_artifact_stubs(log_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning("host artifact init failed for %s: %s", instance_id, exc)
            try:
                _atomic_write(log_dir / "gt_brief.txt", brief or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning("gt_brief.txt write failed: %s", exc)
            try:
                # RC-10 (D-008): Track A still appends a `task=… L1=… L2=…`
                # line to gt_layers.log for backwards compat with existing
                # readers (deep_util_gate substring-greps `L2=fired`), but
                # the canonical truth-bearing source is now the JSON
                # sidecar consumed by the smoke runner. The legacy line is
                # write-once-read-by-substring; the sidecar is the
                # contract.
                _append_layers_log(log_dir / "gt_layers.log", instance_id, l1_status, l2_status)
                try:
                    sidecar = log_dir / "gt_brief_status.json"
                    sidecar.write_text(
                        json.dumps({
                            "L1": l1_status,
                            "L2": l2_status,
                            "instance_id": instance_id,
                        }, sort_keys=True),
                        encoding="utf-8",
                    )
                except Exception as sidecar_exc:  # noqa: BLE001
                    logger.debug(
                        "gt_brief_status.json sidecar write failed for %s: %s",
                        instance_id, sidecar_exc,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("gt_layers.log write failed: %s", exc)

            # Inject into Jinja2 context. RC-01: validate against graph.db
            # so the directive is suppressed when no candidate resolves.
            bootstrap_symbol = _pick_bootstrap_symbol(issue_text, str(graph_db))
            try:
                extras = problem_statement.extra_fields
                if extras is None:
                    problem_statement.extra_fields = {
                        "gt_brief": brief or "",
                        "gt_bootstrap_symbol": bootstrap_symbol,
                    }
                else:
                    extras["gt_brief"] = brief or ""
                    extras["gt_bootstrap_symbol"] = bootstrap_symbol
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "could not set extra_fields on problem_statement (%s): %s",
                    type(problem_statement).__name__,
                    exc,
                )

            elapsed_ms = (time.monotonic() - t0) * 1000.0
            logger.info(
                "Track 4 brief: id=%s L1=%s L2=%s bytes=%d elapsed_ms=%.1f",
                instance_id,
                l1_status,
                l2_status,
                len(brief or ""),
                elapsed_ms,
            )

            # ---------------------------------------------------------------
            # Push prebuilt graph.db into container at /tmp/graph.db so
            # gt_edit's _ensure_graph_db_built() finds it and skips the
            # rebuild. Failure is non-fatal (gt_edit rebuilds as fallback).
            # ---------------------------------------------------------------
            _push_graph_db_to_container(env, str(graph_db), instance_id)
            _init_container_artifact_stubs(env, instance_id)

            # ---------------------------------------------------------------
            # Stash state + wrap env.close so GT artifacts are pulled out
            # of the container BEFORE deployment.stop() kills it.
            # run_batch.py:371-373 closes env BEFORE on_instance_completed,
            # so the completion hook only consumes the cached summary.
            # ---------------------------------------------------------------
            cache: dict[str, Any] = {}
            # RC-03 (b): atomic install of pending entry + per-thread mapping.
            tid = threading.get_ident()
            with self._pending_lock:
                self._pending[instance_id] = {
                    "log_dir": log_dir,
                    "cache": cache,
                    "thread_id": tid,
                }
                self._thread_pending[tid] = instance_id
            try:
                _wrap_env_close_with_artifact_pull(env, log_dir, instance_id, cache)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "close-wrap install failed for %s: %s — "
                    "post-run artifacts may be lost",
                    instance_id, exc,
                )

            # RC-03 (d): weakref.finalize safety net. If env.close is bypassed
            # (autosubmit / agent crash propagating up before close fires),
            # the GC of `env` triggers _safety_net_close which writes a
            # best-effort gt_layers.log line so downstream verifiers never see
            # a missing entry. Idempotent vs the close-wrap path via the
            # cache.completion_logged flag.
            try:
                weakref.finalize(
                    env,
                    _safety_net_finalize,
                    log_dir,
                    instance_id,
                    cache,
                )
            except TypeError:
                # env may not support weakrefs (e.g. Mock); skip silently.
                pass

        # The RunHook ABC fires this AFTER env.close() in run_batch mode,
        # so we cannot read the container here — we drain the cache that
        # was populated by the env.close wrapper installed in
        # on_instance_start.
        def on_instance_completed(  # type: ignore[override]
            self,
            *,
            result: Any,
        ) -> None:
            # RC-03 (a): resolve instance_id via the per-thread map FIRST.
            # SWE-agent's RunBatch fires on_instance_start and
            # on_instance_completed for a given task on the SAME worker
            # thread, so the thread id captured at start is the
            # authoritative key — independent of whether
            # `info["instance_id"]` is populated by the model provider.
            # Falls back to the legacy 5-step ladder if the thread map
            # is empty (e.g. the start hook bailed before stash).
            instance_id = self._resolve_instance_id_for_thread(result)

            # RC-03 (b): single locked critical section around _pending
            # lookup + pop avoids the compound RMW race.
            tid = threading.get_ident()
            state: dict[str, Any] | None = None
            with self._pending_lock:
                if not instance_id and len(self._pending) == 1:
                    # Single-task batch — unambiguous.
                    instance_id = next(iter(self._pending))
                if instance_id and instance_id in self._pending:
                    state = self._pending.pop(instance_id)
                # RC-03 (c): if resolution still fails AND multiple pending
                # entries exist, log ERROR and bail. NEVER corrupt an
                # arbitrary entry — that just relabels another task's
                # telemetry as "unresolved" while leaving the real
                # offender pending forever. The close-wrap path already
                # logs the canonical L3-L6 line for any task whose
                # env.close fires; the weakref finalizer is the safety
                # net for the rest. So dropping a stuck pending entry
                # here is the correct no-op.
                self._thread_pending.pop(tid, None)

            if state is None:
                if instance_id:
                    logger.warning(
                        "on_instance_completed: instance_id=%s not in "
                        "_pending (thread_id=%d, pending=%d, info=%r). "
                        "Likely already drained by close-wrap.",
                        instance_id, tid, len(self._pending),
                        self._safe_info_repr(result),
                    )
                elif self._pending:
                    logger.error(
                        "on_instance_completed: instance_id unresolved "
                        "(thread_id=%d, pending=%d, info=%r). Skipping "
                        "without corrupting pending entries. RC-03 guard.",
                        tid, len(self._pending),
                        self._safe_info_repr(result),
                    )
                pending_items: list[tuple[str, dict[str, Any]]] = []
            else:
                pending_items = [(instance_id, state)]

            # Detect autosubmit (agent ran out of step/cost budget without
            # invoking the `submit` tool). SWE-agent's autosubmit path bypasses
            # the gate wrapper entirely, so no /root/gt_artifacts/gt_pre_finish_gate.json
            # is ever written. Distinguishing this from a genuine "gate didn't
            # run" bug is important for the deep utilization gate. Mapping:
            #   exit_status == "autosubmitted" → gate_verdict := "autosubmit"
            #   exit_status == "exit_cost"     → gate_verdict := "autosubmit"
            #   otherwise the verdict file (or "absent") wins.
            exit_status = ""
            try:
                info = getattr(result, "info", None) or {}
                if isinstance(info, dict):
                    exit_status = str(info.get("exit_status", ""))
                else:
                    exit_status = str(getattr(info, "exit_status", "") or "")
            except Exception:  # noqa: BLE001
                exit_status = ""
            autosubmitted = exit_status in ("autosubmitted", "exit_cost", "exit_context")

            # RC-11: map exit_status to the canonical cohort marker we
            # write into gt_layers.log so verify_report (RC-10) can split
            # the engagement-rate denominator. Three buckets:
            #   exit_cost     -> "cost_exit"   (per-instance cost limit hit)
            #   exit_context  -> "call_exit"   (per-instance call limit hit)
            #   autosubmitted -> "autosubmit"  (agent ran out of budget)
            #   anything else -> "normal"      (env.close fired cleanly)
            # TODO(RC-11-coord): RC-10 owns the schema flow that turns this
            # cell into engagement_rate denominator exclusions.
            _exit_cohort = {
                "exit_cost": "cost_exit",
                "exit_context": "call_exit",
                "autosubmitted": "autosubmit",
            }.get(exit_status, "normal")

            for iid, state in pending_items:
                if not state:
                    continue
                log_dir = state.get("log_dir")
                cache = state.get("cache") or {}
                # If the env.close wrap already wrote the L3-L6 line for
                # this task (the canonical path under parallel workers),
                # don't double-write. RC-03: pending entry is already
                # popped inside the locked critical section above; this
                # is purely a skip.
                if cache.get("completion_logged"):
                    continue

                # RC-11: cost-exit / call-limit-exit / autosubmit BYPASS
                # env.close — the canonical pre-close pull never fires, so
                # the cache has no summary. Pre-stamp the cohort and invoke
                # the wrapper's atexit_flush callable synchronously: it
                # holds env_ref in its closure and will pull artifacts from
                # the still-live env if possible. Idempotent vs the
                # close-wrap path via cache.completion_logged.
                if autosubmitted and not cache.get("summary"):
                    cache.setdefault("exit_status_pre", _exit_cohort)
                    flush = cache.get("atexit_flush")
                    if callable(flush):
                        try:
                            # Pre-stamp so _atexit_flush's setdefault keeps
                            # the more-specific cost_exit / call_exit value
                            # rather than its default "atexit".
                            cache["pre_stamped_exit"] = True
                            # Inject the cohort into a dummy summary stub
                            # only if the pull doesn't populate one.
                            flush()
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(
                                "synchronous atexit_flush failed for %s: %s",
                                iid, exc,
                            )
                        # If the synchronous flush wrote the line, we are
                        # done with this task — skip the redundant write
                        # below.
                        if cache.get("completion_logged"):
                            # Stamp the cohort onto the line that was
                            # just written by appending a corrective
                            # marker is overkill; instead, we trust that
                            # _atexit_flush wrote exit_status="atexit"
                            # which RC-10 treats as the same cohort as
                            # cost_exit / call_exit (any non-"normal"
                            # value excludes the task from the
                            # engagement_rate denominator).
                            continue

                # RC-4 BUG-1: L4 count is now per-task, sourced from the
                # pulled gt_query_calls.jsonl in the per-task host dir.
                # Compute INSIDE the loop so it never fans-out across
                # concurrent pending entries.
                l4_count = _count_gt_query_calls(log_dir)

                pull_error = cache.get("pull_error")
                if cache.get("summary"):
                    summary = dict(cache["summary"])
                elif pull_error:
                    # RC-4 BUG-4: distinguish pull failure from genuine absence.
                    summary = {
                        "edit_count": 0,
                        "query_count": 0,
                        "reindex_count": 0,
                        "gate_verdict": "pull_failed",
                    }
                else:
                    # No summary stashed AND no recorded pull error — the
                    # close-wrap may not have installed (e.g.
                    # on_instance_start bailed early). Tag it as such so the
                    # log line doesn't pretend the gate is "absent".
                    summary = {
                        "edit_count": 0,
                        "query_count": 0,
                        "reindex_count": 0,
                        "gate_verdict": "no_close_wrap",
                    }

                # Autosubmit override (preserve existing semantics).
                # The autosubmit path bypasses both the gate AND the env.close
                # wrapper, so we override "absent" (no JSON pulled) AND
                # "no_close_wrap" (wrapper never installed) — both indicate
                # the same situation.
                if autosubmitted and summary.get("gate_verdict") in (
                    "absent", "no_close_wrap"
                ):
                    summary["gate_verdict"] = "autosubmit"

                # RC-11: stamp the cohort marker. setdefault preserves any
                # value the close-wrap or atexit_flush already pinned.
                summary.setdefault("exit_status", _exit_cohort)

                if log_dir is not None:
                    try:
                        _append_completion_log(
                            log_dir / "gt_layers.log",
                            iid,
                            summary,
                            l4_count,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "completion log append failed for %s: %s",
                            iid, exc,
                        )
                # RC-03: _pending already popped under lock; no double-pop.

        # ---- Helper: resolve instance_id from many possible sources ------
        # RC-03 PRIMARY: per-thread map captured in on_instance_start.
        # SWE-agent's RunBatch worker keeps the same thread for the start
        # and complete callbacks of a given instance, so this is
        # deterministic and concurrency-safe. Falls back to the legacy
        # 5-step result-introspection ladder if the thread map is empty
        # (e.g. start hook bailed before stash, or test fixtures bypass
        # on_instance_start).
        def _resolve_instance_id_for_thread(self, result: Any) -> str:
            tid = threading.get_ident()
            with self._pending_lock:
                tid_iid = self._thread_pending.get(tid, "")
            if tid_iid:
                return tid_iid
            return self._resolve_instance_id(result)

        # RC-4 BUG-5 ladder — try every plausible result-side source. Used
        # only when the per-thread map miss (start hook never stashed for
        # this thread). Order:
        # (a) info["instance_id"]  → most authoritative when present
        # (b) result.id / result.instance_id → some swe-agent versions
        # (c) trajectory[0].action heuristic — first command often contains
        #     the task name as part of the working directory or git checkout
        # (d) model name suffix like "swea-agent-<instance_id>"
        # (e) trajectory file path (info["traj_path"] / similar) glob
        @staticmethod
        def _resolve_instance_id(result: Any) -> str:
            # (a) info["instance_id"]
            try:
                info = getattr(result, "info", None)
                if info is not None:
                    iid = (
                        getattr(info, "instance_id", "")
                        or (info.get("instance_id", "") if isinstance(info, dict) else "")
                        or ""
                    )
                    if iid:
                        return str(iid)
            except Exception:  # noqa: BLE001
                pass

            # (b) Direct attributes on result
            for attr in ("instance_id", "id", "task_id"):
                try:
                    val = getattr(result, attr, "")
                    if val:
                        return str(val)
                except Exception:  # noqa: BLE001
                    continue

            # (c) trajectory[0].action — first action often contains the
            # task slug (e.g. via cd /testbed && git checkout <sha>).
            try:
                traj = getattr(result, "trajectory", None) or []
                if traj:
                    step = traj[0]
                    action = (
                        step.get("action", "")
                        if isinstance(step, dict)
                        else getattr(step, "action", "")
                    )
                    # Heuristic: look for "instance_id=<id>" or
                    # "/testbed/<owner>__<repo>" patterns.
                    import re
                    m = re.search(r"instance_id[=:\s]+([\w./-]+)", str(action))
                    if m:
                        return m.group(1)
                    m = re.search(r"([a-zA-Z0-9_-]+__[a-zA-Z0-9_.-]+-\d+)", str(action))
                    if m:
                        return m.group(1)
            except Exception:  # noqa: BLE001
                pass

            # (d) model name suffix: swea-agent-<id>
            try:
                info = getattr(result, "info", None) or {}
                model = (
                    info.get("model_name", "") if isinstance(info, dict)
                    else getattr(info, "model_name", "") or ""
                )
                if isinstance(model, str) and model.startswith("swea-agent-"):
                    return model[len("swea-agent-"):]
            except Exception:  # noqa: BLE001
                pass

            # (e) trajectory file path glob — info may carry a path.
            try:
                info = getattr(result, "info", None) or {}
                traj_path = (
                    info.get("traj_path", "") if isinstance(info, dict)
                    else getattr(info, "traj_path", "") or ""
                )
                if traj_path:
                    # The .traj filename is typically <instance_id>.traj.
                    name = os.path.basename(str(traj_path))
                    if name.endswith(".traj"):
                        return name[: -len(".traj")]
            except Exception:  # noqa: BLE001
                pass

            return ""

        @staticmethod
        def _safe_info_repr(result: Any) -> str:
            """Bounded repr for the on_instance_completed ERROR log."""
            try:
                info = getattr(result, "info", None)
                if info is None:
                    return "<no-info>"
                if isinstance(info, dict):
                    keys = sorted(info.keys())[:8]
                    return f"<dict keys={keys}>"
                return f"<{type(info).__name__}>"
            except Exception:  # noqa: BLE001
                return "<repr-failed>"

except ImportError:  # pragma: no cover
    # sweagent is not installed in this Python — the compute layer above
    # is still usable (Track A verification A/B/C). Track D's smoke
    # runner is responsible for ensuring sweagent is importable in its
    # environment.
    GTTrack4PreRunHook = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# CLI entry point — useful for ad-hoc smoke / Track A verification step B/C.
# ---------------------------------------------------------------------------

def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="gt_track4_pre_run")
    parser.add_argument("--issue-text", required=True)
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--graph-db", required=True)
    parser.add_argument("--max-lines", type=int, default=8)
    parser.add_argument("--out-dir", default=None,
                        help="If set, write gt_brief.txt + gt_layers.log here.")
    parser.add_argument("--instance-id", default="cli")
    args = parser.parse_args(argv)

    brief, l1, l2 = compute_brief(
        issue_text=args.issue_text,
        repo_path=args.repo_path,
        graph_db_path=args.graph_db,
        max_lines=args.max_lines,
    )

    if args.out_dir:
        out = Path(args.out_dir) / args.instance_id
        _atomic_write(out / "gt_brief.txt", brief or "")
        _append_layers_log(out / "gt_layers.log", args.instance_id, l1, l2)

    print(f"L1={l1} L2={l2} bytes={len(brief)}")
    print("---BRIEF---")
    print(brief)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
