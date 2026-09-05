"""v1.0.5 per-task telemetry sinks.

Six structured per-task JSONL sinks plus a per-task summary, rooted at
``/tmp/gt_telemetry_<instance_id>/`` (overridable via GT_TELEMETRY_ROOT).

Layers:
  1. layer1_localization.jsonl  — v8.2.2 ranker output (top-5 files + top-10 funcs)
  2. layer2_brief.jsonl         — rendered brief + token count + sections
  3. layer3_hook.jsonl          — mirror of /tmp/gt_hook_log.jsonl (per-fire detail)
  4. layer4_endpoints.jsonl     — per gt_lookup/gt_impact/gt_check call
                                  (full output saved separately under
                                  layer4_endpoints_full/<call_id>.json)
  5. layer5_gate.jsonl          — per finish-attempt: edited files, coverage,
                                  intervention/allow/escape
  trajectory_full.jsonl         — written by OH SDK; we document the path
  per_task_summary.json         — rolled up at task end

All writers are best-effort (never raise) — a logging failure must never
break the run.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

LAYERS = (
    "layer1_localization",
    "layer2_brief",
    "layer3_hook",
    "layer4_endpoints",
    "layer5_gate",
    "layer6_index_freshness",
    "trajectory_full",
)

_DEFAULT_ROOT = "/tmp"


def _safe_iid(instance_id: str | None) -> str:
    if instance_id and instance_id.strip():
        return instance_id.strip().replace("/", "_").replace("..", "_")
    env = os.environ.get("GT_INSTANCE_ID", "").strip()
    if env:
        return env.replace("/", "_").replace("..", "_")
    return "global"


def telemetry_dir(instance_id: str | None = None) -> str:
    """Return the per-task telemetry directory; create it on first call."""
    root = os.environ.get("GT_TELEMETRY_ROOT", _DEFAULT_ROOT)
    iid = _safe_iid(instance_id)
    path = os.path.join(root, f"gt_telemetry_{iid}")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def layer_path(layer: str, instance_id: str | None = None) -> str:
    return os.path.join(telemetry_dir(instance_id), f"{layer}.jsonl")


def append_jsonl(layer: str, record: dict[str, Any], instance_id: str | None = None) -> None:
    """Append a record to a layer's JSONL. Stamps ts. Never raises."""
    record = dict(record)
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        path = layer_path(layer, instance_id)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def save_full_payload(call_id: str, payload: dict[str, Any], instance_id: str | None = None) -> str:
    """Save a full payload (e.g. unbounded endpoint output) under
    layer4_endpoints_full/<call_id>.json. Returns the path written, or
    empty on failure.
    """
    root = telemetry_dir(instance_id)
    sub = os.path.join(root, "layer4_endpoints_full")
    try:
        os.makedirs(sub, exist_ok=True)
        safe = call_id.replace("/", "_").replace("..", "_")
        path = os.path.join(sub, f"{safe}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
        return path
    except OSError:
        return ""


def new_call_id(prefix: str = "call") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Convenience writers — same signature shape across layers so callers can be
# brief at the call site.
# ---------------------------------------------------------------------------


def log_localization(
    *,
    instance_id: str | None = None,
    files: list[dict[str, Any]] | None = None,
    functions: list[dict[str, Any]] | None = None,
    ranker: str = "v8.2.2",
) -> None:
    append_jsonl(
        "layer1_localization",
        {"ranker": ranker, "files": files or [], "functions": functions or []},
        instance_id,
    )


def log_brief(
    *,
    instance_id: str | None = None,
    text: str = "",
    token_estimate: int | None = None,
    sections: list[str] | None = None,
    tier_counts: dict[str, int] | None = None,
) -> None:
    if token_estimate is None:
        # Cheap estimate: ~4 chars per token (gpt-style).
        token_estimate = max(1, len(text) // 4) if text else 0
    append_jsonl(
        "layer2_brief",
        {
            "text": text,
            "token_estimate": token_estimate,
            "sections": sections or [],
            "tier_counts": tier_counts or {},
        },
        instance_id,
    )


def log_endpoint(
    *,
    instance_id: str | None = None,
    endpoint: str,
    args: dict[str, Any] | None = None,
    output: str = "",
    output_truncated_chars: int = 2000,
    tier_distribution: dict[str, int] | None = None,
    budget_remaining: int | None = None,
    latency_ms: float | None = None,
) -> str:
    """Log an endpoint call. Returns the call_id used (which also names the
    full-output payload file under layer4_endpoints_full/)."""
    call_id = new_call_id(endpoint)
    save_full_payload(call_id, {"output": output, "args": args or {}}, instance_id)
    append_jsonl(
        "layer4_endpoints",
        {
            "call_id": call_id,
            "endpoint": endpoint,
            "args": args or {},
            "output_preview": (output or "")[:output_truncated_chars],
            "output_chars": len(output or ""),
            "tier_distribution": tier_distribution or {},
            "budget_remaining": budget_remaining,
            "latency_ms": latency_ms,
        },
        instance_id,
    )
    return call_id


def log_gate(
    *,
    instance_id: str | None = None,
    edited_files: list[str] | None = None,
    checked_files: list[str] | None = None,
    uncovered: list[str] | None = None,
    attempt: int = 0,
    decision: str = "",
    intervention: str = "",
) -> None:
    append_jsonl(
        "layer5_gate",
        {
            "edited_files": edited_files or [],
            "checked_files": checked_files or [],
            "uncovered": uncovered or [],
            "attempt": attempt,
            "decision": decision,
            "intervention": intervention,
        },
        instance_id,
    )


def log_hook_mirror(record: dict[str, Any], instance_id: str | None = None) -> None:
    """Mirror one /tmp/gt_hook_log.jsonl entry into layer3_hook.jsonl."""
    append_jsonl("layer3_hook", record, instance_id)


def log_index_freshness(
    *,
    instance_id: str | None = None,
    file: str = "",
    outcome: str = "",
    elapsed_ms: float | None = None,
    db_mtime_before: float | None = None,
    db_mtime_after: float | None = None,
    file_mtime: float | None = None,
    rows_updated: int | None = None,
    pre_hash: str = "",
    post_hash: str = "",
) -> None:
    """Layer-6 incremental re-indexing record.

    ``outcome`` is one of: ``fresh`` (short-circuit, db newer than file),
    ``fresh_after_reindex``, ``stale`` (reindex ran but db still behind),
    ``stale_no_indexer``, ``timeout``, ``error``.
    """
    append_jsonl(
        "layer6_index_freshness",
        {
            "file": file,
            "outcome": outcome,
            "elapsed_ms": elapsed_ms,
            "db_mtime_before": db_mtime_before,
            "db_mtime_after": db_mtime_after,
            "file_mtime": file_mtime,
            "rows_updated": rows_updated,
            "pre_hash": pre_hash,
            "post_hash": post_hash,
        },
        instance_id,
    )
