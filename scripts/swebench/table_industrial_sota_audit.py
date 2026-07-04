#!/usr/bin/env python3
"""Render a structured 27-item industrial/SOTA audit table from trajectory JSONL."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRAJECTORY = ROOT / ".groundtruth" / "industrial_sota_local_trajectory.jsonl"
DEFAULT_LEDGER = ROOT / ".groundtruth" / "industrial_sota_ledger.json"

GENERALIZED_BY_ITEM = {
    1: "yes-code / missing-remote-evidence",
    2: "yes-code / missing-log-evidence",
    3: "yes-code / missing-fresh-proof-artifacts",
    4: "yes-evidenced",
    5: "yes-code / missing-adapter-artifact",
    6: "yes-code / missing-worktree-fixture",
    7: "yes-evidenced",
    8: "yes-code / missing-fresh-resolution-audit",
    9: "yes-code / missing-delivery-log",
    10: "yes-code / missing-ambiguous-fixture",
    11: "yes-code / missing-repeat-run-evidence",
    12: "yes-code / missing-MCP-probe",
    13: "yes-code / missing-fresh-proof-verdict",
    14: "yes-code / missing-fresh-hash-artifacts",
    15: "yes-code / missing-trial-log",
    16: "yes-code / missing-MCP-probe",
    17: "yes-code / missing-parity-probe",
    18: "yes-code / missing-live-agent-receipt",
    19: "yes-code / missing-cross-surface-receipt",
    20: "yes-design / missing-paired-run",
    21: "yes-tooling / missing-fresh-run-artifact",
    22: "partial / SCIP import not evidenced",
    23: "yes-tooling / missing-fresh-run-artifact",
    24: "yes-tooling / missing-fresh-run-artifact",
    25: "yes-tooling / missing-fresh-run-artifact",
    26: "yes-code / missing-confidence-tier-artifact",
    27: "yes-code / missing-consumption-receipt",
}

PROOF_STEP_BY_ITEM = {
    1: ("fresh_remote_trial", "proof_verdict.state=ok and run_manifest.gate_rc clean from a fresh containerized run"),
    2: ("fresh_remote_trial_log", "trial_output.log exists and lacks GT_LOCALIZATION_GOLD_FILES/evaluator leakage"),
    3: ("fresh_proof_reuse_artifacts", "proof_verdict ok plus run_manifest.artifact_sha256 hashes match reused artifacts"),
    4: ("fresh_run_manifest", "run_manifest.commit_parity.status == match"),
    5: ("adapter_witness_artifact", "adapter_witness.workspace_brief_artifact present"),
    6: ("worktree_or_submodule_fixture", "targeted fixture/run proves root detection in worktree/submodule layout"),
    7: ("brief_result_artifact", "brief_result present and exact-name recall does not set witness_verified"),
    8: ("fresh_resolution_audit", "resolution_method_audit.json present from a fresh graph"),
    9: ("delivery_or_trial_log", "delivered instruction/trial log exists and has no name_match verified marker"),
    10: ("ambiguous_composes_fixture", "targeted ambiguous COMPOSES graph proves no fact minted"),
    11: ("repeatability_run_pair", "repeated proof hash/order comparison shows deterministic edge ordering"),
    12: ("mcp_endpoint_probe", "MCP graph-consuming endpoint probe shows LSP promotion metadata/state"),
    13: ("fresh_proof_verdict", "proof_verdict.schema == gt.proof_verdict.v1"),
    14: ("fresh_hash_artifacts", "run_manifest artifact hashes exist and match artifact bytes"),
    15: ("fresh_remote_trial_log", "trial_output.log exists and no eval-taint failure/leak is present"),
    16: ("mcp_metadata_probe", "MCP response probe shows freshness/degraded metadata"),
    17: ("evidence_surface_parity_probe", "alternate evidence surfaces match canonical fact gate behavior"),
    18: ("live_agent_adapter_receipt", "adapter witness proves workspace brief re-read command delivered"),
    19: ("cross_surface_delivery_receipt", "brief_match true and delivered_instruction present across surfaces"),
    20: ("paired_gt_baseline_run", "paired GT/baseline run ids exist and compare delivery bet"),
    21: ("fresh_resolution_audit", "resolution_method_audit.json exists with per-method risk counters"),
    22: ("scip_enabled_run", "fresh audited graph reports SCIP coverage present"),
    23: ("fresh_resolution_audit", "fresh audited graph reports demand_lsp_residuals"),
    24: ("fresh_resolution_audit", "fresh audited graph reports receiver/type propagation counters"),
    25: ("fresh_resolution_audit", "fresh audited graph reports trusted_graph_rank"),
    26: ("fresh_brief_result", "brief_result.confidence_tier present and drives verification horizon"),
    27: ("adapter_or_task_truth_receipt", "adapter_witness or task_truth structured consumption receipt present"),
}

STEP_STAGE = {
    "fresh_remote_trial": "not_run",
    "fresh_remote_trial_log": "not_run",
    "fresh_proof_reuse_artifacts": "not_run",
    "fresh_run_manifest": "old_smoke_checked",
    "adapter_witness_artifact": "not_run",
    "worktree_or_submodule_fixture": "not_run",
    "brief_result_artifact": "old_smoke_checked",
    "fresh_resolution_audit": "old_smoke_checked",
    "delivery_or_trial_log": "not_run",
    "ambiguous_composes_fixture": "not_run",
    "repeatability_run_pair": "not_run",
    "mcp_endpoint_probe": "not_run",
    "fresh_proof_verdict": "not_run",
    "fresh_hash_artifacts": "not_run",
    "mcp_metadata_probe": "not_run",
    "evidence_surface_parity_probe": "not_run",
    "live_agent_adapter_receipt": "not_run",
    "cross_surface_delivery_receipt": "not_run",
    "paired_gt_baseline_run": "not_run",
    "scip_enabled_run": "not_run",
    "fresh_brief_result": "not_run",
    "adapter_or_task_truth_receipt": "not_run",
}

STAGE_MEANING = {
    "passed": "proving artifact exists and passed for this item",
    "old_smoke_checked": "only old smoke artifacts were checked; this cannot close the item",
    "not_run": "the required proving run/probe has not run yet",
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            data = json.loads(line)
            if isinstance(data, dict):
                rows.append(data)
    return rows


def _step(rows: list[dict[str, Any]], check_id: str) -> dict[str, Any]:
    for row in rows:
        if row.get("check_id") == check_id:
            return row
    return {}


def _gate_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    step = _step(rows, "industrial_validation_gate_smoke")
    try:
        payload = json.loads(step.get("stdout") or "{}")
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def _ledger(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def build_table(trajectory: Path, ledger_path: Path) -> dict[str, Any]:
    rows = _load_jsonl(trajectory)
    gate = _gate_payload(rows)
    ledger = _ledger(ledger_path)
    ledger_items = {int(item["id"]): item for item in ledger.get("items", []) if isinstance(item, dict) and item.get("id")}
    gate_items = {int(item["id"]): item for item in gate.get("items", []) if isinstance(item, dict) and item.get("id")}
    table: list[dict[str, Any]] = []
    for item_id in range(1, 28):
        gate_item = gate_items.get(item_id, {})
        ledger_item = ledger_items.get(item_id, {})
        status = str(gate_item.get("status") or "missing")
        evidence = gate_item.get("evidence") or []
        notes = gate_item.get("notes") or []
        proof_step, proof_requires = PROOF_STEP_BY_ITEM.get(item_id, ("unknown", "unknown"))
        proof_stage = "passed" if status == "evidence" else STEP_STAGE.get(proof_step, "not_run")
        table.append(
            {
                "id": item_id,
                "title": gate_item.get("title") or ledger_item.get("title") or "",
                "artifact_status": status,
                "works_as_intended": "proven" if status == "evidence" else "not_proven",
                "generalized": GENERALIZED_BY_ITEM.get(item_id, "unknown"),
                "proof_step": proof_step,
                "proof_stage": proof_stage,
                "proof_stage_meaning": STAGE_MEANING.get(proof_stage, "unknown"),
                "proof_requires": proof_requires,
                "how_read": {
                    "source": str(trajectory),
                    "parser": "json.loads(JSONL line where check_id == 'industrial_validation_gate_smoke')",
                    "fields": {
                        "status": f"items[{item_id}].status",
                        "evidence": f"items[{item_id}].evidence",
                        "notes": f"items[{item_id}].notes",
                    },
                },
                "evidence": evidence,
                "notes": notes,
            }
        )
    return {
        "schema": "gt.industrial_sota_27_table.v1",
        "trajectory": str(trajectory),
        "ledger": str(ledger_path),
        "counts": gate.get("counts") or {},
        "ledger_remaining": ledger.get("remaining"),
        "items": table,
    }


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "| ID | Item | Artifact Status | Works As Intended? | Generalized? | How read | Evidence / missing reason |",
        "|---:|---|---|---|---|---|---|",
    ]
    for item in payload["items"]:
        evidence = item["evidence"] or item["notes"] or []
        ev = "; ".join(str(x).replace("|", "\\|") for x in evidence)
        lines.append(
            "| {id} | {title} | {artifact_status}; proof_stage={proof_stage}; proof_step={proof_step} | {works_as_intended} | {generalized} | {how} | requires: {requires}; evidence: {ev} |".format(
                id=item["id"],
                title=str(item["title"]).replace("|", "\\|"),
                artifact_status=item["artifact_status"],
                proof_stage=item["proof_stage"],
                proof_step=item["proof_step"],
                works_as_intended=item["works_as_intended"],
                generalized=item["generalized"],
                how="JSONL -> industrial_validation_gate_smoke.stdout -> items[id]",
                requires=str(item["proof_requires"]).replace("|", "\\|"),
                ev=ev,
            )
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trajectory", default=str(DEFAULT_TRAJECTORY))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args(argv)
    payload = build_table(Path(args.trajectory), Path(args.ledger))
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_markdown(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
