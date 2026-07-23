"""Campaign feature live log — mid-run honesty channel for GitHub Actions.

Writes compact JSONL lines for DIRECT / control decisions. Vocabulary is locked
to DELIVERED | HOLD | SUPPRESSED | ERROR | NOT_ELIGIBLE. Never logs LIVE because
a Profile flag is ON.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping

DEFAULT_FEATURE_LIVE_ENV = "GT_CAMPAIGN_FEATURE_LIVE"
DEFAULT_FEATURE_LIVE_PATH = "/tmp/gt_out/gt_campaign_feature_live.jsonl"

_ALLOWED_STAGES = frozenset({
    "DELIVERED",
    "HOLD",
    "SUPPRESSED",
    "ERROR",
    "NOT_ELIGIBLE",
})


def resolve_feature_live_path(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return os.environ.get(DEFAULT_FEATURE_LIVE_ENV, DEFAULT_FEATURE_LIVE_PATH)


def stage_from_ledger_row(row: Mapping[str, Any]) -> str | None:
    """Map a durable ledger row onto the locked live-log vocabulary."""
    if not isinstance(row, Mapping):
        return None
    outcome = str(row.get("outcome") or "")
    reason = str(row.get("reason") or "")
    if outcome == "delivered":
        try:
            chars = int(row.get("chars_delivered") or 0)
        except (TypeError, ValueError):
            chars = 0
        if chars > 0 and isinstance(row.get("content_sha256_16"), str):
            return "DELIVERED"
        return "ERROR"  # delivered label without seal/bytes = broken
    if outcome == "measurement_failed":
        return "ERROR"
    if outcome.startswith("suppressed"):
        # Explicit covering / syntax holds ride suppress with named reasons.
        if reason in {
            "covering_empty_sub_fact_floor",
            "covering_no_covering",
            "syntax_ok",
            "syntax_unavailable",
            "submit_clean",
            "no_opportunity",
        }:
            return "HOLD"
        return "SUPPRESSED"
    if outcome in {"allow", "submit_clean"} and reason in {"clean", "submit_clean"}:
        return "NOT_ELIGIBLE"
    return None


def append_feature_live(
    *,
    feature_id: str,
    stage: str,
    reason: str = "",
    role: str = "",
    iteration: int | None = None,
    candidate_id: str = "",
    extra: Mapping[str, Any] | None = None,
    path: str | None = None,
) -> None:
    """Best-effort append one feature-live line. Never raises into the agent loop."""
    try:
        stage_u = str(stage or "").upper()
        if stage_u not in _ALLOWED_STAGES:
            return
        fid = str(feature_id or "").strip()
        if not fid:
            return
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "feature_id": fid,
            "stage": stage_u,
            "reason": str(reason or ""),
        }
        if role:
            entry["role"] = str(role)
        if iteration is not None:
            entry["iteration"] = int(iteration)
        if candidate_id:
            entry["candidate_id"] = str(candidate_id)
        if extra:
            for k, v in extra.items():
                entry.setdefault(str(k), v)
        sink = resolve_feature_live_path(path)
        parent = os.path.dirname(sink)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(sink, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — telemetry must never break the agent
        pass


def mirror_ledger_row_to_feature_live(row: Mapping[str, Any]) -> None:
    """Project a durable ledger row into the feature-live channel when classifiable."""
    try:
        from groundtruth.runtime.direct_live import (
            DIRECT_IDS,
            direct_id_from_row,
        )
    except Exception:
        return
    stage = stage_from_ledger_row(row)
    if stage is None:
        return
    fid = direct_id_from_row(row)
    if fid is None:
        # Still log named HOLD reasons for covering even when fact_class absent.
        reason = str(row.get("reason") or "")
        if reason == "covering_empty_sub_fact_floor":
            fid = "covering_red"
        elif reason == "syntax_ok":
            fid = "syntax_result"
        elif reason in {"submit_clean", "clean"} and str(row.get("layer") or "").startswith("submit"):
            fid = "submit_refusal"
        else:
            return
    if fid not in DIRECT_IDS and fid not in {"covering_red", "syntax_result", "submit_refusal"}:
        return
    append_feature_live(
        feature_id=fid,
        stage=stage,
        reason=str(row.get("reason") or ""),
        role="direct",
        iteration=row.get("iteration") if isinstance(row.get("iteration"), int) else None,
        candidate_id=str(row.get("candidate_id") or ""),
        extra={
            "layer": row.get("layer"),
            "outcome": row.get("outcome"),
            "chars_delivered": row.get("chars_delivered"),
        },
    )


__all__ = [
    "DEFAULT_FEATURE_LIVE_ENV",
    "DEFAULT_FEATURE_LIVE_PATH",
    "append_feature_live",
    "mirror_ledger_row_to_feature_live",
    "resolve_feature_live_path",
    "stage_from_ledger_row",
]
