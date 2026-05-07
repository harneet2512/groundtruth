"""FULL-POTENTIAL behavioral analyzer for GT-layer engagement.

Reads an existing run directory (no LLM calls) and produces a per-task,
per-layer engagement table for the six measurable layers:

  L1  pre-task brief navigation     (first-K agent file actions vs brief files)
  L2  zero-ID fallback navigation   (only relevant when brief was empty)
  L3  evidence consumption          (next-N actions reference brief tokens)
  L4  gt_query invocation           (count + invoked-then-ignored heuristic)
  L5  gate (warn_soft_escape)       (revised diff vs byte-equal bypass)
  L6  graph freshness               (post-edit evidence reflects edited symbols)

The script is repo-agnostic and language-agnostic. It uses only generic
identifier tokenization (TOKEN_RE / SPLIT_RE / STOPWORDS adapted from
``measure_l3_engagement.py``) and never special-cases Python.

Inputs
------
- ``--run-dir <PATH>``: a SWE-bench-Live / SWE-agent-style run directory whose
  immediate children are task subdirs containing ``<task_id>.traj``.
- ``--graph-dir <PATH>`` (optional): directory containing one ``<task_id>.db``
  per task (used to compute the random-walk baseline for L1). When absent
  the L1 random baseline is reported as ``None`` and L1 verdict abstains
  on missing data.
- ``--eval-json <PATH>`` (optional): SWE-bench eval JSON with ``resolved_ids``.
- ``--out-md <PATH>`` (optional): write the markdown report to disk
  (printed to stdout regardless).

Per-task / per-layer thresholds match the spec brief:

  L1 green  : >= 60% of tasks have navigation_impact (= L1_hits / first_3 /
              random_baseline) >= 2.0x random_baseline
  L3 green  : >= 60% of tasks have avg_substring_match_rate >= 0.30
  L4 green  : >= 60% of tasks have >= 1 gt_query invocation AND
              invoked_then_ignored_rate < 0.50
  L5 green  : >= 1 task triggered the gate AND submitted a revised diff
              after the warning. Abstain (``n/a``) if no triggers in run.
  L6 green  : >= 60% of tasks show post-edit evidence that mentions the
              edited symbol or a new caller.

The 60% (3/5) threshold is the smallest majority on a 5-task slice; the
spec brief listed it as ``>= 3/5``. Override per-run via env vars
``FP_L1_FRAC``, ``FP_L3_FRAC``, ``FP_L4_FRAC``, ``FP_L6_FRAC``.

Output
------
Markdown table with one row per task and one verdict row per layer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ---- generic tokenizer (verbatim from measure_l3_engagement.py) ----

TOKEN_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
SPLIT_RE = re.compile(r"[.:/\\]")

STOPWORDS = {
    "def", "class", "func", "function", "fn", "let", "var", "const", "pub", "static",
    "public", "private", "protected", "final", "abstract", "for", "while", "loop",
    "each", "map", "filter", "range", "yield", "await", "async", "try", "catch",
    "except", "finally", "throw", "throws", "raise", "return", "break", "continue",
    "pass", "else", "elif", "then", "case", "switch", "default", "new", "delete",
    "null", "none", "nil", "true", "false", "undefined", "self", "this", "that",
    "args", "kwargs", "arg", "argv", "the", "and", "not", "with", "from", "import",
    "use", "using", "require", "module", "package", "file", "line", "code", "path",
    "name", "value", "type", "size", "len", "length", "int", "str", "bool", "dict",
    "list", "set", "any", "obj", "ref", "ptr", "more", "here", "have", "note",
    "todo", "fix", "bug", "issue",
}

GT_FAMILY_TAGS = {
    "IMPORT", "CALLER", "SIBLING", "TEST", "IMPACT", "TYPE", "PRECEDENT",
    "TARGET", "CALLS", "CALLED BY", "SIMILAR",
}

EDIT_VERBS = {"create", "str_replace", "insert"}


def tokenize(text: str) -> set[str]:
    if not text:
        return set()
    expanded = SPLIT_RE.sub(" ", text)
    out: set[str] = set()
    for m in TOKEN_RE.finditer(expanded):
        tok = m.group(0).lower()
        if len(tok) < 4:
            # Spec asks for noun-tokens >= 4 chars at the L3 layer.
            continue
        if tok in STOPWORDS:
            continue
        out.add(tok)
    return out


def tokenize_ordered(text: str, *, min_len: int = 4) -> list[str]:
    if not text:
        return []
    expanded = SPLIT_RE.sub(" ", text)
    out: list[str] = []
    for m in TOKEN_RE.finditer(expanded):
        tok = m.group(0).lower()
        if len(tok) < min_len:
            continue
        if tok in STOPWORDS:
            continue
        out.append(tok)
    return out


# ---- helpers for action stream ----

# Action verbs that touch a file path. The first arg after the verb is treated
# as a path-like token (we do not parse shell quoting; we keep this generic).
FILE_TOOLS = {"str_replace_editor"}
FILE_SHELL_PREFIXES = ("cat ", "less ", "more ", "head ", "tail ", "view ")
SHELL_TOUCH_PATH_RE = re.compile(
    r"\b(?:cat|less|more|head|tail|wc|file)\s+(?P<path>[^\s|;<>&]+)"
)
EDITOR_PATH_RE = re.compile(
    r"^str_replace_editor\s+(?:create|view|str_replace|insert|undo_edit)\s+(?P<path>[^\s]+)"
)
FIND_RE = re.compile(r"\bfind\s+(?P<root>[^\s|;<>&]+).*?-name\s+(?P<name>[^\s|;<>&]+)")
GREP_RE = re.compile(
    r"\b(?:grep|rg|ripgrep)\b.*?\s(?P<path>[^\s|;<>&]+)\s*$"
)


def _action_paths(action: str) -> list[str]:
    """Extract candidate file paths from an action string. Generic best-effort."""
    if not action:
        return []
    paths: list[str] = []
    m = EDITOR_PATH_RE.match(action.strip())
    if m:
        paths.append(m.group("path"))
        return paths
    # find / grep / cat-style: scan for path-like substrings.
    for rx in (FIND_RE, SHELL_TOUCH_PATH_RE):
        for mm in rx.finditer(action):
            try:
                paths.append(mm.group("path"))
            except IndexError:
                pass
    # Anything that looks like a / or \-separated path with an extension or
    # rooted at /.
    for tok in re.split(r"[\s;|&<>]+", action):
        if not tok:
            continue
        if tok.startswith("/") or "\\" in tok:
            paths.append(tok)
        elif "/" in tok and "." in tok.split("/")[-1]:
            paths.append(tok)
    # dedup preserving order
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        p = p.strip().strip(",.;:'\"")
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _is_file_action(action: str) -> bool:
    if not action:
        return False
    return bool(_action_paths(action))


def _action_verb(action: str) -> str:
    parts = (action or "").split(None, 2)
    return parts[1] if len(parts) >= 2 else ""


def _normalize_path(p: str) -> str:
    """Normalize for substring matching: drop leading roots like /testbed/."""
    if not p:
        return ""
    # strip leading absolute prefixes commonly used in containers
    for prefix in ("/testbed/", "/repo/", "/workspace/", "/home/ubuntu/"):
        if p.startswith(prefix):
            p = p[len(prefix):]
            break
    return p.lstrip("./").rstrip("/")


# ---- evidence parsing ----

GT_EVIDENCE_FILE_RE = re.compile(r"<gt-evidence\s+file=['\"]([^'\"]+)['\"]")
GT_TASK_BRIEF_RE = re.compile(r"<gt-task-brief>(.*?)</gt-task-brief>", re.S)
TARGET_LINE_RE = re.compile(r"^\s*TARGET:\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)
CALLS_LINE_RE = re.compile(
    r"^\s*(?:CALLS|CALLED BY)\s*[→>:\-]+\s*([A-Za-z_][A-Za-z0-9_]*)", re.M
)


def extract_brief_text(history: list[dict[str, Any]]) -> str:
    """Return the contents of the first <gt-task-brief> block in the prompt."""
    for m in history[:6]:
        c = m.get("content")
        text = ""
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text = " ".join(
                (b.get("text", "") if isinstance(b, dict) else "") for b in c
            )
        if not text:
            continue
        match = GT_TASK_BRIEF_RE.search(text)
        if match:
            return match.group(1)
    return ""


def extract_brief_files(brief_text: str) -> list[str]:
    """Pull file paths out of a brief block. Looks for slash-and-dot patterns
    plus explicit ``file=`` style tags. Repo-agnostic."""
    if not brief_text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    # explicit file= tokens
    for m in re.finditer(r"file=['\"]?([^\s'\">]+)['\"]?", brief_text):
        p = m.group(1)
        if p not in seen:
            seen.add(p); out.append(p)
    # bare paths with extension
    for m in re.finditer(r"(?<!=)([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_+]{1,6})\b", brief_text):
        p = m.group(1)
        # skip pure version numbers like "3.10"
        if "/" not in p and "\\" not in p:
            continue
        if p not in seen:
            seen.add(p); out.append(p)
    return out


def extract_evidence_blocks(traj: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per step that carried non-empty gt_evidence."""
    out: list[dict[str, Any]] = []
    for i, step in enumerate(traj):
        state = step.get("state") or {}
        ev = state.get("gt_evidence") or "" if isinstance(state, dict) else ""
        if not ev or len(ev) <= 100:
            continue
        files = GT_EVIDENCE_FILE_RE.findall(ev)
        targets = TARGET_LINE_RE.findall(ev)
        called = CALLS_LINE_RE.findall(ev)
        out.append({
            "step": i,
            "evidence": ev,
            "files": files,
            "target_symbols": targets,
            "neighbor_symbols": called,
            "edit_count": state.get("gt_edit_count") if isinstance(state, dict) else None,
            "reindex_count": state.get("gt_reindex_count") if isinstance(state, dict) else None,
        })
    return out


# ---- per-layer measurement ----

@dataclass
class TaskMetrics:
    task_id: str
    # L1
    brief_files: list[str] = field(default_factory=list)
    first_3_actions: list[str] = field(default_factory=list)
    first_3_paths: list[str] = field(default_factory=list)
    L1_hits: int = 0
    L1_random_baseline: float | None = None
    L1_navigation_impact: float | None = None
    L1_engaged: bool | None = None
    # L2 fallback
    L2_applies: bool = False
    L2_hits: int | None = None
    L2_engaged: bool | None = None
    # L3
    L3_evidence_blocks: int = 0
    L3_token_match_rates: list[float] = field(default_factory=list)
    L3_avg_match_rate: float = 0.0
    L3_engaged: bool = False
    # L4
    L4_invocations: int = 0
    L4_post_invoke_edits: int = 0
    L4_invoked_then_ignored: int = 0
    L4_engaged: bool | None = None
    # L5
    L5_gate_triggers: int = 0
    L5_revised: int = 0
    L5_engaged: bool | None = None
    L5_pre_warn_diff_hash: str | None = None
    L5_post_warn_diff_hash: str | None = None
    # L6
    L6_reindex_count: int = 0
    L6_post_edit_evidence_reflects_edit: bool = False
    L6_engaged: bool = False
    # outcome
    resolved: bool | None = None
    submitted: bool = False
    edit_count: int = 0


def _path_hit(path_in_action: str, brief_files: Iterable[str]) -> bool:
    norm = _normalize_path(path_in_action).lower()
    if not norm:
        return False
    for bf in brief_files:
        bnorm = _normalize_path(bf).lower()
        if not bnorm:
            continue
        if bnorm in norm or norm.endswith(bnorm) or bnorm.endswith(norm):
            return True
    return False


def _first_k_file_actions(traj: list[dict[str, Any]], k: int = 3) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, s in enumerate(traj):
        a = s.get("action") or ""
        if _is_file_action(a):
            out.append({"step": i, "action": a, "paths": _action_paths(a)})
            if len(out) >= k:
                break
    return out


def _next_window_text(traj: list[dict[str, Any]], idx: int, window: int) -> str:
    parts: list[str] = []
    for j in range(idx + 1, min(idx + 1 + window, len(traj))):
        s = traj[j]
        for k in ("thought", "action", "observation", "response"):
            v = s.get(k)
            if isinstance(v, str) and v:
                parts.append(v)
    return "\n".join(parts)


def _diff_from_history(history: list[dict[str, Any]], up_to_step: int) -> str:
    """We do not have per-step diffs; approximate with the cumulative
    str_replace_editor edit signature (concatenation of action + new_str)
    truncated to the requested step. This is sufficient for byte-equal
    detection between successive submissions."""
    parts: list[str] = []
    seen = 0
    for s in history:
        if s.get("message_type") != "action":
            continue
        if seen > up_to_step:
            break
        a = (s.get("action") or "")
        if a.startswith("str_replace_editor"):
            parts.append(a)
        seen += 1
    return "\n".join(parts)


def measure_task(
    task_id: str,
    traj_path: Path,
    *,
    graph_db: Path | None = None,
    resolved: bool | None = None,
    submitted: bool | None = None,
    window: int = 3,
    task_dir: Path | None = None,
) -> TaskMetrics:
    data = json.loads(Path(traj_path).read_text(encoding="utf-8", errors="replace"))
    traj: list[dict[str, Any]] = data.get("trajectory") or []
    history: list[dict[str, Any]] = data.get("history") or []
    info: dict[str, Any] = data.get("info") or {}
    # RC-10 (D-006 / E-fix): default task_dir to the trajectory's parent.
    # The 4 separate L4 readers must agree on counts; this analyzer is one
    # of them. The disagreement check below FAILs LOUD when the on-disk
    # JSONL count diverges from the trajectory tool-call count.
    if task_dir is None:
        task_dir = Path(traj_path).parent

    m = TaskMetrics(task_id=task_id)
    m.submitted = bool(submitted) if submitted is not None else bool(info.get("submission"))
    m.resolved = resolved

    # ---- L1 brief navigation ----
    brief_text = extract_brief_text(history)
    m.brief_files = extract_brief_files(brief_text)

    first3 = _first_k_file_actions(traj, k=3)
    m.first_3_actions = [x["action"][:80] for x in first3]
    m.first_3_paths = [p for x in first3 for p in x["paths"]][:6]
    if m.brief_files:
        m.L1_hits = sum(
            1
            for x in first3
            if any(_path_hit(p, m.brief_files) for p in x["paths"])
        )
    else:
        m.L1_hits = 0

    if graph_db and graph_db.exists() and m.brief_files:
        try:
            con = sqlite3.connect(str(graph_db))
            cur = con.cursor()
            cur.execute(
                "SELECT COUNT(DISTINCT file_path) FROM nodes "
                "WHERE label IN ('Function','Method','Class')"
            )
            (total_files,) = cur.fetchone()
            con.close()
            if total_files and total_files > 0:
                m.L1_random_baseline = round(len(m.brief_files) / total_files, 6)
        except sqlite3.Error:
            m.L1_random_baseline = None

    if m.brief_files:
        observed_rate = m.L1_hits / max(1, len(first3))
        if m.L1_random_baseline:
            m.L1_navigation_impact = round(observed_rate / m.L1_random_baseline, 3)
            m.L1_engaged = m.L1_navigation_impact >= 2.0
        else:
            m.L1_navigation_impact = None
            m.L1_engaged = None
    else:
        m.L1_engaged = None  # cannot measure

    # ---- L2 fallback (zero-ID tasks) ----
    # In Phase 4/6 the brief is generated regardless of ID density. We treat
    # L2_applies = True only when brief_files is empty AND a fallback signal
    # was emitted (string "fallback" inside the brief block). Otherwise we
    # leave it as None / "n/a".
    if not m.brief_files and "fallback" in brief_text.lower():
        m.L2_applies = True
        # No flagged fallback files extracted reliably; report 0/abstain.
        m.L2_hits = 0
        m.L2_engaged = None

    # ---- L3 evidence consumption ----
    blocks = extract_evidence_blocks(traj)
    m.L3_evidence_blocks = len(blocks)
    for b in blocks:
        ev = b["evidence"]
        ev_tokens = tokenize(ev)
        win_text = _next_window_text(traj, b["step"], window)
        win_tokens = tokenize(win_text)
        if ev_tokens:
            overlap = len(ev_tokens & win_tokens) / len(ev_tokens)
            m.L3_token_match_rates.append(overlap)
    if m.L3_token_match_rates:
        m.L3_avg_match_rate = round(
            sum(m.L3_token_match_rates) / len(m.L3_token_match_rates), 4
        )
    m.L3_engaged = m.L3_avg_match_rate >= 0.30

    # ---- L4 gt_query invocation ----
    # Real invocation: action that starts with `gt_query` or registered tool
    # name `gt_query` in tool_calls.
    gt_query_steps: list[int] = []
    for i, s in enumerate(traj):
        a = (s.get("action") or "").strip()
        if a.startswith("gt_query") or "gt_query(" in a:
            gt_query_steps.append(i)
            continue
    # also check history tool_calls
    for hi, hs in enumerate(history):
        tcs = hs.get("tool_calls") or []
        for tc in tcs:
            if isinstance(tc, dict):
                fn = ((tc.get("function") or {}).get("name")) or tc.get("name") or ""
                if fn == "gt_query":
                    # try to map to traj index by counting prior actions
                    gt_query_steps.append(-1)
                    break
    m.L4_invocations = len(gt_query_steps)
    # RC-10 (D-006 / E-fix): cross-validate the trajectory-derived L4
    # count against the on-disk JSONL line count. Pre-fix the four L4
    # readers (smoke runner, Track 4 close-wrap, deep_util_gate, this
    # analyzer) used independent substrates and could each report
    # different numbers without anyone noticing. Now we FAIL LOUD via
    # m.L4_disagreement_reason when they diverge.
    try:
        from gt_layer_counts import (
            count_layer_calls,
            disagreement_check,
        )

        counts = count_layer_calls(task_dir)
        # The trajectory scan above counted gt_query specifically; the
        # canonical helper exposes per-tool breakdown so we compare
        # apples-to-apples.
        jsonl_q = int(counts.get("gt_query", 0))
        diff = disagreement_check(jsonl_q, m.L4_invocations, tolerance=0)
        if diff:
            # Stash the reason on the metrics object — render_markdown
            # surfaces it in the per-task table so the operator sees
            # the disagreement immediately.
            try:
                setattr(m, "L4_disagreement_reason", diff)
            except Exception:  # noqa: BLE001
                pass
    except ImportError:  # pragma: no cover — fallback path
        pass
    m.L4_post_invoke_edits = 0
    m.L4_invoked_then_ignored = 0
    for i in gt_query_steps:
        if i < 0:
            continue
        # next 3 actions: did any edit reference a queried symbol?
        # Heuristic: extract tokens >=4 from the gt_query action argument.
        q_action = traj[i].get("action") or ""
        q_tokens = set(tokenize_ordered(q_action))
        edit_seen = False
        next_text = _next_window_text(traj, i, window)
        next_lower = next_text.lower()
        # find next edit step in window
        for j in range(i + 1, min(i + 1 + window, len(traj))):
            s = traj[j]
            a = s.get("action") or ""
            if a.startswith("str_replace_editor"):
                verb = _action_verb(a)
                if verb in EDIT_VERBS:
                    edit_seen = True
                    paths = _action_paths(a)
                    if any(t and t in next_lower for t in q_tokens):
                        m.L4_post_invoke_edits += 1
                        break
        if not edit_seen:
            m.L4_invoked_then_ignored += 1
    if m.L4_invocations >= 1:
        ignored_rate = m.L4_invoked_then_ignored / m.L4_invocations
        m.L4_engaged = ignored_rate < 0.50
    else:
        m.L4_engaged = None  # cannot measure

    # ---- L5 gate (warn_soft_escape) ----
    # Find any step where observation/state contains warn_soft_escape.
    warn_indices: list[int] = []
    for i, s in enumerate(traj):
        joined = " ".join(
            v for v in (
                s.get("observation"),
                s.get("response"),
                s.get("thought"),
                json.dumps(s.get("state") or {}),
            ) if isinstance(v, str)
        )
        if "warn_soft_escape" in joined:
            warn_indices.append(i)
    m.L5_gate_triggers = len(warn_indices)
    if warn_indices:
        # Compute pre-warn / post-warn cumulative edit signature hash.
        pre = _diff_from_history(history, warn_indices[0])
        post = _diff_from_history(history, len(traj))
        m.L5_pre_warn_diff_hash = hashlib.sha256(pre.encode()).hexdigest()[:12]
        m.L5_post_warn_diff_hash = hashlib.sha256(post.encode()).hexdigest()[:12]
        if m.L5_pre_warn_diff_hash != m.L5_post_warn_diff_hash:
            m.L5_revised = 1
            m.L5_engaged = True
        else:
            m.L5_revised = 0
            m.L5_engaged = False
    else:
        m.L5_engaged = None  # abstain — no gate triggers in this run

    # ---- L6 graph freshness ----
    last_evidence: dict[str, Any] | None = blocks[-1] if blocks else None
    edited_files: list[str] = []
    edited_symbols: list[str] = []
    for s in traj:
        a = s.get("action") or ""
        if not a.startswith("str_replace_editor"):
            continue
        verb = _action_verb(a)
        if verb not in EDIT_VERBS:
            continue
        for p in _action_paths(a):
            edited_files.append(_normalize_path(p).lower())
        # crude symbol guess from a `def`/`class`/`function`/`fn` keyword in the
        # action body; fully repo-agnostic.
        for m_ in re.finditer(
            r"(?:def|class|func|function|fn)\s+([A-Za-z_][A-Za-z0-9_]{2,})", a
        ):
            edited_symbols.append(m_.group(1).lower())
    m.edit_count = sum(
        1 for s in traj
        if (s.get("action") or "").startswith("str_replace_editor")
        and _action_verb(s.get("action") or "") in EDIT_VERBS
    )
    if last_evidence:
        try:
            m.L6_reindex_count = int(last_evidence.get("reindex_count") or 0)
        except (TypeError, ValueError):
            m.L6_reindex_count = 0
        ev_files = [_normalize_path(f).lower() for f in last_evidence.get("files") or []]
        ev_targets = [s.lower() for s in last_evidence.get("target_symbols") or []]
        ev_neighbors = [s.lower() for s in last_evidence.get("neighbor_symbols") or []]
        # "Reflects edit" requires a strong correspondence:
        #   (a) basename match between an edited file and an evidence file, OR
        #   (b) an edited symbol name appears in evidence target/neighbor list.
        # Substring containment alone is too lax (e.g. EqualsIsUseful.py
        # matches test_equals_is_useful.py through stem overlap).
        def _basename(p: str) -> str:
            p = p.replace("\\", "/")
            return p.rsplit("/", 1)[-1]

        ev_basenames = {_basename(f) for f in ev_files if f}
        edited_basenames = {_basename(f) for f in edited_files if f}
        file_match = bool(ev_basenames & edited_basenames)
        sym_match = any(
            sym in ev_targets or sym in ev_neighbors for sym in edited_symbols
        )
        m.L6_post_edit_evidence_reflects_edit = bool(file_match or sym_match)
        m.L6_engaged = m.L6_post_edit_evidence_reflects_edit
    return m


# ---- run-level aggregation ----

def _frac_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def aggregate_verdicts(metrics: list[TaskMetrics]) -> dict[str, str]:
    n = len(metrics)
    if n == 0:
        return {}
    L1_frac = _frac_env("FP_L1_FRAC", 0.6)
    L3_frac = _frac_env("FP_L3_FRAC", 0.6)
    L4_frac = _frac_env("FP_L4_FRAC", 0.6)
    L6_frac = _frac_env("FP_L6_FRAC", 0.6)

    def _verdict(name: str, hits: int, frac: float) -> str:
        return f"green ({hits}/{n} >= {int(frac*n+0.999)})" if hits >= int(frac * n + 0.999) else f"red ({hits}/{n})"

    L1_hits = sum(1 for m in metrics if m.L1_engaged is True)
    L1_unmeasurable = sum(1 for m in metrics if m.L1_engaged is None)
    if L1_unmeasurable == n:
        L1 = "n/a (no brief in any task)"
    else:
        L1 = _verdict("L1", L1_hits, L1_frac)

    L3_hits = sum(1 for m in metrics if m.L3_engaged)
    L3 = _verdict("L3", L3_hits, L3_frac)

    L4_hits = sum(1 for m in metrics if m.L4_engaged is True)
    L4_unmeasurable = sum(1 for m in metrics if m.L4_engaged is None)
    if L4_unmeasurable == n:
        L4 = "n/a (no gt_query invocations)"
    else:
        L4 = _verdict("L4", L4_hits, L4_frac)

    L5_triggers = sum(m.L5_gate_triggers for m in metrics)
    L5_revised = sum(m.L5_revised for m in metrics)
    if L5_triggers == 0:
        L5 = "n/a (no gate triggers in run)"
    else:
        L5 = "green" if L5_revised >= 1 else "red"

    L6_hits = sum(1 for m in metrics if m.L6_engaged)
    L6 = _verdict("L6", L6_hits, L6_frac)

    resolved = sum(1 for m in metrics if m.resolved)
    return {
        "L1": L1,
        "L3": L3,
        "L4": L4,
        "L5": L5,
        "L6": L6,
        "resolved": f"{resolved}/{n}",
    }


# ---- markdown rendering ----

def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "Y" if v else "N"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def render_markdown(run_dir: str, metrics: list[TaskMetrics], verdicts: dict[str, str]) -> str:
    lines: list[str] = []
    lines.append(f"# FULL-POTENTIAL ANALYZER -- {run_dir}\n")
    lines.append(
        "## Per-task table\n\n"
        "| task | resolved | brief_files | first3_paths | L1_hits | L1_baseline | L1_impact | L1_eng | "
        "L3_blocks | L3_match | L3_eng | L4_inv | L4_ignored | L4_eng | "
        "L5_trig | L5_rev | L5_eng | L6_reidx | L6_reflects | L6_eng |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for m in metrics:
        lines.append(
            "| " + " | ".join(_fmt(x) for x in (
                m.task_id,
                m.resolved,
                len(m.brief_files),
                len(m.first_3_paths),
                m.L1_hits,
                m.L1_random_baseline,
                m.L1_navigation_impact,
                m.L1_engaged,
                m.L3_evidence_blocks,
                m.L3_avg_match_rate,
                m.L3_engaged,
                m.L4_invocations,
                m.L4_invoked_then_ignored,
                m.L4_engaged,
                m.L5_gate_triggers,
                m.L5_revised,
                m.L5_engaged,
                m.L6_reindex_count,
                m.L6_post_edit_evidence_reflects_edit,
                m.L6_engaged,
            )) + " |"
        )
    lines.append("")
    lines.append("## Run verdict (per layer)\n")
    lines.append("| layer | verdict |")
    lines.append("|---|---|")
    for k in ("L1", "L3", "L4", "L5", "L6", "resolved"):
        lines.append(f"| {k} | {verdicts.get(k, 'n/a')} |")
    lines.append("")
    lines.append("### Notes")
    lines.append(
        "- L1 random baseline = brief_files / total_files in graph.db "
        "(`SELECT COUNT(DISTINCT file_path) FROM nodes WHERE label IN ('Function','Method','Class')`)."
    )
    lines.append(
        "- L3 token-match = |evidence_tokens & next-3-step_tokens| / |evidence_tokens|, "
        "tokens are >=4 chars, lowercased, stopwords stripped."
    )
    lines.append(
        "- L4 invoked-then-ignored = gt_query call followed by no edit in next-3 window OR an edit "
        "that doesn't share any >=4-char token with the query argument."
    )
    lines.append(
        "- L5 revised = sha256 of cumulative editor-action signature differs between pre-warn and end-of-run."
    )
    lines.append(
        "- L6 reflects edit = last gt_evidence block names an edited file, edited symbol, or symbol-text appears in evidence body."
    )
    return "\n".join(lines) + "\n"


# ---- driver ----

def discover_tasks(run_dir: Path) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for child in sorted(run_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name in {"eval_logs", "logs"}:
            continue
        traj = child / f"{child.name}.traj"
        if traj.exists():
            out.append((child.name, traj))
    return out


def load_eval(eval_path: Path | None) -> tuple[set[str], set[str]]:
    if not eval_path or not eval_path.exists():
        return set(), set()
    try:
        d = json.loads(eval_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return set(), set()
    return set(d.get("resolved_ids", [])), set(d.get("submitted_ids", []))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--graph-dir", type=Path, default=None,
                    help="optional dir containing one <task_id>.db per task "
                         "for L1 random baseline")
    ap.add_argument("--eval-json", type=Path, default=None,
                    help="optional SWE-bench eval JSON with resolved_ids")
    ap.add_argument("--out-md", type=Path, default=None)
    ap.add_argument("--window", type=int, default=3)
    ap.add_argument("--json", action="store_true",
                    help="also emit raw per-task JSON to stdout")
    args = ap.parse_args(argv)

    if not args.run_dir.exists():
        print(f"run-dir not found: {args.run_dir}", file=sys.stderr)
        return 2

    if args.eval_json is None:
        # autodiscover
        candidates = list(args.run_dir.glob("*eval*.json"))
        if candidates:
            args.eval_json = candidates[0]

    resolved_ids, submitted_ids = load_eval(args.eval_json)

    metrics: list[TaskMetrics] = []
    for tid, traj_path in discover_tasks(args.run_dir):
        graph_db = None
        if args.graph_dir:
            cand = args.graph_dir / f"{tid}.db"
            if cand.exists():
                graph_db = cand
            else:
                cand2 = args.graph_dir / tid / "graph.db"
                if cand2.exists():
                    graph_db = cand2
        m = measure_task(
            tid, traj_path,
            graph_db=graph_db,
            resolved=tid in resolved_ids if resolved_ids else None,
            submitted=tid in submitted_ids if submitted_ids else None,
            window=args.window,
            task_dir=traj_path.parent,
        )
        metrics.append(m)

    verdicts = aggregate_verdicts(metrics)
    md = render_markdown(str(args.run_dir), metrics, verdicts)
    print(md)

    if args.out_md:
        args.out_md.write_text(md, encoding="utf-8")

    if args.json:
        payload = [m.__dict__ for m in metrics]
        print("\n--- raw json ---")
        print(json.dumps(payload, indent=2, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
