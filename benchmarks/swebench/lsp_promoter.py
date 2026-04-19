"""LSP promotion wrapper for SWE-agent hook.

Thin sync layer around resolve._get_ambiguous_edges + _resolve_edges.
Scopes resolution to specific source files (checkpoint-relevant),
caches per-task to avoid redundant LSP calls, and returns stats for telemetry.

Normalized promotion_stats always contain these spec-required keys so the
metrics aggregator can count them directly without None-handling:

    attempts, verified, ambiguous, unresolved, stale,
    cache_hits, cache_misses, cache_hit_rate, cache_miss_rate,
    warmup_latency_ms, added_checkpoint_latency_ms, error (optional)
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from typing import Any

# The live task bundle copies the vendored `groundtruth` package into a
# staging directory. Add every known parent directory before importing the
# resolver so the runtime does not depend on shell startup files or an
# ambient PYTHONPATH. Keep both the runner host and task-container paths.
for _p in [
    "/tmp/groundtruth_src",
    "/home/Lenovo/groundtruth_src",
    "/tmp",
    os.path.dirname(os.path.abspath(__file__)),
]:
    if _p and _p not in sys.path and os.path.isdir(_p):
        sys.path.insert(0, _p)

_LSP_CACHE: set[int] = set()
_WARMUP_DONE = False


_SPEC_KEYS: dict[str, Any] = {
    "attempts": 0,
    "verified": 0,
    "corrected": 0,
    "deleted": 0,
    "failed": 0,
    "ambiguous": 0,
    "unresolved": 0,
    "stale": 0,
    "cache_hits": 0,
    "cache_misses": 0,
    "cache_hit_rate": 0.0,
    "cache_miss_rate": 0.0,
    "warmup_latency_ms": 0.0,
    "added_checkpoint_latency_ms": 0.0,
}


def _normalize(stats: dict[str, Any]) -> dict[str, Any]:
    """Ensure all spec-required keys exist in stats dict with 0/None defaults."""
    out = dict(_SPEC_KEYS)
    out.update({k: v for k, v in stats.items() if v is not None})
    attempts = int(out.get("attempts", 0) or 0)
    cache_hits = int(out.get("cache_hits", 0) or 0)
    cache_misses = int(out.get("cache_misses", 0) or 0)
    total = cache_hits + cache_misses
    if total > 0:
        out["cache_hit_rate"] = round(cache_hits / total, 3)
        out["cache_miss_rate"] = round(cache_misses / total, 3)
    # ambiguous = attempts that ended up unresolved (not verified/corrected)
    resolved = int(out.get("verified", 0) or 0) + int(out.get("corrected", 0) or 0)
    if attempts > 0 and "ambiguous" not in stats:
        out["ambiguous"] = max(0, attempts - resolved - int(out.get("deleted", 0) or 0))
    return out


def promote_ambiguous_edges(
    source_files: list[str],
    db_path: str = "/tmp/gt_graph.db",
    root: str = ".",
    language: str = "python",
) -> dict[str, Any]:
    """Promote ambiguous edges in scope files via LSP. Returns normalized stats."""
    global _WARMUP_DONE
    checkpoint_start = time.time()

    if not source_files:
        stats = {"attempts": 0, "reason": "no_files"}
        return _normalize(stats) | {
            "added_checkpoint_latency_ms": round(
                (time.time() - checkpoint_start) * 1000, 1
            )
        }

    if not os.path.exists(db_path):
        return _normalize({"attempts": 0, "error": "no_db"})

    try:
        from groundtruth.resolve import _get_ambiguous_edges, _resolve_edges
    except ImportError as e:
        return _normalize({"attempts": 0, "error": f"resolve_import_failed:{e}"[:200]})

    conn = sqlite3.connect(db_path, timeout=5)
    try:
        edges = _get_ambiguous_edges(
            conn,
            min_confidence=0.9,
            language=language,
            source_files=source_files,
        )
    except Exception as e:
        conn.close()
        return _normalize({"attempts": 0, "error": str(e)[:200]})
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not edges:
        stats = {"attempts": 0, "reason": "no_ambiguous_edges"}
        return _normalize(stats) | {
            "added_checkpoint_latency_ms": round(
                (time.time() - checkpoint_start) * 1000, 1
            )
        }

    cache_hits = 0
    fresh_edges = []
    for e in edges:
        eid = e["id"]
        if eid in _LSP_CACHE:
            cache_hits += 1
        else:
            fresh_edges.append(e)

    if not fresh_edges:
        stats = {
            "attempts": len(edges),
            "cache_hits": cache_hits,
            "cache_misses": 0,
        }
        return _normalize(stats) | {
            "added_checkpoint_latency_ms": round(
                (time.time() - checkpoint_start) * 1000, 1
            )
        }

    warmup_start = time.time()
    try:
        raw = asyncio.run(_resolve_edges(db_path, root, fresh_edges, language))
    except Exception as e:
        return _normalize({
            "attempts": len(fresh_edges),
            "cache_hits": cache_hits,
            "cache_misses": len(fresh_edges),
            "error": str(e)[:200],
        })
    warmup_ms = (time.time() - warmup_start) * 1000
    if not _WARMUP_DONE:
        _WARMUP_DONE = True

    for e in fresh_edges:
        _LSP_CACHE.add(e["id"])

    raw["attempts"] = len(fresh_edges)
    raw["cache_hits"] = cache_hits
    raw["cache_misses"] = len(fresh_edges)
    raw["warmup_latency_ms"] = round(warmup_ms, 1)
    raw["added_checkpoint_latency_ms"] = round(
        (time.time() - checkpoint_start) * 1000, 1
    )
    return _normalize(raw)
