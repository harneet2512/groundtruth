"""Live localization diagnostics for substrate proof runs.

This module validates the persisted ``brief_result.json`` payload before a
brief is delivered to an agent. It is deliberately small and dependency-free so
proof entrypoints can fail closed instead of emitting blind/empty localization
artifacts and discovering the issue only in post-run analysis.
"""

from __future__ import annotations

from typing import Any


EVIDENCE_KEYS = ("lex", "sem", "path", "reach", "anchor_prox", "witness", "code_def", "frame")


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("./").lstrip("/")


def _num(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except Exception:
        return 0.0


def _components(rec: dict[str, Any]) -> dict[str, Any]:
    comps = rec.get("components")
    return comps if isinstance(comps, dict) else {}


def evidence_sum(rec: dict[str, Any]) -> float:
    comps = _components(rec)
    total = 0.0
    for key in EVIDENCE_KEYS:
        total += _num(comps.get(key))
    if rec.get("witness_verified"):
        total += 1.0
    return total


def _has_any_component_key(rec: dict[str, Any]) -> bool:
    comps = _components(rec)
    return any(key in comps for key in EVIDENCE_KEYS)


def is_non_source_or_test(path: str) -> bool:
    p = _norm(path).lower()
    parts = [part for part in p.split("/") if part]
    name = parts[-1] if parts else p
    if not p:
        return True
    if name.endswith((".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".lock")):
        return True
    if name.endswith((".test-d.ts", ".test-d.tsx", ".spec-d.ts", ".spec-d.tsx")):
        return True
    if any(
        part in {"test", "tests", "testing", "__tests__", "fixtures", "fixture"} for part in parts
    ):
        return True
    if any(
        part in {"examples", "example", "demo", "demos", "benchmark", "benchmarks"}
        for part in parts
    ):
        return True
    if any(part in {"node_modules", "vendor", "third_party", "site-packages"} for part in parts):
        return True
    if "dts-test" in parts:
        return True
    return False


def _gold_rank(proof: list[dict[str, Any]], gold_files: list[str]) -> int | None:
    gold = {_norm(path) for path in gold_files}
    for idx, rec in enumerate(proof, start=1):
        if _norm(str(rec.get("path", ""))) in gold:
            return idx
    return None


def validate_brief_payload(
    payload: dict[str, Any],
    *,
    gold_files: list[str] | None = None,
    require_gold: bool = False,
    require_semantic: bool = False,
) -> dict[str, Any]:
    """Return a diagnostic report with fail-closed violations.

    ``payload`` is the persisted brief-cache object:
    ``{brief_text, metrics: {localization_proof, ...}}``.
    """
    metrics = payload.get("metrics", {}) if isinstance(payload.get("metrics"), dict) else {}
    proof_raw = metrics.get("localization_proof")
    proof = (
        [rec for rec in proof_raw if isinstance(rec, dict)] if isinstance(proof_raw, list) else []
    )
    gold = [str(path) for path in (gold_files or []) if str(path).strip()]

    violations: list[str] = []
    warnings: list[str] = []

    if not isinstance(proof_raw, list):
        violations.append("LOCALIZATION_PROOF_MISSING")
    elif not proof:
        violations.append("LOCALIZATION_PROOF_EMPTY")

    if not (payload.get("brief_text") or "").strip():
        violations.append("BRIEF_TEXT_EMPTY")

    rendered = int(_num(metrics.get("rendered_candidate_count")))
    if proof and rendered and rendered != len(proof):
        violations.append("RENDERED_PROOF_COUNT_MISMATCH")

    zero_evidence_paths = [
        _norm(str(rec.get("path", "")))
        for rec in proof
        if evidence_sum(rec) <= 0.0 or not _has_any_component_key(rec)
    ]
    if zero_evidence_paths:
        violations.append("ZERO_EVIDENCE_DELIVERED")

    non_source_paths = [
        _norm(str(rec.get("path", "")))
        for rec in proof
        if is_non_source_or_test(str(rec.get("path", "")))
    ]
    if non_source_paths:
        violations.append("NON_SOURCE_OR_TEST_DELIVERED")

    missing_route_paths = [
        _norm(str(rec.get("path", "")))
        for rec in proof
        if evidence_sum(rec) > 0.0 and not str(rec.get("entered_via", "") or "").strip()
    ]
    if missing_route_paths:
        violations.append("CANDIDATE_ROUTE_MISSING")

    sem_components = metrics.get("sem_components")
    if not isinstance(sem_components, list):
        violations.append("SEMANTIC_COMPONENT_LOG_MISSING")

    sem_count = int(_num(metrics.get("semantic_signal_count")))
    if proof and sem_count <= 0:
        if require_semantic:
            violations.append("SEMANTIC_SIGNAL_ZERO")
        else:
            warnings.append("SEMANTIC_SIGNAL_ZERO")

    gold_rank = _gold_rank(proof, gold) if gold else None
    if require_gold and not gold:
        violations.append("GOLD_ORACLE_MISSING")
    if require_gold and gold and gold_rank is None:
        violations.append("GOLD_ABSENT_FROM_DELIVERED_PROOF")

    return {
        "schema": "gt.live_localization_diagnostic.v1",
        "ok": not violations,
        "violations": violations,
        "warnings": warnings,
        "metrics": {
            "rendered_candidate_count": rendered,
            "proof_count": len(proof),
            "semantic_signal_count": sem_count,
            "gold_rank": gold_rank,
            "zero_evidence_count": len(zero_evidence_paths),
            "non_source_or_test_count": len(non_source_paths),
            "missing_route_count": len(missing_route_paths),
        },
        "zero_evidence_paths": zero_evidence_paths,
        "non_source_or_test_paths": non_source_paths,
        "missing_route_paths": missing_route_paths,
        "gold_files": gold,
    }
