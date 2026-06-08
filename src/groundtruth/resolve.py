"""gt-resolve: Diagnose and resolve ambiguous edges in graph.db using LSP.

Two modes:
  - Diagnostic (default): show ambiguous edges and which LSP servers could resolve them
  - Resolution (--resolve): use installed LSP servers to verify/fix ambiguous edges

Usage:
    groundtruth resolve --db graph.db                        # diagnostic mode
    groundtruth resolve --db graph.db --resolve              # live LSP resolution
    groundtruth resolve --db graph.db --resolve --lang python  # resolve Python only
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


def _path_to_uri(abs_path: str) -> str:
    """Absolute filesystem path -> RFC-8089 file URI, correct on POSIX and Windows.

    The naive f"file:///{path}" double-counts the leading slash on POSIX
    (/home -> file:////home, four slashes), which LSP servers (pyright) reject
    with a UriError. Path.as_uri() emits file:///home on POSIX and
    file:///C:/foo on Windows, and percent-encodes spaces.
    """
    try:
        return Path(abs_path).as_uri()
    except ValueError:
        # as_uri requires an absolute path; fall back defensively.
        p = abs_path.replace(os.sep, "/")
        return "file://" + (p if p.startswith("/") else "/" + p)


def _uri_to_path(uri: str) -> str:
    """file URI -> filesystem path, inverse of _path_to_uri (POSIX + Windows)."""
    parsed = urlparse(uri)
    return url2pathname(unquote(parsed.path))


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE SOURCE OF TRUTH for "which languages this precision pass can serve".
#
# item #30: the dispatch tables (_KNOWN_SERVERS for the install/detect report,
# _LANG_TO_EXT for the name→ext lookup _resolve_edges uses) MUST advertise only
# languages that config.LSP_SERVERS can actually start. Previously they hard-coded
# c/cpp/ruby/kotlin (clangd/solargraph/kotlin-language-server) that LSP_SERVERS has
# NO config for, so resolve_main's `servers.get(args.lang)` gate (~line 872) passed
# whenever that binary was on PATH, the run proceeded, then get_server_config(ext)
# returned Err → stats["skipped"]=len(edges) and the WHOLE pass silently no-op'd —
# while the diagnostic printer told users to "install clangd/solargraph" for a pass
# that could never run. Deriving both tables from LSP_SERVERS makes
# `_KNOWN_SERVERS keys ⊆ LSP_SERVERS keys` a structural invariant, not a hope.
#
# _LANG_TO_EXT maps every language NAME (and the ext spelled as a name, e.g. "py")
# to the canonical LSP_SERVERS extension key. config.LANGUAGE_IDS gives ext→lang-id
# (e.g. ".tsx"→"typescriptreact"); we invert it and add the short ext aliases.
def _build_lang_to_ext() -> dict[str, str]:
    from groundtruth.lsp.config import LANGUAGE_IDS, LSP_SERVERS

    out: dict[str, str] = {}
    for ext in LSP_SERVERS:  # ONLY extensions we can actually serve
        # ext spelled as a name without the dot ("py", "ts", "go", ...)
        out[ext.lstrip(".")] = ext
        # the human language id for that ext ("python", "typescript", ...)
        lang_id = LANGUAGE_IDS.get(ext)
        if lang_id:
            out[lang_id] = ext
    return out


_LANG_TO_EXT: dict[str, str] = _build_lang_to_ext()


# Language NAME -> language-server command, for the install/detect report only.
# Keys are LANGUAGE NAMES + short ext aliases; values are the binary to probe on
# PATH. Built from LSP_SERVERS so a name appears here iff its ext is serveable.
def _build_known_servers() -> dict[str, str]:
    from groundtruth.lsp.config import LANGUAGE_IDS, LSP_SERVERS

    out: dict[str, str] = {}
    for ext, cfg in LSP_SERVERS.items():
        cmd = cfg.command[0] if cfg.command else ""
        out[ext.lstrip(".")] = cmd
        lang_id = LANGUAGE_IDS.get(ext)
        if lang_id:
            out[lang_id] = cmd
    return out


_KNOWN_SERVERS: dict[str, str] = _build_known_servers()


# ext -> LSP languageId, derived from config.LANGUAGE_IDS (the same source of
# truth). Falls back to the bare ext name for any ext config doesn't enumerate.
def _build_ext_to_lang_id() -> dict[str, str]:
    from groundtruth.lsp.config import LANGUAGE_IDS

    return dict(LANGUAGE_IDS)


_EXT_TO_LANG_ID: dict[str, str] = _build_ext_to_lang_id()


def _lang_id_for_ext(ext: str) -> str:
    return _EXT_TO_LANG_ID.get(ext, ext.lstrip("."))


def _detect_servers() -> dict[str, bool]:
    """Detect which language servers are installed."""
    return {lang: shutil.which(cmd) is not None for lang, cmd in _KNOWN_SERVERS.items()}


def _count_residual_method_edges(
    conn: sqlite3.Connection,
    language: str | None = None,
    source_files: list[str] | None = None,
    cap: int | None = None,
) -> int:
    """Count name_match METHOD-CALL edges present BEFORE the resolve pass.

    This is the *denominator* of the resolution-fraction reported on the
    ``LSP_METRICS`` contract line. It is deliberately NOT the same set as
    ``_get_ambiguous_edges`` (which returns *all* sub-threshold CALLS edges and
    is capped by ``--max-edges``): the metric needs the true count of the
    population the LSP precision pass is meant to convert.

    "Method-call edge" is encoded structurally and language-agnostically as a
    ``name_match`` CALLS edge whose TARGET node is a ``Method`` — i.e. ``obj.m()``
    whose receiver type was never resolved, so the call was matched by name across
    classes. Per CLAUDE.md (conan-17123 trace) ~98% of name_match edges are method
    calls; this is the population graph.db cannot trust until LSP/propagation
    resolves the receiver type. The count is scoped to ``source_files`` (the issue
    subgraph) when given, else the whole graph — this is what makes a capped or
    un-scoped pass *detectable*: ``resolved/residual`` drops when only a slice of a
    large residual was touched.

    ``cap`` makes the denominator CAP-CONSISTENT with the attempt budget
    (``--max-edges``). The resolve pass can only attempt at most ``cap`` edges, so
    measuring ``resolved`` against a residual LARGER than ``cap`` yields a ceiling of
    ``cap/residual`` that can fall below the gate floor even at 100% LSP success —
    a mathematically unpassable gate on any large un-scoped repo (the checkov class).
    Capping the residual at the attempt budget makes the fraction "of what we could
    attempt, how many resolved," so the floor is real work, not a coin-flip against
    the cap. Language-agnostic: it is a property of the attempt budget vs population,
    not of any repo/language. When demand-scoping makes the residual naturally small
    (< cap), the cap is a no-op and the fraction is the true in-scope resolution rate.
    """
    try:
        conn.execute("SELECT resolution_method FROM edges LIMIT 0")
    except sqlite3.OperationalError:
        return 0

    query = (
        "SELECT COUNT(*) FROM edges e "
        "JOIN nodes src ON e.source_id = src.id "
        "JOIN nodes tgt ON e.target_id = tgt.id "
        "WHERE e.type = 'CALLS' AND e.resolution_method = 'name_match' "
        "AND tgt.label = 'Method'"
    )
    params: list = []
    if language:
        query += " AND src.language = ?"
        params.append(language)
    if source_files:
        placeholders = ",".join("?" for _ in source_files)
        query += f" AND e.source_file IN ({placeholders})"
        params.extend(source_files)

    row = conn.execute(query, params).fetchone()
    count = int(row[0]) if row else 0
    if cap is not None and cap > 0:
        count = min(count, cap)
    return count


def _get_ambiguous_edges(
    conn: sqlite3.Connection,
    min_confidence: float = 0.9,
    language: str | None = None,
    source_files: list[str] | None = None,
    limit: int = 500,
) -> list[dict]:
    """Get edges below confidence threshold.

    Args:
        source_files: If provided, only return edges whose source_file
            matches one of these paths (scoped promotion).
        limit: Max ambiguous edges to return (was a hardcoded LIMIT 500 — the
            broken-machine-gun cap: graphs with thousands of name_match edges
            could never be more than partially LSP-resolved, so the structural
            graph stayed 30-50% name_match noise regardless of --max-edges).
            Now driven by the caller's --max-edges so a full resolve cleans all.
    """
    conn.row_factory = sqlite3.Row

    # Check if confidence column exists
    try:
        conn.execute("SELECT confidence FROM edges LIMIT 0")
    except sqlite3.OperationalError:
        print(
            "ERROR: graph.db has no confidence column (indexed with old gt-index).", file=sys.stderr
        )
        print("Re-index with gt-index v14+ to add confidence scoring.", file=sys.stderr)
        return []

    query = """
        SELECT e.id, e.source_id, e.target_id, e.resolution_method,
               e.confidence, e.source_file, e.source_line,
               src.name as caller_name, src.language,
               tgt.name as target_name, tgt.file_path as target_file
        FROM edges e
        JOIN nodes src ON e.source_id = src.id
        JOIN nodes tgt ON e.target_id = tgt.id
        WHERE e.confidence < ? AND e.type = 'CALLS'
    """
    params: list = [min_confidence]

    if language:
        query += " AND src.language = ?"
        params.append(language)

    if source_files:
        placeholders = ",".join("?" for _ in source_files)
        query += f" AND e.source_file IN ({placeholders})"
        params.extend(source_files)

    query += " ORDER BY e.confidence ASC LIMIT ?"
    params.append(int(limit))

    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _print_summary(
    edges: list[dict],
    servers: dict[str, bool],
    min_confidence: float,
) -> None:
    """Print human-readable summary of ambiguous edges."""
    if not edges:
        print("No ambiguous edges found below confidence threshold.")
        return

    # Group by confidence bucket
    buckets: dict[str, list] = {"0.0-0.2": [], "0.2-0.4": [], "0.4-0.6": [], "0.6-0.9": []}
    for e in edges:
        c = e["confidence"]
        if c < 0.2:
            buckets["0.0-0.2"].append(e)
        elif c < 0.4:
            buckets["0.2-0.4"].append(e)
        elif c < 0.6:
            buckets["0.4-0.6"].append(e)
        else:
            buckets["0.6-0.9"].append(e)

    print(f"\n{'=' * 60}")
    print(f"Ambiguous edges (confidence < {min_confidence}): {len(edges)}")
    print(f"{'=' * 60}\n")

    for bucket_name, bucket_edges in buckets.items():
        if bucket_edges:
            print(f"  [{bucket_name}] {len(bucket_edges)} edges")

    # Group by language
    by_lang: dict[str, int] = {}
    for e in edges:
        lang = e.get("language", "unknown")
        by_lang[lang] = by_lang.get(lang, 0) + 1

    print("\nBy language:")
    for lang, count in sorted(by_lang.items(), key=lambda x: -x[1]):
        server_status = "installed" if servers.get(lang) else "NOT INSTALLED"
        print(f"  {lang}: {count} edges (LSP server: {server_status})")

    # Show sample edges
    print("\nSample ambiguous edges (top 20):")
    print(f"{'Confidence':>10}  {'Caller':30s}  {'Target':30s}  {'Method'}")
    print(f"{'-' * 10}  {'-' * 30}  {'-' * 30}  {'-' * 12}")
    for e in edges[:20]:
        caller = f"{e['caller_name']}() @ {os.path.basename(e.get('source_file', '?'))}"
        target = f"{e['target_name']}() @ {os.path.basename(e.get('target_file', '?'))}"
        print(f"{e['confidence']:>10.2f}  {caller:30s}  {target:30s}  {e['resolution_method']}")

    if len(edges) > 20:
        print(f"  ... and {len(edges) - 20} more")

    # Resolution recommendation
    resolvable = sum(1 for e in edges if servers.get(e.get("language", ""), False))
    print(f"\n{'=' * 60}")
    print(f"Resolvable with installed LSP servers: {resolvable}/{len(edges)} edges")
    if resolvable < len(edges):
        missing_langs = {e.get("language") for e in edges if not servers.get(e.get("language", ""))}
        print(f"Install LSP servers for: {', '.join(sorted(missing_langs))}")
        for lang in sorted(missing_langs):
            cmd = _KNOWN_SERVERS.get(lang, "?")
            print(f"  {lang}: install '{cmd}'")
    print(f"{'=' * 60}")


def _apply_lsp_resolution(
    conn: sqlite3.Connection,
    *,
    edge: dict,
    target_rel: str,
    target_line: int,
    target_name: str,
    stats: dict[str, int],
    has_trust_tier: bool,
) -> str:
    """Apply one LSP definition outcome to graph.db and bump ``stats``.

    Pure, synchronous, and free of LSP/IO so the production resolve path and the
    unit tests run the IDENTICAL match + delete-guard logic. Returns the outcome
    label ("verified" / "corrected" / "deleted" / "skipped") it recorded.

    item #29 — match PRIMARILY by ``(file_path, line-window)``. The LSP's LOCATION
    is the authority, not the pre-resolution callee NAME. The old query hard-filtered
    ``name = target_name``, so a CORRECTED call to a differently-named symbol (alias,
    re-export, ``super().__init__`` → the parent class name) never matched the real
    node and fell to the destructive DELETE arm — the exact ambiguous-method case
    this pass exists to FIX. ``name`` is now only a TIEBREAKER inside the window
    (``ORDER BY (name = ?) DESC``), never a gate. The window itself
    (``start_line <= target_line <= end_line``, or NULL ``end_line``) is unchanged.
    Exact ``file_path`` match — NOT ``LIKE '%basename'`` which collides on common
    basenames (mod.rs, index.ts, utils.py, __init__.py) and can pick the WRONG node.

    item #28 — a missing node is NOT automatically a false positive. DELETE is the
    highest-harm action in this file (a read-pass that destroys edges), so it fires
    ONLY when we can PROVE the edge is spurious: the LSP definition lands in a file
    the indexer DID ingest, yet no node there spans the call site. Two cases must
    NEVER delete:
      (1) EXTERNAL/stdlib target — ``target_rel`` is empty, escaped with ``..``, or
          absolute (the LSP correctly resolved OUTSIDE the repo — the common
          join/get/append/loads case). That is a real resolution; leave the edge
          intact (correct-or-quiet). Deleting it would erase a true call edge.
      (2) FILE NOT INDEXED — ``target_rel`` has zero nodes in graph.db (generated/
          vendored/excluded). No ground truth there, so a line-window miss (incl.
          NULL ``end_line`` / tree-sitter↔LSP line drift on decorators/comments)
          must NOT trigger a destructive delete.
    Only when the file IS indexed AND still no window match → genuine FP → delete.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT id FROM nodes
           WHERE file_path = ?
           AND start_line <= ? AND (end_line >= ? OR end_line IS NULL)
           ORDER BY (name = ?) DESC, start_line DESC LIMIT 1""",
        (target_rel, target_line, target_line, target_name),
    ).fetchone()

    if row:
        lsp_target_id = row["id"]
        current_target_id = edge["target_id"]
        _tier_clause = ", trust_tier = 'CERTIFIED'" if has_trust_tier else ""
        if lsp_target_id == current_target_id:
            conn.execute(
                f"UPDATE edges SET confidence = 1.0, resolution_method = 'lsp'{_tier_clause} WHERE id = ?",
                (edge["id"],),
            )
            stats["verified"] += 1
            return "verified"
        conn.execute(
            f"UPDATE edges SET target_id = ?, confidence = 1.0, resolution_method = 'lsp'{_tier_clause} WHERE id = ?",
            (lsp_target_id, edge["id"]),
        )
        stats["corrected"] += 1
        return "corrected"

    _is_external = (
        not target_rel
        or target_rel.startswith("..")
        or os.path.isabs(target_rel)
    )
    if _is_external:
        stats["skipped"] += 1
        return "skipped"

    _file_indexed = conn.execute(
        "SELECT 1 FROM nodes WHERE file_path = ? LIMIT 1",
        (target_rel,),
    ).fetchone()
    if _file_indexed:
        # Indexed file, no node spans the call site → genuine FP.
        conn.execute("DELETE FROM edges WHERE id = ?", (edge["id"],))
        stats["deleted"] += 1
        return "deleted"
    # File not in the graph → no ground truth → never delete.
    stats["skipped"] += 1
    return "skipped"


async def _resolve_edges(
    db_path: str,
    root: str,
    edges: list[dict],
    language: str,
) -> dict[str, int]:
    """Resolve ambiguous edges using LSP textDocument/definition.

    For each ambiguous edge:
    1. Open the source file in the LSP server
    2. Ask textDocument/definition at the call site
    3. If LSP returns a target:
       - If it matches the current edge target → upgrade confidence to 1.0
       - If it differs → update edge target + confidence to 1.0
       - If no target in graph → delete the edge (false positive)
    """
    try:
        from groundtruth.lsp.client import LSPClient
        from groundtruth.lsp.config import get_server_config
        from groundtruth.utils.result import Err as LspErr
    except ImportError:
        print(
            "ERROR: LSP client not available. Install with: pip install -e '.[dev]'",
            file=sys.stderr,
        )
        return {"error": 1}

    stats = {"verified": 0, "corrected": 0, "deleted": 0, "failed": 0, "skipped": 0}

    # Map the language NAME to its real file extension (LSP_SERVERS is keyed by
    # extension, e.g. ".py", not ".python"). This is the fix for the universal LSP
    # no-op — without it 4/5 languages fell through to "No LSP server configured"
    # and skipped every edge. Generalized: one map, every language, one product.
    ext = language if language.startswith(".") else _LANG_TO_EXT.get(language, f".{language}")
    config_result = get_server_config(ext)
    if isinstance(config_result, LspErr):
        print(f"  No LSP server configured for {language}", file=sys.stderr)
        stats["skipped"] = len(edges)
        return stats

    config = config_result.value

    # Start LSP server
    abs_root = os.path.abspath(root)
    root_uri = _path_to_uri(abs_root)

    # δ: when the server is pyright and the project has no pyrightconfig,
    # drop a minimal one so pyright doesn't assume python<3.10 and refuse
    # to evaluate `str | None` union annotations. typeCheckingMode=off
    # because textDocument/definition doesn't need full type checking.
    if language == "python" and "pyright" in (config.command[0] or "").lower():
        _pyright_cfg = os.path.join(abs_root, "pyrightconfig.json")
        _pyproject_toml = os.path.join(abs_root, "pyproject.toml")
        if not os.path.exists(_pyright_cfg):
            _has_pyright_in_pyproject = False
            try:
                if os.path.exists(_pyproject_toml):
                    with open(_pyproject_toml, encoding="utf-8", errors="replace") as _pf:
                        _has_pyright_in_pyproject = "[tool.pyright]" in _pf.read()
            except Exception:
                pass
            if not _has_pyright_in_pyproject:
                try:
                    import json as _json
                    with open(_pyright_cfg, "w", encoding="utf-8") as _wf:
                        _wf.write(_json.dumps({
                            "pythonVersion": "3.11",
                            "typeCheckingMode": "off",
                            "reportMissingImports": "none",
                        }))
                except Exception as _e:
                    print(f"  pyrightconfig.json write failed: {_e}", file=sys.stderr)

    print(f"  Starting {config.command[0]} for {language}...")
    client = LSPClient(config.command, root_uri)

    try:
        start_result = await client.start()
        if isinstance(start_result, LspErr):
            print(f"  LSP start failed: {start_result.error.message}", file=sys.stderr)
            stats["failed"] = len(edges)
            return stats
    except Exception as e:
        print(f"  Failed to start LSP: {e}", file=sys.stderr)
        stats["failed"] = len(edges)
        return stats

    # LSP spec requires initialize/initialized handshake before any requests.
    # Without this, servers like Pyright reject all textDocument/* calls.
    init_params = {
        "processId": os.getpid(),
        "rootUri": root_uri,
        "capabilities": {
            "textDocument": {
                "definition": {},
                "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                "hover": {"contentFormat": ["markdown", "plaintext"]},
                "publishDiagnostics": {"relatedInformation": True},
            },
            "workspace": {
                "workspaceFolders": True,
            },
        },
        "workspaceFolders": [
            {"uri": root_uri, "name": os.path.basename(abs_root)},
        ],
    }
    try:
        init_result = await client.send_request("initialize", init_params)
        if isinstance(init_result, LspErr):
            print(f"  LSP initialize failed: {init_result.error.message}", file=sys.stderr)
            stats["failed"] = len(edges)
            return stats
        await client.send_notification("initialized", {})
        await client.drain(timeout=2.0)
        await client.wait_for_progress_complete(timeout=120.0)
        print(f"  LSP initialized, resolving {len(edges)} edges...")
    except Exception as e:
        print(f"  LSP initialize failed: {e}", file=sys.stderr)
        try:
            await client.shutdown()
        except Exception:
            pass
        stats["failed"] = len(edges)
        return stats

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL mode allows concurrent readers + one writer without SQLITE_BUSY.
    # busy_timeout retries for 5s before raising OperationalError.
    # Required for: (1) intra-process: this conn + _enrich_conn both open,
    # (2) inter-process: parallel --lang runs on the same graph.db.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # Performance pragmas. NOTE: query_only is intentionally OMITTED — this
    # connection WRITES to edges (UPDATE/DELETE + commit below). The remaining
    # three are pure read/scratch tuning, safe for the write path.
    conn.execute("PRAGMA mmap_size=268435456")
    conn.execute("PRAGMA cache_size=-8000")
    conn.execute("PRAGMA temp_store=MEMORY")

    # Check if trust_tier column exists (absent in older graph.db versions)
    _has_trust_tier = False
    try:
        conn.execute("SELECT trust_tier FROM edges LIMIT 0")
        _has_trust_tier = True
    except sqlite3.OperationalError:
        pass

    opened_files: set[str] = set()

    for i, edge in enumerate(edges):
        source_file = edge.get("source_file", "")
        source_line = edge.get("source_line", 0) or 0
        target_name = edge.get("target_name", "")

        if not source_file or not target_name:
            stats["skipped"] += 1
            continue

        abs_source = os.path.join(abs_root, source_file)
        if not os.path.exists(abs_source):
            stats["skipped"] += 1
            continue

        # Open the file in LSP if not already opened
        uri = _path_to_uri(abs_source)
        if uri not in opened_files:
            try:
                with open(abs_source, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                await client.did_open(uri, _lang_id_for_ext(ext), 1, text)
                opened_files.add(uri)
            except Exception:
                stats["failed"] += 1
                continue

        # Find column of the call on the source line
        try:
            with open(abs_source, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if source_line <= 0 or source_line > len(lines):
                stats["skipped"] += 1
                continue
            line_text = lines[source_line - 1]  # 1-indexed
            col = line_text.find(target_name)
            if col == -1:
                col = 0
        except Exception:
            stats["failed"] += 1
            continue

        # Ask LSP for definition
        try:
            def_result = await client.definition(uri, source_line - 1, col)
            if isinstance(def_result, LspErr):
                stats["failed"] += 1
                continue

            locations = def_result.value
            if not locations:
                # LSP couldn't resolve — mark as checked
                stats["failed"] += 1
                continue

            # Got a definition location
            target_uri = locations[0].uri
            target_line = locations[0].range.start.line + 1  # 0-indexed → 1-indexed

            # Convert URI to relative path
            target_path = _uri_to_path(target_uri)
            try:
                target_rel = os.path.relpath(target_path, abs_root).replace("\\", "/")
            except ValueError:
                target_rel = target_path

            # Apply the verify/correct/delete decision for this LSP definition.
            # The match + destructive-delete guard live in _apply_lsp_resolution so
            # the production path and the unit test exercise the SAME logic (items
            # #28 destructive-delete guard, #29 location-primary match).
            _apply_lsp_resolution(
                conn,
                edge=edge,
                target_rel=target_rel,
                target_line=target_line,
                target_name=target_name,
                stats=stats,
                has_trust_tier=_has_trust_tier,
            )

        except Exception:
            stats["failed"] += 1
            continue

        # Progress every 100 edges
        if (i + 1) % 100 == 0:
            print(f"  ... {i + 1}/{len(edges)} edges processed", file=sys.stderr)

    conn.commit()

    # ---- LSP TYPE ENRICHMENT (same session, server already warm) ----
    # Query textDocument/hover on the top-N most-referenced nodes to extract
    # return types, parameter types, and exception info. Store in nodes table
    # (signature, return_type columns). This enriches graph.db so the brief
    # and L3 post-edit can deliver precise type contracts to the agent.
    # ONE pipeline: edge verification + type enrichment in the same LSP session.
    enrich_stats = {"hover_ok": 0, "hover_fail": 0, "hover_skip": 0}
    try:
        # Get top-50 most-referenced non-test functions (by incoming edge count)
        _enrich_conn = sqlite3.connect(db_path)
        _enrich_conn.row_factory = sqlite3.Row
        _enrich_conn.execute("PRAGMA journal_mode=WAL")
        _enrich_conn.execute("PRAGMA busy_timeout=5000")
        _top_nodes = _enrich_conn.execute("""
            SELECT n.id, n.name, n.file_path, n.start_line, n.signature, n.return_type,
                   COUNT(e.id) as ref_count
            FROM nodes n
            LEFT JOIN edges e ON e.target_id = n.id
            WHERE n.is_test = 0
              AND n.label IN ('Function', 'Method', 'Class')
              AND n.start_line IS NOT NULL
              AND n.language = ?
            GROUP BY n.id
            ORDER BY ref_count DESC
            LIMIT 50
        """, (language,)).fetchall()

        _enriched = 0
        for node in _top_nodes:
            node_id = node["id"]
            file_path = node["file_path"]
            start_line = node["start_line"]
            name = node["name"]
            existing_sig = node["signature"] or ""
            existing_ret = node["return_type"] or ""

            # Defense-in-depth: skip nodes whose extension doesn't match the
            # current language's LSP server. Even with the SQL n.language filter,
            # an inconsistent language label could send a Go file to pyright.
            _node_ext = os.path.splitext(file_path)[1]
            if _node_ext and _node_ext != ext:
                enrich_stats["hover_skip"] += 1
                continue

            abs_path = os.path.join(abs_root, file_path)
            if not os.path.exists(abs_path):
                enrich_stats["hover_skip"] += 1
                continue

            uri = _path_to_uri(abs_path)

            # Open file if not already opened
            if uri not in opened_files:
                try:
                    with open(abs_path, encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    _node_ext = os.path.splitext(file_path)[1] or ext
                    lang_id = _lang_id_for_ext(_node_ext)
                    await client.did_open(uri, lang_id, 1, text)
                    opened_files.add(uri)
                except Exception:
                    enrich_stats["hover_skip"] += 1
                    continue

            # Find column of the function name on its start line
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                if start_line <= 0 or start_line > len(lines):
                    enrich_stats["hover_skip"] += 1
                    continue
                line_text = lines[start_line - 1]
                col = line_text.find(name)
                if col == -1:
                    col = 0
            except Exception:
                enrich_stats["hover_skip"] += 1
                continue

            # Query hover
            try:
                hover_result = await client.hover(uri, start_line - 1, col, timeout=5.0)
                if isinstance(hover_result, LspErr):
                    enrich_stats["hover_fail"] += 1
                    continue

                hover = hover_result.value
                if hover is None:
                    enrich_stats["hover_fail"] += 1
                    continue

                # Extract hover text
                if hasattr(hover.contents, 'value'):
                    hover_text = hover.contents.value
                elif isinstance(hover.contents, str):
                    hover_text = hover.contents
                elif isinstance(hover.contents, list):
                    hover_text = "\n".join(str(c) for c in hover.contents)
                else:
                    hover_text = str(hover.contents)

                # Parse return type from hover text (language-agnostic patterns)
                _ret_type = ""
                import re as _re_hover
                # Strip markdown fences if present (gopls wraps in ```go ... ```)
                _hover_clean = hover_text
                if "```" in _hover_clean:
                    _hover_clean = _re_hover.sub(r"```\w*\n?", "", _hover_clean).strip()
                # Python/Rust: "def func(...) -> ReturnType" / "fn f() -> T"
                if "->" in _hover_clean:
                    _ret_part = _hover_clean.split("->")[-1].strip()
                    _ret_type = _ret_part.split("\n")[0].strip().rstrip(":")
                # Go: "func Name(...) ReturnType" or "func (r *T) Name(...) (T, error)"
                elif _hover_clean.lstrip().startswith("func ") and ")" in _hover_clean:
                    # For receiver methods "func (r *T) M(x int) RetType",
                    # the FIRST balanced paren is the receiver, not params.
                    # Find the LAST balanced paren group = the parameter list.
                    _paren_depth = 0
                    _param_end = -1
                    for _ci, _ch in enumerate(_hover_clean):
                        if _ch == "(":
                            _paren_depth += 1
                        elif _ch == ")":
                            _paren_depth -= 1
                            if _paren_depth == 0:
                                _param_end = _ci  # keep updating — last one wins
                    if _param_end > 0 and _param_end < len(_hover_clean) - 1:
                        _after = _hover_clean[_param_end + 1:].strip()
                        if _after and not _after.startswith("{"):
                            _ret_type = _after.split("\n")[0].strip()
                # TypeScript/JS: "function name(...): ReturnType"
                elif ": " in _hover_clean and "(" in _hover_clean:
                    _after_colon = _hover_clean.split(")")[-1].strip()
                    if _after_colon.startswith(":"):
                        _ret_type = _after_colon[1:].strip().split("\n")[0].strip()

                # Update node if we found better info than what tree-sitter gave
                _updates = []
                _params = []
                if hover_text and (not existing_sig or len(hover_text) > len(existing_sig)):
                    # D-2: store a SANITIZED signature, NEVER the raw hover markdown. The
                    # brief file-list + EDIT-TARGET contracts read nodes.signature directly,
                    # so a raw ```python\n(method) ...``` hover leaked the fence into the
                    # agent's brief (observed aiogram scene.py 2026-06-05). Extract the
                    # ```code``` block (the signature; Pyright keeps the docstring OUTSIDE
                    # it), drop the leading (method)/(function) hover-kind marker, and
                    # collapse the multi-line signature to one line. Language-agnostic.
                    _m = _re_hover.search(r"```[a-zA-Z]*\s*\n?(.*?)```", hover_text, _re_hover.DOTALL)
                    _sig_clean = (_m.group(1) if _m else _hover_clean).strip()
                    _sig_clean = _re_hover.sub(
                        r"^\((?:method|function|property|variable|class|parameter|field|constant|module|overload)\)\s*",
                        "", _sig_clean,
                    ).strip()
                    _sig_clean = " ".join(_sig_clean.split())
                    if _sig_clean:
                        _updates.append("signature = ?")
                        _params.append(_sig_clean[:500])
                if _ret_type and not existing_ret:
                    _updates.append("return_type = ?")
                    _params.append(_ret_type[:200])

                if _updates:
                    _params.append(node_id)
                    _enrich_conn.execute(
                        f"UPDATE nodes SET {', '.join(_updates)} WHERE id = ?",
                        tuple(_params),
                    )
                    _enriched += 1

                enrich_stats["hover_ok"] += 1

            except Exception:
                enrich_stats["hover_fail"] += 1
                continue

        _enrich_conn.commit()
        _enrich_conn.close()
        print(
            f"  LSP type enrichment: {enrich_stats['hover_ok']} hover OK, "
            f"{enrich_stats['hover_fail']} failed, {enrich_stats['hover_skip']} skipped, "
            f"{_enriched} nodes updated",
            file=sys.stderr,
        )
    except Exception as _enrich_exc:
        print(f"  LSP type enrichment failed (non-fatal): {_enrich_exc}", file=sys.stderr)

    conn.close()

    # Shutdown LSP
    try:
        await client.shutdown()
    except Exception:
        pass

    return stats


def _rebuild_closure(db_path: str) -> None:
    """Recompute the transitive-closure sidecar after the LSP pass mutated edges.

    gt-index owns closure writes (the Go builder applies the RF-4 verified-only
    rules), so we invoke its authoritative ``-rebuild-closure`` mode rather than
    reimplement the BFS in Python. Non-fatal: if the binary is not reachable the
    resolve still succeeded — the closure simply stays as stale as it was before
    this refresh existed (no regression vs. the prior behaviour). Binary is found
    via ``GT_INDEX_BIN`` then ``PATH``.
    """
    import shutil
    import subprocess

    from groundtruth.runtime import proof as _proof

    bin_path = os.environ.get("GT_INDEX_BIN") or shutil.which("gt-index")
    if not bin_path or not os.path.exists(bin_path):
        # PROOF MODE (Stage 2): a stale closure is a partial-operation signal — the
        # closure must rebuild over the LSP-corrected edges or the run fails closed.
        # Outside proof mode: warn + continue (no regression vs prior behaviour).
        _proof.require(False, "closure_binary_present",
                       "gt-index binary not found (set GT_INDEX_BIN) — closure NOT rebuilt; "
                       "it remains pre-LSP stale")
        return
    try:
        r = subprocess.run(
            [bin_path, "-rebuild-closure", "-output", db_path],
            capture_output=True,
            text=True,
            timeout=600,
        )
        line = next(
            (ln for ln in (r.stderr or "").splitlines() if "rebuild-closure:" in ln),
            "",
        )
        if r.returncode == 0:
            # Stamp closure_rebuild_ts so the freshness gate (closure_ts >= lsp_ts)
            # can prove the closure reflects the resolved edges. Substrate-integrity
            # proof (impact/trace), NOT a brief ranking signal (BRIEFING §4).
            _proof.stamp_closure(db_path)
            print(f"[closure] {line.strip() or 'rebuilt over LSP-corrected edges'}")
        else:
            _proof.require(False, "closure_rebuild_ok",
                           f"rc={r.returncode}: {(r.stderr or '')[:200]}")
    except Exception as exc:  # non-fatal outside proof; fail-closed inside
        _proof.require(False, "closure_rebuild_ok", f"{type(exc).__name__}: {exc}")


def resolve_main() -> None:
    """CLI entry point for gt-resolve."""
    parser = argparse.ArgumentParser(
        prog="groundtruth resolve",
        description="Diagnose and resolve ambiguous edges in graph.db using LSP",
    )
    parser.add_argument("--db", required=True, help="Path to graph.db")
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.9,
        help="Show edges below this confidence (default: 0.9)",
    )
    parser.add_argument("--lang", default=None, help="Filter by language")
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="Actually resolve edges via LSP (not just diagnose)",
    )
    parser.add_argument(
        "--max-edges",
        type=int,
        default=500,
        help="Maximum edges to resolve (default: 500)",
    )
    # Demand-driven scoping (Heintze & Tardieu, "Demand-Driven Pointer Analysis,"
    # PLDI 2001): resolve only the issue-relevant subgraph, not the whole repo.
    parser.add_argument(
        "--source-files",
        default=None,
        help=(
            "Restrict resolution to edges from these source files (demand-driven "
            "scoping). Accepts EITHER a comma-separated list of file paths OR a path "
            "to a file containing one source-file path per line. Omit to scan all."
        ),
    )
    # Support both `groundtruth resolve --db ...` and `python -m groundtruth.resolve --db ...`
    if "resolve" in sys.argv:
        _args_list = sys.argv[sys.argv.index("resolve") + 1:]
    else:
        _args_list = sys.argv[1:]
    args = parser.parse_args(_args_list)

    if not os.path.exists(args.db):
        print(f"ERROR: Database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    servers = _detect_servers()
    print(f"Available LSP servers: {', '.join(lang for lang, v in servers.items() if v) or 'none'}")

    # Demand-driven scoping (Heintze & Tardieu, PLDI 2001): if --source-files is a
    # path to an existing file, read one source-file path per line; otherwise treat
    # the value as a comma-separated list. Normalize/strip. None/empty => scan all.
    source_files: list[str] | None = None
    if args.source_files:
        if os.path.isfile(args.source_files):
            with open(args.source_files, encoding="utf-8") as _sf:
                source_files = [line.strip() for line in _sf if line.strip()]
        else:
            source_files = [p.strip() for p in args.source_files.split(",") if p.strip()]
        # Normalize the scope to repo-RELATIVE paths so the `e.source_file IN (...)`
        # filters match. ``edges.source_file`` is stored repo-relative (the resolve
        # path itself relpaths LSP targets against the root, line ~598); an ABSOLUTE
        # scope path (e.g. /tmp/gt/src/pkg/m.py) can never satisfy IN(relative), which
        # silently forces residual=0 / empty-scope on EVERY task regardless of language.
        # Self-correcting: relpath each entry against --root, forward-slash, strip any
        # leading "./" — a no-op for already-relative inputs, a fix for absolute ones.
        if source_files:
            _root = os.path.abspath(args.root)

            def _rel_to_root(p: str) -> str:
                # Only rewrite genuinely ABSOLUTE inputs (the bug class: absolute scope
                # vs repo-relative edges.source_file). Already-relative inputs are left
                # as-is — they are assumed repo-relative, matching edges.source_file.
                if os.path.isabs(p):
                    try:
                        p = os.path.relpath(p, _root)
                    except ValueError:
                        pass
                return p.replace("\\", "/").lstrip("./")

            source_files = [_rel_to_root(p) for p in source_files]

    conn = sqlite3.connect(args.db)
    # Pass --max-edges as the query limit (was hardcoded LIMIT 500). A full resolve
    # now reaches ALL ambiguous edges, so the graph can be fully LSP-cleaned instead
    # of staying name_match-dominated. source_files scopes to the issue subgraph.
    edges = _get_ambiguous_edges(
        conn, args.min_confidence, args.lang, source_files=source_files, limit=args.max_edges
    )
    # Residual = the resolution-fraction DENOMINATOR: count of name_match method-call
    # edges in scope (issue subgraph if --source-files, else whole graph), captured
    # BEFORE _resolve_edges mutates anything. Distinct from len(edges) (which is also
    # capped but not method-specific) so a capped/un-scoped pass is detectable via
    # resolved/residual. CAP-CONSISTENT with --max-edges: the pass can attempt at most
    # max_edges, so a residual larger than that would make the gate ceiling = cap/residual
    # < floor even at 100% success (the un-scoped large-repo "unpassable gate" class).
    # Capping the denominator at the attempt budget makes the floor real work, not a
    # coin-flip against the cap. When demand-scope shrinks residual below the cap, this
    # is a no-op and the fraction is the true in-scope resolution rate.
    residual_method_edges = _count_residual_method_edges(
        conn, args.lang, source_files=source_files, cap=args.max_edges
    )
    conn.close()

    if args.resolve:
        # Live resolution mode
        if not args.lang:
            print("ERROR: --resolve requires --lang (e.g., --lang python)", file=sys.stderr)
            sys.exit(1)

        if not servers.get(args.lang):
            # No LSP server for this language: emit the 0-resolution CONTRACT (not a hard
            # abort) so the gate classifies it (resolved=0/residual -> GATE-2 fails-closed =
            # explicit "unsupported language" proof classification), and the substrate's
            # proof-mode resolve-fatal sees rc=0 instead of aborting before the gates run.
            # Generalized: ANY language with no server gets the same honest contract, never
            # a silent pass and never a hard crash (verifier finding; matches the substrate's
            # documented "unsupported language -> GATE-2 fails-closed" promise).
            _scoped_n0 = len(source_files) if source_files else 0
            print(f"WARN: No LSP server installed for {args.lang} — emitting 0-resolution contract",
                  file=sys.stderr)
            print(
                f"LSP_METRICS resolved=0 residual={residual_method_edges} scoped_source_files={_scoped_n0}",
                flush=True,
            )
            return

        # scoped_source_files: how many files demand-scoped this pass (0 = whole-graph,
        # un-scoped — the blind-500-cap regime that the metric is meant to expose).
        _scoped_n = len(source_files) if source_files else 0

        lang_edges = [e for e in edges if e.get("language") == args.lang][: args.max_edges]
        if not lang_edges:
            print(f"No ambiguous {args.lang} edges to resolve.")
            # Always emit the contract line in --resolve mode so a parser can tell
            # "nothing to resolve" (residual may still be >0 if no name_match METHOD
            # edges were under the confidence threshold) from a crash.
            print(
                f"LSP_METRICS resolved=0 residual={residual_method_edges} "
                f"scoped_source_files={_scoped_n}",
                flush=True,
            )
            return

        print(f"\nResolving {len(lang_edges)} {args.lang} edges via LSP...")
        start = time.time()
        stats = asyncio.run(_resolve_edges(args.db, args.root, lang_edges, args.lang))
        elapsed = time.time() - start

        print(f"\nResults ({elapsed:.1f}s):")
        print(f"  Verified (tree-sitter was correct): {stats.get('verified', 0)}")
        print(f"  Corrected (pointed to wrong target): {stats.get('corrected', 0)}")
        print(f"  Deleted (false positive): {stats.get('deleted', 0)}")
        print(f"  Failed (LSP couldn't resolve): {stats.get('failed', 0)}")
        print(f"  Skipped: {stats.get('skipped', 0)}")

        # Stamp LSP-enrichment completion (the one-pipeline order: index -> LSP ->
        # closure). generate_v1r_brief asserts this stamp exists in proof mode, so a
        # graph scored BEFORE LSP ran fails closed.
        from groundtruth.runtime import proof as _proof
        _proof.stamp_lsp(
            args.db,
            metrics=f"verified={stats.get('verified',0)} corrected={stats.get('corrected',0)} "
                    f"deleted={stats.get('deleted',0)} failed={stats.get('failed',0)}",
        )

        # The LSP pass just promoted/re-pointed/deleted edges. The transitive
        # closure sidecar was built at index time (before this pass), so it is
        # now stale: missing reach via the LSP-verified edges AND retaining reach
        # via edges this pass deleted/re-pointed. Refresh it over the corrected
        # edges so impact/trace/localization see LSP-accurate deep reach.
        _changed = (
            stats.get("corrected", 0)
            + stats.get("deleted", 0)
            + stats.get("verified", 0)
        )
        if _changed:
            _rebuild_closure(args.db)
        else:
            # No edges changed -> the existing closure still matches the edges and is
            # fresh-by-construction. Stamp it so the freshness gate passes (nothing to
            # rebuild is not a stale closure).
            _proof.stamp_closure(args.db)
        # Proof mode: closure must exist and be >= the LSP stamp (rebuilt AFTER LSP).
        _proof.assert_closure_after_lsp(args.db)

        # FINAL machine-parseable contract line (stdout). resolved = edges PROMOTED to
        # resolution_method='lsp' this pass (verified tree-sitter-correct + corrected
        # to the right target). Deletes are removals, NOT promotions, so they are
        # excluded. residual = the name_match method-call denominator captured pre-pass.
        # scoped_source_files exposes whether this was demand-driven (>0) or a blind
        # whole-graph cap (0). The fraction resolved/residual is now measurable, so a
        # capped or un-scoped pass is detectable (resolved << residual).
        resolved_promoted = int(stats.get("verified", 0)) + int(stats.get("corrected", 0))
        print(
            f"LSP_METRICS resolved={resolved_promoted} residual={residual_method_edges} "
            f"scoped_source_files={_scoped_n}",
            flush=True,
        )
    else:
        # Diagnostic mode (default)
        _print_summary(edges, servers, args.min_confidence)


if __name__ == "__main__":
    resolve_main()
