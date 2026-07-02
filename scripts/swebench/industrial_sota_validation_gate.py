#!/usr/bin/env python3
"""Validate targeted DeepSWE artifacts against the industrial/SOTA ledger.

This gate is intentionally evidence-only: it does not edit the ledger. A TODO can
be closed only after this report is produced from remote/containerized DeepSWE
artifacts and reviewed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


DEFAULT_PROBES = Path(".groundtruth") / "industrial_sota_targeted_probes.json"

# B15: the genuinely-sound integrity core (Fable-confirmed) that MUST hold whenever the
# artifacts are present. --strict fails closed (rc=1) if any of these is not evidence, or
# if any item is contradicted. Capability/probe items may be missing/unexercised (their
# real proof is the Phase-4 trajectory witness) without hard-failing the gate.
REQUIRED_ITEMS = {1, 3, 4, 13, 14}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _find(root: Path, name: str) -> Path | None:
    direct = root / name
    if direct.exists():
        return direct
    hits = sorted(root.rglob(name))
    return hits[0] if hits else None


def _has_text(path: Path | None, needle: str) -> bool:
    if not path or not path.is_file():
        return False
    try:
        return needle in path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False


def _negative_text_evidence(path: Path | None, needle: str, label: str) -> tuple[str, list[str], list[str]]:
    """Evidence for absence is valid only when the searched artifact exists."""
    if not path or not path.is_file():
        return "missing", [], [f"{label} source artifact missing"]
    if _has_text(path, needle):
        return "contradicted", [], [f"{needle} present in {label}"]
    return "evidence", [f"{label} lacks {needle}"], []


def _artifact_hashes_ok(root: Path, manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    hashes = manifest.get("artifact_sha256") or {}
    if not isinstance(hashes, dict) or not hashes:
        return False, ["run_manifest.artifact_sha256 missing"]
    errors: list[str] = []
    for name, expected in hashes.items():
        p = _find(root, str(name))
        if not p:
            errors.append(f"{name}:missing")
            continue
        got = _sha256(p)
        if got != str(expected):
            errors.append(f"{name}:sha256_mismatch")
    return not errors, errors


def _result(item_id: int, title: str, status: str, evidence: list[str], notes: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": title,
        "status": status,
        "evidence": evidence,
        "notes": notes or [],
    }


def _probe(probes: dict[str, Any], key: str) -> dict[str, Any]:
    payload = ((probes.get("probes") or {}).get(key) or {})
    return payload if isinstance(payload, dict) else {}


def _probe_result(
    probes: dict[str, Any],
    key: str,
    item_id: int,
    title: str,
    *,
    missing_note: str,
) -> dict[str, Any]:
    payload = _probe(probes, key)
    if payload.get("status") == "evidence":
        return _result(item_id, title, "evidence", list(payload.get("evidence") or []))
    return _result(item_id, title, "missing", [], list(payload.get("notes") or [missing_note]))


def validate(root: Path, *, probes_path: Path | None = None) -> dict[str, Any]:
    gt_artifacts = root / "gt_artifacts"
    search_root = gt_artifacts if gt_artifacts.is_dir() else root
    probes = _load_json(probes_path or DEFAULT_PROBES)
    manifest = _load_json(_find(search_root, "run_manifest.json") or Path())
    verdict = _load_json(_find(search_root, "proof_verdict.json") or Path())
    adapter = _load_json(_find(root, "adapter_witness.json") or _find(search_root, "adapter_witness.json") or Path())
    task_truth = _load_json(_find(root, "task_truth.json") or Path())
    resolution = _load_json(_find(search_root, "resolution_method_audit.json") or Path())
    brief_result = _load_json(_find(search_root, "brief_result.json") or Path())
    trial_log = _find(root, "trial_output.log")
    delivered = _find(root, "delivered_instruction.txt")
    has_trial_log = bool(trial_log and trial_log.is_file())

    hash_ok, hash_notes = _artifact_hashes_ok(search_root, manifest)
    proof_ok = verdict.get("state") == "ok"
    # B15: fail-CLOSED. A missing gate_rc is NOT clean — the old `in (0, None)` passed a
    # manifest that never recorded the rc (a "fail-closed" checkpoint that was fail-open).
    # Require it present AND zero.
    gate_ok = manifest.get("gate_rc") == 0
    adapter_structured = (((task_truth.get("runtime_control") or {}).get("adapter_witness") or {}).get("structured") or {})

    items: list[dict[str, Any]] = []
    items.append(_result(1, "Strict proof Gate 1 fail-closed", "evidence" if proof_ok and gate_ok else "missing", ["proof_verdict.state=ok", "run_manifest.gate_rc clean"] if proof_ok and gate_ok else [], [] if proof_ok and gate_ok else ["proof verdict/gate rc not clean"]))
    st, ev, notes = _negative_text_evidence(trial_log, "GT_LOCALIZATION_GOLD_FILES", "trial_output.log")
    items.append(_result(2, "Remove proof leakage", st, ev, notes))
    items.append(_result(3, "Proof reuse strict-verifier equivalent", "evidence" if proof_ok and hash_ok else "missing", ["proof_verdict ok", "artifact hashes verified"] if proof_ok and hash_ok else [], hash_notes))
    items.append(_result(4, "Strict commit parity fails unknown/dev", "evidence" if (manifest.get("commit_parity") or {}).get("status") == "match" else "missing", ["commit_parity.status=match"] if (manifest.get("commit_parity") or {}).get("status") == "match" else [], [f"commit_parity={(manifest.get('commit_parity') or {}).get('status')}"]))
    items.append(_result(5, "Workspace brief artifact collision-safe/atomic", "evidence" if adapter.get("workspace_brief_artifact") else "missing", ["adapter_witness.workspace_brief_artifact present"] if adapter.get("workspace_brief_artifact") else []))
    items.append(_probe_result(probes, "worktree_or_submodule_fixture", 6, "Root detection supports worktrees/submodules", missing_note="requires targeted worktree/submodule task or fixture evidence"))
    items.append(_result(7, "Exact-name recall cannot set witness_verified", "evidence" if brief_result else "missing", ["brief_result present for offline inspection"] if brief_result else []))
    items.append(_result(8, "v1r canonical fact gate", "evidence" if resolution else "missing", ["resolution_method_audit present"] if resolution else []))
    # B-11: a LEAK is name_match presented as a VERIFIED FACT — not the mandated honest
    # "(unverified)" tag. The old bare `_has_text(delivered, "name_match")` flagged the
    # correct-or-quiet render (`name_match … (unverified)`) as CONTRADICTED, punishing the
    # honesty the architecture requires. Detect the verified-context form on BOTH surfaces.
    name_match_leak = (
        _has_text(delivered, "name_match verified")
        or _has_text(delivered, "verified name_match")
        or _has_text(trial_log, "name_match verified")
        or _has_text(trial_log, "VERIFIED name_match")
    )
    if not delivered and not has_trial_log:
        items.append(_result(9, "No name_match verified evidence", "missing", [], ["delivered instruction and trial log missing"]))
    else:
        items.append(_result(9, "No name_match verified evidence", "contradicted" if name_match_leak else "evidence", ["no name_match verified marker in delivery/log artifacts"] if not name_match_leak else [], ["name_match appears in verified delivery/log context"] if name_match_leak else []))
    items.append(_probe_result(probes, "ambiguous_composes_fixture", 10, "Ambiguous COMPOSES cannot mint fact", missing_note="requires targeted graph with ambiguous COMPOSES fixture/task evidence"))
    items.append(_probe_result(probes, "repeatability_run_pair", 11, "Deterministic edge scan ordering", missing_note="requires repeated proof hash/order comparison evidence"))
    items.append(_probe_result(probes, "mcp_endpoint_probe", 12, "LSP promotion state on graph-consuming endpoints", missing_note="requires MCP endpoint artifact/probe evidence"))
    items.append(_result(13, "One proof verdict schema", "evidence" if verdict.get("schema") == "gt.proof_verdict.v1" else "missing", ["proof_verdict.schema=gt.proof_verdict.v1"] if verdict.get("schema") == "gt.proof_verdict.v1" else []))
    items.append(_result(14, "Manifest-bound required artifact hashes", "evidence" if hash_ok else "missing", ["run_manifest artifact hashes match"] if hash_ok else [], hash_notes))
    st, ev, notes = _negative_text_evidence(trial_log, "EVAL_LEAKAGE_FORBIDDEN", "trial_output.log")
    items.append(_result(15, "Clean-env projection blocks evaluator taint", st, ev, notes))
    items.append(_probe_result(probes, "mcp_metadata_probe", 16, "Freshness/degraded metadata on agent responses", missing_note="requires MCP response metadata probe evidence"))
    items.append(_probe_result(probes, "evidence_surface_parity_probe", 17, "Retire or shim alternate evidence surfaces", missing_note="requires evidence-surface parity probe"))
    items.append(_result(18, "G1 live agent re-read proof", "evidence" if adapter.get("workspace_brief_reread_command_delivered") else "missing", ["workspace_brief_reread_command_delivered=true"] if adapter.get("workspace_brief_reread_command_delivered") else []))
    items.append(_result(19, "G1 generalized delivery across surfaces", "evidence" if adapter.get("brief_match") and delivered else "missing", ["brief_match=true", "delivered_instruction.txt present"] if adapter.get("brief_match") and delivered else []))
    items.append(_result(20, "G1 paired DeepSWE delivery bet", "missing", [], ["requires paired gt/baseline run ids"]))
    items.append(_result(21, "G2 per-resolution-method FP audit", "evidence" if resolution else "missing", ["resolution_method_audit.json present"] if resolution else []))
    items.append(_result(22, "G2 SCIP/batch symbol index import", "evidence" if any((g.get("scip_coverage") or {}).get("present") for g in resolution.get("graphs", [])) else "missing", ["SCIP coverage present"] if any((g.get("scip_coverage") or {}).get("present") for g in resolution.get("graphs", [])) else [], ["SCIP absent in audited graphs"] if resolution else []))
    # B15 exercise-floor: a key existing with a 0/empty value on a trivial graph is NOT
    # evidence the capability ran — it is UNEXERCISED (blocks DONE like missing, but does
    # not hard-fail --strict on a fixture that simply had no method calls / no residuals).
    audit_graphs = resolution.get("graphs", []) if resolution else []
    if not resolution:
        items.append(_result(23, "G2 demand-driven LSP residual", "missing", [], ["resolution_method_audit absent"]))
        items.append(_result(24, "G2 receiver/type propagation", "missing", [], ["resolution_method_audit absent"]))
    else:
        residual_count = sum(len(g.get("demand_lsp_residuals") or []) for g in audit_graphs)
        residual_present = any("demand_lsp_residuals" in g for g in audit_graphs)
        # B-6: demand_lsp_residuals is a sample of UNRESOLVED name_match residual CANDIDATES
        # (resolution_method_audit: WHERE method LIKE 'name_match%'), NOT edges a demand-driven
        # LSP pass resolved — that pass is NOT built (gt_math G2 NOT STARTED). Enumerating
        # candidates must NEVER green as "evidence" of a capability that does not exist
        # (correct-or-quiet for measurement); report an unexercised diagnostic instead.
        items.append(_result(
            23, "G2 demand-driven LSP residual (candidates enumerated; resolver NOT built)",
            "unexercised" if residual_present else "missing",
            [],
            [f"{residual_count} unresolved residual candidate(s) enumerated; demand-driven LSP "
             "resolution not implemented (gt_math G2 NOT STARTED)"] if residual_present
            else ["resolution_method_audit absent or no demand_lsp_residuals key"],
        ))
        recv_proven = sum(g.get("receiver_type_proven_call_edges") or 0 for g in audit_graphs)
        recv_unproven = sum(g.get("receiver_unproven_call_edges") or 0 for g in audit_graphs)
        recv_present = any("receiver_type_proven_call_edges" in g for g in audit_graphs)
        items.append(_result(
            24, "G2 receiver/type propagation",
            "evidence" if (recv_proven + recv_unproven) > 0 else ("unexercised" if recv_present else "missing"),
            [f"receiver-typed {recv_proven} of {recv_proven + recv_unproven} method-call edge(s)"] if (recv_proven + recv_unproven) > 0 else [],
            [] if (recv_proven + recv_unproven) > 0 else ["no method-call edges to exercise receiver typing in these graphs"],
        ))
    items.append(_result(25, "G2 PageRank/RRF over trusted graph", "evidence" if resolution and any(g.get("trusted_graph_rank") for g in resolution.get("graphs", [])) else "missing", ["trusted_graph_rank present"] if resolution and any(g.get("trusted_graph_rank") for g in resolution.get("graphs", [])) else []))
    confidence_probe = _probe(probes, "fresh_brief_result")
    if brief_result.get("confidence_tier"):
        items.append(_result(26, "G3 confidence tiers drive commit-vs-verify", "evidence", [f"confidence_tier={brief_result.get('confidence_tier')}"]))
    elif confidence_probe.get("status") == "evidence":
        items.append(_result(26, "G3 confidence tiers drive commit-vs-verify", "evidence", list(confidence_probe.get("evidence") or [])))
    else:
        items.append(_result(26, "G3 confidence tiers drive commit-vs-verify", "missing", [], list(confidence_probe.get("notes") or [])))
    # B15: require the STRUCTURED consumption receipt. The old `or adapter` made the
    # structured check dead code — any adapter_witness.json presence closed "proves evidence use".
    items.append(_result(27, "G3 consumption receipts prove evidence use", "evidence" if adapter_structured else "missing", ["adapter_witness structured receipt present"] if adapter_structured else ["structured consumption receipt absent"]))

    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "schema": "gt.industrial_sota_validation.v1",
        "artifact_root": str(root),
        "sources": {
            "trial_output_log": str(trial_log) if trial_log else None,
            "delivered_instruction": str(delivered) if delivered else None,
            "run_manifest": bool(manifest),
            "proof_verdict": bool(verdict),
            "adapter_witness": bool(adapter),
            "task_truth": bool(task_truth),
            "resolution_method_audit": bool(resolution),
            "brief_result": bool(brief_result),
            "targeted_probes": bool(probes),
        },
        "counts": counts,
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_root", help="DeepSWE trial_results directory")
    parser.add_argument("--probes", default=str(DEFAULT_PROBES), help="structured targeted probe JSON")
    parser.add_argument("--strict", action="store_true", help="B15: rc=1 if any item is contradicted or any REQUIRED_ITEMS integrity item is not evidence")
    args = parser.parse_args(argv)
    payload = validate(Path(args.artifact_root), probes_path=Path(args.probes))
    print(json.dumps(payload, indent=2, sort_keys=True))
    if args.strict:
        by_id = {it["id"]: it for it in payload["items"]}
        contradicted = sorted(it["id"] for it in payload["items"] if it["status"] == "contradicted")
        required_unmet = sorted(i for i in REQUIRED_ITEMS if by_id.get(i, {}).get("status") != "evidence")
        if contradicted or required_unmet:
            print(json.dumps({"strict_fail": {"rc": 1, "contradicted": contradicted, "required_unmet": required_unmet}}, indent=2))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
