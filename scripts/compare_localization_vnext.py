#!/usr/bin/env python
"""Offline sealed old/new localization comparison.

Gold fields are intentionally loaded in a second manifest pass, after every
localization result is sealed to disk.  Product code receives only issue, repo,
graph, revision, language, and split.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

for _thread_env in (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_thread_env, "1")

from groundtruth.pretask.localization_vnext.comparison import (  # noqa: E402
    evaluate_winner,
    run_sealed_case,
    score_sealed_case,
)


def _read_cases(manifest: Path) -> list[dict[str, Any]]:
    if manifest.is_dir():
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(manifest.glob("*.json"))
        ]
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    if "id" in payload and "issue_text" in payload:
        return [payload]
    return list(payload.get("cases") or payload.get("rows") or [])


def _infer_split(case: dict[str, Any]) -> str:
    explicit = str(case.get("split") or "").strip()
    if explicit:
        return explicit
    case_id = str(case.get("id") or "")
    if case_id.startswith("ext2_"):
        return "ext2"
    if case_id.startswith("held_"):
        return "held"
    if case_id.startswith(("rnd_", "random_")):
        return "random"
    return str(case.get("category") or "unknown")


def _input_pass(manifest: Path, graph_root: Path | None) -> list[dict[str, Any]]:
    """Return an allowlisted input view; never retain a gold-bearing case dict."""
    rows: list[dict[str, Any]] = []
    for raw in _read_cases(manifest):
        case_id = str(raw["id"])
        repository_root = str(
            raw.get("repository_root")
            or raw.get("repo_root")
            or raw.get("repo_dir")
            or ""
        )
        graph = raw.get("graph_db")
        if not graph and graph_root is not None:
            candidates = [
                graph_root / f"{case_id}.db",
                graph_root / case_id / "graph.db",
            ]
            if repository_root:
                candidates.extend(
                    sorted(
                        graph_root.glob(
                            f"{Path(repository_root).name}_*/graph.db"
                        )
                    )
                )
            graph = next((str(path) for path in candidates if path.is_file()), "")
        rows.append(
            {
                "id": case_id,
                "issue_text": str(raw.get("issue_text") or ""),
                "repository_root": repository_root,
                "graph_db": str(graph or ""),
                "revision_identity": str(
                    raw.get("revision_identity") or raw.get("base_commit") or "unknown"
                ),
                "language": str(raw.get("language") or "unknown").lower(),
                "split": _infer_split(raw),
            }
        )
    return rows


def _gold_pass(manifest: Path) -> dict[str, dict[str, Any]]:
    """Second manifest read. Called only after all sealed files exist."""
    out: dict[str, dict[str, Any]] = {}
    for raw in _read_cases(manifest):
        out[str(raw["id"])] = {
            "gold_files": list(raw.get("gold_files") or []),
            "gold_symbols": list(raw.get("gold_symbols") or []),
            "gold_line_ranges": list(raw.get("gold_line_ranges") or []),
            "fix_commit": raw.get("fix_commit"),
            "fix_commit_sha256": raw.get("fix_commit_sha256"),
            "patch_sha256": raw.get("patch_sha256"),
        }
    return out


def _json_ready(value: Any) -> Any:
    if isinstance(value, float):
        return f"{value:.8f}"
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(_json_ready(payload), indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--graph-root")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--case-id", action="append", default=[])
    args = parser.parse_args()

    manifest = Path(args.manifest)
    out_root = Path(args.out)
    graph_root = Path(args.graph_root) if args.graph_root else None
    inputs = _input_pass(manifest, graph_root)
    if args.case_id:
        selected = set(args.case_id)
        inputs = [row for row in inputs if row["id"] in selected]
    if args.max_cases > 0:
        inputs = inputs[: args.max_cases]

    sealed_rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for case_input in inputs:
        case_id = case_input["id"]
        repo = Path(case_input["repository_root"])
        graph = Path(case_input["graph_db"])
        if not repo.is_dir() or not graph.is_file():
            failures.append(
                {
                    "id": case_id,
                    "reason": "missing_repository_or_graph",
                    "repository_available": str(repo.is_dir()),
                    "graph_available": str(graph.is_file()),
                }
            )
            continue
        try:
            sealed = run_sealed_case(case_input, repeats=max(3, args.repeats))
            sealed_rows.append(sealed)
            _write(out_root / "sealed" / f"{case_id}.json", sealed)
            print(f"SEALED {case_id}")
        except Exception as exc:  # comparison must preserve raw per-case failure
            failures.append({"id": case_id, "reason": repr(exc)})
            print(f"FAILED {case_id}: {exc!r}", file=sys.stderr)

    # Gold is not read until every successful result is sealed on disk.
    gold_by_id = _gold_pass(manifest)
    paired: list[dict[str, Any]] = []
    for sealed in sealed_rows:
        case_id = sealed["case"]["id"]
        scored = score_sealed_case(sealed, gold_by_id.get(case_id, {}))
        scored["gold_provenance"] = {
            key: gold_by_id.get(case_id, {}).get(key)
            for key in ("fix_commit", "fix_commit_sha256", "patch_sha256")
        }
        paired.append(scored)
        _write(out_root / "paired" / f"{case_id}.json", scored)

    verdict = evaluate_winner(paired)
    if failures:
        verdict = {
            "verdict": "INCONCLUSIVE",
            "reason": "infrastructure_prevented_fair_paired_run",
            "provisional_gate_result": verdict,
            "failure_count": len(failures),
        }
    report = {
        "schema": "gt.localization.vnext.comparison.v1",
        "manifest": str(manifest),
        "sealed_count": len(sealed_rows),
        "paired_count": len(paired),
        "failures": failures,
        "paired_results": paired,
        "winner": verdict,
        "thread_settings": {
            key: os.environ.get(key, "")
            for key in (
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            )
        },
        "unmeasured": [
            "agent_file_reads",
            "repair_accuracy",
            "live_behavioral_causality",
        ],
    }
    _write(out_root / "COMPARISON.json", report)
    print(json.dumps(_json_ready(verdict), indent=2, sort_keys=True))
    return 0 if verdict.get("verdict") in {"NEW_WINS", "TIE"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
