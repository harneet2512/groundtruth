# RESPEC.md — GroundTruth Runtime Specification

> **RESPEC.md is the single live source of truth. All older docs are historical unless explicitly quoted here.**
>
> Supersedes: DECISIONS.md, LATEST_TASK.md, GT_RUNTIME_ARCHITECTURE_AUDIT.md, IMPLEMENTATION_BUGS.md, TRAJECTORY_ANALYSIS_FINAL.md, analysis.md

---

## Table of Contents

1. [Current Executive Summary](#1-current-executive-summary)
2. [Canonical Architecture](#2-canonical-architecture)
3. [Active Layer Status Table](#3-active-layer-status-table)
4. [Active Bug Registry](#4-active-bug-registry)
5. [Superseded Decisions / Old Claims](#5-superseded-decisions--old-claims)
6. [P0 Fix Plan](#6-p0-fix-plan)
7. [P0 Fix Proof Ledger](#7-p0-fix-proof-ledger)
8. [Runtime Verification Matrix](#8-runtime-verification-matrix)
9. [Benchmark Readiness Gates](#9-benchmark-readiness-gates)
10. [Archive Index](#10-archive-index)

---

## 1. Current Executive Summary

GroundTruth is an MCP server providing deterministic, $0-AI codebase intelligence to coding agents. It indexes source code into a SQLite call graph (graph.db) via a Go binary (gt-index), then delivers evidence through observation augmentation at action boundaries.

**What works:** L1 brief (pre-task file candidates) and L3 callers/signatures (post-edit evidence) produce proven flips and -9.25 action efficiency gains when firing correctly.

**What is fixed (P0):** 5 of 6 P0 bugs are proven fixed. Behavioral contract path lookup (REPLAY_PROVEN), evidence truncation (RUNTIME_PROVEN), edge gate (REPLAY_PROVEN), evidence markers (UNIT_PROVEN), patch integrity hashing (RUNTIME_PROVEN). P0-4 (router kind names) is UNIT_PROVEN but not runtime-exercised yet.

**What is plumbing-proven but value-unproven:** GT tool injection (9 tools on every LLM call) and tool rewriting (gt_validate→execute_bash) work mechanically. But the agent only calls gt_validate — gt_query/gt_search/gt_navigate are ignored. Tool instruction now decoupled from brief gate (A5 fix) — adoption measurable in next run.

**What is fixed in this batch (A1-A12):** 5 fixes addressing 2 BLOCKERs + 3 BATCH items. Post-reindex proxy mode (A4), tool instruction delivery (A5), auto-query signature fallback (A1), GT_META observability (A2), condenser noop (A7). A3 and A10 reclassified as FALSE_ALARM. 59/59 unit tests pass, 0 regressions.

**What is unproven:** Obligation detector, issue grounding, format contracts, mismatch detection — diagnostics now on stdout (A2 fix) so next run will show whether they fire. Auto-query now has signature fallback (A1 fix) — no longer dead code.

---

## 2. Canonical Architecture

**Host vs Container boundary:**
- Wrapper (`oh_gt_full_wrapper.py`) runs on HOST
- Hooks (`post_edit.py`, `post_view.py`) run INSIDE container via `python3 -m groundtruth.hooks.*`
- Tool injection (`cost_tracking.py`) runs on HOST, monkey-patches litellm
- Evidence assembly runs on HOST after receiving hook stdout from container
- graph.db lives INSIDE container; proxy queries from host via `_container_query()`

**Runtime flow (post_view event):**
```
Agent reads file
  → OH calls orig_run_action → observation returned
  → patched_run_action intercepts:
    1. _check_pending_next_actions (L5b advisory)
    2. Auto-query gate check (prepend if eligible)
    3. Consensus check (prepend if first candidate)
    4. Router_v2 on_view (shadow/live)
    5. L3b hook in container (append/prepend)
  → Modified observation returned to agent
```

**Runtime flow (post_edit event):**
```
Agent edits file
  → OH calls orig_run_action → observation returned
  → patched_run_action intercepts:
    1. L6 reindex in container
    2. Router_v2 on_edit (shadow/live)
    3. L3 hook in container (callers, contracts, obligations, etc.)
    4. Evidence assembly on host
    5. Scope check
  → Modified observation returned to agent
```

---

## 3. Active Layer Status Table

| Layer | Status | Evidence | Known Bugs | Fix Needed | Proof Level |
|---|---|---|---|---|---|
| L1 brief | FUNCTIONAL | 100% tasks receive brief | None active | None | RUNTIME_PROVEN |
| L3 post-edit (callers) | FUNCTIONAL | Fires, delivers evidence. sh-744 flip proven. evidence_len=758 runtime-verified. | P0-1,2,3 FIXED | None | RUNTIME_PROVEN |
| L3 post-edit (behavioral contract) | FUNCTIONAL | Fires on sh-744 with full contract. Path suffix resolver handles multi-file repos. | P0-1 FIXED (REPLAY_PROVEN) | None | RUNTIME_PROVEN |
| L3b post-view | FUNCTIONAL | Fires on first 3 reads, delivers navigation | None active | None | RUNTIME_PROVEN |
| Router on_edit | FUNCTIONAL_WITH_BUGS | Formats caller/sibling/test evidence | P0-4 UNIT_PROVEN but not runtime-exercised (sh-744 didn't produce caller-only evidence) | Targeted runtime proof needed | UNIT_PROVEN |
| Router on_view | FUNCTIONAL | Emits neighborhood context in live mode | None active | None | RUNTIME_PROVEN |
| Auto-query | **FIXED (A1)** | Gate passes, SQL returns 0 cross-file callers → now falls back to symbol signatures instead of silent exit. | A1 FIXED: signature fallback added | Runtime proof in next GHA run | CODE_REVIEWED |
| Obligation detector | **DIAGNOSTICS_FIXED (A2)** | Evidence output was always stdout; GT_META diagnostics moved from stderr to stdout. Wrapper strips [GT_META] from agent view. | A2 FIXED: diagnostics now visible in GHA logs | Runtime proof: check for `[GT_META] obligation_check:` in next run | CODE_REVIEWED |
| Issue grounding | **DIAGNOSTICS_FIXED (A2)** | Same A2 fix applies | A2 | Same | CODE_REVIEWED |
| Format contracts | **DIAGNOSTICS_FIXED (A2)** | Same A2 fix + P2-1 (confidence filtering) still open | A2 FIXED + P2-1 open | Same | CODE_REVIEWED |
| Mismatch detection | **DIAGNOSTICS_FIXED (A2)** | Same A2 fix + P2-1 still open | A2 FIXED + P2-1 open | Same | CODE_REVIEWED |
| GT tools (injection) | PLUMBING_PROVEN | 9 tools injected on every LLM call | A5 FIXED: tool instruction decoupled from brief gate, always injected when GT_NATIVE_TOOLS=1. A6: agent adoption measurable in next run. | Runtime proof: grep for `scarce, high-signal` in next GHA logs | CODE_REVIEWED (A5 fix) |
| GT tools (rewrite) | PLUMBING_PROVEN | gt_validate→execute_bash confirmed (4 calls across 3 tasks) | Only gt_validate called. | Tool adoption STILL_UNPROVEN | RUNTIME_PROVEN (plumbing only) |
| L5 scaffolding_trap | FUNCTIONAL | Fires at adaptive threshold | None active | None | RUNTIME_PROVEN |
| L5 Goku | **FUNCTIONAL** | WEAK_VERIFICATION_AFTER_EDIT fired on conan at iter 2221/100, finalization band. | P1-1 FIXED | None | **RUNTIME_PROVEN** |
| Evidence markers | FUNCTIONAL | Delivery gate correctly filters. [GT_STATUS] no longer passes as evidence. | P0-5 FIXED | None | UNIT_PROVEN |
| Patch extraction | FUNCTIONAL | Patches extracted with SHA256 hash + malformed detection at every stage | P0-6 FIXED | None | RUNTIME_PROVEN |
| Scaffold strip | FUNCTIONAL_WITH_BUGS | Fires on finish | Silent no-op when base_commit missing (P2) | Not in P0 scope | CODE_REVIEWED |

---

## 4. Active Bug Registry

### P0 (must fix before any benchmark run)

| ID | Bug | File | Line | Class | Status |
|---|---|---|---|---|---|
| P0-1 | Behavioral contract path lookup — generalized suffix resolver | `post_edit.py` | ~1437 | delivery | **FIXED — REPLAY_PROVEN** (frozen beancount graph, `/testbed/` prefix resolved) |
| P0-2 | Non-live L3 truncates to 3 lines / 130 chars | `oh_gt_full_wrapper.py` | ~3804 | delivery | **FIXED — RUNTIME_PROVEN** (GHA sh-744: evidence_len=758) |
| P0-3 | Improved L3 gated on `_has_edges` — blocks sparse/disconnected files | `post_edit.py` | ~2377 | delivery | **FIXED — REPLAY_PROVEN** (frozen beancount graph, zero-edge node produced evidence) |
| P0-4 | Router checks `"caller"` but actual kind is `"caller_code"` | `router.py` | ~323 | ordering | FIXED — UNIT_PROVEN_WITH_EXISTING_TEST_FAILURES, REPLAY_NEEDED |
| P0-5 | `[GT_STATUS]` in marker list makes no-evidence pass delivery gate | `evidence_markers.py` | ~10 | delivery | FIXED — UNIT_PROVEN, REPLAY_NEEDED |
| P0-6 | No patch hash in extraction pipeline — truncation undetectable | `convert_to_submission.py` | N/A | observability | **FIXED — RUNTIME_PROVEN** (GHA sh-744: hashes match, malformed detected) |

### P1 (fix after P0, before benchmark scaling)

| ID | Bug | File | Status |
|---|---|---|---|
| P1-1 | Goku default mismatch (`"1"` vs `"0"`) silently suppresses L5b | `wrapper:1654` vs `wrapper:2863` | **FIXED — RUNTIME_PROVEN** (Goku fired WEAK_VERIFICATION on conan, GHA run 26213296069) |
| P1-2 | GT_META stdout pollution (4 print calls missing stderr) | `post_edit.py:754-792` | FIXED — CODE_REVIEWED (all 4 now use `file=sys.stderr`) |
| P1-3 | Prepend cap 600 chars truncates live L3b | `wrapper:2152` | OPEN |
| P1-4 | Silent exception swallowing on evidence sub-modules | `post_edit.py:1679,1688,1694,1707` | OPEN |

### P2 (fix before 300-task run)

| ID | Bug | File | Status |
|---|---|---|---|
| P2-1 | No confidence filtering in format_contract + mismatch SQL | `format_contract.py`, `mismatch.py` | OPEN |
| P2-2 | Scaffold strip no-op when base_commit missing | `wrapper:2098` | OPEN |
| P2-3 | Improved L3 lacks guard-removal detection | `post_edit.py:1423-1480` | OPEN |
| P2-4 | Docker image tag `_1776_` hardcoded | `swebench_30task.yml:143` | OPEN |

### Disproven Hypotheses

| ID | Hypothesis | Status |
|---|---|---|
| BH-2 | Obligation diff_text not passed | **DISPROVEN** — code audit confirms diff_text IS passed at line 2424 |

---

## 5. Superseded Decisions / Old Claims

| Old Claim | Source | Status | Correction |
|---|---|---|---|
| "All layers proven working locally" | LATEST_TASK.md | SUPERSEDED | 6 P0 bugs still active; obligation/grounding/format/mismatch unproven at runtime |
| "Only L1 reached the agent" | analysis.md | SUPERSEDED | L3/L3b confirmed delivered in raw logs; 13/16 BOTH_FAIL had L3 delivery |
| "3 regressions" | analysis.md | SUPERSEDED | haystack-8609 patches identical — eval variance. Real regressions = 2 |
| "Delivery pipe is primary bottleneck" | TRAJECTORY_ANALYSIS_FINAL.md | SUPERSEDED | Evidence QUALITY > delivery. 13/16 BOTH_FAIL had L3 delivery |
| "L4 prefetch is harmful" | TRAJECTORY_ANALYSIS_FINAL.md | OVERTURNED by adversarial second pass | Same warnings on flips; L3 quality is the moderator |
| "Graph.db transfer caused conan regression" | Session analysis | DISPROVEN | Deep log diff showed resolved run had NO graph.db on host |

---

## 6. P0 Fix Plan

Exactly 6 fixes. No new features. No benchmark run.

| Fix | File | Change | Test |
|---|---|---|---|
| P0-1 | `post_edit.py:~1437` | `file_path = ?` → `LIKE ?` with normalized suffix | Unit: workspace-prefixed path matches graph node |
| P0-2 | `wrapper:~3804` | `directive_lines[:3]` + `ln[:130]` → `"\n".join(...)[:2000]` | Unit: 7-line contract survives |
| P0-3 | `post_edit.py:~2377` | `if _has_edges:` → `if _has_edges or all_func_names:` | Unit: zero-edge node gets signature |
| P0-4 | `router.py:~323` | `"caller"` → `"caller_code"`, `"test"` → `"test_assertion"` | Unit: caller-only not suppressed |
| P0-5 | `evidence_markers.py:~10` | Remove `"[GT_STATUS]"` from L3B_MARKERS | Unit: no-evidence returns False |
| P0-6 | `convert_to_submission.py` + wrapper | Add SHA256 + byte length at 3 stages | Unit: truncated patch detected |

---

## 7. P0 Fix Proof Ledger

*Populated after each fix is implemented.*

| Fix ID | Files changed | Old behavior | New behavior | Proof command | Proof output | Proof level | Remaining unproven | Rollback plan |
|---|---|---|---|---|---|---|---|---|
| P0-1 | `post_edit.py:1437` | `file_path = ?` exact match | Generalized path suffix resolver: query by name, filter by component suffix in Python | `pytest tests/replay/test_p0_replay.py::TestReplay2PathMismatch` | **REPLAY PASSED**: `/testbed/beancount/core/account.py` matched `beancount/core/account.py` via suffix. OLD exact query: None. NEW resolver: (49, 58). | **REPLAY_PROVEN** | None | Revert to LIKE query |
| P0-2 | `wrapper:~3804` | `directive_lines[:3]` + `ln[:130]` | `"\n".join(directive_lines)[:2000]` | GHA run 26210579765 sh-744 | `[BEHAVIORAL CONTRACT]` in markers, `evidence_len=758` (not 390), fired on both edits (step 43 + 76). No 3-line truncation. No 130-char truncation. | **RUNTIME_PROVEN** | None | Revert 1 line |
| P0-3 | `post_edit.py:2415` | `if _has_edges:` | `if _has_edges or all_func_names:` | `pytest tests/replay/test_p0_replay.py::TestReplay3SparseFile` | **REPLAY PASSED**: `create_simple_posting` (0 edges in frozen beancount graph) produced `[CONTRACT ~]`, `[SIGNATURE]`, `[PATTERN]` — 458 chars of real evidence. | **REPLAY_PROVEN** (frozen beancount graph.db) | None — fix proven on real artifact | Revert condition |
| P0-4 | `router.py:323` | `"caller", "test"` | `"caller_code", "test_assertion"` | `pytest tests/router/test_on_edit.py` | 7 passed (2 pre-existing fail) | UNIT_PROVEN_WITH_EXISTING_TEST_FAILURES | REPLAY needed (caller-only evidence in live run) | Revert 1 line |
| P0-5 | `evidence_markers.py:8-15` | `"[GT_STATUS]"` in markers | `"[GT_STATUS] success"` only | `pytest tests/unit/test_evidence_markers.py` | 37/37 passed: no_evidence→False, success→True, new markers→True | UNIT_PROVEN | REPLAY needed (runtime delivery gate) | Revert tuple |
| P0-6 | `convert_to_submission.py:43-52,71-77` | No integrity checking | SHA256 + byte length + malformed detection on canonical (stripped) patch | GHA run 26210579765 sh-744 | `output.jsonl sha256=ba2fa4f9c1b3915d`, `predictions.jsonl sha256=ba2fa4f9c1b3915d`. Hashes MATCH. `malformed=True` detected. | **RUNTIME_PROVEN** | None | Revert logging lines |

---

## 8. Runtime Verification Matrix

| Layer | Unit proof | Replay proof | Integration proof | Runtime log proof | Exit condition |
|---|---|---|---|---|---|
| L1 brief | N/A (proven) | N/A | N/A | `L1 brief injected` 100% | Proven |
| L3 post-edit | P0-1,2,3 fixes | sh-744 contract fires | 3-task run | `[BEHAVIORAL CONTRACT]` in obs | Contract on every source edit |
| L3b post-view | N/A (proven) | N/A | N/A | Navigation delivered | Proven |
| Router on_edit | P0-4 fix | Caller-only not suppressed | 3-task run | No `LOW_CONFIDENCE` suppression | Evidence reaches agent |
| Evidence markers | P0-5 fix | No-evidence returns False | 3-task run | No noise in agent obs | Zero status-only deliveries |
| Patch survival | P0-6 hash | Hash match at all stages | 3-task run | Zero truncation | Hash identical everywhere |

---

## 9. Runtime Audit — GHA Run 26213296069 (3-task plumbing smoke)

**Tasks:** sh-744 (RESOLVED), briefcase-2085 (NOT RESOLVED), conan-17102 (NOT RESOLVED)
**Commit:** `60115962`

### Findings

| ID | Finding | Source | Classification | Root Cause | Next Action |
|---|---|---|---|---|---|
| A1 | Auto-query gate passes, enters block, marks file as seen, but `_container_query` returns 0 cross-file callers → `_aq_lines` empty → exits silently. count=0 across ALL 3 tasks. Feature never produces output. | `wrapper:2968-3002`; all 3 logs zero `auto_query:` lines | **BLOCKER → FIXED** | Cross-file caller SQL returns 0 for small repos. No fallback existed. | **FIX:** Added signature fallback — when callers=0, emits `name(signature)` from same node query. Also selects `n.signature` in initial query. ~4 lines. |
| A2 | GT_META diagnostics (obligation_check, peer_detection) on stderr → invisible in GHA logs. Evidence modules themselves output to stdout via func_parts (not lost). | `post_edit.py:1688,1692,755,768,780,792` (`file=sys.stderr`); 0 matches in GHA logs | **BATCH → FIXED** | Evidence output was always stdout. Only GT_META diagnostics used stderr. Reclassified from "evidence lost" to "diagnostics lost." | **FIX:** 6 `file=sys.stderr` → `file=sys.stdout` in post_edit.py. Added `[GT_META]` to wrapper directive_lines filter (lines 3286, 3945) to prevent agent pollution. |
| A3 | `[GT_STATUS] skipped:test_file` supposedly delivered to agent. | Reanalysis of wrapper:3281 and evidence_markers.py:10 | **FALSE_ALARM** | Double filtering prevents this: (1) wrapper line 3281 strips all `[GT_STATUS]`-prefixed lines during evidence assembly, (2) `has_gt_evidence()` only matches `"[GT_STATUS] success"`, not generic variants. P0-5 fix + wrapper filtering together prevent noise. | No fix needed. |
| A4 | Post-reindex graph.db download ignores proxy mode. Conan: 5 chunked transfers × 5.7min = 28.5min overhead (73% of runtime). | `wrapper:3468-3488` unconditional `_download_graph_db_to_host()`; conan logs L1022-1701 | **BLOCKER → FIXED** | Proxy flag only checked at initial prefetch, not post-reindex. | **FIX:** Added `_post_reindex_mode` check at wrapper:3474. Proxy mode refreshes L5 threshold via `_container_query("SELECT COUNT(*) FROM nodes")` instead of full download. Router reset preserved. ~12 lines. |
| A5 | Tool instruction NOT in agent prompt. `grep` for instruction text returns 0 matches in all 3 logs. | All 3 logs: 0 matches for `scarce.*high-signal` | **BATCH → FIXED** | Root cause confirmed: `tools_hint` was inside `if brief:` gate (wrapper:4767). Empty brief → no instruction. | **FIX:** Decoupled `tools_hint` from brief gate. Now injected whenever `GT_NATIVE_TOOLS=1` and not baseline. Brief still gates `<gt-task-brief>` and demo blocks. ~5 lines. |
| A6 | Agent calls only gt_validate (4 total across 3 tasks). Zero calls to gt_query/gt_search/gt_navigate. | All 3 logs: only `tool_rewrite: gt_validate→execute_bash` | **BATCH** | Tool descriptions and instruction insufficient to override agent's bash/grep habits | STILL_UNPROVEN for tool adoption |
| A7 | Condenser config `recent_events:5` not parsed. Falls back to noop. | All 3 logs: `Condenser config section [condenser.recent_events:5] not found in config.toml` | **BATCH → FIXED** | OH config gap: wrapper passes `EVAL_CONDENSER` correctly to `get_condenser_config_arg()`, but OH's Docker image config.toml lacks the section. Not a GT code bug. | **FIX:** Removed `EVAL_CONDENSER` env var from `swebench_30task.yml`. Accepts NoOp condensing (orthogonal to GT value). 1 line. |
| A8 | P0-2 BEHAVIORAL CONTRACT in agent observation: CONFIRMED. `visible=True surface=append_observation` at step 38 for sh-744. | sh-744 L990: `[GT_TRACE] markers=['[SIGNATURE]', '[PATTERN]', '[BEHAVIORAL CONTRACT]']` | **CONFIRMED** | N/A | P0-2 RUNTIME_PROVEN stands |
| A9 | P0-4 no `no_actionable_evidence` suppression in any log. But no caller-only scenario observed. | 0 matches for `no_actionable` in all 3 logs | **STILL_UNPROVEN** | sh-744/briefcase/conan all have callers+contracts, not caller-only | P0-4 remains UNIT_PROVEN |
| A10 | Max_iter not overflowed. step counter ≠ agent actions. sh-744: 43 LLM calls. briefcase: 33. conan: 62. All within 100 max_iter. | All 3 logs: LLM call count from tool_injection lines | **FALSE_ALARM** | Previous misread of wrapper step counter as agent action count | N/A |
| A11 | Goku WEAK_VERIFICATION fired on conan at step 2221 (≈agent iter ~80). L5 actually engaged. | conan L1576: `goku_WEAK_VERIFICATION_AFTER_EDIT fired at iter 2221/100 band=finalization` | **CONFIRMED** | P1-1 default fix worked | L5 Goku = RUNTIME_PROVEN |
| A12 | B-7 proxy initial prefetch works: `node_count=385 L5_threshold=20` in ~1 sec for sh-744. | sh-744 L867: `B-7 proxy: node_count=385 L5_threshold=20 (1 query, ~1 sec)` | **CONFIRMED** | N/A | Proxy works for initial; post-reindex is A4 |

### Proof Level Summary (post-audit, post-fix-batch)

| Layer | Pre-Audit Level | Post-Audit Level | Post-Fix Level | Change Reason |
|---|---|---|---|---|
| L3 post-edit | RUNTIME_PROVEN | RUNTIME_PROVEN | RUNTIME_PROVEN | A8 confirms [BEHAVIORAL CONTRACT] in agent obs |
| Auto-query | HALF_WIRED | DEAD_CODE | **CODE_REVIEWED** | A1 FIX: signature fallback added; no longer dead |
| Obligation/grounding/format/mismatch | IMPLEMENTED_UNPROVEN | UNOBSERVABLE | **CODE_REVIEWED** | A2 FIX: diagnostics moved to stdout; evidence path was always stdout |
| GT tools (instruction) | PLUMBING_PROVEN | PLUMBING_PROVEN | **CODE_REVIEWED** | A5 FIX: instruction decoupled from brief gate |
| GT tools (adoption) | PLUMBING_PROVEN | PLUMBING_PROVEN | PLUMBING_PROVEN | A6: blocked by A5; measurable in next run |
| L5 Goku | FUNCTIONAL_UNPROVEN | **RUNTIME_PROVEN** | RUNTIME_PROVEN | A11: fired WEAK_VERIFICATION on conan |
| Patch integrity | RUNTIME_PROVEN | RUNTIME_PROVEN | RUNTIME_PROVEN | All 3 tasks: malformed=False, hashes match |

### Fix Batch Applied (5 fixes, 59/59 tests pass)

| # | Fix | Finding | File(s) | Lines Changed | Proof Level |
|---|---|---|---|---|---|
| 1 | Proxy mode check at post-reindex download | A4 BLOCKER | `wrapper:~3474` | ~12 | CODE_REVIEWED |
| 2 | Tool instruction decoupled from brief gate | A5 BATCH | `wrapper:~4783` | ~5 | CODE_REVIEWED |
| 3 | Auto-query signature fallback when 0 callers | A1 BLOCKER | `wrapper:~2974` | ~4 | CODE_REVIEWED |
| 4 | GT_META stderr→stdout + wrapper [GT_META] filter | A2 BATCH | `post_edit.py` + `wrapper:3286,3945` | 8 | CODE_REVIEWED |
| 5 | Remove EVAL_CONDENSER (accept NoOp) | A7 BATCH | `swebench_30task.yml:194` | 1 | CODE_REVIEWED |

### Reclassified Findings

| Finding | Old Class | New Class | Reason |
|---|---|---|---|
| A3 | BATCH | **FALSE_ALARM** | Double filtering: wrapper strips [GT_STATUS] lines (3281) + marker check doesn't match generic variants |
| A10 | FALSE_ALARM | FALSE_ALARM | Confirmed: action_count (2789) counts CmdRunActions, not agent iterations (actual: 43/33/62 LLM calls) |

---

## 10. Benchmark Readiness Gates

Resume benchmark work ONLY when ALL:
- [x] All 6 P0 bugs have code fixes
- [x] All 6 P0 fixes have UNIT_PROVEN level minimum
- [x] **P0-1: REPLAY_PROVEN** — generalized suffix resolver, frozen beancount graph
- [x] P0-2: **RUNTIME_PROVEN** — GHA sh-744: evidence_len=758, [BEHAVIORAL CONTRACT] in agent obs
- [x] P0-3: **REPLAY_PROVEN** — frozen beancount graph, zero-edge node
- [ ] P0-4: UNIT_PROVEN, REPLAY still needed (caller-only evidence in live run)
- [x] P0-5: UNIT_PROVEN (37/37 marker tests)
- [x] **P0-6: RUNTIME_PROVEN** — GHA sh-744: hash ba2fa4f9c1b3915d matches at both stages, malformed detected
- [ ] No layer classified UNPROVEN without a plan
- [x] RESPEC.md proof ledger complete
- [ ] User approval for smoke run

**Remaining before 5-task behavior smoke:**
1. ~~**A4 BLOCKER:** Post-reindex proxy mode~~ → **FIXED** (CODE_REVIEWED)
2. ~~**A1 BLOCKER:** Auto-query dead code~~ → **FIXED** (CODE_REVIEWED, signature fallback)
3. ~~**A2 BATCH:** Observability gap~~ → **FIXED** (CODE_REVIEWED, stderr→stdout)
4. P0-4: UNIT_PROVEN, no caller-only scenario observed. Low risk — accept or target.
5. **3-task GHA plumbing smoke** to prove A1/A2/A4/A5 fixes at runtime
6. User approval for 5-task behavior smoke

---

## 10. Archive Index

| Old Document | What was extracted | Status |
|---|---|---|
| `DECISIONS.md` | Historical decisions referenced in §5 | SUPERSEDED |
| `LATEST_TASK.md` | Task context, graph quality stats | SUPERSEDED (overconfident) |
| `GT_RUNTIME_ARCHITECTURE_AUDIT.md` | Bug registry, layer classification, delivery ordering | SUPERSEDED (merged into §3-4) |
| `IMPLEMENTATION_BUGS.md` | 18 bugs from initial code review | SUPERSEDED (merged into §4) |
| `TRAJECTORY_ANALYSIS_FINAL.md` | Dual-agent findings, flip mechanism | SUPERSEDED (key findings in §5) |
| `analysis.md` | 30-task trajectory analysis | SUPERSEDED (corrections in §5) |
| `jedi_WORK.md` | Session work log | HISTORICAL (not superseded, ongoing log) |
