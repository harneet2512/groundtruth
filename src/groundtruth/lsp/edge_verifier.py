"""Lazy LSP edge verification — verifies graph.db edges on demand before suggesting.

Architecture (Decision 34 §12 + jedi__branch):
- gt-index produces graph.db with speculative name_match edges (confidence 0.2-0.6)
- Before L3/L3b suggests a caller to the agent, this module verifies the edge via LSP
- LSP query: textDocument/references on the target symbol → check if source file is in results
- Verified edges: confidence promoted to 1.0, cached
- Rejected edges: confidence set to 0.0, cached, never suggested
- Cache persists for task lifetime (same file won't be re-verified)

Performance:
- LSP server started once, stays warm for entire task (~2-5s cold start, amortized)
- Per-verification: ~50-200ms (single textDocument/references call)
- Per-task: ~500-1000ms total (5-10 unique edges verified, rest cached)
- On a 100-iteration task at ~10s/iter, this is <0.1% overhead

Fallback:
- If LSP server unavailable: fall back to confidence filter (>= 0.9 only)
- If LSP query times out: mark edge as UNVERIFIED, suggest with original confidence
- If language not supported by LSP: use gt-index confidence as-is
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from groundtruth.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class VerifiedEdge:
    source_file: str
    target_file: str
    target_symbol: str
    verified: bool
    confidence: float
    method: str
    latency_ms: int = 0


class EdgeVerificationCache:
    """In-memory cache of verified/rejected edges. Keyed by (source_file, target_symbol)."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], VerifiedEdge] = {}
        self._stats = {"hits": 0, "misses": 0, "verified": 0, "rejected": 0, "fallback": 0}

    def get(self, source_file: str, target_symbol: str) -> VerifiedEdge | None:
        key = (source_file, target_symbol)
        result = self._cache.get(key)
        if result:
            self._stats["hits"] += 1
        else:
            self._stats["misses"] += 1
        return result

    def put(self, edge: VerifiedEdge) -> None:
        key = (edge.source_file, edge.target_symbol)
        self._cache[key] = edge
        if edge.verified:
            self._stats["verified"] += 1
        else:
            self._stats["rejected"] += 1

    def put_fallback(self, source_file: str, target_symbol: str, original_confidence: float) -> None:
        key = (source_file, target_symbol)
        self._cache[key] = VerifiedEdge(
            source_file=source_file, target_file="", target_symbol=target_symbol,
            verified=original_confidence >= 0.9, confidence=original_confidence,
            method="fallback_no_lsp",
        )
        self._stats["fallback"] += 1

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)


class LazyEdgeVerifier:
    """Verifies graph.db edges on demand via LSP before suggesting to agent.

    Usage:
        verifier = LazyEdgeVerifier(workspace_root, graph_db_path)
        await verifier.start()  # starts LSP server, warms it

        # Before suggesting a caller:
        is_real = await verifier.verify_caller(edited_file, caller_file, caller_symbol, caller_line)
        if is_real:
            # suggest to agent
        else:
            # suppress — false positive
    """

    def __init__(self, workspace_root: str, graph_db: str = "") -> None:
        self._workspace = workspace_root
        self._graph_db = graph_db
        self._cache = EdgeVerificationCache()
        self._lsp_manager: Any = None
        self._available = False
        self._start_time_ms = 0

    async def start(self) -> bool:
        """Start LSP manager. Returns True if LSP is available."""
        try:
            from groundtruth.lsp.manager import LSPManager
            self._lsp_manager = LSPManager(self._workspace)
            self._available = True
            self._start_time_ms = int(time.time() * 1000)
            logger.info("edge_verifier_started", workspace=self._workspace)
            return True
        except Exception as e:
            logger.warning("edge_verifier_start_failed", error=str(e))
            self._available = False
            return False

    async def verify_caller(
        self,
        target_file: str,
        target_symbol: str,
        target_line: int,
        caller_file: str,
        original_confidence: float = 0.5,
        timeout: float = 5.0,
    ) -> VerifiedEdge:
        """Verify that caller_file actually calls target_symbol in target_file.

        Uses textDocument/references on the target symbol, checks if caller_file
        appears in the results.

        Returns VerifiedEdge with verified=True/False and confidence 1.0/0.0.
        """
        # Check cache first
        cached = self._cache.get(caller_file, target_symbol)
        if cached is not None:
            return cached

        # If LSP not available, fall back to confidence filter
        if not self._available or self._lsp_manager is None:
            self._cache.put_fallback(caller_file, target_symbol, original_confidence)
            return self._cache.get(caller_file, target_symbol)  # type: ignore

        # Determine file extension for LSP server selection
        ext = os.path.splitext(target_file)[1].lower()
        from groundtruth.lsp.config import LSP_SERVERS
        if ext not in LSP_SERVERS:
            self._cache.put_fallback(caller_file, target_symbol, original_confidence)
            return self._cache.get(caller_file, target_symbol)  # type: ignore

        # Get LSP client
        t0 = time.time()
        try:
            client_result = await self._lsp_manager.ensure_server(ext)
            from groundtruth.utils.result import Err
            if isinstance(client_result, Err):
                self._cache.put_fallback(caller_file, target_symbol, original_confidence)
                return self._cache.get(caller_file, target_symbol)  # type: ignore

            client = client_result.value

            # Open the target file if not already open
            target_uri = Path(os.path.join(self._workspace, target_file)).as_uri()
            target_full = os.path.join(self._workspace, target_file)
            if os.path.exists(target_full):
                text = open(target_full, encoding="utf-8", errors="replace").read()
                from groundtruth.lsp.config import get_language_id
                lang_result = get_language_id(ext)
                lang_id = lang_result.value if not isinstance(lang_result, Err) else "python"
                await client.did_open(target_uri, lang_id, 1, text)

            # Query references for the target symbol at its definition line
            refs_result = await client.references(
                target_uri, target_line - 1, 0,  # LSP is 0-indexed
                include_declaration=False,
                timeout=timeout,
            )

            latency_ms = int((time.time() - t0) * 1000)

            if isinstance(refs_result, Err):
                self._cache.put_fallback(caller_file, target_symbol, original_confidence)
                return self._cache.get(caller_file, target_symbol)  # type: ignore

            refs = refs_result.value

            # Check if caller_file appears in references
            caller_norm = caller_file.replace("\\", "/")
            verified = any(
                caller_norm in (ref.uri or "").replace("\\", "/")
                for ref in refs
            )

            edge = VerifiedEdge(
                source_file=caller_file,
                target_file=target_file,
                target_symbol=target_symbol,
                verified=verified,
                confidence=1.0 if verified else 0.0,
                method="lsp_references",
                latency_ms=latency_ms,
            )
            self._cache.put(edge)

            logger.info(
                "edge_verified",
                target=f"{target_file}:{target_symbol}",
                caller=caller_file,
                verified=verified,
                latency_ms=latency_ms,
            )
            return edge

        except (asyncio.TimeoutError, Exception) as e:
            latency_ms = int((time.time() - t0) * 1000)
            logger.warning("edge_verify_failed", error=str(e), latency_ms=latency_ms)
            self._cache.put_fallback(caller_file, target_symbol, original_confidence)
            return self._cache.get(caller_file, target_symbol)  # type: ignore

    def get_stats(self) -> dict[str, Any]:
        """Return verification stats for telemetry."""
        return {
            "cache_stats": self._cache.stats,
            "lsp_available": self._available,
            "uptime_ms": int(time.time() * 1000) - self._start_time_ms if self._available else 0,
        }


def verify_edge_sync(
    workspace_root: str,
    target_file: str,
    target_symbol: str,
    target_line: int,
    caller_file: str,
    original_confidence: float = 0.5,
    timeout: float = 5.0,
) -> VerifiedEdge:
    """Synchronous wrapper for edge verification. Uses asyncio.run or existing loop."""
    verifier = LazyEdgeVerifier(workspace_root)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    _verify_one(verifier, target_file, target_symbol, target_line, caller_file, original_confidence, timeout),
                )
                return future.result(timeout=timeout + 2)
        else:
            return loop.run_until_complete(
                _verify_one(verifier, target_file, target_symbol, target_line, caller_file, original_confidence, timeout),
            )
    except Exception:
        return VerifiedEdge(
            source_file=caller_file, target_file=target_file, target_symbol=target_symbol,
            verified=original_confidence >= 0.9, confidence=original_confidence,
            method="sync_fallback",
        )


async def _verify_one(
    verifier: LazyEdgeVerifier,
    target_file: str, target_symbol: str, target_line: int,
    caller_file: str, original_confidence: float, timeout: float,
) -> VerifiedEdge:
    await verifier.start()
    return await verifier.verify_caller(
        target_file, target_symbol, target_line, caller_file,
        original_confidence=original_confidence, timeout=timeout,
    )
