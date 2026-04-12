"""Critique — post-edit structural validation via tree-sitter re-parse.

Migrated from groundtruth_v2/critique.py + checker.py. Detects:
1. Signature arity changes that break callers
2. Renamed/removed symbols with stale references
3. Sibling coupling warnings (completeness)
4. Scope warnings (exported symbols imported elsewhere)

Language-agnostic: uses lang_config data for tree-sitter grammar selection.
Returns max 5 formatted CRITIQUE lines.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from groundtruth.substrate.protocols import GraphReader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FuncInfo:
    """A function extracted from tree-sitter re-parse."""
    name: str
    start_line: int
    total_params: int
    required_params: int


@dataclass(frozen=True)
class BreakingChange:
    """A change that will break callers."""
    symbol: str
    description: str
    affected_callers: int
    caller_files: tuple[str, ...]


@dataclass(frozen=True)
class StaleReference:
    """A symbol that was in graph.db but is no longer in the file."""
    old_name: str
    referencing_files: tuple[str, ...]
    reference_count: int


@dataclass(frozen=True)
class CritiqueResult:
    """Complete critique result for a file."""
    file_path: str
    language: str
    breaking_changes: list[BreakingChange]
    stale_references: list[StaleReference]
    new_symbols: list[str]


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def compute_critique(reader: GraphReader, file_path: str) -> list[str]:
    """Run structural checks, return formatted CRITIQUE lines (max 5).

    This is the primary entry point. It runs the tree-sitter checker
    and adds completeness/coupling warnings.
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

    # Part 2: Scope warnings (exported symbols with cross-file dependents)
    if len(lines) < 4:
        scope_warnings = _check_scope(reader, file_path)
        lines.extend(scope_warnings[:5 - len(lines)])

    return lines[:5]


def check_file(reader: GraphReader, file_path: str) -> CritiqueResult | None:
    """Compare a file on disk against graph.db to detect structural breakage.

    Returns None if the file can't be parsed (unknown language, missing grammar).
    Requires tree-sitter — gracefully returns None if unavailable.
    """
    try:
        import tree_sitter
    except ImportError:
        logger.debug("tree-sitter not available — skipping structural check")
        return None

    from groundtruth.verification.lang_config import (
        EXT_TO_LANG,
        LANGUAGE_CONFIGS,
        load_grammar,
    )

    # Determine language
    lang = None
    for ext, l in EXT_TO_LANG.items():
        if file_path.endswith(ext):
            lang = l
            break
    if not lang:
        return None

    config = LANGUAGE_CONFIGS.get(lang)
    if not config:
        return None

    # Load grammar
    grammar_ptr = load_grammar(lang)
    if not grammar_ptr:
        return None

    # Read the file
    abs_path = file_path
    if not os.path.isabs(file_path):
        root = os.environ.get("GT_ROOT", ".")
        abs_path = os.path.join(root, file_path)

    if not os.path.exists(abs_path):
        return None

    with open(abs_path, "rb") as f:
        source = f.read()

    # Parse with tree-sitter
    ts_lang = tree_sitter.Language(grammar_ptr)
    parser = tree_sitter.Parser(ts_lang)
    tree = parser.parse(source)

    # Extract current functions
    current_funcs = _extract_functions(tree.root_node, config, source)

    # Get old functions from graph.db
    old_nodes = reader.get_nodes_in_file(file_path)
    old_funcs = {
        n["name"]: n
        for n in old_nodes
        if n.get("label") in ("Function", "Method")
    }

    current_names = {f.name for f in current_funcs}
    old_names = set(old_funcs.keys())

    breaking: list[BreakingChange] = []
    stale: list[StaleReference] = []
    new_symbols: list[str] = []

    # Check 1: Removed/renamed symbols
    removed = old_names - current_names
    for name in removed:
        node = old_funcs[name]
        node_id = node.get("id")
        if node_id is None:
            continue
        callers = reader.get_callers(node_id)
        # Only flag if deterministic callers exist (confidence > 0.5)
        det_callers = [c for c in callers if (c.get("confidence") or 0) >= 0.5]
        if det_callers:
            caller_files = sorted({c.get("source_file", "") for c in det_callers})
            stale.append(StaleReference(
                old_name=name,
                referencing_files=tuple(caller_files[:10]),
                reference_count=len(det_callers),
            ))

    # Check 2: Signature arity changes
    old_funcs_parsed = _parse_old_version(file_path, config, grammar_ptr)

    for func in current_funcs:
        if func.name not in old_funcs:
            new_symbols.append(func.name)
            continue

        old_node = old_funcs[func.name]

        # Try re-parsed old file first
        old_required: int | None = None
        if old_funcs_parsed and func.name in old_funcs_parsed:
            old_required = old_funcs_parsed[func.name].required_params
        else:
            old_sig = old_node.get("signature", "")
            if old_sig:
                old_required = _crude_param_count(old_sig)
                if old_required == 0:
                    old_required = None

        if old_required is not None and func.required_params > old_required:
            node_id = old_node.get("id")
            if node_id is None:
                continue
            callers = reader.get_callers(node_id)
            det_callers = [c for c in callers if (c.get("confidence") or 0) >= 0.5]
            if det_callers:
                caller_files = sorted({c.get("source_file", "") for c in det_callers})
                breaking.append(BreakingChange(
                    symbol=func.name,
                    description=(
                        f"New required parameter: was {old_required}, "
                        f"now {func.required_params}"
                    ),
                    affected_callers=len(det_callers),
                    caller_files=tuple(caller_files[:10]),
                ))

    return CritiqueResult(
        file_path=file_path,
        language=lang,
        breaking_changes=breaking,
        stale_references=stale,
        new_symbols=new_symbols,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_scope(reader: GraphReader, file_path: str) -> list[str]:
    """Check exported symbols for cross-file dependents."""
    lines: list[str] = []
    nodes = reader.get_nodes_in_file(file_path)

    for node in nodes:
        if node.get("label") not in ("Function", "Method"):
            continue
        if not node.get("is_exported"):
            continue

        node_id = node.get("id")
        if node_id is None:
            continue

        callers = reader.get_callers(node_id)
        # Count callers from other files
        cross_file = [
            c for c in callers
            if c.get("source_file", "") != file_path
            and (c.get("confidence") or 0) >= 0.5
        ]
        if len(cross_file) >= 2:
            dep_files = sorted({c.get("source_file", "") for c in cross_file})[:3]
            lines.append(
                f"SCOPE: {node['name']}() imported by {len(cross_file)} file(s):"
                f" {', '.join(dep_files)}"
            )
            if len(lines) >= 3:
                break

    return lines


def _extract_functions(root, config, source: bytes) -> list[FuncInfo]:
    """Walk tree-sitter AST and extract function definitions with param counts."""
    funcs: list[FuncInfo] = []
    _walk_for_funcs(root, config, source, funcs)
    return funcs


def _walk_for_funcs(node, config, source: bytes, out: list[FuncInfo]) -> None:
    """Recursively walk AST to find function definitions."""
    if node.type in config.func_def_types:
        name_node = node.child_by_field_name("name")
        if name_node:
            name = name_node.text.decode("utf-8", errors="replace")
            total, required = _count_ts_params(node, config)
            out.append(FuncInfo(
                name=name,
                start_line=node.start_point[0] + 1,
                total_params=total,
                required_params=required,
            ))

    for child in node.children:
        _walk_for_funcs(child, config, source, out)


def _count_ts_params(func_node, config) -> tuple[int, int]:
    """Count total and required parameters from a tree-sitter function node."""
    param_list = None
    for child in func_node.children:
        if child.type in config.param_list_types:
            param_list = child
            break

    if not param_list:
        return 0, 0

    total = 0
    required = 0
    for child in param_list.children:
        if child.type in config.param_types:
            if config.self_param and child.type == config.self_param:
                continue
            if (
                config.self_param == "self"
                and child.type == "identifier"
                and child.text == b"self"
            ):
                continue
            total += 1
            if child.type not in config.default_param_types:
                required += 1

    return total, required


def _parse_old_version(
    file_path: str, config, grammar_ptr
) -> dict[str, FuncInfo] | None:
    """Get old version of file via git, parse it, extract functions."""
    import subprocess
    try:
        import tree_sitter
    except ImportError:
        return None

    root = os.environ.get("GT_ROOT", ".")
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{file_path}"],
            capture_output=True,
            cwd=root,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        old_source = result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    ts_lang = tree_sitter.Language(grammar_ptr)
    parser = tree_sitter.Parser(ts_lang)
    tree = parser.parse(old_source)
    funcs = _extract_functions(tree.root_node, config, old_source)
    return {f.name: f for f in funcs}


def _crude_param_count(signature: str) -> int:
    """Crude fallback: count commas between parentheses."""
    start = signature.find("(")
    end = signature.rfind(")")
    if start < 0 or end <= start:
        return 0
    params_str = signature[start + 1:end].strip()
    if not params_str:
        return 0
    params = [p.strip() for p in params_str.split(",")]
    params = [p for p in params if p not in ("self", "cls")]
    required = 0
    for p in params:
        if "=" not in p and "..." not in p and "*" not in p:
            required += 1
    return required
