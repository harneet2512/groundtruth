# V2_LIVE Deep Telemetry — Canary 2026-05-17

Session: FINAL_ARCH_V2 canary (Track-B GT_ROUTER_V2 off/shadow/live).
Date: 2026-05-17.
Branch: `jedi__branch` at `b3fccb4f`.
Run: GHA `canary_3arm` workflow, run id `25995605932`, conclusion `success`.

## Status

**Wiring: PROVEN.** All 7 runbook gates pass.
**Behavioural delivery: NOT PROVEN.** Router is wired but blind — all 37 invocations across 3 tasks return `no_graph_db` because the host-side graph.db is not populated before the first post-edit L6 reindex.

## What the artifacts show

| Task | Layer events | L3_router_v2 rows | on_view | on_edit | Router emits | Suppression dist | Unique files seen |
|---|---:|---:|---:|---:|---:|---|---:|
| `beancount__beancount-931` | 22 | 17 | 15 | 2 | 0 | `{no_graph_db: 17}` | 8 |
| `beetbox__beets-5495`      | 25 | 18 | 14 | 4 | 0 | `{no_graph_db: 18}` | 5 |
| `delgan__loguru-1297`      |  6 |  2 |  1 | 1 | 0 | `{no_graph_db:  2}` | 1 |

Router boot log (sampled):
```
[GT_META] router_v2 boot: env='live' resolved=live pid=5211   # beancount
[GT_META] router_v2 boot: env='live' resolved=live pid=5240   # beets
[GT_META] router_v2 boot: env='live' resolved=live pid=5115   # loguru
```

End-of-task final lines:
```
[GT_META] router_v2 final: mode=live calls=17 events_persisted=34   # beancount
[GT_META] router_v2 final: mode=live calls=18 events_persisted=36   # beets
[GT_META] router_v2 final: mode=live calls=2  events_persisted=4    # loguru
```

(events_persisted = router_v2 events + router_v2_legacy_skip events; each call writes both rows.)

## Layer hits (per task) — confirms legacy bypass

```
beancount: {L1:1 L2:1 L4:3 L6:2 L5:skip}  L3=0  L3b=0   (router-only path)
beets:     {L1:1 L2:1 L4:3 L6:4 L5:skip}  L3=0  L3b=0
loguru:    {L1:1 L2:1 L4:3 L6:1 L5:skip}  L3=0  L3b=0
```

vs. OLD_GT (run `25994590953`, same 3 tasks):
- `beets-5495`: 9 L3b events + 2 L3 events fired with `Called by:` / `SIGNATURE:` evidence written to obs.content (verified in archived trajectory: 3 `Called by`, 2 `SIGNATURE: def remove_duplicates`).

So: in live mode the wrapper correctly suppresses the legacy path; without router emission, the agent gets **no L3/L3b help during localization**.

## Output.jsonl markers (agent-visible content)

| Task | `[GT-router-v2` | `<gt-` (mostly L1 brief) | `Called by:` | `CALLERS:` | `SIGNATURE:` |
|---|---:|---:|---:|---:|---:|
| beancount | 0 | 17 | 0 | 0 | 0 |
| beets     | 0 | 17 | 0 | 0 | 0 |
| loguru    | 0 | 17 | 0 | 0 | 0 |

`<gt-` markers come from the L1 task brief (`<gt-task-brief>` block in user message) and pre-submit gate; neither is a router or L3b emission. **Zero post-view / post-edit GT evidence reached the agent.**

## Root cause

Wrapper init order:
1. Agent starts; L1 brief injected via instruction.
2. Pre-edit phase — agent reads files; wrapper hits `post_view` branch.
   - `_router_v2_on_view` called.
   - `_ensure_v2_router` reads `_host_graph_db` (empty) → falls back to `config.graph_db` (container path, not openable from host).
   - Router calls `_load_graph_db` → file does not exist → `RouterEmission(no_graph_db)`.
   - Persisted to `gt_layer_events_*.jsonl` and `gt_interactions_*.jsonl`.
   - Live-mode bypass calls `_write_router_v2_legacy_skip` → legacy hook does not fire.
3. Agent edits a file; wrapper hits `post_edit` branch.
   - Same router call, same `no_graph_db`.
   - L6 reindex fires; later in the same branch (`_l6_post_edit_reindex_then_pull`), the wrapper pulls the freshly-rebuilt container DB to host into `_host_graph_db`.
4. By the time `_host_graph_db` is populated, the agent has already made its first edit. Subsequent on_view / on_edit calls could see a graph, but the trace shows they continued to suppress — likely because the cached router was instantiated with the original (empty) `db_path` and never re-loaded.

## Citations

- Wrapper: `scripts/swebench/oh_gt_full_wrapper.py:1521` — `db_path = getattr(config, "_host_graph_db", "") or config.graph_db` — chooses host DB if present else container path (which the host process cannot open).
- Wrapper: `scripts/swebench/oh_gt_full_wrapper.py:1555` — `config._router_v2 = router; return router` — router cached on `config`; subsequent calls return the same instance without re-checking `_host_graph_db`.
- Wrapper: `scripts/swebench/oh_gt_full_wrapper.py:2613` (approx) — host-side graph.db download happens during the post_edit branch, AFTER `_ensure_v2_router` has already cached the router with `db_path=""`.

## Fix candidates (Track-B follow-up)

1. **Pre-fetch graph.db to host at task start**, before first agent action. Costs one extra in-container build per task but unblocks pre-edit router emission.
2. **Re-init router after host DB lands.** When `_l6_post_edit_reindex_then_pull` populates `_host_graph_db`, reset `config._router_v2 = None` so the next call re-instantiates with the real DB path.
3. **In-container router invocation.** Run the router via subprocess inside the container so it uses the container-resident graph.db directly. Larger refactor; mirrors how the legacy L3b hook works.

None of these are part of the current Track-B scope (which was: implement off/shadow/live + telemetry parity). The decision rule below applies regardless of which fix is chosen.

## Locked decision rule (per runbook)

- V2 worse than OLD_GT on action-path metrics → **do not** continue V2 activation.
- V2 matches OLD_GT with fewer stale/late/injection events → continue to 5-task paired holdout.
- V2 beats OLD_GT and BASELINE on action-path metrics → canary pass, not a success claim.

Given V2_LIVE delivers zero agent-visible evidence in this canary, V2_LIVE cannot **beat** OLD_GT. Whether it is **worse than** OLD_GT depends on the action_count / first_gold_view metrics that the paired analysis (after baseline finishes) will compute. Stop after `CANARY_COMPARISON.md` lands.

## Proven (this artifact set)
- env propagation: `GT_ROUTER_V2=live` reaches wrapper process.
- `_router_v2_mode()` returns `live`.
- Router invoked on post_view and post_edit events.
- Events persisted to BOTH `gt_interactions_<task>.jsonl` (per fix `b3fccb4f`) AND `gt_layer_events_<task>.jsonl` (after enum fix `b3fccb4f`).
- Live-mode legacy bypass executes — no double injection.
- End-of-task fail-fast counter > 0.

## Not proven (cannot claim from this run)
- Agent-visible router evidence (zero emits).
- That V2_LIVE helps (or hurts) action-path metrics — requires paired comparison vs OLD_GT + BASELINE.
- That V2_LIVE works on tasks where graph.db is present pre-edit (no such task exists in this canary).
