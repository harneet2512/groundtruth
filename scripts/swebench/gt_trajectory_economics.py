#!/usr/bin/env python3
"""GT Trajectory Economics — an ADDITIVE, offline-only per-task metric section.

Emitted as the top-level ``trajectory_economics`` key (schema
``gt.trajectory_economics.v1``) inside ``gt_deep_metrics_<task>.json``. This module
is a PURE ADDITION: it never touches the 58 mandatory PERF metrics, the 128-feature
inventory, ``validate_task_performance_record``, or the performance/behavioral deep
parity comparisons. It is computed entirely from artifacts already saved per task:

  * mini-swe-agent.trajectory.json  (the agent's own trajectory — TRUTH for steps)
  * gt_runtime_ledger_<task>.jsonl  (sealed GT deliveries — dose-response join)
  * graph.db                        (CALLS/IMPORTS clusters, symbols, test linkage)
  * brief_result.json               (the GT-ranked candidate files — misdirection)
  * report.json / agent_patch.diff  (the agent's final patch)
  * dataset gold (swebench_live_lite.jsonl, keyed by instance_id) / task_truth.json

DISCIPLINE (mirrors the surrounding suite):
  * every emitted number is 8-dp via ``d8`` (None — NEVER 0.0 — when unmeasurable);
  * every metric carries a ``{applicable, predicate, reason}`` record (the D5 pattern);
  * every emitted list is timeline-ordered or sorted (no set->list, no float from an
    unordered reduction — the D1/D2/D6 determinism lesson class);
  * generalized: no task ids / repo names in logic; graph.db use is language-agnostic
    (nodes/edges/assertions only).

--------------------------------------------------------------------------------
CLASSIFIER / MATCHER RULES (deterministic, no ML) — the load-bearing definitions.
--------------------------------------------------------------------------------
A *turn* is one assistant message plus the tool observation it produced. Turns are
1-indexed in trajectory order. Each turn is assigned exactly ONE primitive action
type from its shell command(s), first match wins in this precedence:

  edit    — mutates a repo source file: an editor verb (`sed -i`, `tee`, `cat >`,
            `apply_patch`, `git apply`, `patch <`), a `>`/`>>` redirect to a repo
            file (NOT /tmp, /dev, or a *.txt scratch sink), or a `python -c`/heredoc
            that opens a repo file for write (`open(<repo file>, 'w'|'a'|...)`,
            `.write_text(`, `.write(`). This is the ONLY reliable edit signal on the
            bash-only mini scaffold — the OH editor-tool detector catches none of
            these (the real babel edit is a `python -c` open-write).
  test    — a recognized test runner (pytest / unittest / tox / nox / py.test /
            `make test` / `go test` / `cargo test` / jest / mocha / npm test).
  execute — runs code to observe behavior but is not a test runner: `python -c`,
            `python <file>`, `node <file>`, `./<script>` (an execution probe).
  search  — repo-wide localization: `grep -r`/`-R`, `rg`, `find`, `git grep`, `ls`,
            `locate`, `glob`. A `grep` naming a single concrete file is a READ.
  read    — inspects a specific file region: `cat`, `sed -n`, `head`, `tail`, `nl`,
            `less`, `awk` over a file, `grep <pat> <one file>`, `python -c` that only
            opens-and-prints a repo file.
  other   — everything else (`cd`, `export`, `pip`, `git status/log/diff`, `echo`).

verify phase == {test, execute}; hunt == pre-edit {search, read, other} not adjacent
to an edit; fix == an edit turn or a non-edit turn immediately adjacent to one.

STUCK: turn t (t>=2) is stuck iff it is not an edit AND (its command tuple equals the
previous turn's, i.e. a verbatim repeat, OR its observation text is non-empty and
byte-identical to the previous turn's observation — spinning with no new state).

PHASE (each turn exactly one, first match): stuck -> edit=fix -> test/execute=verify ->
adjacent-to-edit=fix -> else hunt.

RED (failure signature in an observation): the observation contains one of
`Traceback (most recent call last)`, `AssertionError`, a pytest/py summary
`N failed`/`N error(s)`, `=== FAILURES ===`, `=== ERRORS ===`, or `FAILED`; OR the
turn is a test/execute turn with a non-zero return code. (A non-zero rc on a
search/read turn — e.g. grep-no-match rc=1 — is NOT a RED: conservative.)

GREEN test: a test turn that is not RED and whose return code is 0/None.

DISTINCTIVE-TOKEN reference matcher (observation_yield): a token
`[A-Za-z_][A-Za-z0-9_./-]{7,}` (len>=8) or a repo file path that FIRST appears in
observation O and is NOT present in any earlier assistant message; O is "referenced"
if any LATER assistant message contains that token. Conservative — a token the agent
merely echoed from the brief cannot count (it must be newly introduced by O).

DELIVERED-FACT join (dose-response / full_file_cat): a runtime-ledger row with
outcome=="delivered" whose block_lineage[].candidate_id / file_path names a repo file
F; the fact's content token is F. Re-acquisition = a later SEARCH turn mentioning F;
consumption = a later READ/EDIT turn mentioning F.

EXPLICITLY DEFERRED (this module emits NOTHING for these; they need inputs a single
offline arm cannot supply): ``divergence_step`` (needs a paired arm),
``resolve_stability`` / ``trajectory_determinism`` (need replicate runs), and
compaction-reorientation (the mini scaffold performs no context compaction).
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import sqlite3
from typing import Any


SCHEMA = "gt.trajectory_economics.v1"

_DEFERRED = (
    "divergence_step",
    "resolve_stability",
    "trajectory_determinism",
    "compaction_reorientation",
)


# ---------------------------------------------------------------------------
# Precision + applicability helpers (same discipline as gt_performance_metrics)
# ---------------------------------------------------------------------------
def d8(x):
    """Round to 8 dp. Missing/NaN/inf -> None (JSON null), NEVER 0.0 (G14)."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 8)


def _applicability(applicable: bool, predicate: str, reason: str) -> dict[str, object]:
    """The explicit {applicable, predicate, reason} contract (mirrors D5)."""
    return {"applicable": bool(applicable), "predicate": predicate, "reason": reason}


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _ols_slope(points: list[tuple[float, float]]) -> float | None:
    """Closed-form OLS slope over (x, y) in the GIVEN order. >=2 distinct x needed.

    Deterministic: sums are taken in list order, so the reduction is stable
    (D2 lesson — never a float from an unordered reduction).
    """
    n = len(points)
    if n < 2:
        return None
    sx = sy = sxx = sxy = 0.0
    for x, y in points:
        sx += x
        sy += y
        sxx += x * x
        sxy += x * y
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    return (n * sxy - sx * sy) / denom


# ---------------------------------------------------------------------------
# Trajectory parsing (chat shape — the live mini-swe-agent / pier format)
# ---------------------------------------------------------------------------
_TEST_RUNNER_RE = re.compile(
    r"(?:^|[\s;&|])(?:python[0-9.]*\s+-m\s+)?"
    r"(pytest|py\.test|unittest|tox|nox|jest|mocha)\b"
    r"|(?:^|[\s;&|])(?:make\s+test|npm\s+(?:run\s+)?test|go\s+test|cargo\s+test)\b",
    re.I,
)
# grep recursion is a flag CLUSTER (`-rn`, `-rni`, ...); the [rR] must NOT be
# anchored with a trailing \b (that rejected `grep -rn` — r/n are both word chars).
_SEARCH_RE = re.compile(
    r"(?:^|[\s;&|])grep\s+-[a-zA-Z]*[rR]"
    r"|(?:^|[\s;&|])(?:rg|find|git\s+grep|locate|fdfind|fd|ls)\b",
    re.I,
)
_READ_HEAD_RE = re.compile(
    r"(?:^|[\s;&|])(cat|sed\s+-n|head|tail|nl|less|more|awk)\b", re.I
)
_EXECUTE_RE = re.compile(
    r"(?:^|[\s;&|])(?:python[0-9.]*\s+-c\b|python[0-9.]*\s+\S+\.py\b|node\s+\S+"
    r"|\./\S+)",
    re.I,
)
# whole-file `cat` (no range flags) targeting a file
_CAT_WHOLE_RE = re.compile(r"(?:^|[\s;&|])cat\s+(?!-)\S", re.I)

_RED_TEXT_RE = re.compile(
    r"Traceback \(most recent call last\)"
    r"|\bAssertionError\b"
    r"|=+\s*FAILURES?\s*=+"
    r"|=+\s*ERRORS?\s*=+"
    r"|\b\d+\s+failed\b"
    r"|\b\d+\s+errors?\b"
    r"|\bFAILED\b",
    re.I,
)


def _split_top_level(cmd: str) -> list[str]:
    """Split a bash command into top-level segments on unquoted ``;`` / ``&&`` /
    ``||`` / newline. Pipes are NOT split (a pipeline is ONE command). Quotes and
    ``<<`` heredoc bodies are treated as opaque. Conservative — used only by
    ``actions_per_turn``; documented limitation: it does not model process
    substitution and treats a heredoc body as part of its opener segment.
    """
    segments: list[str] = []
    buf: list[str] = []
    i, n = 0, len(cmd)
    quote = ""  # active quote char, or ""
    heredoc: str | None = None  # active heredoc terminator, or None
    while i < n:
        c = cmd[i]
        if heredoc is not None:
            line_end = cmd.find("\n", i)
            if line_end == -1:
                buf.append(cmd[i:])
                i = n
                break
            line = cmd[i:line_end]
            buf.append(cmd[i:line_end + 1])
            if line.strip() == heredoc:
                heredoc = None
            i = line_end + 1
            continue
        if quote:
            buf.append(c)
            if c == "\\" and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(c)
            buf.append(cmd[i + 1])
            i += 2
            continue
        if c == "<" and cmd[i:i + 2] == "<<":
            m = re.match(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", cmd[i:])
            if m:
                heredoc = m.group(2)
                buf.append(m.group(0))
                i += len(m.group(0))
                continue
        if c == "\n" or c == ";":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        if cmd[i:i + 2] in ("&&", "||"):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        buf.append(c)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _classify_command(cmd: str, known_paths: set[str]) -> str:
    """Return the primitive action type of ONE command string (see module doc)."""
    if _is_edit_command(cmd, known_paths):
        return "edit"
    if _TEST_RUNNER_RE.search(cmd):
        return "test"
    if _EXECUTE_RE.search(cmd):
        # a `python -c` whose ONLY file op is a read/print is a READ, not execute.
        if re.search(r"python[0-9.]*\s+-c", cmd, re.I) and not re.search(
            r"open\s*\([^)]*,\s*['\"][wax]", cmd
        ):
            if re.search(r"open\s*\([^)]*,\s*['\"]r", cmd) and ".write" not in cmd:
                return "read"
        return "execute"
    # grep on a single concrete file is a read; grep -r / rg / find is a search
    if re.search(r"(?:^|[\s;&|])grep\b", cmd, re.I) and not re.search(
        r"grep\s+-[a-z]*[rR]", cmd, re.I
    ):
        if _cmd_files(cmd, known_paths):
            return "read"
        return "search"
    if _SEARCH_RE.search(cmd):
        return "search"
    if _READ_HEAD_RE.search(cmd):
        return "read"
    return "other"


_EDIT_REDIRECT_RE = re.compile(r"(?<![0-9<>])>>?\s*(\S+)")
_SCRATCH_PREFIXES = ("/tmp/", "/dev/", "/var/tmp/")


def _is_edit_command(cmd: str, known_paths: set[str]) -> bool:
    """True iff the command mutates a repo source file (see module doc)."""
    low = cmd
    if re.search(r"(?:^|[\s;&|])sed\s+-i", low):
        return True
    if re.search(r"(?:^|[\s;&|])(?:apply_patch|patch)\b", low) and "<" in low:
        return True
    if re.search(r"(?:^|[\s;&|])git\s+apply\b", low):
        return True
    if re.search(r"(?:^|[\s;&|])tee\b", low):
        for m in re.findall(r"tee\s+(?:-a\s+)?(\S+)", low):
            if _looks_like_repo_write_target(m, known_paths):
                return True
    # a python/pathlib write to a repo file
    if (re.search(r"open\s*\([^)]*,\s*['\"][wax]", low) or ".write_text(" in low
            or re.search(r"\.write\s*\(", low)):
        for _p in _cmd_files(low, known_paths):
            return True
        for m in re.findall(r"open\s*\(\s*['\"]([^'\"]+)['\"]", low):
            if _looks_like_repo_write_target(m, known_paths):
                return True
    # a `>`/`>>` redirect to a repo file (not a scratch sink)
    for m in _EDIT_REDIRECT_RE.findall(low):
        if _looks_like_repo_write_target(m, known_paths):
            return True
    return False


def _looks_like_repo_write_target(tok: str, known_paths: set[str]) -> bool:
    tok = tok.strip().strip("'\"")
    if not tok or tok.startswith(_SCRATCH_PREFIXES) or tok == "/dev/null":
        return False
    if tok in known_paths:
        return True
    if tok.startswith(("/", "~")):
        return False
    if "/" in tok and re.search(r"\.[A-Za-z0-9]{1,5}$", tok):
        if re.search(r"\.(txt|log|json|out|tmp|diff|patch)$", tok, re.I):
            return False
        return True
    return False


def _cmd_files(cmd: str, known_paths: set[str]) -> list[str]:
    """Ordered list of KNOWN repo files named in the command (boundary-matched,
    longest-first so a nested path wins over a shorter suffix)."""
    found: list[str] = []
    for p in sorted(known_paths, key=len, reverse=True):
        if not p:
            continue
        if re.search(r"(?<![\w./-])" + re.escape(p) + r"(?![\w/-])", cmd):
            if p not in found and not any(p in f for f in found):
                found.append(p)
    return found


def parse_trajectory(traj: dict, known_paths: set[str]) -> dict:
    """Parse the chat-shape trajectory into an ordered turn list + brief text.

    Returns {"turns": [...], "brief_text": str, "submission": str, "n_messages": int,
    "assistant_turns": int}. Turns are in strict trajectory order.
    """
    messages = traj.get("messages") or []
    info = traj.get("info") or {}
    submission = str(info.get("submission") or "")
    brief_parts: list[str] = []
    turns: list[dict] = []
    ai = 0
    idx = 0
    n = len(messages)
    while idx < n:
        m = messages[idx]
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):
            content = json.dumps(content)
        content = content or ""
        if role in ("system", "user"):
            brief_parts.append(content)
            idx += 1
            continue
        if role == "assistant":
            ai += 1
            extra = m.get("extra") if isinstance(m.get("extra"), dict) else {}
            cmds: list[str] = []
            for a in extra.get("actions") or []:
                if isinstance(a, dict) and a.get("command") is not None:
                    cmds.append(str(a.get("command")))
            if not cmds:
                for tc in m.get("tool_calls") or []:
                    fn = (tc or {}).get("function") or {}
                    arg = fn.get("arguments")
                    if isinstance(arg, str):
                        try:
                            arg = json.loads(arg)
                        except (ValueError, TypeError):
                            arg = {}
                    if isinstance(arg, dict) and arg.get("command") is not None:
                        cmds.append(str(arg.get("command")))
            usage = (extra.get("response") or {}).get("usage") or m.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
            pt = usage.get("prompt_tokens")
            ct = usage.get("completion_tokens")
            tt = usage.get("total_tokens")
            if tt is None and isinstance(pt, (int, float)) and isinstance(ct, (int, float)):
                tt = pt + ct
            cost = extra.get("cost")
            obs_text = ""
            rc: int | None = None
            if idx + 1 < n and messages[idx + 1].get("role") in ("tool", "exit"):
                om = messages[idx + 1]
                oc = om.get("content")
                if isinstance(oc, list):
                    oc = json.dumps(oc)
                obs_text = oc or ""
                oex = om.get("extra") if isinstance(om.get("extra"), dict) else {}
                if isinstance(oex.get("returncode"), int):
                    rc = oex.get("returncode")
                else:
                    mrc = re.search(r"<returncode>(-?\d+)</returncode>", obs_text)
                    if mrc:
                        rc = int(mrc.group(1))
                idx += 1  # consume observation
            atypes = [_classify_command(c, known_paths) for c in cmds]
            atype = _dominant_atype(atypes)
            files: list[str] = []
            for c in cmds:
                for f in _cmd_files(c, known_paths):
                    if f not in files:
                        files.append(f)
            turns.append({
                "i": ai,
                "commands": cmds,
                "cmd_types": atypes,
                "atype": atype,
                "files": files,
                "thought": content,
                "prompt_tokens": pt if isinstance(pt, (int, float)) else None,
                "completion_tokens": ct if isinstance(ct, (int, float)) else None,
                "total_tokens": tt if isinstance(tt, (int, float)) else None,
                "cost": float(cost) if isinstance(cost, (int, float)) else None,
                "obs": obs_text,
                "returncode": rc,
            })
        idx += 1
    for t in turns:
        t["is_red"] = _is_red(t)
        t["is_green_test"] = (
            t["atype"] == "test" and not t["is_red"] and t["returncode"] in (0, None)
        )
    for k, t in enumerate(turns):
        if k == 0:
            t["is_stuck"] = False
            continue
        prev = turns[k - 1]
        repeat_cmd = t["commands"] == prev["commands"] and bool(t["commands"])
        repeat_obs = bool(t["obs"]) and t["obs"] == prev["obs"]
        t["is_stuck"] = (t["atype"] != "edit") and (repeat_cmd or repeat_obs)
    _assign_phases(turns)
    return {
        "turns": turns,
        "brief_text": "\n".join(brief_parts),
        "submission": submission,
        "n_messages": n,
        "assistant_turns": len(turns),
    }


def _dominant_atype(atypes: list[str]) -> str:
    """The turn's action type = the highest-precedence type among its commands."""
    order = ("edit", "test", "execute", "search", "read", "other")
    present = set(atypes)
    for a in order:
        if a in present:
            return a
    return "other"


def _is_red(turn: dict) -> bool:
    if _RED_TEXT_RE.search(turn["obs"] or ""):
        return True
    if turn["atype"] in ("test", "execute") and isinstance(turn["returncode"], int):
        if turn["returncode"] != 0:
            return True
    return False


def _assign_phases(turns: list[dict]) -> None:
    edit_idx = [k for k, t in enumerate(turns) if t["atype"] == "edit"]
    for k, t in enumerate(turns):
        if t["is_stuck"]:
            t["phase"] = "stuck"
        elif t["atype"] == "edit":
            t["phase"] = "fix"
        elif t["atype"] in ("test", "execute"):
            t["phase"] = "verify"
        elif (k > 0 and turns[k - 1]["atype"] == "edit") or (
            k + 1 < len(turns) and turns[k + 1]["atype"] == "edit"
        ):
            t["phase"] = "fix"
        else:
            t["phase"] = "hunt"


# ---------------------------------------------------------------------------
# graph.db: file clusters (CALLS/IMPORTS), symbols, test linkage
# ---------------------------------------------------------------------------
def _connect_ro(db_path: str) -> "sqlite3.Connection | None":
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            return sqlite3.connect(db_path)
        except sqlite3.Error:
            return None


def _norm_path(p: str) -> str:
    return (p or "").strip().lstrip("./").replace("\\", "/")


def load_graph(db_path: str) -> dict:
    """Return graph facts: files, clusters (file->cluster id), node rows, callers_of,
    test_covered_files. Empty structure on any miss.

    Clusters = connected components of the file graph where files A,B are joined iff
    a CALLS or IMPORTS edge connects a node in A to a node in B (language-agnostic).
    """
    out = {
        "present": False,
        "files": set(),
        "cluster_of": {},
        "nodes": [],
        "node_file": {},
        "callers_of": {},
        "test_covered_files": set(),
    }
    con = _connect_ro(db_path)
    if con is None:
        return out
    try:
        node_file: dict[int, str] = {}
        node_rows: list[tuple] = []
        try:
            for nid, fp, s, e, lbl, ist in con.execute(
                "SELECT id, file_path, start_line, end_line, label, is_test FROM nodes"
            ):
                f = _norm_path(fp)
                if not f:
                    continue
                node_file[int(nid)] = f
                node_rows.append((int(nid), f, s, e, bool(ist)))
                out["files"].add(f)
        except sqlite3.Error:
            return out
        parent: dict[str, str] = {f: f for f in out["files"]}

        def find(x: str) -> str:
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                if ra <= rb:
                    parent[rb] = ra
                else:
                    parent[ra] = rb

        callers: dict[str, set] = {}
        try:
            for src, dst, etype in con.execute(
                "SELECT source_id, target_id, type FROM edges "
                "WHERE type IN ('CALLS','IMPORTS')"
            ):
                fs = node_file.get(int(src)) if src is not None else None
                fd = node_file.get(int(dst)) if dst is not None else None
                if not fs or not fd:
                    continue
                if fs != fd:
                    union(fs, fd)
                if etype == "CALLS" and fs != fd:
                    callers.setdefault(fd, set()).add(fs)
        except sqlite3.Error:
            pass
        roots = sorted({find(f) for f in out["files"]})
        root_id = {r: i for i, r in enumerate(roots)}
        cluster_of = {f: root_id[find(f)] for f in out["files"]}
        test_covered: set = set()
        test_node_ids = {nid for nid, f, s, e, ist in node_rows if ist}
        try:
            for src, dst in con.execute(
                "SELECT source_id, target_id FROM edges WHERE type='CALLS'"
            ):
                if src in test_node_ids:
                    fd = node_file.get(int(dst)) if dst is not None else None
                    if fd:
                        test_covered.add(fd)
        except sqlite3.Error:
            pass
        try:
            for tnid, tgt in con.execute(
                "SELECT test_node_id, target_node_id FROM assertions "
                "WHERE target_node_id IS NOT NULL AND target_node_id != 0"
            ):
                fd = node_file.get(int(tgt)) if tgt is not None else None
                if fd:
                    test_covered.add(fd)
        except sqlite3.Error:
            pass
        out.update({
            "present": True,
            "cluster_of": cluster_of,
            "nodes": node_rows,
            "node_file": node_file,
            "callers_of": dict(callers),
            "test_covered_files": test_covered,
        })
        return out
    finally:
        con.close()


# ---------------------------------------------------------------------------
# unified-diff parsing (agent + gold patch)
# ---------------------------------------------------------------------------
def parse_unified_diff(text: str) -> dict:
    """Parse a unified diff. Return {"files": {file: [(old_start,old_len,new_start,
    new_len), ...]}, "changed_lines": int, "hunks": [(file, old_start, old_len)],
    "file_order": [files first-seen]}.

    Line ranges are OLD-file coordinates (agent and gold share the base commit, so
    they are directly comparable). ``changed_lines`` counts +/- body lines.
    """
    files: dict[str, list] = {}
    hunks: list[tuple] = []
    file_order: list[str] = []
    changed = 0
    cur: str | None = None
    for line in (text or "").splitlines():
        mg = re.match(r"diff --git a/(\S+) b/(\S+)", line)
        if mg:
            cur = _norm_path(mg.group(2))
            if cur not in files:
                files[cur] = []
                file_order.append(cur)
            continue
        mpp = re.match(r"\+\+\+ b/(\S+)", line)
        if mpp:
            cur = _norm_path(mpp.group(1))
            if cur not in files:
                files[cur] = []
                file_order.append(cur)
            continue
        mh = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if mh and cur is not None:
            os_, ol = int(mh.group(1)), int(mh.group(2) or 1)
            ns_, nl = int(mh.group(3)), int(mh.group(4) or 1)
            files[cur].append((os_, ol, ns_, nl))
            hunks.append((cur, os_, ol))
            continue
        if cur is not None and line[:1] in ("+", "-") and not line.startswith(
            ("+++", "---")
        ):
            changed += 1
    return {"files": files, "changed_lines": changed, "hunks": hunks,
            "file_order": file_order}


def _old_line_set(hunks_for_file: list[tuple]) -> set:
    """Old-file line numbers touched by a file's hunks (a 0-length hunk anchors 1)."""
    s: set = set()
    for os_, ol, _ns, _nl in hunks_for_file:
        span = ol if ol > 0 else 1
        for ln in range(os_, os_ + span):
            s.add(ln)
    return s


def _gold_from_dataset(instance_id: str, jsonl_path: str) -> tuple[list[str], str]:
    """(gold_source_files, raw_gold_patch) from the SWE-bench dataset. ([],'') on miss.
    Test files are dropped from the source-file list (they are not localization gold).
    """
    if not instance_id or not jsonl_path or not os.path.exists(jsonl_path):
        return [], ""
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if instance_id not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get("instance_id") != instance_id:
                    continue
                patch = rec.get("patch") or ""
                files = []
                for _a, b in re.findall(r"^diff --git a/(\S+) b/(\S+)", patch, re.M):
                    fp = _norm_path(b)
                    if fp and not _is_test_path(fp) and fp not in files:
                        files.append(fp)
                return files, patch
    except OSError:
        return [], ""
    return [], ""


def _is_test_path(p: str) -> bool:
    base = os.path.basename(p or "")
    return (
        "test" in base.lower()
        or "/tests/" in ("/" + p + "/")
        or p.startswith("tests/")
        or "/test/" in ("/" + p + "/")
    )


# ---------------------------------------------------------------------------
# runtime ledger: delivered GT facts (file tokens)
# ---------------------------------------------------------------------------
def load_delivered_facts(ledger_path: str) -> list[dict]:
    """Delivered GT facts [{"file", "iteration", "fact_class"}], timeline-ordered.
    Sourced from ledger rows with outcome=='delivered' via block_lineage[].candidate_id
    (``<class>:<file>``) and top-level file_path."""
    facts: list[dict] = []
    if not ledger_path or not os.path.exists(ledger_path):
        return facts
    try:
        with open(ledger_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("outcome") != "delivered":
                    continue
                it = row.get("iteration")
                it = int(it) if isinstance(it, int) else 0
                seen_files: list[str] = []
                for blk in row.get("block_lineage") or []:
                    cid = str(blk.get("candidate_id") or "")
                    fclass = str(blk.get("declared_fact_class") or "")
                    fp = ""
                    if ":" in cid:
                        fp = _norm_path(cid.split(":", 1)[1])
                    if not fp:
                        fp = _norm_path(str(blk.get("file_path") or ""))
                    if fp and re.search(r"\.[A-Za-z0-9]{1,5}$", fp) and fp not in seen_files:
                        seen_files.append(fp)
                        facts.append({"file": fp, "iteration": it, "fact_class": fclass})
                topfp = _norm_path(str(row.get("file_path") or ""))
                if (topfp and re.search(r"\.[A-Za-z0-9]{1,5}$", topfp)
                        and topfp not in seen_files):
                    facts.append({"file": topfp, "iteration": it,
                                  "fact_class": str(row.get("layer") or "")})
    except OSError:
        return facts
    return facts


def _brief_ranked_files(brief_result: dict | None) -> list[str]:
    """Repo files GT ranked in the delivered brief (brief_result.json block_receipts).
    Ordered, de-duplicated."""
    files: list[str] = []
    if not isinstance(brief_result, dict):
        return files
    recs = ((brief_result.get("metrics") or {}).get("block_receipts")) or []
    for r in recs:
        cid = str((r or {}).get("candidate_id") or "")
        if ":" in cid:
            fp = _norm_path(cid.split(":", 1)[1])
            if fp and re.search(r"\.[A-Za-z0-9]{1,5}$", fp) and fp not in files:
                files.append(fp)
    return files


# ---------------------------------------------------------------------------
# The metric computation
# ---------------------------------------------------------------------------
def compute_metrics(
    parsed: dict,
    graph: dict,
    delivered_facts: list[dict],
    brief_files: list[str],
    agent_patch: str,
    gold_files: list[str],
    gold_patch: str,
) -> tuple[dict, dict]:
    """Return (metrics, applicability). Every value is 8-dp or None; every metric has
    a {applicable, predicate, reason} record. See the module docstring for rules."""
    turns: list[dict] = parsed["turns"]
    metrics: dict[str, Any] = {}
    appl: dict[str, dict] = {}

    def emit(name: str, value, applicable: bool, predicate: str, reason: str):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            metrics[name] = value
        else:
            metrics[name] = d8(value)
        appl[name] = _applicability(applicable, predicate, reason)

    n_turns = len(turns)
    edit_turns = [t for t in turns if t["atype"] == "edit"]
    first_edit_i = edit_turns[0]["i"] if edit_turns else None
    total_edits = len(edit_turns)
    red_turns = [t for t in turns if t["is_red"]]
    first_red_i = red_turns[0]["i"] if red_turns else None

    agent_diff = parse_unified_diff(agent_patch)
    patch_files = list(agent_diff["file_order"])
    gold_set = set(gold_files or [])
    has_gold = bool(gold_files) and bool(gold_patch)

    # ---- A. Repro discipline -------------------------------------------------
    if total_edits > 0:
        repro = any(t["is_red"] and t["i"] < first_edit_i for t in turns)
        emit("repro_before_edit", repro, True, "edits > 0",
             "a failure signature before the first edit is well-defined")
    else:
        emit("repro_before_edit", None, False, "edits > 0",
             "no edit exists to anchor repro-before-edit")

    if first_red_i is not None:
        emit("steps_to_first_red", first_red_i, True, "a RED observation exists",
             "first RED observation turn index")
    else:
        emit("steps_to_first_red", None, True, "a RED observation exists",
             "no RED observation was seen; right-censored at the terminal horizon")
        appl["steps_to_first_red"]["observation"] = {
            "state": "RIGHT_CENSORED", "event": "first_red",
            "clock": "assistant_turns", "lower_bound": n_turns,
            "terminal_horizon": n_turns,
        }

    emit("red_green_cycles", _red_green_cycles(turns), True, "trajectory has turns",
         "count of RED-verify -> edit -> verify loops")

    if total_edits > 0:
        edits_before_red = sum(
            1 for t in edit_turns if first_red_i is None or t["i"] < first_red_i
        )
        emit("fix_without_repro_rate", edits_before_red / total_edits, True,
             "total_edits > 0", "fraction of edits made before any RED observation")
    else:
        emit("fix_without_repro_rate", None, False, "total_edits > 0", "no edits exist")

    # ---- B. Phase economics --------------------------------------------------
    phase_counts = {"hunt": 0, "fix": 0, "verify": 0, "stuck": 0}
    for t in turns:
        phase_counts[t["phase"]] += 1
    metrics["phase_turns"] = dict(phase_counts)
    appl["phase_turns"] = _applicability(
        n_turns > 0, "assistant_turns > 0",
        "each turn deterministically classified into exactly one phase"
        if n_turns > 0 else "no turns")

    turns_with_tokens = [t for t in turns if t["total_tokens"] is not None]
    total_tok = sum(t["total_tokens"] for t in turns_with_tokens)
    verify_tok = sum(t["total_tokens"] for t in turns_with_tokens if t["phase"] == "verify")
    if turns_with_tokens and total_tok > 0:
        emit("verify_cost_share", verify_tok / total_tok, True,
             "per-turn token usage present and total > 0",
             "share of total tokens spent on verify-phase turns")
    else:
        emit("verify_cost_share", None, False,
             "per-turn token usage present and total > 0",
             "per-turn token usage is absent")

    slope_points = [(float(t["i"]), float(t["total_tokens"])) for t in turns_with_tokens]
    slope = _ols_slope(slope_points)
    emit("tokens_per_round_slope", slope, len(slope_points) >= 2,
         "at least two turns carry token usage",
         "OLS slope of per-round total tokens on turn index"
         if len(slope_points) >= 2 else "fewer than two turns carry token usage")

    seg_counts = [len(_top_level_segments(t)) for t in turns if t["commands"]]
    if seg_counts:
        emit("actions_per_turn", sum(seg_counts) / len(seg_counts), True,
             "at least one turn issues a command",
             "mean top-level shell segments per commanding assistant turn")
    else:
        emit("actions_per_turn", None, False, "at least one turn issues a command",
             "no turn issued a shell command")

    # ---- C. Wrong-branch economics (graph.db) --------------------------------
    have_clusters = bool(graph.get("present") and graph.get("cluster_of"))
    cluster_of = graph.get("cluster_of") or {}
    patch_clusters = {cluster_of.get(f) for f in patch_files if cluster_of.get(f) is not None}

    if have_clusters and patch_files:
        first_edit_cluster_i = None
        for t in turns:
            if any(cluster_of.get(f) in patch_clusters for f in t["files"]):
                first_edit_cluster_i = t["i"]
                break
        seen_clusters: set = set()
        distinct_before = 0
        for t in turns:
            if first_edit_cluster_i is not None and t["i"] >= first_edit_cluster_i:
                break
            for f in t["files"]:
                c = cluster_of.get(f)
                if c is not None and c not in patch_clusters and c not in seen_clusters:
                    seen_clusters.add(c)
                    distinct_before += 1
        emit("hypothesis_churn", distinct_before, True,
             "graph clusters and a final patch exist",
             "distinct file-clusters explored before first touching the edited cluster")
    else:
        emit("hypothesis_churn", None, False, "graph clusters and a final patch exist",
             "graph.db clusters or the final patch are unavailable")

    if have_clusters and patch_files:
        dead_steps = 0
        dead_tokens = 0.0
        dead_tok_seen = False
        for t in turns:
            tcl = {cluster_of.get(f) for f in t["files"] if cluster_of.get(f) is not None}
            if tcl and tcl.isdisjoint(patch_clusters):
                dead_steps += 1
                if t["total_tokens"] is not None:
                    dead_tokens += t["total_tokens"]
                    dead_tok_seen = True
        emit("dead_end_cost_steps", dead_steps, True,
             "graph clusters and a final patch exist",
             "turns spent wholly in clusters untouched by the final patch")
        emit("dead_end_cost_tokens", dead_tokens if dead_tok_seen else None,
             dead_tok_seen, "dead-end turns carry token usage",
             "tokens spent in dead-end clusters"
             if dead_tok_seen else "no dead-end turn carried token usage")
    else:
        emit("dead_end_cost_steps", None, False,
             "graph clusters and a final patch exist", "unavailable")
        emit("dead_end_cost_tokens", None, False,
             "graph clusters and a final patch exist", "unavailable")

    emit("backtrack_count", _backtrack_count(turns), n_turns > 0, "assistant_turns > 0",
         "returns to a previously-abandoned file after >=5 intervening steps")

    if have_clusters and patch_files and has_gold and brief_files:
        harmful = {f for f in brief_files
                   if f not in set(patch_files) and f not in gold_set}
        harm_steps = sum(1 for t in turns if any(f in harmful for f in t["files"]))
        emit("misdirection_harm_steps", harm_steps, True,
             "graph, brief ranking, final patch and gold all present",
             "turns spent in GT-ranked files that are in neither the patch nor gold")
    else:
        emit("misdirection_harm_steps", None, False,
             "graph, brief ranking, final patch and gold all present",
             "one of graph / brief ranking / patch / gold is absent")

    # ---- D. Read economics ---------------------------------------------------
    read_turns = [t for t in turns if t["atype"] == "read"]
    # Denominator is total FILE-read events (a turn reading two known files = two
    # reads), matching the file-granular numerator so the rate stays in [0, 1].
    total_read_events = sum(len(t["files"]) for t in read_turns)
    if total_read_events > 0:
        emit("redundant_read_rate", _redundant_reads(turns) / total_read_events, True,
             "total_file_read_events > 0",
             "re-reads of the same file with no intervening edit / total file reads")
    else:
        emit("redundant_read_rate", None, False, "total_file_read_events > 0",
             "no read turn named a known repo file")

    if patch_files:
        covered = 0
        for f in patch_files:
            read_i = _first_turn_with_file(turns, f, {"read"})
            edit_i = _first_turn_with_file(turns, f, {"edit"}) or first_edit_i
            if read_i is not None and edit_i is not None and read_i < edit_i:
                covered += 1
        emit("read_before_edit_coverage", covered / len(patch_files), True,
             "at least one file was edited",
             "edited files whose content was read before the edit / edited files")
    else:
        emit("read_before_edit_coverage", None, False, "at least one file was edited",
             "no file was edited")

    br = _blast_radius_read_rate(turns, graph, agent_diff)
    if br is None:
        emit("blast_radius_read_rate", None, False,
             "graph, patch, and edited symbols with known callers exist",
             "graph / patch / caller-bearing edited symbols unavailable")
    else:
        emit("blast_radius_read_rate", br, True,
             "graph, patch, and edited symbols with known callers exist",
             "edited symbols whose callers were read before submit / such symbols")

    total_obs = sum(1 for t in turns if t["obs"])
    if total_obs > 0:
        emit("observation_yield", _observation_yield(turns) / total_obs, True,
             "at least one observation exists",
             "observations quoted by a later assistant turn / total observations")
    else:
        emit("observation_yield", None, False, "at least one observation exists",
             "no observation was produced")

    whole_cats = [
        t for t in read_turns
        if any(_CAT_WHOLE_RE.search(c) and not re.search(r"\|\s*(head|tail|sed)", c)
               for c in t["commands"]) and t["files"]
    ]
    if whole_cats and delivered_facts:
        fact_files = {f["file"] for f in delivered_facts}
        covered_cat = sum(1 for t in whole_cats if any(f in fact_files for f in t["files"]))
        emit("full_file_cat_rate", covered_cat / len(whole_cats), True,
             "whole-file reads and delivered GT facts exist",
             "whole-file cats of GT-fact-covered files / whole-file cats")
    else:
        emit("full_file_cat_rate", None, False,
             "whole-file reads and delivered GT facts exist",
             "no whole-file cat, or no delivered GT fact")

    # ---- E. Verification economics ------------------------------------------
    test_turns = [t for t in turns if t["atype"] == "test"]
    if test_turns:
        scoped = sum(1 for t in test_turns if _test_is_scoped(t))
        emit("test_selection_precision", scoped / len(test_turns), True,
             "at least one test invocation exists",
             "file/node-scoped test invocations / total test invocations")
    else:
        emit("test_selection_precision", None, False,
             "at least one test invocation exists", "no test invocation")

    fg = _false_green_submit(turns, graph, agent_diff)
    emit("false_green_submit", fg["value"], fg["applicable"], fg["predicate"], fg["reason"])

    if total_edits > 0:
        last_edit_i = edit_turns[-1]["i"]
        submit_i = turns[-1]["i"] if turns else last_edit_i
        verified_between = any(
            t["atype"] in ("test", "execute") and last_edit_i < t["i"] <= submit_i
            for t in turns
        )
        margin = 0 if verified_between else max(0, submit_i - last_edit_i)
        emit("premature_submit_margin", margin, True, "total_edits > 0",
             "turns between last edit and submit with no verify between (0 if verified)")
    else:
        emit("premature_submit_margin", None, False, "total_edits > 0", "no edits")

    # ---- F. Patch quality ----------------------------------------------------
    if has_gold and gold_patch:
        gold_diff = parse_unified_diff(gold_patch)
        gold_changed = gold_diff["changed_lines"]
        if gold_changed > 0:
            emit("patch_minimality", agent_diff["changed_lines"] / gold_changed, True,
                 "gold patch has changed lines",
                 "agent changed-line count / gold changed-line count")
        else:
            emit("patch_minimality", None, False, "gold patch has changed lines",
                 "gold patch has zero changed lines")
        emit("gold_hunk_overlap", _gold_hunk_overlap(agent_diff, gold_diff), True,
             "gold hunks exist",
             "gold old-lines intersected by an agent edit range / gold old-lines")
        emit("spurious_hunk_rate",
             _spurious_hunk_rate(agent_diff, gold_diff, gold_set, graph),
             bool(agent_diff["hunks"]), "agent patch has hunks and gold is known",
             "hunks in neither gold ranges nor test-covered code / total hunks"
             if agent_diff["hunks"] else "agent patch has no hunks")
    else:
        for name in ("patch_minimality", "gold_hunk_overlap", "spurious_hunk_rate"):
            emit(name, None, False, "gold patch available",
                 "dataset/task gold patch is unavailable")

    # ---- G. GT dose-response -------------------------------------------------
    hl = _fact_half_life(turns, delivered_facts)
    if hl is None:
        emit("fact_half_life", None, False,
             "a delivered fact was re-acquired by a later search",
             "no delivered fact was re-acquired via search")
    else:
        emit("fact_half_life", hl, True,
             "a delivered fact was re-acquired by a later search",
             "mean turns from delivery to redundant search re-acquisition")

    ttc = _time_to_consumption(turns, delivered_facts)
    if ttc is None:
        emit("time_to_consumption", None, False,
             "a delivered fact was consumed (read/edited) later",
             "no delivered fact was consumed")
    else:
        emit("time_to_consumption", ttc, True,
             "a delivered fact was consumed (read/edited) later",
             "mean turns from delivery to first read/edit of the delivered file")

    return metrics, appl


# ---- section helpers -------------------------------------------------------
def _top_level_segments(turn: dict) -> list[str]:
    segs: list[str] = []
    for c in turn["commands"]:
        segs.extend(_split_top_level(c))
    return segs


def _red_green_cycles(turns: list[dict]) -> int:
    """Greedy non-overlapping count of RED-verify -> edit -> verify loops."""
    state = "seek_red"
    cycles = 0
    for t in turns:
        is_verify = t["atype"] in ("test", "execute")
        if state == "seek_red":
            if is_verify and t["is_red"]:
                state = "seek_edit"
        elif state == "seek_edit":
            if t["atype"] == "edit":
                state = "seek_verify"
        elif state == "seek_verify":
            if is_verify:
                cycles += 1
                state = "seek_red"
    return cycles


def _backtrack_count(turns: list[dict]) -> int:
    """Count turns touching a file last touched >=5 turns earlier, with >=1 other
    file touched in between (the file was abandoned then returned to)."""
    last_touch: dict[str, int] = {}
    touched_since: dict[str, set] = {}
    count = 0
    for t in turns:
        for f in t["files"]:
            if f in last_touch and (t["i"] - last_touch[f]) >= 5 and touched_since.get(f):
                count += 1
            last_touch[f] = t["i"]
            touched_since[f] = set()
        for f in list(touched_since.keys()):
            for g in t["files"]:
                if g != f:
                    touched_since[f].add(g)
    return count


def _redundant_reads(turns: list[dict]) -> int:
    """Reads of a file with no edit of that file since the previous read of it."""
    read_seen: dict[str, bool] = {}
    redundant = 0
    for t in turns:
        if t["atype"] == "edit":
            for f in t["files"]:
                read_seen[f] = False
            continue
        if t["atype"] == "read":
            for f in t["files"]:
                if read_seen.get(f):
                    redundant += 1
                read_seen[f] = True
    return redundant


def _first_turn_with_file(turns: list[dict], file: str, atypes: set) -> int | None:
    for t in turns:
        if t["atype"] in atypes and file in t["files"]:
            return t["i"]
        if "edit" in atypes and t["atype"] == "edit" and any(
            re.search(r"(?<![\w./-])" + re.escape(file) + r"(?![\w/-])", c)
            for c in t["commands"]
        ):
            return t["i"]
    return None


def _blast_radius_read_rate(turns, graph, agent_diff):
    if not graph.get("present") or not agent_diff["files"]:
        return None
    per_file_ranges = {f: _old_line_set(hs) for f, hs in agent_diff["files"].items()}
    edited_syms: list[tuple] = []
    for nid, f, s, e, ist in graph["nodes"]:
        if f not in per_file_ranges or ist or s is None or e is None:
            continue
        rng = per_file_ranges[f]
        if any(s <= ln <= e for ln in rng):
            edited_syms.append((nid, f))
    if not edited_syms:
        return None
    callers_of = graph.get("callers_of") or {}
    syms_with_callers = [(nid, f) for nid, f in edited_syms if callers_of.get(f)]
    if not syms_with_callers:
        return None
    read_files: set = set()
    for t in turns:
        if t["atype"] in ("read", "search"):
            read_files.update(t["files"])
    hit = sum(1 for nid, f in syms_with_callers if callers_of.get(f, set()) & read_files)
    return hit / len(syms_with_callers)


def _observation_yield(turns: list[dict]) -> int:
    """Count observations introducing a distinctive token later quoted by the agent.
    Distinctive = len>=8 identifier-ish/path token NOT in any earlier assistant message.
    """
    tok_re = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]{7,}")
    thoughts = [t["thought"] + " " + " ".join(t["commands"]) for t in turns]
    suffix = [""] * len(turns)
    running = ""
    for k in range(len(turns) - 1, -1, -1):
        suffix[k] = running
        running = thoughts[k] + " " + running
    referenced = 0
    prefix_assistant = ""
    for k, t in enumerate(turns):
        obs = t["obs"]
        if obs:
            cand = {m for m in tok_re.findall(obs)
                    if len(m) >= 8 and m not in prefix_assistant}
            later = suffix[k]
            if any(tok in later for tok in cand):
                referenced += 1
        prefix_assistant += " " + thoughts[k]
    return referenced


def _test_is_scoped(turn: dict) -> bool:
    for c in turn["commands"]:
        if "::" in c or re.search(r"\s-k\s", c):
            return True
        toks = re.findall(r"\b[\w/.-]*test[\w/.-]*\.\w+\b", c)
        if toks:
            return True
    return False


def _false_green_submit(turns, graph, agent_diff):
    edit_files = list(agent_diff["files"].keys())
    green_tests = [t for t in turns if t["is_green_test"]]
    if not edit_files or not green_tests:
        return {"value": None, "applicable": False,
                "predicate": "a passing verify precedes submit and edits are known",
                "reason": "no passing test before submit, or no edited files"}
    last_green = green_tests[-1]
    named_test_files: list[str] = []
    scoped = False
    for c in last_green["commands"]:
        if "::" in c or re.search(r"\s-k\s", c):
            scoped = True
        named_test_files.extend(re.findall(r"\b[\w/.-]*test[\w/.-]*\.\w+\b", c))
    if not named_test_files and not scoped:
        return {"value": False, "applicable": True,
                "predicate": "a passing verify precedes submit and edits are known",
                "reason": "the passing verify was a broad (unscoped) run covering the edits"}
    if not graph.get("present"):
        return {"value": None, "applicable": False,
                "predicate": "graph.db present for test-linkage",
                "reason": "graph.db unavailable to establish test coverage"}
    edit_set = {_norm_path(f) for f in edit_files}
    named_set = {_norm_path(f) for f in named_test_files}
    callers_of = graph.get("callers_of") or {}
    covered = any(callers_of.get(ef, set()) & named_set for ef in edit_set)
    if not covered:
        tcf = graph.get("test_covered_files") or set()
        if edit_set & tcf and named_set:
            covered = True
    return {"value": (not covered), "applicable": True,
            "predicate": "a passing scoped verify precedes submit and edits are known",
            "reason": "scoped passing verify's test files "
                      + ("link to the edited files (covered)" if covered
                         else "do not link to the edited files (false green)")}


def _gold_hunk_overlap(agent_diff, gold_diff):
    gold_total = 0
    gold_hit = 0
    agent_ranges = {
        f: [(os_, os_ + (ol if ol > 0 else 1) - 1) for os_, ol, _n, _nl in hs]
        for f, hs in agent_diff["files"].items()
    }
    for f, hs in gold_diff["files"].items():
        aranges = agent_ranges.get(f, [])
        for ln in sorted(_old_line_set(hs)):
            gold_total += 1
            if any(a <= ln <= b for a, b in aranges):
                gold_hit += 1
    if gold_total == 0:
        return None
    return gold_hit / gold_total


def _spurious_hunk_rate(agent_diff, gold_diff, gold_set, graph):
    hunks = agent_diff["hunks"]
    if not hunks:
        return None
    gold_ranges = {
        f: [(os_, os_ + (ol if ol > 0 else 1) - 1) for os_, ol, _n, _nl in hs]
        for f, hs in gold_diff["files"].items()
    }
    test_covered = graph.get("test_covered_files") or set()
    spurious = 0
    for f, os_, ol in hunks:
        a, b = os_, os_ + (ol if ol > 0 else 1) - 1
        in_gold_file = f in gold_set
        in_gold_range = any(not (b < ga or a > gb) for ga, gb in gold_ranges.get(f, []))
        is_test_covered = f in test_covered or _is_test_path(f)
        if not in_gold_file and not in_gold_range and not is_test_covered:
            spurious += 1
    return spurious / len(hunks)


def _fact_half_life(turns, delivered_facts):
    """Mean turns from a fact's delivery to a later SEARCH turn re-deriving it."""
    samples: list[float] = []
    for fact in delivered_facts:
        f = fact["file"]
        dturn = fact["iteration"]
        for t in turns:
            if t["i"] <= dturn:
                continue
            if t["atype"] == "search" and any(
                re.search(r"(?<![\w./-])" + re.escape(f) + r"(?![\w/-])", c)
                for c in t["commands"]
            ):
                samples.append(float(t["i"] - dturn))
                break
    if not samples:
        return None
    return sum(samples) / len(samples)


def _time_to_consumption(turns, delivered_facts):
    """Mean turns from delivery to the first later READ/EDIT of the delivered file."""
    samples: list[float] = []
    for fact in delivered_facts:
        f = fact["file"]
        dturn = fact["iteration"]
        for t in turns:
            if t["i"] <= dturn:
                continue
            if t["atype"] in ("read", "edit") and f in t["files"]:
                samples.append(float(t["i"] - dturn))
                break
    if not samples:
        return None
    return sum(samples) / len(samples)


# ---------------------------------------------------------------------------
# artifact discovery + top-level entry
# ---------------------------------------------------------------------------
def _find_one(base: str, *names: str) -> str:
    if not base:
        return ""
    for name in names:
        direct = os.path.join(base, name)
        if os.path.exists(direct):
            return direct
        hits = sorted(glob.glob(os.path.join(base, "**", name), recursive=True))
        if hits:
            return hits[0]
    return ""


def _load_text(path: str, max_bytes: int = 4_000_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except OSError:
        return ""


def compute_trajectory_economics(
    task_id: str,
    results_dir: str,
    *,
    trajectory: dict | None = None,
    trajectory_path: str = "",
    graph_db: str = "",
    ledger_path: str = "",
    brief_result_path: str = "",
    agent_patch_path: str = "",
    gold_jsonl: str = "",
    gold_files: list[str] | None = None,
    gold_patch: str = "",
) -> dict:
    """Build the additive ``trajectory_economics`` section. NEVER raises — any error
    yields a stamped, still-additive section carrying ``collection_error``."""
    section: dict[str, Any] = {
        "schema": SCHEMA,
        "precision_decimals": 8,
        "deferred": list(_DEFERRED),
    }
    try:
        if trajectory is None:
            trajectory_path = trajectory_path or _find_one(
                results_dir, "mini-swe-agent.trajectory.json", "*.trajectory.json"
            )
            trajectory = _load_json(trajectory_path) if trajectory_path else None
        if not isinstance(trajectory, dict):
            section["collection_error"] = "no_trajectory"
            section["inputs_present"] = {"trajectory": False}
            return section
        graph_db = graph_db or _find_one(results_dir, "graph.db")
        ledger_path = ledger_path or _find_one(
            results_dir, f"gt_runtime_ledger_{task_id}.jsonl", "gt_runtime_ledger*.jsonl"
        )
        brief_result_path = brief_result_path or _find_one(results_dir, "brief_result.json")
        agent_patch_path = agent_patch_path or _find_one(results_dir, "agent_patch.diff")

        graph = load_graph(graph_db)
        if gold_files is None:
            gold_jsonl = gold_jsonl or os.environ.get("GT_GOLD_JSONL", "")
            gold_files, gold_patch = _gold_from_dataset(task_id, gold_jsonl)
        brief_result = _load_json(brief_result_path) if brief_result_path else None
        brief_files = _brief_ranked_files(brief_result)
        delivered_facts = load_delivered_facts(ledger_path)

        agent_patch = str((trajectory.get("info") or {}).get("submission") or "")
        if "diff --git" not in agent_patch and agent_patch_path:
            agent_patch = _load_text(agent_patch_path)

        agent_patch_files = parse_unified_diff(agent_patch)["file_order"]
        known_paths: set = set(graph.get("files") or set())
        known_paths.update(gold_files or [])
        known_paths.update(brief_files)
        known_paths.update(agent_patch_files)
        known_paths.update(f["file"] for f in delivered_facts)
        known_paths = {_norm_path(p) for p in known_paths if p}

        parsed = parse_trajectory(trajectory, known_paths)
        metrics, appl = compute_metrics(
            parsed, graph, delivered_facts, brief_files,
            agent_patch, gold_files or [], gold_patch,
        )
        section["metrics"] = metrics
        section["metric_applicability"] = appl
        section["inputs_present"] = {
            "trajectory": True,
            "graph_db": bool(graph.get("present")),
            "runtime_ledger": bool(delivered_facts)
            or bool(ledger_path and os.path.exists(ledger_path)),
            "brief_result": bool(brief_files),
            "agent_patch": bool(agent_patch_files),
            "gold_patch": bool(gold_files) and bool(gold_patch),
        }
        section["summary"] = {
            "assistant_turns": parsed["assistant_turns"],
            "delivered_facts": len(delivered_facts),
            "brief_ranked_files": len(brief_files),
            "patch_files": len(agent_patch_files),
            "gold_files": len(gold_files or []),
        }
    except Exception as exc:  # noqa: BLE001 — additive section must never fail the run
        section["collection_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return section


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="GT trajectory-economics (additive section)")
    ap.add_argument("task_id")
    ap.add_argument("results_dir", nargs="?", default="")
    ap.add_argument("--gold-jsonl", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    rd = a.results_dir or f"/tmp/results_{a.task_id}"
    section = compute_trajectory_economics(a.task_id, rd, gold_jsonl=a.gold_jsonl)
    out = a.out or f"/tmp/gt_trajectory_economics_{a.task_id}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(section, f, indent=2, sort_keys=False)
    print(json.dumps(section, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
