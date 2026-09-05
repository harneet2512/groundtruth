"""Static patch-delta engine — conservative, advisory-only detection of what a
patch BREAKS or FORGOT (W-C of the enrichment wave).

GroundTruth's strongest verification signal is an EXECUTED covering-test RED
(``covering_runner``), but its selection recall is ~0.02 — the overwhelming
majority of edits get no executed verdict. This module is the STATIC fallback for
that gap. From a diff (before/after file content), ``graph.db``, and the repo
tree, it answers two conservative questions, NEVER blocking:

  (a) SIGNATURE-COMPAT — did a positional-parameter change on an edited function
      leave a FACT-tier caller's call site arity-incompatible? For each edited
      function whose positional bounds changed, the FACT-tier NON-test callers are
      read from the graph — a caller is attributed ONLY on an EXACT normalized
      target-path match, never a LIKE/suffix guess (a ``_`` in a filename is a LIKE
      wildcard; any suffix match collides parallel packages) — the LIVE call-site
      line is parsed conservatively, and a mismatch is emitted ONLY when the call
      was clearly valid against the OLD signature and is clearly INVALID against
      the NEW one. Everything ambiguous abstains (``*args``/``**kwargs``, keyword
      usage, defaulted params covering the delta, decorators that may rewrite the
      call convention, multi-line or unparseable calls, duplicate/ambiguous defs,
      non-Python, a class-body def whose first param is not ``self``/``cls``
      [binding unprovable — method-ness comes from AST context, never a param
      NAME], a dotted call to a plain function [module attribute vs rebound method
      unprovable], an f-string brace expression on the call line [3.12 same-quote
      nesting defeats any line scanner]).

  (b) COMPANION-SURFACE — does an edited symbol have COMPANION surfaces the diff
      did not touch? (1) REGISTRATION/EXPORT: a non-test, non-vendored PYTHON file
      the diff did NOT touch that references >=2 of a newly-added symbol's parallel
      siblings but NOT the added symbol itself — where a reference counts ONLY in a
      REGISTRATION-SHAPED AST context (collection-literal element, dict key/value,
      whole-assignment-RHS alias, ``__all__`` string entry, or an ``__init__.py``
      re-export import). A CALL expression is a CONSUMER and never counts; comments
      and docstrings are invisible to the AST walk by construction; languages we
      cannot parse confidently are NOT counted at all. No framework names
      hardcoded. (2) CO-CHANGE: top co-change partners (via the existing
      ``pretask.cochange`` module) above a minimal historical-co-occurrence floor,
      not touched by the diff.

THE LAWS (identical to the rest of the delivery stack):
- **Correct-or-quiet / conservative.** A false "incompatible caller" or a false
  "forgot to register" is POLLUTION — strictly worse than silence (The
  Distracting Effect, arXiv:2505.06914). Every ambiguity abstains.
- **FACT-tier only** — a caller counts for (a) only through a deterministically
  resolved edge (``DETERMINISTIC_RESOLUTION_METHODS`` AND confidence >= 0.7); a
  ``name_match`` edge is NEVER a fact.
- **Leak law** — ``is_test`` nodes/files are EXCLUDED everywhere. No test name,
  path, or assertion ever enters an advisory row.
- **Honest tiering** — (a) arity mismatch and (b)(1) registration hits carry
  concrete ``file:line`` evidence => WARNING; (b)(2) bare co-change priors =>
  INFO, never more.

EXPLICITLY OUT OF SCOPE (measured to regress): issue-obligation coverage
accounting / any "done-criterion checklist". Also no seam wiring — this is a
pure engine behind ``GT_PATCH_DELTA`` (unset/0 => the entry point returns an
empty result immediately, byte-identical to the module being absent).

LLM-free, deterministic, stdlib + sqlite3 only.
"""

from __future__ import annotations

import ast
import hashlib
import keyword
import os
import re
import sqlite3
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any

from groundtruth.delivery.path_policy import is_test_or_demo, is_vendored_path
from groundtruth.pretask.cochange import cochange_cluster
from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS
from groundtruth.runtime.covering_runner import _connect_ro, _edge_columns

# FACT-tier set — the ONE canonical source (never a re-declared literal).
_DETERMINISTIC_METHODS = DETERMINISTIC_RESOLUTION_METHODS
_FACT_CONFIDENCE_FLOOR = 0.7

# Companion-scan bounds (correct-or-quiet stays cheap; caps keep a post-edit hook
# bounded on a huge repo). All deterministic.
_MAX_SCAN_FILES = 4000
_MAX_FILE_BYTES = 400_000
_MAX_COMPANION_ROWS = 25
_MAX_SIGNATURE_ROWS = 50
_MAX_COCHANGE_ROWS = 5
_MAX_SIBLING_EVIDENCE = 6

# A companion-registry match needs at least this many DISTINCT parallel siblings
# co-referenced in the untouched file (two distinctive names co-occurring is a
# registry-shaped cross-reference; one is incidental).
_MIN_PARALLEL_SIBLINGS = 2
# Co-change partner floor: co-occurred with an edited file in at least this many
# commits (a single shared commit is coincidence, not a prior).
_MIN_COCHANGE_COUNT = 2

# Distinctive-identifier gate for companion cross-reference counting: short and/or
# ultra-generic names match everywhere and would over-fire. Keys on length + a
# small stoplist (language keywords + the most generic verbs/nouns) — no framework
# or benchmark names.
_MIN_DISTINCTIVE_LEN = 3
_GENERIC_NAMES: frozenset[str] = frozenset(keyword.kwlist) | frozenset({
    "run", "main", "get", "set", "add", "put", "pop", "new", "make", "build",
    "call", "init", "test", "name", "type", "self", "cls", "data", "value",
    "item", "items", "key", "keys", "node", "func", "args", "kwargs", "base",
    "util", "utils", "list", "dict", "str", "int", "bool", "obj", "res",
    "result", "out", "tmp", "val", "cfg", "conf", "ctx", "app", "api", "url",
    "the", "and", "for", "not", "all", "any", "map", "row", "col",
})

# Files scanned for registration/export surfaces — PYTHON ONLY (bounce F1 law).
# Counting a reference requires (i) stripping comments/strings and (ii) proving a
# registration-shaped context; both need a real parse. For every other language we
# cannot do that confidently with stdlib alone, so those files are NOT counted at
# all (a config-file entry-point registry is a known, honest gap — never a guess).
_SCAN_EXTS: frozenset[str] = frozenset({".py", ".pyi"})
_PRUNE_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", ".tox", ".venv", "venv", "env", "__pycache__",
    "node_modules", "vendor", "dist", "build", "target", ".mypy_cache",
    ".pytest_cache", ".idea", ".vscode", "site-packages",
})


# ---------------------------------------------------------------------------
# Evidence-carrying result dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SignatureMismatch:
    """A FACT-tier caller whose LIVE call site is arity-incompatible with the new
    signature of an edited function. WARNING tier — concrete file:line evidence."""

    symbol: str
    edited_file: str
    old_min_params: int
    old_max_params: int
    new_min_params: int
    new_max_params: int
    caller: str
    caller_file: str
    caller_line: int
    call_site_text: str      # the literal source line (evidence)
    positional_args: int
    confidence: float
    tier: str = "WARNING"
    resolution_method: str = ""
    before_sha256: str = ""
    after_sha256: str = ""
    caller_source_sha256: str = ""
    edge_id: int | None = None
    definition_id: int | None = None


@dataclass(frozen=True)
class CompanionSurface:
    """An untouched non-test file that registers >=2 parallel siblings of a
    newly-added symbol but NOT the symbol itself. WARNING tier — concrete
    referencing lines are the evidence."""

    symbol: str                                  # the added symbol likely unregistered
    edited_file: str                             # the file it was added to
    file: str                                    # the untouched registration surface
    siblings: tuple[str, ...]                    # parallel sibling names found in `file`
    referencing_lines: tuple[tuple[int, str], ...]  # (line_no, text) per sibling
    tier: str = "WARNING"


@dataclass(frozen=True)
class CochangePartner:
    """A top historical co-change partner of the edited files, not touched by the
    diff. INFO tier — a prior, never more."""

    file: str
    score: float
    count: int
    primaries: tuple[str, ...]
    commits: tuple[str, ...]
    tier: str = "INFO"


@dataclass(frozen=True)
class PatchDeltaResult:
    """The advisory bundle. Empty (and ``abstained`` optionally set) whenever the
    engine is off, has no input, or finds nothing conservative to say."""

    signature_mismatches: list[SignatureMismatch] = field(default_factory=list)
    companion_surfaces: list[CompanionSurface] = field(default_factory=list)
    cochange_partners: list[CochangePartner] = field(default_factory=list)
    abstained: bool = False
    reason: str = ""

    @property
    def is_empty(self) -> bool:
        return not (
            self.signature_mismatches
            or self.companion_surfaces
            or self.cochange_partners
        )


# ---------------------------------------------------------------------------
# Entry point (flag-gated)
# ---------------------------------------------------------------------------
def _flag_on() -> bool:
    return os.environ.get("GT_PATCH_DELTA", "").strip().lower() in {"1", "true", "yes", "on"}


def analyze_patch_delta(
    edited_files: "Mapping[str, tuple[str | None, str]]",
    repo_root: str,
    graph_db: str,
) -> PatchDeltaResult:
    """Advisory-only patch-delta analysis.

    ``edited_files`` maps a repo-relative path to ``(before_content, after_content)``;
    ``before_content is None`` means a NEW file. ``repo_root`` is the repo working
    tree; ``graph_db`` is the path to ``graph.db`` (may be missing — (a) simply
    yields nothing then).

    Returns a :class:`PatchDeltaResult`. Behind ``GT_PATCH_DELTA``: unset/0 returns
    an empty result immediately. Never raises (correct-or-quiet): any internal fault
    degrades to fewer/zero advisories, never a crash or a fabricated row.
    """
    if not _flag_on():
        return PatchDeltaResult()
    if not edited_files or not repo_root:
        return PatchDeltaResult(abstained=True, reason="no_input")

    try:
        sigs = _signature_advisories(edited_files, repo_root, graph_db)
    except Exception:  # noqa: BLE001 -- correct-or-quiet
        sigs = []
    try:
        companions = _companion_surface_advisories(edited_files, repo_root)
    except Exception:  # noqa: BLE001
        companions = []
    try:
        partners = _cochange_advisories(edited_files, repo_root)
    except Exception:  # noqa: BLE001
        partners = []

    return PatchDeltaResult(
        signature_mismatches=sigs,
        companion_surfaces=companions,
        cochange_partners=partners,
    )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").strip().lstrip("./")


# ---------------------------------------------------------------------------
# (a) SIGNATURE-COMPAT
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Sig:
    """Positional bounds of a Python def (all counts INCLUDE a leading self/cls)."""

    min_pos: int   # required positional-or-keyword params (no default)
    max_pos: int   # total positional-or-keyword params
    is_method: bool  # PROVEN instance/class method (class-body def, self/cls first)


def _py_defs(content: str | None) -> "dict[str, list[tuple[Any, bool]]] | None":
    """All Function/AsyncFunction defs keyed by name, each paired with whether the
    def sits DIRECTLY in a ClassDef body (bounce F3: method-ness is an AST-context
    fact, never a first-param-NAME guess). None when the content is absent or not
    parseable Python (=> abstain for this file)."""
    if content is None:
        return None
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    out: dict[str, list[tuple[Any, bool]]] = {}

    def _walk(node: Any, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(child.name, []).append((child, in_class))
                _walk(child, False)  # a def nested in a function is not a method
            elif isinstance(child, ast.ClassDef):
                _walk(child, True)
            else:
                # If/Try/With/... inherit the class-body context (a def inside
                # `if TYPE_CHECKING:` at class level is still a method).
                _walk(child, in_class)

    _walk(tree, False)
    return out


def _py_signature(fn: Any, in_class: bool) -> "_Sig | None":
    """Positional bounds of ``fn``, or None to ABSTAIN.

    Abstains on any construct that makes static arity reasoning unsound: a
    decorator (may rewrite the call convention — click/property/staticmethod/…),
    ``*args``/``**kwargs``, a required keyword-only argument, or (bounce F3) a
    CLASS-BODY def whose first param is not ``self``/``cls`` — its call-site
    binding is unprovable, so no arity claim may rest on it."""
    if fn.decorator_list:
        return None  # a decorator may transform the effective signature
    a = fn.args
    if a.vararg is not None or a.kwarg is not None:
        return None  # *args / **kwargs -> variadic -> abstain
    for d in a.kw_defaults:
        if d is None:
            return None  # a REQUIRED keyword-only arg -> positional reasoning unsafe
    pos = list(a.posonlyargs) + list(a.args)
    total = len(pos)
    required = total - len(a.defaults)  # defaults cover the tail of the positional list
    if in_class:
        first = pos[0].arg if pos else ""
        if first not in ("self", "cls"):
            # F3: a class-body def with a non-self/cls first param — instance
            # method with unconventional naming? staticmethod-by-metaclass?
            # Unprovable -> abstain (the confirmed FP-2 false fire).
            return None
        return _Sig(min_pos=required, max_pos=total, is_method=True)
    return _Sig(min_pos=required, max_pos=total, is_method=False)


def _variant(sig: _Sig, dotted: bool) -> "tuple[int, int] | None":
    """The (min, max) positional bounds AT THE CALL SITE, or None to abstain.

    A dotted call to a PROVEN method binds self/cls implicitly -> drop it. A
    dotted call to a plain function is UNPROVABLE (module-attribute access passes
    every arg; the same function rebound as a class attribute binds the first) ->
    abstain (bounce F3). A bare call to a method (self not bound) -> abstain."""
    if dotted:
        if sig.is_method:
            return (max(0, sig.min_pos - 1), max(0, sig.max_pos - 1))
        return None  # F3: dotted call to a non-method -> binding unprovable
    if sig.is_method:
        return None  # bare call to a self/cls method -> cannot reason -> abstain
    return (sig.min_pos, sig.max_pos)


_KWARG_RE = re.compile(r"[A-Za-z_]\w*\s*=(?!=)")

# F5: a py3.12 nested SAME-quote f-string (f"{d["k"]}") mis-toggles any character
# scanner (the inner quote reads as the string's close). Detect an f-string opening
# that reaches a `{` before its quote closes and ABSTAIN on the whole line.
# Deliberately broader than the 3.12 case (also skips different-quote nesting the
# scanner could handle) — a missed advisory, never a false claim.
_FSTRING_BRACE_RE = re.compile(r"(?i)\b(?:rf|fr|f)(['\"])(?:(?!\1).)*\{")


def _parse_call_args(s: str, open_idx: int) -> "list[str] | None":
    """Top-level argument substrings of the call whose ``(`` is at ``open_idx``.

    Balances ``()[]{}`` and skips string literals so commas/parens inside nested
    expressions or strings never split an argument. Returns None when the call is
    not closed within ``s`` (a multi-line call => abstain)."""
    depth = 0
    quote: str | None = None
    cur: list[str] = []
    args: list[str] = []
    i, n = open_idx, len(s)
    while i < n:
        c = s[i]
        if quote is not None:
            cur.append(c)
            if c == "\\" and i + 1 < n:
                cur.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            cur.append(c)
            i += 1
            continue
        if c in "([{":
            depth += 1
            if depth == 1 and c == "(":
                i += 1  # the outer opening paren itself is not part of an arg
                continue
            cur.append(c)
            i += 1
            continue
        if c in ")]}":
            depth -= 1
            if depth == 0:
                tail = "".join(cur).strip()
                if tail:
                    args.append(tail)
                return args
            cur.append(c)
            i += 1
            continue
        if c == "," and depth == 1:
            args.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    return None  # never closed -> multi-line -> abstain


def _extract_call(line: str, symbol: str) -> "tuple[int, bool] | None":
    """(positional_arg_count, dotted) for the single call to ``symbol`` on ``line``.

    Returns None to ABSTAIN when: ``symbol`` is not called exactly once on the line,
    the call spans multiple lines, any argument is a ``*``/``**`` unpack or a
    keyword argument (positional counting then unreliable), or the line carries an
    f-string brace expression (F5: 3.12 same-quote nesting defeats the scanner)."""
    if _FSTRING_BRACE_RE.search(line):
        return None  # F5: cannot tokenize an f-string interpolation line safely
    cands: list[tuple[int, bool]] = []
    for m in re.finditer(r"\b" + re.escape(symbol) + r"\b", line):
        j = m.end()
        while j < len(line) and line[j] in " \t":
            j += 1
        if j >= len(line) or line[j] != "(":
            continue
        k = m.start() - 1
        while k >= 0 and line[k] in " \t":
            k -= 1
        dotted = k >= 0 and line[k] == "."
        cands.append((j, dotted))
    if len(cands) != 1:
        return None  # zero or ambiguous multiple calls -> abstain
    open_idx, dotted = cands[0]
    args = _parse_call_args(line, open_idx)
    if args is None:
        return None
    for arg in args:
        if arg.startswith("*"):
            return None  # *args / **kwargs unpack -> abstain
        if _KWARG_RE.match(arg):
            return None  # keyword argument -> positional count unreliable -> abstain
    return (len(args), dotted)


def _edge_cols_ok(cols: set[str]) -> bool:
    return {"resolution_method", "source_line"}.issubset(cols)


def _fact_callers(
    con: sqlite3.Connection, symbol: str, edited_file: str
) -> list[dict[str, Any]]:
    """FACT-tier NON-test callers of ``symbol`` DEFINED IN ``edited_file``, with the
    call-site file+line so the live line can be parsed.

    Same FACT discipline as ``edit_check.caller_diff_advisory`` /
    ``select_covering_tests``: deterministic method AND confidence >= 0.7,
    ``is_test = 0`` on BOTH ends (leak law).

    TARGET-PATH LAW (bounce F2 — two confirmed false attributions): the target is
    matched by EXACT NORMALIZED PATH, never LIKE and never a bare suffix. A ``_``
    in a filename is a LIKE any-char wildcard (pkg/a_b.py matched pkg/axb.py) and
    any suffix match collides parallel packages (pkg/api.py matched
    other/pkg/api.py) and name-prefixed siblings (api.py matched legacy_api.py).
    Tolerated stored variants: backslash separators (REPLACE) and a ``./`` or
    ``/`` prefix (explicit IN list) — plus a Python-side ``_norm`` equality
    re-check as belt-and-braces. A graph storing differently-rooted/absolute
    paths yields a MISS (abstain), never a false caller.

    F4: rows are ORDER BY'd deterministically (confidence DESC, caller, file,
    line) so any downstream cap sees a stable order. Correct-or-quiet on any
    error / legacy schema."""
    cols = _edge_columns(con)
    if not _edge_cols_ok(cols):
        return []
    has_conf = "confidence" in cols
    has_srcfile = "source_file" in cols
    det = "','".join(sorted(_DETERMINISTIC_METHODS))
    conf_gate = f"AND COALESCE(e.confidence, 0) >= {_FACT_CONFIDENCE_FLOOR} " if has_conf else ""
    conf_expr = "COALESCE(e.confidence, 1.0)" if has_conf else "1.0"
    srcfile_sel = "e.source_file" if has_srcfile else "ns.file_path"
    norm_f = _norm(edited_file)
    if not norm_f:
        return []
    try:
        rows = con.execute(
            f"SELECT ns.name, ns.file_path, {srcfile_sel}, e.source_line, {conf_expr}, "
            "nt.file_path, LOWER(TRIM(e.resolution_method)), e.id, e.target_id "
            "FROM edges e "
            "JOIN nodes ns ON ns.id = e.source_id "
            "JOIN nodes nt ON nt.id = e.target_id "
            "WHERE nt.name = ? AND e.type = 'CALLS' "
            "AND REPLACE(nt.file_path, '\\', '/') IN (?, ?, ?) "
            "AND COALESCE(nt.is_test, 0) = 0 "
            "AND COALESCE(ns.is_test, 0) = 0 "
            f"AND LOWER(TRIM(e.resolution_method)) IN ('{det}') {conf_gate}"
            f"ORDER BY {conf_expr} DESC, ns.name, ns.file_path, e.source_line",
            (symbol, norm_f, "./" + norm_f, "/" + norm_f),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    for caller, caller_file, src_file, src_line, conf, target_file, method, edge_id, target_id in rows:
        if not caller or src_line is None:
            continue
        # Belt-and-braces exact re-check: the SQL IN list already excludes any
        # wildcard/suffix collision; this pins the invariant against future SQL
        # drift (a mutation here must bite the F2 tests).
        if _norm(target_file or "") != norm_f:
            continue
        try:
            line_no = int(src_line)
        except (TypeError, ValueError):
            continue
        if line_no <= 0:
            continue
        try:
            conf_f = float(conf) if conf is not None else 1.0
        except (TypeError, ValueError):
            conf_f = 1.0
        out.append({
            "caller": caller,
            "caller_file": caller_file or "",
            "site_file": (src_file or caller_file or ""),
            "line": line_no,
            "confidence": conf_f,
            "resolution_method": str(method or ""),
            "edge_id": int(edge_id) if edge_id is not None else None,
            "definition_id": int(target_id) if target_id is not None else None,
        })
    return out


def _line_at(
    repo_root: str,
    rel_file: str,
    line_no: int,
    edited_after: dict[str, str],
    cache: dict[str, list[str]],
    source_hashes: dict[str, str] | None = None,
) -> str:
    """The source text at ``rel_file:line_no`` — from the diff's AFTER content when
    the caller file is itself edited (so a call the agent already fixed reads with
    its new arity), else from disk. Empty string on any miss."""
    norm = _norm(rel_file)
    lines = cache.get(norm)
    if lines is None:
        if norm in edited_after:
            source_text = edited_after[norm]
            lines = source_text.splitlines()
        else:
            try:
                with open(os.path.join(repo_root, norm), "rb") as fh:
                    source_bytes = fh.read()
                    source_text = source_bytes.decode("utf-8", errors="ignore")
                    lines = source_text.splitlines()
            except OSError:
                source_bytes = b""
                source_text = ""
                lines = []
        cache[norm] = lines
        if source_hashes is not None and source_text:
            exact_bytes = (
                edited_after[norm].encode("utf-8")
                if norm in edited_after else source_bytes
            )
            source_hashes[norm] = hashlib.sha256(exact_bytes).hexdigest()
    if 0 < line_no <= len(lines):
        return lines[line_no - 1]
    return ""


def _signature_advisories(
    edited_files: "Mapping[str, tuple[str | None, str]]",
    repo_root: str,
    graph_db: str,
) -> list[SignatureMismatch]:
    """(a) arity-mismatch advisories — see module docstring."""
    if not graph_db or not os.path.isfile(graph_db):
        return []
    edited_after = {_norm(k): v[1] for k, v in edited_files.items()}
    con = _connect_ro(graph_db)
    if con is None:
        return []
    out: list[SignatureMismatch] = []
    line_cache: dict[str, list[str]] = {}
    source_hashes: dict[str, str] = {}
    try:
        for raw_path, (before, after) in edited_files.items():
            if before is None:
                continue  # a new file has no prior signature to break
            edited_file = _norm(raw_path)
            before_defs = _py_defs(before)
            after_defs = _py_defs(after)
            if before_defs is None or after_defs is None:
                continue  # non-Python / unparseable -> abstain (a)
            for name, b_nodes in before_defs.items():
                a_nodes = after_defs.get(name)
                if not a_nodes:
                    continue
                if len(b_nodes) != 1 or len(a_nodes) != 1:
                    continue  # duplicate/overloaded name -> ambiguous -> abstain
                b_fn, b_in_class = b_nodes[0]
                a_fn, a_in_class = a_nodes[0]
                old_sig = _py_signature(b_fn, b_in_class)
                new_sig = _py_signature(a_fn, a_in_class)
                if old_sig is None or new_sig is None:
                    continue
                if old_sig.is_method != new_sig.is_method:
                    continue  # method-ness changed -> abstain
                if (old_sig.min_pos, old_sig.max_pos) == (new_sig.min_pos, new_sig.max_pos):
                    continue  # positional bounds unchanged -> nothing to break
                for site in _fact_callers(con, name, edited_file):
                    line = _line_at(
                        repo_root, site["site_file"], site["line"], edited_after,
                        line_cache, source_hashes,
                    )
                    if not line:
                        continue
                    parsed = _extract_call(line, name)
                    if parsed is None:
                        continue  # abstain (multiline / kwargs / ambiguous / not present)
                    n_args, dotted = parsed
                    v_old = _variant(old_sig, dotted)
                    v_new = _variant(new_sig, dotted)
                    if v_old is None or v_new is None:
                        continue
                    valid_before = v_old[0] <= n_args <= v_old[1]
                    valid_after = v_new[0] <= n_args <= v_new[1]
                    if valid_before and not valid_after:
                        out.append(SignatureMismatch(
                            symbol=name,
                            edited_file=edited_file,
                            old_min_params=v_old[0],
                            old_max_params=v_old[1],
                            new_min_params=v_new[0],
                            new_max_params=v_new[1],
                            caller=site["caller"],
                            caller_file=_norm(site["site_file"]),
                            caller_line=site["line"],
                            call_site_text=line.strip(),
                            positional_args=n_args,
                            confidence=site["confidence"],
                            resolution_method=site["resolution_method"],
                            before_sha256=hashlib.sha256(before.encode("utf-8")).hexdigest(),
                            after_sha256=hashlib.sha256(after.encode("utf-8")).hexdigest(),
                            caller_source_sha256=source_hashes.get(
                                _norm(site["site_file"]), ""
                            ),
                            edge_id=site["edge_id"],
                            definition_id=site["definition_id"],
                        ))
                        if len(out) >= _MAX_SIGNATURE_ROWS:
                            break
                if len(out) >= _MAX_SIGNATURE_ROWS:
                    break
            if len(out) >= _MAX_SIGNATURE_ROWS:
                break
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
    out.sort(key=lambda m: (-m.confidence, m.symbol, m.caller_file, m.caller_line))
    return out[:_MAX_SIGNATURE_ROWS]


# ---------------------------------------------------------------------------
# (b)(1) COMPANION REGISTRATION/EXPORT SURFACES
# ---------------------------------------------------------------------------
def _is_distinctive(name: str) -> bool:
    return (
        len(name) >= _MIN_DISTINCTIVE_LEN
        and name.lower() not in _GENERIC_NAMES
        and name.isidentifier()
    )


def _py_top_names(content: str | None) -> "dict[str, int] | None":
    """Top-level Function/AsyncFunction/Class names -> lineno, or None when the
    content is not parseable Python (the family a registry would enumerate)."""
    if content is None:
        return {}
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    out: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.setdefault(node.name, node.lineno)
    return out


def _py_registration_refs(
    text: str, wanted: set[str], *, allow_imports: bool = False
) -> "dict[str, int]":
    """Family names referenced in REGISTRATION-SHAPED AST contexts only -> lineno.

    Bounce F1: a raw ``\\bname\\b`` text count fired on CONSUMERS and on
    docstring/comment-only mentions. A name now counts ONLY as:
      - an element of a list/tuple/set literal (dispatch/registry enumeration),
      - a dict literal key or value (name -> handler mapping),
      - the ENTIRE RHS of an assignment (an alias/re-export binding),
      - a string entry of ``__all__`` (the export registry),
      - (``allow_imports`` — an ``__init__.py`` package surface only) an
        imported name (the package re-export convention).
    A CALL expression is a CONSUMER and never counts. Comments and docstrings are
    invisible to the AST walk by construction. Unparseable text -> {} (cannot
    judge confidently -> not counted). Deterministic (ast.walk order is fixed for
    fixed input)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return {}
    hits: dict[str, int] = {}

    def _add(node: Any) -> None:
        if isinstance(node, ast.Name) and node.id in wanted and node.id not in hits:
            hits[node.id] = node.lineno

    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for elt in node.elts:
                _add(elt)
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if key is not None:  # None key = ``{**spread}``
                    _add(key)
            for value in node.values:
                _add(value)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = getattr(node, "value", None)
            if value is None:
                continue  # bare annotation ``x: T`` -> no reference
            _add(value)  # RHS exactly a bare Name -> alias/re-export binding
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if (any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets)
                    and isinstance(value, (ast.List, ast.Tuple, ast.Set))):
                for elt in value.elts:
                    if (isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                            and elt.value in wanted and elt.value not in hits):
                        hits[elt.value] = elt.lineno
        elif allow_imports and isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound in wanted and bound not in hits:
                    hits[bound] = node.lineno
    return hits


def _iter_scan_files(repo_root: str) -> "list[str]":
    """Deterministically-ordered repo-relative Python files worth scanning for
    registration surfaces (pruned dirs skipped, capped)."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _PRUNE_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _SCAN_EXTS:
                continue
            abs_p = os.path.join(dirpath, fn)
            rel = _norm(os.path.relpath(abs_p, repo_root))
            found.append(rel)
            if len(found) >= _MAX_SCAN_FILES:
                return found
    return found


def _companion_surface_advisories(
    edited_files: "Mapping[str, tuple[str | None, str]]",
    repo_root: str,
) -> list[CompanionSurface]:
    """(b)(1) registration/export advisories — see module docstring."""
    edited_norm = {_norm(k) for k in edited_files}

    # Per edited file: (distinctive family, distinctive ADDED names). Only files
    # that ADDED at least one distinctive symbol matter (the forgot-to-register
    # signal is about a NEW sibling).
    families: dict[str, tuple[set[str], set[str]]] = {}
    search_names: set[str] = set()
    for raw_path, (before, after) in edited_files.items():
        after_names = _py_top_names(after)
        if not after_names:
            continue
        before_names = _py_top_names(before)
        if before_names is None:
            before_names = {}
        added = set(after_names) - set(before_names)
        fam = {n for n in after_names if _is_distinctive(n)}
        added_d = {n for n in added if _is_distinctive(n)}
        if not added_d or len(fam) < _MIN_PARALLEL_SIBLINGS + 1:
            continue
        families[_norm(raw_path)] = (fam, added_d)
        search_names |= fam
    if not families or not search_names:
        return []

    name_re = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(search_names)) + r")\b")

    rows: list[CompanionSurface] = []
    for rel in _iter_scan_files(repo_root):
        if rel in edited_norm or is_test_or_demo(rel) or is_vendored_path(rel):
            continue
        try:
            path = os.path.join(repo_root, rel)
            if os.path.getsize(path) > _MAX_FILE_BYTES:
                continue
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        if not name_re.search(text):
            continue  # cheap lexical prefilter before the (heavier) parse
        # F1: registration-shaped AST contexts ONLY — a consumer's calls and any
        # docstring/comment mention never count (both confirmed false positives).
        is_init = rel.rsplit("/", 1)[-1] == "__init__.py"
        refs = _py_registration_refs(text, search_names, allow_imports=is_init)
        if len(refs) < _MIN_PARALLEL_SIBLINGS:
            continue
        src_lines = text.splitlines()
        hits: dict[str, tuple[int, str]] = {}
        for name, lineno in refs.items():
            line_text = (
                src_lines[lineno - 1].strip() if 0 < lineno <= len(src_lines) else ""
            )
            hits[name] = (lineno, line_text)
        hit_names = set(hits)
        for edited_file, (fam, added_d) in families.items():
            for symbol in sorted(added_d):
                if symbol in hit_names:
                    continue  # already registered here -> nothing forgotten
                siblings = sorted((fam - {symbol}) & hit_names)
                if len(siblings) < _MIN_PARALLEL_SIBLINGS:
                    continue
                evidence = tuple(hits[s] for s in siblings[:_MAX_SIBLING_EVIDENCE])
                rows.append(CompanionSurface(
                    symbol=symbol,
                    edited_file=edited_file,
                    file=rel,
                    siblings=tuple(siblings),
                    referencing_lines=evidence,
                ))
        if len(rows) >= _MAX_COMPANION_ROWS:
            break
    rows.sort(key=lambda r: (r.symbol, r.file))
    return rows[:_MAX_COMPANION_ROWS]


# ---------------------------------------------------------------------------
# (b)(2) CO-CHANGE PARTNERS (reuse the existing module — INFO only)
# ---------------------------------------------------------------------------
def _cochange_advisories(
    edited_files: "Mapping[str, tuple[str | None, str]]",
    repo_root: str,
) -> list[CochangePartner]:
    """(b)(2) co-change advisories — top historical partners not in the diff."""
    edited_norm = {_norm(k) for k in edited_files}
    result = cochange_cluster(repo_root, list(edited_files.keys()))
    out: list[CochangePartner] = []
    for hit in result.hits:
        hf = _norm(hit.file)
        if hf in edited_norm:
            continue  # a primary / already-touched file is not a forgotten partner
        if hit.count < _MIN_COCHANGE_COUNT:
            continue
        out.append(CochangePartner(
            file=hf,
            score=round(float(hit.score), 6),
            count=int(hit.count),
            primaries=tuple(_norm(p) for p in hit.primaries),
            commits=tuple(hit.commits),
        ))
    out.sort(key=lambda p: (-p.score, p.file))
    return out[:_MAX_COCHANGE_ROWS]
