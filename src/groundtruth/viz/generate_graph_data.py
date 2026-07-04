"""Generate graph data for the 3D Code City hallucination risk map."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

from groundtruth.analysis.risk_scorer import RiskScorer
from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result


class SymbolInfo(BaseModel):
    """Info about an exported symbol in a file."""

    name: str
    kind: str
    signature: str | None
    usage_count: int
    is_dead: bool


class GraphNode(BaseModel):
    """A node (file) in the Code City visualization."""

    id: str
    label: str
    directory: str
    risk_score: float
    risk_tag: str
    usage_count: int
    symbol_count: int
    risk_factors: dict[str, float]
    symbols: list[SymbolInfo]
    imports_from: list[str]
    imported_by: list[str]
    confusions: list[str]
    hallucination_rate: float | None
    directory_depth: int = 0
    normalized_height: float = 0.0
    normalized_width: float = 0.0
    has_dead_code: bool = False


class GraphEdge(BaseModel):
    """An edge (dependency) in the Code City visualization."""

    source: str
    target: str
    edge_type: str


class GraphMetadata(BaseModel):
    """Aggregate metadata for the visualization."""

    total_files: int
    total_symbols: int
    total_refs: int
    risk_summary: dict[str, int]


class GraphData(BaseModel):
    """Complete data for rendering the Code City."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    metadata: GraphMetadata


def _risk_tag(score: float) -> str:
    """Map a risk score to a human-readable tag."""
    if score >= 0.7:
        return "CRITICAL"
    if score >= 0.45:
        return "HIGH"
    if score >= 0.25:
        return "MODERATE"
    return "LOW"


def generate_graph_data(
    store: SymbolStore,
    risk_scorer: RiskScorer,
    limit: int = 200,
) -> Result[GraphData, GroundTruthError]:
    """Extract graph data from the store and risk scorer.

    Returns a GraphData object suitable for rendering as a Code City.
    """
    # 1. Get risk scores for all files
    scores_result = risk_scorer.score_codebase(limit=limit)
    if isinstance(scores_result, Err):
        return Err(scores_result.error)

    risk_scores = scores_result.value
    file_set = {rs.file_path for rs in risk_scores}

    # 2. Build nodes with full file intelligence
    nodes: list[GraphNode] = []
    for rs in risk_scores:
        symbols_result = store.get_symbols_in_file(rs.file_path)
        if isinstance(symbols_result, Err):
            continue
        file_symbols = symbols_result.value

        exported_symbols = [
            SymbolInfo(
                name=s.name,
                kind=s.kind,
                signature=s.signature,
                usage_count=s.usage_count,
                is_dead=s.is_exported and s.usage_count == 0,
            )
            for s in file_symbols
            if s.is_exported
        ]

        total_usage = sum(s.usage_count for s in file_symbols)

        # imports_from: files this file depends on (via refs table)
        imports_from_set: set[str] = set()
        refs_result = store.get_refs_from_file(rs.file_path)
        if isinstance(refs_result, Ok):
            for ref in refs_result.value:
                sym_result = store.get_symbol_by_id(ref.symbol_id)
                if isinstance(sym_result, Ok) and sym_result.value is not None:
                    dep_path = sym_result.value.file_path
                    if dep_path != rs.file_path:
                        imports_from_set.add(dep_path)

        # imported_by: files that reference symbols in this file
        importers_result = store.get_importers_of_file(rs.file_path)
        imported_by: list[str] = []
        if isinstance(importers_result, Ok):
            imported_by = [p for p in importers_result.value if p != rs.file_path]

        directory = os.path.dirname(rs.file_path)
        label = os.path.basename(rs.file_path)
        dir_depth = directory.count("/") if directory else 0
        has_dead = any(s.is_dead for s in exported_symbols)

        nodes.append(
            GraphNode(
                id=rs.file_path,
                label=label,
                directory=directory if directory else ".",
                risk_score=rs.overall_risk,
                risk_tag=_risk_tag(rs.overall_risk),
                usage_count=total_usage,
                symbol_count=len(exported_symbols),
                risk_factors=rs.factors,
                symbols=exported_symbols,
                imports_from=sorted(imports_from_set),
                imported_by=sorted(imported_by),
                confusions=[],
                hallucination_rate=None,
                directory_depth=dir_depth,
                has_dead_code=has_dead,
            )
        )

    # 3. Build edges via single SQL query for efficiency
    edges: list[GraphEdge] = []
    seen_edges: set[tuple[str, str]] = set()
    try:
        cursor = store.connection.execute(
            """SELECT DISTINCT r.referenced_in_file AS source_file,
                              s.file_path AS target_file,
                              r.reference_type
               FROM refs r
               JOIN symbols s ON r.symbol_id = s.id
               WHERE r.referenced_in_file != s.file_path"""
        )
        for row in cursor.fetchall():
            source = row["source_file"]
            target = row["target_file"]
            if source in file_set and target in file_set:
                edge_key = (source, target)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append(
                        GraphEdge(
                            source=source,
                            target=target,
                            edge_type=row["reference_type"],
                        )
                    )
    except Exception:
        pass  # Edges are best-effort; nodes are the core data

    # 4. Compute metadata
    stats_result = store.get_stats()
    total_symbols = 0
    total_files = 0
    total_refs = 0
    if isinstance(stats_result, Ok):
        stats = stats_result.value
        sc = stats.get("symbols_count", 0)
        total_symbols = int(sc) if isinstance(sc, (int, float, str)) else 0
        fc = stats.get("files_count", 0)
        total_files = int(fc) if isinstance(fc, (int, float, str)) else 0
        rc = stats.get("refs_count", 0)
        total_refs = int(rc) if isinstance(rc, (int, float, str)) else 0

    # Compute normalized height and width
    import math

    max_usage = max((n.usage_count for n in nodes), default=1) or 1
    max_sym_sqrt = max((math.sqrt(n.symbol_count) for n in nodes), default=1.0) or 1.0
    for node in nodes:
        node.normalized_height = node.usage_count / max_usage
        node.normalized_width = math.sqrt(node.symbol_count) / max_sym_sqrt

    # 4-tier risk distribution
    critical = sum(1 for n in nodes if n.risk_score >= 0.7)
    high = sum(1 for n in nodes if 0.45 <= n.risk_score < 0.7)
    moderate = sum(1 for n in nodes if 0.25 <= n.risk_score < 0.45)
    low = sum(1 for n in nodes if n.risk_score < 0.25)

    metadata = GraphMetadata(
        total_files=total_files,
        total_symbols=total_symbols,
        total_refs=total_refs,
        risk_summary={
            "critical": critical,
            "high": high,
            "moderate": moderate,
            "low": low,
        },
    )

    return Ok(GraphData(nodes=nodes, edges=edges, metadata=metadata))


def render_risk_map(
    graph_data: GraphData,
    output_path: str,
    theme: str = "dark",
    bloom: bool = True,
) -> Result[str, GroundTruthError]:
    """Render the Code City HTML file with inlined graph data.

    Returns the absolute path to the generated HTML file.
    """
    import json as json_mod

    from groundtruth.viz.risk_map_template import RISK_MAP_TEMPLATE

    json_str = graph_data.model_dump_json()
    config_json = json_mod.dumps({"theme": theme, "bloom": bloom})
    html = RISK_MAP_TEMPLATE.replace("__GRAPH_DATA_JSON__", json_str)
    html = html.replace("__CONFIG_JSON__", config_json)

    try:
        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        return Ok(str(out))
    except OSError as exc:
        return Err(
            GroundTruthError(
                code="file_write_failed",
                message=f"Failed to write risk map: {exc}",
            )
        )
