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
    # 1. redirect whose TARGET is a source file — but NOT a /tmp scratch driver the agent
    #    writes then executes (`cat > /tmp/edit_x.py << PYEOF ... open('real.go','w') ...`).
    #    A redirect to /tmp/* is a scratch script; the REAL edited file is the open()/write
    #    target inside the body (step 3). Skipping scratch redirects stops contract/reindex
    #    from mis-targeting /tmp (the per-edit evidence was landing on the wrong file).
    for mm in re.finditer(r">>?\s*([^\s'\"<>|&;]+)", nohd):
        t = mm.group(1).strip("\"'`()")
        if (t.endswith(_SRC_EXT) and "*" not in t and "$" not in t
                and not t.startswith(("/tmp/", "/var/tmp/", "/dev/shm/"))):
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


_GT_HOOK = os.environ.get("GT_HOOK_PATH", "/opt/gt/gt_hook.py")


# Where gt_agent extracts the per-action import-closure (post_edit/post_view/curation_map/
# contract_delta + deps). Used as PYTHONPATH so `python3 -m groundtruth.hooks.*` resolves.
_GT_CLOSURE = os.environ.get("GT_DRIFT_CLOSURE", "/opt/gt/_drift")


def _run_graph_engine(kind: str, rel: str, root: str, db_path: str) -> str:
    """Per-action evidence from the GRAPH engines — PARITY with the OH push path (gt_gt §6):
      post_edit (L3): [SIGNATURE]/[CALLERS]/[TWIN]/[COMPLETENESS]/PRESERVE + [CONTRACT-DELTA],
                      facts-only (curation_map DETERMINISTIC gate), contract-DELTA self-derived
                      from git HEAD vs current.
      post_view (L3b): [CONTRACT]/[RAISES]/Called by/Calls into, graph-based, facts-only.
    These are the SAME modules OH runs (not the AST gt_hook.py), shipped in the closure. -S
    skips the minisweagent .pth banner; the closure is stdlib-only so -S is safe."""
    env = dict(os.environ)
    env["PYTHONPATH"] = _GT_CLOSURE + os.pathsep + env.get("PYTHONPATH", "")
    # post_edit's contract-DELTA sub-indexes via gt-index (compute_delta -> _index_one). Pin
    # the LOCAL binary so it never tries to DOWNLOAD gt-index at runtime (egress-blocked in
    # the task container — same failure the `gt drift` shim hit). Explicit env wins if set.
    env.setdefault("GT_INDEX_BINARY", "/tmp/gt-index")
    if kind == "post_edit":
        args = [sys.executable, "-S", "-m", "groundtruth.hooks.post_edit",
                f"--root={root}", f"--db={db_path}", f"--file={rel}",
                "--quiet", "--max-items=3"]
    else:
        args = [sys.executable, "-S", "-m", "groundtruth.hooks.post_view",
                f"--root={root}", f"--db={db_path}", f"--file={rel}"]
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=_HOOK_TIMEOUT, env=env)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001 -- correct-or-quiet
        return ""


def _run_gt_hook_ast(kind: str, rel: str, root: str, db_path: str) -> str:
    """Legacy AST single-file engine (gt_hook.py). Fallback ONLY when the graph engines
    yield nothing (e.g. graph.db absent or the file is not in the graph) so we never
    regress to zero per-action evidence. -S avoids the .pth banner."""
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


def _run_hook(kind: str, rel: str) -> str:
    """Per-action evidence: GRAPH engines first (OH parity), AST gt_hook only as a no-graph
    fallback so we never deliver less than before. Correct-or-quiet."""
    root = _root()
    db_path = os.environ.get("GT_GRAPH_DB", "/tmp/graph.db")
    out = ""
    if os.path.isfile(db_path):
        out = _run_graph_engine(kind, rel, root, db_path)
    if not out:
        out = _run_gt_hook_ast(kind, rel, root, db_path)
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


def _consensus_block(rel: str) -> str:
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


# NOTE: the former `_graph_contract_block` (a Python-AST-era stopgap that emitted
# [SIGNATURE]/[CALLERS] straight from graph.db) was REMOVED — it is fully superseded by the
# graph-based, cross-language post_edit engine now wired in `_run_hook` (Gap #1), which adds
# [TWIN]/PRESERVE/[CONTRACT-DELTA] AND respects the G7 isolation + facts-only gates. Re-using
# it as a fallback would re-surface exactly the isolated-function noise post_edit suppresses,
# so post_edit (gated) is the single authoritative per-edit engine = OH parity.


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


# =====================================================================================
# Gap #2 — grep-intercept (OH parity): on `grep <symbol>`, inject cross-file VERIFIED
# callers of that symbol from the graph, scoped to the grepped file. Turns the agent's own
# search into a graph-augmented one. Facts-only (name_match excluded, conf>=0.6); silent if
# the symbol isn't defined in the grepped scope (a homonym from elsewhere is worse than none).
# =====================================================================================
_GREP_SYM_RE = re.compile(
    r"\b(?:grep|rg|egrep|ggrep|ripgrep)\b[^|;&]*?(?:-{1,2}\w[\w-]*\s+)*['\"]?([A-Za-z_][A-Za-z0-9_]{1,})['\"]?")
_GREP_PATH_RE = re.compile(r"[A-Za-z0-9_./\-]+")
_GREP_STOP = {"def", "class", "import", "from", "return", "if", "else", "for", "while",
              "try", "except", "with", "async", "await", "function", "const", "let", "var"}
_grep_seen: set[str] = set()


def _grep_symbol(cmd: str) -> str | None:
    if not re.search(r"\b(?:grep|rg|egrep|ggrep|ripgrep)\b", cmd):
        return None
    m = _GREP_SYM_RE.search(cmd)
    if not m:
        return None
    s = m.group(1)
    return None if (len(s) < 2 or s in _GREP_STOP) else s


def _grep_file_scope(cmd: str, sym: str | None) -> str | None:
    head = re.split(r"[|;&]|\d?>>?|2>", cmd, maxsplit=1)[0]
    cands: list[str] = []
    for tok in head.split():
        t = tok.strip("'\"")
        if not t or t.startswith("-") or t in ("grep", "rg", "egrep", "fgrep", "ripgrep", "ggrep"):
            continue
        if sym and t == sym:
            continue
        if not _GREP_PATH_RE.fullmatch(t):
            continue
        if "/" in t or t.endswith(_SRC_EXT):
            cands.append(t)
    return cands[-1] if cands else None  # grep convention: paths after the pattern


def _grep_intercept(cmd: str) -> str:
    if _GT_BASELINE:
        return ""
    sym = _grep_symbol(cmd)
    if not sym or sym in _grep_seen:
        return ""
    db = os.environ.get("GT_GRAPH_DB", "/tmp/graph.db")
    if not os.path.isfile(db):
        return ""
    scope = _grep_file_scope(cmd, sym)
    import sqlite3
    rows: list = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            con.execute("PRAGMA busy_timeout=3000")
            base_q = (
                "SELECT DISTINCT nsrc.file_path, e.source_line FROM edges e "
                "JOIN nodes nt ON e.target_id=nt.id JOIN nodes nsrc ON e.source_id=nsrc.id "
                "WHERE nt.name=? AND e.type='CALLS' "
                "AND LOWER(COALESCE(e.resolution_method,''))!='name_match' "
                "AND COALESCE(e.confidence,0.5)>=0.6 AND nsrc.file_path!=nt.file_path "
                "AND COALESCE(nsrc.is_test,0)=0 ")
            if scope:
                suffix = "%" + scope.replace("\\", "/").lstrip("/")
                if not con.execute(
                    "SELECT 1 FROM nodes WHERE name=? AND file_path LIKE ? AND COALESCE(is_test,0)=0 LIMIT 1",
                    (sym, suffix),
                ).fetchone():
                    return ""  # not defined in the grepped scope → silent (no homonym)
                rows = con.execute(base_q + "AND nt.file_path LIKE ? LIMIT 5", (sym, suffix)).fetchall()
            else:
                rows = con.execute(base_q + "LIMIT 5", (sym,)).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 -- correct-or-quiet
        return ""
    if not rows:
        return ""
    _grep_seen.add(sym)
    body = "\n".join(f"  {fp}:{ln or '?'}" for fp, ln in rows)
    return (f"\n<gt-callers symbol=\"{sym}\">\n"
            f"{sym} is called (verified) from {len(rows)} cross-file site(s) — do not break them:\n"
            f"{body}\n</gt-callers>")


# =====================================================================================
# Gap #3 — presubmit-verify (OH L6 parity): when the agent is about to submit, remind it to
# run the project's tests for the edited modules + confirm the behavioral CONTRACT. SANITIZED
# (per 0e70222d): sourced from the `properties` table (is_test=0), NEVER assertions/tests —
# no grader test-name leak. Fires once.
# =====================================================================================
_presubmit_fired = False
_edited_for_verify: set[str] = set()


def _presubmit_verify(cmd: str) -> str:
    global _presubmit_fired
    if _GT_BASELINE or _presubmit_fired:
        return ""
    if "COMPLETE_TASK_AND_SUBMIT" not in cmd and "/tmp/patch.txt" not in cmd:
        return ""
    if not _edited_for_verify:
        return ""
    db = os.environ.get("GT_GRAPH_DB", "/tmp/graph.db")
    contracts: list[str] = []
    if os.path.isfile(db):
        import sqlite3
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                for ef in list(_edited_for_verify)[:10]:
                    norm = ef.replace("\\", "/").lstrip("/")
                    for k, v in con.execute(
                        "SELECT DISTINCT p.kind,p.value FROM properties p "
                        "JOIN nodes n ON p.node_id=n.id WHERE n.file_path LIKE ? "
                        "AND COALESCE(n.is_test,0)=0 "
                        "AND p.kind IN ('return_shape','exception_type','guard_clause') LIMIT 4",
                        ("%" + norm,),
                    ).fetchall():
                        line = f"  {os.path.basename(norm)}: {k} = {str(v)[:80]}"
                        if line not in contracts:
                            contracts.append(line)
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            pass
    _presubmit_fired = True
    tail = (":\n" + "\n".join(contracts[:8])) if contracts else " (return shape, error handling)."
    return (f"\n<gt-verify>\n[GT_VERIFY] You edited {len(_edited_for_verify)} file(s). Before "
            f"finishing, run the project's own tests for the affected modules and confirm your "
            f"change preserves the behavioral contract{tail}\n</gt-verify>")


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
                _edited_for_verify.add(_krel)  # Gap #3: presubmit-verify tracks edited files
                _invalidate_on_edit(_krel, _kroot)  # L6
                # OH PARITY: the per-edit contract ([SIGNATURE]/[CALLERS]/[TWIN]/PRESERVE +
                # [CONTRACT-DELTA], facts-only, cross-language) is delivered by post_edit via
                # _evidence(cmd) below — the SAME graph engine OH runs. The old local
                # _graph_contract_block was a Python-AST-era stopgap and is now superseded
                # (post_edit is graph-based + cross-lang), so it is no longer fired (no dup).
                _cc = _cochange_block(_krel)  # co-change (git history — distinct from post_edit)
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
                _cons = _consensus_block(_crel) if not _consensus_fired \
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
        # Gap #2: grep-intercept — verified cross-file callers of a grepped symbol (OH parity).
        if not _GT_BASELINE:
            _gi = _grep_intercept(cmd)
            if _gi:
                out["output"] = (out.get("output") or "") + _gi
        # Gap #3: presubmit-verify — contract-sourced verify reminder at submit, no test leak.
        if not _GT_BASELINE:
            _pv = _presubmit_verify(cmd)
            if _pv:
                out["output"] = (out.get("output") or "") + _pv
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
