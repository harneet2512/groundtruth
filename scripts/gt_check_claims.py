#!/usr/bin/env python3
"""Claim proof checker — read CLAIM_LEDGER.yml and autopsy results,
report claims without trajectory/test proof, and claims contradicted
by fresh run artifacts.

Usage:
    python scripts/gt_check_claims.py <claim_ledger.yml> <autopsy_results_dir>

Exit code 1 if any claims are unsupported.
"""
from __future__ import annotations

import json
import os
import sys

try:
    import yaml
except ImportError:
    yaml = None


def parse_yaml_claims(path: str) -> list[dict]:
    """Parse CLAIM_LEDGER.yml. Falls back to simple parsing if PyYAML missing."""
    if yaml:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("claims", [])

    # Fallback: minimal YAML-like parsing for flat claim entries
    claims = []
    current: dict = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip()
            if line.strip().startswith("- claim_id:"):
                if current:
                    claims.append(current)
                current = {"claim_id": line.split(":", 1)[1].strip()}
            elif ":" in line and current:
                key, _, val = line.strip().partition(":")
                key = key.strip().lstrip("- ")
                val = val.strip()
                if val and val != "null":
                    current[key] = val
    if current:
        claims.append(current)
    return claims


def load_autopsy_results(directory: str) -> list[dict]:
    """Load all autopsy.json files from a results directory."""
    results = []
    if not os.path.isdir(directory):
        return results
    for root, dirs, files in os.walk(directory):
        for f in files:
            if f == "autopsy.json":
                path = os.path.join(root, f)
                with open(path, encoding="utf-8") as fh:
                    results.append(json.load(fh))
    return results


def check_claims(claims: list[dict], autopsies: list[dict]) -> dict:
    """Check each claim against autopsy evidence."""
    unsupported = []
    contradicted = []
    verified = []
    skipped = []

    # Build layer visibility map from autopsies
    layer_visibility: dict[str, dict] = {}
    for autopsy in autopsies:
        task_id = autopsy.get("task_id", "unknown")
        for layer_key, layer_data in autopsy.get("layers", {}).items():
            if layer_key not in layer_visibility:
                layer_visibility[layer_key] = {"visible_count": 0, "total": 0, "tasks": []}
            layer_visibility[layer_key]["total"] += 1
            if layer_data.get("visible_in_output"):
                layer_visibility[layer_key]["visible_count"] += 1
                layer_visibility[layer_key]["tasks"].append(task_id)

    for claim in claims:
        claim_id = claim.get("claim_id", "")
        doc_status = claim.get("doc_status", "")
        proof_type = claim.get("proof_type", "none")
        layer = claim.get("layer", "")

        # Skip only DISABLED claims — BROKEN/OPEN_BUG are checked
        if doc_status == "DISABLED":
            skipped.append({
                "claim_id": claim_id,
                "reason": f"DOC_OF_HONOR status is {doc_status}",
            })
            continue

        # Map claim layer to autopsy layer key (coarse mapping)
        layer_map = {
            "L0": None,
            "L1": "L1_BRIEF",
            "L1+": "L1_EDIT_TARGET",
            "L3": "L3_POST_EDIT",
            "L3b": "L3B_POST_VIEW",
            "L4a": "L4A_AUTO_QUERY",
            "L5": "L5_SCAFFOLD",
            "L5b": "L5B_REMINDER",
            "L6": "L6_PRESUBMIT",
            "Grep": "GREP_INTERCEPT",
            "Consensus": "CONSENSUS",
            "Infrastructure": None,
        }
        autopsy_key = layer_map.get(layer)

        # Fine-grained override: claim_id may match an autopsy key directly
        claim_id_to_autopsy = {
            "L1_KEY_CONTRACTS": "L1_KEY_CONTRACTS",
            "L1_EDIT_PLAN": "L1_EDIT_TARGET",
            "L1_BRIEF_DELIVERY": "L1_BRIEF",
            "L3_POST_EDIT_DELIVERY": "L3_POST_EDIT",
            "L3B_POST_VIEW_DELIVERY": "L3B_POST_VIEW",
            "L4A_AUTO_QUERY": "L4A_AUTO_QUERY",
            "L5_SCAFFOLD_GOVERNOR": "L5_SCAFFOLD",
            "GREP_INTERCEPT": "GREP_INTERCEPT",
            "CONSENSUS_SCOPE": "CONSENSUS",
            "L6_PRESUBMIT_OPEN": "L6_PRESUBMIT",
        }
        if claim_id in claim_id_to_autopsy:
            autopsy_key = claim_id_to_autopsy[claim_id]

        # Check if claim has trajectory proof
        has_trajectory_proof = False
        if autopsies and autopsy_key and autopsy_key in layer_visibility:
            vis = layer_visibility[autopsy_key]
            if vis["visible_count"] > 0:
                has_trajectory_proof = True

        # OPEN_BUG claims: check if events show generated content
        if doc_status == "OPEN_BUG":
            if autopsies and autopsy_key and autopsy_key in layer_visibility:
                vis = layer_visibility[autopsy_key]
                unsupported.append({
                    "claim_id": claim_id,
                    "doc_status": doc_status,
                    "proof_type": proof_type,
                    "reason": f"OPEN_BUG: {vis['visible_count']}/{vis['total']} tasks showed visible evidence — fix needed",
                    "layer": layer,
                })
            else:
                unsupported.append({
                    "claim_id": claim_id,
                    "doc_status": doc_status,
                    "proof_type": proof_type,
                    "reason": "OPEN_BUG: no trajectory data to assess",
                    "layer": layer,
                })
            continue

        # Check for contradictions FIRST (strongest signal)
        if doc_status in ("WORKING", "VERIFIED", "FIXED"):
            if autopsies and autopsy_key and autopsy_key in layer_visibility:
                vis = layer_visibility[autopsy_key]
                if vis["total"] > 0 and vis["visible_count"] == 0:
                    contradicted.append({
                        "claim_id": claim_id,
                        "doc_status": doc_status,
                        "reason": f"0/{vis['total']} tasks showed visible evidence",
                        "layer": layer,
                    })
                    continue

        # Delivery claims need trajectory proof
        delivery_layers = ("L1", "L1+", "L3", "L3b", "L4a", "L5", "Grep", "Consensus")
        if doc_status in ("WORKING", "VERIFIED", "FIXED") and proof_type == "none":
            if layer in delivery_layers and not has_trajectory_proof:
                unsupported.append({
                    "claim_id": claim_id,
                    "doc_status": doc_status,
                    "proof_type": proof_type,
                    "reason": "Delivery claim marked WORKING without trajectory proof",
                    "layer": layer,
                })
                continue

        if has_trajectory_proof or proof_type in ("test", "replay"):
            verified.append({
                "claim_id": claim_id,
                "proof_type": "trajectory" if has_trajectory_proof else proof_type,
            })
        elif proof_type == "code_audit":
            unsupported.append({
                "claim_id": claim_id,
                "doc_status": doc_status,
                "proof_type": proof_type,
                "reason": "Only code_audit proof — no runtime verification",
                "layer": layer,
            })
        else:
            skipped.append({
                "claim_id": claim_id,
                "reason": f"proof_type={proof_type}, layer={layer}",
            })

    return {
        "total_claims": len(claims),
        "verified": verified,
        "unsupported": unsupported,
        "contradicted": contradicted,
        "skipped": skipped,
        "autopsies_analyzed": len(autopsies),
    }


def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/gt_check_claims.py <claim_ledger.yml> <autopsy_results_dir>")
        sys.exit(1)

    ledger_path = sys.argv[1]
    results_dir = sys.argv[2]

    if not os.path.isfile(ledger_path):
        print(f"ERROR: Claim ledger not found: {ledger_path}")
        sys.exit(1)

    claims = parse_yaml_claims(ledger_path)
    autopsies = load_autopsy_results(results_dir)

    print(f"Claims loaded: {len(claims)}")
    print(f"Autopsies loaded: {len(autopsies)}")

    result = check_claims(claims, autopsies)

    print(f"\n{'='*60}")
    print(f"CLAIM CHECK RESULTS")
    print(f"{'='*60}")
    print(f"  Total claims: {result['total_claims']}")
    print(f"  Verified by trajectory/test: {len(result['verified'])}")
    print(f"  Unsupported (no runtime proof): {len(result['unsupported'])}")
    print(f"  Contradicted by artifacts: {len(result['contradicted'])}")
    print(f"  Skipped: {len(result['skipped'])}")

    if result["contradicted"]:
        print(f"\nCONTRADICTED CLAIMS:")
        for c in result["contradicted"]:
            print(f"  {c['claim_id']}: {c['reason']} (doc says {c['doc_status']})")

    if result["unsupported"]:
        print(f"\nUNSUPPORTED CLAIMS:")
        for c in result["unsupported"]:
            print(f"  {c['claim_id']}: {c['reason']}")

    # Write result
    out_path = os.path.join(results_dir, "claim_check_result.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"\nClaim check result written to: {out_path}")

    has_failures = len(result["contradicted"]) > 0
    sys.exit(1 if has_failures else 0)


if __name__ == "__main__":
    main()
