# spec: gt_mini_patch._record_hook_fire (Layer-4b FIRE counter)

**Invariant:** auditability — every hook FIRE (producer invoked) is counted to disk,
independent of delivery outcome. The runtime ledger records DELIVERED/SUPPRESSED only;
a fired-but-quiet (empty correct-or-quiet) producer must still be counted.

- **READS:** `GT_HOOK_FIRE_COUNTS` env (path; default `/tmp/gt_hook_fire_counts.json`).
- **WRITES:** the `{kind: count}` JSON + the in-memory `_HOOK_FIRE_COUNTS`.
- **RAISES:** never (auditability must not break delivery — bare except).

**Cases:**
1. an empty producer `("l3.contract", "")` → counted as 1 fire (the gap this closes).
2. a delivering producer `("l3b.evidence", "text")` → counted as 1 fire.
3. N invocations of the same kind → count == N, persisted.
4. reset (`_reset_oracle_state`) → counts cleared.

**Placement:** called at the TOP of the `_lane_a_deliver` loop, BEFORE the empty-skip,
so a fired-but-quiet or crashing producer is still counted.
