"""Direct Python bridge from SWE-bench agent to GroundTruth handlers.

Bypasses MCP transport. Instantiates the same components server.py creates,
then calls handle_* functions from tools.py directly.
"""

from __future__ import annotations

import asyncio
import json
import logging

from groundtruth.index.store import SymbolStore
from groundtruth.index.graph import ImportGraph
from groundtruth.index.indexer import Indexer
from groundtruth.lsp.manager import LSPManager
from groundtruth.validators.orchestrator import ValidationOrchestrator
from groundtruth.analysis.risk_scorer import RiskScorer
from groundtruth.analysis.adaptive_briefing import AdaptiveBriefing
from groundtruth.analysis.grounding_gap import GroundingGapAnalyzer
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.stats.token_tracker import TokenTracker
from groundtruth.ai.task_parser import TaskParser
from groundtruth.ai.briefing import BriefingEngine
from groundtruth.utils.result import Err
from groundtruth.mcp.tools import (
    handle_orient,
    handle_find_relevant,
    handle_brief,
    handle_validate,
    handle_trace,
    handle_status,
    handle_dead_code,
    handle_unused_packages,
    handle_hotspots,
    handle_symbols,
    handle_context,
    handle_explain,
    handle_impact,
    handle_patterns,
    handle_checkpoint,
)

logger = logging.getLogger(__name__)


class GroundTruthBridge:
    """Direct Python bridge to GroundTruth — no MCP transport overhead."""

    def __init__(self, repo_path: str, db_path: str = ":memory:", index_timeout: int = 120):
        self.repo_path = repo_path
        self.db_path = db_path
        self.index_timeout = index_timeout
        self._initialized = False

        # Components (created in initialize())
        self.store: SymbolStore | None = None
        self.graph: ImportGraph | None = None
        self.tracker: InterventionTracker | None = None
        self.token_tracker: TokenTracker | None = None
        self.task_parser: TaskParser | None = None
        self.briefing_engine: BriefingEngine | None = None
        self.lsp_manager: LSPManager | None = None
        self.orchestrator: ValidationOrchestrator | None = None
        self.risk_scorer: RiskScorer | None = None
        self.adaptive: AdaptiveBriefing | None = None
        self.grounding_analyzer: GroundingGapAnalyzer | None = None

    async def initialize(self) -> bool:
        """Index the repo and initialize all components. Returns True on success."""
        try:
            self.store = SymbolStore(self.db_path)
            self.store.initialize()
            self.graph = ImportGraph(self.store)
            self.tracker = InterventionTracker(self.store)
            self.token_tracker = TokenTracker()
            self.task_parser = TaskParser(self.store, api_key=None)
            self.briefing_engine = BriefingEngine(self.store, api_key=None)
            self.lsp_manager = LSPManager(self.repo_path)
            self.orchestrator = ValidationOrchestrator(self.store, self.lsp_manager, api_key=None)
            self.risk_scorer = RiskScorer(self.store)
            self.adaptive = AdaptiveBriefing(self.store, self.risk_scorer)
            self.grounding_analyzer = GroundingGapAnalyzer(self.store)

            # Index the repo
            indexer = Indexer(self.store, self.lsp_manager)
            result = await asyncio.wait_for(
                indexer.index_project(self.repo_path),
                timeout=self.index_timeout,
            )
            # Check if indexing succeeded (Result type)
            if isinstance(result, Err):
                logger.warning("Indexing returned error: %s", result.error.message)
                # Continue anyway — partial index is better than none

            self._initialized = True
            stats_result = self.store.get_stats()
            stats = stats_result.value if not isinstance(stats_result, Err) else {}
            logger.info(
                "GroundTruth indexed %s: %s symbols, %s files, %s refs",
                self.repo_path, stats.get("symbols_count", 0),
                stats.get("files_count", 0), stats.get("refs_count", 0),
            )
            return True
        except asyncio.TimeoutError:
            logger.error("GroundTruth indexing timed out after %ds", self.index_timeout)
            return False
        except Exception:
            logger.exception("GroundTruth initialization failed")
            return False

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a GroundTruth tool by name. Returns JSON string result."""
        if not self._initialized:
            return json.dumps({"error": "GroundTruth not initialized"})

        assert self.store is not None
        assert self.graph is not None
        assert self.tracker is not None
        assert self.token_tracker is not None

        try:
            result = await self._dispatch(tool_name, arguments)
            return json.dumps(result, default=str)
        except Exception as e:
            logger.exception("GroundTruth tool %s failed", tool_name)
            return json.dumps({"error": str(e)})

    async def _dispatch(self, tool_name: str, args: dict) -> dict:
        """Route tool calls to handler functions."""
        assert self.store and self.graph and self.tracker and self.token_tracker

        handlers = {
            "groundtruth_orient": lambda: handle_orient(
                store=self.store,
                graph=self.graph,
                tracker=self.tracker,
                risk_scorer=self.risk_scorer,
                root_path=self.repo_path,
            ),
            "groundtruth_find_relevant": lambda: handle_find_relevant(
                description=args.get("description", ""),
                store=self.store,
                graph=self.graph,
                tracker=self.tracker,
                task_parser=self.task_parser,
                max_files=args.get("max_files", 10),
            ),
            "groundtruth_brief": lambda: handle_brief(
                intent=args.get("intent", ""),
                briefing_engine=self.briefing_engine,
                tracker=self.tracker,
                store=self.store,
                graph=self.graph,
                target_file=args.get("target_file"),
                adaptive=self.adaptive,
            ),
            "groundtruth_validate": lambda: handle_validate(
                proposed_code=args.get("proposed_code", ""),
                file_path=args.get("file_path", ""),
                orchestrator=self.orchestrator,
                tracker=self.tracker,
                store=self.store,
                language=args.get("language"),
                grounding_analyzer=self.grounding_analyzer,
            ),
            "groundtruth_explain": lambda: handle_explain(
                symbol=args.get("symbol", ""),
                store=self.store,
                graph=self.graph,
                tracker=self.tracker,
                root_path=self.repo_path,
                file_path=args.get("file_path"),
            ),
            "groundtruth_impact": lambda: handle_impact(
                symbol=args.get("symbol", ""),
                store=self.store,
                graph=self.graph,
                tracker=self.tracker,
                root_path=self.repo_path,
                max_depth=args.get("max_depth", 3),
            ),
            "groundtruth_patterns": lambda: handle_patterns(
                file_path=args.get("file_path", ""),
                store=self.store,
                tracker=self.tracker,
                root_path=self.repo_path,
            ),
            "groundtruth_trace": lambda: handle_trace(
                symbol=args.get("symbol", ""),
                store=self.store,
                graph=self.graph,
                tracker=self.tracker,
                direction=args.get("direction", "both"),
                max_depth=args.get("max_depth", 3),
            ),
            "groundtruth_symbols": lambda: handle_symbols(
                file_path=args.get("file_path", ""),
                store=self.store,
                tracker=self.tracker,
            ),
            "groundtruth_context": lambda: handle_context(
                symbol=args.get("symbol", ""),
                store=self.store,
                graph=self.graph,
                tracker=self.tracker,
                root_path=self.repo_path,
                limit=args.get("limit", 10),
            ),
            "groundtruth_status": lambda: handle_status(
                store=self.store, tracker=self.tracker,
            ),
            "groundtruth_checkpoint": lambda: handle_checkpoint(
                store=self.store, tracker=self.tracker, risk_scorer=self.risk_scorer,
            ),
            "groundtruth_dead_code": lambda: handle_dead_code(
                store=self.store, tracker=self.tracker,
            ),
            "groundtruth_unused_packages": lambda: handle_unused_packages(
                store=self.store, tracker=self.tracker,
            ),
            "groundtruth_hotspots": lambda: handle_hotspots(
                store=self.store, tracker=self.tracker,
                limit=args.get("limit", 20),
            ),
        }

        handler = handlers.get(tool_name)
        if handler is None:
            return {"error": f"Unknown tool: {tool_name}"}

        result = handler()
        # Handle both sync and async handlers
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def shutdown(self) -> None:
        """Clean up resources."""
        if self.lsp_manager:
            try:
                await self.lsp_manager.shutdown_all()
            except Exception:
                pass
        if self.store:
            try:
                self.store.close()
            except Exception:
                pass
