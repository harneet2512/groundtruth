# Canary V2 Runbook — 3-arm paired tiny run

Status: V2 RE-DISPATCH BLOCKED until local proof gate clears (2026-05-17).

## V2 re-dispatch gate (mandatory)

The 2026-05-17 first canary dispatch produced V2_LIVE with **zero** router
events, no `[GT-router-v2`-tagged observations, and no `router_v2_legacy_skip`
telemetry. Root cause: router helpers wrote only to an in-memory list which
never reached disk artifacts. Fixed in commit-after-`41857aaa`.

**Before any new V2_LIVE GHA dispatch the following must all be true:**

1. `tests/wrapper/test_router_v2_telemetry.py` passes locally
   (6/6 tests covering mode resolution, persistence, counter, off-mode).
2. `python scripts/repro_v2_live_silence.py` runs cleanly against an
   archived trajectory and prints non-zero suppress/emit counts.
3. The wrapper logs at task start: `[GT_META] router_v2 boot: env='live'
   resolved=live pid=N` — proves env propagation into Python runtime.
4. On at least one task: `[GT_META] router_v2 on_view mode=live …` lines
   appear in `full_run.log`, AND `/tmp/gt_interactions_<task>.jsonl`
   contains at least one `"layer": "L3_router_v2"` row, AND
   `gt_layer_events_<task>.jsonl` contains at least one
   `"layer": "L3_router_v2"` row.
5. End-of-task summary prints `[GT_META] router_v2 final: mode=live
   calls=N events_persisted=M` with both > 0.
6. If router emits real evidence: a `[GT_DELIVERY] L3b LIVE post_view
   evidence_len=…` line appears AND the agent's `output.jsonl` contains
   `[GT-router-v2 ` somewhere in `history[*].content`.
7. Legacy `make_view_hook_command` and `make_edit_hook_command_with_artifacts`
   produce ZERO `[GT_DELIVERY] L3` lines on tasks where router emits
   (no double injection). `router_v2_legacy_skip` rows in
   `gt_interactions_<task>.jsonl` corroborate.

If any of (1)–(7) fails, debug locally first. Do NOT dispatch GHA until
all seven gates pass.

## Goal
Produce V2_ROUTER_GT trajectories so the existing CANARY_COMPARISON.md table can
be filled in for column 3. Same model / temperature / max_iter as the OLD_GT and
BASELINE arms.

## GT_ROUTER_V2 modes (Track-B refactor 2026-05-17)
- `off`    — router never instantiated; legacy paths unchanged. Equivalent to baseline GT.
- `shadow` — router runs in parallel and logs structured events; observation NOT mutated.
- `live`   — router emits into the agent observation AND the legacy
             `graph_navigation` / `generate_improved_evidence` path is SKIPPED
             for that event. Router is the sole L3/L3b evidence source. No
             double injection.
- Back-compat: `GT_ROUTER_V2=1` is accepted and maps to `shadow`.

## Arms recap
1. BASELINE       — `GT_ROUTER_V2=off` AND `GT_BASELINE=1` (no GT)
                    — existing dataset in `.tmp_run_20_baseline/`
2. OLD_GT         — `GT_ROUTER_V2=off`, GT layers ON
                    — existing dataset in `.tmp_diag_artifacts/`
3. V2_ROUTER_GT   — `GT_ROUTER_V2=live`, GT layers ON, **legacy L3/L3b suppressed by wrapper**
                    — must be re-run; the prior `GT_ROUTER_V2=1` runs were shadow, not live

## Pick 2–3 tasks
Per the directive: cover one task where OLD_GT previously helped, one where it
likely hurt, and (optionally) one blind-holdout-style task.

Recommended from existing canary data (action_economy = OLD_GT/BL):

| task | OLD_GT action_economy vs BL | OLD_GT helped? |
|------|-----------------------------|----------------|
| `beetbox__beets-5495`     | 0.40 | helped |
| `delgan__loguru-1297`     | 2.46 | hurt (over-injection) |
| `beancount__beancount-931`| 1.19 | neutral / slight regression |

If a holdout task is desired, pick any task NOT in `.tmp_diag_artifacts/` and NOT
in `.tmp_run_20_baseline/` so V2 isn't compared against memorized traces.

## Required env vars (per task)
```bash
# V2_ROUTER_GT arm
export GT_ROUTER_V2=live                                    # router is the sole L3/L3b path
export GT_REPO_ROOT=/workspace/<task>                       # canonicalization root
export GT_ARTIFACT_DIR=/path/to/canary/v2/task-<task>/results/... \
                                                            # where _pull_graph_db_artifact lands graph.db
export GT_HOOK_LOG=/tmp/gt_hooks.log
export DEEPSEEK_API_KEY=...                                 # same model as OLD_GT runs

# OLD_GT arm — same wrapper, GT_ROUTER_V2 unset (or =off)
# BASELINE arm — GT_BASELINE=1 plus GT_ROUTER_V2 unset
```

After each task, verify the V2 arm appended router emissions (not legacy):
```bash
grep -c '\[GT-router-v2 ' /path/to/canary/v2/task-<task>/results/.../output.jsonl
grep -c '"router_v2_legacy_skip"' /path/to/canary/v2/task-<task>/results/.../gt_interactions.jsonl
# Expect both > 0. If legacy_path_skipped is missing, live mode did not engage.
```

## Launch command (per task)
```bash
python scripts/swebench/oh_gt_full_wrapper.py \
  --instance-ids '<task_id>' \
  -l eval -i 100 \
  --eval-num-workers 1 \
  --eval-output-dir /path/to/canary/v2/task-<task_id> \
  --dataset 'SWE-bench-Live/SWE-bench-Live' \
  --split lite
```

Run BASELINE / OLD_GT comparison arms with the SAME `--instance-ids` /
`-i 100` / `--split lite` flags. BASELINE sets `GT_BASELINE=1`. OLD_GT keeps
the GT layers on and **leaves `GT_ROUTER_V2` unset (= off)**.

## Per-task artifacts to collect
The wrapper already writes these to `--eval-output-dir`:
- `output.jsonl`                  — agent trajectory
- `gt_interactions.jsonl`         — structured GT layer events (incl. `router_v2` entries when flag is ON)
- `gt_hooks.log`                  — hook fire/skip log

The flag-on path additionally:
- writes `{layer: "L3_router_v2", "mode": "shadow"|"live", ...}` rows into `gt_interactions.jsonl`
- registers each emit as a pending suggestion on the in-process `AgentState`
- in `live` mode: writes `{"router_v2_legacy_skip": {trigger, file, router_emitted}}`
  rows to prove the legacy graph_navigation / generate_improved_evidence path was bypassed
- in `live` mode: appends `[GT-router-v2 on_view|on_edit]` blocks to the
  observation when the router decides to emit. These markers MUST appear in
  `output.jsonl` for the V2 arm to count.

`_pull_graph_db_artifact(config)` (already wired into the wrapper's task-end paths)
will copy `graph.db` into `$GT_ARTIFACT_DIR` so the shadow replay can use it.

## Build the comparison
After all 3 arms finish:
```bash
python scripts/compute_canary_metrics.py \
  --baseline /path/to/canary/baseline \
  --old-gt   /path/to/canary/old_gt \
  --v2       /path/to/canary/v2 \
  --report   reports/canary/CANARY_COMPARISON.md \
  --json     reports/canary/canary_metrics.json
```

## Decision rule (locked)
- V2 worse than OLD_GT on action-path metrics → **do not** continue V2 activation.
- V2 matches OLD_GT with fewer stale/late/injection events → continue to 5-task paired holdout.
- V2 beats OLD_GT and BASELINE on action-path metrics → canary **pass**, not a success claim.

## Hard rules
- No tuning thresholds on task outcomes.
- No claim of success from internal tests.
- Resolve is a lagging signal, not the gate.
- Do not run 15 / 30 / 300 tasks until the 2–3 task canary has been read and a 5-task paired holdout has run cleanly.

## Cost expectation
3 arms × 3 tasks × DeepSeek V4 Flash ≈ 9 task-runs at ~$0.05 each ≈ **$0.45 LLM**.
Plus VM time. Total under $5.
