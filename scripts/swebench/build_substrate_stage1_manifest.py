"""Build a Stage-1 substrate-localization manifest.

This is a measurement helper, not a scorer. It freezes the substrate product
surface before any localization-ranker change:

  task repo + issue text -> gt-run-proof -> brief_result.json / brief.txt

Gold files are never inferred here. Submitted patch files from a diagnostic run
are emitted as hints only, so they cannot accidentally become the acceptance
oracle.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _patch_files_from_preds(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = _read_json(path)
    except Exception:
        return []
    patches: list[str] = []
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, dict) and isinstance(value.get("model_patch"), str):
                patches.append(value["model_patch"])
    files: set[str] = set()
    for patch in patches:
        for match in re.finditer(r"^diff --git a/.*? b/(.*?)$", patch, re.MULTILINE):
            files.add(match.group(1))
    return sorted(files)


def _top_files_from_brief(text: str, limit: int = 10) -> list[str]:
    files: list[str] = []
    for match in re.finditer(r"^\s*\d+\.\s+([^\s]+)", text or "", re.MULTILINE):
        files.append(match.group(1))
        if len(files) >= limit:
            break
    return files


def _artifact_overlay(run_root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not run_root.exists():
        return out
    for task_dir in sorted(p for p in run_root.iterdir() if p.is_dir()):
        instance_id = task_dir.name.removeprefix("pro-")
        gt = task_dir / "gt"
        row: dict[str, Any] = {
            "artifact_task_dir": str(task_dir),
            "has_graph_db": (gt / "graph.db").exists(),
            "has_brief_result": (gt / "brief_result.json").exists(),
            "has_delivered_instruction": (gt / "delivered_instruction.txt").exists(),
            "submitted_patch_files_hint": _patch_files_from_preds(task_dir / "pro_output" / "preds.json"),
        }
        if (gt / "brief_result.json").exists():
            try:
                brief = _read_json(gt / "brief_result.json")
                metrics = brief.get("metrics", {}) if isinstance(brief, dict) else {}
                row.update(
                    {
                        "delivered_top_files": _top_files_from_brief(brief.get("brief_text", "")),
                        "effective_w_sem": metrics.get("effective_w_sem"),
                        "semantic_signal_count": metrics.get("semantic_signal_count"),
                        "localization_proof_present": bool(metrics.get("localization_proof")),
                    }
                )
            except Exception as exc:
                row["brief_result_error"] = type(exc).__name__
        if (gt / "graph_certificate.json").exists():
            try:
                cert = _read_json(gt / "graph_certificate.json")
                row.update(
                    {
                        "nodes_count": cert.get("nodes_count"),
                        "edges_count": cert.get("edges_count"),
                        "name_match_edge_count": cert.get("name_match_edge_count"),
                        "fts5_exists": cert.get("fts5_exists"),
                    }
                )
            except Exception:
                pass
        if (gt / "lsp_certificate.json").exists():
            try:
                lsp = _read_json(gt / "lsp_certificate.json")
                row.update(
                    {
                        "artifact_language": lsp.get("language"),
                        "lsp_verdict_hint": lsp.get("verdict_hint"),
                        "lsp_warm": lsp.get("lsp_warm"),
                    }
                )
            except Exception:
                pass
        out[instance_id] = row
    return out


def _load_gold(gold_path: Path | None) -> dict[str, dict[str, Any]]:
    if not gold_path:
        return {}
    data = _read_json(gold_path)
    if not isinstance(data, dict):
        raise ValueError("--gold must be a JSON object keyed by instance_id")
    out: dict[str, dict[str, Any]] = {}
    for instance_id, value in data.items():
        if isinstance(value, list):
            out[str(instance_id)] = {
                "gold_files": [str(x) for x in value],
                "gold_source": "explicit_gold_json",
            }
        elif isinstance(value, dict):
            out[str(instance_id)] = {
                "gold_files": [str(x) for x in value.get("gold_files", [])],
                "gold_source": str(value.get("gold_source", "explicit_gold_json")),
                "split": value.get("split"),
                "role": value.get("role"),
            }
        else:
            raise ValueError(f"invalid gold entry for {instance_id!r}")
    return out


def build_manifest(
    dataset: Path,
    diagnostic_run: Path | None = None,
    gold_path: Path | None = None,
) -> dict[str, Any]:
    overlay = _artifact_overlay(diagnostic_run) if diagnostic_run else {}
    gold = _load_gold(gold_path)
    tasks: list[dict[str, Any]] = []
    for line in dataset.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        instance_id = row["instance_id"]
        task: dict[str, Any] = {
            "instance_id": instance_id,
            "repo": row.get("repo"),
            "language": row.get("repo_language"),
            "dockerhub_tag": row.get("dockerhub_tag"),
            "gold_status": "unlocked",
            "gold_files": [],
            "gold_source": "",
            "split": "unassigned",
            "role": "candidate_unlocked",
        }
        task.update(overlay.get(instance_id, {}))
        if instance_id in gold:
            g = gold[instance_id]
            task["gold_files"] = g["gold_files"]
            task["gold_source"] = g["gold_source"]
            task["gold_status"] = "locked" if task["gold_files"] else "unlocked"
            if g.get("split"):
                task["split"] = g["split"]
            if g.get("role"):
                task["role"] = g["role"]
        tasks.append(task)
    by_lang = Counter(str(t.get("language") or "unknown") for t in tasks)
    with_artifacts = sum(1 for t in tasks if t.get("has_brief_result"))
    return {
        "schema": "gt.substrate_stage1_manifest.v1",
        "dataset": str(dataset),
        "diagnostic_run": str(diagnostic_run) if diagnostic_run else "",
        "gold_input": str(gold_path) if gold_path else "",
        "rules": {
            "surface": "gt-run-proof substrate artifacts only",
            "stage1_not_stage2": "agent pass/fail is excluded from localization proof",
            "gold_policy": "submitted patches are hints only; acceptance requires independent gold lock",
            "overfit_policy": "no task/repo/file/language literals may drive product logic",
        },
        "counts": {
            "tasks": len(tasks),
            "with_diagnostic_artifacts": with_artifacts,
            "by_language": dict(sorted(by_lang.items())),
            "gold_locked": sum(1 for t in tasks if t.get("gold_status") == "locked"),
        },
        "tasks": tasks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        default="benchmarks/data/swebench_pro_public_tags.jsonl",
        help="SWE-bench Pro task index jsonl",
    )
    parser.add_argument("--diagnostic-run", default="", help="Optional D:/gt_runs/pro_* artifact root")
    parser.add_argument(
        "--gold",
        default="",
        help="Optional explicit JSON map: instance_id -> {gold_files, gold_source, split, role}",
    )
    parser.add_argument("--out", required=True, help="Output manifest JSON")
    args = parser.parse_args()

    manifest = build_manifest(
        Path(args.dataset),
        Path(args.diagnostic_run) if args.diagnostic_run else None,
        Path(args.gold) if args.gold else None,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(
        f"wrote {out} tasks={manifest['counts']['tasks']} "
        f"artifacts={manifest['counts']['with_diagnostic_artifacts']} "
        f"gold_locked={manifest['counts']['gold_locked']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
