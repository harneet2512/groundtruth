# CANARY_COMPARISON — 3-arm paired metric table

Status: regression-detection canary. **Not a success claim.**

## Arms
- BASELINE     : `D:/tmp/canary_done/baseline` (3 tasks)
- OLD_GT       : `D:/tmp/canary_done/old_gt` (3 tasks)
- V2_ROUTER_GT : `D:/tmp/canary_done` (3 tasks)
- shared tasks across all populated arms: 3

## Per-task table

### beancount__beancount-931

| metric | baseline | OLD_GT | V2_ROUTER_GT |
|--------|----------|--------|---------------|
| first_gold_view_step | 4 | — | 9 |
| first_gold_edit_step | 30 | 30 | 36 |
| files_viewed_before_gold | 0 | 0 | 1 |
| action_count | 48 | 47 | 54 |
| edit_file_precision | 1.00 | 1.00 | 1.00 |
| bridge_event_before_gold | 0 | 2 | 0 |
| agent_followed_gt_edge | 0 | 0 | 0 |
| stale_guidance_count | 0 | 0 | 0 |
| late_guidance_count | 0 | 0 | 0 |
| injections_per_task | 0 | 6 | 2 |
| resolved | N | N | N |
| action_economy (GT/BL) | 1.00 | 0.98 | 1.12 |

*Note: gold files differ across arms — baseline `['beancount/plugins/leafonly.py']` vs OLD_GT `['.openhands/TASKS.md', 'beancount/plugins/leafonly.py']`*

### beetbox__beets-5495

| metric | baseline | OLD_GT | V2_ROUTER_GT |
|--------|----------|--------|---------------|
| first_gold_view_step | 26 | 4 | 26 |
| first_gold_edit_step | 25 | 16 | 25 |
| files_viewed_before_gold | 0 | 0 | 0 |
| action_count | 50 | 47 | 50 |
| edit_file_precision | 1.00 | 1.00 | 1.00 |
| bridge_event_before_gold | 0 | 1 | 0 |
| agent_followed_gt_edge | 0 | 0 | 0 |
| stale_guidance_count | 0 | 2 | 0 |
| late_guidance_count | 0 | 0 | 0 |
| injections_per_task | 0 | 10 | 0 |
| resolved | N | N | N |
| action_economy (GT/BL) | 1.00 | 0.94 | 1.00 |

*Note: gold files differ across arms — baseline `['.openhands/TASKS.md', 'beets/importer.py']` vs OLD_GT `['beets/importer.py']`*

### delgan__loguru-1297

| metric | baseline | OLD_GT | V2_ROUTER_GT |
|--------|----------|--------|---------------|
| first_gold_view_step | 3 | 19 | 3 |
| first_gold_edit_step | 14 | 7 | 14 |
| files_viewed_before_gold | 0 | 0 | 0 |
| action_count | 47 | 22 | 47 |
| edit_file_precision | 1.00 | 1.00 | 1.00 |
| bridge_event_before_gold | 0 | 2 | 0 |
| agent_followed_gt_edge | 0 | 0 | 0 |
| stale_guidance_count | 0 | 0 | 0 |
| late_guidance_count | 0 | 0 | 0 |
| injections_per_task | 0 | 4 | 0 |
| resolved | N | N | N |
| action_economy (GT/BL) | 1.00 | 0.47 | 1.00 |

## Aggregate medians (over shared tasks)

| metric | baseline | OLD_GT | V2_ROUTER_GT |
|--------|----------|--------|---------------|
| first_gold_view_step | 4 | 11.50 | 9 |
| files_viewed_before_gold | 0 | 0 | 0 |
| action_count | 48 | 47 | 50 |
| edit_file_precision | 1.00 | 1.00 | 1.00 |
| injections_per_task | 0 | 6 | 0 |
| stale_guidance_count | 0 | 0 | 0 |
| late_guidance_count | 0 | 0 | 0 |
| resolved (of 3) | 0 | 0 | 0 |

## Decision rule (per session directive)

- If V2 is worse than OLD_GT on action-path metrics: do NOT continue V2 activation.
- If V2 matches OLD_GT with fewer stale/late/injection events: continue to 5-task paired holdout.
- If V2 beats OLD_GT and BASELINE on action-path metrics: this is still a canary pass, not a success.

## Decision note (2026-05-17, commit `b3fccb4f`)

### Ruling: V2_LIVE is worse than OLD_GT on action-path metrics. Do NOT continue V2 activation.

Per-task action_economy (GT/BL): V2 is >= 1.00 on every task (= no improvement over baseline), while OLD_GT is < 1.00 on 2/3 tasks (= faster than baseline). V2 first_gold_edit is later than OLD_GT on every task (36>30, 25>16, 14>7). V2 injections_per_task = 0 on 2/3 tasks (= no GT evidence delivered). V2 bridge_event_before_gold = 0 on every task (= no GT helped the agent find gold pre-localization).

### Root cause (not a code bug)

V2_LIVE's router wiring is correct (13–18 calls per task, all persisted). But the router suppresses with `no_graph_db` on 100% of invocations because the host-side graph.db is only downloaded after the first L6 reindex (post-edit), and the router is cached on the first call (pre-edit). The live-mode bypass correctly suppresses the legacy L3b/L3 hook path — so the agent gets **zero** L3/L3b help during localization. V2_LIVE is behaviorally identical to BASELINE.

See `reports/canary/V2_LIVE_DEEP_TELEMETRY_2026-05-17.md` and `RUNTIME_PARITY_AUDIT.md` addendum B-7 for cited evidence.

### What OLD_GT does that V2_LIVE cannot (yet)

OLD_GT runs `make_view_hook_command` / `make_edit_hook_command_with_artifacts` as subprocesses **inside the container** where `config.graph_db` is a valid container-local path. The hook script reads the DB, produces `Called by:` / `SIGNATURE:` / `SIBLING:` evidence, and the wrapper appends it to `obs.content`. This is why OLD_GT has injections (6/10/4 per task) and V2_LIVE has 0.

### What must change before V2_LIVE can be re-tested

One of these B-7 fix candidates (from RUNTIME_PARITY_AUDIT.md):
1. Pre-fetch graph.db to host at task start (before first agent action).
2. Reset router cache after L6 reindex populates `_host_graph_db`.
3. Run router via in-container subprocess.

Until at least one lands AND a re-canary shows V2 `injections_per_task > 0` on tasks where OLD_GT delivers, V2_LIVE must not replace the legacy path.

### Proven (this canary)
- Track-A wiring: env→mode→router call→persistence→fail-fast all green.
- Track-B layer separation: router is invoked, legacy is bypassed, no double injection.
- V2_LIVE does not introduce regressions vs baseline (action_economy ~1.0, edit_precision 1.0).

### Not proven (cannot claim)
- That V2_LIVE helps. It delivers zero agent-visible evidence.
- That V2_LIVE is comparable to OLD_GT. It is strictly weaker.
- That the B-7 fix restores parity. Requires a re-canary.

## Notes

- All metrics are descriptive. The canary is a regression-detection gate, not an evaluation.
- Resolve is a lagging outcome; do not use it as a single signal.
- `bridge_event_before_gold`, `agent_followed_gt_edge`, `stale_guidance_count`, `late_guidance_count`, `injections_per_task` are derived from `[GT]` and `<gt-` markers in the trajectory observation `content` field — this matches the live wrapper's `append_observation` format.
- BASELINE traces should have GT-derived metrics at 0; non-zero indicates a leak. V2_LIVE having injections=2 on beancount comes from L1 brief `<gt-task-brief>` tags (not router emission).
- beets-5495 baseline used archived data from `.tmp_run_20_baseline` because the GHA run's beets task was cancelled at the 90-min timeout.

## GHA run inventory

| Arm | Run ID | Conclusion | Commit |
|---|---|---|---|
| BASELINE | `25994197610` | cancelled (beets timeout; 2/3 valid + archive fallback) | `c350347c` |
| OLD_GT | `25994590953` | success | `41857aaa` |
| V2_ROUTER_GT | `25995605932` | success | `b3fccb4f` |
