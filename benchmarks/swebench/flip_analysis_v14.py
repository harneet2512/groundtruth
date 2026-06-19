#!/usr/bin/env python3
"""v1.4 vs v1.3 flip analysis — understand exactly what tightening broke."""
import json, glob, os, sys
from collections import Counter, defaultdict

# Paths
V13_EVAL_DIR = "/home/Lenovo/logs/run_evaluation/v1.3_g3f_merged/"
V14_EVAL_DIR = "/home/Lenovo/logs/run_evaluation/v14_g3f/"
V14_EVIDENCE_DIR = "/home/Lenovo/results/v14_g3f_submission/gt_logs/"
V13_EVIDENCE_DIR = "/home/Lenovo/results/v1.3_g3f_submission/gt_logs/"

# Try alternate v1.3 paths
for alt in ["/home/Lenovo/logs/run_evaluation/v1.3_g3f_gt/",
            "/home/Lenovo/logs/run_evaluation/v13_flash_gt/",
            "/home/Lenovo/logs/run_evaluation/v1.3_g3f_merged/"]:
    if os.path.isdir(alt):
        V13_EVAL_DIR = alt
        break

# Also try alternate evidence dirs
for alt in ["/home/Lenovo/results/v1.3_g3f_submission/gt_logs/",
            "/home/Lenovo/results/v13_verified_500_20260329_233605/gt_v13/gt_logs/",
            "/home/Lenovo/results/v13_verified_seq_20260330_022557/gt_logs/"]:
    if os.path.isdir(alt):
        V13_EVIDENCE_DIR = alt
        break

print(f"v1.3 eval dir: {V13_EVAL_DIR}")
print(f"v1.4 eval dir: {V14_EVAL_DIR}")
print(f"v1.4 evidence dir: {V14_EVIDENCE_DIR}")
print(f"v1.3 evidence dir: {V13_EVIDENCE_DIR}")
print()

def load_eval_results(eval_dir):
    """Load per-task resolved status from eval reports."""
    results = {}
    # Find the model subdirectory
    for model_dir in glob.glob(os.path.join(eval_dir, "*/")):
        for task_dir in glob.glob(os.path.join(model_dir, "*/")):
            report = os.path.join(task_dir, "report.json")
            if os.path.exists(report):
                try:
                    data = json.load(open(report))
                    for task_id, info in data.items():
                        results[task_id] = info.get("resolved", False)
                except:
                    pass
    return results

def load_evidence_logs(evidence_dir):
    """Load per-task evidence events from JSONL logs."""
    evidence = defaultdict(list)
    for f in glob.glob(os.path.join(evidence_dir, "*.evidence.jsonl")):
        task_id = os.path.basename(f).replace(".evidence.jsonl", "")
        for line in open(f):
            try:
                evidence[task_id].append(json.loads(line))
            except:
                pass
    return evidence

# Load results
v13_results = load_eval_results(V13_EVAL_DIR)
v14_results = load_eval_results(V14_EVAL_DIR)

print(f"v1.3 tasks evaluated: {len(v13_results)}")
print(f"v1.4 tasks evaluated: {len(v14_results)}")

v13_resolved = sum(1 for v in v13_results.values() if v)
v14_resolved = sum(1 for v in v14_results.values() if v)
print(f"v1.3 resolved: {v13_resolved}/{len(v13_results)}")
print(f"v1.4 resolved: {v14_resolved}/{len(v14_results)}")
print()

# Compute flips
common_tasks = set(v13_results.keys()) & set(v14_results.keys())
print(f"Common tasks: {len(common_tasks)}")

gained = []  # v1.3 FAIL -> v1.4 PASS
lost = []    # v1.3 PASS -> v1.4 FAIL
both_pass = 0
both_fail = 0

for task_id in sorted(common_tasks):
    v13_pass = v13_results[task_id]
    v14_pass = v14_results[task_id]
    if v14_pass and not v13_pass:
        gained.append(task_id)
    elif v13_pass and not v14_pass:
        lost.append(task_id)
    elif v13_pass and v14_pass:
        both_pass += 1
    else:
        both_fail += 1

print(f"\n{'='*60}")
print(f"FLIP ANALYSIS: v1.4 vs v1.3")
print(f"{'='*60}")
print(f"Both pass:  {both_pass}")
print(f"Both fail:  {both_fail}")
print(f"GAINED (v1.3 fail -> v1.4 pass): {len(gained)}")
print(f"LOST   (v1.3 pass -> v1.4 fail): {len(lost)}")
print(f"NET: {len(gained) - len(lost):+d}")
print()

# Load evidence for analysis
v14_evidence = load_evidence_logs(V14_EVIDENCE_DIR)
v13_evidence = load_evidence_logs(V13_EVIDENCE_DIR)
print(f"v1.4 evidence logs: {len(v14_evidence)} tasks")
print(f"v1.3 evidence logs: {len(v13_evidence)} tasks")
print()

# Analyze LOST tasks — what did v1.3 show that v1.4 suppressed?
print(f"\n{'='*60}")
print(f"LOST TASKS ANALYSIS ({len(lost)} tasks)")
print(f"{'='*60}")

lost_with_v13_evidence = 0
lost_with_v14_evidence = 0
lost_v13_suppressed_by_threshold = 0
lost_families_v13 = Counter()
lost_families_v14 = Counter()
lost_score_1_count = 0

for task_id in lost:
    repo = task_id.split("__")[0] + "/" + task_id.split("__")[1].split("-")[0]

    # v1.3 evidence
    v13_ev = v13_evidence.get(task_id, [])
    v13_shown = sum(1 for e in v13_ev if e.get("post_edit_evidence_shown"))
    v13_families = set()
    v13_scores = []
    for e in v13_ev:
        for s in e.get("selected", []):
            v13_families.add(s["family"])
            v13_scores.append(s.get("score", 0))
        for fam in e.get("post_edit_families_shown", []):
            lost_families_v13[fam] += 1

    # v1.4 evidence
    v14_ev = v14_evidence.get(task_id, [])
    v14_shown = sum(1 for e in v14_ev if e.get("post_edit_evidence_shown"))
    v14_families = set()
    v14_scores = []
    v14_candidates_score_1 = 0
    for e in v14_ev:
        for s in e.get("selected", []):
            v14_families.add(s["family"])
            v14_scores.append(s.get("score", 0))
        for fam in e.get("post_edit_families_shown", []):
            lost_families_v14[fam] += 1
        # Count candidates that would have passed score >= 1 but not >= 2
        for c in e.get("candidates", []):
            if c.get("score", 0) == 1:
                v14_candidates_score_1 += 1

    if v13_shown > 0:
        lost_with_v13_evidence += 1
    if v14_shown > 0:
        lost_with_v14_evidence += 1
    if v14_candidates_score_1 > 0:
        lost_v13_suppressed_by_threshold += 1
        lost_score_1_count += v14_candidates_score_1

print(f"\nLost tasks with v1.3 evidence shown: {lost_with_v13_evidence}/{len(lost)}")
print(f"Lost tasks with v1.4 evidence shown: {lost_with_v14_evidence}/{len(lost)}")
print(f"Lost tasks where score=1 candidates were SUPPRESSED by v1.4 threshold: {lost_v13_suppressed_by_threshold}/{len(lost)}")
print(f"Total score=1 candidates suppressed in lost tasks: {lost_score_1_count}")
print(f"\nv1.3 families in lost tasks: {dict(lost_families_v13)}")
print(f"v1.4 families in lost tasks: {dict(lost_families_v14)}")

# Detail first 15 lost tasks
print(f"\n--- Lost Task Details (first 15) ---")
for task_id in lost[:15]:
    v13_ev = v13_evidence.get(task_id, [])
    v14_ev = v14_evidence.get(task_id, [])

    v13_shown = sum(1 for e in v13_ev if e.get("post_edit_evidence_shown"))
    v14_shown = sum(1 for e in v14_ev if e.get("post_edit_evidence_shown"))

    v13_fams = set()
    v14_fams = set()
    v14_suppressed_score1 = 0

    for e in v13_ev:
        for fam in e.get("post_edit_families_shown", []):
            v13_fams.add(fam)
    for e in v14_ev:
        for fam in e.get("post_edit_families_shown", []):
            v14_fams.add(fam)
        for c in e.get("candidates", []):
            if c.get("score", 0) == 1:
                v14_suppressed_score1 += 1

    delta = ""
    if v13_shown > 0 and v14_shown == 0:
        delta = " ← EVIDENCE LOST"
    elif v14_suppressed_score1 > 0:
        delta = f" ← {v14_suppressed_score1} score=1 suppressed"

    print(f"  {task_id}: v1.3={v13_shown}ev({','.join(v13_fams) or 'none'}) v1.4={v14_shown}ev({','.join(v14_fams) or 'none'}){delta}")

# Analyze GAINED tasks
print(f"\n{'='*60}")
print(f"GAINED TASKS ANALYSIS ({len(gained)} tasks)")
print(f"{'='*60}")

gained_with_v14_evidence = 0
gained_families = Counter()

for task_id in gained:
    v14_ev = v14_evidence.get(task_id, [])
    v14_shown = sum(1 for e in v14_ev if e.get("post_edit_evidence_shown"))
    if v14_shown > 0:
        gained_with_v14_evidence += 1
    for e in v14_ev:
        for fam in e.get("post_edit_families_shown", []):
            gained_families[fam] += 1

print(f"Gained tasks with v1.4 evidence: {gained_with_v14_evidence}/{len(gained)}")
print(f"v1.4 families in gained tasks: {dict(gained_families)}")

print(f"\n--- Gained Task Details (first 15) ---")
for task_id in gained[:15]:
    v14_ev = v14_evidence.get(task_id, [])
    v14_shown = sum(1 for e in v14_ev if e.get("post_edit_evidence_shown"))
    v14_fams = set()
    for e in v14_ev:
        for fam in e.get("post_edit_families_shown", []):
            v14_fams.add(fam)
    print(f"  {task_id}: v1.4={v14_shown}ev({','.join(v14_fams) or 'none'})")

# GLOBAL EVIDENCE COMPARISON
print(f"\n{'='*60}")
print(f"GLOBAL EVIDENCE COMPARISON")
print(f"{'='*60}")

# v1.4 stats
v14_all_events = []
for evts in v14_evidence.values():
    v14_all_events.extend(evts)

v14_total = len(v14_all_events)
v14_shown_total = sum(1 for e in v14_all_events if e.get("post_edit_evidence_shown"))
v14_suppressed_total = sum(1 for e in v14_all_events if e.get("post_edit_suppressed"))

v14_score_dist = Counter()
v14_candidate_score_dist = Counter()
for e in v14_all_events:
    for s in e.get("selected", []):
        v14_score_dist[s.get("score", 0)] += 1
    for c in e.get("candidates", []):
        v14_candidate_score_dist[c.get("score", 0)] += 1

print(f"\nv1.4 evidence events: {v14_total}")
print(f"  Shown: {v14_shown_total} ({v14_shown_total/max(v14_total,1)*100:.1f}%)")
print(f"  Suppressed: {v14_suppressed_total} ({v14_suppressed_total/max(v14_total,1)*100:.1f}%)")
print(f"  Selected score distribution: {dict(v14_score_dist)}")
print(f"  ALL candidate score distribution: {dict(v14_candidate_score_dist)}")
print(f"  Score=1 candidates KILLED by threshold: {v14_candidate_score_dist.get(1, 0)}")

# v1.3 stats if available
if v13_evidence:
    v13_all_events = []
    for evts in v13_evidence.values():
        v13_all_events.extend(evts)

    v13_total = len(v13_all_events)
    v13_shown_total = sum(1 for e in v13_all_events if e.get("post_edit_evidence_shown"))
    v13_suppressed_total = sum(1 for e in v13_all_events if e.get("post_edit_suppressed"))

    v13_score_dist = Counter()
    v13_candidate_score_dist = Counter()
    for e in v13_all_events:
        for s in e.get("selected", []):
            v13_score_dist[s.get("score", 0)] += 1
        for c in e.get("candidates", []):
            v13_candidate_score_dist[c.get("score", 0)] += 1

    print(f"\nv1.3 evidence events: {v13_total}")
    print(f"  Shown: {v13_shown_total} ({v13_shown_total/max(v13_total,1)*100:.1f}%)")
    print(f"  Suppressed: {v13_suppressed_total} ({v13_suppressed_total/max(v13_total,1)*100:.1f}%)")
    print(f"  Selected score distribution: {dict(v13_score_dist)}")
    print(f"  ALL candidate score distribution: {dict(v13_candidate_score_dist)}")

# COMPRESSION IMPACT — did shorter output help or hurt?
print(f"\n{'='*60}")
print(f"HYPOTHESIS: SCORE THRESHOLD DAMAGE")
print(f"{'='*60}")
total_score1_killed = v14_candidate_score_dist.get(1, 0)
print(f"Total score=1 candidates across ALL tasks: {total_score1_killed}")
print(f"These were ALL suppressed by v1.4's score>=2 threshold.")
print(f"In v1.3 (score>=1), these would have been eligible for selection.")
print(f"")
print(f"Of {len(lost)} lost tasks, {lost_v13_suppressed_by_threshold} had score=1 candidates suppressed.")
print(f"That's {lost_v13_suppressed_by_threshold/max(len(lost),1)*100:.1f}% of losses potentially caused by threshold tightening.")
print(f"")

# Family breakdown of score=1 candidates killed
score1_families = Counter()
for evts in v14_evidence.values():
    for e in evts:
        for c in e.get("candidates", []):
            if c.get("score", 0) == 1:
                score1_families[c.get("family", "?")] += 1

print(f"Score=1 candidates killed by family: {dict(score1_families)}")
print(f"")
print(f"KEY FINDING: If SIBLING/TEST/IMPACT score=1 signals were helping in v1.3,")
print(f"killing them in v1.4 would explain the regression.")

# SUMMARY
print(f"\n{'='*60}")
print(f"SUMMARY")
print(f"{'='*60}")
print(f"v1.3: {v13_resolved}/500 = {v13_resolved/5:.1f}%")
print(f"v1.4: {v14_resolved}/{len(v14_results)} = {v14_resolved/max(len(v14_results),1)*100:.1f}%")
print(f"Delta: {v14_resolved - v13_resolved:+d}")
print(f"Gained: {len(gained)}, Lost: {len(lost)}, Net: {len(gained)-len(lost):+d}")
print(f"")
print(f"LIKELY ROOT CAUSE: score threshold >=2 killed {total_score1_killed} score=1 candidates")
print(f"that were useful context (SIBLING norms, TEST locations, low-score CALLERS).")
print(f"The compression + imperative framing was probably neutral or slightly positive,")
print(f"but the threshold tightening overwhelmed any delivery gains.")
