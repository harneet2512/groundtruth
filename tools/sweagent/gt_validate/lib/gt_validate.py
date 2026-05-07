#!/usr/bin/env python3
"""gt_validate — read-only structural-diff check on a single edited file.

Reuses the structural-check logic from `gt_pre_finish_gate`
(tools/sweagent/gt_pre_finish_gate/lib/gt_pre_finish_gate.py:check_*) but in
read-only on-demand mode: takes ONE file_path, compares the worktree state
vs HEAD via `git show HEAD:<path>` + worktree read, and reports the same
three structural concerns the pre-finish gate raises.

The output is informational — never blocks. The pre-finish gate is the
authoritative blocker. Agent uses this mid-trajectory to catch issues
before submission.

Output:
  # gt_validate: <file_path>
  HALLUCINATED-IMPORT  +<line>  unresolved=<name>  module=<mod>
  CALLER-BLIND-EDIT    symbol=<name>  callers=<n>  (no test file in diff)
  CONTRACT-BREAK       symbol=<name>  before=(...)->...  after=(...)->...

Telemetry: appends one JSON line to $GT_INSTANCE_LOG_DIR/gt_validate_calls.jsonl.

Exit codes: 0 = success (incl. zero flags), 2 = bad usage / missing env,
            3 = graph.db missing or unreadable.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

TOOL = "gt_validate"
VERIFIED_RESOLUTIONS = frozenset({"same_file", "import", "name_match"})
# RC-04: legacy fallback. Runtime threshold comes from project_meta or live
# P50; see _conf_for(conn).
MIN_CONFIDENCE = 0.5

# RC-06: language-agnostic structural recognition. Used to decide whether
# gt_validate has language-specific checks (Python today) or only the
# language-agnostic graph-based blast-radius check (everything else with
# an extension we recognize).
_KNOWN_SOURCE_EXTS: frozenset[str] = frozenset({
    ".py", ".go", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".rs", ".rb", ".php", ".cs", ".kt", ".swift", ".scala",
})
_PER_LANG_CHECKS_AVAILABLE: frozenset[str] = frozenset({".py"})

PY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([\w\.]+)\s+import\s+([\w\*,\s]+)|import\s+([\w\.,\s]+))",
)
DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)
CLASS_RE = re.compile(r"^\s*class\s+(\w+)\s*[\(:]", re.MULTILINE)
SIG_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)\s*(?:->\s*([^:]+))?:",
    re.MULTILINE,
)
CLASS_BASE_RE = re.compile(r"^\s*class\s+(\w+)\s*\(([^)]*)\)\s*:", re.MULTILINE)


def _run(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            errors="backslashreplace", timeout=30,
        )
        return proc.returncode, proc.stdout
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def _file_text_after(repo: str, path: str) -> str:
    p = Path(repo) / path
    try:
        return p.read_text(errors="backslashreplace")
    except OSError:
        return ""


def _file_text_before(repo: str, path: str) -> str:
    rc, out = _run(["git", "show", f"HEAD:{path}"], cwd=repo)
    if rc != 0:
        return ""
    return out


def _file_diff(repo: str, path: str) -> str:
    rc, out = _run(["git", "diff", "HEAD", "--", path], cwd=repo)
    if rc != 0 or not out:
        rc, out = _run(["git", "diff", "--cached", "--", path], cwd=repo)
    return out


def _added_import_lines(diff_text: str) -> list[str]:
    out: list[str] = []
    for ln in diff_text.splitlines():
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        if ln.startswith("+"):
            body = ln[1:]
            if body.lstrip().startswith(("import ", "from ")):
                out.append(body)
    return out


def _parse_import_targets(import_line: str) -> list[tuple[str, str]]:
    m = PY_IMPORT_RE.match(import_line)
    if not m:
        return []
    if m.group(1):
        module = m.group(1)
        names = m.group(2) or ""
        out: list[tuple[str, str]] = []
        for n in names.split(","):
            n = n.strip().split(" as ")[0]
            if n and n != "*":
                out.append((module, n))
        return out
    if m.group(3):
        return [(n.strip().split(" as ")[0], n.strip().split(" as ")[0])
                for n in m.group(3).split(",") if n.strip()]
    return []


def _looks_local(name: str, db_files: set[str]) -> bool:
    top = name.split(".")[0]
    if not top:
        return False
    for f in db_files:
        fn = f.replace("\\", "/")
        if fn == top or fn.startswith(top + "/"):
            return True
        if f"/{top}/" in fn:
            return True
        if fn.endswith(f"/{top}.py") or fn == f"{top}.py":
            return True
    return False


def _node_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM nodes WHERE name = ? OR qualified_name = ? "
        "OR qualified_name LIKE ? LIMIT 1",
        (name, name, f"%.{name}"),
    )
    return cur.fetchone() is not None


def _is_test_file(path: str) -> bool:
    """RC-06: language-agnostic test-file detection.

    Recognized patterns by language:
      python:     test_foo.py, foo_test.py, conftest.py, /test/, /tests/
      javascript: foo.test.js, foo.spec.js, /__tests__/
      typescript: foo.test.ts, foo.spec.ts
      go:         foo_test.go (canonical)
      java:       FooTest.java, FooTests.java, src/test/java/...
      ruby:       foo_spec.rb, /spec/
      csharp:     FooTests.cs
      php:        FooTest.php
      rust:       /tests/ (integration tests)
    """
    norm_orig = path.replace("\\", "/")
    p = norm_orig.lower()
    base_orig = os.path.basename(norm_orig)
    base = base_orig.lower()
    if ("/test/" in p or "/tests/" in p
            or "/__tests__/" in p or "/spec/" in p or "/specs/" in p):
        return True
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    if base == "conftest.py":
        return True
    if base.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts",
                      ".test.jsx", ".test.tsx", ".spec.jsx", ".spec.tsx")):
        return True
    if base.endswith("_test.go"):
        return True
    if base.endswith("_spec.rb") or base.endswith("_test.rb"):
        return True
    if base_orig.endswith("Test.java") or base_orig.endswith("Tests.java"):
        return True
    if base_orig.endswith("Tests.cs") or base_orig.endswith("Test.cs"):
        return True
    if base_orig.endswith("Test.php") or base_orig.endswith("Tests.php"):
        return True
    return False


def _has_confidence(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT confidence FROM edges LIMIT 0")
        return True
    except sqlite3.OperationalError:
        return False


def _file_blast_radius(conn: sqlite3.Connection, file_path: str) -> int:
    """RC-06: language-agnostic file-level caller count.

    Sums CALLS edges into ANY node defined in `file_path`. Works for any
    language gt-index extracted (Python, Go, JS/TS, Java, Rust, …) — the
    edges are populated by the indexer regardless of source language.
    """
    has_conf = _has_confidence(conn)
    methods = tuple(sorted(VERIFIED_RESOLUTIONS))
    placeholders = ",".join("?" * len(methods))
    conf_clause = f" AND e.confidence >= {MIN_CONFIDENCE}" if has_conf else ""
    sql = f"""
        SELECT COUNT(*) FROM edges e
        JOIN nodes t ON e.target_id = t.id
        WHERE t.file_path = ? AND e.type = 'CALLS'
          AND e.resolution_method IN ({placeholders})
          {conf_clause}
    """
    try:
        return int(conn.execute(sql, (file_path, *methods)).fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def _resolve_min_confidence(conn: sqlite3.Connection) -> float:
    """RC-04: read per-repo min_confidence from project_meta; fall back to
    0.5 (brief-layer parity) when missing. Clamped to (0, 0.9] to prevent a
    degenerate index from over-filtering legitimate name_match edges."""
    try:
        row = conn.execute(
            "SELECT value FROM project_meta WHERE key = 'min_confidence'"
        ).fetchone()
        if row and row[0] is not None:
            try:
                v = float(row[0])
                if 0.0 < v <= 0.9:
                    return v
            except (TypeError, ValueError):
                pass
    except sqlite3.Error:
        pass
    return 0.5


_CONF_CACHE: dict[int, float] = {}


def _conf_for(conn: sqlite3.Connection) -> float:
    key = id(conn)
    cached = _CONF_CACHE.get(key)
    if cached is None:
        cached = _resolve_min_confidence(conn)
        _CONF_CACHE[key] = cached
    return cached


def _caller_count(conn: sqlite3.Connection, symbol: str) -> int:
    has_conf = _has_confidence(conn)
    methods = tuple(sorted(VERIFIED_RESOLUTIONS))
    placeholders = ",".join("?" * len(methods))
    conf_clause = f" AND e.confidence >= {_conf_for(conn)}" if has_conf else ""
    sql = f"""
        SELECT COUNT(*) FROM edges e
        JOIN nodes t ON e.target_id = t.id
        WHERE t.name = ? AND e.type = 'CALLS'
          AND e.resolution_method IN ({placeholders})
          {conf_clause}
    """
    try:
        return int(conn.execute(sql, (symbol, *methods)).fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def _changed_symbols(before: str, after: str) -> set[str]:
    before_names = set(DEF_RE.findall(before)) | set(CLASS_RE.findall(before))
    after_names = set(DEF_RE.findall(after)) | set(CLASS_RE.findall(after))
    return after_names if before != after else (after_names - before_names)


def _emit_telemetry(file_path: str, returned_lines: int, flags: int) -> None:
    log_dir = os.environ.get("GT_INSTANCE_LOG_DIR")
    if not log_dir:
        return
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        rec = {
            "tool": TOOL,
            "args": {"file_path": file_path},
            "flags": flags,
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


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].strip():
        print(f"usage: {TOOL} <file_path>", file=sys.stderr)
        _emit_telemetry(argv[1] if len(argv) > 1 else "", 0, 0)
        return 2
    file_path = argv[1].strip().replace("\\", "/")

    db_path = os.environ.get("GT_GRAPH_DB")
    if not db_path:
        print(f"{TOOL}: GT_GRAPH_DB not set", file=sys.stderr)
        _emit_telemetry(file_path, 0, 0)
        return 2
    if not Path(db_path).exists():
        print(f"{TOOL}: graph.db not found at {db_path}", file=sys.stderr)
        _emit_telemetry(file_path, 0, 0)
        return 3

    repo_root = os.environ.get("GT_REPO_ROOT") or os.getcwd()
    if not os.path.isdir(repo_root):
        print(f"{TOOL}: GT_REPO_ROOT not a directory: {repo_root}", file=sys.stderr)
        _emit_telemetry(file_path, 0, 0)
        return 2

    abs_file = os.path.join(repo_root, file_path)
    if not os.path.isfile(abs_file):
        print(f"# {TOOL}: {file_path}")
        print(f"# (file not in worktree at {abs_file}; nothing to validate)")
        _emit_telemetry(file_path, 2, 0)
        return 0

    try:
        # RC-04: dropped immutable=1 (writer can run concurrently). Add
        # PRAGMA integrity_check; surface db_corrupt as exit 4.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.execute("PRAGMA busy_timeout = 5000")
        ic = conn.execute("PRAGMA integrity_check").fetchone()
        if ic is None or ic[0] != "ok":
            print(f"{TOOL}: db_corrupt: {ic[0] if ic else 'unknown'}", file=sys.stderr)
            _emit_telemetry(file_path, 0, 0)
            return 4
    except sqlite3.Error as e:
        print(f"{TOOL}: cannot open graph.db: {e}", file=sys.stderr)
        _emit_telemetry(file_path, 0, 0)
        return 3
    conn.row_factory = sqlite3.Row
    _conf_for(conn)

    out_lines: list[str] = [f"# {TOOL}: {file_path}"]
    flags = 0
    try:
        # Cache db_files for hallucinated-import local-claim check.
        db_files: set[str] = set(
            r["file_path"] for r in conn.execute(
                "SELECT DISTINCT file_path FROM nodes"
            ).fetchall()
            if r["file_path"]
        )

        # RC-06: language-agnostic structural finding for non-Python files.
        # Previously gt_validate emitted a silent green-light on any
        # non-.py file. Now we explicitly disclose coverage and run the
        # graph-based blast-radius check (which is intrinsically language-
        # agnostic — caller counts come from graph.db edges populated by
        # the indexer regardless of source language).
        # Examples:
        #   foo.go (50 callers, no test)  -> BLAST-RADIUS finding emitted
        #   bar.ts (12 callers)            -> BLAST-RADIUS finding emitted
        #   util.rs (3 callers)            -> info note (under threshold)
        #   FooTest.java                   -> recognized as test file
        ext = os.path.splitext(file_path)[1].lower()
        if ext in _KNOWN_SOURCE_EXTS and ext not in _PER_LANG_CHECKS_AVAILABLE:
            norm = file_path.replace("\\", "/")
            blast = _file_blast_radius(conn, norm)
            if blast > 0:
                flags += 1
                out_lines.append(
                    f"BLAST-RADIUS         file={file_path}  callers={blast}  "
                    f"(language-agnostic: counted from graph.db edges; "
                    f"per-language def/import parsing for {ext} not yet "
                    f"available — Python-only checks below are skipped "
                    f"by design)"
                )
            else:
                out_lines.append(
                    f"# (no callers found in graph.db for {file_path}; "
                    f"per-language structural checks for {ext} not yet "
                    f"implemented — only graph-based BLAST-RADIUS ran)"
                )

        # Check 1 — only meaningful for python files.
        if file_path.endswith(".py"):
            diff = _file_diff(repo_root, file_path)
            for line in _added_import_lines(diff):
                for module, name in _parse_import_targets(line):
                    if not _looks_local(module, db_files):
                        continue
                    short = name.split(".")[-1]
                    if not _node_exists(conn, short):
                        flags += 1
                        out_lines.append(
                            f"HALLUCINATED-IMPORT  +{line.strip()}  unresolved={name}  module={module}"
                        )

        # Check 2 — caller-blind edit.
        if file_path.endswith(".py") and not _is_test_file(file_path):
            before = _file_text_before(repo_root, file_path)
            after = _file_text_after(repo_root, file_path)
            for sym in _changed_symbols(before, after):
                n = _caller_count(conn, sym)
                if n >= 3:
                    flags += 1
                    out_lines.append(
                        f"CALLER-BLIND-EDIT    symbol={sym}  callers={n}  "
                        f"(no test file edited alongside this change)"
                    )

        # Check 3 — contract break (signature / class bases).
        if file_path.endswith(".py"):
            before = _file_text_before(repo_root, file_path)
            after = _file_text_after(repo_root, file_path)
            if before and after:
                before_sigs = {
                    m.group(1): (m.group(2).strip(), (m.group(3) or "").strip())
                    for m in SIG_RE.finditer(before)
                }
                after_sigs = {
                    m.group(1): (m.group(2).strip(), (m.group(3) or "").strip())
                    for m in SIG_RE.finditer(after)
                }
                for name, (params, ret) in after_sigs.items():
                    if name in before_sigs and before_sigs[name] != (params, ret):
                        flags += 1
                        out_lines.append(
                            f"CONTRACT-BREAK       symbol={name}  "
                            f"before=({before_sigs[name][0]})->{before_sigs[name][1]}  "
                            f"after=({params})->{ret}"
                        )
                before_classes = {
                    m.group(1): m.group(2).strip()
                    for m in CLASS_BASE_RE.finditer(before)
                }
                after_classes = {
                    m.group(1): m.group(2).strip()
                    for m in CLASS_BASE_RE.finditer(after)
                }
                for name, bases in after_classes.items():
                    if name in before_classes and before_classes[name] != bases:
                        flags += 1
                        out_lines.append(
                            f"CONTRACT-BREAK       symbol={name}  "
                            f"kind=class_bases  before={before_classes[name]}  after={bases}"
                        )
    except sqlite3.OperationalError as e:
        print(f"{TOOL}: query failed: {e}", file=sys.stderr)
        _emit_telemetry(file_path, len(out_lines), flags)
        return 3
    finally:
        conn.close()

    if flags == 0:
        out_lines.append("# (no structural flags raised — file looks consistent with graph.db)")
    else:
        out_lines.append(f"# (total flags: {flags} — informational; the pre-finish gate is authoritative)")
    print("\n".join(out_lines))
    _emit_telemetry(file_path, len(out_lines), flags)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
