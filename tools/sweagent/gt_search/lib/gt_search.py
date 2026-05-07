#!/usr/bin/env python3
"""gt_search — consolidated structural search bundle.

Discriminator-driven dispatch. Replaces 6 prior bundles:
  gt_search_class, gt_search_method, gt_search_method_in_class,
  gt_search_method_in_file, gt_search_code, gt_search_code_in_file.

Usage:
  gt_search <kind> <query> [<scope>]

Where <kind> is one of:
  class                — query=name; scope=ignored
  method               — query=name; scope=ignored
  method_in_class      — query=method; scope=class_name (REQUIRED)
  method_in_file       — query=method; scope=file_path  (REQUIRED)
  code                 — query=snippet; scope=ignored
  code_in_file         — query=snippet; scope=file_path (REQUIRED)

Output mirrors the prior per-bundle format exactly so existing parsers
(verifier, agent prompts) keep working unchanged. Each invocation appends
ONE JSON line to $GT_INSTANCE_LOG_DIR/gt_search_calls.jsonl with
{tool, kind, args, returned_lines, ts}.

Exit codes:
  0  success (incl. zero results)
  2  bad usage / missing required scope / missing GT_GRAPH_DB
  3  graph.db missing or unreadable
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

TOOL = "gt_search"

VALID_KINDS = (
    "class",
    "method",
    "method_in_class",
    "method_in_file",
    "code",
    "code_in_file",
)
KINDS_REQUIRING_SCOPE = frozenset(
    {"method_in_class", "method_in_file", "code_in_file"}
)
GRAPH_DB_KINDS = frozenset(
    {"class", "method", "method_in_class", "method_in_file"}
)

MAX_HITS = 20
MAX_CODE_HITS = 30
MAX_FILE_SIZE = 1_000_000

TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx",
    ".rb", ".php", ".kt", ".scala", ".swift", ".cs", ".lua", ".ex", ".exs",
    ".html", ".htm", ".md", ".rst", ".txt",
    ".yaml", ".yml", ".toml", ".json", ".ini", ".cfg",
}
# RC-06: SKIP_DIR_NAMES used to silently exclude every directory named
# test/tests/Tests/__tests__ from `kind=code` grep. That made gt_search
# blind to test code on TDD repos and on SWE-bench tasks where the fix
# touches a test (~50% of SWE-bench-Live tasks). Default is now
# include-tests; pass --exclude-tests to restore the prior filter.
SKIP_DIR_NAMES = {
    "node_modules", "__pycache__", "dist", "build", ".venv", "venv",
    ".tox", ".pytest_cache", ".mypy_cache", ".ruff_cache", "site-packages",
    ".eggs", "egg-info", "target", ".idea", ".vscode",
}
# Legacy test-dir filter (only applied when --exclude-tests is passed).
TEST_DIR_NAMES = frozenset({"test", "tests", "Tests", "__tests__"})
TEST_PATH_TOKENS = ("/test/", "/tests/", "/__tests__/")


# ── Helpers ──────────────────────────────────────────────────────────────────
def _shorten(s: str, n: int = 110) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def _is_test_path(rel: str) -> bool:
    rel_n = "/" + rel.replace("\\", "/").lstrip("/")
    if any(tok in rel_n for tok in TEST_PATH_TOKENS):
        return True
    base = os.path.basename(rel)
    if base.startswith("test_"):
        return True
    if base.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts")):
        return True
    return False


def _emit_telemetry(
    kind: str,
    query: str,
    scope: str,
    returned_lines: int,
) -> None:
    log_dir = os.environ.get("GT_INSTANCE_LOG_DIR")
    if not log_dir:
        return
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        rec = {
            "tool": TOOL,
            "kind": kind,
            "args": {"query": query, "scope": scope},
            "returned_lines": returned_lines,
            "ts": time.time(),
        }
        with open(Path(log_dir) / f"{TOOL}_calls.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except OSError:
        pass


def _open_graph_db() -> tuple[sqlite3.Connection | None, int]:
    db_path = os.environ.get("GT_GRAPH_DB")
    if not db_path:
        print(f"{TOOL}: GT_GRAPH_DB not set", file=sys.stderr)
        return None, 2
    if not Path(db_path).exists():
        print(f"{TOOL}: graph.db not found at {db_path}", file=sys.stderr)
        return None, 3
    try:
        # RC-04: dropped immutable=1 (writer can run concurrently). Add
        # PRAGMA integrity_check; surface db_corrupt as exit 4.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.execute("PRAGMA busy_timeout = 5000")
        ic = conn.execute("PRAGMA integrity_check").fetchone()
        if ic is None or ic[0] != "ok":
            print(f"{TOOL}: db_corrupt: {ic[0] if ic else 'unknown'}", file=sys.stderr)
            return None, 4
    except sqlite3.Error as e:
        print(f"{TOOL}: cannot open graph.db: {e}", file=sys.stderr)
        return None, 3
    conn.row_factory = sqlite3.Row
    return conn, 0


# ── Mode: class ──────────────────────────────────────────────────────────────
def _mode_class(query: str) -> tuple[list[str], int]:
    conn, rc = _open_graph_db()
    if conn is None:
        return [], rc
    try:
        rows = conn.execute(
            "SELECT name, qualified_name, file_path, start_line, end_line "
            "FROM nodes WHERE LOWER(name) = LOWER(?) "
            "AND label IN ('Class', 'Interface') "
            "AND (is_test = 0 OR is_test IS NULL) "
            "ORDER BY file_path, start_line LIMIT ?",
            (query, MAX_HITS),
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"{TOOL}: query failed: {e}", file=sys.stderr)
        conn.close()
        return [], 3
    conn.close()

    out: list[str] = [f"# gt_search class: {query} — {len(rows)} hit(s)"]
    if not rows:
        out.append(
            f"# (no class named '{query}' in graph.db; "
            "try kind=method or kind=code)"
        )
    else:
        for r in rows:
            qn = r["qualified_name"] or r["name"]
            sl = r["start_line"] or 0
            el = r["end_line"] or 0
            out.append(f"{r['file_path']}:{sl}-{el}  {qn}")
    return out, 0


# ── Mode: method ─────────────────────────────────────────────────────────────
def _mode_method(query: str) -> tuple[list[str], int]:
    conn, rc = _open_graph_db()
    if conn is None:
        return [], rc
    try:
        rows = conn.execute(
            "SELECT name, qualified_name, file_path, start_line, end_line, signature "
            "FROM nodes WHERE LOWER(name) = LOWER(?) "
            "AND label IN ('Function', 'Method') "
            "AND (is_test = 0 OR is_test IS NULL) "
            "ORDER BY file_path, start_line LIMIT ?",
            (query, MAX_HITS),
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"{TOOL}: query failed: {e}", file=sys.stderr)
        conn.close()
        return [], 3
    conn.close()

    out: list[str] = [f"# gt_search method: {query} — {len(rows)} hit(s)"]
    if not rows:
        out.append(
            f"# (no function/method named '{query}' in graph.db; "
            "try kind=class or kind=code)"
        )
    else:
        for r in rows:
            qn = r["qualified_name"] or r["name"]
            sl = r["start_line"] or 0
            el = r["end_line"] or 0
            sig = _shorten(r["signature"] or "", 90)
            sig_part = f"  {sig}" if sig else ""
            out.append(f"{r['file_path']}:{sl}-{el}  {qn}{sig_part}")
    return out, 0


# ── Mode: method_in_class ────────────────────────────────────────────────────
def _mode_method_in_class(query: str, class_name: str) -> tuple[list[str], int]:
    conn, rc = _open_graph_db()
    if conn is None:
        return [], rc
    try:
        class_rows = conn.execute(
            "SELECT id, name, file_path FROM nodes "
            "WHERE LOWER(name) = LOWER(?) "
            "AND label IN ('Class', 'Interface') "
            "AND (is_test = 0 OR is_test IS NULL) "
            "ORDER BY file_path, start_line LIMIT 20",
            (class_name,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"{TOOL}: query failed: {e}", file=sys.stderr)
        conn.close()
        return [], 3

    if not class_rows:
        conn.close()
        return [
            f"# gt_search method_in_class: {query} in {class_name} — 0 hit(s)",
            f"# (try kind=class to confirm the class name)",
        ], 0

    class_ids = tuple(r["id"] for r in class_rows)
    placeholders = ",".join("?" * len(class_ids))
    try:
        method_rows = conn.execute(
            f"SELECT name, qualified_name, file_path, start_line, end_line, signature, parent_id "
            f"FROM nodes WHERE LOWER(name) = LOWER(?) "
            f"AND label IN ('Function', 'Method') "
            f"AND parent_id IN ({placeholders}) "
            f"AND (is_test = 0 OR is_test IS NULL) "
            f"ORDER BY file_path, start_line LIMIT ?",
            (query, *class_ids, MAX_HITS),
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"{TOOL}: query failed: {e}", file=sys.stderr)
        conn.close()
        return [], 3
    conn.close()

    out: list[str] = [
        f"# gt_search method_in_class: {query} in {class_name} — "
        f"{len(method_rows)} hit(s)"
    ]
    if not method_rows:
        out.append(
            f"# (class '{class_name}' resolved to {len(class_rows)} candidate(s) "
            f"but none has method '{query}' as a direct child)"
        )
        for r in class_rows[:5]:
            out.append(f"#   class candidate: {r['file_path']}  ({r['name']})")
    else:
        for r in method_rows:
            qn = r["qualified_name"] or r["name"]
            sl = r["start_line"] or 0
            el = r["end_line"] or 0
            sig = _shorten(r["signature"] or "", 90)
            sig_part = f"  {sig}" if sig else ""
            out.append(f"{r['file_path']}:{sl}-{el}  {qn}{sig_part}")
    return out, 0


# ── Mode: method_in_file ─────────────────────────────────────────────────────
def _mode_method_in_file(query: str, file_path: str) -> tuple[list[str], int]:
    file_path = file_path.replace("\\", "/")
    conn, rc = _open_graph_db()
    if conn is None:
        return [], rc
    try:
        rows = conn.execute(
            "SELECT name, qualified_name, file_path, start_line, end_line, signature "
            "FROM nodes WHERE file_path = ? "
            "AND label IN ('Function', 'Method') "
            "AND LOWER(name) = LOWER(?) "
            "ORDER BY start_line LIMIT ?",
            (file_path, query, MAX_HITS),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT name, qualified_name, file_path, start_line, end_line, signature "
                "FROM nodes WHERE file_path LIKE ? "
                "AND label IN ('Function', 'Method') "
                "AND LOWER(name) = LOWER(?) "
                "ORDER BY file_path, start_line LIMIT ?",
                (f"%{file_path}", query, MAX_HITS),
            ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"{TOOL}: query failed: {e}", file=sys.stderr)
        conn.close()
        return [], 3
    conn.close()

    out: list[str] = [
        f"# gt_search method_in_file: {query} in {file_path} — "
        f"{len(rows)} hit(s)"
    ]
    if not rows:
        out.append(
            f"# (no method named '{query}' in '{file_path}'; "
            f"try kind=method '{query}' to scan all files)"
        )
    else:
        for r in rows:
            qn = r["qualified_name"] or r["name"]
            sl = r["start_line"] or 0
            el = r["end_line"] or 0
            sig = _shorten(r["signature"] or "", 90)
            sig_part = f"  {sig}" if sig else ""
            out.append(f"{r['file_path']}:{sl}-{el}  {qn}{sig_part}")
    return out, 0


# ── Mode: code ───────────────────────────────────────────────────────────────
def _iter_files(root: str, *, exclude_tests: bool = False):
    """RC-06: walk source files, optionally skipping test directories.

    Default `exclude_tests=False` keeps test/, tests/, Tests/, __tests__/
    in the walk — matching the new include-tests-by-default policy. When
    `exclude_tests=True`, those directories are pruned from the walk too.
    """
    skip = SKIP_DIR_NAMES if not exclude_tests else (SKIP_DIR_NAMES | TEST_DIR_NAMES)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if d not in skip and not d.startswith(".")
        ]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in TEXT_EXTENSIONS:
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.getsize(full) > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            yield full


def _mode_code(query: str, *, exclude_tests: bool = False) -> tuple[list[str], int]:
    if len(query) < 3:
        print(
            f"{TOOL}: snippet must be 3+ chars (got {len(query)})",
            file=sys.stderr,
        )
        return [], 2
    repo_root = os.environ.get("GT_REPO_ROOT") or os.getcwd()
    if not os.path.isdir(repo_root):
        print(
            f"{TOOL}: GT_REPO_ROOT not a directory: {repo_root}",
            file=sys.stderr,
        )
        return [], 2

    hits: list[tuple[str, int, str]] = []
    for full in _iter_files(repo_root, exclude_tests=exclude_tests):
        rel = os.path.relpath(full, repo_root).replace("\\", "/")
        # Default include-tests: walk through test paths. When
        # exclude_tests=True, drop matches in test files too.
        if exclude_tests and _is_test_path(rel):
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if query in line:
                        hits.append((rel, lineno, line.rstrip("\n")))
                        if len(hits) >= MAX_CODE_HITS:
                            break
        except OSError:
            continue
        if len(hits) >= MAX_CODE_HITS:
            break

    suffix = " (capped)" if len(hits) >= MAX_CODE_HITS else ""
    out: list[str] = [
        f"# gt_search code: '{query}' under {repo_root} — "
        f"{len(hits)} hit(s){suffix}"
    ]
    if not hits:
        out.append(
            f"# (no source-file lines contain '{query}'; "
            "try kind=code_in_file or relax the snippet)"
        )
    else:
        for rel, lineno, text in hits:
            out.append(f"{rel}:{lineno}: {_shorten(text, 160)}")
    return out, 0


# ── Mode: code_in_file ───────────────────────────────────────────────────────
def _mode_code_in_file(query: str, file_arg: str) -> tuple[list[str], int]:
    if len(query) < 3:
        print(
            f"{TOOL}: snippet must be 3+ chars (got {len(query)})",
            file=sys.stderr,
        )
        return [], 2
    file_arg = file_arg.replace("\\", "/")
    repo_root = os.environ.get("GT_REPO_ROOT") or os.getcwd()
    if os.path.isabs(file_arg):
        candidate = file_arg
    else:
        candidate = os.path.join(repo_root, file_arg)
    if not os.path.isfile(candidate):
        return [
            f"# gt_search code_in_file: '{query}' in {file_arg} — 0 hit(s)",
            f"# (file not found under {repo_root}; "
            "check the path printed by kind=class / kind=method)",
        ], 0
    try:
        if os.path.getsize(candidate) > MAX_FILE_SIZE:
            return [
                f"# gt_search code_in_file: '{query}' in {file_arg} — 0 hit(s)",
                "# (file > 1 MB; not searched)",
            ], 0
    except OSError:
        pass

    hits: list[tuple[int, str]] = []
    try:
        with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                if query in line:
                    hits.append((lineno, line.rstrip("\n")))
                    if len(hits) >= MAX_CODE_HITS:
                        break
    except OSError as e:
        print(f"{TOOL}: cannot read {candidate}: {e}", file=sys.stderr)
        return [], 2

    suffix = " (capped)" if len(hits) >= MAX_CODE_HITS else ""
    out: list[str] = [
        f"# gt_search code_in_file: '{query}' in {file_arg} — "
        f"{len(hits)} hit(s){suffix}"
    ]
    if not hits:
        out.append(f"# (no lines contain '{query}' in this file)")
    else:
        for lineno, text in hits:
            out.append(f"{lineno}: {_shorten(text, 160)}")
    return out, 0


# ── Main ─────────────────────────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    # RC-06: extract --include-tests / --exclude-tests flag (filtered out
    # before positional arg parsing). Default is include-tests so the
    # agent sees test code by default — fix-touches-tests is the common
    # case on SWE-bench-Live and on TDD repos generally.
    raw_args = list(argv[1:])
    exclude_tests = False
    filtered: list[str] = []
    for tok in raw_args:
        if tok == "--exclude-tests":
            exclude_tests = True
        elif tok == "--include-tests":
            exclude_tests = False
        else:
            filtered.append(tok)

    if len(filtered) < 2:
        print(
            f"usage: {TOOL} <kind> <query> [<scope>] [--include-tests|--exclude-tests]\n"
            f"  kind = one of: {', '.join(VALID_KINDS)}",
            file=sys.stderr,
        )
        _emit_telemetry(
            filtered[0] if len(filtered) > 0 else "",
            filtered[1] if len(filtered) > 1 else "",
            filtered[2] if len(filtered) > 2 else "",
            0,
        )
        return 2

    kind = filtered[0].strip()
    query = filtered[1].strip() if filtered[1] else ""
    scope = (filtered[2].strip() if len(filtered) > 2 and filtered[2] else "")

    if kind not in VALID_KINDS:
        print(
            f"{TOOL}: invalid kind '{kind}'. Valid: {', '.join(VALID_KINDS)}",
            file=sys.stderr,
        )
        _emit_telemetry(kind, query, scope, 0)
        return 2

    if not query:
        print(f"{TOOL}: query is required", file=sys.stderr)
        _emit_telemetry(kind, query, scope, 0)
        return 2

    if kind in KINDS_REQUIRING_SCOPE and not scope:
        print(
            f"{TOOL}: kind={kind} requires a 3rd argument "
            f"({'class_name' if kind == 'method_in_class' else 'file_path'})",
            file=sys.stderr,
        )
        _emit_telemetry(kind, query, scope, 0)
        return 2

    # Dispatch.
    if kind == "class":
        out_lines, rc = _mode_class(query)
    elif kind == "method":
        out_lines, rc = _mode_method(query)
    elif kind == "method_in_class":
        out_lines, rc = _mode_method_in_class(query, scope)
    elif kind == "method_in_file":
        out_lines, rc = _mode_method_in_file(query, scope)
    elif kind == "code":
        out_lines, rc = _mode_code(query, exclude_tests=exclude_tests)
    elif kind == "code_in_file":
        out_lines, rc = _mode_code_in_file(query, scope)
    else:  # pragma: no cover — already validated above.
        out_lines, rc = [], 2

    if out_lines:
        print("\n".join(out_lines))
    _emit_telemetry(kind, query, scope, len(out_lines))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
