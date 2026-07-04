"""Fail-closed diagnostic gate for Stage-1 localization artifacts.

This script is intentionally artifact-only: it reads persisted substrate
``summary.json`` / ``brief_result.json`` outputs and refuses to silently accept
missing proof, empty candidates, zero-evidence delivered files, or non-source
leakage. It also classifies each miss as ranking vs recall/evidence so ranking
bugs do not get mislabeled as localization recall bugs.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


EVIDENCE_KEYS = ("lex", "sem", "path", "reach", "anchor_prox", "witness", "code_def", "frame")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


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


def _class_scores(rec: dict[str, Any]) -> dict[str, float]:
    comps = _components(rec)
    return {
        "lexical": _num(comps.get("lex")) + _num(comps.get("code_def")),
        "semantic": _num(comps.get("sem")),
        "structural": (
            _num(comps.get("reach"))
            + _num(comps.get("anchor_prox"))
            + _num(comps.get("witness"))
            + (1.0 if rec.get("witness_verified") else 0.0)
        ),
        "path": _num(comps.get("path")),
        "historical": _num(comps.get("frame")),
    }


def _evidence_sum(rec: dict[str, Any]) -> float:
    return sum(_class_scores(rec).values())


def _class_count(rec: dict[str, Any]) -> int:
    return sum(1 for value in _class_scores(rec).values() if value > 0.0)


def _has_any_component_key(rec: dict[str, Any]) -> bool:
    comps = _components(rec)
    return any(key in comps for key in EVIDENCE_KEYS)


def _is_non_source_or_test(path: str) -> bool:
    p = _norm(path).lower()
    parts = [part for part in p.split("/") if part]
    name = parts[-1] if parts else p
    if not p:
        return True
    if name.endswith((".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".lock")):
        return True
    if name.endswith((".test-d.ts", ".test-d.tsx", ".spec-d.ts", ".spec-d.tsx")):
        return True
    if any(part in {"test", "tests", "testing", "__tests__", "fixtures", "fixture"} for part in parts):
        return True
    if any(part in {"examples", "example", "demo", "demos", "benchmark", "benchmarks"} for part in parts):
        return True
    if any(part in {"node_modules", "vendor", "third_party", "site-packages"} for part in parts):
        return True
    if "dts-test" in parts:
        return True
    return False


def _load_summary(input_path: Path) -> dict[str, Any]:
    if input_path.is_dir():
        input_path = input_path / "summary.json"
    data = _read_json(input_path)
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ValueError(f"expected Stage-1 summary with rows: {input_path}")
    return data


def _load_brief_result(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    brief = row.get("brief") if isinstance(row.get("brief"), dict) else {}
    path = brief.get("brief_result")
    if not path:
        return None, ""
    brief_path = Path(str(path))
    if not brief_path.exists():
        return None, str(brief_path)
    data = _read_json(brief_path)
    return data if isinstance(data, dict) else None, str(brief_path)


def _gold_rank(proof: list[dict[str, Any]], gold_files: list[str]) -> int | None:
    gold = {_norm(path) for path in gold_files}
    for idx, rec in enumerate(proof, start=1):
        if _norm(str(rec.get("path", ""))) in gold:
            return idx
    return None


def _gold_record(proof: list[dict[str, Any]], gold_files: list[str]) -> dict[str, Any] | None:
    gold = {_norm(path) for path in gold_files}
    for rec in proof:
        if _norm(str(rec.get("path", ""))) in gold:
            return rec
    return None


def _classify(
    *,
    proof: list[dict[str, Any]],
    gold_files: list[str],
    gold_rank: int | None,
    metrics: dict[str, Any],
) -> list[str]:
    labels: list[str] = []
    if not proof:
        return ["missing_or_empty_proof"]
    if any(_evidence_sum(rec) <= 0.0 for rec in proof):
        labels.append("zero_evidence_delivered")
    if any(_is_non_source_or_test(str(rec.get("path", ""))) for rec in proof):
        labels.append("non_source_or_test_delivered")
    sem_count = int(_num(metrics.get("semantic_signal_count")))
    if sem_count <= 0:
        labels.append("semantic_missing")
    if not gold_files:
        labels.append("no_gold_oracle")
        return labels
    gold = _gold_record(proof, gold_files)
    if gold_rank is None:
        labels.append("recall_miss_gold_absent")
    elif gold_rank == 1:
        labels.append("localized_at_1")
    else:
        labels.append("ranking_miss_gold_present")
    if gold is not None and _evidence_sum(gold) <= 0.0:
        labels.append("gold_zero_evidence")
    top = proof[0]
    if gold is not None and gold_rank and gold_rank > 1:
        top_classes = _class_scores(top)
        gold_classes = _class_scores(gold)
        if top_classes["structural"] > gold_classes["structural"] and (
            gold_classes["semantic"] >= top_classes["semantic"]
            or gold_classes["lexical"] >= top_classes["lexical"]
        ):
            labels.append("graph_or_structural_overreach")
        if top_classes["path"] > gold_classes["path"]:
            labels.append("path_overreach")
    return labels


def diagnose(summary: dict[str, Any], *, require_gold: bool, require_semantic: bool) -> dict[str, Any]:
    rows_out: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    label_counts: Counter[str] = Counter()
    by_language: dict[str, Counter[str]] = defaultdict(Counter)

    for row in summary.get("rows", []):
        if not isinstance(row, dict):
            continue
        case_id = str(row.get("id") or row.get("instance_id") or "")
        language = str(row.get("language") or "unknown")
        gold_files = [str(path) for path in row.get("gold_files", [])]
        brief = row.get("brief") if isinstance(row.get("brief"), dict) else {}
        row_violations: list[str] = []
        brief_result, brief_result_path = _load_brief_result(row)

        if not brief.get("ok"):
            row_violations.append("BRIEF_NOT_OK")
        if require_gold and not gold_files:
            row_violations.append("GOLD_ORACLE_MISSING")
        if brief_result is None:
            row_violations.append("BRIEF_RESULT_MISSING")
            proof: list[dict[str, Any]] = []
            metrics: dict[str, Any] = {}
        else:
            metrics = brief_result.get("metrics", {}) if isinstance(brief_result.get("metrics"), dict) else {}
            proof_raw = metrics.get("localization_proof")
            proof = [rec for rec in proof_raw if isinstance(rec, dict)] if isinstance(proof_raw, list) else []
            if not isinstance(proof_raw, list):
                row_violations.append("LOCALIZATION_PROOF_MISSING")
            elif not proof:
                row_violations.append("LOCALIZATION_PROOF_EMPTY")

        rendered = int(_num(metrics.get("rendered_candidate_count"))) if metrics else 0
        if proof and rendered and rendered != len(proof):
            row_violations.append("RENDERED_PROOF_COUNT_MISMATCH")
        if proof and any(not str(rec.get("path", "")) for rec in proof):
            row_violations.append("CANDIDATE_PATH_EMPTY")
        zero_evidence_paths = [
            _norm(str(rec.get("path", "")))
            for rec in proof
            if _evidence_sum(rec) <= 0.0 or not _has_any_component_key(rec)
        ]
        if zero_evidence_paths:
            row_violations.append("ZERO_EVIDENCE_DELIVERED")
        non_source_paths = [
            _norm(str(rec.get("path", "")))
            for rec in proof
            if _is_non_source_or_test(str(rec.get("path", "")))
        ]
        if non_source_paths:
            row_violations.append("NON_SOURCE_OR_TEST_DELIVERED")
        sem_count = int(_num(metrics.get("semantic_signal_count"))) if metrics else 0
        sem_components = metrics.get("sem_components") if metrics else None
        if sem_components is None or not isinstance(sem_components, list):
            row_violations.append("SEMANTIC_COMPONENT_LOG_MISSING")
        if require_semantic and proof and sem_count <= 0:
            row_violations.append("SEMANTIC_SIGNAL_ZERO")

        rank = _gold_rank(proof, gold_files) if gold_files else None
        gold = _gold_record(proof, gold_files) if gold_files else None
        if require_gold and gold_files and rank is None:
            row_violations.append("GOLD_ABSENT_FROM_DELIVERED_PROOF")
        if gold is not None and _evidence_sum(gold) <= 0.0:
            row_violations.append("GOLD_ZERO_EVIDENCE")

        labels = _classify(proof=proof, gold_files=gold_files, gold_rank=rank, metrics=metrics)
        label_counts.update(labels)
        by_language[language].update(labels)
        for violation in row_violations:
            violations.append({"id": case_id, "language": language, "violation": violation})

        top = proof[0] if proof else {}
        rows_out.append(
            {
                "id": case_id,
                "language": language,
                "brief_result": brief_result_path,
                "gold_files": gold_files,
                "gold_rank": rank,
                "labels": labels,
                "violations": row_violations,
                "rendered_candidate_count": rendered,
                "proof_count": len(proof),
                "semantic_signal_count": sem_count,
                "zero_evidence_paths": zero_evidence_paths,
                "non_source_or_test_paths": non_source_paths,
                "top": {
                    "path": top.get("path", ""),
                    "class_count": _class_count(top) if top else 0,
                    "evidence_sum": round(_evidence_sum(top), 6) if top else 0.0,
                    "classes": _class_scores(top) if top else {},
                },
                "gold": {
                    "path": gold.get("path", "") if gold else "",
                    "class_count": _class_count(gold) if gold else 0,
                    "evidence_sum": round(_evidence_sum(gold), 6) if gold else 0.0,
                    "classes": _class_scores(gold) if gold else {},
                },
            }
        )

    return {
        "schema": "gt.localization_surface_diagnostic.v1",
        "strict_contract": {
            "require_gold": require_gold,
            "require_semantic": require_semantic,
            "zero_evidence_delivered_is_violation": True,
            "missing_localization_proof_is_violation": True,
            "non_source_or_test_delivered_is_violation": True,
        },
        "counts": {
            "cases": len(rows_out),
            "violations": len(violations),
            "labels": dict(sorted(label_counts.items())),
            "by_language": {
                lang: dict(sorted(counter.items()))
                for lang, counter in sorted(by_language.items())
            },
        },
        "violations": violations,
        "rows": rows_out,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Stage-1 result dir or summary.json")
    parser.add_argument("--out", required=True, help="diagnostic JSON report")
    parser.add_argument("--strict", action="store_true", help="exit nonzero when any diagnostic violation is present")
    parser.add_argument("--require-gold", action="store_true", help="gold oracle must exist and be delivered")
    parser.add_argument("--require-semantic", action="store_true", help="semantic_signal_count must be positive for delivered candidates")
    parser.add_argument(
        "--full-potential",
        action="store_true",
        help="strict substrate gate: require gold, require semantic, and halt on any violation",
    )
    args = parser.parse_args()

    strict = args.strict or args.full_potential
    require_gold = args.require_gold or args.full_potential
    require_semantic = args.require_semantic or args.full_potential

    report = diagnose(_load_summary(Path(args.input)), require_gold=require_gold, require_semantic=require_semantic)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report["counts"], indent=2, sort_keys=True))
    if strict and report["counts"]["violations"]:
        print(
            f"GT localization diagnostic HALT: {report['counts']['violations']} violations; see {out}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
