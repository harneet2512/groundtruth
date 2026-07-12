"""Capability-receipt PRODUCER — emit the baked-surface facts the Profile-2 preflight consumes.

Fix B (``rl_profile._MEMBER_CAPABILITY_RECEIPT``, 2026-07-12) fails the 4 substrate-property
Profile-2 members CLOSED against a ``GT_CAPABILITY_RECEIPT`` inline-JSON bundle: a stale substrate
that ships the code but lacks the baked SURFACE aborts before spend rather than self-attesting. This
module is the other half of that contract — it reads the REAL shipped substrate (base graph.db +
staged gt-index binary + baked brief) and emits exactly the fields those predicates read.

Wired in ``swebench_live_lite_full.yml`` immediately BEFORE the ``rl_profile --emit-exports`` preflight:

    export GT_CAPABILITY_RECEIPT="$(PYTHONPATH=/opt/gt python -m groundtruth.runtime.capability_receipt --emit-json)"

so the preflight subprocess inherits it. Absent/empty/unreadable substrate => the fully-closed
receipt => those members abort (correct-or-quiet: GT never grades a surface it does not carry).

Design invariant: ``build_receipt`` NEVER raises. Each field is computed independently and degrades
to its own fail-closed value (0 / "" / False) on any error — one broken source can't blind the rest.

Field sources (verified against runtime code 2026-07-12):
  * symbol_content_fts_rows : SELECT count(*) FROM symbol_content_fts   (content_fts.go:201; the
                              per-symbol body-BM25 content leg — GT_CONTENT_LEG)
  * sem_body_rows           : count of properties.kind IN (body_terms,string_literals) — the EXACT
                              body channels the C1 fail-closed gate reads (graph_localizer.py:2288;
                              the semantic body leg — GT_SEM_BODY)
  * gt_index_bin            : GT_INDEX_BIN iff it is a real file  (the staged per-turn reindex binary
                              /opt/gt/gt-index — GT_L6_FRESH)
  * brief_minimal           : $GT_CERT_DIR/brief.txt exists AND carries none of the SM-6-retired heavy
                              surfaces  (gt_agent.py:1013; the reduced step-0 brief — GT_BRIEF_MINIMAL)

The base-graph precedence (arg -> GT_HOST_GRAPH_DB -> GT_GRAPH_DB) mirrors the runtime resolver
(``gt_mini_patch._db_path`` / ``runtime.context.from_env``): the receipt proves the SHIPPED base
surface, so it reads the untouched base mount, never the derived L6 work-copy.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Mapping, Optional, Sequence

# The SM-6-retired heavy brief surfaces. A brief that still carries any of these is the FULL brief,
# not the reduced step-0 brief — so GT_BRIEF_MINIMAL must fail closed until a rebake reduces it.
_HEAVY_BRIEF_MARKERS = ("<gt-graph-map", "<gt-localization", "<gt-contract")


def _resolve_graph_db(graph_db: Optional[str], env: Mapping[str, str]) -> str:
    """Base-graph path, runtime-resolver precedence. Explicit arg wins; else the untouched base
    mount (GT_HOST_GRAPH_DB); else the in-container canonical name (GT_GRAPH_DB)."""
    if graph_db:
        return graph_db
    return (env.get("GT_HOST_GRAPH_DB") or env.get("GT_GRAPH_DB") or "").strip()


def _count(graph_db: str, sql: str) -> int:
    """A scalar count off a READ-ONLY graph.db; ANY failure (absent file, 0-byte handoff, missing
    table, unreadable db) degrades to 0 so the gated member fails closed rather than crashing."""
    if not graph_db or not os.path.isfile(graph_db):
        return 0
    conn = None
    try:
        conn = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:  # noqa: BLE001 — correct-or-quiet: a broken source proves no surface
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _resolve_brief_path(brief_path: Optional[str], env: Mapping[str, str]) -> str:
    """The baked-brief path. Explicit arg wins; else ``$GT_CERT_DIR/brief.txt`` (the read-only
    substrate brief the adapter consumes, gt_agent.py:1013)."""
    if brief_path:
        return brief_path
    cert_dir = (env.get("GT_CERT_DIR") or "").strip()
    return os.path.join(cert_dir, "brief.txt") if cert_dir else ""


def _brief_is_minimal(brief_path: str) -> bool:
    """True IFF the baked brief exists, is non-empty, and carries NONE of the SM-6-retired heavy
    surfaces. Absent/empty/unreadable => False (a run with no reduced brief cannot claim minimal)."""
    if not brief_path or not os.path.isfile(brief_path):
        return False
    try:
        text = open(brief_path, "r", encoding="utf-8", errors="replace").read()
    except Exception:  # noqa: BLE001
        return False
    if not text.strip():
        return False
    low = text.lower()
    return not any(marker in low for marker in _HEAVY_BRIEF_MARKERS)


def build_receipt(
    graph_db: Optional[str] = None,
    *,
    index_bin: Optional[str] = None,
    brief_path: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    """The capability receipt for the current substrate. Pure + deterministic + NEVER raises.

    Any argument left ``None`` is resolved from ``env`` (default ``os.environ``). Each field is
    independently fail-closed: a broken source yields that field's closed value only.
    """
    e = os.environ if env is None else env
    gdb = _resolve_graph_db(graph_db, e)
    idx = (index_bin if index_bin is not None else (e.get("GT_INDEX_BIN") or "")).strip()
    brief = _resolve_brief_path(brief_path, e)
    return {
        "symbol_content_fts_rows": _count(gdb, "SELECT count(*) FROM symbol_content_fts"),
        "sem_body_rows": _count(
            gdb,
            "SELECT count(*) FROM properties WHERE kind IN ('body_terms','string_literals')",
        ),
        "gt_index_bin": idx if (idx and os.path.isfile(idx)) else "",
        "brief_minimal": _brief_is_minimal(brief),
    }


def _compact(obj: dict) -> str:
    """One-line deterministic JSON (sorted keys) — captured into a single env var by the workflow."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def main(
    argv: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, str]] = None,
    out=None,
    err=None,
) -> int:
    """CLI. ``--emit-json`` (default) prints the compact receipt JSON; ``--emit-export`` prints a
    shell ``export GT_CAPABILITY_RECEIPT='<json>'`` line. Always returns 0 — the receipt is
    fail-closed data, never an error condition (an empty substrate is a valid, fully-closed receipt).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    out = sys.stdout if out is None else out
    receipt = build_receipt(env=env)
    payload = _compact(receipt)
    if "--emit-export" in args:
        out.write("export GT_CAPABILITY_RECEIPT=" + _shell_single_quote(payload) + "\n")
    else:  # --emit-json (default)
        out.write(payload + "\n")
    return 0


def _shell_single_quote(s: str) -> str:
    """POSIX single-quote a string for a safe ``export`` line (handles any embedded quote)."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":  # pragma: no cover — thin CLI shim
    raise SystemExit(main())
