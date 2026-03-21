"""Consolidated MCP tool handlers — 5 high-signal tools.

Each handler merges multiple legacy tools into a single coherent operation,
reducing agent exploration overhead while preserving all capabilities.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from groundtruth.analysis.conventions import Convention, detect_all as detect_conventions
from groundtruth.analysis.edit_site import EditSiteResolver
from groundtruth.analysis.pattern_roles import classify_roles_for_obligation
from groundtruth.analysis.risk_scorer import RiskScorer
from groundtruth.core import flags
from groundtruth.index.freshness import FreshnessChecker, FreshnessLevel
from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.mcp.formatter import EvidenceItem, add_evidence
from groundtruth.policy.abstention import AbstentionPolicy, EmissionLevel, TrustTier
from groundtruth.mcp.tools.legacy_tools import (
    _build_dependency_chain,
    _check_path,
    _read_source_lines,
    handle_check_patch,
    handle_confusions,
    handle_dead_code,
    handle_find_relevant,
    handle_hotspots,
    handle_impact,
    handle_obligations,
    handle_orient,
    handle_scope,
    handle_status,
    handle_symbols,
    handle_trace,
    handle_unused_packages,
)
from groundtruth.policy.test_feedback import TestFeedbackPolicy
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err, Ok
from groundtruth.validators.autocorrect import AutoCorrector
from groundtruth.validators.contradictions import ContradictionDetector
from groundtruth.validators.obligations import ObligationEngine

log = get_logger("mcp.core_tools")


# ---------------------------------------------------------------------------
# 1. groundtruth_impact — "What changes if I modify this?"
# ---------------------------------------------------------------------------


async def handle_consolidated_impact(
    symbol: str,
    store: SymbolStore,
    graph: ImportGraph,
    tracker: InterventionTracker,
    root_path: str,
    max_depth: int = 3,
    file_context: str | None = None,
) -> dict[str, Any]:
    """Consolidated impact: obligations + callers/callees + scope + blast radius.

    Merges: impact + obligations + trace + scope into one call.
    """
    start = time.monotonic_ns()

    # Run the legacy impact handler for callers/callees/blast radius
    impact_result = await handle_impact(
        symbol=symbol,
        store=store,
        graph=graph,
        tracker=tracker,
        root_path=root_path,
        max_depth=max_depth,
    )
    if "error" in impact_result:
        return impact_result

    # Run obligations
    engine = ObligationEngine(store, graph)
    obligations = engine.infer(symbol)

    # Classify pattern roles if we have source context
    obligation_roles: list[dict[str, Any]] = []
    for obl in obligations[:10]:
        obl_dict: dict[str, Any] = {
            "kind": obl.kind,
            "target": obl.target,
            "file": obl.target_file,
            "line": obl.target_line,
            "reason": obl.reason,
            "confidence": obl.confidence,
        }
        # Try to classify role from source if file_context provided
        if file_context and obl.kind == "shared_state":
            try:
                roles = classify_roles_for_obligation(
                    file_context, obl.target, [symbol]
                )
                if roles:
                    obl_dict["pattern_roles"] = roles
            except Exception:
                pass
        obligation_roles.append(obl_dict)

    # Run scope for files needing changes
    scope_result = await handle_scope(
        symbol=symbol,
        store=store,
        graph=graph,
        tracker=tracker,
    )
    scope_files = scope_result.get("files", []) if "error" not in scope_result else []

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)

    # Build evidence
    evidence_items: list[EvidenceItem] = []
    for caller in impact_result.get("direct_callers", []):
        evidence_items.append(EvidenceItem(
            role="calls",
            location=f"{caller['file']}:{caller.get('line', '?')}",
            detail=caller.get("usage", f"calls {symbol}"),
            confidence=0.9,
        ))
    for obl in obligations[:5]:
        evidence_items.append(EvidenceItem(
            role="obligation",
            location=f"{obl.target_file}:{obl.target_line}" if obl.target_line else obl.target_file,
            detail=f"[{obl.kind}] {obl.target}: {obl.reason}",
            confidence=obl.confidence,
        ))

    result: dict[str, Any] = {
        "symbol": impact_result.get("symbol", {}),
        "direct_callers": impact_result.get("direct_callers", []),
        "indirect_dependents": impact_result.get("indirect_dependents", []),
        "impact_summary": impact_result.get("impact_summary", {}),
        "obligations": obligation_roles,
        "obligation_count": len(obligations),
        "scope_files": scope_files[:15],
        "scope_total": len(scope_files),
        "safe_changes": impact_result.get("safe_changes", []),
        "unsafe_changes": impact_result.get("unsafe_changes", []),
        "latency_ms": elapsed_ms,
    }
    return add_evidence(result, evidence_items)


# ---------------------------------------------------------------------------
# 2. groundtruth_references — "Where is this used?"
# ---------------------------------------------------------------------------


async def handle_consolidated_references(
    symbol: str,
    store: SymbolStore,
    graph: ImportGraph,
    tracker: InterventionTracker,
    root_path: str,
    max_results: int = 10,
) -> dict[str, Any]:
    """Consolidated references: definition + all references + canonical edit site.

    Merges: symbols + context + find_relevant into one call.
    """
    start = time.monotonic_ns()

    # Resolve symbol
    resolve_result = store.resolve_symbol(symbol)
    if isinstance(resolve_result, Err):
        return {"error": resolve_result.error.message}

    sym = resolve_result.value
    if sym is None:
        return {"error": f"Symbol '{symbol}' not found in index"}

    symbol_info: dict[str, Any] = {
        "name": sym.name,
        "file": sym.file_path,
        "kind": sym.kind,
        "signature": sym.signature,
        "line_number": sym.line_number,
        "is_exported": sym.is_exported,
        "usage_count": sym.usage_count,
    }

    # Find all references (callers)
    references: list[dict[str, Any]] = []
    callers_result = graph.find_callers(symbol)
    if isinstance(callers_result, Ok):
        for ref in callers_result.value[:max_results]:
            ref_info: dict[str, Any] = {
                "file": ref.file_path,
                "line": ref.line,
            }
            # Read context snippet
            if ref.line is not None:
                try:
                    full_path = Path(root_path) / ref.file_path
                    if full_path.exists():
                        lines = full_path.read_text(encoding="utf-8").splitlines()
                        idx = ref.line - 1
                        snippet: list[str] = []
                        if 0 <= idx < len(lines):
                            snippet.append(lines[idx])
                        ref_info["context"] = "\n".join(snippet)
                except (OSError, UnicodeDecodeError):
                    pass
            references.append(ref_info)

    # Canonical edit site
    edit_site: dict[str, Any] | None = None
    try:
        resolver = EditSiteResolver(store, graph)
        edit_result = resolver.resolve(symbol, max_candidates=3)
        if isinstance(edit_result, Ok) and edit_result.value:
            top = edit_result.value[0]
            edit_site = {
                "file": top.file_path,
                "line": top.line_number,
                "score": round(top.score, 3),
                "reason": top.reason,
                "is_ambiguous": top.is_ambiguous,
            }
    except Exception as exc:
        log.debug("edit_site_resolution_failed", error=str(exc))

    # Imports/importers for the defining file
    imports_from: list[str] = []
    imported_by: list[str] = []
    refs_result = store.get_imports_for_file(sym.file_path)
    if isinstance(refs_result, Ok):
        seen: set[str] = set()
        for ref in refs_result.value:
            sym_r = store.get_symbol_by_id(ref.symbol_id)
            if isinstance(sym_r, Ok) and sym_r.value is not None:
                dep = sym_r.value.file_path
                if dep != sym.file_path and dep not in seen:
                    seen.add(dep)
                    imports_from.append(dep)
    importers_result = store.get_importers_of_file(sym.file_path)
    if isinstance(importers_result, Ok):
        imported_by = [p for p in importers_result.value if p != sym.file_path]

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
    tracker.record(
        tool="groundtruth_references",
        phase="context",
        outcome="valid",
        file_path=sym.file_path,
        latency_ms=elapsed_ms,
    )

    # Evidence
    evidence_items: list[EvidenceItem] = []
    evidence_items.append(EvidenceItem(
        role="defines",
        location=f"{sym.file_path}:{sym.line_number}",
        detail=f"{sym.kind} {sym.name}" + (f" — {sym.signature}" if sym.signature else ""),
        confidence=1.0,
    ))
    for r in references[:3]:
        evidence_items.append(EvidenceItem(
            role="calls",
            location=f"{r['file']}:{r.get('line', '?')}",
            detail=r.get("context", f"references {symbol}"),
            confidence=0.9,
        ))

    result: dict[str, Any] = {
        "symbol": symbol_info,
        "references": references,
        "total_references": len(references),
        "edit_site": edit_site,
        "imports_from": sorted(imports_from),
        "imported_by": sorted(imported_by),
        "latency_ms": elapsed_ms,
    }
    return add_evidence(result, evidence_items)


# ---------------------------------------------------------------------------
# 3. groundtruth_check — "Is my patch complete?"
# ---------------------------------------------------------------------------


async def handle_consolidated_check(
    diff: str,
    autocorrector: AutoCorrector,
    store: SymbolStore,
    graph: ImportGraph,
    tracker: InterventionTracker,
    root_path: str,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Consolidated check: patch corrections + obligations + contradictions + test suggestion.

    Merges: check_patch + validate + confusions + contradictions into one call.
    """
    start = time.monotonic_ns()

    # Run check_patch for autocorrections
    patch_result = await handle_check_patch(
        diff=diff,
        autocorrector=autocorrector,
        tracker=tracker,
    )

    # Run obligations from diff
    engine = ObligationEngine(store, graph)
    obligations = engine.infer_from_patch(diff)

    # Classify pattern roles for obligations where possible
    obligation_out: list[dict[str, Any]] = []
    for obl in obligations[:10]:
        obl_dict: dict[str, Any] = {
            "kind": obl.kind,
            "target": obl.target,
            "file": obl.target_file,
            "line": obl.target_line,
            "reason": obl.reason,
            "confidence": obl.confidence,
        }
        # Try pattern role classification
        if obl.target_file and obl.kind == "shared_state":
            try:
                source_lines = _read_source_lines(root_path, obl.target_file)
                if source_lines:
                    source_text = "\n".join(source_lines)
                    roles = classify_roles_for_obligation(
                        source_text, obl.target, [obl.target]
                    )
                    if roles:
                        obl_dict["pattern_roles"] = roles
            except Exception:
                pass
        obligation_out.append(obl_dict)

    # Run contradiction detection on changed files
    contradictions_out: list[dict[str, Any]] = []
    info_out: list[dict[str, Any]] = []
    detector = ContradictionDetector(store)
    abstention_policy = AbstentionPolicy() if flags.abstention_enabled() else None
    freshness_checker = FreshnessChecker() if flags.abstention_enabled() else None
    # Extract files from diff
    changed_files = _extract_files_from_diff(diff)
    for cf in changed_files[:5]:
        try:
            source_lines = _read_source_lines(root_path, cf)
            if source_lines:
                source_text = "\n".join(source_lines)
                contras = detector.check_file(cf, source_text)
                for c in contras:
                    finding = {
                        "kind": c.kind,
                        "file": c.file_path,
                        "line": c.line,
                        "message": c.message,
                        "evidence": c.evidence,
                        "confidence": c.confidence,
                    }
                    # Apply abstention policy if enabled
                    if abstention_policy and freshness_checker:
                        meta_result = store.get_file_metadata(cf)
                        file_meta = meta_result.value if isinstance(meta_result, Ok) else None
                        indexed_at = file_meta["indexed_at"] if file_meta else None
                        fr = freshness_checker.check_file(
                            os.path.join(root_path, cf), indexed_at
                        )
                        is_stale = fr.level == FreshnessLevel.STALE
                        # Map to trust tier: contradictions have structural evidence
                        trust = TrustTier.YELLOW if is_stale else TrustTier.GREEN
                        emission = abstention_policy.decide(
                            trust=trust,
                            evidence_count=2,  # contradictions always have both sides
                            coverage=5.0,
                            is_stale=is_stale,
                            is_contradiction=True,
                        )
                        if emission == EmissionLevel.EMIT_NOTHING:
                            continue
                        if emission == EmissionLevel.EMIT_SOFT_INFO:
                            finding["emission_level"] = "soft_info"
                            info_out.append(finding)
                            continue
                        finding["emission_level"] = "hard_blocker"
                    contradictions_out.append(finding)
        except Exception:
            pass

    # Test suggestion
    test_suggestion: dict[str, Any] | None = None
    if changed_files:
        try:
            policy = TestFeedbackPolicy(max_test_files=1)
            # Gather available test files from store
            test_files: list[str] = []
            for cf in changed_files:
                dir_path = os.path.dirname(cf)
                # Look for test files in common locations
                for test_prefix in ["tests/", "test/", "tests/unit/"]:
                    sibs = store.get_sibling_files(test_prefix + os.path.basename(cf))
                    if isinstance(sibs, Ok):
                        test_files.extend(
                            f for f in sibs.value
                            if "test_" in f or "/tests/" in f
                        )
            # Also try direct name matching
            all_test_files: list[str] = []
            try:
                cursor = store.connection.execute(
                    "SELECT DISTINCT file_path FROM symbols WHERE file_path LIKE '%test_%'"
                )
                all_test_files = [row["file_path"] for row in cursor.fetchall()]
            except Exception:
                pass

            if all_test_files:
                targets = policy.find_relevant_tests(changed_files, all_test_files)
                if targets:
                    top = targets[0]
                    test_suggestion = {
                        "test_file": top.test_file,
                        "source_file": top.source_file,
                        "match_type": top.match_type,
                        "confidence": top.confidence,
                    }
        except Exception as exc:
            log.debug("test_suggestion_failed", error=str(exc))

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)

    # Build evidence
    evidence_items: list[EvidenceItem] = []
    for corr in patch_result.get("corrections", [])[:3]:
        evidence_items.append(EvidenceItem(
            role="correction",
            location=f"{corr['file']}:{corr.get('line', '?')}",
            detail=f"{corr['old_name']} -> {corr['new_name']} ({corr['check_type']})",
            confidence=corr.get("confidence", 0.8),
        ))
    for obl in obligations[:3]:
        evidence_items.append(EvidenceItem(
            role="obligation",
            location=f"{obl.target_file}:{obl.target_line}" if obl.target_line else obl.target_file,
            detail=f"[{obl.kind}] {obl.reason}",
            confidence=obl.confidence,
        ))

    # Log patterns for accumulated intelligence (when enabled)
    if flags.repo_intel_enabled():
        session_id = os.environ.get("GROUNDTRUTH_RUN_ID")
        for obl in obligations[:10]:
            store.log_pattern(
                pattern_type="obligation",
                subject=obl.source,
                detail={"kind": obl.kind, "target": obl.target, "confidence": obl.confidence},
                session_id=session_id,
            )
        for c_dict in contradictions_out:
            store.log_pattern(
                pattern_type="contradiction",
                subject=c_dict.get("file", ""),
                detail={"kind": c_dict["kind"], "message": c_dict["message"]},
                session_id=session_id,
            )

    result: dict[str, Any] = {
        "corrected_diff": patch_result.get("corrected_diff", diff),
        "corrections": patch_result.get("corrections", []),
        "corrections_total": patch_result.get("total", 0),
        "corrections_by_type": patch_result.get("by_type", {}),
        "obligations": obligation_out,
        "obligation_count": len(obligations),
        "contradictions": contradictions_out,
        "contradiction_count": len(contradictions_out),
        "info": info_out if info_out else [],
        "test_suggestion": test_suggestion,
        "files_checked": patch_result.get("files_checked", 0),
        "latency_ms": elapsed_ms,
    }
    return add_evidence(result, evidence_items)


def _extract_files_from_diff(diff: str) -> list[str]:
    """Extract file paths from a unified diff."""
    import re
    files: list[str] = []
    for match in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE):
        path = match.group(1).strip()
        if path and path != "/dev/null":
            files.append(path)
    return files


# ---------------------------------------------------------------------------
# 4. groundtruth_orient — "Help me understand this codebase"
# ---------------------------------------------------------------------------


async def handle_consolidated_orient(
    store: SymbolStore,
    graph: ImportGraph,
    tracker: InterventionTracker,
    risk_scorer: RiskScorer,
    root_path: str,
    path: str | None = None,
    depth: str = "overview",
) -> dict[str, Any]:
    """Consolidated orient: structure + hotspots + conventions + freshness.

    Merges: orient + status + patterns + explain into one call.
    """
    start = time.monotonic_ns()

    # Run legacy orient for base info
    orient_result = await handle_orient(
        store=store,
        graph=graph,
        tracker=tracker,
        risk_scorer=risk_scorer,
        root_path=root_path,
    )
    if "error" in orient_result:
        return orient_result

    # Add conventions if detailed mode
    conventions_out: list[dict[str, Any]] = []
    if depth == "detailed" and path:
        try:
            source_lines = _read_source_lines(root_path, path)
            if source_lines:
                source_text = "\n".join(source_lines)
                convs = detect_conventions(source_text)
                conventions_out = [
                    {
                        "kind": c.kind,
                        "scope": c.scope,
                        "pattern": c.pattern,
                        "frequency": round(c.frequency, 2),
                        "confidence": round(c.confidence, 2),
                    }
                    for c in convs
                ]
        except Exception as exc:
            log.debug("convention_detection_failed", error=str(exc))

    # Freshness check on entry point files
    freshness_summary: list[dict[str, Any]] = []
    checker = FreshnessChecker()
    entry_points = orient_result.get("entry_points", [])
    for ep in entry_points[:5]:
        try:
            # Get last_indexed_at from store
            syms_result = store.get_symbols_in_file(ep)
            last_indexed: int | None = None
            if isinstance(syms_result, Ok) and syms_result.value:
                last_indexed = syms_result.value[0].last_indexed_at
            full_path = os.path.join(root_path, ep)
            fr = checker.check_file(full_path, last_indexed)
            if fr.level != FreshnessLevel.FRESH:
                freshness_summary.append({
                    "file": ep,
                    "level": fr.level.value,
                    "staleness_seconds": round(fr.staleness_seconds, 0)
                    if fr.staleness_seconds is not None else None,
                })
        except Exception:
            pass

    # Add hotspots in detailed mode
    hotspots: list[dict[str, Any]] = []
    if depth == "detailed":
        hotspot_result = await handle_hotspots(store=store, tracker=tracker, limit=10)
        if "error" not in hotspot_result:
            hotspots = hotspot_result.get("hotspots", [])

    # Status info
    status_result = await handle_status(store=store, tracker=tracker)
    index_health: dict[str, Any] = {}
    if "error" not in status_result:
        index_health = {
            "indexed": status_result.get("indexed", False),
            "symbols_count": status_result.get("symbols_count", 0),
            "languages": status_result.get("languages", []),
        }

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)

    result: dict[str, Any] = {
        "project": orient_result.get("project", {}),
        "structure": orient_result.get("structure", {}),
        "build_commands": orient_result.get("build_commands", {}),
        "entry_points": orient_result.get("entry_points", []),
        "top_modules": orient_result.get("top_modules", []),
        "risk_summary": orient_result.get("risk_summary", []),
        "index_health": index_health,
        "conventions": conventions_out,
        "freshness_warnings": freshness_summary,
        "hotspots": hotspots,
        "latency_ms": elapsed_ms,
    }
    return result


# ---------------------------------------------------------------------------
# 5. groundtruth_search — "Find symbols matching this"
# ---------------------------------------------------------------------------


async def handle_consolidated_search(
    query: str,
    store: SymbolStore,
    graph: ImportGraph,
    tracker: InterventionTracker,
    root_path: str,
    max_results: int = 10,
    include_dead_code: bool = False,
) -> dict[str, Any]:
    """Consolidated search: matching symbols ranked by relevance.

    Merges: find_relevant + dead_code + unused_packages + hotspots into one call.
    """
    start = time.monotonic_ns()

    # FTS5 search
    search_result = store.search_symbols_fts(query, limit=max_results)
    matches: list[dict[str, Any]] = []
    if isinstance(search_result, Ok):
        for sym in search_result.value:
            matches.append({
                "name": sym.name,
                "file": sym.file_path,
                "kind": sym.kind,
                "signature": sym.signature,
                "line_number": sym.line_number,
                "is_exported": sym.is_exported,
                "usage_count": sym.usage_count,
            })

    # If FTS5 returned nothing, try exact name lookup
    if not matches:
        find_result = store.find_symbol_by_name(query)
        if isinstance(find_result, Ok):
            for sym in find_result.value[:max_results]:
                matches.append({
                    "name": sym.name,
                    "file": sym.file_path,
                    "kind": sym.kind,
                    "signature": sym.signature,
                    "line_number": sym.line_number,
                    "is_exported": sym.is_exported,
                    "usage_count": sym.usage_count,
                })

    # Optionally include dead code
    dead_symbols: list[dict[str, Any]] = []
    if include_dead_code:
        dead_result = await handle_dead_code(store=store, tracker=tracker)
        if "error" not in dead_result:
            dead_symbols = dead_result.get("dead_symbols", [])

    # Top hotspots for context
    top_hotspots: list[dict[str, Any]] = []
    hotspot_result = await handle_hotspots(store=store, tracker=tracker, limit=5)
    if "error" not in hotspot_result:
        top_hotspots = hotspot_result.get("hotspots", [])

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
    tracker.record(
        tool="groundtruth_search",
        phase="find_relevant",
        outcome="valid",
        latency_ms=elapsed_ms,
    )

    result: dict[str, Any] = {
        "matches": matches,
        "total_matches": len(matches),
        "top_hotspots": top_hotspots,
        "latency_ms": elapsed_ms,
    }
    if include_dead_code:
        result["dead_symbols"] = dead_symbols
        result["dead_total"] = len(dead_symbols)

    return result
