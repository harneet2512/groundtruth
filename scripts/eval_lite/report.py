"""Reporter for Track D — turns results.json into the comparison table,
fail-category histogram, per-task CSV, and top-failures dump.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

_BASELINE_TABLE: list[tuple[str, str, str, str, str]] = [
    ("File Acc@1",      "{gt:>5}", "    -    ", "    -    ", "    -    "),
    ("File Acc@3",      "{gt:>5}", "    -    ", "    -    ", "    -    "),
    ("File Acc@5",      "{gt:>5}", " 94.16%  ", "    -    ", "    -    "),
    ("Function Acc@5",  "{gt:>5}", "    -    ", "    -    ", "    -    "),
    ("Function Acc@10", "{gt:>5}", "    -    ", "    -    ", "    -    "),
]

_METRIC_KEY = {
    "File Acc@1": "acc_file_at_1",
    "File Acc@3": "acc_file_at_3",
    "File Acc@5": "acc_file_at_5",
    "Function Acc@5": "acc_func_at_5",
    "Function Acc@10": "acc_func_at_10",
}


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def write_results_table(agg: dict[str, Any], path: Path) -> None:
    lines = [
        "| Metric              | GT V2-loc | LocAgent | Agentless | SweRank |",
        "|---------------------|-----------|----------|-----------|---------|",
    ]
    for label, _gt_fmt, locagent, agentless, swerank in _BASELINE_TABLE:
        key = _METRIC_KEY[label]
        gt_pct = _fmt_pct(float(agg.get(key, 0.0)))
        lines.append(
            f"| {label:<19} | {gt_pct:>9} | {locagent} | {agentless} | {swerank} |"
        )

    hist = agg.get("fail_category_hist") or {}
    lines.append("")
    lines.append("## Fail-category histogram")
    lines.append("")
    lines.append("| Category | Count |")
    lines.append("|----------|-------|")
    for cat in sorted(hist, key=lambda k: (-hist[k], k)):
        lines.append(f"| {cat} | {hist[cat]} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_failure_modes(per_task: list[dict[str, Any]], path: Path) -> None:
    fields = ["instance_id", "hit_file_at_5", "hit_func_at_10", "fail_category"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in per_task:
            w.writerow(
                {
                    "instance_id": r.get("instance_id", ""),
                    "hit_file_at_5": r.get("hit_file_at_5", ""),
                    "hit_func_at_10": r.get("hit_func_at_10", ""),
                    "fail_category": r.get("fail_category", ""),
                }
            )


def _is_no_hit(r: dict[str, Any]) -> bool:
    keys = (
        "hit_file_at_1",
        "hit_file_at_3",
        "hit_file_at_5",
        "hit_func_at_5",
        "hit_func_at_10",
    )
    return not any(r.get(k) for k in keys)


def write_top_failures(
    per_task: list[dict[str, Any]],
    instance_index: dict[str, int],
    issue_text_by_id: dict[str, str],
    path: Path,
    n: int = 20,
) -> None:
    failures = [r for r in per_task if _is_no_hit(r)]
    failures.sort(
        key=lambda r: instance_index.get(r.get("instance_id", ""), -1),
        reverse=True,
    )
    lines: list[str] = ["# Top failures (no hit at any K)", ""]
    for r in failures[:n]:
        iid = r.get("instance_id", "")
        text = (issue_text_by_id.get(iid) or "")[:500]
        lines.append(f"## {iid}")
        lines.append("")
        lines.append("**Issue (first 500 chars):**")
        lines.append("")
        lines.append("```")
        lines.append(text)
        lines.append("```")
        lines.append("")
        lines.append(f"- predicted_top5: `{r.get('predicted_top5')}`")
        lines.append(f"- gold_files: `{r.get('gold_files')}`")
        lines.append(f"- predicted_funcs10: `{r.get('predicted_funcs10')}`")
        lines.append(f"- gold_functions: `{r.get('gold_functions')}`")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _load_issue_text_index(
    cache_dir: str | None,
) -> tuple[dict[str, int], dict[str, str]]:
    try:
        if cache_dir:
            from datasets import load_from_disk  # type: ignore[import-untyped]
            from pathlib import Path as _P

            if _P(cache_dir).is_dir():
                ds = load_from_disk(cache_dir)
            else:
                from datasets import load_dataset  # type: ignore[import-untyped]
                ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
        else:
            from datasets import load_dataset  # type: ignore[import-untyped]
            ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    except Exception:
        return {}, {}
    idx: dict[str, int] = {}
    text: dict[str, str] = {}
    for i, row in enumerate(ds):
        iid = row.get("instance_id")
        if iid:
            idx[iid] = i
            text[iid] = row.get("problem_statement") or ""
    return idx, text


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--results", required=True,
                   help="Path to results.json from runner")
    p.add_argument("--out", required=True, help="Output directory")
    p.add_argument("--dataset-cache", default="/data1tb/swe_lite_dataset")
    p.add_argument("--top-n", type=int, default=20)
    args = p.parse_args()

    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    per_task = results.get("per_task") or []
    aggregate = results.get("aggregate") or {}

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    write_results_table(aggregate, out_dir / "results_table.md")
    write_failure_modes(per_task, out_dir / "failure_modes.csv")

    instance_idx, issue_text = _load_issue_text_index(args.dataset_cache)
    write_top_failures(
        per_task, instance_idx, issue_text, out_dir / "top_failures.md", args.top_n
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
