#!/usr/bin/env python3
"""Shared witness-over-cert reconciliation (gt_gt.md §12). P1-08."""
from __future__ import annotations

from typing import Any


def reconcile_graph_handoff(signal: dict[str, Any]) -> dict[str, Any]:
    """Reconcile graph cert vs runtime witness — single authority for task_truth + outcome."""
    witness_holds = (
        signal.get("gt_prebuilt_active") is True
        and signal.get("hook_hash_match") is True
    )
    cert_verdicts = signal.get("cert_verdicts") or {}
    graph_cert = cert_verdicts.get("graph_certificate.json") or {}
    graph_verdict = graph_cert.get("verdict") or ""
    contradictions: list[str] = []

    if graph_cert.get("is_fail") and graph_verdict == "GRAPH_FAIL_MISSING_HANDOFF":
        if witness_holds:
            status = "witness_overrides"
        else:
            status = "fail"
            contradictions.append("GRAPH_FAIL_MISSING_HANDOFF without runtime witness")
    elif graph_cert.get("is_fail"):
        status = "fail"
        contradictions.append(f"graph cert fail: {graph_verdict}")
    elif witness_holds:
        status = "pass"
    elif signal.get("gt_prebuilt_active") is False:
        status = "fail"
        contradictions.append("gt_prebuilt_active=false")
    elif not signal.get("gt_meta_present"):
        status = "unproven"
        contradictions.append("no [GT_META] witness")
    else:
        status = "pass"

    return {
        "graph_handoff": status,
        "witness_holds": witness_holds,
        "contradictions": contradictions,
    }
