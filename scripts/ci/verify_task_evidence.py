#!/usr/bin/env python3
"""Verify per-task evidence artifacts before upload."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


COMMON_REQUIRED = [
    "trial_output.log",
    "graph.db",
    "gt_artifacts/run_manifest.json",
    "gt_artifacts/run_provenance.json",
    "gt_artifacts/proof_status.json",
    "gt_artifacts/brief.txt",
    "gt_artifacts/issue.txt",
    "gt_artifacts/graph_certificate.json",
    "gt_artifacts/embedder_certificate.json",
    "gt_artifacts/foundational_gate_report.json",
    "task_truth.json",
    "outcome.json",
]

BENCHMARK_REQUIRED = {
    "deepswe": ["adapter_witness.json"],
    "pro": ["reward.txt", "pro_eval/pro_eval_summary.json"],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", choices=sorted(BENCHMARK_REQUIRED), required=True)
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--task", required=True)
    args = parser.parse_args()

    root = args.dir
    required = COMMON_REQUIRED + BENCHMARK_REQUIRED[args.benchmark]
    missing = [rel for rel in required if not (root / rel).is_file()]
    lsp_certs = sorted((root / "gt_artifacts").glob("lsp_certificate*.json"))
    if not lsp_certs:
        missing.append("gt_artifacts/lsp_certificate*.json")

    manifest = {
        "schema": "gt.task_evidence_manifest.v1",
        "benchmark": args.benchmark,
        "task": args.task,
        "required": required,
        "lsp_certificate_count": len(lsp_certs),
        "missing": missing,
        "ok": not missing,
    }
    (root / "artifact_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    if missing:
        print("EVIDENCE_MISSING: " + ", ".join(missing), file=sys.stderr)
        return 1
    print(f"evidence manifest OK: {args.benchmark} {args.task}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
