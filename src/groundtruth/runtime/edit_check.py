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
  .py/.pyi       IN-PROCESS ``ast.parse`` (executor=None) / ``python -c ast.parse``
                 (via executor). Pure parse, no bytecode cache.
  .js/.mjs/.cjs  ``node --check`` — Node's own parse-only flag.
  .go            ``gofmt -e`` — parses to AST, reports parse errors, exit != 0.
  .rb            ``ruby -c`` — syntax-only check ("Syntax OK" / exit != 0).
  .ts/.tsx/.jsx  ``node -e <TS parse probe>`` — PARSE-ONLY via the typescript
                 package's ``createSourceFile`` ``parseDiagnostics`` (2026-07-24
                 language-coverage fix; ``node --check`` cannot parse TS/JSX and
                 ``tsc --noEmit`` would conflate type/module errors). If the
                 typescript module cannot be resolved the probe prints the
                 ``GT_TS_UNAVAILABLE`` sentinel and exits 3, mapped to verdict
                 ``unavailable`` — an unexecuted check is never an executed-ok
                 and never a fabricated syntax error (2026-07-29 tier-honesty
                 fix; see _TS_PARSE_SCRIPT resolution notes).
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
import posixpath
import re
import subprocess
import tempfile
import traceback
from typing import Any, Callable

from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS
from groundtruth.runtime.covering_runner import _connect_ro, _edge_columns
from groundtruth.runtime.test_runner import _run_subprocess, classify_environment_failure

# The injectable execution boundary — the SAME frozen contract Wave-1 established
# (test_runner / covering_runner). ``None`` selects the host subprocess path.
Executor = Callable[[list[str], str, int], "tuple[int | None, str, str]"]

# Positive "checker could not run" declaration (2026-07-29). A probe that cannot
# load its parser prints this sentinel and exits a distinct code, so an UNEXECUTED
# check can never be classified as an executed clean parse (see _classify step 0).
_CHECKER_UNAVAILABLE_SENTINEL = "GT_TS_UNAVAILABLE"

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
_PY_FILE_RE = re.compile(r'^\s*File "([^"]+)", line (\d+)(?:,.*)?$')
_PY_ERROR_RE = re.compile(r"^(SyntaxError|IndentationError|TabError):\s*(.*)$")
_CONTAINER_REPO_ROOTS = ("/testbed", "/home/user", "/workspace", "/app", "/repo")


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
    # L-1b: name the file REPO-RELATIVE in any diagnostic (not by basename) so the
    # model does not have to guess the path back. If file_path arrived relative, that
    # IS the repo-relative name; if absolute, derive it against repo_root (fall back to
    # the basename only when relpath is impossible — different drive / no root).
    rel_name = norm
    if os.path.isabs(file_path):
        try:
            rel_name = (
                os.path.relpath(abs_path, repo_root or ".").replace("\\", "/")
                if repo_root
                else os.path.basename(norm)
            )
        except ValueError:  # e.g. different Windows drive -> no relative path exists
            rel_name = os.path.basename(norm)

    # Python fast path: parse IN-PROCESS (no spawn, no bytecode cache) when no
    # executor is injected. With an executor, use the subprocess ast.parse form so
    # the check runs inside the task's own interpreter/container.
    if ext in (".py", ".pyi") and executor is None:
        return _check_py_in_process(abs_path, ext, rel_name)

    cmd = _build_check_command(ext, abs_path)
    if cmd is None:
        # Correct-or-quiet: a language we cannot cheaply + soundly check.
        return _verdict("unavailable", reason="unsupported_language", ext=ext)

    cwd = repo_root or os.path.dirname(abs_path) or "."
    rc, out, err, status = _execute(cmd, cwd, timeout, executor)
    result = _classify(rc, out, err, status, ext, cmd)
    # SS edit-diagnostic refinement. The explicit kill-switch preserves the
    # executor's legacy diagnostic bytes; Profile-2 activates the stable
    # source-only diagnostic in production. Exact replay fidelity still depends
    # on executing under the recorded interpreter/toolchain.
    if (
        ext in (".py", ".pyi")
        and result.get("verdict") == "syntax_error"
        and _ss_edit_diag_enabled()
    ):
        normalized = _normalize_python_syntax_diagnostic(
            str(result.get("diagnostic") or ""),
            rel_name,
            abs_path=abs_path,
            repo_root=repo_root,
        )
        if normalized:
            result["diagnostic"] = normalized
    # L-1b for EVERY language. Python has had `_normalize_python_syntax_diagnostic` above; every
    # other toolchain (gofmt, node --check, the TS probe) is handed ``abs_path`` and echoes it, so
    # its diagnostic read `/testbed/pkg/x.ts:1:22: ...` — the container path, which the docstring of
    # this function explicitly forbids in model-facing bytes ("name the file REPO-RELATIVE ... so the
    # model does not have to guess the path back"). Only the literal path of the file under check is
    # rewritten, so no other content can be altered; on the Python path the normalizer already
    # produced ``rel_name`` and this is a byte no-op.
    if result.get("diagnostic"):
        result["diagnostic"] = _relativize_diagnostic(str(result["diagnostic"]), abs_path, rel_name)
    result = _apply_name_check(result, ext, abs_path, rel_name, executor)
    return result


def check_edit_syntax_bytes(
    file_path: str,
    source_bytes: bytes,
    repo_root: str,
    *,
    executor: Executor | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    """Check one immutable captured postimage and return path-stable evidence.

    The ordinary path API reopens the worktree. Observation compilation already
    owns exact postimage bytes, so reopening would permit the hash and checker to
    observe different revisions. A private temporary file gives every local
    parser the captured bytes while all temporary identities are scrubbed from
    the returned diagnostic and checker command. The executor contract cannot
    transfer bytes into a remote container, so that mode fails quiet instead of
    claiming it parsed bytes the executor could not observe.
    """

    if not isinstance(source_bytes, bytes):
        raise TypeError("source_bytes must be bytes")
    normalized = (file_path or "").replace("\\", "/")
    extension = os.path.splitext(normalized)[1].lower()
    if not normalized or not extension:
        return _verdict("unavailable", reason="no_file_path", ext=extension)
    if executor is not None:
        return _verdict(
            "unavailable",
            reason="captured_bytes_executor_unsupported",
            ext=extension,
        )
    with tempfile.TemporaryDirectory(prefix="gt-syntax-capture-") as directory:
        captured_path = os.path.join(directory, "captured" + extension)
        with open(captured_path, "wb") as handle:
            handle.write(source_bytes)
        result = check_edit_syntax(
            captured_path,
            repo_root,
            executor=executor,
            timeout=timeout,
        )
        aliases = {
            captured_path,
            captured_path.replace("\\", "/"),
            os.path.basename(captured_path),
        }
        if repo_root:
            try:
                aliases.add(os.path.relpath(captured_path, repo_root).replace("\\", "/"))
            except ValueError:
                pass
        sanitized = dict(result)
        diagnostic = str(sanitized.get("diagnostic") or "")
        for alias in sorted((item for item in aliases if item), key=len, reverse=True):
            diagnostic = diagnostic.replace(alias, normalized)
        sanitized["diagnostic"] = diagnostic
        checker = sanitized.get("checker")
        if isinstance(checker, list):
            sanitized["checker"] = [
                normalized if str(item) in aliases else str(item) for item in checker
            ]
        return sanitized


def _relativize_diagnostic(diag: str, abs_path: str, rel_name: str) -> str:
    """Replace the checked file's ABSOLUTE path with its repo-relative name (L-1b).

    Language-agnostic and conservative: substitutes only the literal ``abs_path`` (and its
    forward-slash form, since toolchains normalise separators inconsistently on Windows). Longest
    form first so a partial overlap cannot corrupt the text."""
    if not diag or not abs_path or not rel_name:
        return diag
    forms = {abs_path, abs_path.replace("\\", "/"), os.path.normpath(abs_path)}
    for form in sorted((f for f in forms if f), key=len, reverse=True):
        if form and form != rel_name:
            diag = diag.replace(form, rel_name)
    return diag


def _ss_edit_diag_enabled() -> bool:
    """Resolve the internal edit.syntax behavior version without adding a CAP row.

    An explicit non-empty ``GT_SS_EDIT_DIAG`` is the replay/debug kill-switch.
    Otherwise the existing effective RL profile controls the refinement: Profile-2
    emits stable source-only diagnostics; an explicit legacy/off profile preserves
    the legacy formatting path (and executor bytes when an executor is supplied).
    """
    explicit = os.environ.get("GT_SS_EDIT_DIAG")
    if explicit is not None and explicit.strip() != "":
        return explicit.strip() == "1"
    try:
        from groundtruth.runtime.rl_profile import resolve_default_token

        return resolve_default_token(os.environ) == "2"
    except Exception:  # noqa: BLE001 -- a profile-resolution fault preserves legacy bytes
        return False


def _edit_check_names_on() -> bool:
    """GT_EDIT_CHECK_NAMES (default off => byte-identical): after a CLEAN parse, additionally surface
    DEFINITE undefined-name errors (pyflakes UndefinedName/UndefinedLocal/UndefinedExport — a runtime
    NameError the agent's edit introduced), NOT noisy style/type diagnostics. Closes the 17-feature
    audit's edit-check delivery gap: GT parse-only vs Aider's default full linter. Correct-or-quiet:
    pyflakes absent / no undefined name => the 'ok' verdict is unchanged."""
    return os.environ.get("GT_EDIT_CHECK_NAMES", "0").strip() == "1"


def _check_py_undefined_names(src: bytes, rel_name: str) -> str:
    """In-process pyflakes pass -> ONE native undefined-name diagnostic line, or ''. ONLY the
    DEFINITE-error classes (UndefinedName/UndefinedLocal/UndefinedExport); unused-import/style are
    excluded (correct-or-quiet, high precision). Any fault / tool-absent => ''."""
    try:
        from pyflakes import checker as _pf_checker  # type: ignore
        from pyflakes import messages as _pf_messages  # type: ignore
    except Exception:  # noqa: BLE001 -- not in THIS interpreter; try the mounted substrate's
        # AUDIT 2026-07-24: the in-process path runs under the harness interpreter (mini-swe-agent),
        # which need not have pyflakes. GT's substrate python is mounted read-only at /opt/gt and
        # BUNDLES pyflakes (pure-python => cross-version sys.path import is safe). Without this the
        # capability was a per-interpreter lottery. Still correct-or-quiet if nothing is found.
        try:
            import glob as _glob
            import sys as _sys

            _added = False
            for _sp in _glob.glob("/opt/gt/python/lib/python3*/site-packages"):
                if _sp not in _sys.path:
                    _sys.path.append(_sp)
                    _added = True
            if not _added:
                return ""
            from pyflakes import checker as _pf_checker  # type: ignore
            from pyflakes import messages as _pf_messages  # type: ignore
        except Exception:  # noqa: BLE001 -- tool genuinely absent => quiet
            return ""
    try:
        tree = ast.parse(src, filename=rel_name)
        w = _pf_checker.Checker(tree, filename=rel_name)
    except Exception:  # noqa: BLE001 -- a parse fault is the caller's syntax path, not ours
        return ""
    _defs = tuple(
        c
        for c in (
            getattr(_pf_messages, "UndefinedName", None),
            getattr(_pf_messages, "UndefinedLocal", None),
            getattr(_pf_messages, "UndefinedExport", None),
        )
        if isinstance(c, type)
    )
    if not _defs:
        return ""
    for m in sorted(getattr(w, "messages", []), key=lambda x: getattr(x, "lineno", 0)):
        if isinstance(m, _defs):
            try:
                msg = m.message % m.message_args
            except Exception:  # noqa: BLE001
                msg = "undefined name"
            return _python_diagnostic(
                rel_name=rel_name,
                line=int(getattr(m, "lineno", 1) or 1),
                source="",
                offset=None,
                error_type="NameError",
                message=str(msg),
            )
    return ""


def _name_check_interpreter() -> str:
    """The interpreter used for the in-container undefined-name probe.

    AUDIT 2026-07-24 (pre-dispatch): the probe MUST NOT depend on the TASK container happening to
    have pyflakes — that made the capability a per-task lottery (silently quiet everywhere pyflakes
    was absent, i.e. the exact "trigger absent" trap). GT's own substrate python is mounted into the
    task container at /opt/gt (read-only) and BUNDLES pyflakes (docker/Dockerfile.gt-substrate), so
    prefer it; ``GT_PYTHON`` (exported by the substrate image) overrides; plain ``python`` is the
    last-resort fallback (still correct-or-quiet: the probe exits 0 when pyflakes is absent)."""
    cand = (os.environ.get("GT_PYTHON") or "").strip()
    if cand and os.path.exists(cand):
        return cand
    if os.path.exists("/opt/gt/python/bin/python3"):
        return "/opt/gt/python/bin/python3"
    return "python"


def _build_name_check_command(ext: str, path: str) -> "list[str] | None":
    """Executor (in-container) undefined-name probe via pyflakes, or None if unsupported. Prints ONE
    ``NameError:`` line on a definite undefined name; exits 0 silently when pyflakes is absent or the
    file is clean (correct-or-quiet). Runs under ``_name_check_interpreter`` (the mounted substrate
    python, which bundles pyflakes) so availability does not depend on the task container's env."""
    if ext != ".py":
        return None
    script = (
        "import ast,sys\n"
        "try:\n from pyflakes import checker,messages\n"
        "except Exception: sys.exit(0)\n"
        "p=sys.argv[1]\n"
        "try:\n t=ast.parse(open(p,'rb').read(),p); w=checker.Checker(t,p)\n"
        "except Exception: sys.exit(0)\n"
        "D=tuple(c for c in (getattr(messages,'UndefinedName',None),getattr(messages,'UndefinedLocal',None),getattr(messages,'UndefinedExport',None)) if isinstance(c,type))\n"
        "for m in sorted(getattr(w,'messages',[]),key=lambda x:getattr(x,'lineno',0)):\n"
        " if isinstance(m,D):\n"
        "  try: msg=m.message%m.message_args\n"
        "  except Exception: msg='undefined name'\n"
        "  print('%s:%d: NameError: %s'%(p,getattr(m,'lineno',1),msg)); break\n"
    )
    return [_name_check_interpreter(), "-I", "-c", script, path]


def _apply_name_check(
    result: dict, ext: str, abs_path: str, rel_name: str, executor: "Executor | None"
) -> dict:
    """Post-syntax undefined-name refinement (flag-gated). Only upgrades a clean ``ok`` to
    ``name_error`` on a DEFINITE undefined name. Correct-or-quiet: any fault leaves ``result``."""
    if not _edit_check_names_on() or result.get("verdict") != "ok" or ext != ".py":
        return result
    diag = ""
    try:
        if executor is None:
            try:
                with open(abs_path, "rb") as fh:
                    diag = _check_py_undefined_names(fh.read(), rel_name)
            except OSError:
                diag = ""
        else:
            cmd = _build_name_check_command(ext, abs_path)
            if cmd is not None:
                _rc, out, _err, status = _execute(
                    cmd, os.path.dirname(abs_path) or ".", 10, executor
                )
                out = (out or "").strip()
                # `_execute` returns status "ran" | "timeout" | "spawn_error" (see its docstring) —
                # NEVER "ok". The first cut compared against "ok", so this branch could not execute
                # and the ENTIRE subprocess name-check was dead by construction. The in-process leg
                # (executor is None) was unaffected, which is precisely why local tests passed while
                # GT_EDIT_CHECK_NAMES came back UNPROVEN from the live run: production injects an
                # executor, so production only ever took the dead branch.
                if status == "ran" and "NameError:" in out:
                    diag = out.splitlines()[0].strip()
                    # L-1b (see check_edit_syntax): a diagnostic names the file REPO-RELATIVE so the
                    # model never has to guess the path back. The subprocess probe is handed
                    # ``abs_path`` and echoes it, so it would emit the raw container path
                    # (`/testbed/pkg/x.py:2: NameError: ...`) — inconsistent with the in-process leg
                    # and with every other diagnostic GT emits. Rewrite the prefix only.
                    if diag.startswith(abs_path):
                        diag = rel_name + diag[len(abs_path) :]
                    else:
                        alt = abs_path.replace("\\", "/")
                        if diag.startswith(alt):
                            diag = rel_name + diag[len(alt) :]
    except Exception:  # noqa: BLE001
        return result
    if diag:
        return _verdict(
            "name_error",
            reason="undefined_name",
            ext=ext,
            checker=list(result.get("checker") or []) + ["pyflakes"],
            diagnostic=_bound_text(diag),
        )
    # AUDIT 2026-07-24 — OBSERVABILITY: record that the name leg RAN even when it found nothing.
    # Previously `pyflakes` entered ``checker`` only on an upgrade, so a clean file was
    # indistinguishable from "the name check never executed" (e.g. the module was missing). That
    # ambiguity is what made GT_EDIT_CHECK_NAMES unprovable from the live ledger. Verdict/diagnostic
    # are untouched — this is a provenance annotation only.
    out = dict(result)
    out["checker"] = list(result.get("checker") or []) + ["pyflakes:clean"]
    return out


# TypeScript/TSX/JSX PARSE-ONLY probe (audit 2026-07-24). Emits ONE `path:line:col: error TSxxxx:
# msg` frame (the compiler's own wording — §0 native voice) and exits 1 on a real syntax error;
# exits 0 silently when clean. When the bundled `typescript` module cannot be loaded it prints
# the GT_TS_UNAVAILABLE sentinel and exits 3 -> verdict `unavailable` (2026-07-29: the silent
# exit-0 form let an UNEXECUTED check classify as executed-ok and feed the completion cert's
# syntax head as a real PASS). Never a fabricated error either way.
#
# RESOLUTION (fixed 2026-07-24 by the hardened build gate, which failed on exactly this): `node -e`
# resolves `require` from CWD, so a repo WITHOUT its own node_modules can only reach the substrate's
# copy by absolute path — and the single hardcoded path used at first
# (/opt/gt/node/lib/node_modules/typescript) DOES NOT EXIST in the published image. The probe then
# exited 0 and every such repo silently reported "ok", i.e. the 76%-coverage-hole fix was half dead
# for precisely the repos that need it most. Rather than guess a path a third time, try the known
# global layouts in order and let GT_TS_MODULE override. Still correct-or-quiet: if none resolve the
# probe declares GT_TS_UNAVAILABLE (exit 3) -> `unavailable`, never a fabricated syntax error.
_TS_PARSE_SCRIPT = (
    "let ts;"
    "const c=[process.env.GT_TS_MODULE,'typescript',"
    "'/opt/gt/tsmod/node_modules/typescript',"
    "'/opt/gt/node/lib/node_modules/typescript',"
    "'/opt/gt/node/node_modules/typescript',"
    "'/usr/lib/node_modules/typescript','/usr/local/lib/node_modules/typescript'];"
    "for(const m of c){if(!m)continue;try{ts=require(m);break}catch(e){}}"
    "if(!ts){"
    "try{ts=require(require.resolve('typescript',"
    "{paths:['/opt/gt/node/lib/node_modules','/opt/gt/node/node_modules']}))}catch(e){}}"
    "if(!ts){console.error('GT_TS_UNAVAILABLE');process.exit(3)}"
    "const fs=require('fs');const p=process.argv[1];"
    "let src;try{src=fs.readFileSync(p,'utf8')}catch(e){process.exit(0)}"
    "const sf=ts.createSourceFile(p,src,ts.ScriptTarget.Latest,false);"
    "const d=(sf.parseDiagnostics||[])[0];"
    "if(!d){process.exit(0)}"
    "const lc=sf.getLineAndCharacterOfPosition(d.start||0);"
    "const m=ts.flattenDiagnosticMessageText(d.messageText,' ');"
    "console.log(p+':'+(lc.line+1)+':'+(lc.character+1)+': error TS'+d.code+': '+m);"
    "process.exit(1);"
)


def _build_check_command(ext: str, path: str) -> list[str] | None:
    """Per-language parse-only command, or None when unsupported (correct-or-quiet).

    Mirrors covering_runner.build_covering_command's extension-dispatch style. Only
    invocations that yield POSITIVE syntax evidence WITHOUT conflating type/module
    errors are listed; everything else is intentionally None (see the honesty table
    in the module docstring)."""
    if ext in (".py", ".pyi"):
        # ast.parse only — no bytecode cache written (unlike py_compile). Encoding-safe.
        return [
            "python",
            "-I",
            "-c",
            "import ast,sys; ast.parse(open(sys.argv[1],'rb').read(), sys.argv[1])",
            path,
        ]
    if ext in (".js", ".mjs", ".cjs"):
        return ["node", "--check", path]
    if ext in (".ts", ".tsx", ".jsx"):
        # AUDIT 2026-07-24 — LANGUAGE-COVERAGE FIX. Measured live (run 30121930273, superjson):
        # 13 of 17 edit opportunities returned `dependency_unavailable:unsupported_language`
        # because .ts/.tsx had NO at-edit checker — 76% of edits on a TypeScript task were
        # unverifiable. `node --check` cannot parse TS. The substrate ALREADY bundles the
        # `typescript` package (docker/Dockerfile.gt-substrate: npm install -g ... typescript),
        # so the capability existed and was simply unwired at the edit boundary.
        # PARSE-ONLY by construction (`parseDiagnostics` from createSourceFile) — the same
        # honesty contract as the other languages: NO type errors, NO module resolution, so a
        # missing import or an unresolved type can never be reported as a syntax error.
        # Correct-or-quiet: if the typescript module is absent the probe declares
        # GT_TS_UNAVAILABLE (exit 3) -> verdict `unavailable`, never an executed-ok.
        return ["node", "-e", _TS_PARSE_SCRIPT, path]
    if ext == ".go":
        return ["gofmt", "-e", path]
    if ext == ".rb":
        return ["ruby", "-c", path]
    # .ts/.tsx/.jsx/.rs/.java/unknown -> unavailable (never guess).
    return None


def _check_py_in_process(abs_path: str, ext: str, rel_name: str | None = None) -> dict[str, Any]:
    """Parse ``abs_path`` with ``ast.parse`` in-process. No subprocess, no bytecode.

    Raw-byte parsing honors the source's declared encoding. A SyntaxError
    (incl. IndentationError) is
    POSITIVE evidence -> ``syntax_error`` with the native Python error text. A file
    we cannot read, or a non-SyntaxError parse fault (e.g. null bytes -> ValueError),
    is ambiguous -> ``unavailable`` (correct-or-quiet).

    ``rel_name`` (L-1b): the REPO-RELATIVE display name stamped into the diagnostic's
    ``File "…"`` line so the model reads the same path it edited (default: basename,
    the pre-L-1b behaviour, for direct callers that pass none)."""
    try:
        with open(abs_path, "rb") as fh:
            src = fh.read()
    except OSError:
        return _verdict("unavailable", reason="unreadable_file", ext=ext, checker=["ast.parse"])
    try:
        ast.parse(src, filename=rel_name or os.path.basename(abs_path))
    except SyntaxError as exc:
        # The in-process path must obey the same behavior switch as the executor path.
        # Replay commonly has no live environment executor, so an unconditional stable
        # formatter here would silently defeat the explicit legacy/off kill-switch.
        if _ss_edit_diag_enabled():
            diag = _format_python_syntax_error(exc, rel_name or os.path.basename(abs_path))
        else:
            diag = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return _verdict(
            "syntax_error",
            reason="parse_error",
            ext=ext,
            checker=["ast.parse"],
            diagnostic=_bound_text(diag),
        )
    except (ValueError, RecursionError, MemoryError):
        # Not a grammar syntax error (e.g. null bytes) -> do not overclaim.
        return _verdict("unavailable", reason="parse_ambiguous", ext=ext, checker=["ast.parse"])
    return _apply_name_check(
        _verdict("ok", reason="parsed", ext=ext, checker=["ast.parse"]),
        ext,
        abs_path,
        rel_name or os.path.basename(abs_path),
        None,
    )


def _python_diagnostic(
    *,
    rel_name: str,
    line: int,
    source: str,
    offset: int | None,
    error_type: str,
    message: str,
) -> str:
    """Render the stable, native Python syntax-error subset used across runtimes."""
    parts = [f'File "{rel_name}", line {max(1, int(line or 1))}']
    source = (source or "").rstrip("\r\n")
    if source:
        parts.append("    " + source)
        if isinstance(offset, int) and offset > 0:
            prefix = "".join("\t" if char == "\t" else " " for char in source[: offset - 1])
            parts.append("    " + prefix + "^")
    parts.append(f"{error_type}: {message}".rstrip())
    return "\n".join(parts)


def _format_python_syntax_error(exc: SyntaxError, rel_name: str) -> str:
    return _python_diagnostic(
        rel_name=rel_name,
        line=int(exc.lineno or 1),
        source=str(exc.text or ""),
        offset=int(exc.offset) if isinstance(exc.offset, int) else None,
        error_type=type(exc).__name__,
        message=str(getattr(exc, "msg", "") or exc),
    )


def _normalize_python_syntax_diagnostic(
    text: str,
    rel_name: str,
    *,
    abs_path: str = "",
    repo_root: str = "",
) -> str:
    """Remove interpreter stack frames while preserving the parser's own error facts.

    Python's ``ast.parse`` subprocess includes version-specific ``ast.py`` frames.  Only the
    final source frame, source line, caret position, exception class, and message are stable
    model-facing facts.
    """
    lines = (text or "").splitlines()
    error_index = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if _PY_ERROR_RE.match(lines[index].strip())
        ),
        None,
    )
    if error_index is None:
        return ""
    error_match = _PY_ERROR_RE.match(lines[error_index].strip())
    assert error_match is not None
    # A SyntaxError's source details belong only to the final traceback frame.
    # Searching farther backward can splice a target frame to a later internal one.
    frame_rows = [
        (index, match)
        for index in range(error_index)
        if (match := _PY_FILE_RE.match(lines[index])) is not None
    ]
    if not frame_rows:
        return f"{error_match.group(1)}: {error_match.group(2)}".rstrip()
    file_index, file_match = frame_rows[-1]
    if not _python_frame_matches(
        file_match.group(1), rel_name, abs_path=abs_path, repo_root=repo_root
    ):
        return f"{error_match.group(1)}: {error_match.group(2)}".rstrip()
    source = ""
    offset: int | None = None
    if file_index + 1 < error_index:
        displayed = lines[file_index + 1]
        source = displayed[4:] if displayed.startswith("    ") else displayed.strip()
    if file_index + 2 < error_index:
        caret = lines[file_index + 2]
        caret = caret[4:] if caret.startswith("    ") else caret
        position = caret.find("^")
        if position >= 0:
            offset = position + 1
    return _python_diagnostic(
        rel_name=rel_name,
        line=int(file_match.group(2)),
        source=source,
        offset=offset,
        error_type=error_match.group(1),
        message=error_match.group(2),
    )


def _python_frame_matches(
    frame_path: str,
    rel_name: str,
    *,
    abs_path: str = "",
    repo_root: str = "",
) -> bool:
    """Whether a traceback frame names the source file being checked.

    Accept the exact repository-relative identity, exact host identity, or that
    same identity under a known task-container mount. Arbitrary suffix matches
    are forbidden because another checkout can contain the same relative path.
    """

    def _norm(path: str) -> str:
        value = (path or "").replace("\\", "/")
        if not value:
            return ""
        while "//" in value:
            value = value.replace("//", "/")
        return posixpath.normpath(value).rstrip("/")

    frame = _norm(frame_path)
    rel = _norm(rel_name)
    if not frame or not rel or frame.startswith("<"):
        return False
    identities = {rel}
    if abs_path:
        identities.add(_norm(abs_path))
    if repo_root:
        identities.add(_norm(repo_root) + "/" + rel)
    identities.update(root + "/" + rel for root in _CONTAINER_REPO_ROOTS)
    return frame in identities


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
        if type(rc) is not int:
            return None, "", "", "spawn_error"
        if not all(value is None or type(value) is str for value in (out, err)):
            return None, "", "", "spawn_error"
        return rc, out or "", err or "", "ran"
    try:
        ret = executor(list(cmd), cwd, timeout)
    except subprocess.TimeoutExpired:
        return None, "", "", "timeout"
    except Exception:  # noqa: BLE001 -- a buggy executor is a spawn failure, never a fail
        return None, "", "", "spawn_error"
    if not isinstance(ret, (tuple, list)) or len(ret) != 3:
        return None, "", "", "spawn_error"
    rc_raw, out, err = ret
    if rc_raw is not None and type(rc_raw) is not int:
        return None, "", "", "spawn_error"
    if not all(value is None or type(value) is str for value in (out, err)):
        return None, "", "", "spawn_error"
    rc: int | None = rc_raw
    return rc, ("" if out is None else out), ("" if err is None else err), "ran"


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
    combined = (err or "") + "\n" + (out or "")
    # (0) A checker that POSITIVELY declares it could not run (the TS probe's
    #     GT_TS_UNAVAILABLE sentinel + exit 3) is an UNEXECUTED check — verdict
    #     ``unavailable`` (pass-with-record downstream: _SYNTAX_MAP maps it to
    #     UNKNOWN, never PASS), NEVER an executed-ok. Checked BEFORE rc == 0 so a
    #     clamped/laundered exit code cannot re-classify it as a clean parse.
    #     (2026-07-29 tier-honesty fix: the probe used to exit 0 silently, and
    #     rc == 0 -> "ok" fed the completion cert's syntax head as a real PASS.)
    if _CHECKER_UNAVAILABLE_SENTINEL in combined:
        return _verdict("unavailable", reason="checker_module_unavailable", ext=ext, checker=cmd)
    if rc == 0:
        return _verdict("ok", reason="clean_exit", ext=ext, checker=cmd)
    # (1) Environment failure (tool missing / offline / manifest) is NEVER a syntax
    #     error — quiet. Reuses test_runner's canonical classifier (one surface).
    if classify_environment_failure(combined, command=cmd):
        return _verdict("unavailable", reason="environment_failure", ext=ext, checker=cmd)
    # (2) POSITIVE parse evidence + a real non-zero exit -> syntax_error.
    if _looks_like_syntax_error(combined):
        return _verdict(
            "syntax_error",
            reason="parse_error",
            ext=ext,
            checker=cmd,
            diagnostic=_bound_diagnostic(err, out),
        )
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
            "JOIN nodes ns ON ns.id = e.source_id "  # the caller
            "JOIN nodes nt ON nt.id = e.target_id "  # the edited symbol
            f"WHERE nt.name IN ({sph}) AND e.type = 'CALLS' "
            "AND COALESCE(nt.is_test, 0) = 0 "  # edit subject is source code
            "AND COALESCE(ns.is_test, 0) = 0 "  # LEAK-LAW: no test callers
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
