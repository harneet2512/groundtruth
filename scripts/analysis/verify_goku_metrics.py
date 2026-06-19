"""Verify Decision 34 metrics from a real GHA run artifact.

Downloads artifacts, runs metrics aggregator, checks every cell filled.
Usage: python scripts/analysis/verify_goku_metrics.py <run_id>
"""

from __future__ import annotations

import json
import os
import sys
import glob
import subprocess
import tempfile


def download_artifacts(run_id: str, dest: str) -> None:
    subprocess.run(
        ["gh", "run", "download", str(run_id), "--dir", dest],
        check=True,
    )


def find_jsonl(artifact_dir: str, pattern: str) -> list[str]:
    return glob.glob(os.path.join(artifact_dir, "**", pattern), recursive=True)


def load_jsonl(path: str) -> list[dict]:
    records = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                records.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    return records


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python verify_goku_metrics.py <run_id>")
        sys.exit(1)

    run_id = sys.argv[1]
    dest = os.path.join(tempfile.gettempdir(), f"goku_verify_{run_id}")
    os.makedirs(dest, exist_ok=True)

    print(f"Downloading artifacts from run {run_id}...")
    download_artifacts(run_id, dest)

    layer_files = find_jsonl(dest, "gt_layer_events_*.jsonl")
    reaction_files = find_jsonl(dest, "gt_agent_reactions_*.jsonl")
    agent_files = find_jsonl(dest, "gt_agent_events_*.jsonl")
    belief_files = find_jsonl(dest, "gt_belief_ledger_*.jsonl")
    summary_files = find_jsonl(dest, "gt_run_summary_*.json")

    print(f"\nArtifacts found:")
    print(f"  Layer events:   {len(layer_files)} files")
    print(f"  Agent reactions: {len(reaction_files)} files")
    print(f"  Agent events:   {len(agent_files)} files")
    print(f"  Belief ledger:  {len(belief_files)} files")
    print(f"  Run summaries:  {len(summary_files)} files")

    if not layer_files:
        print("\nFATAL: No gt_layer_events JSONL found. GT_STRUCTURED_EVENTS may be off.")
        sys.exit(1)

    all_layer = []
    for f in layer_files:
        all_layer.extend(load_jsonl(f))
    all_reactions = []
    for f in reaction_files:
        all_reactions.extend(load_jsonl(f))
    all_agent = []
    for f in agent_files:
        all_agent.extend(load_jsonl(f))

    print(f"\nRecords loaded:")
    print(f"  Layer events:   {len(all_layer)}")
    print(f"  Agent reactions: {len(all_reactions)}")
    print(f"  Agent events:   {len(all_agent)}")

    # Run the metrics aggregator
    from groundtruth.telemetry.metrics import compute_run_summary, print_summary

    lf = layer_files[0] if layer_files else ""
    rf = reaction_files[0] if reaction_files else ""
    af = agent_files[0] if agent_files else ""
    bf = belief_files[0] if belief_files else ""

    summary = compute_run_summary(lf, rf, af, bf)
    print_summary(summary)

    # Check every metric section present
    sections = ["l1", "l3", "l3b", "l5", "l6", "hygiene", "meta_reaction", "agent_events"]
    missing_sections = [s for s in sections if s not in summary]
    if missing_sections:
        print(f"\nFAIL: Missing sections: {missing_sections}")
    else:
        print("\nPASS: All metric sections present")

    # Check for blank cells
    blanks = []
    for section_name in sections:
        section = summary.get(section_name, {})
        for key, value in section.items():
            if value is None:
                blanks.append(f"{section_name}.{key}")
    if blanks:
        print(f"FAIL: Blank metrics: {blanks}")
    else:
        print("PASS: No blank metrics")

    # Check utilization
    below_threshold = []
    for layer, data in summary.get("per_layer", {}).items():
        score = data.get("utilization_score", 0)
        reason = data.get("utilization_reason", "")
        if score < 0.75 and not reason.startswith("by_design:"):
            below_threshold.append(f"{layer}: {score} (reason: {reason})")
    if below_threshold:
        print(f"FAIL: Layers below 0.75 without documented reason: {below_threshold}")
    else:
        print("PASS: All layers >= 0.75 or documented reason")

    # Check proof spine
    if summary.get("proof_spine_pass"):
        print("PASS: Proof spine")
    else:
        print(f"FAIL: Proof spine: {summary.get('proof_spine')}")

    # Check hard fails
    if summary.get("run_valid"):
        print("PASS: No hard fails")
    else:
        print(f"FAIL: Hard fails: {summary.get('hard_fails')}")

    # Check Decision 34 specific: L5 events generalized
    l5_events = [e for e in all_layer if e.get("layer") == "L5"]
    framework_violations = []
    for e in l5_events:
        et = e.get("event_type", "")
        for fw in ("pytest", "jest", "cargo", "go_test", "npm_test"):
            if fw in et.lower():
                framework_violations.append(et)
    if framework_violations:
        print(f"FAIL: L5 framework names in event types: {framework_violations}")
    else:
        print("PASS: L5 event types generalized (no framework names)")

    # Check GTAgentEvent stream
    if all_agent:
        buckets = set(e.get("event_bucket", "") for e in all_agent)
        print(f"PASS: GTAgentEvent stream populated ({len(all_agent)} events, buckets: {buckets})")
    else:
        print("FAIL: GTAgentEvent stream empty (GT_DEEP_LAYER_GROUNDED_METRICS may be off)")

    # Write verification result
    result_path = os.path.join(dest, "goku_verification_result.json")
    result = {
        "run_id": run_id,
        "layer_events": len(all_layer),
        "agent_events": len(all_agent),
        "reactions": len(all_reactions),
        "missing_sections": missing_sections,
        "blank_metrics": blanks,
        "below_threshold": below_threshold,
        "proof_spine_pass": summary.get("proof_spine_pass"),
        "run_valid": summary.get("run_valid"),
        "framework_violations": framework_violations,
        "all_pass": (
            not missing_sections
            and not blanks
            and not below_threshold
            and summary.get("proof_spine_pass")
            and summary.get("run_valid")
            and not framework_violations
            and len(all_agent) > 0
        ),
    }
    with open(result_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nVerification result written to {result_path}")
    print(f"\n{'='*40}")
    print(f"OVERALL: {'PASS' if result['all_pass'] else 'FAIL'}")
    print(f"{'='*40}")

    sys.exit(0 if result["all_pass"] else 1)


if __name__ == "__main__":
    main()
