# ORDER_1.0 Decision Audit — FINAL_ARCH_V2

**Date:** 2026-05-17
**Branch:** `jedi__branch`
**Session:** ORDER_1.0 Stage 1

## Classification Key

- **PRESERVE** — still valid, locked, correctly implemented
- **SUPERSEDED** — replaced by newer decision or V2 architecture
- **CONTRADICTED** — runtime evidence contradicts the claimed state
- **LAYER-CONFUSING** — correct mechanism placed in wrong layer
- **NEEDS IMPLEMENTATION** — designed but not yet built or wired
- **NEEDS VALIDATION** — implemented but not proven by paired metrics

---

## Decision Audit Table

| # | Decision | Classification | Citation | Reason |
|---|----------|----------------|----------|--------|
| D0 | GT+agent collaboration is the localization layer | PRESERVE | `DECISIONS.md:3-12` | Core principle. V2 reaffirms. |
| D0b | Brief is curation, not localization | PRESERVE | `DECISIONS.md:24-26` | Locked. Agent finds gold 88% alone. |
| D1 | L3 evidence = caller code + siblings + signatures + tests | LAYER-CONFUSING | `DECISIONS.md:39-75`; `post_edit.py:749` | Evidence types valid as L4 providers. Timing rule "fires after every edit up to budget 5" is router concern (L3), not provider concern (L4). |
| D2 | L3b shows callers/callees/importers on file READ | LAYER-CONFUSING | `DECISIONS.md:77-106`; `post_view.py:232` | Same: evidence types = L4 providers; "fires on every read" = L3 router decision. |
| D3 | L4 prefetch = git precedent + signatures | PRESERVE | `DECISIONS.md:107-119` | Stays as L4 provider. |
| D5 | Comparative stop/go criteria | PRESERVE | `DECISIONS.md:172-178` | Locked. No arbitrary thresholds. |
| D6 | Dev slice before frozen 30 | PRESERVE | `DECISIONS.md:179-182` | Locked. |
| D7 | Cost notification after every run | PRESERVE | `DECISIONS.md:183-186` | Locked. |
| D8 | OH wrapper switched to V1R brief | PRESERVE | `DECISIONS.md:187-201` | Confirmed active. |
| D9 | Full layer audit "all layers working" | CONTRADICTED | `DECISIONS.md:203-220`; D31 run 25903546947 | Status table based on emission counts, not delivery verification. D35 found delivery pipe broken (later proven wrong — pipe works, over-injection was the issue). |
| D10 | Anti-overfitting rules | PRESERVE | `DECISIONS.md:222-230` | Locked. |
| D11 | Product first, benchmark second | PRESERVE | `DECISIONS.md:232-245` | Locked. |
| D12 | Brief format — add signatures | PRESERVE | `DECISIONS.md:338-340` | Layer 1 feature. |
| D13 | Evidence design principles | PRESERVE | `DECISIONS.md:342-352` | Research-backed. All still valid. |
| D14 | L1 ceiling ~34% hit@3 | PRESERVE (measurement), SUPERSEDED (framing) | `DECISIONS.md:353-368` | Measurement valid. "This is the ceiling" framing rejected. V2 demotes hit@K to L1 quality metric. |
| D15 | Brief shows graph connections | PRESERVE (principle), LAYER-CONFUSING (implementation) | `DECISIONS.md:316-336` | Right idea (agent needs graph map). FINAL_ARCH tried to make neighbors ranked candidates — displaced BM25-correct files. V2 keeps neighbors as context, router supplies them on demand. |
| D16 | Integration = modify tool results at action boundaries | PRESERVE | `DECISIONS.md:247-260` | Locked. Observation augmentation stays. Router controls timing. |
| D19 | Phase B regressions = sentence-transformers missing | PRESERVE | `DECISIONS.md:268-285` | W_SEM=0 fallback works. |
| D20 | Two regression failure modes (retrieval false positive vs over-trust) | PRESERVE | `DECISIONS.md:422-454` | Locked. V2 separates these: false positive = L1 quality; over-trust = L3 router concern. |
| D22 | 7 generalization fixes | PRESERVE | `DECISIONS.md:479-503` | Locked. All repo-relative. |
| D23 | Generalization audit — 8 scenarios | PRESERVE | `DECISIONS.md:504-541` | Quick Fixes A-C valid. |
| D24 | Full relationship taxonomy — 47 types | PRESERVE (long-horizon) | `DECISIONS.md:546-616` | Layer 0 target. Not on critical path for V2. Current CALLS-only edges are sufficient if used correctly. |
| D25 | L3 self-correction via task-relevance annotation | PRESERVE | `DECISIONS.md:617-632` | Valid L4 provider feature. Gating moves to L3 router. |
| D26 | Cross-domain bridging | PRESERVE | `DECISIONS.md:634-650` | Valid L4 provider. Convergence detection (Part A) is an L3 signal. |
| D27 | Go binary build + deployment | PRESERVE | `DECISIONS.md:652-659` | Binaries deployed on VMs. |
| D28 | Submission format + run config | PRESERVE | `DECISIONS.md:664-674` | Operational config. |
| D29 | Generalization regression fix plan | PRESERVE | `DECISIONS.md:675-857` | Fixes A-D: A applied via better gate, B/C applied (silent return), D applied. G3a removed. |
| D30 | L5 event-driven triggers | SUPERSEDED | `DECISIONS.md:858-931` | Replaced by D31 governor + D34 Goku. |
| D31 | L5 trajectory governor | LAYER-CONFUSING | `DECISIONS.md:932-1075` | Correct infrastructure. 0 new hook fires on 30-task run. The governor IS the L3 router in V2 — named L5 by position, not by role. |
| D32 | next_action from callers, not tests | PRESERVE | `DECISIONS.md:1077-1143` | Priority order: callers > consumers > signature > tests. Sits in L3 consuming L4 providers. |
| D33 | Goku items 1-5 | PRESERVE (mechanism), LAYER-CONFUSING (placement) | `DECISIONS.md:1146-1236` | Structural next_action, primary edge, online tracker — all valid. Currently scattered across wrapper + post_view + governor. V2 collapses decisions into L3, evidence into L4. |
| D34 | L5 Goku event-driven governor | PRESERVE (event taxonomy), LAYER-CONFUSING (naming) | `DECISIONS.md:1239-1404` | Event types valid. §12 context budget rule locked. Governor = L3 router. |
| D34§12 | Context budget rule (max 2 L5b injections/task) | PRESERVE | `DECISIONS.md:1387-1404` | beets-5495 regression evidence. Locked. V2 makes this an L3 router rule. |
| D35 | L3/L3b delivery wiring + budget gates | PRESERVE (Part 1 closed), NEEDS VALIDATION (Part 2) | `DECISIONS.md:1406-1471` | Delivery confirmed. Budget gates (L3b<=3, L3<=5, 75% suppress) active. Need paired verification that budget gates don't suppress too aggressively. |
| FINAL_ARCH | Layer A-E static retrieval architecture | SUPERSEDED | `DECISIONS.md:1475-1661` | Rejected 2026-05-17. Blind holdout failed: +27 steps first_gold_view, 1.09 action economy. |
| FINAL_ARCH_V2 | 7-layer collaboration architecture | PRESERVE | `DECISIONS.md:1685-1899` | Current active architecture. |

## Summary

| Classification | Count |
|---|---|
| PRESERVE | 22 |
| SUPERSEDED | 3 |
| CONTRADICTED | 1 |
| LAYER-CONFUSING | 6 |
| NEEDS IMPLEMENTATION | 0 (but L5 validator wiring missing — see layer map) |
| NEEDS VALIDATION | 1 |

## Key Findings

1. **Six decisions are LAYER-CONFUSING.** D1, D2, D15, D31, D33, D34 all have correct mechanisms placed in the wrong architectural layer. The fix is the same in all cases: move WHEN-decisions to L3 router, keep WHAT-providers in L4.

2. **D9 "all layers working" is CONTRADICTED.** The claim was based on emission counts (layers generate output). Actual delivery was broken (D35 Part 1) and over-injection was the real problem (D35 Part 2).

3. **The FINAL_ARCH was SUPERSEDED with evidence.** Blind holdout showed +27 steps first_gold_view, 1.09 action economy. Static retrieval optimization was rejected.

4. **22 decisions PRESERVE.** The core principles (GT+agent collaboration, comparative criteria, no arbitrary thresholds, product first) remain locked and valid.
