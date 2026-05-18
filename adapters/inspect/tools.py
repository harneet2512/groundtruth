"""GroundTruth tools exposed as native Inspect AI @tool functions.

Each tool wraps GT's underlying graph.db queries so an Inspect agent can call
them as first-class tool invocations during SWE-bench evaluation.

Requires: GT_GRAPH_DB environment variable pointing to the graph.db file built
by gt-index for the repo under test.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from inspect_ai.tool import tool


def _get_db_path() -> str | None:
    """Resolve graph.db path from GT_GRAPH_DB env var."""
    return os.environ.get("GT_GRAPH_DB")


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection with row_factory."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _no_db_message() -> str:
    return (
        "graph.db not found. Set GT_GRAPH_DB to the path of the graph database "
        "built by gt-index, or ensure on_sample_init ran successfully."
    )


# ---------------------------------------------------------------------------
# groundtruth_brief
# ---------------------------------------------------------------------------


@tool
def groundtruth_brief():
    """Get a pre-edit briefing with callers, callees, and key symbols for a file.

    Use this BEFORE editing a file to understand its contracts, dependencies,
    and high-impact symbols. Helps prevent breaking callers or missing patterns.
    """

    async def run(file_path: str, intent: str = "") -> str:
        """Generate a briefing for a file before editing it.

        Args:
            file_path: Path to the file to get briefing for (relative to repo root).
            intent: Optional description of what you plan to change.

        Returns:
            Briefing with key symbols, callers, callees, and warnings.
        """
        db_path = _get_db_path()
        if not db_path or not os.path.exists(db_path):
            return _no_db_message()

        try:
            conn = _connect(db_path)

            # Get symbols in this file
            cursor = conn.execute(
                "SELECT id, name, label, signature, return_type, start_line, end_line, is_exported "
                "FROM nodes WHERE file_path = ? ORDER BY start_line",
                (file_path,),
            )
            symbols = cursor.fetchall()

            if not symbols:
                # Try suffix match for relative vs absolute path differences
                cursor = conn.execute(
                    "SELECT id, name, label, signature, return_type, start_line, end_line, is_exported "
                    "FROM nodes WHERE file_path LIKE ? ORDER BY start_line",
                    (f"%{file_path}",),
                )
                symbols = cursor.fetchall()

            if not symbols:
                return f"No symbols found for '{file_path}' in graph.db."

            result_parts: list[str] = []
            result_parts.append(f"## Briefing for {file_path}")
            if intent:
                result_parts.append(f"Intent: {intent}")
            result_parts.append(f"\n### Symbols ({len(symbols)} total)")

            for sym in symbols:
                # Count callers (incoming edges)
                caller_count = conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE target_id = ?", (sym["id"],)
                ).fetchone()[0]

                # Count callees (outgoing edges from this symbol's file at this line range)
                callee_count = conn.execute(
                    "SELECT COUNT(*) FROM edges e "
                    "JOIN nodes n ON e.source_id = n.id "
                    "WHERE n.id = ?",
                    (sym["id"],),
                ).fetchone()[0]

                sig = sym["signature"] or sym["name"]
                exported = " [EXPORTED]" if sym["is_exported"] else ""
                impact = ""
                if caller_count >= 5:
                    impact = " **HIGH IMPACT**"
                elif caller_count >= 2:
                    impact = " *moderate impact*"

                result_parts.append(
                    f"- `{sig}` ({sym['label']}, L{sym['start_line']}-{sym['end_line']})"
                    f"{exported}{impact} -- {caller_count} caller(s), {callee_count} callee(s)"
                )

            # Top callers from other files
            all_ids = [s["id"] for s in symbols]
            if all_ids:
                placeholders = ",".join("?" for _ in all_ids)
                caller_files = conn.execute(
                    f"SELECT DISTINCT e.source_file, COUNT(*) as cnt "
                    f"FROM edges e WHERE e.target_id IN ({placeholders}) "
                    f"AND e.source_file IS NOT NULL AND e.source_file != ? "
                    f"GROUP BY e.source_file ORDER BY cnt DESC LIMIT 10",
                    (*all_ids, file_path),
                ).fetchall()

                if caller_files:
                    result_parts.append("\n### External Callers")
                    for cf in caller_files:
                        result_parts.append(f"- {cf['source_file']} ({cf['cnt']} call(s))")

            # Warnings for high-impact exported symbols
            high_impact = [s for s in symbols if s["is_exported"]]
            if high_impact:
                hi_callers = []
                for s in high_impact:
                    cc = conn.execute(
                        "SELECT COUNT(*) FROM edges WHERE target_id = ?", (s["id"],)
                    ).fetchone()[0]
                    if cc >= 3:
                        hi_callers.append((s["name"], cc))
                if hi_callers:
                    result_parts.append("\n### Warnings")
                    for name, cc in hi_callers:
                        result_parts.append(
                            f"- CAUTION: `{name}` has {cc} callers. "
                            f"Changing its signature or return type may break dependents."
                        )

            conn.close()
            return "\n".join(result_parts)

        except sqlite3.Error as exc:
            return f"Database error querying graph.db: {exc}"

    return run


# ---------------------------------------------------------------------------
# groundtruth_trace
# ---------------------------------------------------------------------------


@tool
def groundtruth_trace():
    """Trace callers and callees of a symbol through the call graph.

    Use this to understand who calls a function and what it calls, helping
    you assess the blast radius of changes.
    """

    async def run(
        symbol: str, direction: str = "both", max_results: int = 20
    ) -> str:
        """Trace callers and/or callees of a symbol.

        Args:
            symbol: Name of the function/method/class to trace.
            direction: One of 'callers', 'callees', or 'both'.
            max_results: Maximum number of results per direction.

        Returns:
            List of callers and/or callees with file locations.
        """
        db_path = _get_db_path()
        if not db_path or not os.path.exists(db_path):
            return _no_db_message()

        try:
            conn = _connect(db_path)

            # Find the symbol
            cursor = conn.execute(
                "SELECT id, name, file_path, signature, label FROM nodes WHERE name = ?",
                (symbol,),
            )
            nodes = cursor.fetchall()
            if not nodes:
                return f"Symbol '{symbol}' not found in graph.db."

            node = nodes[0]
            result_parts: list[str] = [
                f"## Trace: {node['name']}",
                f"Defined in: {node['file_path']}",
                f"Signature: {node['signature'] or 'N/A'}",
            ]

            if direction in ("callers", "both"):
                callers = conn.execute(
                    "SELECT e.source_file, e.source_line, e.resolution_method, "
                    "e.confidence, n.name as caller_name "
                    "FROM edges e "
                    "JOIN nodes n ON e.source_id = n.id "
                    "WHERE e.target_id = ? "
                    "ORDER BY e.confidence DESC LIMIT ?",
                    (node["id"], max_results),
                ).fetchall()

                result_parts.append(f"\n### Callers ({len(callers)})")
                for c in callers:
                    conf = f" (confidence={c['confidence']:.1f})" if c["confidence"] else ""
                    result_parts.append(
                        f"- {c['caller_name']} in {c['source_file']}:{c['source_line']}"
                        f" [{c['resolution_method']}]{conf}"
                    )

            if direction in ("callees", "both"):
                callees = conn.execute(
                    "SELECT n.name, n.file_path, e.resolution_method, e.confidence "
                    "FROM edges e "
                    "JOIN nodes n ON e.target_id = n.id "
                    "WHERE e.source_id = ? "
                    "ORDER BY e.confidence DESC LIMIT ?",
                    (node["id"], max_results),
                ).fetchall()

                result_parts.append(f"\n### Callees ({len(callees)})")
                for c in callees:
                    conf = f" (confidence={c['confidence']:.1f})" if c["confidence"] else ""
                    result_parts.append(
                        f"- {c['name']} in {c['file_path']}"
                        f" [{c['resolution_method']}]{conf}"
                    )

            conn.close()
            return "\n".join(result_parts)

        except sqlite3.Error as exc:
            return f"Database error: {exc}"

    return run


# ---------------------------------------------------------------------------
# groundtruth_validate
# ---------------------------------------------------------------------------


@tool
def groundtruth_validate():
    """Validate proposed code against the codebase index.

    Checks that imports, function calls, and symbol references in your proposed
    code actually exist in the indexed codebase.
    """

    async def run(file_path: str, proposed_code: str) -> str:
        """Validate proposed code changes against the graph index.

        Args:
            file_path: Path to the file being edited.
            proposed_code: The proposed new content for the file.

        Returns:
            Validation results with any errors found.
        """
        db_path = _get_db_path()
        if not db_path or not os.path.exists(db_path):
            return _no_db_message()

        try:
            conn = _connect(db_path)
            errors: list[str] = []
            warnings: list[str] = []

            # Get all known symbol names for reference checking
            all_names_cursor = conn.execute("SELECT DISTINCT name FROM nodes")
            known_symbols: set[str] = {row["name"] for row in all_names_cursor.fetchall()}

            # Get symbols currently in this file
            file_symbols = conn.execute(
                "SELECT name, signature, return_type, is_exported FROM nodes "
                "WHERE file_path = ? OR file_path LIKE ?",
                (file_path, f"%{file_path}"),
            ).fetchall()

            # Check for exported symbols that might be removed
            exported_in_file = {s["name"] for s in file_symbols if s["is_exported"]}
            for name in exported_in_file:
                if name not in proposed_code:
                    # Check if it has callers
                    node = conn.execute(
                        "SELECT id FROM nodes WHERE name = ? AND (file_path = ? OR file_path LIKE ?)",
                        (name, file_path, f"%{file_path}"),
                    ).fetchone()
                    if node:
                        caller_count = conn.execute(
                            "SELECT COUNT(*) FROM edges WHERE target_id = ? AND source_file != ?",
                            (node["id"], file_path),
                        ).fetchone()[0]
                        if caller_count > 0:
                            errors.append(
                                f"EXPORTED symbol `{name}` appears to be removed but has "
                                f"{caller_count} external caller(s). This will break dependents."
                            )

            # Check signature changes for exported symbols
            for sym in file_symbols:
                if sym["is_exported"] and sym["signature"] and sym["name"] in proposed_code:
                    node = conn.execute(
                        "SELECT id FROM nodes WHERE name = ? AND (file_path = ? OR file_path LIKE ?)",
                        (sym["name"], file_path, f"%{file_path}"),
                    ).fetchone()
                    if node:
                        caller_count = conn.execute(
                            "SELECT COUNT(*) FROM edges WHERE target_id = ?",
                            (node["id"],),
                        ).fetchone()[0]
                        if caller_count >= 3:
                            warnings.append(
                                f"`{sym['name']}` has {caller_count} callers. "
                                f"Verify signature changes are backward-compatible."
                            )

            conn.close()

            result_parts: list[str] = [f"## Validation: {file_path}"]
            if errors:
                result_parts.append(f"\n### Errors ({len(errors)})")
                for e in errors:
                    result_parts.append(f"- ERROR: {e}")
            if warnings:
                result_parts.append(f"\n### Warnings ({len(warnings)})")
                for w in warnings:
                    result_parts.append(f"- WARNING: {w}")
            if not errors and not warnings:
                result_parts.append("\nNo structural issues detected.")

            result_parts.append(
                f"\nValid: {'No' if errors else 'Yes'} "
                f"({len(errors)} error(s), {len(warnings)} warning(s))"
            )
            return "\n".join(result_parts)

        except sqlite3.Error as exc:
            return f"Database error: {exc}"

    return run


# ---------------------------------------------------------------------------
# groundtruth_hotspots
# ---------------------------------------------------------------------------


@tool
def groundtruth_hotspots():
    """Find the most-referenced symbols in the codebase.

    High-usage symbols have the biggest blast radius if modified incorrectly.
    Check hotspots before making broad changes.
    """

    async def run(limit: int = 20) -> str:
        """Get the most-referenced symbols across the codebase.

        Args:
            limit: Maximum number of hotspots to return.

        Returns:
            Ranked list of symbols by reference count.
        """
        db_path = _get_db_path()
        if not db_path or not os.path.exists(db_path):
            return _no_db_message()

        try:
            conn = _connect(db_path)

            cursor = conn.execute(
                "SELECT n.name, n.file_path, n.label, n.signature, "
                "COUNT(e.id) as usage_count "
                "FROM nodes n "
                "JOIN edges e ON e.target_id = n.id "
                "WHERE n.is_test = 0 "
                "GROUP BY n.id "
                "ORDER BY usage_count DESC LIMIT ?",
                (limit,),
            )
            hotspots = cursor.fetchall()
            conn.close()

            if not hotspots:
                return "No hotspots found (index may be empty)."

            result_parts: list[str] = [f"## Hotspots (top {len(hotspots)} most-referenced symbols)"]
            for i, h in enumerate(hotspots, 1):
                sig = h["signature"] or h["name"]
                result_parts.append(
                    f"{i}. `{sig}` ({h['label']}) in {h['file_path']} "
                    f"-- {h['usage_count']} reference(s)"
                )

            return "\n".join(result_parts)

        except sqlite3.Error as exc:
            return f"Database error: {exc}"

    return run


# ---------------------------------------------------------------------------
# groundtruth_impact
# ---------------------------------------------------------------------------


@tool
def groundtruth_impact():
    """Assess the blast radius of modifying a symbol.

    Shows direct callers, indirect dependents, and break risk. Use before
    changing function signatures or removing symbols.
    """

    async def run(symbol: str) -> str:
        """Assess blast radius of modifying a symbol.

        Args:
            symbol: Name of the function/method/class to analyze.

        Returns:
            Impact analysis with direct callers, indirect dependents, and risk levels.
        """
        db_path = _get_db_path()
        if not db_path or not os.path.exists(db_path):
            return _no_db_message()

        try:
            conn = _connect(db_path)

            nodes = conn.execute(
                "SELECT id, name, file_path, signature FROM nodes WHERE name = ?",
                (symbol,),
            ).fetchall()
            if not nodes:
                return f"Symbol '{symbol}' not found in graph.db."

            node = nodes[0]
            result_parts: list[str] = [
                f"## Impact Analysis: {node['name']}",
                f"File: {node['file_path']}",
                f"Signature: {node['signature'] or 'N/A'}",
            ]

            # Direct callers
            callers = conn.execute(
                "SELECT DISTINCT e.source_file, e.source_line, n.name as caller_name "
                "FROM edges e "
                "JOIN nodes n ON e.source_id = n.id "
                "WHERE e.target_id = ?",
                (node["id"],),
            ).fetchall()

            direct_files = {c["source_file"] for c in callers if c["source_file"]}
            result_parts.append(f"\n### Direct Callers ({len(callers)} in {len(direct_files)} file(s))")
            for c in callers[:20]:
                result_parts.append(
                    f"- {c['caller_name']} in {c['source_file']}:{c['source_line']}"
                )

            # Indirect: callers of callers (2nd-order dependents)
            caller_ids = [
                conn.execute(
                    "SELECT id FROM nodes WHERE name = ? AND file_path = ?",
                    (c["caller_name"], c["source_file"]),
                ).fetchone()
                for c in callers
            ]
            caller_ids = [r["id"] for r in caller_ids if r]

            indirect_files: set[str] = set()
            if caller_ids:
                placeholders = ",".join("?" for _ in caller_ids)
                indirect = conn.execute(
                    f"SELECT DISTINCT source_file FROM edges "
                    f"WHERE target_id IN ({placeholders}) AND source_file IS NOT NULL",
                    caller_ids,
                ).fetchall()
                indirect_files = {r["source_file"] for r in indirect} - direct_files

            total_at_risk = len(direct_files) + len(indirect_files)
            if total_at_risk >= 5:
                level = "HIGH"
            elif total_at_risk >= 2:
                level = "MODERATE"
            else:
                level = "LOW"

            result_parts.append(f"\n### Impact Summary")
            result_parts.append(f"- Impact level: **{level}**")
            result_parts.append(f"- Direct files: {len(direct_files)}")
            result_parts.append(f"- Indirect files: {len(indirect_files)}")
            result_parts.append(f"- Total files at risk: {total_at_risk}")

            if indirect_files:
                result_parts.append("\n### Indirect Dependents")
                for f in sorted(indirect_files)[:10]:
                    result_parts.append(f"- {f}")

            conn.close()
            return "\n".join(result_parts)

        except sqlite3.Error as exc:
            return f"Database error: {exc}"

    return run


# ---------------------------------------------------------------------------
# groundtruth_symbols
# ---------------------------------------------------------------------------


@tool
def groundtruth_symbols():
    """List all symbols (functions, classes, methods) defined in a file.

    Use this to understand the structure of a file before reading or editing it.
    """

    async def run(file_path: str) -> str:
        """List symbols defined in a file.

        Args:
            file_path: Path to the file (relative to repo root).

        Returns:
            List of symbols with their signatures, types, and line ranges.
        """
        db_path = _get_db_path()
        if not db_path or not os.path.exists(db_path):
            return _no_db_message()

        try:
            conn = _connect(db_path)

            cursor = conn.execute(
                "SELECT id, name, label, signature, return_type, start_line, end_line, is_exported "
                "FROM nodes WHERE file_path = ? ORDER BY start_line",
                (file_path,),
            )
            symbols = cursor.fetchall()

            if not symbols:
                # Try suffix match
                cursor = conn.execute(
                    "SELECT id, name, label, signature, return_type, start_line, end_line, is_exported "
                    "FROM nodes WHERE file_path LIKE ? ORDER BY start_line",
                    (f"%{file_path}",),
                )
                symbols = cursor.fetchall()

            if not symbols:
                return f"No symbols found for '{file_path}'."

            # Get import relationships
            all_ids = [s["id"] for s in symbols]
            placeholders = ",".join("?" for _ in all_ids)

            # Files that import from this file
            imported_by = conn.execute(
                f"SELECT DISTINCT e.source_file FROM edges e "
                f"WHERE e.target_id IN ({placeholders}) "
                f"AND e.source_file IS NOT NULL AND e.source_file != ? "
                f"AND e.source_file NOT LIKE ?",
                (*all_ids, file_path, f"%{file_path}"),
            ).fetchall()

            # Files this file imports from
            imports_from = conn.execute(
                "SELECT DISTINCT n.file_path FROM edges e "
                "JOIN nodes n ON e.target_id = n.id "
                "WHERE e.source_file = ? OR e.source_file LIKE ? "
                "AND n.file_path != ? AND n.file_path NOT LIKE ?",
                (file_path, f"%{file_path}", file_path, f"%{file_path}"),
            ).fetchall()

            result_parts: list[str] = [f"## Symbols in {file_path} ({len(symbols)} total)"]

            for sym in symbols:
                usage = conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE target_id = ?", (sym["id"],)
                ).fetchone()[0]
                sig = sym["signature"] or sym["name"]
                exported = " [exported]" if sym["is_exported"] else ""
                ret = f" -> {sym['return_type']}" if sym["return_type"] else ""
                result_parts.append(
                    f"- `{sig}`{ret} ({sym['label']}, L{sym['start_line']}-{sym['end_line']})"
                    f"{exported} [{usage} ref(s)]"
                )

            if imported_by:
                result_parts.append(f"\n### Imported by ({len(imported_by)} file(s))")
                for r in imported_by[:10]:
                    result_parts.append(f"- {r['source_file']}")

            if imports_from:
                result_parts.append(f"\n### Imports from ({len(imports_from)} file(s))")
                for r in imports_from[:10]:
                    result_parts.append(f"- {r['file_path']}")

            conn.close()
            return "\n".join(result_parts)

        except sqlite3.Error as exc:
            return f"Database error: {exc}"

    return run


def gt_tools() -> list:
    """Return all GroundTruth tools for inclusion in an Inspect agent."""
    return [
        groundtruth_brief(),
        groundtruth_trace(),
        groundtruth_validate(),
        groundtruth_hotspots(),
        groundtruth_impact(),
        groundtruth_symbols(),
    ]
