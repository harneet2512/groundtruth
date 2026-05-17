# ORDER_1.0 Stage 3 — Local Layer Utilization Report

**Date:** 2026-05-17
**Branch:** `jedi__branch`
**Session:** ORDER_1.0 Stage 3

---

## Known Blocker (per ORDER_1.0): L0 graph.db unavailable to L3 router

**Status: B-7 PARTIALLY FIXED. B-8 DISCOVERED AND FIXED.**

### B-7 Status: Code correct, download mechanism broken

The B-7 code fix (commit `e0527e7e`) correctly:
1. Pre-fetches graph.db to host at task init (wrapper lines 3439-3468)
2. Resets router cache after L6 reindex (wrapper line 2767)
3. Fail-fast prints when live mode + pre-fetch fails

GHA run `25996587814` (B-7 fix canary) verified:
- `[GT_META] B-7 pre-fetch: graph.db downloaded to host` appears for all 3 tasks
- Router boot: `env='live' resolved=live` for all 3 tasks
- Suppression reason changed from `no_graph_db` (old) to `no_evidence` (new)
- Legacy bypassed: 17/14/24 `router_v2_legacy_skip` entries

### B-8: Downloaded graph.db is malformed

**Root cause:** `_download_graph_db_to_host()` at wrapper line 1905 uses `base64 -w0` to encode the container's graph.db, then extracts the base64 via regex `r"[A-Za-z0-9+/=]{128,}"`. OH observations can inject noise characters (shell prompts, timing markers, ANSI codes) that split the base64 stream into multiple tokens. The function then takes `max(tokens, key=len)` — i.e., only the LONGEST fragment. This is a subset of the full binary → malformed SQLite file.

**Evidence (all 3 tasks):**
- beancount: `[GT_META] router_v2 schema check error (DatabaseError): database disk image is malformed` (line 314)
- beets: Same error at lines 310, 400, 434 (3 occurrences — initial + 2 post-L6 resets)
- loguru: Same error at lines 312, 404, 420, 428, 452, 482, 492, 512, 566 (9 occurrences — many L6 resets)

**Consequence:** Router has a DB path but cannot query it. All router calls return `emit=False sup=no_evidence text_len=0`. V2 live mode delivers zero agent-visible evidence — identical to baseline.

### B-8 Fix

Changed `_download_graph_db_to_host()` to:
1. **Concatenate ALL base64 tokens** instead of taking only the longest (`"".join(tokens)`)
2. **Validate the downloaded DB** with `sqlite3.connect().execute("SELECT count(*) FROM nodes")` before returning
3. **Discard and return empty** if validation fails, with `[GT_META] B-8:` log marker

**File:** `scripts/swebench/oh_gt_full_wrapper.py`, lines 1905-1937.

**Test verification:** 96 tests pass (state/router/providers) + 6 router V2 telemetry tests pass.

---

## Stage 3 Required Local Behaviors (per ORDER_1.0)

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | `graph.db` is host-readable before first post_view | FAIL (B-8) | DB downloaded but malformed. Fix applied, needs re-canary. |
| 2 | `GT_ROUTER_V2=live` fails fast if graph.db is missing | PASS | `[GT_FATAL] GT_ROUTER_V2=live but graph.db pre-fetch failed` at wrapper line 3458 |
| 3 | Router initializes only with `graph_db_present=true` | FAIL (B-8) | Router initializes with a path to a malformed DB. After fix, it will return empty → router gets None → correct suppression. |
| 4 | L6 graph refresh resets router cache | PASS | `config._router_v2 = None` at wrapper line 2767. Confirmed by multiple schema check errors in loguru (router re-instantiated after each L6). |
| 5 | Live mode bypasses legacy L3/L3b for same event | PASS | 55 total `router_v2_legacy_skip` entries across 3 tasks. 0 legacy evidence in output.jsonl. |
| 6 | If router emits, observation contains `[GT-router-v2 ...]` | NOT TESTED | B-8 prevented any emits. After fix, first successful emit should produce this tag. |
| 7 | If router suppresses, reason is logged | PASS | All suppression reasons logged: `no_evidence` (after B-8 DB query returns empty). |
| 8 | No silent router-blind live mode | PASS | `[GT_FATAL]` prints if pre-fetch fails entirely. `no_evidence` logged for each suppression. |

**Overall Stage 3 status: BLOCKED on B-8 re-canary.** Fix applied. 5/8 requirements pass. 2/8 fail due to B-8 (will pass after fix). 1/8 not testable until router emits.

---

## L0–L6 Layer Utilization (from B-7 canary artifacts)

### L0 Graph Substrate
- graph.db built in container: YES (gt-index runs successfully)
- graph.db pre-fetched to host: YES (B-7 code works)
- graph.db host-readable: **NO** (B-8 — malformed after download)
- Schema version in Go indexer: YES (`v15.1-trust-tier` stamped)

### L1 Pre-task Seed
- Brief emitted: YES (all 3 tasks have `<gt-task-brief>` in output.jsonl)
- Candidate files produced: YES (BM25+reach+hub_pen working)
- Injected into agent instruction: YES

### L2 AgentState
- AgentState initialized: YES (`_ensure_agent_state` called from router)
- Views tracked: YES (via router on_view calls)
- Suggestions registered: NO (no router emits → no suggestions)

### L3 Router (V2)
- Mode: live (all 3 tasks)
- Called: beancount=17, beets=14, loguru=24
- Emits: **0** (all `no_evidence` due to B-8)
- Legacy bypassed: YES (55 total skips)
- Agent-visible evidence: **0**

### L4 Providers
- Called via router: YES (router calls providers)
- Evidence returned: **0** (B-8 prevents graph queries)

### L5 Validator
- Called: **0** (unwired — no call path from wrapper)
- Warnings emitted: **0**

### L5 Governor (Goku)
- Called: YES (via Goku integration in wrapper)
- Fires: NOT CHECKED in B-7 canary (focused on router)

### L6 Metrics & Reindex
- Reindex fired: YES (beancount=2, beets=4, loguru=many — confirmed by multiple `graph_refreshed_after_l6` logs)
- Router cache reset: YES (confirmed by repeated schema check errors = router re-instantiated)
- Graph.db re-downloaded: YES (but still malformed each time)

---

## Next Step (per ORDER_1.0)

**Do not run GHA yet.** B-8 fix needs re-canary first. Before dispatching:
1. The B-8 fix must be committed and pushed
2. A 3-task canary (`canary_3arm.yml`, arm=v2_live) must show:
   - 0 `database disk image is malformed` in full_run.log
   - At least 1 `emit=True` per task in router on_view/on_edit logs
   - `[GT-router-v2 on_view]` or `[GT-router-v2 on_edit]` in output.jsonl
3. Only then proceed to Stage 5 (3-task local canary comparison)
