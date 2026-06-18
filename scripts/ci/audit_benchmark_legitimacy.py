#!/usr/bin/env python3
"""Static CI audit for benchmark-legitimacy invariants."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
PRO_WORKFLOW = ROOT / ".github" / "workflows" / "swebench_pro_full.yml"
DEEPSWE_WORKFLOW = ROOT / ".github" / "workflows" / "deepswe_full.yml"
PRO_EVAL_HELPER = ROOT / "scripts" / "ci" / "pro_official_eval.py"
PRO_MANIFEST = ROOT / "benchmarks" / "data" / "swebench_pro_public_tags.jsonl"
DEEPSWE_MANIFEST = ROOT / "artifact_deepswe" / "repo_manifest.json"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def record(
    results: list[dict[str, str]],
    status: str,
    benchmark: str,
    check: str,
    detail: str,
) -> None:
    results.append(
        {"status": status, "benchmark": benchmark, "check": check, "detail": detail}
    )


def section_has_continue_on_error(text: str, step_name: str) -> bool:
    marker = f"- name: {step_name}"
    start = text.find(marker)
    if start < 0:
        return False
    next_step = text.find("\n      - name:", start + len(marker))
    block = text[start : next_step if next_step >= 0 else len(text)]
    return "continue-on-error: true" in block


def require_present(
    results: list[dict[str, str]],
    text: str,
    benchmark: str,
    check: str,
    needles: Iterable[str],
) -> None:
    missing = [needle for needle in needles if needle not in text]
    if missing:
        record(results, "FAIL", benchmark, check, "missing: " + ", ".join(missing))
    else:
        record(results, "OK", benchmark, check, "all required tokens present")


def require_absent(
    results: list[dict[str, str]],
    text: str,
    benchmark: str,
    check: str,
    forbidden: Iterable[str],
) -> None:
    found = [needle for needle in forbidden if needle in text]
    if found:
        record(results, "FAIL", benchmark, check, "forbidden tokens present: " + ", ".join(found))
    else:
        record(results, "OK", benchmark, check, "no forbidden tokens present")


def audit_pro_manifest(results: list[dict[str, str]]) -> None:
    if not PRO_MANIFEST.is_file():
        record(results, "FAIL", "pro", "manifest", f"missing {PRO_MANIFEST}")
        return
    ids: set[str] = set()
    duplicates: list[str] = []
    languages: set[str] = set()
    count = 0
    with PRO_MANIFEST.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            count += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                record(results, "FAIL", "pro", "manifest", f"line {lineno}: invalid JSON: {exc}")
                continue
            for key in ("instance_id", "repo", "repo_language", "dockerhub_tag"):
                if not row.get(key):
                    record(results, "FAIL", "pro", "manifest", f"line {lineno}: missing {key}")
            iid = row.get("instance_id")
            if iid in ids:
                duplicates.append(iid)
            elif iid:
                ids.add(iid)
            if row.get("repo_language"):
                languages.add(row["repo_language"])
    if duplicates:
        record(results, "FAIL", "pro", "manifest", f"duplicate instance_id(s): {duplicates[:10]}")
    elif count == 731:
        record(results, "OK", "pro", "manifest", "731 unique public Pro instances")
    else:
        record(results, "FAIL", "pro", "manifest", f"expected 731 rows, found {count}")
    record(results, "OK", "pro", "manifest_languages", ",".join(sorted(languages)) or "<none>")


def audit_deepswe_manifest(results: list[dict[str, str]]) -> None:
    if not DEEPSWE_MANIFEST.is_file():
        record(results, "FAIL", "deepswe", "manifest", f"missing {DEEPSWE_MANIFEST}")
        return
    data = json.loads(read(DEEPSWE_MANIFEST))
    tasks = data.get("tasks") or []
    ids = [task.get("instance_id") for task in tasks]
    if len(tasks) != 113:
        record(results, "FAIL", "deepswe", "manifest", f"expected 113 tasks, found {len(tasks)}")
    elif len(ids) != len(set(ids)):
        record(results, "FAIL", "deepswe", "manifest", "duplicate instance_id in repo_manifest.json")
    else:
        record(results, "OK", "deepswe", "manifest", "113 unique DeepSWE manifest tasks")


def audit_workflows(results: list[dict[str, str]], benchmark: str) -> None:
    pro = read(PRO_WORKFLOW)
    deepswe = read(DEEPSWE_WORKFLOW)
    pro_eval_helper = read(PRO_EVAL_HELPER)

    if benchmark in {"all", "pro"}:
        require_present(
            results,
            pro,
            "pro",
            "native_runner",
            [
                "benchmarks/swebench/run_mini_gt_pro_v10.py",
                "--subset ScaleAI/SWE-bench_Pro",
                "--filter \"^${{ matrix.task }}$\"",
                "benchmarks/data/swebench_pro_public_tags.jsonl",
                "scaleapi/SWE-bench_Pro-os",
                "scripts/ci/pro_official_eval.py",
                "GT_FORBID_PREBUILT_GRAPH: \"1\"",
            ],
        )
        require_absent(
            results,
            pro,
            "pro",
            "no_pier_coupling",
            [
                "pier run",
                "datacurve-pier",
                "GTMiniSweAgent",
                "swebench_pro_gt_pier",
                "materialize_pro",
                "--ak config_file",
            ],
        )
        require_absent(results, pro, "pro", "no_gold_patch_access", ["solution.patch", "test.patch"])
        require_present(
            results,
            pro_eval_helper,
            "pro",
            "official_evaluator",
            [
                "swe_bench_pro_eval.py",
                "--raw_sample_path",
                "--patch_path",
                "--scripts_dir",
                "--use_local_docker",
                "eval_results.json",
                "fail_to_pass",
                "pass_to_pass",
            ],
        )
        if section_has_continue_on_error(pro, "Run GT Pro trial (native SWE-bench Pro runner)"):
            record(results, "FAIL", "pro", "trial_fail_closed", "trial step has continue-on-error")
        else:
            record(results, "OK", "pro", "trial_fail_closed", "trial step is fail-closed")

    if benchmark in {"all", "deepswe"}:
        require_present(
            results,
            deepswe,
            "deepswe",
            "official_task_dirs",
            [
                "https://github.com/datacurve-ai/deep-swe.git",
                "deepswe-bench/tasks/${{ matrix.task }}",
                "artifact_deepswe/repo_manifest.json",
                "task.toml",
                "docker_image",
                "Stage-0 built NO task graph.db",
                "GT_FORBID_PREBUILT_GRAPH: \"1\"",
                "pier run",
            ],
        )
        require_absent(
            results,
            deepswe,
            "deepswe",
            "no_gold_patch_access",
            ["solution.patch", "test.patch"],
        )
        if section_has_continue_on_error(deepswe, "Run GT trial (pier + GTMiniSweAgent)"):
            record(results, "FAIL", "deepswe", "trial_fail_closed", "trial step has continue-on-error")
        else:
            record(results, "OK", "deepswe", "trial_fail_closed", "trial step is fail-closed")

    write_patterns = [
        r">\s*(?:benchmarks/data/swebench_pro_public_tags\.jsonl|artifact_deepswe/repo_manifest\.json)",
        r"tee\s+(?:benchmarks/data/swebench_pro_public_tags\.jsonl|artifact_deepswe/repo_manifest\.json)",
        r"sed\s+-i\s+.*(?:swebench_pro_public_tags\.jsonl|repo_manifest\.json)",
    ]
    writes = [pat for pat in write_patterns if re.search(pat, pro + "\n" + deepswe)]
    if writes:
        record(results, "FAIL", "all", "source_of_truth_readonly", "workflow appears to mutate task source files")
    else:
        record(results, "OK", "all", "source_of_truth_readonly", "no task source mutation patterns found")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", choices=["all", "deepswe", "pro"], default="all")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    results: list[dict[str, str]] = []
    if args.benchmark in {"all", "pro"}:
        audit_pro_manifest(results)
    if args.benchmark in {"all", "deepswe"}:
        audit_deepswe_manifest(results)
    audit_workflows(results, args.benchmark)

    failed = [row for row in results if row["status"] == "FAIL"]
    print("# Benchmark legitimacy audit")
    for row in results:
        print(f"{row['status']:4} {row['benchmark']:8} {row['check']}: {row['detail']}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"ok": not failed, "results": results}, indent=2) + "\n",
            encoding="utf-8",
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
