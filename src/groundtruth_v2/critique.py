"""critique.py — Unified post-edit CRITIQUE: checker + completeness.

v1.0.4: Runs after every meaningful edit to detect structural breakage.
Returns formatted CRITIQUE lines (max 5) for injection.
"""

from __future__ import annotations

from groundtruth_v2.graph import GraphReader
from groundtruth_v2.checker import check_file


def compute_critique(reader: GraphReader, file_path: str) -> list[str]:
    """Run structural checks, return formatted CRITIQUE lines (max 5).

    Calls checker.py for arity changes and removed symbols.
    Adds COMPLETENESS warnings for siblings sharing patterns.
    """
    lines: list[str] = []

    # Part 1: Structural check (arity changes, removed symbols)
    result = check_file(reader, file_path)
    if result:
        for bc in result.breaking_changes[:3]:
            caller_list = ", ".join(bc.caller_files[:3])
            lines.append(
                f"BREAKING: {bc.symbol}() — {bc.description};"
                f" {bc.affected_callers} caller(s) in {caller_list}"
            )
        for sr in result.stale_references[:2]:
            ref_list = ", ".join(sr.referencing_files[:3])
            lines.append(
                f"STALE: {sr.old_name}() removed;"
                f" {sr.reference_count} reference(s) in {ref_list}"
            )

    # Part 2: COMPLETENESS — sibling pattern warnings
    # If an edited function shares patterns with siblings and the edit
    # breaks the shared pattern, warn about specific siblings.
    if result and not result.breaking_changes:
        # Only check completeness if no breaking changes (avoid noise)
        try:
            from groundtruth_v2.contracts import compute_sibling_pattern

            nodes = reader.get_nodes_in_file(file_path)
            for node in nodes:
                if node.label not in ("Function", "Method"):
                    continue
                sib = compute_sibling_pattern(reader, node.id)
                if sib and sib.sibling_count >= 2 and sib.return_type_agreement >= 0.8:
                    # Check if this function's return type differs from the common one
                    if node.return_type and sib.common_return_type and node.return_type != sib.common_return_type:
                        lines.append(
                            f"INCOMPLETE: {node.name}() returns {node.return_type}"
                            f" but {sib.sibling_count} siblings return {sib.common_return_type}"
                        )
        except Exception:
            pass  # Sibling analysis is additive

    # Part 3: COMPLETENESS — import dependent warnings
    # If a changed symbol is imported by other untouched files, warn.
    if result and len(lines) < 4:
        try:
            nodes = reader.get_nodes_in_file(file_path)
            for node in nodes:
                if node.label not in ("Function", "Method") or not node.is_exported:
                    continue
                # Find files that import this symbol
                dependents = reader._conn.execute(
                    "SELECT DISTINCT e.source_file FROM edges e "
                    "WHERE e.target_id = ? AND e.type = 'CALLS' "
                    "AND e.source_file != ? AND e.resolution_method = 'import'",
                    (node.id, file_path),
                ).fetchall()
                if len(dependents) >= 2:
                    dep_files = [r[0] for r in dependents[:3]]
                    lines.append(
                        f"SCOPE: {node.name}() imported by {len(dependents)} file(s):"
                        f" {', '.join(dep_files)}"
                    )
                    if len(lines) >= 5:
                        break
        except Exception:
            pass

    return lines[:5]
