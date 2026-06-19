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

**What blocks flips (diagnosed GHA 26236589181):** Agent makes plausible fixes that pass existing tests but misses the specific failing test. briefcase-2085: try/except fix passes 41 tests, wrong approach. conan-17102: 6 iterations on install_graph.py, never saw expected JSON schema. Root cause: GT delivers structural context (signatures, patterns, contracts, callers) but not WHAT the failing test expects. Zero [TEST] markers on any post-edit delivery for either task. Injecting FAIL_TO_PASS labels was attempted and REVERTED (benchmark metadata, violates CLAUDE.md). Legitimate path: mine test names from issue text + graph to suggest targeted test commands. See `FAILURE_DIAGNOSIS_26236589181.md`.

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
| Auto-query | **FIXED (A1)** | Gate passes on 5 files across 3 tasks. Signature fallback added but signatures NULL in all 3 test repos → fallback not exercised. | A1 FIXED: logic correct, needs repo with non-NULL signatures to prove | Repo with indexed signatures | CODE_REVIEWED |
| Obligation detector | **DIAGNOSTICS_FIXED (A2)** | Diagnostics moved to stdout, wrapper strips from agent view. GHA run 26236589181: container GT_META captured but NOT re-emitted to host stdout → invisible in GHA logs. | A2 PARTIAL: need wrapper to re-emit container GT_META (~1 line) | Wrapper re-emit follow-up | CODE_REVIEWED |
| Issue grounding | **DIAGNOSTICS_FIXED (A2)** | Same A2 partial status | A2 PARTIAL | Same follow-up | CODE_REVIEWED |
| Format contracts | **DIAGNOSTICS_FIXED (A2)** | Same A2 partial + P2-1 (confidence filtering) still open | A2 PARTIAL + P2-1 open | Same follow-up | CODE_REVIEWED |
| Mismatch detection | **DIAGNOSTICS_FIXED (A2)** | Same A2 partial + P2-1 still open | A2 PARTIAL + P2-1 open | Same follow-up | CODE_REVIEWED |
| GT tools (injection) | PLUMBING_PROVEN | 9 tools injected on every LLM call. GHA run 26236589181: instruction injected via brief path on all 3 tasks. No-brief decoupling path untested (all tasks had briefs). | A5 FIXED: brief path RUNTIME_PROVEN. Decoupled path needs no-brief task. A6: gt_validate called (1x each on sh-744 + conan), gt_query/gt_search/gt_navigate still unused. | No-brief task for full A5 proof | **RUNTIME_PROVEN (brief path)** |
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

### Proof Level Summary (post-audit, post-fix-batch, post-GHA-26236589181)

| Layer | Pre-Audit | Post-Audit | Post-Fix | Post-GHA 26236589181 | Evidence |
|---|---|---|---|---|---|
| L3 post-edit | RUNTIME_PROVEN | RUNTIME_PROVEN | RUNTIME_PROVEN | RUNTIME_PROVEN | A8: [BEHAVIORAL CONTRACT] in agent obs. Run 26236589181: evidence_len=747 (sh-744), 350-2058 (conan), 6 L3 deliveries on conan. |
| Auto-query | HALF_WIRED | DEAD_CODE | CODE_REVIEWED | **CODE_REVIEWED** | A1: gate passed on 5 files/3 tasks. Signature fallback not exercised — all 3 repos have NULL signatures. Logic correct, needs non-NULL repo. |
| Obligation/grounding/format/mismatch | IMPLEMENTED_UNPROVEN | UNOBSERVABLE | CODE_REVIEWED | **CODE_REVIEWED (PARTIAL)** | A2: diagnostics on stdout, wrapper strips from agent. But wrapper does NOT re-emit container GT_META to host stdout → invisible in GHA logs. 1-line follow-up needed. |
| GT tools (instruction) | PLUMBING_PROVEN | PLUMBING_PROVEN | CODE_REVIEWED | **RUNTIME_PROVEN (brief path)** | A5: instruction injected on all 3 tasks via brief path. Decoupled no-brief path untested (all tasks had briefs). |
| GT tools (adoption) | PLUMBING_PROVEN | PLUMBING_PROVEN | PLUMBING_PROVEN | PLUMBING_PROVEN | A6: gt_validate called 1x each on sh-744 + conan. gt_query/gt_search/gt_navigate still 0 calls. |
| L5 Goku | FUNCTIONAL_UNPROVEN | RUNTIME_PROVEN | RUNTIME_PROVEN | RUNTIME_PROVEN | A11: enabled on conan (no fire this run — didn't enter finalization band). Previous run: fired WEAK_VERIFICATION. |
| Patch integrity | RUNTIME_PROVEN | RUNTIME_PROVEN | RUNTIME_PROVEN | RUNTIME_PROVEN | All 3 tasks: hashes match, malformed=False. |

### Fix Batch Applied (5 fixes, 59/59 tests pass, GHA run 26236589181)

| # | Fix | Finding | File(s) | Lines | Pre-GHA Level | **Post-GHA Level** |
|---|---|---|---|---|---|---|
| 1 | Proxy mode check at post-reindex download | A4 BLOCKER | `wrapper:~3474` | ~12 | CODE_REVIEWED | **RUNTIME_PROVEN** — 9 proxy refreshes, 0 downloads. Conan: 39→26.5 min. |
| 2 | Tool instruction decoupled from brief gate | A5 BATCH | `wrapper:~4783` | ~5 | CODE_REVIEWED | **RUNTIME_PROVEN (brief path)** — injected on all 3 tasks. No-brief untested. |
| 3 | Auto-query signature fallback when 0 callers | A1 BLOCKER | `wrapper:~2974` | ~4 | CODE_REVIEWED | **CODE_REVIEWED** — gate passed, sigs NULL in test repos. Not exercised. |
| 4 | GT_META stderr→stdout + wrapper filter | A2 BATCH | `post_edit.py` + `wrapper:3286,3945` | 8 | CODE_REVIEWED | **CODE_REVIEWED (PARTIAL)** — stdout correct, filter correct, wrapper re-emit missing. |
| 5 | Remove EVAL_CONDENSER (accept NoOp) | A7 BATCH | `swebench_30task.yml:194` | 1 | CODE_REVIEWED | **RUNTIME_PROVEN** — `condenser_config: noop`, zero errors, all 3 tasks. |

### Reclassified Findings

| Finding | Old Class | New Class | Reason |
|---|---|---|---|
| A3 | BATCH | **FALSE_ALARM** | Double filtering: wrapper strips [GT_STATUS] lines (3281) + marker check doesn't match generic variants |
| A10 | FALSE_ALARM | FALSE_ALARM | Confirmed: action_count (2789) counts CmdRunActions, not agent iterations (actual: 43/33/62 LLM calls) |

### Failure Diagnosis — GHA 26236589181 (briefcase-2085, conan-17102)

Full analysis: `FAILURE_DIAGNOSIS_26236589181.md`

**briefcase-2085 (NOT RESOLVED):**
- Agent read base.py (correct file), read test file (GT navigated), edited line 1017 adding try/except for GitCommandError
- All 41 existing tests pass. But FAIL_TO_PASS test not satisfied.
- 30 LLM calls, 1 edit, 6 min. Agent confident but wrong.
- **Root cause:** Agent ran existing tests (PASS_TO_PASS) not the specific failing test. Fix is plausible but wrong approach — agent didn't know what the FAIL_TO_PASS test asserts.

**conan-17102 (NOT RESOLVED):**
- Agent read graph.py + install_graph.py (both correct). GT delivered callers (`Called by: conans/client/installer.py`), [CONTRACT], [PEER], [PROPAGATE] on every edit.
- 6 edits to install_graph.py, 1 edit to graph.py. F2P=0/1.
- 63 LLM calls, 7 edits, 26.5 min. Agent iterated but never converged.
- **Root cause:** Zero [TEST] markers in any of 6 post-edit deliveries. Agent never saw expected JSON output schema. Iterated blindly on serialization approach.

**Common failure pattern:**
GT delivers structural context (signatures, patterns, contracts, callers) but NOT what the failing test expects. Agent makes plausible fixes, passes existing tests, misses FAIL_TO_PASS criterion.

**sh-744 (RESOLVED) comparison:**
sh-744 flips because the fix is LOCAL — behavioral contract in the function body (guards, returns) tells the agent everything. briefcase/conan fail because the fix requires EXTERNAL info: what specific test assertion must be satisfied.

**Evidence gap:** [TEST] marker = 0 on both failed tasks. The most impactful evidence (test assertion content) never reaches the agent.

### Flip-blocking fixes (priority order)

| # | Fix | Impact | Lines | File |
|---|---|---|---|---|
| F1 | ~~Surface FAIL_TO_PASS test names~~ | **REVERTED** — FAIL_TO_PASS is benchmark metadata, violates CLAUDE.md ("No gold labels, task IDs, or benchmark metadata in product logic"). Legitimate alternative: mine test keywords from issue text + graph to suggest `pytest -k` commands. | — | — | REVERTED |
| F2 | A2 wrapper re-emit (container GT_META to host stdout) | Unblocks observability for obligation/mismatch/format | ~6 | `wrapper:3118,3812` | **IMPLEMENTED** |
| F3 | Include test assertion content in L3 [TEST] evidence | Agent sees expected behavior on every edit | ~20 | post_edit.py | NOT YET |

---

## 10. Benchmark Readiness Gates

### Plumbing gates (all passed)
- [x] All 6 P0 bugs have code fixes
- [x] All 6 P0 fixes have UNIT_PROVEN level minimum
- [x] **P0-1: REPLAY_PROVEN** — generalized suffix resolver, frozen beancount graph
- [x] P0-2: **RUNTIME_PROVEN** — GHA sh-744: evidence_len=758, [BEHAVIORAL CONTRACT] in agent obs
- [x] P0-3: **REPLAY_PROVEN** — frozen beancount graph, zero-edge node
- [x] P0-5: UNIT_PROVEN (37/37 marker tests)
- [x] **P0-6: RUNTIME_PROVEN** — GHA sh-744: hash ba2fa4f9c1b3915d matches at both stages
- [x] A4: **RUNTIME_PROVEN** — 9 proxy refreshes, 0 downloads, conan 39→26.5 min
- [x] A5: **RUNTIME_PROVEN (brief path)** — instruction on all 3 tasks
- [x] A7: **RUNTIME_PROVEN** — condenser noop, zero errors
- [x] 3-task plumbing smoke: **PASSED** (sh-744 RESOLVED, no regressions)

### Flip gates
- [ ] **F1: Test discovery from issue text** — F1 original (FAIL_TO_PASS injection) REVERTED as benchmark metadata. Legitimate path: mine test file/function names from issue text + graph callers to suggest targeted `pytest -k` commands.
- [x] **F2: A2 wrapper re-emit** — IMPLEMENTED. Container GT_META lines re-emitted to host stdout at both L3b (view) and L3 (edit) paths.
- [ ] F3: Test assertions in L3 evidence — reduces blind iteration (NOT YET)
- [ ] P0-4: UNIT_PROVEN only, no caller-only scenario
- [ ] **Paired 30-task run** — measure actual flips vs baseline
- [ ] User approval

### Plumbing follow-ups (non-blocking)
- A1: signature fallback not exercised (all test repos have NULL sigs)
- A2: container GT_META captured but not re-emitted to host stdout

---

## 11. Patch-Gated Runtime Fixes (2026-05-21)

Proof command:

`PYTHONPATH=src;scripts/swebench pytest tests/unit/test_evidence_markers.py tests/unit/test_delivery_invariant.py tests/unit/test_evidence_gate.py tests/unit/test_evidence_module_errors.py tests/swebench/test_cost_tracking.py tests/router tests/openhands/test_oh_gt_full_wrapper.py::test_scaffold_edit_skip_is_host_only tests/openhands/test_oh_gt_full_wrapper.py::test_l1_logging_occurs_only_for_real_brief_injection tests/openhands/test_oh_gt_full_wrapper.py::test_tool_hint_without_brief_is_not_logged_as_l1 tests/openhands/test_oh_gt_full_wrapper.py::test_l3b_budget_skip_is_host_only tests/openhands/test_oh_gt_full_wrapper.py::test_l3b_late_iteration_skip_is_host_only tests/openhands/test_oh_gt_full_wrapper.py::test_l3_budget_skip_is_host_only tests/openhands/test_oh_gt_full_wrapper.py::test_l3_same_file_skip_is_host_only tests/openhands/test_oh_gt_full_wrapper.py::test_auto_query_no_symbols_logs_no_output tests/openhands/test_oh_gt_full_wrapper.py::test_auto_query_no_actionable_lines_logs_no_output tests/openhands/test_oh_gt_full_wrapper.py::test_auto_query_error_logs_no_output tests/openhands/test_oh_gt_full_wrapper.py::test_graph_db_chunked_base64_transfer_assembles_split_tokens`

Result: 105 passed.

- `[GT_STATUS] success` is status only, not evidence. Marker gates and hook telemetry require structural/evidence markers.
- Scaffold edits, L3/L3b budget skips, L3 same-file skips, and auto-query no-output reasons are host-only events/log records, not agent-visible `<gt-evidence>`.
- L1 logging now corresponds to actual `<gt-task-brief>` injection; tool-hint-only instruction text is not reported as L1.
- Native-tool injection logs `no_tools_payload`; async tool-call rewrite exceptions are logged.
- Graph DB fallback transfer joins split base64 tokens and validates sqlite integrity before returning a host DB path.
- Router tests follow the current deterministic budget API: total budget caps view emissions; edit emissions bypass that ceiling by contract.
- Missing graph DB remains distinct from an empty graph: `NO_GRAPH_DB` vs `NO_EVIDENCE`.

---

## 12. Archive Index

| Old Document | What was extracted | Status |
|---|---|---|
| `DECISIONS.md` | Historical decisions referenced in §5 | SUPERSEDED |
| `LATEST_TASK.md` | Task context, graph quality stats | SUPERSEDED (overconfident) |
| `GT_RUNTIME_ARCHITECTURE_AUDIT.md` | Bug registry, layer classification, delivery ordering | SUPERSEDED (merged into §3-4) |
| `IMPLEMENTATION_BUGS.md` | 18 bugs from initial code review | SUPERSEDED (merged into §4) |
| `TRAJECTORY_ANALYSIS_FINAL.md` | Dual-agent findings, flip mechanism | SUPERSEDED (key findings in §5) |
| `analysis.md` | 30-task trajectory analysis | SUPERSEDED (corrections in §5) |
| `jedi_WORK.md` | Session work log | HISTORICAL (not superseded, ongoing log) |
