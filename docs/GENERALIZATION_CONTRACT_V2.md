# GENERALIZATION_CONTRACT_V2 — FINAL_ARCH_V2

**Date:** 2026-05-17
**Branch:** `jedi__branch`
**Session:** ORDER_1.0 Stage 2.5

---

## Forbidden

- No task-id rules
- No repo-name rules
- No gold-file-specific logic
- No benchmark-specific path patterns
- No thresholds tuned from a single task
- No prompts or routing logic optimized only for the current 3-task/5-task set
- No success claims from dev-set improvement only

## Required evidence for every architectural change

Every change must state:
1. What generalized failure mode it addresses
2. Why the fix should apply across repositories
3. Which layer contract it improves
4. Which metric should move if it works
5. Which holdout will test it
6. What result would falsify it

## Dataset split discipline

| Role | Tasks | Usage rule |
|---|---|---|
| DEV_SET | Tasks used for debugging implementation | May tune code against these. CANNOT claim generalization from these. |
| CANARY_SET | 2-3 tasks for regression detection | beets-5495, loguru-1297, beancount-931 (current). Regression gate only. |
| HOLDOUT_SET | Unseen tasks for generalization signal | Must NOT be used to design or tune a fix. |
| EXPANSION_SET | 15/30/300 for official evaluation | Only after holdout passes. |

A task used to design or tune a fix CANNOT be used to claim generalization.

## Generalization metrics (report separately)

| Metric set | What it measures | When reported |
|---|---|---|
| Dev-set layer utilization | GT produces intended signals on debugging tasks | After local testing |
| Canary action-path metrics | V2 vs OLD_GT vs BASELINE regression | After canary run |
| Holdout action-path metrics | V2 generalizes beyond debug tasks | After holdout run |
| Cross-repo behavior | Same code works on different repos | After multi-repo test |
| Cross-task variance | Consistent behavior across task types | After 15+ task run |
| Negative-transfer cases | Where GT hurts vs baseline | Always flagged explicitly |

## Required holdout gates

- V2 must not be worse than OLD_GT on median action_count
- V2 must not be worse than OLD_GT on first_gold_edit_step
- V2 must produce non-zero agent-visible evidence when OLD_GT does
- V2 must reduce stale/late/injection failures or preserve action economy
- Any improvement must appear on unseen tasks, not only tasks used during debugging

## Research-backed change rule

If a layer redesign is needed, cite reputable general methods:

| Source type | Examples |
|---|---|
| Structured code retrieval | BM25, TF-IDF, dense retrieval literature |
| Graph-based navigation | RepoGraph ICLR 2025, CodexGraph NAACL 2025 |
| Agent-state-aware routing | Strands AWS 2025, SWE-agent ACI NeurIPS 2024 |
| Retrieval/reranking | SWE-Pruner 2025, Agentless ICLR 2025 |
| Agent scaffolding systems | OpenHands, SWE-Search ICLR 2025 |
| Official framework docs | OH 0.54 API, FastMCP, etc. |

Each research note must include:
- Source
- Principle
- Generalized design implication
- Rejected benchmark-specific shortcut
- Expected metric impact

## Generalization decision rules

| Observation | Interpretation |
|---|---|
| Dev-set improves, holdout fails | Overfit or metric illusion |
| Static metrics improve, agent-path metrics fail | Retrieval improved, collaboration not proven |
| Local passes, GHA fails | Runtime parity failure, not product result |
| V2 emits evidence but action path worsens | Evidence is mistimed or harmful |
| V2 improves action path but resolve stays flat | Localization improved; fix-quality layer unsolved |

## Current compliance check

| Rule | Current status | Evidence |
|---|---|---|
| No task-id rules | COMPLIANT | Router code contains no task IDs. Wrapper contains no task IDs. |
| No repo-name rules | COMPLIANT | No repo-name conditionals in any GT source. |
| No gold-file-specific logic | COMPLIANT | No gold file references in product code. |
| No benchmark-specific paths | COMPLIANT | All paths are parameterized via config. |
| No single-task thresholds | COMPLIANT | Budget caps (3/5) from D35 research-backed. Debounce=3 from D34§12 beets evidence. |
| Dataset split discipline | PARTIAL | CANARY_SET defined (3 tasks). HOLDOUT_SET not yet selected. |
| Research backing | COMPLIANT | D34 cites 12 papers. D33 cites 6 papers. All budget/routing decisions research-backed. |
