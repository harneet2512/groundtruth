"""Sibling Selector V2 -- ranked, filtered sibling methods from graph.db.

Queries graph.db for siblings (same parent_id class), filters out dunder methods
and trivial methods, then ranks by behavioral similarity (shared symbols,
param count, return type match). Returns top 2 as a structured table.

Feature flag: GT_SIBLING_SELECTOR_V2_ENABLED (env var, default "0" = OFF).
When OFF, all public functions return empty results.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any


# Dunder methods to always filter out
_DUNDER_PATTERN = re.compile(r"^__\w+__$")

# Names to extract from function bodies as "referenced symbols"
_SYMBOL_REF_PATTERN = re.compile(r"\b(?:self\.)?(\w{3,})\b")

# Trivial body indicators: single return/pass/raise statements
_TRIVIAL_BODY_PATTERN = re.compile(
    r"^\s*(?:pass|return\s+\S+|return|raise\s+\w+)\s*$"
)


def _is_enabled() -> bool:
    return os.environ.get("GT_SIBLING_SELECTOR_V2_ENABLED", "0") == "1"


def _is_dunder(name: str) -> bool:
    """Check if a method name is a dunder method."""
    return bool(_DUNDER_PATTERN.match(name))


def _is_property_getter(signature: str | None, body: str) -> bool:
    """Heuristic: property getter has no params beyond self and a single return."""
    if signature and "self" in signature:
        # Count params (rough): split by comma, subtract self
        parts = [p.strip() for p in signature.split(",") if p.strip()]
        non_self = [p for p in parts if "self" not in p]
        if len(non_self) == 0:
            lines = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith("#")]
            if len(lines) <= 2 and any(l.startswith("return self.") for l in lines):
                return True
    return False


def _is_trivial(body: str) -> bool:
    """Check if a method body is trivial (1 non-comment line that is pass/return/raise)."""
    lines = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith("#")]
    # Skip the def line itself
    content_lines = [l for l in lines if not l.startswith("def ")]
    if len(content_lines) <= 1:
        if not content_lines:
            return True
        return bool(_TRIVIAL_BODY_PATTERN.match(content_lines[0]))
    return False


def _extract_referenced_symbols(body: str) -> set[str]:
    """Extract referenced names from a function body for similarity comparison."""
    # Remove string literals to avoid false matches
    cleaned = re.sub(r'["\'].*?["\']', '', body)
    symbols = set(_SYMBOL_REF_PATTERN.findall(cleaned))
    # Remove Python keywords and very common names
    _noise = {
        "self", "None", "True", "False", "return", "raise", "pass", "break",
        "continue", "for", "while", "def", "class", "import", "from", "try",
        "except", "finally", "with", "yield", "async", "await", "not", "and",
        "elif", "else", "lambda",
    }
    return symbols - _noise


def _parse_param_count(signature: str | None) -> int:
    """Count parameters from a signature string, excluding self/cls."""
    if not signature:
        return 0
    # Extract content between parens
    m = re.search(r"\(([^)]*)\)", signature)
    if not m:
        return 0
    params_str = m.group(1)
    if not params_str.strip():
        return 0
    params = [p.strip() for p in params_str.split(",") if p.strip()]
    # Exclude self and cls
    params = [p for p in params if p.split(":")[0].split("=")[0].strip() not in ("self", "cls")]
    return len(params)


def _similarity_score(
    target_symbols: set[str],
    target_param_count: int,
    target_return_type: str | None,
    sibling_symbols: set[str],
    sibling_param_count: int,
    sibling_return_type: str | None,
) -> float:
    """Compute behavioral similarity score between target and sibling."""
    score = 0.0

    # Shared symbols (Jaccard-like, weighted heavily)
    if target_symbols or sibling_symbols:
        union = target_symbols | sibling_symbols
        intersection = target_symbols & sibling_symbols
        if union:
            score += 3.0 * (len(intersection) / len(union))

    # Param count similarity (inverse of absolute difference)
    param_diff = abs(target_param_count - sibling_param_count)
    score += max(0, 1.0 - param_diff * 0.3)

    # Return type match
    if target_return_type and sibling_return_type:
        if target_return_type == sibling_return_type:
            score += 1.0
        # Partial match (e.g. Optional[X] vs X)
        elif target_return_type in sibling_return_type or sibling_return_type in target_return_type:
            score += 0.5

    return score


def select_siblings_v2(
    db_path: str,
    file_path: str,
    func_name: str,
    repo_root: str,
) -> list[dict[str, Any]]:
    """Select top-2 behaviorally similar siblings from the same class.

    Args:
        db_path: Path to graph.db.
        file_path: File containing the target function.
        func_name: Name of the target function.
        repo_root: Repository root for reading source files.

    Returns:
        List of dicts with keys: name, shared_symbols, return_type, score.
        Empty list when feature is disabled.
    """
    if not _is_enabled():
        return []

    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []

    try:
        return _select_siblings_impl(conn, file_path, func_name, repo_root)
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _select_siblings_impl(
    conn: sqlite3.Connection,
    file_path: str,
    func_name: str,
    repo_root: str,
) -> list[dict[str, Any]]:
    """Core implementation."""
    norm = _normalize_path(file_path)

    # Find the target node -- try exact match first, then normalized suffix
    cursor = conn.execute(
        "SELECT id, parent_id, signature, return_type, start_line, end_line "
        "FROM nodes WHERE name = ? AND file_path = ?",
        (func_name, file_path),
    )
    target = cursor.fetchone()
    if not target:
        cursor = conn.execute(
            "SELECT id, parent_id, signature, return_type, start_line, end_line "
            "FROM nodes WHERE name = ? AND file_path LIKE ?",
            (func_name, f"%{norm}"),
        )
        target = cursor.fetchone()
    if not target:
        # Try basename-only match (Windows backslash paths)
        basename = os.path.basename(file_path)
        cursor = conn.execute(
            "SELECT id, parent_id, signature, return_type, start_line, end_line "
            "FROM nodes WHERE name = ? AND file_path LIKE ?",
            (func_name, f"%{basename}"),
        )
        target = cursor.fetchone()
    if not target or target["parent_id"] is None:
        return []

    parent_id = target["parent_id"]

    # Find all siblings (same parent_id, different name)
    cursor = conn.execute(
        "SELECT id, name, signature, return_type, start_line, end_line, file_path "
        "FROM nodes WHERE parent_id = ? AND name != ?",
        (parent_id, func_name),
    )
    siblings = cursor.fetchall()

    if not siblings:
        return []

    # Read source file for body extraction
    source = ""
    candidates = [file_path]
    if repo_root:
        candidates.insert(0, os.path.join(repo_root, file_path))
    # Also try the file_path stored in DB for the target node
    stored_path_cursor = conn.execute("SELECT file_path FROM nodes WHERE id = ?", (target["id"],))
    stored_row = stored_path_cursor.fetchone()
    if stored_row and stored_row["file_path"] not in candidates:
        candidates.append(stored_row["file_path"])

    for candidate in candidates:
        try:
            with open(candidate, "r", errors="replace") as f:
                source = f.read()
            break
        except OSError:
            continue

    source_lines = source.splitlines() if source else []

    # Extract target function body and symbols
    target_body = _extract_body(source_lines, target["start_line"], target["end_line"])
    target_symbols = _extract_referenced_symbols(target_body)
    target_param_count = _parse_param_count(target["signature"])
    target_return_type = target["return_type"]

    # Score and filter siblings
    scored: list[tuple[float, dict[str, Any], set[str]]] = []

    for sib in siblings:
        sib_name = sib["name"]

        # Filter: dunder methods
        if _is_dunder(sib_name):
            continue

        sib_body = _extract_body(source_lines, sib["start_line"], sib["end_line"])

        # Filter: trivial methods
        if sib_body and _is_trivial(sib_body):
            continue

        # Filter: property getters
        if sib_body and _is_property_getter(sib["signature"], sib_body):
            continue

        sib_symbols = _extract_referenced_symbols(sib_body)
        sib_param_count = _parse_param_count(sib["signature"])
        sib_return_type = sib["return_type"]

        score = _similarity_score(
            target_symbols, target_param_count, target_return_type,
            sib_symbols, sib_param_count, sib_return_type,
        )

        shared = target_symbols & sib_symbols
        scored.append((score, {
            "name": sib_name,
            "shared_symbols": sorted(shared),
            "return_type": sib_return_type or "",
        }, shared))

    # Sort by score descending, take top 2
    scored.sort(key=lambda x: x[0], reverse=True)
    results: list[dict[str, Any]] = []
    for score_val, info, shared in scored[:2]:
        info["score"] = round(score_val, 2)
        results.append(info)

    return results


def _extract_body(lines: list[str], start: int | None, end: int | None) -> str:
    """Extract function body from source lines (1-based line numbers)."""
    if not lines or start is None or end is None:
        return ""
    # Clamp to valid range
    s = max(0, start - 1)
    e = min(len(lines), end)
    return "\n".join(lines[s:e])


def format_sibling_table(siblings: list[dict[str, Any]]) -> str:
    """Format siblings as a structured table.

    Returns empty string when feature is disabled or no siblings.
    """
    if not _is_enabled() or not siblings:
        return ""

    lines = ["[SIBLING PATTERN]"]
    lines.append("  | name | shared symbols | return |")
    lines.append("  |------|----------------|--------|")
    for sib in siblings:
        name = sib["name"]
        shared = ", ".join(sib.get("shared_symbols", [])[:5])  # limit display
        ret = sib.get("return_type", "")
        lines.append(f"  | {name} | {shared} | {ret} |")

    return "\n".join(lines)
