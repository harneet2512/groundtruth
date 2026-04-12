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

    # Part 2: COMPLETENESS — sibling/peer coupling warnings
    # v6: Check if edited function has peers that share structural patterns.
    # If so, warn about unchecked peers that may need the same fix.
    try:
        from groundtruth_v2.contracts import compute_sibling_pattern

        nodes = reader.get_nodes_in_file(file_path)
        edited_names = {n.name for n in nodes if n.label in ("Function", "Method")}

        for node in nodes:
            if node.label not in ("Function", "Method"):
                continue
            siblings = reader.get_siblings(node.id)
            if not siblings or len(siblings) < 2:
                continue

            sib = compute_sibling_pattern(reader, node.id)

            # Check 1: Return type inconsistency
            if sib and sib.sibling_count >= 2 and sib.return_type_agreement >= 0.8:
                if node.return_type and sib.common_return_type and node.return_type != sib.common_return_type:
                    lines.append(
                        f"INCOMPLETE: {node.name}() returns {node.return_type}"
                        f" but {sib.sibling_count} siblings return {sib.common_return_type}"
                    )

            # Check 2: Peer coupling — if this function was edited, check if
            # structurally similar siblings may need the same change.
            # "Similar" = same return type + similar param count
            if node.name in edited_names:
                similar_peers = []
                for s in siblings:
                    if s.name == node.name:
                        continue
                    # Same return type OR both have no return type
                    type_match = (s.return_type == node.return_type) or (not s.return_type and not node.return_type)
                    if type_match and s.name not in edited_names:
                        similar_peers.append(s.name)
                if similar_peers and len(similar_peers) <= 3:
                    peer_list = ", ".join(similar_peers[:3])
                    lines.append(
                        f"COUPLING: {node.name}() edited — check peer(s) {peer_list} (same pattern in class)"
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
