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

Evidence comes from gt_hook.py (the stdlib-only single-file evidence engine):
  gt_hook.py understand <file> --root=... --quiet --max-lines=10
  gt_hook.py verify --root=... --quiet --max-items=3
The container must have gt_hook.py at /opt/gt/gt_hook.py (injected by
GTMiniSweAgent.install_spec). If a graph.db is present at /tmp/graph.db,
gt_hook.py uses it for richer evidence; otherwise it self-indexes via AST.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

_GT_BASELINE = bool(os.environ.get("GT_BASELINE"))
_ROOT_FILE = os.environ.get("GT_ROOT_FILE", "/opt/gt/gt_root.txt")
_HOOK_TIMEOUT = int(os.environ.get("GT_HOOK_TIMEOUT", "30"))

# per-file-once dedup, keyed (kind, relpath)
_seen: set[tuple[str, str]] = set()
# Layer-A consensus fires once per run (first source-view), like the OH wrapper.
_consensus_fired = False
# L5 (trajectory governor, minimal port): track actions/edits/loops so a stuck
# trajectory gets ONE nudge instead of burning to maxiter unguarded (the OH governor's
# core job; the full L5Governor cannot run here — execute() has no max_iter — so we
# port the two highest-value heuristics: scaffold-trap + repeated-command loop).
_action_count = 0
_source_edit_count = 0
_cmd_history: list[str] = []
_l5_fired = False
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


def _first_src_file(cmd: str) -> str | None:
    """Pick the most plausible source-file token from a shell command."""
    best: str | None = None
    for tok in re.split(r"\s+", cmd):
        t = tok.strip("\"'`()<>;|&")
        if t.endswith(_SRC_EXT) and "*" not in t and "$" not in t:
            best = t
    return best


def _classify(cmd: str) -> tuple[str | None, str | None]:
    """Map a bash command to (kind, file): post_edit | post_view | (None, None)."""
    if not cmd:
        return None, None
    f = _first_src_file(cmd)
    if not f:
        return None, None
    if _EDIT_RE.search(cmd):
        return "post_edit", f
    if _VIEW_RE.search(cmd):
        return "post_view", f
    return None, None


_GT_HOOK = os.environ.get("GT_HOOK_PATH", "/opt/gt/gt_hook.py")


def _run_hook(kind: str, rel: str) -> str:
    """Run gt_hook.py for post-edit or post-view evidence.

    Uses -S to skip site processing so our own .pth does not re-import
    minisweagent (which prints a banner to stdout).  gt_hook.py is
    stdlib-only so -S is safe.
    """
    root = _root()
    db_path = os.environ.get("GT_GRAPH_DB", "/tmp/graph.db")
    db_flag = f"--db={db_path}" if os.path.isfile(db_path) else ""

    if kind == "post_edit":
        args = [sys.executable, "-S", _GT_HOOK, "verify",
                f"--root={root}", "--quiet", "--max-items=3"]
        if db_flag:
            args.append(db_flag)
    else:
        args = [sys.executable, "-S", _GT_HOOK, "understand", rel,
                f"--root={root}", "--quiet", "--max-lines=10"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=_HOOK_TIMEOUT)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001 -- correct-or-quiet
        return ""


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
    ev = _run_hook(kind, rel)
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
                "SELECT n.name, n.signature, "
                " (SELECT COUNT(DISTINCT e.source_id) FROM edges e "
                "    WHERE e.target_id = n.id AND e.type='CALLS') AS ncallers, "
                " (SELECT COUNT(DISTINCT n2.file_path) FROM edges e JOIN nodes n2 ON n2.id = e.source_id "
                "    WHERE e.target_id = n.id AND e.type='CALLS') AS nfiles "
                "FROM nodes n WHERE (n.file_path = ? OR n.file_path LIKE ?) "
                "AND n.label IN ('Function','Method') AND COALESCE(n.is_test,0)=0 "
                "ORDER BY ncallers DESC LIMIT 3"
            )
            rows = con.execute(q, (rel, "%" + base)).fetchall()
        finally:
            con.close()
        rows = [r for r in rows if (r[1] or "").strip()]
        if not rows:
            return ""
        out = [f'<gt-contract file="{os.path.basename(rel)}">']
        for name, sig, ncallers, nfiles in rows:
            sig = (sig or "").strip()
            out.append(f"[SIGNATURE] {sig}")
            if ncallers and int(ncallers) > 0:
                out.append(f"[CALLERS] {name}: {int(ncallers)} caller(s) in {int(nfiles)} "
                           "file(s) — preserve this interface")
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
    _cochange_fired = True
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
        # CONSENSUS (Layer-A parity): on the FIRST source-view, prepend the
        # graph-connected scope (same role as the OH wrapper's <gt-scope>).
        if not _GT_BASELINE and not _consensus_fired:
            _ckind, _cf = _classify(cmd)
            if _ckind == "post_view" and _cf:
                _croot = _root()
                _crel = os.path.relpath(_cf, _croot) if os.path.isabs(_cf) else _cf
                _cons = _consensus_block(_crel, _croot)
                if _cons:
                    out["output"] = (out.get("output") or "") + _cons
        # L5 stuck-detection nudge (at most once).
        if not _GT_BASELINE:
            _nudge = _l5_nudge(cmd)
            if _nudge:
                out["output"] = (out.get("output") or "") + _nudge
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
