# Canary Run Verification Checklist

> Run this checklist after EVERY canary/eval run. A run is invalid if any MUST item fails.

## GT Stack Verification (MUST pass)

### 1. GT Install (per Docker container)
- [ ] `[GT] groundtruth package installed (wheel)` in trace log
- [ ] `[GT] pyright installed` in trace log
- [ ] `[GT] Index built: N nodes` where N > 0

### 2. GT Tools Called (per task)
- [ ] `gt_orient` called ≥1x (agent uses it at start)
- [ ] `gt_lookup` called ≥1x (agent investigates symbols)
- [ ] `gt_check` called ≥1x (agent checks edits)
- [ ] `gt_impact` called ≥1x on tasks where agent edits functions with callers

### 3. Hook Firing (per task)
- [ ] Startup briefing delivered (`gt_evidence` in first state retrieval)
- [ ] Hook runs after every action (`_state_gt_v2` in trace log)
- [ ] `material_edit` event logged when agent edits source files

### 4. Micro-Update Channel (v2.0+)
- [ ] `GT MICRO` appears in trace for tasks where agent edits source files
- [ ] Micro fires on FIRST material edit to a file (not suppressed by stale hash)
- [ ] Confidence tier is `verified` or `likely` (never presents ambiguous as fact)
- [ ] Dedup works: same micro NOT repeated >2x consecutively

### 5. Verification Channel (v2.0+)
- [ ] `GT VERIFY` fires at pre-submit OR every 3rd edit
- [ ] Verify budget not exceeded (≤8 per task, presubmit exempt)

### 6. Token / Auth
- [ ] No `401 ACCESS_TOKEN_EXPIRED` errors in debug logs
- [ ] All 10 tasks produce trajs (no 1-step exits from auth failure)
- [ ] Token refreshed between batches if run >30 min

### 7. Predictions
- [ ] `preds.json` exists with ≥8/10 entries
- [ ] ≥50% of entries have non-empty `model_patch`
- [ ] No task has `exit_status: exit_error` from auth (1-step trajs = token issue)

## How to Check

```bash
# GT Install
grep '\[GT\]' /tmp/canary_*/run1/astropy__astropy-12907/*.trace.log

# Tool calls (per task)
for tlog in /tmp/canary_*/run1/*/*.trace.log; do
  task=$(basename $(dirname $tlog))
  orient=$(grep -c gt_orient $tlog)
  lookup=$(grep -c gt_lookup $tlog)
  check=$(grep -c gt_check $tlog)
  micro=$(grep -c 'GT MICRO' $tlog)
  echo "$task: orient=$orient lookup=$lookup check=$check micro=$micro"
done

# Token errors
grep -r '401\|ACCESS_TOKEN' /tmp/canary_*/run*/*.debug.log | wc -l

# Patch rate
python3 -c "
import json, glob
for f in sorted(glob.glob('/tmp/canary_*/run*/preds*.json')):
    p = json.load(open(f))
    patches = sum(1 for v in p.values() if v.get('model_patch','').strip())
    print(f'{f}: {patches}/{len(p)} patches')
"
```

## Red Flags (investigate immediately)
- `gt_orient` = 0 → GT tools not in agent's tool bundle
- `GT MICRO` = 0 on ALL tasks → hook query broken (check `assertions` table schema)
- All tasks 1-step `exit_error` → token expired, auth broken
- `gt_evidence` never appears in state → hook not wired into SWE-agent config
- `[GT] groundtruth package install FAILED` → wheel incompatible with Docker Python version
