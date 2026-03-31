import json, os, glob, sys
from collections import defaultdict

results_dir = "/home/Lenovo/results/v13_verified_500_20260329_233605"
output_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/v13_analysis.json"

# Load predictions
bl_preds = json.load(open(os.path.join(results_dir, "baseline/preds.json")))
gt_preds = json.load(open(os.path.join(results_dir, "gt_v13/preds.json")))

# Find resolved task IDs from eval logs
bl_resolved = set()
gt_resolved = set()

for d in glob.glob("logs/run_evaluation/v13_flash_baseline/openai__gemini-flash/*/report.json"):
    r = json.load(open(d))
    iid = os.path.basename(os.path.dirname(d))
    if r.get("resolved", False):
        bl_resolved.add(iid)

for d in glob.glob("logs/run_evaluation/v13_flash_gt/openai__gemini-flash/*/report.json"):
    r = json.load(open(d))
    iid = os.path.basename(os.path.dirname(d))
    if r.get("resolved", False):
        gt_resolved.add(iid)

# Gained and lost
gained = sorted(gt_resolved - bl_resolved)
lost = sorted(bl_resolved - gt_resolved)
both = bl_resolved & gt_resolved

# GT utilization
log_dir = os.path.join(results_dir, "gt_v13/gt_logs")
ev_files = set()
for f in glob.glob(os.path.join(log_dir, "*.evidence.jsonl")):
    name = os.path.basename(f).replace(".evidence.jsonl", "")
    ev_files.add(name)

# GT log analysis
gt_log_path = os.path.join(results_dir, "gt_v13/minisweagent.log")
import re
indexed_tasks = set()
briefed_tasks = set()
if os.path.exists(gt_log_path):
    for line in open(gt_log_path):
        m = re.search(r"v11 Go indexer: (\S+)", line)
        if m:
            indexed_tasks.add(m.group(1))
        m = re.search(r"v12 briefing for (\S+):", line)
        if m:
            briefed_tasks.add(m.group(1))

# Evidence analysis
evidence_shown = 0
evidence_suppressed = 0
families_counter = defaultdict(int)
total_candidates = 0
total_admissible = 0
total_rejected_nm = 0

for f in glob.glob(os.path.join(log_dir, "*.evidence.jsonl")):
    for line in open(f):
        try:
            e = json.loads(line)
            adm = e.get("v13_admissibility", {})
            total_candidates += adm.get("admissible_candidates", 0) + adm.get("edges_name_match_rejected", 0)
            total_admissible += adm.get("admissible_candidates", 0)
            total_rejected_nm += adm.get("edges_name_match_rejected", 0)
            if e.get("post_edit_evidence_shown"):
                evidence_shown += 1
                for fam in e.get("post_edit_families_shown", []):
                    families_counter[fam] += 1
            elif e.get("post_edit_suppressed"):
                evidence_suppressed += 1
        except:
            pass

# Per-repo breakdown
repo_stats = defaultdict(lambda: {"total": 0, "bl": 0, "gt": 0, "gained": [], "lost": []})
all_tasks = set(bl_preds.keys()) | set(gt_preds.keys())
for t in all_tasks:
    repo = "__".join(t.split("__")[:2])
    repo_stats[repo]["total"] += 1
    if t in bl_resolved:
        repo_stats[repo]["bl"] += 1
    if t in gt_resolved:
        repo_stats[repo]["gt"] += 1
    if t in gained:
        repo_stats[repo]["gained"].append(t)
    if t in lost:
        repo_stats[repo]["lost"].append(t)

# Trajectory stats
bl_turns = 0
gt_turns = 0
bl_api_calls = 0
gt_api_calls = 0
bl_traj_count = 0
gt_traj_count = 0

for t in glob.glob(os.path.join(results_dir, "baseline/*/*.traj.json")):
    try:
        d = json.load(open(t))
        bl_turns += len(d.get("messages", []))
        bl_api_calls += d.get("info", {}).get("model_stats", {}).get("api_calls", 0)
        bl_traj_count += 1
    except:
        pass

for t in glob.glob(os.path.join(results_dir, "gt_v13/*/*.traj.json")):
    try:
        d = json.load(open(t))
        gt_turns += len(d.get("messages", []))
        gt_api_calls += d.get("info", {}).get("model_stats", {}).get("api_calls", 0)
        gt_traj_count += 1
    except:
        pass

# Patch quality
bl_with_patch = sum(1 for v in bl_preds.values() if isinstance(v, dict) and v.get("model_patch", "").strip())
gt_with_patch = sum(1 for v in gt_preds.values() if isinstance(v, dict) and v.get("model_patch", "").strip())

# Build output
analysis = {
    "top_line": {
        "baseline_resolved": len(bl_resolved),
        "gt_resolved": len(gt_resolved),
        "delta": len(gt_resolved) - len(bl_resolved),
        "baseline_pct": round(len(bl_resolved) / 500 * 100, 1),
        "gt_pct": round(len(gt_resolved) / 500 * 100, 1),
    },
    "flips": {
        "gained": gained,
        "gained_count": len(gained),
        "lost": lost,
        "lost_count": len(lost),
        "both_resolved": len(both),
        "net_delta": len(gained) - len(lost),
    },
    "gt_delivery": {
        "indexer_success": len(indexed_tasks),
        "briefings_shown": len(briefed_tasks),
        "evidence_log_files": len(ev_files),
        "evidence_shown": evidence_shown,
        "evidence_suppressed": evidence_suppressed,
        "total_candidates": total_candidates,
        "total_admissible": total_admissible,
        "total_rejected_name_match": total_rejected_nm,
        "families_shown": dict(families_counter),
    },
    "gt_attribution": {
        "gained_with_gt_evidence": len(set(gained) & ev_files),
        "gained_with_gt_briefing": len(set(gained) & briefed_tasks),
        "lost_with_gt_evidence": len(set(lost) & ev_files),
        "lost_with_gt_briefing": len(set(lost) & briefed_tasks),
    },
    "patch_production": {
        "bl_with_patch": bl_with_patch,
        "gt_with_patch": gt_with_patch,
        "delta_patches": gt_with_patch - bl_with_patch,
    },
    "agent_behavior": {
        "bl_trajectories": bl_traj_count,
        "gt_trajectories": gt_traj_count,
        "bl_total_turns": bl_turns,
        "gt_total_turns": gt_turns,
        "bl_avg_turns": round(bl_turns / max(bl_traj_count, 1), 1),
        "gt_avg_turns": round(gt_turns / max(gt_traj_count, 1), 1),
        "bl_total_api_calls": bl_api_calls,
        "gt_total_api_calls": gt_api_calls,
    },
    "per_repo": {},
}

for repo in sorted(repo_stats.keys(), key=lambda r: repo_stats[r]["total"], reverse=True):
    s = repo_stats[repo]
    analysis["per_repo"][repo] = {
        "total": s["total"],
        "bl_resolved": s["bl"],
        "gt_resolved": s["gt"],
        "delta": s["gt"] - s["bl"],
        "gained": s["gained"],
        "lost": s["lost"],
    }

json.dump(analysis, open(output_path, "w"), indent=2)
print(json.dumps(analysis, indent=2))
