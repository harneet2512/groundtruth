"""Primary SDK entrypoint."""

from __future__ import annotations

import os
from collections import deque
from functools import cached_property
from typing import Any, Dict, Literal, Mapping, MutableSet, cast

from groundtruth.exceptions import GraphNotFoundError, SymbolNotFoundError
from groundtruth.filters import filter_edges, is_deterministic
from groundtruth.formatters import format_briefing, format_context as format_context_markup
from groundtruth.graph import GraphStore
from groundtruth.models import (
    AffectedSymbol,
    Briefing,
    Caller,
    Confidence,
    ContextResult,
    Direction,
    Impact,
    ResolutionMethod,
)

FormatChoice = Literal["markdown", "xml", "plain"]


class GroundTruth:
    """Deterministic read-path API over ``graph.db``."""

    def __init__(self, db_path: str, *, read_only: bool | None = None) -> None:
        """Open ``db_path`` with schema validation.

        Args:
            db_path: Path to SQLite graph (``nodes`` / ``edges``).
            read_only: Force read-only mode (`False` for ``:memory:`` debugging).

        Raises:
            GraphNotFoundError: File missing/not regular (non-memory paths).
            SchemaVersionError: Schema mismatch detected by ``GraphStore``.
        """
        if db_path != ":memory:" and not os.path.isfile(db_path):
            raise GraphNotFoundError(f"Graph database not found or not a file: {db_path!r}")

        ro = False if db_path == ":memory:" else (True if read_only is None else read_only)
        self._db_path = db_path
        self._store = GraphStore(db_path, read_only=ro)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _symbol_label(node: Mapping[str, Any]) -> str:
        qualified = node.get("qualified_name")
        if isinstance(qualified, str) and qualified.strip():
            return qualified
        return str(node["name"])

    @staticmethod
    def _confidence_from_methods(methods: list[str]) -> Confidence:
        if not methods:
            return "high"
        if all(is_deterministic(m) for m in methods):
            return "high"
        if any(is_deterministic(m) for m in methods):
            return "medium"
        return "low"

    @cached_property
    def graph(self) -> GraphStore:
        """Underyling ``GraphStore`` instance."""
        return self._store

    def close(self) -> None:
        """Release SQLite handles."""
        self._store.close()

    # ---------------------------------------------------------------- briefing
    def briefing(self, symbol: str, *, family: str = "TARGET", max_results: int = 10) -> Briefing:
        """Summarize deterministic-first callers for nodes matching ``symbol``.

        Raises:
            SymbolNotFoundError: No indexed symbol matches ``symbol``.
        """
        nodes = self._store.find_symbol(symbol)
        if not nodes:
            raise SymbolNotFoundError(f"No symbol match for query: {symbol!r}")
        primary = sorted(nodes, key=lambda r: int(r["id"]))[0]

        _ = family  # placeholder for ALSO/SCOPE routing in future indexer packs

        aggregated: Dict[tuple[str, str, str, int], dict[str, Any]] = {}
        for node in nodes:
            nid = int(node["id"])
            for edge in filter_edges(self._store.callers_of(nid), deterministic_only=False):
                key = (
                    str(edge.get("caller_name") or ""),
                    str(edge.get("caller_file") or ""),
                    str(edge.get("resolution_method") or "name_match"),
                    int(edge.get("source_line") or 0),
                )
                aggregated[key] = edge

        ordered = sorted(
            aggregated.values(),
            key=lambda edge: (str(edge.get("caller_file")), int(edge.get("source_line") or 0)),
        )[:max_results]

        callers: list[Caller] = []
        method_names: list[str] = []
        for edge in ordered:
            method = cast(ResolutionMethod, str(edge.get("resolution_method") or "name_match"))
            method_names.append(method)
            callers.append(
                Caller(
                    symbol=str(edge.get("caller_name") or ""),
                    file=str(edge.get("caller_file") or ""),
                    line=int(edge.get("source_line") or 0),
                    resolution_method=method,
                    is_deterministic=is_deterministic(method),
                )
            )

        confidence = self._confidence_from_methods(method_names)
        base = Briefing(
            symbol=self._symbol_label(primary),
            file=str(primary.get("file_path") or ""),
            callers=callers,
            behaviors=[],
            rules=[],
            evidence_text="",
            confidence=confidence,
        )

        narrative = format_briefing(base, "markdown").rstrip() + "\n"
        return Briefing(
            symbol=base.symbol,
            file=base.file,
            callers=callers,
            behaviors=[],
            rules=[],
            evidence_text=narrative,
            confidence=confidence,
        )

    # ---------------------------------------------------------------- check
    def check(self, file: str, *, diff: str | None = None) -> Impact:
        """File-level rollup of callers reachable from symbols defined in ``file``.

        Raises:
            FileNotFoundError: Path not present in indexed ``nodes``.
        """
        symbols = self._store.symbols_in_file(file)
        if not symbols:
            raise FileNotFoundError(f"No indexed symbols for file: {file!r}")

        affected: Dict[tuple[str, str, str], AffectedSymbol] = {}
        breaking: Dict[tuple[str, str, int, str], Caller] = {}
        direct_locations: MutableSet[tuple[str, str]] = set()

        for node in symbols:
            nid = int(node["id"])
            callee_label = self._symbol_label(node)

            for edge in filter_edges(self._store.callers_of(nid), deterministic_only=False):
                caller_symbol = str(edge.get("caller_name") or "")
                caller_fp = str(edge.get("caller_file") or "")
                method = cast(ResolutionMethod, str(edge.get("resolution_method") or "name_match"))
                line = int(edge.get("source_line") or 0)
                direct_locations.add((caller_symbol, caller_fp))

                caller_model = Caller(
                    symbol=caller_symbol,
                    file=caller_fp,
                    line=line,
                    resolution_method=method,
                    is_deterministic=is_deterministic(method),
                )
                affected[(caller_symbol, caller_fp, "direct_caller")] = AffectedSymbol(
                    symbol=caller_symbol,
                    file=caller_fp,
                    relationship="direct_caller",
                )
                if method == "name_match":
                    breaking[(caller_symbol, caller_fp, line, method)] = caller_model

            for label in self._store.ego(
                callee_label,
                depth=2,
                deterministic_only=True,
                direction="callers",
            ):
                if label == callee_label:
                    continue
                hits = self._store.find_symbol(label)
                if not hits:
                    continue
                hit = sorted(hits, key=lambda row: int(row["id"]))[0]
                external_symbol = self._symbol_label(hit)
                external_file = str(hit.get("file_path") or "")
                if (external_symbol, external_file) in direct_locations:
                    continue
                affected.setdefault(
                    (external_symbol, external_file, "transitive"),
                    AffectedSymbol(
                        symbol=external_symbol,
                        file=external_file,
                        relationship="transitive",
                    ),
                )

        summary_parts = [
            f"file={file}",
            f"symbols={len(symbols)}",
            f"affected={len(affected)}",
            f"ambiguous_callers={len(breaking)}",
        ]
        summary = "; ".join(summary_parts)
        if diff is not None:
            summary = summary + "\n\n--- diff (verbatim) ---\n" + diff

        ordered_affected = sorted(
            affected.values(),
            key=lambda item: (item.relationship, item.file, item.symbol),
        )
        ordered_breaking = sorted(
            breaking.values(),
            key=lambda caller: (caller.file, caller.line, caller.symbol),
        )
        return Impact(
            affected_symbols=ordered_affected,
            breaking_callers=ordered_breaking,
            rule_violations=[],
            summary=summary,
        )

    # ---------------------------------------------------------------- context
    def context(
        self,
        symbol: str,
        *,
        direction: Direction | str = "callers",
        scope: str | None = None,
        depth: int = 2,
    ) -> ContextResult:
        """Return deterministic subgraph context for ``symbol``."""
        dir_norm = str(direction)
        if dir_norm not in ("callers", "callees", "both"):
            raise ValueError("direction must be one of callers|callees|both")

        seeds = self._store.find_symbol(symbol)
        if not seeds:
            raise SymbolNotFoundError(f"No symbol match for query: {symbol!r}")
        seed = sorted(seeds, key=lambda row: int(row["id"]))[0]
        seed_id = int(seed["id"])
        seed_label = self._symbol_label(seed)

        def scope_ok(fp: str) -> bool:
            return True if scope is None else fp.startswith(scope)

        matches: Dict[tuple[str, str, int, str], Caller] = {}
        edges: Dict[str, MutableSet[str]] = {}

        visited: MutableSet[int] = set()
        frontier: deque[tuple[int, int]] = deque([(seed_id, 0)])

        while frontier:
            current_id, dist = frontier.popleft()
            if current_id in visited:
                continue
            visited.add(current_id)

            rows = self._store.query(
                "SELECT id, name, qualified_name, file_path FROM nodes WHERE id = ? LIMIT 1",
                (current_id,),
            )
            if not rows:
                continue
            row = rows[0]
            label = self._symbol_label(row)

            if dist >= depth:
                continue

            next_depth_reached = dist + 1
            if dir_norm in ("callers", "both"):
                caller_edges = filter_edges(self._store.callers_of(current_id), deterministic_only=True)
                for edge in caller_edges:
                    fp = str(edge.get("caller_file") or "")
                    if not scope_ok(fp):
                        continue
                    caller_id = int(edge["source_id"])
                    caller_row = self._store.query(
                        "SELECT id, name, qualified_name, file_path FROM nodes WHERE id = ?",
                        (caller_id,),
                    )[0]
                    caller_label = self._symbol_label(caller_row)

                    resolution = cast(
                        ResolutionMethod,
                        str(edge.get("resolution_method") or "name_match"),
                    )
                    matched = Caller(
                        symbol=str(edge.get("caller_name") or caller_label),
                        file=fp,
                        line=int(edge.get("source_line") or 0),
                        resolution_method=resolution,
                        is_deterministic=is_deterministic(resolution),
                    )
                    key = (
                        matched.symbol,
                        matched.file,
                        matched.line,
                        matched.resolution_method,
                    )
                    matches[key] = matched
                    edges.setdefault(caller_label, set()).add(label)
                    if caller_id not in visited:
                        frontier.append((caller_id, next_depth_reached))

            if dir_norm in ("callees", "both"):
                callee_edges = filter_edges(self._store.callees_of(current_id), deterministic_only=True)
                for edge in callee_edges:
                    fp = str(edge.get("callee_file") or "")
                    if not scope_ok(fp):
                        continue
                    callee_id = int(edge["target_id"])
                    callee_row = self._store.query(
                        "SELECT id, name, qualified_name, file_path FROM nodes WHERE id = ?",
                        (callee_id,),
                    )[0]
                    callee_label = self._symbol_label(callee_row)

                    resolution = cast(
                        ResolutionMethod,
                        str(edge.get("resolution_method") or "name_match"),
                    )
                    line = int(edge.get("callee_line") or edge.get("source_line") or 0)
                    matched = Caller(
                        symbol=str(edge.get("callee_name") or callee_label),
                        file=fp,
                        line=line,
                        resolution_method=resolution,
                        is_deterministic=is_deterministic(resolution),
                    )
                    key = (
                        matched.symbol,
                        matched.file,
                        matched.line,
                        matched.resolution_method,
                    )
                    matches[key] = matched
                    edges.setdefault(label, set()).add(callee_label)
                    if callee_id not in visited:
                        frontier.append((callee_id, next_depth_reached))

        ordered_matches = sorted(matches.values(), key=lambda caller: (caller.file, caller.line, caller.symbol))
        call_graph = {src: sorted(dst) for src, dst in sorted(edges.items())}

        base = ContextResult(
            matches=ordered_matches,
            call_graph=call_graph,
            evidence="",
        )
        evidence_plain = format_context_markup(base, "plain", seed_symbol=seed_label).rstrip() + "\n"
        return ContextResult(
            matches=ordered_matches,
            call_graph=call_graph,
            evidence=evidence_plain,
        )

    # ---------------------------------------------------------------- inject
    def inject(
        self,
        prompt: str,
        symbols: list[str],
        *,
        fmt: FormatChoice | None = None,
        format: FormatChoice | None = None,
    ) -> str:
        """Inject formatted briefings for each explicit ``symbols`` entry before ``prompt``.

        Prefer ``fmt=`` — ``format=`` duplicates the same knob for MCP wrapper parity.

        Raises:
            ValueError: ``symbols`` is empty.
            SymbolNotFoundError: Propagated from ``briefing`` if a lookup fails.
        """
        if not symbols:
            raise ValueError("symbols must be a non-empty list")

        resolved = fmt if fmt is not None else (format if format is not None else "markdown")

        snippets: list[str] = []
        for token in symbols:
            briefing = self.briefing(token)
            snippets.append(format_briefing(briefing, resolved))
        preamble = "".join(snippet.rstrip("\n") + "\n\n" for snippet in snippets)
        return f"{preamble}{prompt}"
