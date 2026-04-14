"""groundtruth_check — Post-edit structural completeness check.

Question: "After my edits, is this patch structurally complete and correct?"
When: AFTER making edits, before submitting. Optionally iterative.

Synthesizes:
  - Git diff parsing to identify modified files
  - Obligation engine: maps modified classes to all obligation groups
  - AutoCorrect: 7-phase name validation
  - Contradiction detector: override/arity/import conflicts
  - ValidationOrchestrator: import/signature validation
  - Freshness gating, abstention, communication state

Output shape: MISSING OBLIGATION SITES + CORRECTIONS NEEDED + CONTRADICTIONS + STATUS
"""

from __future__ import annotations

import ast
import os
import subprocess
from typing import Any

from groundtruth.index.graph import ImportGraph
from groundtruth.index.graph_store import GraphStore
from groundtruth.index.store import SymbolStore
from groundtruth.observability.schema import ComponentStatus
from groundtruth.observability.tracer import EndpointTracer, TraceContext
from groundtruth.utils.result import Err
from groundtruth.utils.logger import get_logger

log = get_logger("endpoints.check")

_MAX_ISSUES = 10
_CORRECTION_CONFIDENCE_THRESHOLD = 0.7


_SUPPORTED_EXTENSIONS = frozenset(
    {
        ".py",
        ".go",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".cs",
        ".php",
        ".swift",
        ".c",
        ".h",
        ".cpp",
        ".cc",
        ".cxx",
        ".hpp",
        ".rb",
        ".ex",
        ".exs",
        ".lua",
        ".ml",
        ".groovy",
        ".mjs",
        ".cjs",
    }
)


def _should_gate(
    *,
    stale: bool = False,
    ambiguous: bool = False,
    unsupported_language: bool = False,
    evidence_insufficient: bool = False,
    not_checkable: bool = False,
) -> tuple[bool, str]:
    """Canonical confidence gating policy for GT check.

    Returns (should_gate, reason_string) where should_gate=True means the
    checker must abstain from semantic guidance (no hard blocks allowed).

    Gate conditions (any one is sufficient):
    - stale:                  graph is stale for modified files
    - ambiguous:              symbol resolution is ambiguous
    - unsupported_language:   language is not supported for the claimed family
    - evidence_insufficient:  evidence diversity is too weak (possible tier)
    - not_checkable:          contract family is not machine-checkable

    Design rule: these conditions must NEVER lead to a hard gate. They
    downgrade or suppress guidance. They never fabricate signal.
    """
    if stale:
        return True, "index stale for modified files"
    if ambiguous:
        return True, "symbol resolution ambiguous"
    if unsupported_language:
        return True, "language not supported for claimed family"
    if evidence_insufficient:
        return True, "evidence diversity insufficient (possible tier)"
    if not_checkable:
        return True, "contract family not machine-checkable in v1"
    return False, ""


def _get_modified_files(root_path: str) -> list[str]:
    """Get list of modified source files from git diff."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True,
            text=True,
            cwd=root_path,
            timeout=10,
        )
        files = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and os.path.splitext(line)[1].lower() in _SUPPORTED_EXTENSIONS:
                files.append(line)
        return files
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _get_diff_text(root_path: str) -> str:
    """Get unified diff text."""
    try:
        result = subprocess.run(
            ["git", "diff"],
            capture_output=True,
            text=True,
            cwd=root_path,
            timeout=10,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _parse_file(root_path: str, file_path: str) -> ast.Module | None:
    """Parse a Python file, return AST or None."""
    full = os.path.join(root_path, file_path)
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            return ast.parse(f.read(), filename=file_path)
    except (SyntaxError, ValueError, OSError):
        return None


def _extract_classes_from_file(
    tree: ast.Module,
) -> list[dict[str, Any]]:
    """Extract class info: name, methods, init attrs, base classes."""
    classes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        cls_info: dict[str, Any] = {
            "name": node.name,
            "line": node.lineno,
            "bases": [ast.unparse(b) if hasattr(ast, "unparse") else str(b) for b in node.bases],
            "methods": {},
            "init_attrs": set(),
        }
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_attrs: set[str] = set()
                for sub in ast.walk(item):
                    if (
                        isinstance(sub, ast.Attribute)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "self"
                    ):
                        method_attrs.add(sub.attr)
                cls_info["methods"][item.name] = {
                    "line": item.lineno,
                    "attrs": method_attrs,
                }
                if item.name == "__init__":
                    # Only attrs that are WRITTEN in __init__
                    for sub in ast.walk(item):
                        if (
                            isinstance(sub, ast.Assign)
                            and len(sub.targets) == 1
                            and isinstance(sub.targets[0], ast.Attribute)
                            and isinstance(sub.targets[0].value, ast.Name)
                            and sub.targets[0].value.id == "self"
                        ):
                            cls_info["init_attrs"].add(sub.targets[0].attr)
        classes.append(cls_info)
    return classes


def _check_obligations(
    classes: list[dict[str, Any]],
    modified_methods: set[str],
    file_path: str,
) -> list[dict[str, Any]]:
    """Check for missing obligation sites based on shared state.

    If a method was modified and shares self.attrs with another method,
    that other method is an obligation site.
    """
    issues: list[dict[str, Any]] = []
    for cls in classes:
        methods = cls.get("methods", {})

        for mod_name in modified_methods:
            if mod_name not in methods:
                continue
            mod_attrs = methods[mod_name]["attrs"]

            for other_name, other_info in methods.items():
                if other_name == mod_name:
                    continue
                if other_name.startswith("_") and other_name != "__init__":
                    continue

                shared = mod_attrs & other_info["attrs"]
                if len(shared) >= 1:
                    qualified = f"{cls['name']}.{other_name}"
                    if qualified not in modified_methods and other_name not in modified_methods:
                        issues.append(
                            {
                                "kind": "shared_state",
                                "target": qualified,
                                "target_file": file_path,
                                "target_line": other_info["line"],
                                "reason": (
                                    f"shares {', '.join(sorted(shared))} "
                                    f"with modified {cls['name']}.{mod_name}"
                                ),
                                "status": "NOT_MODIFIED",
                            }
                        )
    return issues


def _extract_modified_symbols(diff_text: str) -> set[str]:
    """Extract function/method names from diff hunks."""
    modified: set[str] = set()
    import re

    for line in diff_text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            # Look for def/class definitions in added lines
            m = re.match(r"\+\s*(?:async\s+)?def\s+(\w+)", line)
            if m:
                modified.add(m.group(1))
            m = re.match(r"\+\s*class\s+(\w+)", line)
            if m:
                modified.add(m.group(1))
        # Also check hunk headers for context
        if line.startswith("@@"):
            m = re.search(r"def\s+(\w+)", line)
            if m:
                modified.add(m.group(1))
    return modified


def _extract_changed_files_from_diff(diff_text: str) -> tuple[str, ...]:
    """Extract changed files from unified diff headers."""
    import re

    matches = re.findall(r"^\+\+\+\s+b/(.+)$", diff_text, re.MULTILINE)
    return tuple(dict.fromkeys(m for m in matches if m != "/dev/null"))


def _build_patch_candidate(diff_text: str, modified_symbols: set[str]):
    """Create a PatchCandidate for contract-aware verification."""
    from groundtruth.verification.models import PatchCandidate

    return PatchCandidate(
        task_ref="gt_check",
        candidate_id="working_tree",
        diff=diff_text,
        changed_files=_extract_changed_files_from_diff(diff_text),
        changed_symbols=tuple(sorted(modified_symbols)),
    )


def _collect_symbol_timestamps(
    store: SymbolStore,
    file_paths: list[str],
) -> list[tuple[str, int | None]]:
    """Collect last-indexed timestamps for freshness checks."""
    entries: list[tuple[str, int | None]] = []
    for fp in file_paths:
        result = store.get_symbols_in_file(fp)
        if isinstance(result, Err) or not result.value:
            entries.append((fp, None))
            continue
        last_indexed = max((sym.last_indexed_at for sym in result.value), default=None)
        entries.append((fp, last_indexed))
    return entries


def _extract_runtime_contracts(
    store: SymbolStore,
    root_path: str,
    modified_files: list[str],
    modified_symbols: set[str],
) -> list[Any]:
    """Extract contracts for modified symbols from graph-backed runtime.

    Only returns vNext family contracts (behavioral_assertion, paired_behavior,
    constructor_postcondition, dispatch_registration). Legacy families are
    isolated from the scoring/rejection pipeline.
    """
    if not isinstance(store, GraphStore):
        return []

    try:
        from groundtruth.contracts.engine import VNEXT_CONTRACT_TYPES
        from groundtruth.substrate.graph_reader_impl import GraphStoreReader
        from groundtruth.substrate.service import SubstrateService

        reader = GraphStoreReader(store)
        service = SubstrateService(reader)
        bundle = service.get_contracts(
            tuple(sorted(modified_symbols)),
            changed_files=tuple(modified_files),
            root_path=root_path,
        )
        # Filter to vNext families only — legacy must not drive rejection
        return [c for c in bundle.contracts if c.contract_type in VNEXT_CONTRACT_TYPES]
    except Exception:
        return []


async def handle_check(
    store: SymbolStore,
    graph: ImportGraph,
    root_path: str,
    tracer: EndpointTracer | None = None,
    *,
    obligation_engine: Any | None = None,
    contradiction_detector: Any | None = None,
    autocorrect_engine: Any | None = None,
    freshness_checker: Any | None = None,
    file_path: str | None = None,
    proposed_code: str | None = None,
) -> dict[str, Any]:
    """Check if the current patch is structurally complete and correct.

    Default: reads git diff. Optional: check specific file + code.
    """
    _tracer = tracer or EndpointTracer()

    with _tracer.trace(
        "groundtruth_check",
        file_path=file_path,
        input_summary="patch completeness check",
    ) as t:
        return await _run(
            store,
            graph,
            root_path,
            t,
            obligation_engine=obligation_engine,
            contradiction_detector=contradiction_detector,
            autocorrect_engine=autocorrect_engine,
            freshness_checker=freshness_checker,
            file_path=file_path,
            proposed_code=proposed_code,
        )


async def _run(
    store: SymbolStore,
    graph: ImportGraph,
    root_path: str,
    t: TraceContext,
    *,
    obligation_engine: Any | None = None,
    contradiction_detector: Any | None = None,
    autocorrect_engine: Any | None = None,
    freshness_checker: Any | None = None,
    file_path: str | None = None,
    proposed_code: str | None = None,
) -> dict[str, Any]:

    all_obligations: list[dict[str, Any]] = []
    all_corrections: list[dict[str, Any]] = []
    all_contradictions: list[dict[str, Any]] = []
    contract_warnings: list[dict[str, Any]] = []
    modified_files: list[str] = []

    # --- Get modified files ---
    if file_path and proposed_code:
        modified_files = [file_path]
        diff_text = ""
    else:
        modified_files = _get_modified_files(root_path)
        diff_text = _get_diff_text(root_path)

    if not modified_files:
        t.log_component("diff_parser", ComponentStatus.USED, output_summary="no modified files")
        t.synthesize(included=[], verdict="NO_CHANGES")
        t.respond(
            response_type="patch_check",
            verdict="NO_CHANGES",
            output_summary="No modified source files detected",
        )
        return {"status": "NO_CHANGES", "message": "No modified source files detected."}

    t.log_component(
        "diff_parser",
        ComponentStatus.USED,
        output_summary=f"{len(modified_files)} files modified",
        item_count=len(modified_files),
    )

    modified_symbols = _extract_modified_symbols(diff_text)

    # --- Freshness gate for structural truth ---
    stale_structure = False
    freshness_entries: list[tuple[str, int | None]] = []
    if freshness_checker:
        try:
            from groundtruth.index.freshness import FreshnessLevel

            freshness_entries = _collect_symbol_timestamps(store, modified_files)
            freshness_results = freshness_checker.check_files(freshness_entries)
            worst = freshness_checker.overall_freshness(freshness_results)
            stale_structure = worst == FreshnessLevel.STALE
            t.log_component(
                "freshness",
                ComponentStatus.ABSTAINED if stale_structure else ComponentStatus.USED,
                output_summary=f"{worst.value}: {len(freshness_results)} files checked",
                confidence=0.0 if stale_structure else 1.0,
            )
        except Exception as e:
            t.log_component("freshness", ComponentStatus.FAILED, reason=str(e))
    else:
        t.log_component("freshness", ComponentStatus.SKIPPED, reason="no checker provided")

    # --- AST-based obligation checking per file ---
    #
    # This remains a Python-specific fallback. Prefer graph-backed cross-file
    # contracts when the runtime contract path can already see modified symbols.
    should_run_local_ast = not stale_structure and (
        not modified_symbols or not isinstance(store, GraphStore)
    )
    if should_run_local_ast:
        for fp in modified_files:
            tree = _parse_file(root_path, fp)
            if tree is None:
                continue

            classes = _extract_classes_from_file(tree)
            file_obligations = _check_obligations(classes, modified_symbols, fp)
            all_obligations.extend(file_obligations)

    if stale_structure:
        t.log_component(
            "obligations_local",
            ComponentStatus.ABSTAINED,
            reason="index stale for modified files",
        )
    elif not should_run_local_ast:
        t.log_component(
            "obligations_local",
            ComponentStatus.SKIPPED,
            reason="graph-backed contract path available for modified symbols",
        )
    elif all_obligations:
        t.log_component(
            "obligations_local",
            ComponentStatus.USED,
            output_summary=f"{len(all_obligations)} missing obligation sites",
            item_count=len(all_obligations),
        )
    else:
        t.log_component(
            "obligations_local",
            ComponentStatus.USED,
            output_summary="all obligation sites covered",
        )

    # --- Obligation engine (cross-file, if available) ---
    if stale_structure:
        t.log_component(
            "obligations_cross",
            ComponentStatus.ABSTAINED,
            reason="index stale for modified files",
        )
    elif obligation_engine and diff_text:
        try:
            cross_obs = obligation_engine.infer_from_patch(diff_text)
            if isinstance(cross_obs, list):
                for ob in cross_obs[:_MAX_ISSUES]:
                    all_obligations.append(
                        {
                            "kind": ob.kind,
                            "target": ob.target,
                            "target_file": ob.target_file,
                            "target_line": ob.target_line,
                            "reason": ob.reason,
                            "status": "NOT_MODIFIED",
                        }
                    )
                t.log_component(
                    "obligations_cross",
                    ComponentStatus.USED,
                    output_summary=f"{len(cross_obs)} cross-file obligations",
                    item_count=len(cross_obs),
                )
        except Exception as e:
            t.log_component("obligations_cross", ComponentStatus.FAILED, reason=str(e))
    else:
        t.log_component("obligations_cross", ComponentStatus.SKIPPED, reason="no engine or no diff")

    # --- Contract-aware verification ---
    gate, gate_reason = _should_gate(stale=stale_structure)
    if gate:
        t.log_component(
            "contracts",
            ComponentStatus.ABSTAINED,
            reason=gate_reason,
        )
    elif diff_text and modified_symbols:
        try:
            from groundtruth.substrate.graph_reader_impl import GraphStoreReader
            from groundtruth.substrate.service import SubstrateService

            candidate = _build_patch_candidate(diff_text, modified_symbols)
            contracts = _extract_runtime_contracts(store, root_path, modified_files, modified_symbols)
            if contracts:
                service = SubstrateService(GraphStoreReader(store))
                verdict = service.score_patch(
                    diff_text,
                    tuple(candidate.changed_files),
                    tuple(candidate.changed_symbols),
                    task_ref=candidate.task_ref,
                    candidate_id=candidate.candidate_id,
                    root_path=root_path,
                )
                warnings = (
                    [("hard", msg) for msg in verdict.hard_violations]
                    + [("soft", msg) for msg in verdict.soft_warnings]
                )
                for severity, message in warnings[:_MAX_ISSUES]:
                    contract_warnings.append(
                        {
                            "kind": "patch_verdict",
                            "severity": severity,
                            "predicate": "contract-aware verification",
                            "message": message,
                        }
                    )
                t.log_component(
                    "contracts",
                    ComponentStatus.USED,
                    output_summary=f"{len(contracts)} contracts, {len(warnings)} warnings",
                    item_count=len(warnings),
                )
            else:
                t.log_component(
                    "contracts",
                    ComponentStatus.SKIPPED,
                    reason="no runtime contracts for modified symbols",
                )
        except Exception as e:
            t.log_component("contracts", ComponentStatus.FAILED, reason=str(e))
    else:
        t.log_component("contracts", ComponentStatus.SKIPPED, reason="no diff or no modified symbols")

    # --- Contradiction detector ---
    if contradiction_detector:
        for fp in modified_files:
            full = os.path.join(root_path, fp)
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    source = f.read()
                contras = contradiction_detector.check_file(fp, source)
                if isinstance(contras, list):
                    for c in contras:
                        all_contradictions.append(
                            {
                                "kind": c.kind,
                                "file": c.file_path,
                                "line": c.line,
                                "message": c.message,
                                "confidence": c.confidence,
                            }
                        )
            except Exception:
                pass

        if all_contradictions:
            t.log_component(
                "contradictions",
                ComponentStatus.USED,
                output_summary=f"{len(all_contradictions)} contradictions",
                item_count=len(all_contradictions),
            )
        else:
            t.log_component(
                "contradictions", ComponentStatus.USED, output_summary="no contradictions"
            )
    else:
        t.log_component("contradictions", ComponentStatus.SKIPPED, reason="no detector provided")

    # --- AutoCorrect (name hallucination check) ---
    if autocorrect_engine:
        try:
            corrections = autocorrect_engine.check_files(modified_files, root_path)
            if isinstance(corrections, list):
                for corr in corrections:
                    if corr.get("confidence", 0) >= _CORRECTION_CONFIDENCE_THRESHOLD:
                        all_corrections.append(corr)
                t.log_component(
                    "autocorrect",
                    ComponentStatus.USED,
                    output_summary=f"{len(all_corrections)} corrections (>={_CORRECTION_CONFIDENCE_THRESHOLD})",
                    item_count=len(all_corrections),
                )
        except Exception as e:
            t.log_component("autocorrect", ComponentStatus.FAILED, reason=str(e))
    else:
        t.log_component("autocorrect", ComponentStatus.SKIPPED, reason="no autocorrect engine")

    # --- Determine status ---
    if gate:
        status = "ABSTAIN"
    elif all_obligations or all_corrections or all_contradictions or contract_warnings:
        if all_obligations and not all_corrections and not all_contradictions:
            status = "INCOMPLETE"
        elif all_corrections or all_contradictions or any(
            warning["severity"] == "hard" for warning in contract_warnings
        ):
            status = "NEEDS_FIXES"
        else:
            status = "NEEDS_FIXES"
    else:
        status = "CLEAN"

    # --- Synthesis ---
    included = ["diff_parser", "obligations_local"]
    excluded = []
    exclusion_reasons: dict[str, str] = {}

    if obligation_engine or stale_structure:
        included.append("obligations_cross")
    if contradiction_detector:
        included.append("contradictions")
    if autocorrect_engine:
        included.append("autocorrect")
    included.append("contracts")
    if freshness_checker or stale_structure:
        included.append("freshness")

    t.synthesize(
        included=included,
        excluded=excluded,
        exclusion_reasons=exclusion_reasons,
        verdict=f"{status}: {len(all_obligations)} obligations, "
        f"{len(all_corrections)} corrections, {len(all_contradictions)} contradictions, "
        f"{len(contract_warnings)} contract warnings",
    )

    total_issues = (
        len(all_obligations)
        + len(all_corrections)
        + len(all_contradictions)
        + len(contract_warnings)
    )
    t.respond(
        response_type="patch_check",
        item_count=total_issues,
        verdict=status,
        output_summary=f"{len(modified_files)} files, {total_issues} issues",
    )

    result: dict[str, Any] = {
        "status": status,
        "files_checked": len(modified_files),
        "obligations": all_obligations[:_MAX_ISSUES],
        "corrections": all_corrections[:_MAX_ISSUES],
        "contradictions": all_contradictions[:_MAX_ISSUES],
        "contract_warnings": contract_warnings[:_MAX_ISSUES],
    }

    if stale_structure:
        result["message"] = "Graph-derived structural checks abstained because modified files are stale relative to the index."
        result["freshness"] = [{"file": fp, "last_indexed_at": ts} for fp, ts in freshness_entries]
    elif status == "CLEAN":
        result["message"] = "All obligation sites covered. No corrections needed."

    return result
