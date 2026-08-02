#!/usr/bin/env python3
"""gt_navigate — consolidated graph-navigation bundle.

Discriminator-driven dispatch. Replaces 3 prior bundles:
  gt_trace, gt_impact, gt_find_relevant.

Usage:
  gt_navigate <symbol> <mode>

Where <mode> is one of:
  trace      — callers + callees of the resolved symbol
  impact     — blast radius (direct + 2nd-hop indirect) with HIGH/MODERATE/LOW
  relevant   — identifier-extraction + BFS expansion → top-5 candidate files
               (the <symbol> arg is treated as an issue description string)

Output mirrors the prior per-bundle format exactly so existing parsers
keep working unchanged. Each invocation appends ONE JSON line to
$GT_INSTANCE_LOG_DIR/gt_navigate_calls.jsonl with
{tool, mode, args, returned_lines, ts}.

Exit codes:
  0  success (incl. zero results)
  2  bad usage / missing GT_GRAPH_DB
  3  graph.db missing or unreadable
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

TOOL = "gt_navigate"
VALID_MODES = ("trace", "impact", "relevant")

# ── Shared constants ─────────────────────────────────────────────────────────
VERIFIED_RESOLUTIONS = frozenset({"same_file", "import", "name_match"})
# RC-04: legacy fallback. Runtime threshold comes from project_meta or live
# P50; see _conf_for(conn).
MIN_CONFIDENCE = 0.5
LABELS_FILTER = "label IN ('Function','Method','Class','Interface')"

# trace / impact
MAX_CALLERS = 5
MAX_CALLEES = 5
MAX_CALLERS_REPORTED = 10
MAX_INDIRECT_REPORTED = 10

# relevant
MAX_DEPTH = 3
MIN_REL_CONFIDENCE = 0.5
RELEVANCE_THRESHOLD = 0.2
MAX_CANDIDATES = 5
MAX_IDENTIFIERS = 20

# identifier extraction noise list (mirror gt_intel + gt_find_relevant).
#
# RC-01: This list is the LANGUAGE-LEVEL stoplist only — language keywords,
# Python dunders, generic English filler, true/false/None. Repo-specific
# high-frequency identifiers (the kind that used to accumulate as literal
# entries here, e.g. one repo's dominant noun) MUST NOT be added here.
# Instead, ``_high_freq_repo_identifiers`` derives them per-repo at query
# time from the graph.db node-name distribution, so the same code generalises
# to any codebase / any language without a benchmark-specific entry.
_NOISE_WORDS = frozenset({
    "true", "false", "True", "False", "None",
    "__main__", "__init__", "__name__", "__file__",
    "TODO", "FIXME", "NOTE", "XXX",
    "the", "and", "for", "with", "this", "that", "you", "have",
    "are", "not", "but", "all", "can", "any", "use", "set", "get",
})


# ── Per-repo high-frequency identifier filter (RC-01) ────────────────────────
# Cached top-1% of name-match identifiers, computed once per process per db.
# Lookup order:
#   1. graph.db ``meta`` table key ``high_freq_identifiers`` (CSV) — written
#      by the indexer when available. TODO(RC-01-coord): Go-side meta
#      population is owned by RC-17/RC-04 (gt-index/internal/store/sqlite.go
#      + main.go).
#   2. Live computation: top-1% of node names by frequency in the current
#      graph.db. Fallback path so this works even if the indexer hasn't been
#      taught to populate the meta table yet.
_HIGH_FREQ_CACHE: dict[str, frozenset[str]] = {}


def _high_freq_repo_identifiers(conn: sqlite3.Connection) -> frozenset[str]:
    """Return the per-repo high-frequency identifier set for this graph.db.

    Top-1% of node names by frequency (min 5 occurrences). Computed once per
    process per db_path. Repo-agnostic by construction: every repo has a
    natural Zipf-like name distribution and the head of that distribution is
    almost always low-information (project name, base class, generic enum).
    """
    db_path = os.environ.get("GT_GRAPH_DB", "")
    if db_path in _HIGH_FREQ_CACHE:
        return _HIGH_FREQ_CACHE[db_path]
    names: list[str] = []
    # 1. Try meta table first (written at index time when available).
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'high_freq_identifiers' LIMIT 1"
        ).fetchone()
        if row is not None and row[0]:
            names = [n.strip() for n in str(row[0]).split(",") if n.strip()]
    except sqlite3.OperationalError:
        names = []  # meta column missing — fall through to live compute
    # 2. Live fallback: top-1% of name-match counts (min 5 hits).
    if not names:
        try:
            rows = conn.execute(
                "SELECT name, COUNT(*) AS c FROM nodes "
                "WHERE name IS NOT NULL AND name != '' "
                "GROUP BY name HAVING c >= 5 ORDER BY c DESC"
            ).fetchall()
            total = len(rows)
            if total:
                cutoff = max(1, total // 100)
                names = [r["name"] for r in rows[:cutoff]]
        except sqlite3.OperationalError:
            names = []
    out = frozenset(names)
    _HIGH_FREQ_CACHE[db_path] = out
    return out


# ── DB helpers ───────────────────────────────────────────────────────────────
def _has_confidence(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT confidence FROM edges LIMIT 0")
        return True
    except sqlite3.OperationalError:
        return False


def _conf_clause(has_conf: bool, threshold: float = MIN_CONFIDENCE) -> str:
    return f" AND e.confidence >= {threshold}" if has_conf else ""


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


def _resolution_in_clause() -> tuple[str, tuple[str, ...]]:
    methods = tuple(sorted(VERIFIED_RESOLUTIONS))
    return ",".join("?" * len(methods)), methods


def _tier_for(rm: str | None) -> str:
    return "[VERIFIED]" if rm in ("same_file", "import") else "[POSSIBLE]"


def _open_graph_db() -> tuple[sqlite3.Connection | None, int]:
    db_path = os.environ.get("GT_GRAPH_DB")
    if not db_path:
        print(f"{TOOL}: GT_GRAPH_DB not set", file=sys.stderr)
        return None, 2
    if not Path(db_path).exists():
        print(f"{TOOL}: graph.db not found at {db_path}", file=sys.stderr)
        return None, 3
    try:
        # RC-04: dropped immutable=1 (writer can run concurrently). Add
        # PRAGMA integrity_check; surface db_corrupt as exit 4. Warm
        # per-repo MIN_CONFIDENCE cache.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.execute("PRAGMA busy_timeout = 5000")
        ic = conn.execute("PRAGMA integrity_check").fetchone()
        if ic is None or ic[0] != "ok":
            print(f"{TOOL}: db_corrupt: {ic[0] if ic else 'unknown'}", file=sys.stderr)
            return None, 4
    except sqlite3.Error as e:
        print(f"{TOOL}: cannot open graph.db: {e}", file=sys.stderr)
        return None, 3
    conn.row_factory = sqlite3.Row
    _conf_for(conn)
    return conn, 0


# ── Symbol resolution (shared by trace + impact) ─────────────────────────────
def _resolve_qualified(
    conn: sqlite3.Connection, symbol: str
) -> sqlite3.Row | None:
    parts = [p for p in symbol.split(".") if p]
    if len(parts) < 2:
        return None
    leaf, qualifiers = parts[-1], parts[:-1]
    cur = conn.cursor()
    candidates = cur.execute(
        f"SELECT * FROM nodes WHERE name = ? AND {LABELS_FILTER} "
        # RC-06: drop is_test ASC tie-breaker.
        "ORDER BY id ASC LIMIT 50",
        (leaf,),
    ).fetchall()
    if not candidates:
        return None
    parent_ids = {c["parent_id"] for c in candidates if c["parent_id"]}
    parents: dict[int, sqlite3.Row] = {}
    if parent_ids:
        rows = cur.execute(
            f"SELECT * FROM nodes WHERE id IN ({','.join('?' * len(parent_ids))})",
            tuple(parent_ids),
        ).fetchall()
        parents = {r["id"]: r for r in rows}
    best: tuple[int, sqlite3.Row] | None = None
    for cand in candidates:
        chain: list[str] = []
        node = cand
        seen: set[int] = set()
        while node["parent_id"] and node["parent_id"] not in seen:
            seen.add(node["parent_id"])
            parent = parents.get(node["parent_id"])
            if parent is None:
                break
            chain.append(parent["name"])
            node = parent
        matched = 0
        for q, c_name in zip(reversed(qualifiers), chain):
            if q == c_name:
                matched += 1
            else:
                break
        if matched == len(qualifiers):
            return cand
        if best is None or matched > best[0]:
            best = (matched, cand)
    if best and best[0] >= 1:
        return best[1]
    return None


def resolve_symbol(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row | None:
    cur = conn.cursor()
    row = cur.execute(
        f"SELECT * FROM nodes WHERE qualified_name = ? AND {LABELS_FILTER} LIMIT 1",
        (symbol,),
    ).fetchone()
    if row:
        return row
    if "." in symbol:
        row = _resolve_qualified(conn, symbol)
        if row:
            return row
    row = cur.execute(
        f"SELECT * FROM nodes WHERE name = ? AND {LABELS_FILTER} "
        # RC-06: drop is_test ASC tie-breaker.
        "ORDER BY id ASC LIMIT 1",
        (symbol,),
    ).fetchone()
    if row:
        return row
    leaf = symbol.split(".")[-1] if "." in symbol else symbol
    row = cur.execute(
        f"SELECT * FROM nodes WHERE LOWER(name) = LOWER(?) AND {LABELS_FILTER} "
        # RC-06: drop is_test ASC tie-breaker.
        "ORDER BY id ASC LIMIT 1",
        (leaf,),
    ).fetchone()
    return row


# ── Mode: trace ──────────────────────────────────────────────────────────────
def _get_callers_trace(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    has_conf = _has_confidence(conn)
    placeholders, methods = _resolution_in_clause()
    sql = f"""
        SELECT s.name AS caller_name, s.qualified_name AS caller_qn,
               s.file_path AS caller_file, e.source_line AS line,
               e.resolution_method AS rm
        FROM edges e
        JOIN nodes s ON e.source_id = s.id
        WHERE e.target_id = ? AND e.type = 'CALLS'
          AND e.resolution_method IN ({placeholders})
          {_conf_clause(has_conf, threshold=_conf_for(conn))}
        ORDER BY (e.resolution_method = 'same_file') DESC,
                 (e.resolution_method = 'import') DESC,
                 -- RC-06: drop s.is_test ASC tie-breaker.
                 e.source_line ASC
        LIMIT ?
    """
    return list(conn.execute(sql, (target_id, *methods, MAX_CALLERS)))


def _get_callees_trace(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    has_conf = _has_confidence(conn)
    placeholders, methods = _resolution_in_clause()
    sql = f"""
        SELECT t.name AS callee_name, t.qualified_name AS callee_qn,
               t.file_path AS callee_file, e.source_line AS line,
               e.resolution_method AS rm
        FROM edges e
        JOIN nodes t ON e.target_id = t.id
        WHERE e.source_id = ? AND e.type = 'CALLS'
          AND e.resolution_method IN ({placeholders})
          {_conf_clause(has_conf, threshold=_conf_for(conn))}
        ORDER BY (e.resolution_method = 'same_file') DESC,
                 (e.resolution_method = 'import') DESC
        LIMIT ?
    """
    return list(conn.execute(sql, (source_id, *methods, MAX_CALLEES)))


def _mode_trace(symbol: str) -> tuple[list[str], int]:
    conn, rc = _open_graph_db()
    if conn is None:
        return [], rc
    try:
        target = resolve_symbol(conn, symbol)
        if target is None:
            conn.close()
            return [
                f"# gt_navigate trace: no symbol named '{symbol}' in graph.db"
            ], 0
        qn = target["qualified_name"] or target["name"]
        loc = (
            f"{target['file_path']}:{target['start_line']}"
            if target["start_line"] else target["file_path"]
        )
        callers = _get_callers_trace(conn, target["id"])
        callees = _get_callees_trace(conn, target["id"])
    except sqlite3.OperationalError as e:
        print(f"{TOOL}: query failed: {e}", file=sys.stderr)
        conn.close()
        return [], 3
    conn.close()

    out: list[str] = [f"# gt_navigate trace: {qn} @ {loc}"]
    if not callers and not callees:
        out.append(
            "# (no verified callers or callees in graph.db; "
            "may be a leaf or unconnected symbol)"
        )
    for c in callers:
        line = f":{c['line']}" if c["line"] else ""
        out.append(
            f"CALLER  {c['caller_qn'] or c['caller_name']} at "
            f"{c['caller_file']}{line} {_tier_for(c['rm'])}"
        )
    for c in callees:
        out.append(
            f"CALLEE  {c['callee_qn'] or c['callee_name']} at "
            f"{c['callee_file']} {_tier_for(c['rm'])}"
        )
    return out, 0


# ── Mode: impact ─────────────────────────────────────────────────────────────
def _read_source_line(repo_root: str, file_path: str, line: int) -> str:
    if not repo_root or not file_path or line is None or line < 1:
        return ""
    candidates = [
        os.path.join(repo_root, file_path),
        file_path if os.path.isabs(file_path) else None,
    ]
    for cand in candidates:
        if not cand or not os.path.isfile(cand):
            continue
        try:
            with open(cand, "r", encoding="utf-8", errors="replace") as fh:
                for i, txt in enumerate(fh, start=1):
                    if i == line:
                        return txt.rstrip("\n")
        except OSError:
            continue
    return ""


def _classify_call(usage: str, sym_name: str) -> tuple[str, str]:
    if f"{sym_name}(" in usage:
        inside = usage.split(f"{sym_name}(", 1)[-1].split(")", 1)[0]
        if "=" in inside:
            return ("keyword", "MODERATE")
        return ("positional", "HIGH")
    if sym_name in usage:
        return ("reference", "LOW")
    return ("unknown", "MODERATE")


def _get_direct_callers_impact(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    has_conf = _has_confidence(conn)
    placeholders, methods = _resolution_in_clause()
    sql = f"""
        SELECT s.name AS caller_name, s.qualified_name AS caller_qn,
               s.file_path AS caller_file, e.source_line AS line,
               e.resolution_method AS rm
        FROM edges e
        JOIN nodes s ON e.source_id = s.id
        WHERE e.target_id = ? AND e.type = 'CALLS'
          AND e.resolution_method IN ({placeholders})
          {_conf_clause(has_conf, threshold=_conf_for(conn))}
        ORDER BY (e.resolution_method = 'same_file') DESC,
                 (e.resolution_method = 'import') DESC,
                 -- RC-06: drop s.is_test ASC tie-breaker.
                 e.source_line ASC
        LIMIT ?
    """
    return list(conn.execute(sql, (target_id, *methods, MAX_CALLERS_REPORTED)))


def _get_indirect_files(
    conn: sqlite3.Connection, direct_caller_files: set[str]
) -> list[str]:
    if not direct_caller_files:
        return []
    has_conf = _has_confidence(conn)
    placeholders_files = ",".join("?" * len(direct_caller_files))
    placeholders_meth, methods = _resolution_in_clause()
    sql = f"""
        SELECT DISTINCT s.file_path AS src_file
        FROM edges e
        JOIN nodes s ON e.source_id = s.id
        JOIN nodes t ON e.target_id = t.id
        WHERE t.file_path IN ({placeholders_files})
          AND e.type = 'CALLS'
          AND e.resolution_method IN ({placeholders_meth})
          {_conf_clause(has_conf, threshold=_conf_for(conn))}
        ORDER BY e.id ASC
        LIMIT 500
    """
    rows = conn.execute(sql, (*direct_caller_files, *methods)).fetchall()
    return [
        r["src_file"] for r in rows
        if r["src_file"] and r["src_file"] not in direct_caller_files
    ]


def _mode_impact(symbol: str) -> tuple[list[str], int]:
    conn, rc = _open_graph_db()
    if conn is None:
        return [], rc
    repo_root = os.environ.get("GT_REPO_ROOT") or os.getcwd()
    try:
        target = resolve_symbol(conn, symbol)
        if target is None:
            conn.close()
            return [
                f"# gt_navigate impact: no symbol named '{symbol}' in graph.db"
            ], 0
        callers = _get_direct_callers_impact(conn, target["id"])
        direct_files = {c["caller_file"] for c in callers if c["caller_file"]}
        indirect = _get_indirect_files(conn, direct_files)
    except sqlite3.OperationalError as e:
        print(f"{TOOL}: query failed: {e}", file=sys.stderr)
        conn.close()
        return [], 3
    conn.close()

    total_at_risk = len(direct_files) + len(indirect)
    if total_at_risk >= 5:
        level = "HIGH"
    elif total_at_risk >= 2:
        level = "MODERATE"
    else:
        level = "LOW"

    qn = target["qualified_name"] or target["name"]
    loc = (
        f"{target['file_path']}:{target['start_line']}"
        if target["start_line"] else target["file_path"]
    )
    out: list[str] = [
        f"# gt_navigate impact: {qn} @ {loc} — {level} "
        f"(direct={len(direct_files)}, indirect={len(indirect)})"
    ]
    if not callers:
        out.append(
            "# (no verified direct callers; "
            "symbol may be entry-point or low-traffic)"
        )
    for c in callers[:MAX_CALLERS_REPORTED]:
        usage = _read_source_line(repo_root, c["caller_file"], c["line"]).strip()
        style, risk = _classify_call(usage, target["name"])
        line = f":{c['line']}" if c["line"] else ""
        usage_part = f": {usage}" if usage else ""
        out.append(
            f"{risk:<8}  {c['caller_file']}{line}{usage_part}  [{style}]"
        )
    for f in indirect[:MAX_INDIRECT_REPORTED]:
        out.append(f"INDIRECT  {f}  (2nd-hop)")
    return out, 0


# ── Mode: relevant ───────────────────────────────────────────────────────────
def _extract_identifiers(
    text: str, repo_high_freq: frozenset[str] | None = None,
) -> list[str]:
    """Extract identifier candidates from issue text.

    ``repo_high_freq`` is the per-repo high-frequency identifier set (RC-01) —
    when provided, names appearing in that set are dropped. Pass ``None`` (the
    default) when running outside a graph.db context.
    """
    high_freq = repo_high_freq or frozenset()
    identifiers: set[str] = set()
    identifiers.update(re.findall(r"`([a-zA-Z_][\w.]*)`", text))
    identifiers.update(re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b", text))
    identifiers.update(re.findall(r"`([A-Z][a-z]{3,})`", text))
    identifiers.update(re.findall(
        r"(?:class|import|isinstance|issubclass|type)\s*[\s(]+([A-Z][a-z]{3,})",
        text, re.I,
    ))
    identifiers.update(re.findall(
        r"[\w/]+\.(?:py|go|js|ts|rs|java|rb|php|c|cpp|h|hpp|cs|kt|scala|swift|ex|exs|lua|ml|elm|jsx|tsx|mjs|cjs|groovy)\b",
        text,
    ))
    identifiers.update(re.findall(r"\b([a-z]+_[a-z_]+)\b", text))
    identifiers.update(re.findall(
        r"\b(\w+(?:Error|Exception|Failure|Warning|Panic))\b", text,
    ))
    identifiers.update(re.findall(r"\b([a-zA-Z_]\w+\.[a-zA-Z_]\w+)\b", text))
    identifiers.update(re.findall(
        r"(?:function|method|class|module|package|func|def|struct|interface)\s+[`\"]?(\w+)",
        text, re.I,
    ))
    identifiers.update(re.findall(r"File \"([^\"]+\.py)\", line \d+", text))
    identifiers.update(re.findall(r", in (\w+)\s*$", text, re.MULTILINE))

    filtered: list[str] = []
    for ident in identifiers:
        if isinstance(ident, tuple):
            for part in ident:
                if (
                    isinstance(part, str)
                    and len(part) >= 3
                    and part not in _NOISE_WORDS
                    and part not in high_freq
                    and not part.startswith(".")
                ):
                    filtered.append(part)
            continue
        if (
            ident in _NOISE_WORDS
            or ident in high_freq
            or len(ident) < 3
            or ident.startswith(".")
        ):
            continue
        filtered.append(ident)

    seen: set[str] = set()
    out: list[str] = []
    for ident in sorted(filtered, key=len, reverse=True):
        if "." in ident:
            for part in ident.split("."):
                if (
                    part not in seen
                    and part not in _NOISE_WORDS
                    and part not in high_freq
                    and len(part) >= 3
                ):
                    seen.add(part)
            if ident not in seen:
                seen.add(ident)
                out.append(ident)
        elif ident not in seen:
            seen.add(ident)
            out.append(ident)
        if len(out) >= MAX_IDENTIFIERS:
            break
    return out


def _resolve_to_seeds(
    conn: sqlite3.Connection, identifiers: list[str]
) -> tuple[set[str], set[str]]:
    entry_files: set[str] = set()
    entry_symbols: set[str] = set()
    for ident in identifiers:
        leaf = ident.split(".")[-1]
        rows = conn.execute(
            f"SELECT file_path FROM nodes "
            f"WHERE name = ? AND {LABELS_FILTER} "
            f"AND (is_test = 0 OR is_test IS NULL) "
            f"ORDER BY id ASC LIMIT 50",
            (leaf,),
        ).fetchall()
        if rows:
            entry_symbols.add(leaf)
            for r in rows:
                entry_files.add(r["file_path"])
        else:
            if "/" in ident or ident.endswith(
                (".py", ".go", ".js", ".ts", ".rs", ".java")
            ):
                fp_rows = conn.execute(
                    "SELECT DISTINCT file_path FROM nodes WHERE file_path = ? "
                    "OR file_path LIKE ? ORDER BY file_path ASC LIMIT 5",
                    (ident, f"%{ident}"),
                ).fetchall()
                for r in fp_rows:
                    entry_files.add(r["file_path"])
    return entry_files, entry_symbols


def _expand_one_step(
    conn: sqlite3.Connection, frontier: set[str], has_conf: bool
) -> set[str]:
    if not frontier:
        return set()
    placeholders = ",".join("?" * len(frontier))
    conf_clause = (
        f" AND e.confidence >= {MIN_REL_CONFIDENCE}" if has_conf else ""
    )
    sql = f"""
        SELECT DISTINCT s.file_path AS src, t.file_path AS tgt
        FROM edges e
        JOIN nodes s ON e.source_id = s.id
        JOIN nodes t ON e.target_id = t.id
        WHERE (s.file_path IN ({placeholders}) OR t.file_path IN ({placeholders}))
          AND e.type IN ('CALLS', 'IMPORTS')
          {conf_clause}
        ORDER BY e.id ASC
        LIMIT 5000
    """
    files = list(frontier) + list(frontier)
    rows = conn.execute(sql, files).fetchall()
    out: set[str] = set()
    for r in rows:
        if r["src"]:
            out.add(r["src"])
        if r["tgt"]:
            out.add(r["tgt"])
    return out


def _bfs(
    conn: sqlite3.Connection, seeds: set[str], max_depth: int
) -> dict[str, int]:
    has_conf = _has_confidence(conn)
    distances: dict[str, int] = {f: 0 for f in seeds}
    frontier: set[str] = set(seeds)
    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        reached = _expand_one_step(conn, frontier, has_conf)
        new_frontier: set[str] = set()
        for f in reached:
            if f not in distances:
                distances[f] = depth
                new_frontier.add(f)
        frontier = new_frontier
    return distances


def _files_with_symbol(conn: sqlite3.Connection, symbol: str) -> set[str]:
    rows = conn.execute(
        f"SELECT DISTINCT file_path FROM nodes "
        f"WHERE name = ? AND {LABELS_FILTER} "
        f"ORDER BY file_path ASC LIMIT 50",
        (symbol,),
    ).fetchall()
    return {r["file_path"] for r in rows}


def _mode_relevant(description: str) -> tuple[list[str], int]:
    conn, rc = _open_graph_db()
    if conn is None:
        return [], rc
    try:
        # RC-01: feed the per-repo high-frequency name set to identifier
        # extraction so the dominant Zipf-head identifiers (e.g. a project's
        # ubiquitous noun) are dropped without any literal repo name in code.
        identifiers = _extract_identifiers(
            description, _high_freq_repo_identifiers(conn)
        )
        seed_files, entry_symbols = _resolve_to_seeds(conn, identifiers)

        if not seed_files:
            short = re.sub(r"\s+", " ", description).strip()[:80]
            conn.close()
            return [
                f"# gt_navigate relevant: '{short}' — entry symbols="
                f"{sorted(entry_symbols)}, seeds=0, candidates=0",
                "# (no graph nodes matched extracted identifiers; "
                "try gt_search code for prose tokens or gt_search class on a CamelCase term)",
            ], 0

        distances = _bfs(conn, seed_files, MAX_DEPTH)
        entry_symbol_files: set[str] = set()
        for sym in entry_symbols:
            entry_symbol_files |= _files_with_symbol(conn, sym)

        scored: list[tuple[float, int, str]] = []
        for f, d in distances.items():
            score = 1.0 / (2 ** d)
            if f in entry_symbol_files:
                score *= 1.5
            if d >= 1 and f not in entry_symbol_files:
                continue
            if score >= RELEVANCE_THRESHOLD:
                scored.append((score, d, f))
        scored.sort(key=lambda t: (-t[0], t[1], t[2]))
        top = scored[:MAX_CANDIDATES]
    except sqlite3.OperationalError as e:
        print(f"{TOOL}: query failed: {e}", file=sys.stderr)
        conn.close()
        return [], 3
    conn.close()

    short = re.sub(r"\s+", " ", description).strip()[:80]
    out: list[str] = [
        f"# gt_navigate relevant: '{short}' — entry symbols={sorted(entry_symbols)}, "
        f"seeds={len(seed_files)}, candidates={len(top)}"
    ]
    if not top:
        out.append(
            "# (BFS produced no files passing the relevance threshold; "
            "try gt_navigate trace on one of the entry symbols, "
            "or gt_search code on a key token)"
        )
    else:
        for score, d, f in top:
            if d == 0:
                rel = "HIGH"
            elif d == 1:
                rel = "MEDIUM"
            else:
                rel = "LOW"
            note = (
                "contains entry symbol"
                if f in entry_symbol_files else f"distance {d}"
            )
            out.append(f"{rel:<6}  score={score:.3f}  {f}  ({note})")
    return out, 0


# ── Telemetry ────────────────────────────────────────────────────────────────
def _emit_telemetry(
    mode: str, symbol: str, returned_lines: int
) -> None:
    log_dir = os.environ.get("GT_INSTANCE_LOG_DIR")
    if not log_dir:
        return
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        # Truncate description-style first arg for telemetry sanity.
        rec = {
            "tool": TOOL,
            "mode": mode,
            "args": {"symbol": symbol[:200]},
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


# ── Main ─────────────────────────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            f"usage: {TOOL} <symbol> <mode>\n"
            f"  mode = one of: {', '.join(VALID_MODES)}",
            file=sys.stderr,
        )
        _emit_telemetry(
            argv[2] if len(argv) > 2 else "",
            argv[1] if len(argv) > 1 else "",
            0,
        )
        return 2

    symbol = argv[1].strip() if argv[1] else ""
    mode = argv[2].strip() if argv[2] else ""

    if mode not in VALID_MODES:
        print(
            f"{TOOL}: invalid mode '{mode}'. Valid: {', '.join(VALID_MODES)}",
            file=sys.stderr,
        )
        _emit_telemetry(mode, symbol, 0)
        return 2
    if not symbol:
        print(f"{TOOL}: symbol/description is required", file=sys.stderr)
        _emit_telemetry(mode, symbol, 0)
        return 2

    if mode == "trace":
        out_lines, rc = _mode_trace(symbol)
    elif mode == "impact":
        out_lines, rc = _mode_impact(symbol)
    elif mode == "relevant":
        out_lines, rc = _mode_relevant(symbol)
    else:  # pragma: no cover — already validated above.
        out_lines, rc = [], 2

    if out_lines:
        print("\n".join(out_lines))
    _emit_telemetry(mode, symbol, len(out_lines))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
