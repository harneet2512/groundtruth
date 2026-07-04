#!/usr/bin/env python3
"""Topology verifier — consume autopsy JSON, classify each layer, produce
machine-readable pass/fail per layer.

This is the JUDGE. gt_autopsy.py describes what happened.
gt_verify_topology.py decides whether the architecture passed.

Usage:
    python scripts/gt_verify_topology.py <autopsy.json>
    python scripts/gt_verify_topology.py <autopsy.json> --baseline  (for baseline arm)

Checks:
  G1: gt_layer_events says delivered but output.jsonl lacks evidence -> FAIL
  C6: baseline arm contains GT evidence -> FAIL
  F2: L6/pre-submit evidence appears after agent can no longer act -> FAIL
  E3: hidden prefix leaks into agent observations -> FAIL
  D1: expected layer never fired -> WARN
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass


@dataclass
class LayerVerdict:
    layer: str
    passed: bool
    failure_class: str = ""
    reason: str = ""
    severity: str = ""


def verify_topology(autopsy: dict, is_baseline: bool = False) -> dict:
    """Verify topology from autopsy JSON. Returns verification result."""
    layers = autopsy.get("layers", {})
    verdicts: list[dict] = []
    overall_pass = True
    fail_count = 0
    warn_count = 0

    for layer_key, layer_data in layers.items():
        generated = layer_data.get("generated", False)
        visible = layer_data.get("visible_in_output", False)
        expected = layer_data.get("expected", "yes")
        status = layer_data.get("status", "")

        verdict = {
            "layer": layer_key,
            "passed": True,
            "failure_class": "",
            "reason": "",
            "severity": "PASS",
        }

        # F2: L6/pre-submit fired but agent can't act (check BEFORE G1)
        if layer_key == "L6_PRESUBMIT" and generated and not visible:
            verdict["passed"] = False
            verdict["failure_class"] = "F2"
            verdict["reason"] = "L6 pre-submit evidence generated after agent finished — dead write"
            verdict["severity"] = "FAIL"
            overall_pass = False
            fail_count += 1

        # C6: baseline arm contains GT evidence
        elif is_baseline and visible:
            verdict["passed"] = False
            verdict["failure_class"] = "C6"
            verdict["reason"] = "Baseline arm contains GT evidence — gate is broken"
            verdict["severity"] = "FAIL"
            overall_pass = False
            fail_count += 1

        # G1: generated but not visible (non-L6 layers)
        elif generated and not visible:
            verdict["passed"] = False
            verdict["failure_class"] = "G1"
            verdict["reason"] = "gt_layer_events says generated but output.jsonl lacks evidence"
            verdict["severity"] = "FAIL"
            overall_pass = False
            fail_count += 1

        # D1: expected but never fired
        elif expected == "yes" and not generated and not visible:
            verdict["passed"] = True  # warn, not fail
            verdict["failure_class"] = "D1"
            verdict["reason"] = "Expected layer never fired"
            verdict["severity"] = "WARN"
            warn_count += 1

        # Suppressed layer visible (unexpected)
        elif expected == "suppressed" and visible:
            verdict["passed"] = True  # info
            verdict["failure_class"] = ""
            verdict["reason"] = "Suppressed layer was visible — check if intentional"
            verdict["severity"] = "WARN"
            warn_count += 1

        # Broken layer visible (DOC_OF_HONOR contradiction)
        elif expected == "broken(OH)" and visible:
            verdict["passed"] = True  # info — may mean bug was fixed
            verdict["failure_class"] = ""
            verdict["reason"] = "DOC_OF_HONOR says broken but evidence IS visible — update claim"
            verdict["severity"] = "WARN"
            warn_count += 1

        verdicts.append(verdict)

    # E3: hidden prefix leaks
    leaks = autopsy.get("hidden_prefix_leaks", [])
    if leaks:
        verdicts.append({
            "layer": "HIDDEN_PREFIXES",
            "passed": False,
            "failure_class": "E3",
            "reason": f"{len(leaks)} hidden prefix leaks into agent observations",
            "severity": "FAIL",
        })
        overall_pass = False
        fail_count += 1

    result = {
        "task_id": autopsy.get("task_id", "unknown"),
        "overall_pass": overall_pass,
        "fail_count": fail_count,
        "warn_count": warn_count,
        "total_layers": len(layers),
        "verdicts": verdicts,
    }

    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/gt_verify_topology.py <autopsy.json> [--baseline]")
        sys.exit(1)

    autopsy_path = sys.argv[1]
    is_baseline = "--baseline" in sys.argv

    if not os.path.isfile(autopsy_path):
        print(f"ERROR: File not found: {autopsy_path}")
        sys.exit(1)

    with open(autopsy_path, encoding="utf-8") as f:
        autopsy = json.load(f)

    result = verify_topology(autopsy, is_baseline)

    # Print results
    print(f"\nTopology Verification: {result['task_id']}")
    print(f"  Overall: {'PASS' if result['overall_pass'] else 'FAIL'}")
    print(f"  Failures: {result['fail_count']}")
    print(f"  Warnings: {result['warn_count']}")
    print()
    print(f"{'Layer':<25} {'Severity':<8} {'Class':<6} {'Reason'}")
    print("-" * 80)
    for v in result["verdicts"]:
        if v["severity"] != "PASS":
            print(f"{v['layer']:<25} {v['severity']:<8} {v['failure_class']:<6} {v['reason']}")

    # Write result
    out_dir = os.path.dirname(autopsy_path)
    out_path = os.path.join(out_dir, "topology_result.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"\nTopology result written to: {out_path}")

    sys.exit(0 if result["overall_pass"] else 1)


if __name__ == "__main__":
    main()
