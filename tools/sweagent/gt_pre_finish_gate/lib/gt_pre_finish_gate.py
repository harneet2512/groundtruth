#!/usr/bin/env python3
"""gt_pre_finish_gate — L5 structural pre-finish gate for SWE-agent.

This script REPLACES the default `submit` tool. Wrap mechanism is identical
to tools/review_on_submit_m/bin/submit: this script either prints a warning
and exits 0 (= no submit; agent sees the warning text), or prints the
<<SWE_AGENT_SUBMISSION>> markers + the staged patch (= submission committed).

Checks (all repo-agnostic, language-agnostic where graph.db has data):

  HALLUCINATED-IMPORT
    For each Python file in the diff, parse new `import X` / `from X import Y`
    statements added by the edit. For each imported name, check that some
    node in graph.db has matching name OR qualified_name. Unresolved imports
    are flagged (high false-negative tolerance: stdlib + 3rd-party libs are
    legitimately not in graph.db, so we only flag local-looking names — i.e.
    names that share a top-level component with any file_path in nodes).

  CALLER-BLIND-EDIT
    For each function/class symbol whose body changed in the diff, count
    callers via the edges table (CALLS, resolution in VERIFIED_RESOLUTIONS,
    confidence >= 0.7). If callers >= 3 AND no test file (path containing
    /test/ or test_*.py / *_test.py) is among the edited files, flag.

  CONTRACT-BREAK (structural invariants)
    Detect signature changes via regex on `def NAME(...)` or `class NAME(...)`
    lines in the diff. If the parameter list or class bases change, flag.

Soft-escape:
  After 3 consecutive blocks for the same instance (counter at
  $GT_INSTANCE_LOG_DIR/gt_finish_attempts.json), the 4th invocation emits
  the warning to stderr but proceeds with submission. `submit -f` forces
  bypass on any attempt.

Telemetry:
  Always writes $GT_INSTANCE_LOG_DIR/gt_pre_finish_gate.json with the full
  verdict + per-check breakdown for Track D's [GT_LAYERS] L5=<pass|warn|fail>
  cell. (pass = no flags, warn = flagged but soft-escaped or force-submitted,
  fail/blocked = flagged and submission held back.)

Exit:
  Always exit 0 on success path (the agent reads stdout). Exit 1 only on
  hard internal errors so SWE-agent surfaces the failure.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────
VERIFIED_RESOLUTIONS = frozenset({"same_file", "import", "name_match"})
# RC-04: legacy fallback. Runtime threshold loaded from graph.db
# project_meta.min_confidence (writes via gt-index) with live-P50 + 0.5
# fallbacks. See _conf_for(conn).
MIN_CONFIDENCE = 0.5
MAX_BLOCKS = 3  # soft-escape threshold

SUBMISSION_MARKER = "<<SWE_AGENT_SUBMISSION>>"


# ── RC-04: per-repo MIN_CONFIDENCE ──────────────────────────────────────────
def _resolve_min_confidence(conn: sqlite3.Connection) -> float:
    """Read per-repo min_confidence from project_meta; fall back to 0.5
    (brief-layer parity) when missing. Clamped to (0, 0.9] to prevent a
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

# ── Language dispatch (RC-06) ────────────────────────────────────────────────
# Per-language structural support. Each entry maps a file extension to:
#   - "name": short language label for telemetry / log lines.
#   - "structural": True if at least one check_* predicate has a real
#     implementation for this language. False means we honestly skip but
#     log the rationale (avoids the silent green-light failure mode).
#
# Goal: every non-Python edit either runs the same predicate using that
# language's syntax OR emits an explicit "skip with rationale" log line so
# the operator can see L5 disengaged. The dispatch table is repo-agnostic
# and language-agnostic — adding Kotlin or Swift is a one-line addition.
LANG_BY_EXT: dict[str, dict[str, object]] = {
    ".py": {"name": "python", "structural": True},
    ".go": {"name": "go", "structural": True},
    ".js": {"name": "javascript", "structural": True},
    ".jsx": {"name": "javascript", "structural": True},
    ".ts": {"name": "typescript", "structural": True},
    ".tsx": {"name": "typescript", "structural": True},
    ".mjs": {"name": "javascript", "structural": True},
    ".cjs": {"name": "javascript", "structural": True},
    ".java": {"name": "java", "structural": True},
    ".rs": {"name": "rust", "structural": True},
    ".rb": {"name": "ruby", "structural": False},
    ".php": {"name": "php", "structural": False},
    ".cs": {"name": "csharp", "structural": False},
    ".kt": {"name": "kotlin", "structural": False},
    ".swift": {"name": "swift", "structural": False},
    ".scala": {"name": "scala", "structural": False},
}


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _lang_for(path: str) -> dict[str, object] | None:
    """Return the language record for a path, or None if unsupported."""
    return LANG_BY_EXT.get(_ext(path))


def _log_skip(check: str, path: str, reason: str) -> None:
    """Emit a 'skip with rationale' line so the operator can see L5
    disengaged on a given file. Visible in gt_layers.log via stderr."""
    sys.stderr.write(
        f"<gt-pre-finish-gate> SKIP {check} on {path}: {reason}\n"
    )

# ── Paths ────────────────────────────────────────────────────────────────────
def _instance_log_dir() -> Path | None:
    p = os.environ.get("GT_INSTANCE_LOG_DIR")
    if not p:
        return None
    out = Path(p)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return out


def _attempts_path() -> Path | None:
    d = _instance_log_dir()
    return d / "gt_finish_attempts.json" if d else None


def _history_path() -> Path | None:
    """JSONL log of every submit attempt — one entry per call. Survives
    across attempts so the gate can read prior diff hashes and detect
    no-progress retries.
    """
    d = _instance_log_dir()
    return d / "gt_gate_history.jsonl" if d else None


def _verdict_path() -> Path | None:
    d = _instance_log_dir()
    return d / "gt_pre_finish_gate.json" if d else None


def _read_attempts() -> int:
    p = _attempts_path()
    if not p or not p.exists():
        return 0
    try:
        return int(json.loads(p.read_text()).get("blocks", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def _write_attempts(n: int) -> None:
    p = _attempts_path()
    if not p:
        return
    try:
        p.write_text(json.dumps({"blocks": n, "ts": time.time()}))
    except OSError:
        pass


def _read_history() -> list[dict]:
    """Return the chronological list of prior gate attempts (oldest first).

    Empty list if no history file or malformed lines (best-effort: skip
    bad lines, do not raise).
    """
    p = _history_path()
    if not p or not p.exists():
        return []
    out: list[dict] = []
    try:
        for raw in p.read_text().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except (ValueError, json.JSONDecodeError):
                # Skip malformed line; do not lose prior history.
                continue
    except OSError:
        return []
    return out


def _append_history(entry: dict) -> None:
    """Append one JSON object to gt_gate_history.jsonl (one line)."""
    p = _history_path()
    if not p:
        return
    try:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _write_verdict(verdict: dict) -> None:
    p = _verdict_path()
    if not p:
        return
    try:
        p.write_text(json.dumps(verdict, indent=2))
    except OSError:
        pass


# ── Diff collection ──────────────────────────────────────────────────────────
def _run(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            errors="backslashreplace", timeout=30,
        )
        return proc.returncode, proc.stdout
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def _repo_root() -> str:
    # Mirror the registry pattern used by review_on_submit_m: prefer $ROOT,
    # then cwd. Tests pass cwd via $GT_GATE_CWD.
    return os.environ.get("GT_GATE_CWD") or os.environ.get("ROOT") or os.getcwd()


def collect_edited_files(repo: str) -> list[str]:
    # Include tracked changes (modified/staged) plus untracked files. The
    # diff-only variant misses untracked scratch (str_replace_editor creates
    # files in the worktree without staging), which made check_scratch_files
    # never run because main() short-circuits when this list is empty.
    # ``git status --porcelain`` surfaces both, in one call, with universal
    # git semantics (no Live-Lite-specific paths).
    files: list[str] = []
    seen: set[str] = set()
    rc, out = _run(["git", "diff", "--name-only", "HEAD"], cwd=repo)
    if rc != 0:
        # Could be no commits yet; try staged-only
        rc, out = _run(["git", "diff", "--cached", "--name-only"], cwd=repo)
    for ln in out.splitlines():
        path = ln.strip()
        if path and path not in seen:
            files.append(path)
            seen.add(path)
    # Now merge in untracked + worktree-modified entries from porcelain.
    rc2, out2 = _run(["git", "status", "--porcelain"], cwd=repo)
    if rc2 == 0 and out2:
        for raw in out2.splitlines():
            if len(raw) < 4:
                continue
            status = raw[:2]
            path = raw[3:]
            # Skip deletions and renames (rename arrow = "orig -> new").
            if "D" in status:
                continue
            if " -> " in path:
                continue
            # Strip surrounding quotes git adds for paths with special chars.
            if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
                path = path[1:-1]
            if path and path not in seen:
                files.append(path)
                seen.add(path)
    return files


def file_diff(repo: str, path: str) -> str:
    rc, out = _run(["git", "diff", "HEAD", "--", path], cwd=repo)
    if rc != 0 or not out:
        rc, out = _run(["git", "diff", "--cached", "--", path], cwd=repo)
    return out


def file_text_after(repo: str, path: str) -> str:
    p = Path(repo) / path
    try:
        return p.read_text(errors="backslashreplace")
    except OSError:
        return ""


def file_text_before(repo: str, path: str) -> str:
    rc, out = _run(["git", "show", f"HEAD:{path}"], cwd=repo)
    if rc != 0:
        return ""
    return out


def compute_diff_hash(repo: str, edited: list[str]) -> str:
    """Stable sha256 over the worktree state the gate is about to evaluate.

    Hashes the concatenation of (path, file_diff(path)) for tracked edits
    plus (path, file_text_after(path)) for files with no diff (untracked).
    The latter case covers str_replace_editor-created files that ``git
    diff`` returns empty for. Files are sorted to make the hash
    permutation-invariant, so the order ``collect_edited_files`` returns
    them in does not perturb the result.

    A diff hash is "no progress" when the agent re-submits without
    changing any byte of any edited file — covers the failure mode where
    multiple submit attempts produce byte-identical diffs.
    """
    h = hashlib.sha256()
    for path in sorted(edited):
        d = file_diff(repo, path)
        if not d:
            # Untracked / new file: ``git diff`` is empty. Hash the
            # current contents instead, otherwise creating a brand-new
            # repro_*.py and resubmitting unchanged would look like
            # "no progress" only because git diff doesn't see it yet.
            d = file_text_after(repo, path)
        h.update(path.encode("utf-8", errors="replace"))
        h.update(b"\0")
        h.update(d.encode("utf-8", errors="replace"))
        h.update(b"\0")
    return h.hexdigest()


# ── Check 1: HALLUCINATED-IMPORT ─────────────────────────────────────────────
PY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([\w\.]+)\s+import\s+([\w\*,\s]+)|import\s+([\w\.,\s]+))",
)

# RC-06: per-language import-line detector. Returns the import-statement
# bodies on added (`+`) lines, with each language's syntax respected.
# Anti-benchmaxxing: each language's prefix is the canonical import keyword;
# nothing here is repo-specific.
_IMPORT_PREFIXES_BY_LANG: dict[str, tuple[str, ...]] = {
    "python":     ("import ", "from "),
    "go":         ("import ",),                       # `import "x"` and `import (`
    "javascript": ("import ", "const ", "require("),  # ES + CJS
    "typescript": ("import ",),
    "java":       ("import ",),
    "rust":       ("use ", "extern crate "),
}

# RC-06: per-language import-target parser. Each returns a list of
# (module, name) tuples (matching PY semantics so the rest of the
# pipeline is identical regardless of source language).
GO_IMPORT_RE = re.compile(r'import\s+(?:[\w]+\s+)?"([^"]+)"')
JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[^'"]+from\s+)?['"]([^'"]+)['"]"""
    r"""|require\(['"]([^'"]+)['"]\))""",
)
JAVA_IMPORT_RE = re.compile(r"import\s+(?:static\s+)?([\w\.]+)\s*;")
RUST_USE_RE = re.compile(r"use\s+([\w:]+)")


def _added_import_lines_for_lang(diff_text: str, lang: str) -> list[str]:
    """Like _added_import_lines but uses the language's import-keyword set.

    Examples:
      python     -> 'import os', 'from x import y'
      go         -> 'import "fmt"' or block lines starting with '"x"'
      javascript -> 'import x from "y"', 'const x = require("y")'
      java       -> 'import com.foo.Bar;'
      rust       -> 'use foo::bar;'
    """
    prefixes = _IMPORT_PREFIXES_BY_LANG.get(lang, ("import ",))
    out: list[str] = []
    for ln in diff_text.splitlines():
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        if not ln.startswith("+"):
            continue
        body = ln[1:].lstrip()
        if any(body.startswith(p) for p in prefixes):
            out.append(ln[1:])
            continue
        # Go block-import: lines inside `import (...)` are bare `"path"` strings.
        # We capture them too — they survive the per-language parser.
        if lang == "go" and body.startswith('"') and body.rstrip().endswith('"'):
            out.append(ln[1:])
    return out


def _parse_import_targets(import_line: str) -> list[tuple[str, str]]:
    """Return [(module, name)] tuples for a Python import statement.

    For `from X import Y, Z` → [('X', 'Y'), ('X', 'Z')]
    For `import X.Y`        → [('X.Y', 'X.Y')]
    The (module, name) pair lets callers test:
      - module looks local → in-repo claim
      - name not in graph  → unresolved claim (i.e. hallucinated)
    """
    m = PY_IMPORT_RE.match(import_line)
    if not m:
        return []
    if m.group(1):  # from MODULE import NAMES
        module = m.group(1)
        names = m.group(2) or ""
        out: list[tuple[str, str]] = []
        for n in names.split(","):
            n = n.strip().split(" as ")[0]
            if n and n != "*":
                out.append((module, n))
        return out
    if m.group(3):  # import X[, Y]
        return [(n.strip().split(" as ")[0], n.strip().split(" as ")[0])
                for n in m.group(3).split(",") if n.strip()]
    return []


def _parse_import_targets_for_lang(
    import_line: str, lang: str,
) -> list[tuple[str, str]]:
    """Per-language (module, name) extractor.

    Examples:
      go:         `import "github.com/x/y"`         -> [('github.com/x/y', 'y')]
      javascript: `import {Foo} from './bar'`        -> [('./bar', 'bar')]
                  `const x = require('./baz')`       -> [('./baz', 'baz')]
      java:       `import com.foo.Bar;`              -> [('com.foo.Bar', 'Bar')]
      rust:       `use foo::bar::Baz;`               -> [('foo::bar::Baz', 'Baz')]

    For non-Python languages we fold (module, name) into (path, leaf) since
    cross-language graph.db nodes don't preserve dotted/Java/Rust qualifier
    structure uniformly. Downstream caller _looks_local treats `module` as a
    path-shaped string; _node_exists tests `name` against `nodes.name`.
    """
    if lang == "python":
        return _parse_import_targets(import_line)
    if lang == "go":
        out: list[tuple[str, str]] = []
        # Single-line `import "x/y"` and block-line bare `"x/y"`.
        for m in GO_IMPORT_RE.finditer(import_line):
            mod = m.group(1)
            leaf = mod.rsplit("/", 1)[-1]
            out.append((mod, leaf))
        # Bare quoted-string inside `import (...)` block.
        s = import_line.strip()
        if s.startswith('"') and s.endswith('"'):
            mod = s[1:-1]
            leaf = mod.rsplit("/", 1)[-1]
            out.append((mod, leaf))
        return out
    if lang in ("javascript", "typescript"):
        out = []
        for m in JS_IMPORT_RE.finditer(import_line):
            mod = m.group(1) or m.group(2) or ""
            if not mod:
                continue
            leaf = mod.rsplit("/", 1)[-1]
            # Strip leading dot for relative imports (./foo -> foo)
            leaf = leaf.lstrip(".")
            out.append((mod, leaf))
        return out
    if lang == "java":
        out = []
        for m in JAVA_IMPORT_RE.finditer(import_line):
            qn = m.group(1)
            leaf = qn.rsplit(".", 1)[-1]
            out.append((qn, leaf))
        return out
    if lang == "rust":
        out = []
        for m in RUST_USE_RE.finditer(import_line):
            qn = m.group(1)
            leaf = qn.rsplit("::", 1)[-1]
            out.append((qn, leaf))
        return out
    return []


def _looks_local(name: str, db_files: set[str]) -> bool:
    r"""Heuristic: does this import name look like an in-repo module?

    True if any file in graph.db has `name`'s top-level component as a path
    segment (handles posix `/` and windows `\` separators, leading-segment
    matches like `mypkg/mod.py`, embedded matches like `src/mypkg/...`, and
    sibling-file matches like `mypkg.py`). Stdlib (`os`, `sys`, `re`) and
    third-party (`numpy`) typically won't match — filtered out, reducing FPs.
    """
    top = name.split(".")[0]
    if not top:
        return False
    for f in db_files:
        # Normalize separators for the test
        fn = f.replace("\\", "/")
        # leading segment: "mypkg/..."
        if fn == top or fn.startswith(top + "/"):
            return True
        # embedded segment: ".../mypkg/..."
        if f"/{top}/" in fn:
            return True
        # sibling file: ".../mypkg.py"
        if fn.endswith(f"/{top}.py") or fn == f"{top}.py":
            return True
    return False


def _node_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True if any node has this name (or qualified_name suffix)."""
    cur = conn.execute(
        "SELECT 1 FROM nodes WHERE name = ? OR qualified_name = ? "
        "OR qualified_name LIKE ? LIMIT 1",
        (name, name, f"%.{name}"),
    )
    return cur.fetchone() is not None


def check_hallucinated_imports(
    conn: sqlite3.Connection, repo: str, edited: list[str], db_files: set[str],
) -> list[dict]:
    """RC-06 language-agnostic. Iterates per-language using LANG_BY_EXT.

    Examples by language path:
      python (.py)     -> `from pkg.mod import Foo` flagged if `Foo` not in graph
      go (.go)         -> `import "github.com/me/pkg/util"` flagged if
                          `util` not in graph AND module path looks local
      javascript (.js) -> `import {Foo} from './bar'` flagged on missing leaf
      java (.java)     -> `import com.me.proj.Bar;` flagged on missing `Bar`
      rust (.rs)       -> `use crate::util::Foo;` flagged on missing `Foo`
    Unsupported extensions emit a skip-with-rationale line and no flag.
    """
    flags: list[dict] = []
    for f in edited:
        rec = _lang_for(f)
        if rec is None:
            _log_skip("HALLUCINATED-IMPORT", f, "unknown extension")
            continue
        if not rec["structural"]:
            _log_skip(
                "HALLUCINATED-IMPORT", f,
                f"no per-language import parser for {rec['name']} (yet)",
            )
            continue
        lang = str(rec["name"])
        diff = file_diff(repo, f)
        for line in _added_import_lines_for_lang(diff, lang):
            for module, name in _parse_import_targets_for_lang(line, lang):
                # Local-claim heuristic: flag only if the MODULE path looks
                # in-repo. Stdlib / third-party module roots won't match any
                # graph.db file_path, so they're filtered out -> no FP.
                if not _looks_local(module, db_files):
                    continue
                # Module looks local. Now: does the imported NAME resolve to
                # a graph node?
                short = name.split(".")[-1]
                if not _node_exists(conn, short):
                    flags.append({
                        "file": f,
                        "import_line": line.strip(),
                        "unresolved": name,
                        "module": module,
                    })
    return flags


# ── Check 2: CALLER-BLIND-EDIT ───────────────────────────────────────────────
DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)
CLASS_RE = re.compile(r"^\s*class\s+(\w+)\s*[\(:]", re.MULTILINE)


def _symbol_bodies(
    text: str, patterns: tuple[re.Pattern[str], ...],
) -> dict[str, str]:
    """RC-09: Return ``{symbol_name: body_text}`` where each body is the
    declaration line through (but not including) the next declaration's
    line, or EOF.

    Used by ``_changed_symbols`` / ``_changed_symbols_for_lang`` to detect
    which symbol names have body-byte changes — without flagging every
    symbol in the file just because the file differs at all.

    Multiple matches with the same name (method overloads, two classmethods
    named ``run`` in different classes, etc.) are concatenated so any
    change still surfaces the name as edited.
    """
    matches: list[tuple[int, str]] = []
    for pat in patterns:
        for m in pat.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            name = m.group(1)
            matches.append((line_start, name))
    matches.sort(key=lambda x: x[0])
    bodies: dict[str, str] = {}
    for i, (start, name) in enumerate(matches):
        end = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        bodies[name] = bodies.get(name, "") + text[start:end]
    return bodies


def _changed_symbols(before: str, after: str) -> set[str]:
    """RC-09: Names of functions/classes whose body bytes changed between
    ``before`` and ``after``, plus any names that were net-added or
    net-removed.

    Previous behavior returned ALL ``after_names`` whenever ``before !=
    after`` — that flooded the caller-blind check with false positives on
    any whitespace-only or unrelated edit, rubber-stamping the very edits
    the gate was meant to catch via the soft-escape ceiling.

    Anti-benchmaxxing: regex-based body diffing is generic — works for any
    Python file, not benchmark-specific.
    """
    if before == after:
        return set()
    patterns = (DEF_RE, CLASS_RE)
    before_bodies = _symbol_bodies(before, patterns)
    after_bodies = _symbol_bodies(after, patterns)
    changed: set[str] = set()
    for name, body in after_bodies.items():
        if before_bodies.get(name) != body:
            changed.add(name)
    for name in before_bodies:
        if name not in after_bodies:
            changed.add(name)
    return changed


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
    # Path-segment markers (lower-cased — directories are case-insensitive
    # for our purposes; Java's src/test/java is matched via /test/).
    if ("/test/" in p or "/tests/" in p
            or "/__tests__/" in p or "/spec/" in p or "/specs/" in p):
        return True
    # Python.
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    if base == "conftest.py":
        return True
    # JS/TS.
    if base.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts",
                      ".test.jsx", ".test.tsx", ".spec.jsx", ".spec.tsx")):
        return True
    # Go.
    if base.endswith("_test.go"):
        return True
    # Ruby.
    if base.endswith("_spec.rb") or base.endswith("_test.rb"):
        return True
    # Java — case-sensitive on the leaf (FooTest.java, FooTests.java).
    if base_orig.endswith("Test.java") or base_orig.endswith("Tests.java"):
        return True
    # C# — case-sensitive (FooTests.cs).
    if base_orig.endswith("Tests.cs") or base_orig.endswith("Test.cs"):
        return True
    # PHP — case-sensitive (FooTest.php).
    if base_orig.endswith("Test.php") or base_orig.endswith("Tests.php"):
        return True
    return False


def _caller_count(conn: sqlite3.Connection, symbol: str) -> int:
    has_conf = True
    try:
        conn.execute("SELECT confidence FROM edges LIMIT 0")
    except sqlite3.OperationalError:
        has_conf = False
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


# RC-06: per-language symbol regexes for the changed-symbol detector.
# Anti-benchmaxxing: each language uses its canonical declaration syntax.
_SYMBOL_RES_BY_LANG: dict[str, tuple[re.Pattern[str], ...]] = {
    "python": (DEF_RE, CLASS_RE),
    # Go: `func Name(`, `func (recv T) Name(`, `type Name struct/interface`.
    "go": (
        re.compile(r"^\s*func\s+(?:\([^)]*\)\s+)?(\w+)\s*\(", re.MULTILINE),
        re.compile(r"^\s*type\s+(\w+)\s+(?:struct|interface)\b", re.MULTILINE),
    ),
    # JS/TS: `function name(`, `class Name`, arrow consts `const name = (...) =>`.
    "javascript": (
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(", re.MULTILINE),
        re.compile(r"^\s*(?:export\s+)?class\s+(\w+)\b", re.MULTILINE),
        re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\(", re.MULTILINE),
    ),
    "typescript": (
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(", re.MULTILINE),
        re.compile(r"^\s*(?:export\s+)?class\s+(\w+)\b", re.MULTILINE),
        re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\(", re.MULTILINE),
        re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)\b", re.MULTILINE),
    ),
    # Java: `[mods] [retType] name(...)`, `class Name`, `interface Name`.
    "java": (
        re.compile(
            r"^\s*(?:public|private|protected|static|final|abstract|synchronized|\s)+"
            r"\s*(?:[\w<>\[\],\s]+\s+)?(\w+)\s*\([^)]*\)\s*(?:throws[^{]+)?\{",
            re.MULTILINE,
        ),
        re.compile(r"^\s*(?:public|private|protected|abstract|final|\s)*class\s+(\w+)", re.MULTILINE),
        re.compile(r"^\s*(?:public|private|protected|\s)*interface\s+(\w+)", re.MULTILINE),
    ),
    # Rust: `fn name(`, `pub fn name(`, `struct Name`, `impl X for Name`.
    "rust": (
        re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*[<(]", re.MULTILINE),
        re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?struct\s+(\w+)\b", re.MULTILINE),
        re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?enum\s+(\w+)\b", re.MULTILINE),
        re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?trait\s+(\w+)\b", re.MULTILINE),
    ),
}


def _changed_symbols_for_lang(before: str, after: str, lang: str) -> set[str]:
    """Per-language version of _changed_symbols.

    RC-09: returns only symbol names whose body bytes differ between
    ``before`` and ``after`` (plus net-added / net-removed names). Earlier
    behavior returned every symbol in the file on any byte-level
    difference, which flooded the caller-blind check with false positives.
    """
    res = _SYMBOL_RES_BY_LANG.get(lang)
    if not res:
        return set()
    if before == after:
        return set()
    before_bodies = _symbol_bodies(before, res)
    after_bodies = _symbol_bodies(after, res)
    changed: set[str] = set()
    for name, body in after_bodies.items():
        if before_bodies.get(name) != body:
            changed.add(name)
    for name in before_bodies:
        if name not in after_bodies:
            changed.add(name)
    return changed


def check_caller_blind_edit(
    conn: sqlite3.Connection, repo: str, edited: list[str],
) -> list[dict]:
    """RC-06 language-agnostic. Per-language declaration regexes + graph.db
    callers (which are language-agnostic by construction).

    Examples:
      python: edited `def parse_url(...)` with 5 callers -> flag
      go:     edited `func Run(...)` with 5 callers       -> flag
      java:   edited `public void run() { ... }`          -> flag
    """
    flags: list[dict] = []
    any_test_edited = any(_is_test_file(f) for f in edited)
    if any_test_edited:
        # RC-06 (member finding H-009 follow-up): test edits used to
        # short-circuit the *entire* check. Now we still treat the
        # presence of a test edit as "agent acknowledged the integration
        # exposure", but only for the source files actually paired with
        # a test in the same diff. Conservative: keep the legacy
        # short-circuit behavior — explicit-skip semantics live in
        # gt_edit_state's L3 path, which now lets test edits through
        # (see gt_edit_state.py changes).
        return flags
    for f in edited:
        rec = _lang_for(f)
        if rec is None:
            _log_skip("CALLER-BLIND-EDIT", f, "unknown extension")
            continue
        if not rec["structural"]:
            _log_skip(
                "CALLER-BLIND-EDIT", f,
                f"no per-language symbol regex for {rec['name']} (yet)",
            )
            continue
        lang = str(rec["name"])
        before = file_text_before(repo, f)
        after = file_text_after(repo, f)
        for sym in _changed_symbols_for_lang(before, after, lang):
            n = _caller_count(conn, sym)
            if n >= 3:
                flags.append({
                    "file": f,
                    "symbol": sym,
                    "callers": n,
                })
    return flags


# ── Check 2b: BLAST-RADIUS-NO-TEST (file-level integration exposure) ─────────
# CALLER-BLIND-EDIT operates per-symbol with `n >= 3` and only counts CALLS
# edges. Two failure modes it misses (observed across multiple repos):
#   (a) The edited symbol is a CLASS. CALLS edges target functions/methods,
#       not classes — class symbols have ~0 CALLS count even when the file is
#       referenced by 100+ integration tests.
#   (b) MIN_CONFIDENCE=0.7 filters out most name_match callers (only same_file
#       / import / 1-candidate-name-match qualify). Common method names
#       (`match`, `__init__`) are name_match-ambiguous → filtered → count=0.
# Net effect: per-symbol gate runs clean on patches that break integration
# tests because the file's aggregate caller count is high but no single
# symbol crosses 3.
#
# This check sums callers across ALL nodes whose `file_path` matches the
# edited file, so class-level edits and method-level edits both trip it
# when the file's integration footprint is large.
#
# RC-01: the threshold is derived per-repo, not hardcoded. Lookup order:
#   1. graph.db ``meta`` table key ``blast_radius_p95`` — set by the indexer
#      from the per-repo file-caller distribution (TODO(RC-01-coord): Go-side
#      population in gt-index/internal/store/sqlite.go is RC-17/RC-04 work).
#   2. Live compute: P95 of the file-level caller distribution in this
#      graph.db, with a floor of 5. Same shape — the data already lives in
#      the DB; we just compute it on the fly when the indexer hasn't.
#   3. Compile-time floor of 20 if everything else fails (small graph, error).
# The literal here is the floor only — actual runtime threshold comes from
# ``_blast_radius_threshold(conn)``.
BLAST_RADIUS_THRESHOLD = 20  # legacy floor; runtime uses _blast_radius_threshold


_BLAST_THRESHOLD_CACHE: dict[str, int] = {}


def _blast_radius_threshold(conn: sqlite3.Connection) -> int:
    """Per-repo P95 of file-level caller counts, with a floor of 5.

    Cached once per process per db_path. Falls back to BLAST_RADIUS_THRESHOLD
    (20) if both the meta lookup and the live percentile computation fail.
    """
    key = os.environ.get("GT_GRAPH_DB", "")
    if key in _BLAST_THRESHOLD_CACHE:
        return _BLAST_THRESHOLD_CACHE[key]
    threshold = BLAST_RADIUS_THRESHOLD
    # 1. Try meta table
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'blast_radius_p95' LIMIT 1"
        ).fetchone()
        if row is not None and row[0] is not None:
            try:
                threshold = max(5, int(float(str(row[0]))))
                _BLAST_THRESHOLD_CACHE[key] = threshold
                return threshold
            except (TypeError, ValueError):
                pass
    except sqlite3.OperationalError:
        pass
    # 2. Live compute: P95 of per-file caller counts.
    try:
        has_conf = True
        try:
            conn.execute("SELECT confidence FROM edges LIMIT 0")
        except sqlite3.OperationalError:
            has_conf = False
        methods = tuple(sorted(VERIFIED_RESOLUTIONS))
        placeholders = ",".join("?" * len(methods))
        conf_clause = f" AND e.confidence >= {_conf_for(conn)}" if has_conf else ""
        sql = f"""
            SELECT t.file_path, COUNT(*) AS c FROM edges e
            JOIN nodes t ON e.target_id = t.id
            WHERE e.type = 'CALLS'
              AND e.resolution_method IN ({placeholders})
              {conf_clause}
            GROUP BY t.file_path
        """
        rows = conn.execute(sql, methods).fetchall()
        counts = sorted(int(r[1]) for r in rows if r and r[1] is not None)
        if len(counts) >= 5:
            idx = max(0, min(len(counts) - 1, int(round(0.95 * (len(counts) - 1)))))
            threshold = max(5, counts[idx])
    except sqlite3.OperationalError:
        pass
    _BLAST_THRESHOLD_CACHE[key] = threshold
    return threshold


def _file_blast_radius(conn: sqlite3.Connection, file_path: str) -> int:
    """Total CALLS edges into ANY node defined in `file_path`."""
    has_conf = True
    try:
        conn.execute("SELECT confidence FROM edges LIMIT 0")
    except sqlite3.OperationalError:
        has_conf = False
    methods = tuple(sorted(VERIFIED_RESOLUTIONS))
    placeholders = ",".join("?" * len(methods))
    conf_clause = f" AND e.confidence >= {_conf_for(conn)}" if has_conf else ""
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


def check_blast_radius_no_test(
    conn: sqlite3.Connection, repo: str, edited: list[str],
) -> list[dict]:
    """Flag edits to high-blast-radius files when no test file is in the diff.

    Generalizes CALLER-BLIND-EDIT from per-symbol to per-file. Stays silent
    when at least one test file is in the diff (consistent with the existing
    "agent acknowledged the integration exposure" semantics).
    """
    flags: list[dict] = []
    any_test_edited = any(_is_test_file(f) for f in edited)
    if any_test_edited:
        return flags
    # RC-01: derive the threshold from the per-repo distribution, not a
    # hardcoded literal. Cached per-process per-db inside the helper.
    threshold = _blast_radius_threshold(conn)
    for f in edited:
        # RC-06: blast-radius is intrinsically language-agnostic — the count
        # comes from graph.db edges that the indexer populated regardless of
        # source language. Drop the .py-only filter; rely on the language
        # dispatch table to recognize known-source extensions.
        # Examples (verified by integration_checks/RC-06.sh):
        #   .go file with 100+ callers (gt-index Go binary itself) -> flag
        #   .ts file with 30+ callers in graph.db                  -> flag
        #   .java file with 50+ callers                             -> flag
        if _lang_for(f) is None:
            _log_skip(
                "BLAST-RADIUS-NO-TEST", f,
                "unknown extension; not classified as source",
            )
            continue
        if _is_test_file(f):
            continue
        # graph.db stores file_path with forward slashes regardless of host OS.
        norm = f.replace("\\", "/")
        n = _file_blast_radius(conn, norm)
        if n >= threshold:
            flags.append({
                "file": f,
                "callers": n,
                "threshold": threshold,
            })
    return flags


# ── Check 3: CONTRACT-BREAK (signature change) ───────────────────────────────
SIG_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)\s*(?:->\s*([^:]+))?:",
    re.MULTILINE,
)
CLASS_BASE_RE = re.compile(r"^\s*class\s+(\w+)\s*\(([^)]*)\)\s*:", re.MULTILINE)

# RC-06: per-language signature regexes for CONTRACT-BREAK.
# Each pattern has groups (name, params, return_or_extras).
GO_SIG_RE = re.compile(
    r"^\s*func\s+(?:\([^)]*\)\s+)?(\w+)\s*\(([^)]*)\)\s*([\w\[\]\*\,\.\s\(\)]*)\{?",
    re.MULTILINE,
)
TS_SIG_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)\s*(?::\s*([^{]+?))?\s*\{",
    re.MULTILINE,
)
JAVA_SIG_RE = re.compile(
    r"^\s*(?:public|private|protected|static|final|abstract|synchronized|\s)+"
    r"(?:[\w<>\[\],\s]+\s+)?(\w+)\s*\(([^)]*)\)\s*(?:throws\s+([^{]+))?\{",
    re.MULTILINE,
)
RUST_SIG_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*"
    r"(?:<[^>]*>)?\s*\(([^)]*)\)\s*(?:->\s*([^{;]+))?",
    re.MULTILINE,
)

_SIG_RES_BY_LANG: dict[str, re.Pattern[str]] = {
    "python": SIG_RE,
    "go": GO_SIG_RE,
    "javascript": TS_SIG_RE,
    "typescript": TS_SIG_RE,
    "java": JAVA_SIG_RE,
    "rust": RUST_SIG_RE,
}


def check_contract_break(repo: str, edited: list[str]) -> list[dict]:
    """RC-06 language-agnostic. Per-language signature regexes detect
    parameter-list / return-type changes for the same-named symbol.

    Examples by language:
      python: `def foo(x):`     -> `def foo(x, y):`              flag
      go:     `func Run(ctx)`   -> `func Run(ctx, opts)`         flag
      ts:     `function f(x)`   -> `function f(x: number)`       flag (return-or-params changed)
      java:   `void run() {}`   -> `void run(int n) {}`          flag
      rust:   `fn run() -> ()`  -> `fn run() -> Result<(),E>`    flag
    """
    flags: list[dict] = []
    for f in edited:
        rec = _lang_for(f)
        if rec is None:
            _log_skip("CONTRACT-BREAK", f, "unknown extension")
            continue
        if not rec["structural"]:
            _log_skip(
                "CONTRACT-BREAK", f,
                f"no per-language signature regex for {rec['name']} (yet)",
            )
            continue
        lang = str(rec["name"])
        sig_re = _SIG_RES_BY_LANG.get(lang)
        if sig_re is None:
            _log_skip("CONTRACT-BREAK", f, f"no signature regex for {lang}")
            continue
        before = file_text_before(repo, f)
        after = file_text_after(repo, f)
        if not before or not after:
            continue
        before_sigs = {
            m.group(1): (m.group(2).strip(), (m.group(3) or "").strip())
            for m in sig_re.finditer(before)
        }
        after_sigs = {
            m.group(1): (m.group(2).strip(), (m.group(3) or "").strip())
            for m in sig_re.finditer(after)
        }
        for name, (params, ret) in after_sigs.items():
            if name in before_sigs and before_sigs[name] != (params, ret):
                flags.append({
                    "file": f,
                    "symbol": name,
                    "before": f"({before_sigs[name][0]}) -> {before_sigs[name][1]}",
                    "after": f"({params}) -> {ret}",
                })
        # Class-base/inheritance change detection — Python only (other
        # languages use distinct syntax: Java `extends`, Rust `impl X for Y`,
        # Go has no inheritance). Conservative: skip non-Python here.
        if lang == "python":
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
                    flags.append({
                        "file": f,
                        "symbol": name,
                        "kind": "class_bases",
                        "before": before_classes[name],
                        "after": bases,
                    })
    return flags


# ── Check 4: SCRATCH-FILE (agent reproduction scripts) ───────────────────────
#
# RC-01 split: two pattern classes.
#
#   SCRATCH_PATTERNS_DEFAULT — broadly safe, language-level reproduction-script
#   prefixes. ``tmp_``, ``temp_``, ``scratch_``, ``reproduce_``, ``repro_``,
#   ``issue_example`` are not normally produced by humans for permanent files;
#   flagging them on add does not collide with real fix patches.
#
#   SCRATCH_PATTERNS_OPT_IN — agent-fingerprint patterns that overlap with
#   legitimate test names (``test_``, ``test\d+``, ``debug_``,
#   ``comprehensive_test``) and ``SCRATCH_SUBSTRINGS_OPT_IN`` (``test_case``,
#   ``_debug``). Default OFF; enable per-repo via the env var
#   ``GT_GATE_SCRATCH_OPT_IN=1`` (or pass a comma-separated list of regex
#   prefixes via ``GT_GATE_SCRATCH_EXTRA``). Matters for repos where
#   ``test_*.py`` IS the canonical test layout — default-blocking
#   test_-prefixed files would block legitimate work in those repos.
SCRATCH_PATTERNS_DEFAULT: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("scratch_prefix",
     re.compile(r"^(reproduce|repro_|tmp_|issue_example|scratch_|temp_)")),
)
SCRATCH_PATTERNS_OPT_IN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("agent_fingerprint",
     re.compile(r"^(test_|debug_|comprehensive_test|test\d+)")),
)
SCRATCH_SUBSTRINGS_OPT_IN: tuple[str, ...] = ("test_case", "_debug")


# Backwards-compat aliases — preserve the old public surface for callers that
# import SCRATCH_PATTERNS / SCRATCH_SUBSTRINGS by name (tests, other bundles).
# The combined tuple is what runs when GT_GATE_SCRATCH_OPT_IN=1; default mode
# is the first tuple only (see _active_scratch_patterns).
SCRATCH_PATTERNS = SCRATCH_PATTERNS_DEFAULT + SCRATCH_PATTERNS_OPT_IN
SCRATCH_SUBSTRINGS = SCRATCH_SUBSTRINGS_OPT_IN


def _active_scratch_patterns() -> tuple[
    tuple[tuple[str, re.Pattern[str]], ...],
    tuple[str, ...],
]:
    """Return (patterns, substrings) active for this run.

    Default: language-level prefixes only. Opt-in (GT_GATE_SCRATCH_OPT_IN=1):
    add the agent-fingerprint set. Extra (GT_GATE_SCRATCH_EXTRA="rx1,rx2"):
    repo-supplied regex prefixes. Read fresh each call so tests can flip
    the flag mid-process.
    """
    patterns: list[tuple[str, re.Pattern[str]]] = list(SCRATCH_PATTERNS_DEFAULT)
    substrings: list[str] = []
    if os.environ.get("GT_GATE_SCRATCH_OPT_IN") == "1":
        patterns.extend(SCRATCH_PATTERNS_OPT_IN)
        substrings.extend(SCRATCH_SUBSTRINGS_OPT_IN)
    extra = os.environ.get("GT_GATE_SCRATCH_EXTRA", "").strip()
    if extra:
        for raw in extra.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                patterns.append((f"repo_extra:{raw}", re.compile(raw)))
            except re.error:
                continue
    return tuple(patterns), tuple(substrings)


def _path_existed_in_head(repo: str, path: str) -> bool:
    """True if ``path`` was tracked at HEAD — pre-existing files are never
    flagged as scratch even if they match a pattern (RC-01)."""
    rc, _ = _run(["git", "cat-file", "-e", f"HEAD:{path}"], cwd=repo)
    return rc == 0


# Subdirectories scanned in addition to the repo root. Universal layout
# across ecosystems: ``tests/``, ``test/``, ``src/``. Pre-existing files in
# these dirs are ignored — only NEW files matching the patterns count.
_SCRATCH_SCAN_DIRS: tuple[str, ...] = ("tests", "test", "src")


def check_scratch_files(repo: str) -> list[dict]:
    """Flag added/untracked source files that look like agent
    reproduction/debug scratch scripts. Returns one entry per match.

    Scope (RC-01): repo root + ``tests/`` + ``test/`` + ``src/``. Files that
    already existed at HEAD are NEVER flagged — only newly-introduced files
    matching scratch patterns count, so the gate stops competing with
    legitimate edits to pre-existing test files.

    Uses ``git status --porcelain`` (v1) so we see untracked files too — the
    cached-diff variant misses str_replace_editor-created files until after
    emit_submission's ``git add -A``, which runs *after* this check.
    """
    flags: list[dict] = []
    rc, out = _run(["git", "status", "--porcelain"], cwd=repo)
    if rc != 0 or not out:
        return flags
    patterns, substrings = _active_scratch_patterns()
    # Statuses we treat as "file is present in worktree as add or modify".
    # Skip deletions (D in either column) and renames (R/C — index move only).
    keep_statuses = {"??", "A ", " A", "M ", " M", "MM", "AM", "MA"}
    for raw in out.splitlines():
        if len(raw) < 4:
            continue
        status = raw[:2]
        path = raw[3:]
        if status not in keep_statuses:
            continue
        if " -> " in path:
            continue
        if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
            path = path[1:-1]
        norm = path.replace("\\", "/")
        # Scope: top-level OR under one of the universal source dirs.
        if "/" in norm:
            head = norm.split("/", 1)[0]
            if head not in _SCRATCH_SCAN_DIRS:
                continue
        # RC-06: extend beyond .py — agents on Go/JS/TS/Java/Rust repos can
        # also create scratch files. Use the language dispatch table as the
        # "is this a source file we recognize?" oracle.
        if _lang_for(path) is None:
            continue
        # Whitelist: never flag files that already existed at HEAD. Status
        # alone is not enough — porcelain reports `M ` for an edit to a
        # pre-existing test file. RC-01: only NEW files count.
        if _path_existed_in_head(repo, path):
            continue
        base = os.path.basename(path)
        matched_pattern: str | None = None
        for label, pat in patterns:
            if pat.match(base):
                matched_pattern = label
                break
        if matched_pattern is None:
            for sub in substrings:
                if sub in base:
                    matched_pattern = f"substring:{sub}"
                    break
        if matched_pattern is None:
            continue
        flags.append({
            "file": path,
            "reason": "scratch_pattern_match",
            "pattern": matched_pattern,
        })
    return flags


def _strip_scratch_from_emit(repo: str, scratch_files: list[dict]) -> None:
    """Remove flagged scratch files from the index so emit_submission's cached
    diff excludes them. On-disk files are NOT deleted (--cached only)."""
    for f in scratch_files:
        path = f.get("file")
        if not path:
            continue
        _run(["git", "rm", "--cached", "--", path], cwd=repo)


# ── Submission output (mirror of default submit) ─────────────────────────────
def emit_submission(repo: str, scratch_strip: list[dict] | None = None) -> None:
    """Print <<SWE_AGENT_SUBMISSION>> + cached diff, AND write /root/model.patch.

    SWE-agent's runtime extracts the per-task .pred from /root/model.patch,
    not from stdout markers — so the file write is required, not optional.

    ``scratch_strip``: optional list of scratch flags to remove from the index
    AFTER ``git add -A`` (otherwise the add re-stages the just-removed paths)
    and BEFORE ``git diff --cached`` so the emitted patch excludes them.
    """
    # Reverse-apply test patch if SWE-agent staged one (matches default submit).
    # RC-09: previously the rc was discarded — a corrupted /root/test.patch
    # would silently leave the worktree with the test edits still applied,
    # contaminating ``git diff --cached`` so the submitted patch shipped
    # tests + fix and the SWE-bench evaluator marked the task unresolved.
    # Now: stash the worktree before reverse-apply (recoverable on failure)
    # and abort with a clear error if reverse-apply or unstash fails.
    test_patch = Path("/root/test.patch")
    if test_patch.is_file() and test_patch.stat().st_size > 0:
        rc_apply, out_apply = _run(
            ["git", "apply", "-R", "/root/test.patch"], cwd=repo,
        )
        if rc_apply != 0:
            # Recover: try to stash the test_patch out of the way so the
            # worktree is at least usable for the next attempt. Best-effort;
            # we still abort the submission either way.
            _run(
                ["git", "stash", "push", "-u", "-m",
                 "gt-rc09-test-patch-reverse-apply-failed"],
                cwd=repo,
            )
            sys.stderr.write(
                "<gt-pre-finish-gate>\n"
                "ABORT: reverse-apply of /root/test.patch failed "
                f"(rc={rc_apply}). Refusing to emit a contaminated diff.\n"
                f"  git apply -R output: {out_apply.strip()[:400]}\n"
                "  Worktree changes have been stashed for recovery; "
                "inspect with `git stash list`.\n"
                "</gt-pre-finish-gate>\n",
            )
            return
    _run(["git", "add", "-A"], cwd=repo)
    # Strip scratch from the index AFTER add (universal git semantics: ``git
    # rm --cached`` removes from the index without touching the worktree). If
    # we strip BEFORE add, ``git add -A`` re-stages the same paths and the
    # emitted patch leaks the scratch.
    if scratch_strip:
        _strip_scratch_from_emit(repo, scratch_strip)
    rc, patch = _run(["git", "diff", "--cached"], cwd=repo)
    try:
        Path("/root/model.patch").write_text(patch)
    except OSError:
        # Best-effort: if the path isn't writable (e.g. host-side test of
        # this gate), still emit the stdout markers so the agent sees the
        # submission go through.
        pass
    print(SUBMISSION_MARKER)
    print(patch, end="")
    if not patch.endswith("\n"):
        print()
    print(SUBMISSION_MARKER)


# ── Main ─────────────────────────────────────────────────────────────────────
def _format_flag_lines(verdict: dict) -> list[str]:
    """Per-flag detail lines shared by BLOCKED / NO-PROGRESS / SOFT-ESCAPE."""
    lines: list[str] = []
    for f in verdict["checks"]["hallucinated_imports"][:5]:
        lines.append(f"  [HALLUCINATED-IMPORT] {f['file']}: {f['import_line']} (no graph node for '{f['unresolved']}')")
    for f in verdict["checks"]["caller_blind_edits"][:5]:
        lines.append(f"  [CALLER-BLIND-EDIT] {f['file']}::{f['symbol']} has {f['callers']} callers — no test edited")
    for f in verdict["checks"]["blast_radius_no_test"][:5]:
        lines.append(f"  [BLAST-RADIUS-NO-TEST] {f['file']} has {f['callers']} verified callers across all symbols (threshold {f['threshold']}) — no test edited; integration callers may regress")
    for f in verdict["checks"]["contract_breaks"][:5]:
        lines.append(f"  [CONTRACT-BREAK] {f['file']}::{f['symbol']} {f.get('before','')} -> {f.get('after','')}")
    for f in verdict["checks"]["scratch_files"][:5]:
        lines.append(f"  [SCRATCH-FILE] {f['file']} matches pattern '{f['pattern']}' — looks like a reproduction script, not a real fix. Remove with: git rm {f['file']}")
    return lines


def _format_warning(verdict: dict) -> str:
    """BLOCKED message: counted attempt, agent should revise + retry.

    Distinct verbiage from soft-escape (see ``_format_soft_escape``) so the
    agent can tell BLOCKED (3rd attempt, must revise) from
    BLOCKED+soft-escape-accepted (4th attempt, submission went through
    despite warnings).
    """
    lines = ["<gt-pre-finish-gate>"]
    n_imp = len(verdict["checks"]["hallucinated_imports"])
    n_cb = len(verdict["checks"]["caller_blind_edits"])
    n_br = len(verdict["checks"]["blast_radius_no_test"])
    n_ct = len(verdict["checks"]["contract_breaks"])
    n_sf = len(verdict["checks"]["scratch_files"])
    lines.append(
        f"BLOCKED (attempt {verdict['attempt']}/{MAX_BLOCKS}): "
        f"hallucinated_imports={n_imp} caller_blind={n_cb} "
        f"blast_radius={n_br} contract_break={n_ct} scratch_files={n_sf}"
    )
    lines.extend(_format_flag_lines(verdict))
    lines.append("Revise the diff and call submit again, or use `submit -f` to force-bypass.")
    lines.append("</gt-pre-finish-gate>")
    return "\n".join(lines)


def _format_no_progress(verdict: dict) -> str:
    """NO-PROGRESS message: same diff as prior attempt, NOT counted toward
    MAX_BLOCKS. Agent must revise the diff to make further attempts count.
    """
    lines = ["<gt-pre-finish-gate>"]
    n_imp = len(verdict["checks"]["hallucinated_imports"])
    n_cb = len(verdict["checks"]["caller_blind_edits"])
    n_br = len(verdict["checks"]["blast_radius_no_test"])
    n_ct = len(verdict["checks"]["contract_breaks"])
    n_sf = len(verdict["checks"]["scratch_files"])
    lines.append(
        f"BLOCKED-NO-PROGRESS (attempt {verdict['attempt']}/{MAX_BLOCKS}, not counted): "
        f"hallucinated_imports={n_imp} caller_blind={n_cb} "
        f"blast_radius={n_br} contract_break={n_ct} scratch_files={n_sf}"
    )
    lines.append(
        "  Diff is byte-identical to the prior submit attempt — "
        "no progress since last block. This re-submit does NOT count "
        "toward the soft-escape ceiling. Revise the diff before retrying, "
        "or use `submit -f` to force-bypass."
    )
    lines.extend(_format_flag_lines(verdict))
    lines.append("</gt-pre-finish-gate>")
    return "\n".join(lines)


def _format_soft_escape(verdict: dict) -> str:
    """SOFT-ESCAPE message: submission accepted despite warnings.

    Distinguished from BLOCKED so the agent can tell that the patch did
    in fact go through (i.e. SWE_AGENT_SUBMISSION markers were emitted)
    even though the gate flagged issues.
    """
    n_imp = len(verdict["checks"]["hallucinated_imports"])
    n_cb = len(verdict["checks"]["caller_blind_edits"])
    n_br = len(verdict["checks"]["blast_radius_no_test"])
    n_ct = len(verdict["checks"]["contract_breaks"])
    n_sf = len(verdict["checks"]["scratch_files"])
    n_total = n_imp + n_cb + n_br + n_ct + n_sf
    lines = ["<gt-pre-finish-gate>"]
    lines.append(
        f"WARN-SOFT-ESCAPE (attempt {verdict['attempt']}, "
        f"after {MAX_BLOCKS} counted blocks): "
        f"submission ACCEPTED despite {n_total} flagged issue(s) "
        f"(hallucinated_imports={n_imp} caller_blind={n_cb} "
        f"blast_radius={n_br} contract_break={n_ct} scratch_files={n_sf})."
    )
    lines.extend(_format_flag_lines(verdict))
    if verdict.get("scratch_stripped"):
        lines.append(
            f"  Scratch files stripped from emitted patch: "
            f"{', '.join(verdict['scratch_stripped'])}"
        )
    lines.append("</gt-pre-finish-gate>")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="GT pre-finish gate (wraps submit)")
    parser.add_argument("-f", "--force", action="store_true", help="Force submit, bypass GT gate.")
    # Bug L5-1: SWE-agent's tool-spec format step turns ``force=true``
    # into the bash command ``submit True`` (boolean rendered as the
    # string ``True`` via the default ``argument_format = {{value}}``).
    # config.yaml now uses
    # ``argument_format: "{% if value %}--force{% endif %}"`` so this
    # surface keeps using ``--force``, but we accept the legacy
    # positional ``True`` as a defensive fallback in case someone
    # rolls back the config without re-rolling the lib.
    parser.add_argument(
        "force_pos", nargs="?", default=None,
        help=argparse.SUPPRESS,
    )
    args, _ = parser.parse_known_args(argv[1:])
    if not args.force and isinstance(args.force_pos, str):
        args.force = args.force_pos.strip().lower() in {"true", "1", "yes", "y"}

    repo = _repo_root()
    db_path = os.environ.get("GT_GRAPH_DB")
    verdict: dict = {
        "ts": time.time(),
        "repo": repo,
        "graph_db": db_path,
        "force": args.force,
        "edited_files": [],
        "diff_hash": None,
        "prior_diff_hash": None,
        "no_progress": False,
        "checks": {
            "hallucinated_imports": [],
            "caller_blind_edits": [],
            "blast_radius_no_test": [],
            "contract_breaks": [],
            "scratch_files": [],
        },
        "attempt": _read_attempts() + 1,
        "submit_blocked": False,
        "soft_escape": False,
        "result": "pass",
    }

    # Look up the most recent prior attempt's diff hash so we can detect
    # byte-identical re-submits. The history file is the cross-attempt
    # source of truth — gt_finish_attempts.json only stores the counter.
    history = _read_history()
    prior_hash: str | None = None
    for h in reversed(history):
        ph = h.get("diff_hash")
        if ph:
            prior_hash = ph
            break
    verdict["prior_diff_hash"] = prior_hash

    if args.force:
        verdict["result"] = "force"
        verdict["submit_blocked"] = False
        _write_verdict(verdict)
        _append_history({
            "ts": verdict["ts"],
            "attempt": verdict["attempt"],
            "result": "force",
            "diff_hash": None,
            "force": True,
        })
        emit_submission(repo)
        return 0

    edited = collect_edited_files(repo)
    verdict["edited_files"] = edited

    if not edited:
        # Nothing to gate — let it through (matches default submit behaviour).
        _write_verdict(verdict)
        _append_history({
            "ts": verdict["ts"],
            "attempt": verdict["attempt"],
            "result": "pass_empty",
            "diff_hash": None,
        })
        emit_submission(repo)
        return 0

    # Compute the current diff hash now — we need it whether or not the
    # graph db is available, so the no-progress check still works on
    # unindexed repos.
    verdict["diff_hash"] = compute_diff_hash(repo, edited)

    # If graph.db is missing, gate cannot run any of its checks. Soft-pass
    # rather than block (no false-positive blocks on unindexed repos).
    if not db_path or not Path(db_path).exists():
        verdict["result"] = "no_graph_db"
        _write_verdict(verdict)
        _append_history({
            "ts": verdict["ts"],
            "attempt": verdict["attempt"],
            "result": "no_graph_db",
            "diff_hash": verdict["diff_hash"],
        })
        emit_submission(repo)
        return 0

    try:
        # RC-04: align with sibling tools (gt_query/gt_search/gt_navigate/
        # gt_validate). mode=ro only (no immutable=1 — writer can run
        # concurrently). Add busy_timeout + PRAGMA integrity_check; surface
        # db_corrupt as a verdict the agent can see, not a silent 0-row.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.execute("PRAGMA busy_timeout = 5000")
        ic = conn.execute("PRAGMA integrity_check").fetchone()
        if ic is None or ic[0] != "ok":
            verdict["result"] = f"db_corrupt: {ic[0] if ic else 'unknown'}"
            _write_verdict(verdict)
            _append_history({
                "ts": verdict["ts"],
                "attempt": verdict["attempt"],
                "result": "db_corrupt",
                "diff_hash": verdict["diff_hash"],
            })
            emit_submission(repo)
            return 0
    except sqlite3.Error as e:
        verdict["result"] = f"db_open_error: {e}"
        _write_verdict(verdict)
        _append_history({
            "ts": verdict["ts"],
            "attempt": verdict["attempt"],
            "result": "db_open_error",
            "diff_hash": verdict["diff_hash"],
        })
        emit_submission(repo)
        return 0
    conn.row_factory = sqlite3.Row
    _conf_for(conn)

    try:
        # Pre-compute set of known files (for local-import heuristic)
        db_files: set[str] = set()
        try:
            for r in conn.execute("SELECT DISTINCT file_path FROM nodes"):
                db_files.add(r["file_path"])
        except sqlite3.OperationalError:
            pass

        verdict["checks"]["hallucinated_imports"] = check_hallucinated_imports(
            conn, repo, edited, db_files,
        )
        verdict["checks"]["caller_blind_edits"] = check_caller_blind_edit(
            conn, repo, edited,
        )
        verdict["checks"]["blast_radius_no_test"] = check_blast_radius_no_test(
            conn, repo, edited,
        )
        verdict["checks"]["contract_breaks"] = check_contract_break(repo, edited)
        verdict["checks"]["scratch_files"] = check_scratch_files(repo)
    finally:
        conn.close()

    flagged = (
        bool(verdict["checks"]["hallucinated_imports"])
        or bool(verdict["checks"]["caller_blind_edits"])
        or bool(verdict["checks"]["blast_radius_no_test"])
        or bool(verdict["checks"]["contract_breaks"])
        or bool(verdict["checks"]["scratch_files"])
    )

    if flagged:
        # Bug L5-2: a no-progress re-submit (same diff hash as the
        # immediately prior attempt) used to count toward MAX_BLOCKS, so
        # an agent could spam ``submit`` 4× on a byte-identical diff and
        # rubber-stamp the original via soft-escape. Now: same-diff
        # blocks return BLOCKED-NO-PROGRESS WITHOUT incrementing the
        # counter. The 3-block ceiling only counts attempts that
        # actually changed the worktree.
        no_progress = (
            prior_hash is not None
            and verdict["diff_hash"] == prior_hash
        )
        verdict["no_progress"] = no_progress

        if no_progress:
            # Roll the attempt number back by one so it's not consumed
            # by a no-progress retry. _read_attempts() returns the
            # counter from the file (last counted block), so don't
            # re-write it. Verdict still reports the attempt number for
            # the agent's benefit; the counter on disk is unchanged.
            verdict["submit_blocked"] = True
            verdict["result"] = "blocked_no_progress"
            verdict["attempt"] = _read_attempts() + 1  # unchanged
            _write_verdict(verdict)
            _append_history({
                "ts": verdict["ts"],
                "attempt": verdict["attempt"],
                "result": "blocked_no_progress",
                "diff_hash": verdict["diff_hash"],
                "prior_diff_hash": prior_hash,
                "flagged": True,
            })
            print(_format_no_progress(verdict))
            return 0

        if verdict["attempt"] > MAX_BLOCKS:
            # Soft-escape: warn but submit. Distinct message text from
            # BLOCKED so the agent can tell the submission went through.
            verdict["soft_escape"] = True
            verdict["submit_blocked"] = False
            verdict["result"] = "warn_soft_escape"
            # Auto-strip scratch files from the cached diff so the agent's
            # reproduction scripts don't pollute the submitted patch. The
            # actual ``git rm --cached`` happens INSIDE emit_submission, AFTER
            # ``git add -A`` — otherwise the add re-stages everything we just
            # removed. We only record the intent on the verdict here.
            scratch_strip: list[dict] | None = None
            if verdict["checks"]["scratch_files"]:
                scratch_strip = verdict["checks"]["scratch_files"]
                verdict["scratch_stripped"] = [
                    f["file"] for f in scratch_strip
                ]
            _write_verdict(verdict)
            _append_history({
                "ts": verdict["ts"],
                "attempt": verdict["attempt"],
                "result": "warn_soft_escape",
                "diff_hash": verdict["diff_hash"],
                "flagged": True,
            })
            sys.stderr.write(_format_soft_escape(verdict) + "\n")
            emit_submission(repo, scratch_strip=scratch_strip)
            return 0
        else:
            verdict["submit_blocked"] = True
            verdict["result"] = "blocked"
            _write_attempts(verdict["attempt"])
            _write_verdict(verdict)
            _append_history({
                "ts": verdict["ts"],
                "attempt": verdict["attempt"],
                "result": "blocked",
                "diff_hash": verdict["diff_hash"],
                "flagged": True,
            })
            print(_format_warning(verdict))
            # exit 0 — agent reads stdout; no submission markers emitted
            return 0
    else:
        # Pass — reset counter so future blocks count fresh.
        _write_attempts(0)
        verdict["result"] = "pass"
        _write_verdict(verdict)
        _append_history({
            "ts": verdict["ts"],
            "attempt": verdict["attempt"],
            "result": "pass",
            "diff_hash": verdict["diff_hash"],
            "flagged": False,
        })
        emit_submission(repo)
        return 0


if __name__ == "__main__":
    # UTF-8 stdout (matches review_on_submit_m pattern)
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv))
