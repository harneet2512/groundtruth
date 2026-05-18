"""Lifecycle hooks for GroundTruth + Inspect AI integration.

on_sample_init: Builds graph.db for the repo under test using gt-index.
on_sample_end: Collects GT utilization metrics from the completed sample.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from inspect_ai.solver import TaskState


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# gt-index binary location. The GHA workflow builds from source and puts it
# at /tmp/gt-index; local dev may have it on PATH or in GT_INDEX_BINARY.
def _find_gt_index() -> str | None:
    """Locate the gt-index binary."""
    # 1. Explicit env var
    explicit = os.environ.get("GT_INDEX_BINARY")
    if explicit and os.path.isfile(explicit):
        return explicit

    # 2. Common GHA location
    gha_path = "/tmp/gt-index"
    if os.path.isfile(gha_path):
        return gha_path

    # 3. On PATH
    found = shutil.which("gt-index")
    if found:
        return found

    return None


# Where to write graph.db inside the sandbox. The sandbox mounts the repo
# at /workspace or the repo root; we write graph.db next to it and set
# GT_GRAPH_DB so the tools find it.
GT_OUTPUT_DIR = os.environ.get("GT_OUTPUT_DIR", "/tmp/gt_inspect")


# ---------------------------------------------------------------------------
# on_sample_init: Build graph.db
# ---------------------------------------------------------------------------


async def on_sample_init(state: TaskState) -> TaskState:
    """Build graph.db for the repo under test before the agent starts.

    Steps:
    1. Locate gt-index binary.
    2. Determine repo root from the sandbox working directory.
    3. Run gt-index to produce graph.db.
    4. Set GT_GRAPH_DB environment variable for the tools.

    If gt-index is unavailable or fails, the sample proceeds without GT
    tools (they return helpful messages when graph.db is missing).
    """
    instance_id = state.sample_id or "unknown"
    metadata = state.metadata or {}
    repo = metadata.get("repo", "")

    gt_index = _find_gt_index()
    if gt_index is None:
        print(
            f"[GT] WARNING: gt-index binary not found for {instance_id}. "
            f"GT tools will return fallback messages."
        )
        return state

    # Determine repo root. In Docker sandbox, the repo is typically cloned
    # to /workspace or /testbed. We try several conventions.
    repo_root = None
    for candidate in ["/workspace", "/testbed", f"/tmp/repos/{repo}", "."]:
        if os.path.isdir(candidate):
            # Check it has source files (not just an empty dir)
            has_files = any(
                f.endswith((".py", ".go", ".js", ".ts", ".java", ".rs"))
                for f in os.listdir(candidate)
                if os.path.isfile(os.path.join(candidate, f))
            )
            if has_files:
                repo_root = candidate
                break
            # Also check one level deep for src/ or lib/
            for subdir in ["src", "lib", repo.split("/")[-1] if "/" in repo else ""]:
                sub = os.path.join(candidate, subdir)
                if os.path.isdir(sub):
                    repo_root = candidate
                    break
            if repo_root:
                break

    if repo_root is None:
        # Default to /workspace which is the standard SWE-bench mount
        repo_root = "/workspace"
        if not os.path.isdir(repo_root):
            print(f"[GT] WARNING: No repo root found for {instance_id}. Skipping index.")
            return state

    # Create output directory
    output_dir = os.path.join(GT_OUTPUT_DIR, instance_id.replace("/", "_"))
    os.makedirs(output_dir, exist_ok=True)
    db_path = os.path.join(output_dir, "graph.db")

    # Run gt-index
    start = time.monotonic()
    try:
        result = subprocess.run(
            [gt_index, "-root", repo_root, "-output", db_path],
            capture_output=True,
            text=True,
            timeout=300,  # 5 min max for indexing
        )
        elapsed = time.monotonic() - start

        if result.returncode == 0 and os.path.exists(db_path):
            os.environ["GT_GRAPH_DB"] = db_path
            # Log index stats
            try:
                import sqlite3

                conn = sqlite3.connect(db_path)
                node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
                conn.close()
                print(
                    f"[GT] Indexed {instance_id}: {node_count} nodes, "
                    f"{edge_count} edges in {elapsed:.1f}s -> {db_path}"
                )
            except Exception:
                print(f"[GT] Indexed {instance_id} in {elapsed:.1f}s -> {db_path}")

            # Store indexing metadata for post-run analysis
            state.metadata = state.metadata or {}
            state.metadata["gt_graph_db"] = db_path
            state.metadata["gt_index_time_s"] = round(elapsed, 2)
        else:
            print(
                f"[GT] WARNING: gt-index failed for {instance_id} "
                f"(rc={result.returncode}, {elapsed:.1f}s)"
            )
            if result.stderr:
                # Print first 500 chars of stderr for debugging
                print(f"[GT] stderr: {result.stderr[:500]}")

    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        print(f"[GT] WARNING: gt-index timed out for {instance_id} after {elapsed:.1f}s")
    except FileNotFoundError:
        print(f"[GT] WARNING: gt-index binary not executable: {gt_index}")
    except Exception as exc:
        print(f"[GT] WARNING: gt-index error for {instance_id}: {exc}")

    return state


# ---------------------------------------------------------------------------
# on_sample_end: Collect metrics
# ---------------------------------------------------------------------------


async def on_sample_end(state: TaskState) -> TaskState:
    """Collect GT utilization metrics after the agent finishes.

    Writes a JSON metrics file per sample and updates state.metadata with
    summary statistics.
    """
    instance_id = state.sample_id or "unknown"
    metadata = state.metadata or {}
    db_path = metadata.get("gt_graph_db")

    metrics: dict[str, Any] = {
        "instance_id": instance_id,
        "gt_indexed": db_path is not None and os.path.exists(db_path) if db_path else False,
        "gt_index_time_s": metadata.get("gt_index_time_s", 0),
    }

    # Count GT tool usage from the conversation
    gt_tool_calls = 0
    gt_tool_names: dict[str, int] = {}
    total_tool_calls = 0

    if state.messages:
        for msg in state.messages:
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    total_tool_calls += 1
                    tool_name = tc.function if hasattr(tc, "function") else str(tc.id)
                    if tool_name.startswith("groundtruth_"):
                        gt_tool_calls += 1
                        gt_tool_names[tool_name] = gt_tool_names.get(tool_name, 0) + 1

    metrics["total_tool_calls"] = total_tool_calls
    metrics["gt_tool_calls"] = gt_tool_calls
    metrics["gt_tool_breakdown"] = gt_tool_names
    metrics["gt_utilization_rate"] = (
        round(gt_tool_calls / total_tool_calls, 3) if total_tool_calls > 0 else 0.0
    )

    # Get index stats if graph.db exists
    if db_path and os.path.exists(db_path):
        try:
            import sqlite3

            conn = sqlite3.connect(db_path)
            metrics["graph_nodes"] = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            metrics["graph_edges"] = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

            # Edge confidence distribution
            try:
                cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()
                }
                if "confidence" in cols:
                    avg_conf = conn.execute(
                        "SELECT AVG(confidence) FROM edges WHERE confidence > 0"
                    ).fetchone()[0]
                    high_conf = conn.execute(
                        "SELECT COUNT(*) FROM edges WHERE confidence >= 0.7"
                    ).fetchone()[0]
                    total_edges = metrics["graph_edges"]
                    metrics["avg_edge_confidence"] = round(avg_conf or 0, 3)
                    metrics["high_confidence_ratio"] = (
                        round(high_conf / total_edges, 3) if total_edges > 0 else 0.0
                    )
            except Exception:
                pass

            conn.close()
        except Exception as exc:
            metrics["graph_error"] = str(exc)

    # Write metrics file
    output_dir = os.path.join(GT_OUTPUT_DIR, instance_id.replace("/", "_"))
    os.makedirs(output_dir, exist_ok=True)
    metrics_path = os.path.join(output_dir, "gt_metrics.json")
    try:
        with open(metrics_path, "w") as f:
            json.dump(metrics, f, indent=2)
        print(
            f"[GT] Metrics for {instance_id}: "
            f"{gt_tool_calls}/{total_tool_calls} GT calls, "
            f"utilization={metrics['gt_utilization_rate']:.1%}"
        )
    except Exception as exc:
        print(f"[GT] WARNING: Failed to write metrics for {instance_id}: {exc}")

    # Update state metadata
    state.metadata = state.metadata or {}
    state.metadata["gt_metrics"] = metrics

    return state
