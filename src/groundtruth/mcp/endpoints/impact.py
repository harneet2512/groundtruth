"""groundtruth_impact — Pre-edit structural judgment.

Question: "Before I modify this symbol, what are the structural consequences?"
When: BEFORE making changes, after identifying the symbol to modify.

Synthesizes:
  - ImportGraph callers with break_risk
  - Obligation engine (constructor_symmetry, shared_state, override_contract)
  - Contracts (behavioral constraints)
  - Freshness gating, abstention, trust

Output shape: decision-oriented — obligations, callers at risk, safe/unsafe changes.
"""

from __future__ import annotations

import time
from typing import Any

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.observability.schema import ComponentStatus
from groundtruth.observability.tracer import EndpointTracer, TraceContext
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err, Ok

log = get_logger("endpoints.impact")

# Caps
_MAX_CALLERS = 10
_MAX_OBLIGATIONS = 10


def _read_line(root_path: str, file_path: str, line: int) -> str:
    """Read a single line from disk. Returns empty string on failure."""
    import os

    full = os.path.join(root_path, file_path)
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f, 1):
                if i == line:
                    return ln.rstrip()
    except OSError:
        pass
    return ""


def _detect_call_style(symbol_name: str, usage_line: str) -> tuple[str, str]:
    """Detect call style and break risk from a usage line.

    Returns (call_style, break_risk).
    """
    if f"{symbol_name}(" in usage_line:
        after_open = usage_line.split(f"{symbol_name}(", 1)[-1].split(")", 1)[0]
        if "=" in after_open:
            return "keyword", "MODERATE"
        return "positional", "HIGH"
    if symbol_name in usage_line:
        return "reference", "LOW"
    return "unknown", "MODERATE"


async def handle_impact(
    symbol: str,
    store: SymbolStore,
    graph: ImportGraph,
    root_path: str,
    tracer: EndpointTracer | None = None,
    *,
    obligation_engine: Any | None = None,
    freshness_checker: Any | None = None,
    abstention_policy: Any | None = None,
) -> dict[str, Any]:
    """Assess blast radius of modifying a symbol.

    Returns a decision-oriented impact analysis with obligations,
    callers at risk, and safe/unsafe change guidance.
    """
    _tracer = tracer or EndpointTracer()

    with _tracer.trace(
        "groundtruth_impact",
        symbol=symbol,
        input_summary=f"impact analysis for {symbol}",
    ) as t:
        return await _run(
            symbol,
            store,
            graph,
            root_path,
            t,
            obligation_engine=obligation_engine,
            freshness_checker=freshness_checker,
            abstention_policy=abstention_policy,
        )


async def _run(
    symbol: str,
    store: SymbolStore,
    graph: ImportGraph,
    root_path: str,
    t: TraceContext,
    *,
    obligation_engine: Any | None = None,
    freshness_checker: Any | None = None,
    abstention_policy: Any | None = None,
) -> dict[str, Any]:
    start = time.monotonic()

    # --- Resolve symbol ---
    find_result = store.find_symbol_by_name(symbol)
    if isinstance(find_result, Err):
        t.respond(
            response_type="error",
            verdict="NOT_FOUND",
            output_summary=f"Symbol '{symbol}' not found",
        )
        return {"error": f"Symbol '{symbol}' not found in index"}

    symbols = find_result.value
    if not symbols:
        t.respond(
            response_type="error",
            verdict="NOT_FOUND",
            output_summary=f"Symbol '{symbol}' not found",
        )
        return {"error": f"Symbol '{symbol}' not found in index"}

    sym = symbols[0]

    # --- Freshness check ---
    is_stale = False
    if freshness_checker:
        try:
            from groundtruth.index.freshness import FreshnessLevel

            result = freshness_checker.check_file(
                sym.file_path,
                getattr(sym, "last_indexed_at", None),
            )
            is_stale = result.level == FreshnessLevel.STALE
            t.log_component(
                "freshness",
                ComponentStatus.USED,
                output_summary=f"{result.level.value} ({result.staleness_seconds:.0f}s)",
                confidence=0.0 if is_stale else 1.0,
            )
        except Exception as e:
            t.log_component("freshness", ComponentStatus.FAILED, reason=str(e))
    else:
        t.log_component("freshness", ComponentStatus.SKIPPED, reason="no checker provided")

    # --- Graph callers ---
    direct_callers: list[dict[str, Any]] = []
    direct_caller_files: set[str] = set()
    callers_result = graph.find_callers(sym.name)
    if isinstance(callers_result, Ok):
        for ref in callers_result.value[:_MAX_CALLERS]:
            direct_caller_files.add(ref.file_path)
            usage_line = ""
            if ref.line is not None:
                usage_line = _read_line(root_path, ref.file_path, ref.line)

            call_style, break_risk = _detect_call_style(sym.name, usage_line)
            caller_entry: dict[str, Any] = {
                "file": ref.file_path,
                "line": ref.line,
                "call_style": call_style,
                "break_risk": break_risk,
            }
            if usage_line:
                caller_entry["usage"] = usage_line.strip()
            direct_callers.append(caller_entry)

        t.log_component(
            "graph_callers",
            ComponentStatus.USED,
            output_summary=f"{len(callers_result.value)} callers found",
            item_count=len(callers_result.value),
            duration_ms=(time.monotonic() - start) * 1000,
        )
    else:
        t.log_component(
            "graph_callers", ComponentStatus.USED, output_summary="0 callers", item_count=0
        )

    # --- Indirect dependents ---
    indirect_files: list[str] = []
    impact_result = graph.get_impact_radius(sym.name)
    if isinstance(impact_result, Ok):
        indirect_files = [
            f for f in impact_result.value.impacted_files if f not in direct_caller_files
        ]

    # --- Obligations ---
    obligations: list[dict[str, Any]] = []
    if obligation_engine and not is_stale:
        try:
            obs = obligation_engine.infer(symbol, sym.file_path)
            if isinstance(obs, list):
                for ob in obs[:_MAX_OBLIGATIONS]:
                    obligations.append(
                        {
                            "kind": ob.kind,
                            "target": ob.target,
                            "target_file": ob.target_file,
                            "target_line": ob.target_line,
                            "reason": ob.reason,
                            "confidence": ob.confidence,
                        }
                    )
                t.log_component(
                    "obligations",
                    ComponentStatus.USED,
                    output_summary=f"{len(obligations)} obligations found",
                    item_count=len(obligations),
                )
            else:
                t.log_component(
                    "obligations",
                    ComponentStatus.USED,
                    output_summary="0 obligations",
                    item_count=0,
                )
        except Exception as e:
            t.log_component("obligations", ComponentStatus.FAILED, reason=str(e))
    elif is_stale:
        t.log_component(
            "obligations", ComponentStatus.ABSTAINED, reason="index stale for symbol file"
        )
    else:
        t.log_component(
            "obligations", ComponentStatus.SKIPPED, reason="no obligation engine provided"
        )

    # --- Abstention check ---
    total_at_risk = len(direct_caller_files) + len(indirect_files)
    if total_at_risk == 0 and not obligations:
        # Nothing to report
        if abstention_policy:
            t.log_component(
                "abstention",
                ComponentStatus.USED,
                output_summary="EMIT_NOTHING: 0 callers, 0 obligations",
            )
        t.synthesize(
            included=[],
            excluded=["graph_callers", "obligations"],
            exclusion_reasons={"all": "no callers or obligations found"},
            verdict="NO_IMPACT",
        )
        t.respond(
            response_type="impact_analysis",
            verdict="NO_IMPACT",
            item_count=0,
            output_summary=f"{symbol}: no callers, no obligations",
        )
        return {
            "symbol": {"name": sym.name, "file": sym.file_path, "signature": sym.signature},
            "impact_level": "NONE",
            "direct_callers": [],
            "indirect_dependents": 0,
            "obligations": [],
            "message": f"'{symbol}' has no callers and no structural obligations.",
        }

    # --- Impact level ---
    if total_at_risk >= 5:
        impact_level = "HIGH"
    elif total_at_risk >= 2:
        impact_level = "MODERATE"
    else:
        impact_level = "LOW"

    # --- Synthesis ---
    included = ["graph_callers"]
    excluded = []
    exclusion_reasons: dict[str, str] = {}
    if obligations:
        included.append("obligations")
    elif is_stale:
        excluded.append("obligations")
        exclusion_reasons["obligations"] = "index stale"

    t.synthesize(
        included=included,
        excluded=excluded,
        exclusion_reasons=exclusion_reasons,
        verdict=f"{impact_level}: {total_at_risk} files at risk, {len(obligations)} obligations",
    )

    result: dict[str, Any] = {
        "symbol": {"name": sym.name, "file": sym.file_path, "signature": sym.signature},
        "impact_level": impact_level,
        "direct_callers": direct_callers,
        "indirect_dependents": len(indirect_files),
        "obligations": obligations,
        "safe_changes": [
            "Add optional parameter with default value",
            "Change internal logic without altering inputs/outputs",
        ],
        "unsafe_changes": [
            "Add required parameter",
            "Remove parameter",
            "Change return type",
            "Rename the symbol",
        ],
    }

    t.respond(
        response_type="impact_analysis",
        item_count=len(direct_callers) + len(obligations),
        verdict=impact_level,
        output_summary=(
            f"{len(direct_callers)} callers, {len(indirect_files)} indirect, "
            f"{len(obligations)} obligations"
        ),
    )

    return result
