"""In-container GroundTruth patch for mini-swe-agent (observation interception).

Injected into the task container and loaded at interpreter startup via a .pth
file in site-packages (primary) or an append to mini-swe-agent's default.py
(backup). Patches the environment's execute() method to append GT evidence
after edit/view commands.

Attachment mapping (GT integration guide -> mini-swe-agent):
  run_action            -> Environment.execute
  classify_tool_event   -> _classify(command)
  observation text      -> output["output"] (rendered into <output> by the
                           model's format_observation_messages, so appended
                           text reaches the agent verbatim)
  GT_BASELINE switch    -> _GT_BASELINE early no-op

Per-turn <gt-evidence> is built DIRECTLY from graph.db (tree-sitter, ALL
languages) via the same deterministic, categorical-fact-gated pillars the
host-side brief uses (resolved-witness / caller-contract / sibling +
edit-target callee contracts). This is pure SQL and cross-language by
construction — it replaces the old `gt_hook.py understand/verify` route, which
was Python-`ast`-only (`.py`-filtered) and therefore emitted EMPTY evidence on
the ~70% of DeepSWE tasks that are Go/Rust/TS/JS.

The pillar logic is PORTED INLINE here (stdlib-only) rather than imported from
`groundtruth.pretask.*`, because only the two single files gt_hook.py +
gt_mini_patch.py (plus /tmp/graph.db) are injected into the task container —
the full `groundtruth` package is NOT importable in-container. The categorical
FACT gate (`_DETERMINISTIC_METHODS`) + stdlib-shadow guard are reproduced
verbatim from curation_map / v1r_brief so no name_match edge is ever laundered
as a fact (parity with the brief).

gt_hook.py is still injected at /opt/gt/gt_hook.py for the agent's optional
manual use, but the AUTOMATIC per-view/per-edit evidence no longer routes
through it.
"""
from __future__ import annotations

import os
import re
import subprocess

_GT_BASELINE = bool(os.environ.get("GT_BASELINE"))
_ROOT_FILE = os.environ.get("GT_ROOT_FILE", "/opt/gt/gt_root.txt")
_HOOK_TIMEOUT = int(os.environ.get("GT_HOOK_TIMEOUT", "30"))

# ---------------------------------------------------------------------------
# Categorical FACT gate (ported verbatim from groundtruth.pretask.curation_map
# DETERMINISTIC_RESOLUTION_METHODS). A cross-file call edge is a FACT only when
# its resolution_method is one of these STRUCTURAL methods; a `name_match` edge
# (even a single-candidate one, scored 0.9) is a NAME GUESS, never a fact.
# Reproduced inline because the groundtruth package is NOT importable in the
# task container (only gt_hook.py + gt_mini_patch.py + /tmp/graph.db injected).
# ---------------------------------------------------------------------------
_DETERMINISTIC_METHODS: frozenset[str] = frozenset(
    {
        "same_file", "import", "import_type", "type_flow", "verified_unique",
        "impl_method", "inherited", "unique_method", "return_type",
        "lsp", "lsp_verified",
    }
)

# Stdlib/builtin module names whose attribute calls (os.walk, json.loads, ...)
# get name-matched to a same-named PROJECT function by the indexer. Ported from
# v1r_brief._STDLIB_MODULES; defends against a DETERMINISTIC-tagged false fact.
_STDLIB_MODULES: frozenset[str] = frozenset(
    {
        "os", "sys", "re", "io", "json", "math", "time", "copy", "glob", "uuid",
        "shutil", "random", "typing", "logging", "pathlib", "datetime", "string",
        "decimal", "inspect", "warnings", "argparse", "textwrap", "itertools",
        "functools", "operator", "collections", "subprocess", "contextlib",
    }
)
_STDLIB_SHADOW_RE = re.compile(r"([A-Za-z_][\w.]*)\.([A-Za-z_]\w*)\s*\(")

# per-file-once dedup, keyed (kind, relpath)
_seen: set[tuple[str, str]] = set()
# Layer-A consensus fires once per run (first source-view), like the OH wrapper.
_consensus_fired = False
# Consensus PROGRESSIVE (Layer-B) + OVERRIDE-on-divergence (OH parity): remember the
# scope set so subsequent in-scope views get "also in scope" reinforcement, and if the
# agent wanders off-scope for a while, re-anchor consensus on where it actually is.
_consensus_scope: set[str] = set()
_offscope_views = 0
# L5 (trajectory governor, minimal port): track actions/edits/loops so a stuck
# trajectory gets ONE nudge instead of burning to maxiter unguarded (the OH governor's
# core job; the full L5Governor cannot run here — execute() has no max_iter — so we
# port the two highest-value heuristics: scaffold-trap + repeated-command loop).
_action_count = 0
_source_edit_count = 0
_cmd_history: list[str] = []
_l5_fired = False
# Additional L5 governor behaviours (each fires once): unsafe-finish (submit with no
# source edit) and repeated-test-failure (the same test fails again after an edit —
# OH's hook_same_failure_persisted / hypothesis-falsified).
_l5_finish_fired = False
_l5_failure_fired = False
_test_fail_history: list[str] = []
# L6 (incremental freshness, minimal port): the gt_hook understand AST cache
# (/tmp/gt_index.json) has no mtime invalidation, and graph.db is frozen at base commit.
# After a source EDIT we invalidate the cache + best-effort single-file reindex so the
# next understand/consensus/verify sees the agent's NEW code, not base-commit.
_GT_INDEX_CACHE = os.environ.get("GT_INDEX_CACHE", "/tmp/gt_index.json")
# COMPLETENESS / co-change fires once on the first source edit (the multi-file scope
# signal DeepSWE entirely lacked — OH ships it from the cochanges table).
_cochange_fired = False
# diagnostic: one-time marker so trajectory analysis can tell
# "patch never loaded" from "loaded but no evidence"
_marker_sent = False

# Source-file extensions GT indexes (matches gt-index language set).
_SRC_EXT = (
    ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".rs", ".java", ".rb",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".kt", ".scala", ".swift",
)

# Edit-shaped commands: sed -i, tee, patch, apply_patch, redirects, heredocs.
_EDIT_RE = re.compile(
    r"(^|[|&;]\s*)(sed\s+-i|tee\b|patch\b|apply_patch\b)"
    r"|>>?\s*\S+"
    r"|<<\s*'?[A-Z_]+'?\s*>\s*\S+",
)
# Read-shaped commands: cat, grep, head, tail, etc.
_VIEW_RE = re.compile(
    r"(^|[|&;]\s*)(cat|grep|rg|head|tail|less|more|view|nl|awk|sed\s+-n)\b",
)


def _root() -> str:
    try:
        return (open(_ROOT_FILE).read().strip()) or "/"
    except Exception:  # noqa: BLE001
        return "/"


# Python/Node in-place file WRITE (the agent's DOMINANT JS edit shape: a python heredoc
# `python3 << EOF ... open('file','w') ... EOF`). The filename lives INSIDE the heredoc
# body, so a redirect/heredoc-strip scan misses it entirely (the bug the JS re-audit found:
# 24/36 real gold-file edits were uncaught). Match the open()/writeFileSync target directly.
_PY_WRITE_RE = re.compile(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*['"][wa]""")
_JS_WRITE_RE = re.compile(r"""(?:writeFileSync|appendFileSync|writeFile)\(\s*['"]([^'"]+)['"]""")
# sed -i / tee / patch / apply_patch, at line start or after a shell separator.
_EDIT_KW_RE = re.compile(r"(?:^|[|&;]\s*)(sed\s+-i|tee\b|patch\b|apply_patch\b)")


def _src_tokens(text: str) -> list[str]:
    out: list[str] = []
    for tok in re.split(r"\s+", text or ""):
        t = tok.strip("\"'`()<>;|&")
        if t.endswith(_SRC_EXT) and "*" not in t and "$" not in t:
            out.append(t)
    return out


def _edit_target(cmd: str) -> str | None:
    """The SOURCE file this command WRITES, or None. Covers every shape the real agent uses:
      - redirect to a source file (`cat > x.js`, `... >> x.js`);
      - sed -i / tee / apply_patch on a source arg (incl. multi-line sed-append);
      - python/node in-place write (`open('x.js','w'|'a')`, `writeFileSync('x.js')`) — incl.
        inside a heredoc body.
    A redirect to a NON-source path (`cat x.js > /tmp/x.bak`, `git diff x.js > /tmp/p.txt`)
    is NOT a source write — that falls to _view_target (read) or to nothing."""
    if not cmd:
        return None
    nohd = cmd.split("<<", 1)[0] if "<<" in cmd else cmd  # shell scans exclude heredoc body
    # 1. redirect whose TARGET is a source file
    for mm in re.finditer(r">>?\s*([^\s'\"<>|&;]+)", nohd):
        t = mm.group(1).strip("\"'`()")
        if t.endswith(_SRC_EXT) and "*" not in t and "$" not in t:
            return t
    # 2. sed -i / tee / apply_patch -> the source-file argument (last source token)
    first = cmd.split("\n", 1)[0]
    if _EDIT_KW_RE.search(first.lstrip()) or _EDIT_KW_RE.search(first):
        toks = _src_tokens(nohd)
        if toks:
            return toks[-1]
    # 3. python/node in-place write (scans the FULL cmd incl. heredoc body)
    for rx in (_PY_WRITE_RE, _JS_WRITE_RE):
        m = rx.search(cmd)
        if m and m.group(1).endswith(_SRC_EXT) and "*" not in m.group(1):
            return m.group(1)
    return None


def _view_target(cmd: str) -> str | None:
    """A SOURCE file being READ (cat/grep/head/...) without being written."""
    head = (cmd or "").split("\n", 1)[0].lstrip()
    if not _VIEW_RE.search(head):
        return None
    toks = _src_tokens(head)
    return toks[0] if toks else None


def _first_src_file(cmd: str) -> str | None:  # kept for compatibility
    return _edit_target(cmd) or _view_target(cmd)


def _classify(cmd: str) -> tuple[str | None, str | None]:
    """Map a bash command to (kind, file). A WRITE to a source file (_edit_target) takes
    priority over a READ (_view_target). Verified by replaying the FULL real agent command
    stream offline — sed, heredoc cat, multi-line sed, python/node open-write, redirects."""
    if not cmd:
        return None, None
    et = _edit_target(cmd)
    if et:
        return "post_edit", et
    vt = _view_target(cmd)
    if vt:
        return "post_view", vt
    return None, None


def _db_path() -> str:
    return os.environ.get("GT_GRAPH_DB", "/tmp/graph.db")


def _has_columns(con) -> tuple[bool, bool]:
    """(has_confidence, has_resolution_method) for the edges table.
    Ported from curation_map._has_columns."""
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(edges)").fetchall()}
    except Exception:  # noqa: BLE001
        return (False, False)
    return ("confidence" in cols, "resolution_method" in cols)


def _is_stdlib_shadow(code: str, target_name: str) -> bool:
    """True when ``code`` calls ``<stdlib_module>.<target_name>(`` — a stdlib
    attribute call the indexer name-matched to a project function of the same
    name. Ported from v1r_brief._is_stdlib_shadow. Language-agnostic."""
    if not code or not target_name:
        return False
    for m in _STDLIB_SHADOW_RE.finditer(code):
        head = m.group(1).split(".")[0]
        if m.group(2) == target_name and head in _STDLIB_MODULES:
            return True
    return False


def _code_at(repo_root: str, rel_file: str, line: int) -> str:
    """The source line at (rel_file, line), 1-based, or '' on any error."""
    if not rel_file or not line or line <= 0:
        return ""
    try:
        with open(os.path.join(repo_root, rel_file), encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
        if 0 < line <= len(lines):
            return lines[line - 1].strip()
    except OSError:
        pass
    return ""


def _norm_fp(file_path: str) -> str:
    return (file_path or "").replace("\\", "/").lstrip("./").lstrip("/")


def _top_func_names(con, file_path: str, limit: int = 3) -> list[str]:
    """Most-referenced non-test Function/Method names in the file (suffix-LIKE
    match, so a stored `pkg/foo.go` matches a relative `foo.go`). Cross-language:
    pure node-label query, no per-language branch. Mirrors v1r_brief._top_function_names
    (issueless variant — per-view has no issue-anchor context; the host brief owns
    issue-anchored selection)."""
    out: list[str] = []
    try:
        rows = con.execute(
            "SELECT n.name, COUNT(e.id) AS rc FROM nodes n "
            "LEFT JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' "
            "WHERE (n.file_path = ? OR n.file_path LIKE ?) "
            "AND n.label IN ('Function','Method') AND COALESCE(n.is_test,0)=0 "
            "GROUP BY n.id ORDER BY rc DESC, n.name LIMIT ?",
            (file_path, "%" + os.path.basename(file_path), limit),
        ).fetchall()
        for (name, _rc) in rows:
            if name and name not in out:
                out.append(name)
    except Exception:  # noqa: BLE001
        pass
    return out


def _resolved_witnesses_for_file(con, file_path: str, repo_root: str, max_each: int = 2) -> list[dict]:
    """Deterministic-provenance caller AND callee witnesses for ``file_path``.

    Ported from v1r_brief._resolved_witnesses_for_file (pure SQL, cross-language).
    A witness is emitted ONLY when its edge resolution_method is in
    ``_DETERMINISTIC_METHODS``; name_match is NEVER a witness. The same
    stdlib-shadow guard the brief applies is applied here. Correct-or-quiet."""
    _, has_method = _has_columns(con)
    if not has_method:
        return []  # cannot judge provenance -> emit nothing (never launder)
    det_sql = "','".join(sorted(_DETERMINISTIC_METHODS))
    nfp = _norm_fp(file_path)
    out: list[dict] = []
    try:
        caller_rows = con.execute(
            f"""
            SELECT nsrc.file_path, e.source_line, nsrc.name, nt.name
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path LIKE ? AND nsrc.file_path != nt.file_path
              AND COALESCE(nsrc.is_test,0) = 0 AND e.source_line > 0
              AND LOWER(TRIM(e.resolution_method)) IN ('{det_sql}')
            ORDER BY e.source_line LIMIT ?
            """,
            ("%" + nfp, max_each * 4),
        ).fetchall()
        for caller_file, line, caller_name, target_name in caller_rows:
            code = _code_at(repo_root, caller_file, line)
            if _is_stdlib_shadow(code, target_name or ""):
                continue
            out.append({"direction": "caller", "file_path": caller_file,
                        "line": int(line) if line else 0, "symbol": caller_name or "",
                        "target": target_name or "", "code": code})
            if sum(1 for w in out if w["direction"] == "caller") >= max_each:
                break

        callee_rows = con.execute(
            f"""
            SELECT nt.file_path, e.source_line, nt.name, nsrc.name, nt.start_line
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS'
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path LIKE ? AND nt.file_path != nsrc.file_path
              AND COALESCE(nt.is_test,0) = 0
              AND LOWER(TRIM(e.resolution_method)) IN ('{det_sql}')
            ORDER BY e.source_line LIMIT ?
            """,
            ("%" + nfp, max_each * 4),
        ).fetchall()
        for callee_file, source_line, callee_name, src_name, def_line in callee_rows:
            call_code = _code_at(repo_root, file_path, source_line)
            if _is_stdlib_shadow(call_code, callee_name or ""):
                continue
            out.append({"direction": "callee", "file_path": callee_file,
                        "line": int(def_line) if def_line else 0, "symbol": callee_name or "",
                        "target": src_name or "",
                        "code": _code_at(repo_root, callee_file, def_line) if def_line else ""})
            if sum(1 for w in out if w["direction"] == "callee") >= max_each:
                break
    except Exception:  # noqa: BLE001
        return []
    return out


def _caller_contract_for_file(con, file_path: str, repo_root: str, func_names: list[str]) -> str:
    """Categorical, correct-or-quiet caller evidence.
    Ported from v1r_brief._caller_contract_for_file. A cross-file caller renders as a
    confident FACT (``name() in file:line``) ONLY over a DETERMINISTIC edge; name_match
    is never laundered — at/above the floor it renders as a bare `file:line (unverified)`
    location hint with no caller-name claim, facts-first. Cross-language: pure SQL."""
    if not func_names:
        return ""
    has_conf, has_method = _has_columns(con)
    conf_sel = "e.confidence" if has_conf else "0.0"
    method_sel = "e.resolution_method" if has_method else "''"
    det_sql = "','".join(sorted(_DETERMINISTIC_METHODS))
    nfp = _norm_fp(file_path)
    fact_parts: list[str] = []
    unverified_parts: list[str] = []
    try:
        for fname in func_names[:2]:
            rows = con.execute(
                f"""
                SELECT nsrc.file_path, e.source_line, nsrc.name, {conf_sel}, {method_sel}
                FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.name = ? AND nt.file_path LIKE ?
                  AND nsrc.file_path != nt.file_path AND COALESCE(nsrc.is_test,0) = 0
                  AND e.source_line > 0
                ORDER BY CASE WHEN LOWER(TRIM({method_sel})) IN ('{det_sql}') THEN 0 ELSE 1 END,
                         {conf_sel} DESC, e.source_line
                LIMIT ?
                """,
                (fname, "%" + nfp, 8),
            ).fetchall()
            for caller_file, source_line, caller_name, conf, method in rows:
                try:
                    conf_f = float(conf) if conf is not None else 0.0
                except (TypeError, ValueError):
                    conf_f = 0.0
                code = _code_at(repo_root, caller_file, source_line)
                if _is_stdlib_shadow(code, fname):
                    continue
                is_fact = (method or "").strip().lower() in _DETERMINISTIC_METHODS
                if is_fact:
                    snippet = code if len(code) <= 80 else code[:77] + "..."
                    rendered = (f"{caller_name}() in {caller_file}:{source_line} `{snippet}`"
                                if snippet else f"{caller_name}() in {caller_file}:{source_line}")
                    if rendered not in fact_parts:
                        fact_parts.append(rendered)
                elif conf_f >= 0.5 or not has_conf:
                    hint = f"{caller_file}:{source_line}"
                    if hint not in unverified_parts:
                        unverified_parts.append(hint)
                if len(fact_parts) >= 3:
                    break
            if len(fact_parts) >= 3:
                break
    except Exception:  # noqa: BLE001
        return ""
    if fact_parts:
        return " | ".join(fact_parts[:3])
    if unverified_parts:
        return " | ".join(unverified_parts[:2])
    return ""


def _sibling_context(con, file_path: str, func_names: list[str]) -> str:
    """Sibling functions at the same scope — parallel patterns to follow.
    Ported from v1r_brief._sibling_context. Cross-language: pure node query."""
    if not func_names:
        return ""
    try:
        rows = con.execute(
            "SELECT DISTINCT n.name FROM nodes n "
            "WHERE (n.file_path = ? OR n.file_path LIKE ?) "
            "AND n.label IN ('Function','Method') AND COALESCE(n.is_test,0)=0 "
            "AND n.name NOT IN ({}) ORDER BY n.start_line LIMIT 8".format(
                ",".join("?" * len(func_names))),
            (file_path, "%" + os.path.basename(file_path), *func_names),
        ).fetchall()
        names = [r[0] for r in rows if r[0] and len(r[0]) > 2 and not r[0].startswith("_")]
        return ", ".join(names[:5]) if names else ""
    except Exception:  # noqa: BLE001
        return ""


def _edit_target_callee_contracts(con, file_path: str, func_names: list[str],
                                  max_funcs: int = 3, max_callees: int = 3) -> list[str]:
    """Verified callees (signature + location) of the edit-target functions.
    Ported from contract_map.edit_target_callee_contracts (the deciding
    "what does the method I'm editing CALL, and how" fact). name_match callees
    are NEVER included. Cross-language: pure SQL over edges/nodes + signatures."""
    if not func_names:
        return []
    _, has_method = _has_columns(con)
    nfp = _norm_fp(file_path)
    det_sql = "','".join(sorted(_DETERMINISTIC_METHODS))
    method_clause = (f"AND LOWER(TRIM(e.resolution_method)) IN ('{det_sql}')"
                     if has_method else "")
    out: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    try:
        for fname in func_names[:max_funcs]:
            if not fname:
                continue
            rows = con.execute(
                f"""
                SELECT nt.name, nt.signature, nt.file_path, nt.start_line
                FROM nodes nsrc
                JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS' {method_clause}
                JOIN nodes nt ON e.target_id = nt.id
                WHERE nsrc.name = ? AND nsrc.file_path LIKE ?
                  AND COALESCE(nt.is_test,0) = 0
                  AND nt.signature IS NOT NULL AND TRIM(nt.signature) != ''
                ORDER BY nt.start_line LIMIT ?
                """,
                (fname, "%" + nfp, max_callees * 3),
            ).fetchall()
            added = 0
            for callee_name, sig, callee_file, line in rows:
                if added >= max_callees:
                    break
                if callee_name == fname and _norm_fp(callee_file) == nfp:
                    continue
                key = (fname, callee_name or "", callee_file or "")
                if key in seen:
                    continue
                seen.add(key)
                sig = (sig or "").strip()
                loc = f" ({callee_file}:{int(line)})" if line else ""
                out.append(f"[CALLEE] {fname} -> {sig}{loc}")
                added += 1
    except Exception:  # noqa: BLE001
        return []
    return out


def _norm_rel(p: str) -> str:
    """Normalize a path for scope-membership comparison."""
    return (p or "").replace("\\", "/").lstrip("./").lower()


def _query_scope(rel: str) -> list[str]:
    """Graph 1-hop neighbours of `rel`, confidence-gated (>= 0.5). Shared by the
    first-view consensus and the override re-anchor."""
    db = os.environ.get("GT_GRAPH_DB", "/tmp/graph.db")
    if not os.path.isfile(db):
        return []
    out: list[str] = []
    try:
        import sqlite3
        con = sqlite3.connect(db)
        base = os.path.basename(rel)
        q = (
            "SELECT DISTINCT n2.file_path FROM nodes n1 "
            "JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id) "
            "JOIN nodes n2 ON n2.id = (CASE WHEN e.source_id = n1.id "
            "                          THEN e.target_id ELSE e.source_id END) "
            "WHERE (n1.file_path = ? OR n1.file_path LIKE ?) "
            "AND n2.file_path != n1.file_path AND n2.file_path IS NOT NULL "
            "AND COALESCE(e.confidence, 0) >= 0.5 ORDER BY e.confidence DESC LIMIT 6"
        )
        try:
            for (fp,) in con.execute(q, (rel, "%" + base)):
                if fp and fp not in out:
                    out.append(fp)
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return []
    return out


def _consensus_progressive(rel: str) -> str:
    """Consensus Layer-B (progressive) + OVERRIDE-on-divergence — OH parity.
    On subsequent source-views: if the file is in the established scope, reinforce it
    once ("also in GT scope"); if the agent has wandered OFF-scope repeatedly, RE-ANCHOR
    consensus on where it actually is now (OH's prefer-divergent-evidence rescue)."""
    global _offscope_views
    if _GT_BASELINE or not _consensus_scope:
        return ""
    n = _norm_rel(rel)

    def _short(p: str) -> str:
        return "/".join((p or "").replace("\\", "/").split("/")[-2:])

    if n in _consensus_scope:
        _offscope_views = 0
        key = ("consensus_b", n)
        if key in _seen:
            return ""
        _seen.add(key)
        return f'\n<gt-scope note="in-scope">\n[GT] {_short(rel)}: also in GT scope.\n</gt-scope>'
    # off-scope view
    _offscope_views += 1
    if _offscope_views < 3:
        return ""
    _offscope_views = 0
    key = ("consensus_override", n)
    if key in _seen:
        return ""
    _seen.add(key)
    scope = _query_scope(rel)
    _consensus_scope.add(n)
    for s in scope:
        _consensus_scope.add(_norm_rel(s))
    lines = [f"1. {_short(rel)} — you have moved here; re-grounding scope"]
    for i, s in enumerate(scope[:4], 2):
        lines.append(f"{i}. {_short(s)} — graph-connected")
    return ('\n<gt-scope reason="re-anchored">\n' + "\n".join(lines)
            + "\nGT re-anchored scope on your current file — confirm the edit target with grep.\n</gt-scope>")


def _consensus_block(rel: str, root: str) -> str:
    """Layer-A CONSENSUS (architecture parity with the OH wrapper's <gt-scope>).

    On the FIRST source-view, deliver the graph-connected SCOPE around the file the
    agent just opened — re-grounding it the moment it starts exploring, the same role
    consensus plays on the OpenHands path. Correct-or-quiet: we list the connected
    scope and tell the agent to confirm the edit target with grep; we do NOT anoint a
    single "primary target" (the imperative steer) here — that confident claim lives in
    the brief's gt-localization, now gated to require >=2 issue anchors. Pure graph
    1-hop neighbours; empty/absent graph -> a minimal scope note, never a guess."""
    global _consensus_fired
    if _consensus_fired:
        return ""
    _consensus_fired = True
    try:
        db = os.environ.get("GT_GRAPH_DB", "/tmp/graph.db")
        scope: list[str] = []
        if os.path.isfile(db):
            import sqlite3
            con = sqlite3.connect(db)
            base = os.path.basename(rel)
            q = (
                "SELECT DISTINCT n2.file_path FROM nodes n1 "
                "JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id) "
                "JOIN nodes n2 ON n2.id = (CASE WHEN e.source_id = n1.id "
                "                          THEN e.target_id ELSE e.source_id END) "
                # Confidence gate (parity with OH _detect_scope, which filters >= 0.7):
                # the graph is 70-80% name_match; without this, 0.2-confidence SPECULATIVE
                # neighbours were shown identically to verified edges as "graph-connected".
                # >= 0.5 keeps CERTIFIED + CANDIDATE, drops SPECULATIVE (correct-or-quiet).
                "WHERE (n1.file_path = ? OR n1.file_path LIKE ?) "
                "AND n2.file_path != n1.file_path AND n2.file_path IS NOT NULL "
                "AND COALESCE(e.confidence, 0) >= 0.5 "
                "ORDER BY e.confidence DESC "
                "LIMIT 6"
            )
            try:
                for (fp,) in con.execute(q, (rel, "%" + base)):
                    if fp and fp not in scope:
                        scope.append(fp)
            finally:
                con.close()

        # Remember the scope so Layer-B progressive + override can re-ground later views.
        _consensus_scope.add(_norm_rel(rel))
        for _s in scope:
            _consensus_scope.add(_norm_rel(_s))

        def _short(p: str) -> str:
            r = (p or "").replace("\\", "/")
            return "/".join(r.split("/")[-2:]) if "/" in r else r

        if not scope:
            return (
                f'\n<gt-scope files="1">\n'
                f"1. {_short(rel)} — in scope (you are viewing this); GT could not expand "
                f"scope from the graph — confirm the edit target with grep.\n</gt-scope>"
            )
        lines = [f"1. {_short(rel)} — in scope (you are viewing this)"]
        for i, fp in enumerate(scope[:4], 2):
            lines.append(f"{i}. {_short(fp)} — graph-connected")
        return (
            f'\n<gt-scope files="{len(scope[:4]) + 1}">\n'
            + "\n".join(lines)
            + "\nThese files are related in scope; GT has not confirmed a single primary "
            "target — confirm the edit target with grep.\n</gt-scope>"
        )
    except Exception:  # noqa: BLE001 -- correct-or-quiet, never break the loop
        return ""


def _evidence_body(kind: str, rel: str, root: str) -> str:
    """Build the <gt-evidence> body from graph.db (pure SQL, cross-language).

    post_view : resolved-witness facts + caller-contract for the viewed file.
    post_edit : edit-target callee contracts + resolved witnesses.
    Both obey the categorical FACT gate (_DETERMINISTIC_METHODS) + stdlib-shadow
    guard, so no name_match edge is ever laundered as a fact (parity with the
    brief). Correct-or-quiet: empty body -> the caller emits nothing.

    This replaces the old gt_hook.py understand/verify shell-out, which was
    Python-ast-only (.py-filtered at gt_hook.py:4110) and therefore EMPTY on
    every Go/Rust/TS/JS file. graph.db is tree-sitter over ALL languages."""
    db = _db_path()
    if not os.path.isfile(db):
        return ""
    import sqlite3
    try:
        con = sqlite3.connect(db)
    except Exception:  # noqa: BLE001
        return ""
    lines: list[str] = []
    try:
        func_names = _top_func_names(con, rel, limit=3)
        if kind == "post_edit":
            # What the edited functions CALL, and how to call it correctly.
            for cl in _edit_target_callee_contracts(con, rel, func_names):
                if cl not in lines:
                    lines.append(cl)
        # Resolved cross-file witnesses (caller + callee FACTS) for both kinds.
        for w in _resolved_witnesses_for_file(con, rel, root, max_each=2):
            arrow = "called by" if w["direction"] == "caller" else "calls"
            loc = f"{w['file_path']}:{w['line']}" if w["line"] else w["file_path"]
            sym = w["symbol"] or "?"
            snippet = f" `{w['code']}`" if w.get("code") else ""
            ln = f"[WITNESS] {sym} {arrow} -> {loc}{snippet}".rstrip()
            if ln not in lines:
                lines.append(ln)
        # Caller-contract line for the viewed file (facts-first, unverified hint
        # only when no fact exists). Mainly meaningful on a view.
        if kind == "post_view":
            cc = _caller_contract_for_file(con, rel, root, func_names)
            if cc:
                ln = f"[CALLERS] {cc}"
                if ln not in lines:
                    lines.append(ln)
            sib = _sibling_context(con, rel, func_names)
            if sib:
                ln = f"[SIBLINGS] {sib}"
                if ln not in lines:
                    lines.append(ln)
    except Exception:  # noqa: BLE001 -- correct-or-quiet
        return ""
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines[:6]).strip()


def _evidence(cmd: str) -> str:
    if _GT_BASELINE:
        return ""
    kind, f = _classify(cmd)
    if not kind or not f:
        return ""
    root = _root()
    rel = os.path.relpath(f, root) if os.path.isabs(f) else f
    key = (kind, rel)
    if key in _seen:
        return ""
    _seen.add(key)
    ev = _evidence_body(kind, rel, root)
    if not ev:
        return ""
    return f"\n<gt-evidence kind=\"{kind}\" file=\"{rel}\">\n{ev}\n</gt-evidence>"


_contract_seen: set[str] = set()


def _graph_contract_block(rel: str) -> str:
    """CROSS-LANGUAGE per-edit contract (parity with OH post_edit [SIGNATURE]/[CALLERS]).
    gt_hook.verify is Python-AST-only — it no-ops on every Go/Rust/TS/JS edit
    (_get_modified_files filters to .py). But the graph (tree-sitter, ALL languages) has
    nodes.signature + CALLS edges for every language. So we deliver the contract + blast
    radius straight from graph.db, which works cross-language by construction. Per-file
    once. Correct-or-quiet: empty graph / no functions -> nothing."""
    if _GT_BASELINE or rel in _contract_seen:
        return ""
    _contract_seen.add(rel)
    try:
        db = os.environ.get("GT_GRAPH_DB", "/tmp/graph.db")
        if not os.path.isfile(db):
            return ""
        import sqlite3
        con = sqlite3.connect(db)
        base = os.path.basename(rel)
        rows: list = []
        try:
            q = (
                "SELECT n.id, n.name, n.signature, "
                " (SELECT COUNT(DISTINCT e.source_id) FROM edges e "
                "    WHERE e.target_id = n.id AND e.type='CALLS') AS ncallers, "
                " (SELECT COUNT(DISTINCT n2.file_path) FROM edges e JOIN nodes n2 ON n2.id = e.source_id "
                "    WHERE e.target_id = n.id AND e.type='CALLS') AS nfiles "
                "FROM nodes n WHERE (n.file_path = ? OR n.file_path LIKE ?) "
                "AND n.label IN ('Function','Method') AND COALESCE(n.is_test,0)=0 "
                "ORDER BY ncallers DESC LIMIT 3"
            )
            rows = con.execute(q, (rel, "%" + base)).fetchall()
            # PRESERVE: behavioural properties of the top (most-called) function — the
            # cross-language equivalent of OH's guard_removed/return_shape safety family
            # (gt_hook's is Python-AST-only). Properties are tree-sitter-mined per language
            # (thin on Go, richer on Python/TS) — correct-or-quiet where absent.
            preserve: list[str] = []
            top_rows = [r for r in rows if (r[2] or "").strip()]
            if top_rows:
                pq = ("SELECT kind, value FROM properties WHERE node_id = ? "
                      "AND kind IN ('guard_clause','conditional_return','exception_flow','return_shape') "
                      "LIMIT 5")
                for kind, val in con.execute(pq, (top_rows[0][0],)):
                    val = (val or "").strip()
                    if not val:
                        continue
                    tag = {"guard_clause": "PRESERVE", "conditional_return": "PRESERVE",
                           "exception_flow": "[RAISES]", "return_shape": "[RETURNS]"}.get(kind, "PRESERVE")
                    preserve.append(f"{tag} {val[:120]}")
        finally:
            con.close()
        rows = [r for r in rows if (r[2] or "").strip()]
        if not rows:
            return ""
        out = [f'<gt-contract file="{os.path.basename(rel)}">']
        for _id, name, sig, ncallers, nfiles in rows:
            sig = (sig or "").strip()
            out.append(f"[SIGNATURE] {sig}")
            if ncallers and int(ncallers) > 0:
                out.append(f"[CALLERS] {name}: {int(ncallers)} caller(s) in {int(nfiles)} "
                           "file(s) — preserve this interface")
        for p in preserve:
            out.append(p)
        out.append("</gt-contract>")
        return "\n" + "\n".join(out)
    except Exception:  # noqa: BLE001 -- correct-or-quiet
        return ""


def _cochange_block(rel: str) -> str:
    """COMPLETENESS / co-change (parity with OH post_edit [CO-CHANGE]). On the first
    source EDIT, surface files that HISTORICALLY change together with the edited file —
    the graph's `cochanges` table, git-mined at index time (Zimmermann ICSE'04). This is
    the multi-file completeness signal DeepSWE entirely lacked — the recurring 'edited the
    primary gold file, missed its siblings' bottleneck. Count-gated, correct-or-quiet."""
    global _cochange_fired
    if _cochange_fired or _GT_BASELINE:
        return ""
    try:
        db = os.environ.get("GT_GRAPH_DB", "/tmp/graph.db")
        if not os.path.isfile(db):
            return ""
        import sqlite3
        con = sqlite3.connect(db)
        base = os.path.basename(rel)
        like = "%" + base
        rows: list[tuple[str, int]] = []
        try:
            q = (
                "SELECT file_a, file_b, count FROM cochanges "
                "WHERE (file_a = ? OR file_a LIKE ? OR file_b = ? OR file_b LIKE ?) "
                "AND count >= 2 ORDER BY count DESC LIMIT 8"
            )
            for fa, fb, cnt in con.execute(q, (rel, like, rel, like)):
                other = fb if (fa == rel or (fa or "").endswith(base)) else fa
                if other and os.path.basename(other) != base and other not in [r[0] for r in rows]:
                    rows.append((other, cnt))
        except Exception:  # noqa: BLE001 -- cochanges table may be absent on old graphs
            return ""
        finally:
            con.close()
        if not rows:
            return ""

        def _short(p: str) -> str:
            r = (p or "").replace("\\", "/")
            return "/".join(r.split("/")[-2:]) if "/" in r else r

        _cochange_fired = True  # consume the one-shot only on a REAL emit (not an empty new-file)
        lines = [f"- {_short(o)} (co-changed {c}x)" for o, c in rows[:4]]
        return (
            "\n<gt-cochange>\nFiles that historically change WITH "
            f"{_short(rel)} — check whether THIS edit also needs them (completeness):\n"
            + "\n".join(lines)
            + "\n</gt-cochange>"
        )
    except Exception:  # noqa: BLE001
        return ""


def _invalidate_on_edit(rel: str, root: str) -> None:
    """L6 (minimal incremental-freshness port): after a source edit, drop the stale
    gt_hook AST cache and best-effort single-file reindex graph.db, so the next
    understand / consensus / verify reads the agent's NEW code rather than base-commit.
    On OH the wrapper reindexes after every edit; DeepSWE had nothing, leaving the
    cross-file intelligence frozen for the whole trajectory."""
    try:
        if os.path.isfile(_GT_INDEX_CACHE):
            os.remove(_GT_INDEX_CACHE)
    except Exception:  # noqa: BLE001
        pass
    try:
        gt_index = os.environ.get("GT_INDEX_BIN", "/tmp/gt-index")
        db = os.environ.get("GT_GRAPH_DB", "/tmp/graph.db")
        if os.path.isfile(gt_index) and os.path.isfile(db):
            subprocess.run(
                [gt_index, f"-root={root}", f"-file={rel}", f"-output={db}"],
                capture_output=True, timeout=_HOOK_TIMEOUT,
            )
    except Exception:  # noqa: BLE001 -- best-effort, never break the loop
        pass


def _l5_nudge(cmd: str) -> str:
    """L5 (minimal trajectory-governor port): fire AT MOST ONCE on the two highest-value
    stuck patterns the OH governor catches. The full L5Governor cannot run here (execute()
    has no max_iter / per-turn callback), but these two prevent the unguarded burn:
      (a) scaffold trap  -- many actions, zero source edits (the dominant failure mode);
      (b) repeated-command loop -- the same command 4+ times (the maxiter-burn pattern)."""
    global _l5_fired
    if _l5_fired or _GT_BASELINE:
        return ""
    norm = (cmd or "").strip()
    if norm:
        _cmd_history.append(norm)
        if len(_cmd_history) > 12:
            del _cmd_history[0]
        if _cmd_history.count(norm) >= 4:
            _l5_fired = True
            return ('\n<gt-nudge reason="loop">\nGT: you have repeated the same command 4+ '
                    "times with no progress. Stop, re-read the last error, and change approach "
                    "(open a different file or test a new hypothesis).\n</gt-nudge>")
    if _action_count >= 25 and _source_edit_count == 0:
        _l5_fired = True
        return ('\n<gt-nudge reason="scaffold_trap">\nGT: 25+ actions and no source-file edit '
                "yet — you are likely stuck exploring/scaffolding. Use the brief's gt-scope to "
                "localize and make a concrete edit to a SOURCE file now.\n</gt-nudge>")
    return ""


_FAIL_RE = re.compile(r"(FAILED|AssertionError|Traceback|[0-9]+ failed|FAIL:|Error:)", re.I)


def _l5_failure_nudge(out_text: str) -> str:
    """L5 hypothesis-falsified (OH hook_same_failure_persisted): the SAME test failure
    recurs across the agent's edit(s) -> the current hypothesis is likely wrong. Fires
    once, only after a source edit has happened (so it means 'your fix didn't take')."""
    global _l5_failure_fired
    if _l5_failure_fired or _GT_BASELINE or not out_text:
        return ""
    if not _FAIL_RE.search(out_text):
        return ""
    fails = [ln.strip() for ln in out_text.splitlines() if _FAIL_RE.search(ln)]
    sig = "|".join(sorted(set(fails))[:3])[:200]
    if not sig:
        return ""
    _test_fail_history.append(sig)
    if _test_fail_history.count(sig) >= 2 and _source_edit_count >= 1:
        _l5_failure_fired = True
        return ('\n<gt-nudge reason="failure_persisted">\nGT: the same test failure has '
                "persisted across your edit(s) — your current hypothesis is likely wrong. "
                "Re-read the failing assertion and reconsider the root cause / target file.\n</gt-nudge>")
    return ""


def _augment_output(action, out) -> None:
    """Append GT evidence to a command's output dict."""
    global _marker_sent, _action_count, _source_edit_count
    if not isinstance(out, dict):
        return
    try:
        if not _marker_sent:
            out["output"] = (out.get("output") or "") + "\n[gt-patch:loaded]"
            _marker_sent = True
        cmd = action.get("command", "") if isinstance(action, dict) else str(action)
        _orig_out = out.get("output") or ""  # the command's own output (for failure detect)
        # L5/L6 bookkeeping: count actions, track source edits, refresh on edit.
        if not _GT_BASELINE:
            _action_count += 1
            _kkind, _kf = _classify(cmd)
            if _kkind == "post_edit" and _kf:
                _source_edit_count += 1
                _kroot = _root()
                _krel = os.path.relpath(_kf, _kroot) if os.path.isabs(_kf) else _kf
                _invalidate_on_edit(_krel, _kroot)  # L6
                _gc = _graph_contract_block(_krel)  # cross-language [SIGNATURE]/[CALLERS]
                if _gc:
                    out["output"] = (out.get("output") or "") + _gc
                _cc = _cochange_block(_krel)  # COMPLETENESS / co-change
                if _cc:
                    out["output"] = (out.get("output") or "") + _cc
        # CONSENSUS (Layer-A first-view + Layer-B progressive/override): same role as
        # the OH wrapper's <gt-scope> — first view builds scope; later views reinforce
        # in-scope or re-anchor on divergence.
        if not _GT_BASELINE:
            _ckind, _cf = _classify(cmd)
            if _ckind == "post_view" and _cf:
                _croot = _root()
                _crel = os.path.relpath(_cf, _croot) if os.path.isabs(_cf) else _cf
                _cons = _consensus_block(_crel, _croot) if not _consensus_fired \
                    else _consensus_progressive(_crel)
                if _cons:
                    out["output"] = (out.get("output") or "") + _cons
        # L5 stuck-detection: scaffold/loop (once) + hypothesis-falsified (once).
        if not _GT_BASELINE:
            _nudge = _l5_nudge(cmd)
            if _nudge:
                out["output"] = (out.get("output") or "") + _nudge
            _fn = _l5_failure_nudge(_orig_out)
            if _fn:
                out["output"] = (out.get("output") or "") + _fn
        ev = _evidence(cmd)
        if ev:
            out["output"] = (out.get("output") or "") + ev
    except Exception:  # noqa: BLE001 -- never break the agent loop
        pass


def _wrap_execute(orig):
    def execute(self, action, *args, **kwargs):
        out = orig(self, action, *args, **kwargs)
        _augment_output(action, out)
        return out

    return execute


# Patch the ENVIRONMENT classes, not agent classes.  Every agent type
# (DefaultAgent, InteractiveAgent, ProgressTrackingAgent) calls
# self.env.execute(action), so wrapping env.execute is agent-class-agnostic.
_ENV_CLASSES = [
    ("minisweagent.environments.local", "LocalEnvironment"),
    ("minisweagent.environments.docker", "DockerEnvironment"),
    ("minisweagent.environments.singularity", "SingularityEnvironment"),
]


def _install() -> None:
    if _GT_BASELINE:
        return
    import importlib

    for modname, clsname in _ENV_CLASSES:
        try:
            cls = getattr(importlib.import_module(modname), clsname)
        except Exception:  # noqa: BLE001 -- env class not in this install
            continue
        if getattr(cls, "_gt_patched", False):
            continue
        try:
            cls.execute = _wrap_execute(cls.execute)
            cls._gt_patched = True
        except Exception:  # noqa: BLE001
            pass


_install()
