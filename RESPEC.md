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

**What is broken:** 6 P0 code-proven bugs prevent evidence from reaching the agent correctly. The behavioral contract query uses wrong path matching. The delivery pipeline truncates evidence. The router suppresses valid callers. Status markers masquerade as evidence. No patch integrity checking exists.

**What is unproven:** Obligation detector, issue grounding, format contracts, mismatch detection, auto-query — all implemented in code but zero runtime proof of activation or agent impact.

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
| L3 post-edit (callers) | FUNCTIONAL_WITH_BUGS | Fires, delivers evidence. sh-744 flip proven. | P0-1 (path match), P0-2 (truncation), P0-3 (edge gate) | Fix 3 P0 bugs | RUNTIME_PROVEN (partial) |
| L3 post-edit (behavioral contract) | FUNCTIONAL_WITH_BUGS | Fires on sh-744 (single-file repo) | P0-1 blocks on multi-file repos | Fix path lookup | RUNTIME_PROVEN (single-file only) |
| L3b post-view | FUNCTIONAL | Fires on first 3 reads, delivers navigation | None active | None | RUNTIME_PROVEN |
| Router on_edit | FUNCTIONAL_WITH_BUGS | Formats caller/sibling/test evidence | P0-4 (kind mismatch suppresses callers) | Fix kind names | CODE_REVIEWED |
| Router on_view | FUNCTIONAL | Emits neighborhood context in live mode | None active | None | RUNTIME_PROVEN |
| Auto-query | HALF_WIRED | Gate fires correctly, identifies eligible files | Execution unproven; consensus may prepend first | Not in P0 scope | CODE_REVIEWED |
| Obligation detector | IMPLEMENTED_UNPROVEN | diff_text plumbing verified correct in code | No runtime proof of activation | Not in P0 scope | CODE_REVIEWED |
| Issue grounding | IMPLEMENTED_UNPROVEN | Anchors load from /tmp/gt_issue_terms.txt | No runtime proof of ranking effect | Not in P0 scope | CODE_REVIEWED |
| Format contracts | IMPLEMENTED_UNPROVEN | Queries graph.db for caller subscripts | No confidence filtering (P2 bug) | Not in P0 scope | CODE_REVIEWED |
| Mismatch detection | IMPLEMENTED_UNPROVEN | Uses diff_text + graph callers | No confidence filtering (P2 bug) | Not in P0 scope | CODE_REVIEWED |
| GT tools (injection) | FUNCTIONAL | 9 tools injected on every LLM call | Agent ignores 3/4 tools | Not in P0 scope | RUNTIME_PROVEN |
| GT tools (rewrite) | FUNCTIONAL | gt_validate→execute_bash confirmed | Only gt_validate called | Not in P0 scope | RUNTIME_PROVEN |
| L5 scaffolding_trap | FUNCTIONAL | Fires at adaptive threshold | None active | None | RUNTIME_PROVEN |
| L5 Goku | NON_FUNCTIONAL | Silently suppressed (B3 default mismatch) | P1 bug (defaults disagree) | Not in P0 scope | CODE_REVIEWED |
| Evidence markers | FUNCTIONAL_WITH_BUGS | Delivery gate works for real evidence | P0-5 ([GT_STATUS] as evidence) | Fix marker list | CODE_REVIEWED |
| Patch extraction | FUNCTIONAL_WITH_BUGS | Patches extracted and submitted | P0-6 (no integrity hash) | Add hash logging | CODE_REVIEWED |
| Scaffold strip | FUNCTIONAL_WITH_BUGS | Fires on finish | Silent no-op when base_commit missing (P2) | Not in P0 scope | CODE_REVIEWED |

---

## 4. Active Bug Registry

### P0 (must fix before any benchmark run)

| ID | Bug | File | Line | Class | Status |
|---|---|---|---|---|---|
| P0-1 | Behavioral contract path lookup — generalized suffix resolver | `post_edit.py` | ~1437 | delivery | **FIXED — REPLAY_PROVEN** (frozen beancount graph, `/testbed/` prefix resolved) |
| P0-2 | Non-live L3 truncates to 3 lines / 130 chars | `oh_gt_full_wrapper.py` | ~3804 | delivery | FIXED — UNIT_PROVEN, REPLAY_NEEDED |
| P0-3 | Improved L3 gated on `_has_edges` — blocks sparse/disconnected files | `post_edit.py` | ~2377 | delivery | **FIXED — REPLAY_PROVEN** (frozen beancount graph, zero-edge node produced evidence) |
| P0-4 | Router checks `"caller"` but actual kind is `"caller_code"` | `router.py` | ~323 | ordering | FIXED — UNIT_PROVEN_WITH_EXISTING_TEST_FAILURES, REPLAY_NEEDED |
| P0-5 | `[GT_STATUS]` in marker list makes no-evidence pass delivery gate | `evidence_markers.py` | ~10 | delivery | FIXED — UNIT_PROVEN, REPLAY_NEEDED |
| P0-6 | No patch hash in extraction pipeline — truncation undetectable | `convert_to_submission.py` | N/A | observability | **FIXED — INTEGRATION_PARTIAL** (hash on canonical patch, malformed detection, pre-eval not testable locally) |

### P1 (fix after P0, before benchmark scaling)

| ID | Bug | File | Status |
|---|---|---|---|
| P1-1 | Goku default mismatch (`"1"` vs `"0"`) silently suppresses L5b | `wrapper:1654` vs `wrapper:2863` | OPEN |
| P1-2 | GT_META stdout pollution (4 print calls missing stderr) | `post_edit.py:754-792` | OPEN |
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
| P0-2 | `wrapper:~3804` | `directive_lines[:3]` + `ln[:130]` | `"\n".join(directive_lines)[:2000]` | `pytest tests/unit/test_evidence_formatting.py` + `pytest tests/replay/test_p0_replay.py::TestReplay1ShContract::test_wrapper_formatting` | Unit: 5/5 passed. Replay: source file not on disk so generate_improved_evidence returned empty — formatting test could not prove contract survives with real data. | UNIT_PROVEN | REPLAY needs graph.db with matching source files on local disk | Revert 1 line |
| P0-3 | `post_edit.py:2415` | `if _has_edges:` | `if _has_edges or all_func_names:` | `pytest tests/replay/test_p0_replay.py::TestReplay3SparseFile` | **REPLAY PASSED**: `create_simple_posting` (0 edges in frozen beancount graph) produced `[CONTRACT ~]`, `[SIGNATURE]`, `[PATTERN]` — 458 chars of real evidence. | **REPLAY_PROVEN** (frozen beancount graph.db) | None — fix proven on real artifact | Revert condition |
| P0-4 | `router.py:323` | `"caller", "test"` | `"caller_code", "test_assertion"` | `pytest tests/router/test_on_edit.py` | 7 passed (2 pre-existing fail) | UNIT_PROVEN_WITH_EXISTING_TEST_FAILURES | REPLAY needed (caller-only evidence in live run) | Revert 1 line |
| P0-5 | `evidence_markers.py:8-15` | `"[GT_STATUS]"` in markers | `"[GT_STATUS] success"` only | `pytest tests/unit/test_evidence_markers.py` | 37/37 passed: no_evidence→False, success→True, new markers→True | UNIT_PROVEN | REPLAY needed (runtime delivery gate) | Revert tuple |
| P0-6 | `convert_to_submission.py:43-52,71-77` | No integrity checking | SHA256 + byte length + malformed detection on canonical (stripped) patch | `pytest tests/replay/test_p0_replay.py::TestReplay4PatchIntegrity` | **BOTH PASSED**: Clean hash output=predictions=`900d830090206205`. Truncated: `malformed=True` detected. | **INTEGRATION_PARTIAL** (pre-eval stage not testable locally) | Pre-eval hash verification needs runtime proof | Revert logging lines |

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

## 9. Benchmark Readiness Gates

Resume benchmark work ONLY when ALL:
- [x] All 6 P0 bugs have code fixes
- [x] All 6 P0 fixes have UNIT_PROVEN level minimum
- [x] **P0-1: REPLAY_PROVEN** — generalized suffix resolver, frozen beancount graph
- [x] P0-2: UNIT_PROVEN (replay blocked by missing source files on disk, not a code bug)
- [x] P0-3: **REPLAY_PROVEN** — frozen beancount graph, zero-edge node
- [ ] P0-4: UNIT_PROVEN, REPLAY still needed (caller-only evidence in live run)
- [x] P0-5: UNIT_PROVEN (37/37 marker tests)
- [x] **P0-6: INTEGRATION_PARTIAL** — hash + malformed detection proven, pre-eval not testable locally
- [ ] No layer classified UNPROVEN without a plan
- [x] RESPEC.md proof ledger complete
- [ ] User approval for smoke run

**Remaining before smoke:**
1. P0-4 replay (caller-only evidence not suppressed in live run) — low risk, UNIT_PROVEN
2. User approval

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
