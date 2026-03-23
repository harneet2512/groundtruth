"""groundtruth_references — Exploration reference lookup.

Question: "Where is this symbol defined and where is it used?"
When: During exploration, when agent needs import-resolved usage sites.

Synthesizes:
  - Symbol definition from SymbolStore
  - All reference sites from ImportGraph
  - 1-line usage context per caller
  - Source vs test file classification

Output shape: definition + grouped references (source/test), capped at 15.
"""

from __future__ import annotations

import os
import time
from typing import Any

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.observability.schema import ComponentStatus
from groundtruth.observability.tracer import EndpointTracer, TraceContext
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err, Ok

log = get_logger("endpoints.references")

_MAX_SOURCE_REFS = 10
_MAX_TEST_REFS = 5

_TEST_MARKERS = frozenset({
    "/tests/", "/test/", "/__tests__/", "/testing/",
    "/fixtures/", "/conftest",
})


def _is_test_file(path: str) -> bool:
    """Heuristic: is this a test file?"""
    p = path.lower().replace("\\", "/")
    if any(m in p for m in _TEST_MARKERS):
        return True
    base = os.path.basename(p)
    return base.startswith("test_") or base.endswith("_test.py")


def _read_line(root_path: str, file_path: str, line: int) -> str:
    """Read a single line from disk."""
    full = os.path.join(root_path, file_path)
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f, 1):
                if i == line:
                    return ln.rstrip()
    except OSError:
        pass
    return ""


async def handle_references(
    symbol: str,
    store: SymbolStore,
    graph: ImportGraph,
    root_path: str,
    tracer: EndpointTracer | None = None,
) -> dict[str, Any]:
    """Find where a symbol is defined and all its usage sites.

    Returns definition location + references grouped by source/test.
    """
    _tracer = tracer or EndpointTracer()

    with _tracer.trace(
        "groundtruth_references",
        symbol=symbol,
        input_summary=f"references for {symbol}",
    ) as t:
        return await _run(symbol, store, graph, root_path, t)


async def _run(
    symbol: str,
    store: SymbolStore,
    graph: ImportGraph,
    root_path: str,
    t: TraceContext,
) -> dict[str, Any]:
    # --- Resolve symbol ---
    find_result = store.find_symbol_by_name(symbol)
    if isinstance(find_result, Err):
        t.respond(response_type="error", verdict="NOT_FOUND",
                  output_summary=f"Symbol '{symbol}' not found")
        return {"error": f"Symbol '{symbol}' not found in index"}

    symbols = find_result.value
    if not symbols:
        t.respond(response_type="error", verdict="NOT_FOUND",
                  output_summary=f"Symbol '{symbol}' not found")
        return {"error": f"Symbol '{symbol}' not found in index"}

    sym = symbols[0]
    definition = {
        "name": sym.name,
        "file": sym.file_path,
        "line": sym.line_number,
        "kind": sym.kind,
        "signature": sym.signature,
    }

    t.log_component(
        "symbol_lookup", ComponentStatus.USED,
        output_summary=f"found {sym.name} in {sym.file_path}:{sym.line_number}",
    )

    # --- Find all callers ---
    source_refs: list[dict[str, Any]] = []
    test_refs: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()

    callers_result = graph.find_callers(sym.name)
    if isinstance(callers_result, Ok):
        for ref in callers_result.value:
            key = (ref.file_path, ref.line)
            if key in seen:
                continue
            seen.add(key)

            # Skip self-references (definition file, same line)
            if ref.file_path == sym.file_path and ref.line == sym.line_number:
                continue

            context = ""
            if ref.line is not None:
                context = _read_line(root_path, ref.file_path, ref.line).strip()

            entry = {
                "file": ref.file_path,
                "line": ref.line,
            }
            if context:
                entry["context"] = context

            if _is_test_file(ref.file_path):
                if len(test_refs) < _MAX_TEST_REFS:
                    test_refs.append(entry)
            else:
                if len(source_refs) < _MAX_SOURCE_REFS:
                    source_refs.append(entry)

        total = len(callers_result.value)
        t.log_component(
            "graph_callers", ComponentStatus.USED,
            output_summary=f"{total} total references",
            item_count=total,
        )
    else:
        t.log_component("graph_callers", ComponentStatus.USED,
                        output_summary="0 references", item_count=0)

    # --- Synthesis ---
    total_shown = len(source_refs) + len(test_refs)
    t.synthesize(
        included=["symbol_lookup", "graph_callers"],
        verdict=f"{total_shown} references shown",
    )
    t.respond(
        response_type="references",
        item_count=total_shown,
        verdict="FOUND",
        output_summary=f"{sym.name}: {len(source_refs)} source, {len(test_refs)} test refs",
    )

    return {
        "definition": definition,
        "source_references": source_refs,
        "test_references": test_refs,
        "total_references": len(seen),
    }
