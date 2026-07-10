"""At-edit SYNTAX validation — the E capability (catch-up to SWE-agent's linter guard).

SWE-agent's linter-guarded edit (discard a syntactically-invalid edit, +3.0pp,
NeurIPS 2024; ~51.7% of edits carry a catchable error) is the field's strongest
deterministic in-loop receipt. GroundTruth had NOTHING here: the old
``kernel.validate_against_graph`` is regex-only and dead. This module is the
replacement ENGINE (the post-write seam that consumes it is wired next wave).

DESIGN — generalized, no new deps. GT does NOT ship a parser per language. The
repo's OWN toolchain is guaranteed present in the task container (the container
builds and tests that repo), so we invoke it through the SAME frozen Wave-1
executor contract::

    executor(cmd: list[str], cwd: str, timeout: int) -> tuple[int | None, str, str]
                                                        (exit_code, stdout, stderr)

``executor=None`` runs a HOST subprocess via ``test_runner._run_subprocess`` (the
identical capture/decoding/kill semantics Wave-1 froze); an injected executor
runs the check INSIDE the task's own container. Python is special-cased to run
IN-PROCESS via ``ast.parse`` when no executor is given — no subprocess spawn, and
NO bytecode cache side effect (why ``ast.parse``, not ``py_compile``).

THE LAW — correct-or-quiet + positive evidence (accuracy invariant: a false
``syntax_error`` is worse than no check):
- ``syntax_error`` requires a non-zero exit AND an error-shaped diagnostic (a
  real parse/syntax token, not an environment failure). A crash that merely exits
  non-zero with no parse evidence is NOT a syntax error.
- ``unavailable`` for everything ambiguous: tool missing / not on PATH, timeout,
  no reliable exit code, a language we cannot cheaply and SOUNDLY check, or a
  non-zero exit with no positive parse evidence. Never a guessed verdict.

PER-LANGUAGE HONESTY (checked vs unavailable, and WHY):
  .py            IN-PROCESS ``ast.parse`` (executor=None) / ``python -c ast.parse``
                 (via executor). Pure parse, no bytecode cache.
  .js/.mjs/.cjs  ``node --check`` — Node's own parse-only flag.
  .go            ``gofmt -e`` — parses to AST, reports parse errors, exit != 0.
  .rb            ``ruby -c`` — syntax-only check ("Syntax OK" / exit != 0).
  .ts/.tsx       UNAVAILABLE — ``node --check`` cannot parse TS; ``tsc --noEmit``
                 conflates SYNTAX errors with TYPE/module-resolution errors on a
                 single file, so it cannot give POSITIVE syntax evidence. Quiet.
  .jsx           UNAVAILABLE — JSX is not valid JS; ``node --check`` false-positives.
  .rs            UNAVAILABLE — no fast parse-only rustc invocation for a non-lib
                 file; ``--emit=metadata`` needs a crate/type context. Quiet.
  .java          UNAVAILABLE — ``javac`` needs classpath/type resolution; no cheap
                 parse-only mode -> would conflate. Quiet.

The ``diagnostic`` is the toolchain's OWN error text (trimmed, bounded, ``<gt-*>``
scrubbed). It naturally names the edited file + line — that is the agent's OWN
file, native and fine; it carries no test identity (this checks a SOURCE edit).

LLM-free, deterministic, no network.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import traceback
from typing import Any, Callable

from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS
from groundtruth.runtime.covering_runner import _connect_ro, _edge_columns
from groundtruth.runtime.test_runner import _run_subprocess, classify_environment_failure

# The injectable execution boundary — the SAME frozen contract Wave-1 established
# (test_runner / covering_runner). ``None`` selects the host subprocess path.
Executor = Callable[[list[str], str, int], "tuple[int | None, str, str]"]

# Diagnostic bound — Format-D-adjacent: short by design (lost-in-the-middle).
_MAX_DIAG_CHARS = 1200
_MAX_DIAG_LINES = 12

# FACT-tier resolution methods — the ONE canonical set (shared with covering_runner
# and the whole delivery stack). A caller is a FACT only through a RESOLVED edge.
_DETERMINISTIC_METHODS = DETERMINISTIC_RESOLUTION_METHODS

# Positive parse/syntax-error evidence, language-spanning. A KEYWORD match, or a
# compiler/gofmt parse FRAME (`path:line:col: <error word>`). Deliberately does NOT
# match environment failures ("command not found") — those are classified first.
_SYNTAX_KEYWORD_RE = re.compile(
    r"syntaxerror"
    r"|indentationerror"
    r"|taberror"
    r"|invalid\s+syntax"
    r"|syntax\s+error"
    r"|unexpected\s+(?:eof|token|end\s+of\s+input|identifier|keyword|indent|newline|character)"
    r"|unterminated"
    r"|was\s+never\s+closed"
    r"|parse\s+error"
    r"|missing\s+[)\]}]"
    r"|unexpected\s+end",
    re.IGNORECASE,
)
_PARSE_FRAME_RE = re.compile(
    r":\d+:\d+:\s.*(expected|unexpected|found|missing|illegal|declaration)",
    re.IGNORECASE,
)
_GT_TAG_RE = re.compile(r"<gt-[^>]*>", re.IGNORECASE)


# ---------------------------------------------------------------------------
# check_edit_syntax
# ---------------------------------------------------------------------------
def check_edit_syntax(
    file_path: str,
    repo_root: str,
    *,
    executor: Executor | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    """Validate the SYNTAX of a just-edited file using the repo's own toolchain.

    Returns ``{"verdict", "diagnostic", "language", "reason", "checker"}`` where
    ``verdict`` is ``"ok" | "syntax_error" | "unavailable"``. Only ``verdict`` and
    ``diagnostic`` are model-facing; the rest is host-side context for the seam.

    Correct-or-quiet by construction: an unknown/unsupported extension, a missing
    tool, a timeout, or a non-zero exit without positive parse evidence all return
    ``unavailable`` — never a fabricated ``syntax_error``.
    """
    if not file_path:
        return _verdict("unavailable", reason="no_file_path", ext="")
    norm = file_path.replace("\\", "/")
    ext = os.path.splitext(norm)[1].lower()
    abs_path = file_path if os.path.isabs(file_path) else os.path.join(repo_root or "", file_path)

    # Python fast path: parse IN-PROCESS (no spawn, no bytecode cache) when no
    # executor is injected. With an executor, use the subprocess ast.parse form so
    # the check runs inside the task's own interpreter/container.
    if ext == ".py" and executor is None:
        return _check_py_in_process(abs_path, ext)

    cmd = _build_check_command(ext, abs_path)
    if cmd is None:
        # Correct-or-quiet: a language we cannot cheaply + soundly check.
        return _verdict("unavailable", reason="unsupported_language", ext=ext)

    cwd = repo_root or os.path.dirname(abs_path) or "."
    rc, out, err, status = _execute(cmd, cwd, timeout, executor)
    return _classify(rc, out, err, status, ext, cmd)


def _build_check_command(ext: str, path: str) -> list[str] | None:
    """Per-language parse-only command, or None when unsupported (correct-or-quiet).

    Mirrors covering_runner.build_covering_command's extension-dispatch style. Only
    invocations that yield POSITIVE syntax evidence WITHOUT conflating type/module
    errors are listed; everything else is intentionally None (see the honesty table
    in the module docstring)."""
    if ext == ".py":
        # ast.parse only — no bytecode cache written (unlike py_compile). Encoding-safe.
        return [
            "python", "-c",
            "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8',errors='replace')"
            ".read(), sys.argv[1])",
            path,
        ]
    if ext in (".js", ".mjs", ".cjs"):
        return ["node", "--check", path]
    if ext == ".go":
        return ["gofmt", "-e", path]
    if ext == ".rb":
        return ["ruby", "-c", path]
    # .ts/.tsx/.jsx/.rs/.java/unknown -> unavailable (never guess).
    return None


def _check_py_in_process(abs_path: str, ext: str) -> dict[str, Any]:
    """Parse ``abs_path`` with ``ast.parse`` in-process. No subprocess, no bytecode.

    Encoding-safe read (utf-8 / replace). A SyntaxError (incl. IndentationError) is
    POSITIVE evidence -> ``syntax_error`` with the native Python error text. A file
    we cannot read, or a non-SyntaxError parse fault (e.g. null bytes -> ValueError),
    is ambiguous -> ``unavailable`` (correct-or-quiet)."""
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError:
        return _verdict("unavailable", reason="unreadable_file", ext=ext, checker=["ast.parse"])
    try:
        ast.parse(src, filename=os.path.basename(abs_path))
    except SyntaxError as exc:
        diag = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return _verdict("syntax_error", reason="parse_error", ext=ext,
                        checker=["ast.parse"], diagnostic=_bound_text(diag))
    except (ValueError, RecursionError, MemoryError):
        # Not a grammar syntax error (e.g. null bytes) -> do not overclaim.
        return _verdict("unavailable", reason="parse_ambiguous", ext=ext, checker=["ast.parse"])
    return _verdict("ok", reason="parsed", ext=ext, checker=["ast.parse"])


def _execute(
    cmd: list[str], cwd: str, timeout: int, executor: Executor | None
) -> tuple[int | None, str, str, str]:
    """Run ``cmd``, returning ``(exit_code|None, stdout, stderr, status)`` where
    ``status`` is ``"ran" | "timeout" | "spawn_error"``.

    Default path harvests the frozen ``test_runner._run_subprocess`` (byte-identical
    capture/decode/kill). An injected executor is validated against the frozen
    triple contract; any executor fault degrades to ``spawn_error`` so the checker
    can never crash the run (correct-or-quiet)."""
    if executor is None:
        try:
            rc, out, err = _run_subprocess(cmd, cwd, timeout)
        except subprocess.TimeoutExpired:
            return None, "", "", "timeout"
        except Exception:  # noqa: BLE001 -- OSError/FileNotFoundError/etc == spawn failure
            return None, "", "", "spawn_error"
        return int(rc), out or "", err or "", "ran"
    try:
        ret = executor(list(cmd), cwd, timeout)
    except subprocess.TimeoutExpired:
        return None, "", "", "timeout"
    except Exception:  # noqa: BLE001 -- a buggy executor is a spawn failure, never a fail
        return None, "", "", "spawn_error"
    if not isinstance(ret, (tuple, list)) or len(ret) != 3:
        return None, "", "", "spawn_error"
    rc_raw, out, err = ret
    rc: int | None
    if rc_raw is None:
        rc = None
    elif isinstance(rc_raw, int) and not isinstance(rc_raw, bool):
        rc = rc_raw
    else:
        try:
            rc = int(rc_raw)
        except (TypeError, ValueError):
            rc = None  # contract violation -> ambiguous -> unavailable
    return rc, ("" if out is None else str(out)), ("" if err is None else str(err)), "ran"


def _classify(
    rc: int | None, out: str, err: str, status: str, ext: str, cmd: list[str]
) -> dict[str, Any]:
    """Map an execution outcome to a verdict under the positive-evidence law."""
    if status == "timeout":
        return _verdict("unavailable", reason="timeout", ext=ext, checker=cmd)
    if status == "spawn_error":
        return _verdict("unavailable", reason="spawn_error", ext=ext, checker=cmd)
    # status == "ran"
    if rc is None:
        return _verdict("unavailable", reason="no_exit_code", ext=ext, checker=cmd)
    if rc == 0:
        return _verdict("ok", reason="clean_exit", ext=ext, checker=cmd)
    combined = (err or "") + "\n" + (out or "")
    # (1) Environment failure (tool missing / offline / manifest) is NEVER a syntax
    #     error — quiet. Reuses test_runner's canonical classifier (one surface).
    if classify_environment_failure(combined, command=cmd):
        return _verdict("unavailable", reason="environment_failure", ext=ext, checker=cmd)
    # (2) POSITIVE parse evidence + a real non-zero exit -> syntax_error.
    if _looks_like_syntax_error(combined):
        return _verdict("syntax_error", reason="parse_error", ext=ext,
                        checker=cmd, diagnostic=_bound_diagnostic(err, out))
    # (3) Non-zero exit with no positive evidence -> ambiguous -> quiet.
    return _verdict("unavailable", reason="nonzero_no_evidence", ext=ext, checker=cmd)


def _looks_like_syntax_error(text: str) -> bool:
    """POSITIVE evidence that ``text`` reports a parse/syntax error (not an env
    failure). A syntax keyword OR a `path:line:col: <error word>` parse frame."""
    if not text:
        return False
    return bool(_SYNTAX_KEYWORD_RE.search(text) or _PARSE_FRAME_RE.search(text))


def _bound_diagnostic(err: str, out: str) -> str:
    """The toolchain's own error text, preferring stderr, bounded + scrubbed."""
    return _bound_text((err or "").strip() or (out or "").strip())


def _bound_text(text: str) -> str:
    """Trim to the last ``_MAX_DIAG_LINES`` lines / ``_MAX_DIAG_CHARS`` chars and
    strip any ``<gt-*>`` marker (defensive — toolchain text never emits one, but the
    model-facing invariant is absolute)."""
    if not text:
        return ""
    text = _GT_TAG_RE.sub("", text)
    lines = text.splitlines()
    if len(lines) > _MAX_DIAG_LINES:
        lines = lines[-_MAX_DIAG_LINES:]
    text = "\n".join(lines)
    if len(text) > _MAX_DIAG_CHARS:
        text = text[-_MAX_DIAG_CHARS:]
    return text.strip()


def _verdict(
    verdict: str,
    *,
    reason: str,
    ext: str,
    checker: list[str] | None = None,
    diagnostic: str = "",
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "diagnostic": diagnostic,
        "language": ext,
        "reason": reason,
        "checker": checker or [],
    }


# ---------------------------------------------------------------------------
# caller_diff_advisory — the ADVISORY half (never blocking; data for the seam)
# ---------------------------------------------------------------------------
def caller_diff_advisory(
    db_path: str, edited_symbols: set[str] | list[str], *, limit: int = 25
) -> list[dict[str, Any]]:
    """Verified (FACT-tier) NON-test callers of each edited symbol, from graph.db.

    For each edited symbol, the callers that reach it through a FACT-tier edge
    (``_DETERMINISTIC_METHODS``, confidence >= 0.7) — the blast radius the agent's
    grep loop cannot cheaply assemble. Returned as DATA (list of
    ``{"symbol", "caller", "file", "line", "confidence"}``) for the seam to render
    later; this function NEVER blocks and NEVER speaks.

    Same query discipline as ``covering_runner.select_covering_tests``: read-only
    connection, legacy-schema tolerance (no ``resolution_method`` column -> [] since
    provenance is unjudgeable; no ``confidence`` column -> every FACT edge trusted at
    1.0), FACT gate only, correct-or-quiet on any error.

    LEAK-LAW: ``is_test = 1`` callers are EXCLUDED — a test's identity must never
    leak into an advisory. The edited symbol itself is required to be non-test
    (an edit subject is source code).
    """
    syms = {str(s) for s in (edited_symbols or ()) if s}
    if not syms or not db_path or not os.path.isfile(db_path):
        return []
    con = _connect_ro(db_path)
    if con is None:
        return []
    try:
        ecols = _edge_columns(con)
        if "resolution_method" not in ecols:
            return []  # provenance unjudgeable -> correct-or-quiet
        has_conf = "confidence" in ecols
        has_line = "source_line" in ecols
        det = "','".join(sorted(_DETERMINISTIC_METHODS))
        conf_gate = "AND COALESCE(e.confidence, 0) >= 0.7 " if has_conf else ""
        conf_expr = "COALESCE(e.confidence, 1.0)" if has_conf else "1.0"
        line_sel = "e.source_line" if has_line else "NULL"
        sph = ",".join("?" * len(syms))
        rows = con.execute(
            f"SELECT nt.name, ns.name, ns.file_path, {line_sel}, {conf_expr} "
            "FROM edges e "
            "JOIN nodes ns ON ns.id = e.source_id "   # the caller
            "JOIN nodes nt ON nt.id = e.target_id "   # the edited symbol
            f"WHERE nt.name IN ({sph}) AND e.type = 'CALLS' "
            "AND COALESCE(nt.is_test, 0) = 0 "        # edit subject is source code
            "AND COALESCE(ns.is_test, 0) = 0 "        # LEAK-LAW: no test callers
            f"AND LOWER(TRIM(e.resolution_method)) IN ('{det}') {conf_gate}",
            list(syms),
        ).fetchall()
    except Exception:  # noqa: BLE001 -- correct-or-quiet
        return []
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass

    # Dedup by (symbol, caller, file) keeping the max-confidence edge — deterministic.
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for symbol, caller, fpath, line, conf in rows:
        if not symbol or not caller:
            continue
        key = (symbol, caller, fpath or "")
        try:
            c = float(conf) if conf is not None else 1.0
        except (TypeError, ValueError):
            c = 1.0
        cur = best.get(key)
        if cur is None or c > cur["confidence"]:
            best[key] = {
                "symbol": symbol,
                "caller": caller,
                "file": fpath or "",
                "line": int(line) if isinstance(line, int) else None,
                "confidence": c,
            }
    out = sorted(best.values(), key=lambda d: (-d["confidence"], d["symbol"], d["caller"]))
    return out[:limit]
