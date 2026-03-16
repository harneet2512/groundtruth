"""With-MCP benchmark path: spawn GroundTruth MCP server, connect client, run same tasks, record proof."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))
if str(_ROOT / "benchmarks") not in sys.path:
    sys.path.insert(0, str(_ROOT / "benchmarks"))

from benchmarks.ab.models import ABReport, MCPProof, RunMetadata
from benchmarks.runner import (
    _error_type_matches,
    _AI_RESOLVABLE_TYPES,
    load_cases,
    load_file_relevance_cases,
)
from benchmarks._fixtures import LANG_CONFIG, populate_store  # noqa: I001


def _prepare_server_index(temp_dir: str, db_path: str) -> None:
    """Create a single SQLite index with all fixture languages for the MCP server."""
    from groundtruth.index.store import SymbolStore
    from groundtruth.utils.result import Ok

    store = SymbolStore(db_path)
    store.initialize()
    for lang in ("typescript", "python", "go"):
        config = LANG_CONFIG[lang]
        populate_store(store, config)
    store.close()


async def _run_with_mcp_anyio(
    temp_root: str,
    db_path: str,
    fixture_filter: str,
    run_id: str | None = None,
) -> tuple[ABReport, MCPProof]:
    """Run benchmark via MCP client (call with anyio.run(..., backend='asyncio'))."""
    import anyio
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    bench_dir = _ROOT / "benchmarks"
    hallucination_dir = str(bench_dir / "hallucination-cases")
    relevance_dir = str(bench_dir / "file-relevance-cases")

    all_hallucination_cases = load_cases(hallucination_dir)
    all_relevance_cases = load_file_relevance_cases(relevance_dir)

    if fixture_filter == "all":
        languages = list(LANG_CONFIG.keys())
    else:
        lang_map = {
            "project_ts": "typescript",
            "project_py": "python",
            "project_go": "go",
            "typescript": "typescript",
            "python": "python",
            "go": "go",
        }
        lang = lang_map.get(fixture_filter, fixture_filter)
        languages = [lang] if lang in LANG_CONFIG else list(LANG_CONFIG.keys())

    tool_calls_log: list[dict] = []
    substantive_names = {
        "groundtruth_validate",
        "groundtruth_find_relevant",
        "groundtruth_brief",
        "groundtruth_trace",
        "groundtruth_dead_code",
        "groundtruth_unused_packages",
        "groundtruth_hotspots",
        "groundtruth_orient",
        "groundtruth_explain",
        "groundtruth_impact",
        "groundtruth_patterns",
    }

    if run_id:
        os.environ["GROUNDTRUTH_RUN_ID"] = run_id

    command = sys.executable
    args = [
        "-m",
        "groundtruth.main",
        "serve",
        "--root",
        temp_root,
        "--db",
        db_path,
        "--no-auto-index",
    ]

    server_params = StdioServerParameters(command=command, args=args, cwd=str(_ROOT))

    proof = MCPProof(mcp_enabled=True, connection_ok=False, valid=False, run_id=run_id)
    case_results = []
    file_relevance_results = []
    by_category: dict[str, dict] = {}
    start_time = time.monotonic()

    async def run_session() -> None:
        nonlocal proof, case_results, file_relevance_results, by_category
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                proof.connection_ok = True

                tools_result = await session.list_tools()
                if hasattr(tools_result, "tools") and tools_result.tools:
                    proof.tools_discovered = [t.name for t in tools_result.tools]
                else:
                    proof.tools_discovered = []

                # Hallucination cases: call groundtruth_validate for each
                for lang in languages:
                    lang_cases = [c for c in all_hallucination_cases if c.language == lang]
                    for bc in lang_cases:
                        t0 = time.monotonic()
                        result = await session.call_tool(
                            "groundtruth_validate",
                            arguments={
                                "proposed_code": bc.code,
                                "file_path": bc.file_path,
                                "language": bc.language,
                            },
                        )
                        latency_ms = (time.monotonic() - t0) * 1000
                        success = not getattr(result, "isError", True)
                        tool_calls_log.append(
                            {"name": "groundtruth_validate", "success": success}
                        )

                        # Parse result: CallToolResult.content is list[ContentBlock]
                        content = getattr(result, "content", []) or []
                        text = "{}"
                        if isinstance(content, list) and content:
                            parts = [
                                getattr(p, "text", str(p))
                                for p in content
                                if hasattr(p, "text")
                            ]
                            text = "".join(parts) if parts else "{}"
                        try:
                            data = json.loads(text) if isinstance(text, str) else {}
                        except json.JSONDecodeError:
                            data = {}

                        errors = data.get("errors", [])
                        detected = any(
                            _error_type_matches(e.get("type", ""), bc.error_type or "")
                            for e in errors
                        ) if bc.error_type else False

                        fix_correct = False
                        if detected and (bc.correct_symbol or bc.correct_import):
                            for err in errors:
                                if not _error_type_matches(
                                    err.get("type", ""), bc.error_type or ""
                                ):
                                    continue
                                sug = err.get("suggestion")
                                if not sug:
                                    continue
                                fix_text = sug.get("fix", "")
                                if bc.correct_symbol and bc.correct_symbol in fix_text:
                                    fix_correct = True
                                if bc.correct_import and bc.correct_import in fix_text:
                                    fix_correct = True

                        ai_needed = False
                        if detected:
                            for err in errors:
                                if not _error_type_matches(
                                    err.get("type", ""), bc.error_type or ""
                                ):
                                    continue
                                if not err.get("suggestion") and err.get(
                                    "type"
                                ) in _AI_RESOLVABLE_TYPES:
                                    ai_needed = True
                                    break

                        briefing_would_inform = False
                        case_results.append(
                            {
                                "id": bc.id,
                                "category": bc.category,
                                "subcategory": bc.subcategory,
                                "language": bc.language,
                                "detected": detected,
                                "fix_correct": fix_correct,
                                "ai_needed": ai_needed,
                                "briefing_would_inform": briefing_would_inform,
                                "latency_ms": round(latency_ms, 2),
                            }
                        )

                        key = (
                            f"{bc.category}/{bc.subcategory}"
                            if bc.subcategory
                            else bc.category
                        )
                        if key not in by_category:
                            by_category[key] = {
                                "total": 0,
                                "detected": 0,
                                "fix_correct": 0,
                                "ai_needed": 0,
                                "briefing_would_inform": 0,
                            }
                        by_category[key]["total"] += 1
                        if detected:
                            by_category[key]["detected"] += 1
                        if fix_correct:
                            by_category[key]["fix_correct"] += 1
                        if ai_needed:
                            by_category[key]["ai_needed"] += 1
                        if briefing_would_inform:
                            by_category[key]["briefing_would_inform"] += 1

                # File relevance: call groundtruth_find_relevant
                for lang in languages:
                    lang_relevance = [
                        c for c in all_relevance_cases if c.language == lang
                    ]
                    for fc in lang_relevance:
                        result = await session.call_tool(
                            "groundtruth_find_relevant",
                            arguments={
                                "description": fc.task,
                                "entry_symbols": fc.entry_symbols,
                                "max_files": 5,
                            },
                        )
                        success = not getattr(result, "isError", True)
                        tool_calls_log.append(
                            {"name": "groundtruth_find_relevant", "success": success}
                        )

                        content = getattr(result, "content", []) or []
                        text = "{}"
                        if isinstance(content, list) and content:
                            parts = [
                                getattr(p, "text", str(p))
                                for p in content
                                if hasattr(p, "text")
                            ]
                            text = "".join(parts) if parts else "{}"
                        try:
                            data = json.loads(text) if isinstance(text, str) else {}
                        except json.JSONDecodeError:
                            data = {}
                        files = data.get("files", [])
                        found_paths = {f.get("path", "") for f in files}
                        expected_set = set(fc.expected_files)
                        excluded_set = set(fc.should_not_include)
                        found_expected = expected_set & found_paths
                        recall = (
                            len(found_expected) / len(expected_set)
                            if expected_set
                            else 1.0
                        )
                        precision = (
                            len(found_expected) / len(found_paths)
                            if found_paths
                            else 0.0
                        )
                        missed = list(expected_set - found_paths)
                        file_relevance_results.append(
                            {
                                "id": fc.id,
                                "language": fc.language,
                                "precision": precision,
                                "recall": recall,
                                "missed_files": missed,
                            }
                        )

    try:
        await run_session()
    except Exception as e:
        proof.connection_ok = False
        proof.tool_calls = [{"name": "error", "success": False, "error": str(e)}]
        elapsed_s = time.monotonic() - start_time
        metadata = RunMetadata(
            condition="with_groundtruth_mcp",
            mcp_proof=proof,
            elapsed_s=elapsed_s,
            total_cases=len(case_results),
            total_file_relevance=len(file_relevance_results),
            run_id=run_id,
        )
        return (
            ABReport(
                metadata=metadata,
                total_cases=len(case_results),
                detected=0,
                fix_correct=0,
                ai_needed=0,
                briefing_would_inform=0,
                by_category=by_category,
                file_relevance_results=file_relevance_results,
                case_results=case_results,
                elapsed_s=elapsed_s,
            ),
            proof,
        )

    proof.tool_calls = tool_calls_log
    proof.substantive_tool_count = sum(
        1 for t in tool_calls_log if t.get("name") in substantive_names
    )
    proof.valid = proof.connection_ok and proof.substantive_tool_count >= 1

    elapsed_s = time.monotonic() - start_time
    total_detected = sum(1 for r in case_results if r.get("detected"))
    total_fix = sum(1 for r in case_results if r.get("fix_correct"))
    total_ai = sum(1 for r in case_results if r.get("ai_needed"))
    total_briefing = sum(1 for r in case_results if r.get("briefing_would_inform"))

    metadata = RunMetadata(
        condition="with_groundtruth_mcp",
        mcp_proof=proof,
        elapsed_s=elapsed_s,
        total_cases=len(case_results),
        total_file_relevance=len(file_relevance_results),
        run_id=run_id,
    )

    return (
        ABReport(
            metadata=metadata,
            total_cases=len(case_results),
            detected=total_detected,
            fix_correct=total_fix,
            ai_needed=total_ai,
            briefing_would_inform=total_briefing,
            by_category=by_category,
            file_relevance_results=file_relevance_results,
            case_results=case_results,
            elapsed_s=elapsed_s,
        ),
        proof,
    )


def run_with_groundtruth_mcp(
    fixture_filter: str = "all",
    run_id: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> ABReport:
    """Run the benchmark with real MCP server; prove tool usage."""
    import anyio

    with tempfile.TemporaryDirectory(prefix="groundtruth_ab_") as tmp:
        temp_root = tmp
        db_dir = os.path.join(temp_root, ".groundtruth")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "index.db")
        _prepare_server_index(temp_root, db_path)

        report, proof = anyio.run(
            _run_with_mcp_anyio,
            temp_root,
            db_path,
            fixture_filter,
            run_id,
            backend="asyncio",
        )
        report.metadata.mcp_proof = proof
        report.metadata.model = model
        report.metadata.temperature = temperature
        report.metadata.max_tokens = max_tokens
        return report
