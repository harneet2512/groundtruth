"""Replay Stage-1 localization rankings from frozen brief artifacts.

This diagnostic does not call the product pipeline. It reads persisted
``brief_result.json`` / summary artifacts and recomputes alternative rankings
from the already-emitted localization proof. That makes it suitable for
held-out OSS repos and locked manifests without giving the ranker another
chance to adapt to the task.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


CLASS_KEYS = ("lexical", "semantic", "structural", "path", "historical")


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("./").lstrip("/")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _num(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except Exception:
        return 0.0


def _classes(rec: dict[str, Any]) -> dict[str, float]:
    comps = rec.get("components") if isinstance(rec.get("components"), dict) else {}
    structural = (
        _num(comps.get("reach"))
        + _num(comps.get("anchor_prox"))
        + _num(comps.get("witness"))
        + (1.0 if rec.get("witness_verified") else 0.0)
    )
    return {
        "lexical": _num(comps.get("lex")) + _num(comps.get("code_def")),
        "semantic": _num(comps.get("sem")),
        "structural": structural,
        "path": _num(comps.get("path")),
        "historical": _num(comps.get("frame")),
    }


def _evidence_sum(rec: dict[str, Any]) -> float:
    return sum(_classes(rec).values())


def _class_count(rec: dict[str, Any]) -> int:
    return sum(1 for value in _classes(rec).values() if value > 0.0)


def _rrf_scores(candidates: list[dict[str, Any]], k: int = 60) -> dict[int, float]:
    scores = {idx: 0.0 for idx in range(len(candidates))}
    for key in CLASS_KEYS:
        active: list[tuple[int, float]] = []
        for idx, rec in enumerate(candidates):
            value = _classes(rec).get(key, 0.0)
            if value > 0.0:
                active.append((idx, value))
        active.sort(key=lambda item: (-item[1], item[0]))
        for rank, (idx, _value) in enumerate(active, start=1):
            scores[idx] += 1.0 / float(k + rank)
    return scores


def _ranked_paths(candidates: list[dict[str, Any]], method: str) -> list[str]:
    indexed = list(enumerate(candidates))
    if method == "current":
        ranked = indexed
    elif method == "evidence_sum":
        ranked = sorted(indexed, key=lambda item: (-_evidence_sum(item[1]), item[0]))
    elif method == "combmnz":
        ranked = sorted(indexed, key=lambda item: (-(_evidence_sum(item[1]) * _class_count(item[1])), item[0]))
    elif method == "class_primary":
        ranked = sorted(indexed, key=lambda item: (-_class_count(item[1]), -_evidence_sum(item[1]), item[0]))
    elif method == "rrf":
        rrf = _rrf_scores(candidates)
        ranked = sorted(indexed, key=lambda item: (-rrf[item[0]], item[0]))
    else:
        raise ValueError(f"unknown method: {method}")
    return [_norm(rec.get("path", "")) for _idx, rec in ranked]


def _gold_rank(paths: list[str], gold_files: list[str]) -> int | None:
    gold = {_norm(path) for path in gold_files}
    for idx, path in enumerate(paths, start=1):
        if _norm(path) in gold:
            return idx
    return None


def _proof_from_brief_result(path: Path) -> list[dict[str, Any]]:
    data = _read_json(path)
    metrics = data.get("metrics", {}) if isinstance(data, dict) else {}
    proof = metrics.get("localization_proof", [])
    if not isinstance(proof, list):
        return []
    return [item for item in proof if isinstance(item, dict) and item.get("path")]


def _load_cases_from_summary(summary_path: Path) -> list[dict[str, Any]]:
    summary = _read_json(summary_path)
    rows = summary.get("rows", []) if isinstance(summary, dict) else []
    cases: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        brief = row.get("brief") if isinstance(row.get("brief"), dict) else {}
        brief_result = brief.get("brief_result")
        if not brief_result:
            continue
        cases.append(
            {
                "id": str(row.get("id") or row.get("instance_id") or Path(brief_result).parent.name),
                "language": row.get("language"),
                "gold_files": [str(x) for x in row.get("gold_files", [])],
                "brief_result": str(brief_result),
            }
        )
    return cases


def _load_cases(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        summary = path / "summary.json"
        if summary.exists():
            return _load_cases_from_summary(summary)
        cases: list[dict[str, Any]] = []
        for brief_result in sorted(path.rglob("brief_result.json")):
            cases.append(
                {
                    "id": brief_result.parent.name,
                    "language": "",
                    "gold_files": [],
                    "brief_result": str(brief_result),
                }
            )
        return cases
    data = _read_json(path)
    if isinstance(data, dict) and "rows" in data:
        return _load_cases_from_summary(path)
    if isinstance(data, dict) and "cases" in data:
        cases = data.get("cases") or []
        out: list[dict[str, Any]] = []
        for case in cases:
            if isinstance(case, dict) and case.get("brief_result"):
                out.append(case)
        return out
    if isinstance(data, list):
        return [case for case in data if isinstance(case, dict) and case.get("brief_result")]
    raise ValueError(f"cannot load cases from {path}")


def replay(cases: list[dict[str, Any]]) -> dict[str, Any]:
    methods = ("current", "evidence_sum", "combmnz", "class_primary", "rrf")
    counts = {
        method: {"cases": 0, "gold_at_1": 0, "gold_in_8": 0, "no_gold": 0}
        for method in methods
    }
    by_language: dict[str, dict[str, dict[str, int]]] = {}
    rows: list[dict[str, Any]] = []
    for case in cases:
        proof = _proof_from_brief_result(Path(case["brief_result"]))
        gold_files = [str(path) for path in case.get("gold_files", [])]
        language = str(case.get("language") or "unknown")
        if language not in by_language:
            by_language[language] = {
                method: {"cases": 0, "gold_at_1": 0, "gold_in_8": 0, "no_gold": 0}
                for method in methods
            }
        method_ranks: dict[str, int | None] = {}
        method_top: dict[str, str] = {}
        for method in methods:
            paths = _ranked_paths(proof, method)
            rank = _gold_rank(paths, gold_files) if gold_files else None
            method_ranks[method] = rank
            method_top[method] = paths[0] if paths else ""
            counts[method]["cases"] += 1
            by_language[language][method]["cases"] += 1
            if not gold_files:
                counts[method]["no_gold"] += 1
                by_language[language][method]["no_gold"] += 1
            elif rank == 1:
                counts[method]["gold_at_1"] += 1
                counts[method]["gold_in_8"] += 1
                by_language[language][method]["gold_at_1"] += 1
                by_language[language][method]["gold_in_8"] += 1
            elif rank is not None and rank <= 8:
                counts[method]["gold_in_8"] += 1
                by_language[language][method]["gold_in_8"] += 1
        current_top = proof[0] if proof else {}
        gold_rec = None
        gold_norm = {_norm(path) for path in gold_files}
        for rec in proof:
            if _norm(rec.get("path", "")) in gold_norm:
                gold_rec = rec
                break
        rows.append(
            {
                "id": case.get("id"),
                "language": case.get("language"),
                "gold_files": gold_files,
                "candidate_count": len(proof),
                "ranks": method_ranks,
                "tops": method_top,
                "current_top": {
                    "path": current_top.get("path", ""),
                    "class_count": _class_count(current_top) if current_top else 0,
                    "evidence_sum": round(_evidence_sum(current_top), 6) if current_top else 0.0,
                    "classes": _classes(current_top) if current_top else {},
                },
                "gold": {
                    "path": gold_rec.get("path", "") if gold_rec else "",
                    "class_count": _class_count(gold_rec) if gold_rec else 0,
                    "evidence_sum": round(_evidence_sum(gold_rec), 6) if gold_rec else 0.0,
                    "classes": _classes(gold_rec) if gold_rec else {},
                },
            }
        )
    return {
        "schema": "gt.localization_ranker_replay.v1",
        "methods": list(methods),
        "counts": counts,
        "by_language": by_language,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="summary.json, result dir, or replay manifest")
    parser.add_argument("--out", required=True, help="output JSON report")
    args = parser.parse_args()

    cases = _load_cases(Path(args.input))
    report = replay(cases)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report["counts"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
