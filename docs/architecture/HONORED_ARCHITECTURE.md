# HONORED_ARCHITECTURE.md

Research-backed architecture for GroundTruth. No production code without a section here.

## Global Product Invariant

GroundTruth is an assistive context layer, not a controller.
Default safe behavior is silence.
GT must never invent targets, spam, mislead, block, or override the model.

When GT lacks high-confidence, actionable, task-relevant evidence,
it must suppress agent-visible output and log why.
Correct silence is success, not failure.

## L1 Localization Invariants

**L1-INV-1 (Issue-symbol supremacy):** If issue text contains an exact function/class
name that exists in graph.db, the file containing that function MUST be searched
for edit targeting, regardless of whether v7.4 scored it in the brief's top files.

**L1-INV-2 (Edit target issue-relevance):** The edit target function must have a higher
issue-relevance score than any other candidate. Caller count is tiebreaker only (capped
at +5). Direct name mention: Functions/Methods score +1000, Classes/Interfaces score +200
(classes mentioned in issue text are setup context, not bug targets). If no function has
issue relevance, emit `<gt-orientation>` not `<gt-edit-target>`.
Research: Agentless ICLR 2025 (function-level localization), SweRank ICLR 2025.

**Tests:** tests/invariants/test_l1_issue_symbol_localization.py (10 tests)
**Code:** oh_gt_full_wrapper.py:5853 (issue-symbol file injection)

## L5b Noise Control Invariants

**L5B-INV-1 (Cap):** Max 2 L5b agent-visible injections per task.
**L5B-INV-2 (Relevance):** L5b only suggests files in brief_candidates.
**L5B-INV-3 (Dedup):** Same file never suggested twice by L5b.

Before: weasyprint got 9x L5b for table.py, float.py, column.py — all irrelevant,
all ignored. cfn-lint got 9x similarly. 0% follow rate = pure context waste.

After: max 2 firings, only for brief-candidate files, never repeated.

**Tests:** tests/invariants/test_l5b_noise_control.py (9 tests)
**Code:** oh_gt_full_wrapper.py:1837-1852 (three gates)

## L3b Per-File-Once Dedup Invariant

**DEDUP-INV-1:** L3b delivers evidence for a file AT MOST ONCE per task.
Re-reads of the same file must not re-inject callers/callees.

Before: hash-based dedup was defeated because post_view.py filters visited_files
from callers, changing the evidence body hash on each re-read. Weasyprint got
the same core callers 5x.

After: hybrid gate. `l3b_file:{path}` in evidence_sent blocks re-reads. L6 reindex
resets gate for the EDITED file only (not all files). Hash-based dedup as safety net.

**Research:** Du et al. EMNLP 2025 (context length hurts), OCD/SWEzze 2026
(8.4% sufficient), Lost in the Middle NeurIPS 2024 (repeated = dead zone).

**Tests:** tests/invariants/test_l3b_dedup_per_file_once.py (11 tests)
**Code:** oh_gt_full_wrapper.py:3694-3704 (per-file-once gate)

## L3 Test Evidence: Naming Convention Fallback

**TEST-INV-1:** If `test_<stem>.py` exists in graph.db for `<stem>.py`,
the file-grep fallback must find it even without graph edges.

Before: file-grep fallback (`_get_test_assertions_from_file`) only searched
test files found via graph edges (`SELECT ... WHERE nsrc.is_test = 1`). If
assertion linking failed (no edges from test to source), no test files were
searched. Flexget got 0 [TEST] despite test_qbittorrent.py existing.

After: `_discover_test_files_by_convention()` at post_edit.py:1371 searches
graph.db nodes for test files matching `test_<stem>`, `<stem>_test`,
`test_<stem>s`, `test_<stem>_*` patterns. Graph-independent — works even
when assertion resolution scores below threshold 3.5.

**Research:** TCTracer ICSE 2020 (naming convention signal, weight 2.0),
RepoGraph ICLR 2025 (is_test flag for test discovery).

**Tests:** tests/invariants/test_test_discovery_naming_convention.py (5 tests)
**Code:** post_edit.py:1371 (_discover_test_files_by_convention)

## T02: Assertion Resolution Strengthening

**ASSERT-INV-1 (Dynamic threshold):** Fewer candidates → lower threshold.
1 candidate: 2.0, 2-3: 3.0, 4+: 3.5 (unchanged). Cursor principle: confident
when unambiguous, silent when ambiguous.

**ASSERT-INV-2 (File-stem rescue):** When all 5 signals produce 0 candidates,
derive stem from test filename and find production functions in matching file.
Rescue threshold 2.0. Only fires when main pass found nothing → no regression.

**ASSERT-INV-3 (Resolution score stored):** `resolution_score` column in
assertions table. Schema v15.2-trust-tier. Python side can use for tiering.

**Research:** TCTracer ICSE 2020 (naming convention at file level for rescue),
edge confidence model §0.5 (fewer candidates = higher confidence).

**Code:** gt-index/cmd/gt-index/main.go:1043-1120 (dynamic threshold + rescue)

## Phase 3 Output Diet Invariants

**DIET-INV-1 (REVIEW first):** Post-edit output U-shape: PRESERVE/[REVIEW] in primacy
position before [SIGNATURE]. Research: R6 "Agents Don't Know When to Stop" (ETH 2026),
R7 CodeR verification stages.

**DIET-INV-2 (RAISES/CATCHES gate):** Exception evidence only emits when issue text
contains error-handling keywords (error, exception, raise, catch, handle, traceback,
crash, fail, etc.). Research: R4 OpenAI "relevant context, not all context."

**DIET-INV-3 (Callee suppression):** Callees suppressed during read-only exploration.
Callee info reserved for post-edit propagation checks. Research: R2/R5 Agentless
phase separation, SE-agent lifecycle.

**DIET-INV-4 (L4a dedup):** L4a suppressed when L3b already fired for same file.
Research: R2/R3 Lost in the Middle (no duplicate graph summaries).

**Tests:** tests/invariants/test_phase3_output_diet.py (11 tests)
**Code:** post_edit.py (U-shape), post_view.py (RAISES gate + callee suppress),
oh_gt_full_wrapper.py (L4a l3b_file check)

## Deep Trajectory Bug Fixes

**MISMATCH-INV-1:** Common method names (get, set, pop, keys, values, items, format,
join, split, strip, etc.) excluded from mismatch detection. Prevents false positives
when `entry.get()` removal flags conftest.py's unrelated `dict.get()`.
Research: SWE-agent NeurIPS 2024 (false positives degrade ACI trust).

**L5-INV-1 (Specific nudge):** "No Source Edits" nudge includes specific files from
brief_candidates, not just "edit source files first."
Research: TRAJEVAL 2026 (+2.2-4.6pp from specific real-time trajectory feedback).

**TOOL-INV-1 (No dead context):** GT tool instructions removed from agent prompt.
0% adoption across 12 trajectories. ~300 tokens of static context wasted.
Research: ETH Zurich AGENTS.md eval 2026 (static context reduces success + 20% cost),
Du et al. EMNLP 2025 (context length hurts even with perfect retrieval).

**Code:** mismatch.py (_COMMON_KEYWORDS), governor.py (brief_candidates in nudge),
oh_gt_full_wrapper.py (tools_hint removed)

## Pattern Evidence Decision

**P2 4.2 INTENTIONALLY NOT IMPLEMENTED.** Gating [PATTERN]/[SIMILAR] on issue keywords
would have suppressed the single most valuable GT injection in the 6-task run: sh-744's
[PATTERN] sibling wait() — which the agent used to add self.wait() in the fix. The issue
text doesn't mention "wait." Pattern evidence's value comes from showing structural
alternatives the agent didn't think of — by definition these won't match issue keywords.

## Smoke Results (Phase 3, run 26551984847)

| Task | Before | After | GT inj before | GT inj after |
|------|--------|-------|---------------|-------------|
| sh-744 | True | True | 41 | 27 (-34%) |
| weasyprint | False | **True (FLIP)** | 54 | 29 (-46%) |
| cfn-lint | False | False | 73 | 44 (-40%) |
| pypsa | False | False | 47 | 26 (-45%) |
| arviz | False | False | 31 | 15 (-52%) |
| flexget | False | False | 20 | 22 (+10%) |
| **Total** | **1/6** | **2/6** | **266** | **163 (-39%)** |

## Implementation Status

| Layer | Research verified | Invariant test | Production code | Agent-visible proof | Status |
|-------|-------------------|----------------|-----------------|---------------------|--------|
| L0 substrate | ENGINEERING_INVARIANT | path_resolution (6) | gt-index v15.2 | 6/6 tasks indexed | VERIFIED |
| Path resolver | ENGINEERING_INVARIANT | path_resolution (6) | _escape_like + LIKE | 6/6 queries work | VERIFIED |
| Delivery ledger | ENGINEERING_INVARIANT | delivery_truth (4) | _deliver_or_trace | 6/6 tasks | VERIFIED |
| L1 brief | R1,R2,R5 | l1_visibility (3) | graph_map.py + v1r_brief | 6/6 delivered | VERIFIED |
| L1 edit target | SweRank, Agentless | l1_issue_symbol (11) | wrapper:5853 + class scoring | weasyprint FLIP | FIXED |
| L1 key contracts | R2 | l1_key_contracts (6) | wrapper:5941 | 1/6 delivered | VERIFIED |
| L3 post-edit | R5,R4,TCTracer | l3_post_edit (3) + phase3 (11) | post_edit.py | 4/6 delivered | VERIFIED |
| L3b post-view | R2,R3,Du et al. | l3b_dedup (11) | post_view.py + per-file-once | 5/6 delivered | FIXED |
| L4a auto-query | R2 | phase3 (2) | wrapper:3414 + l3b dedup | 6/6 delivered | FIXED |
| L5 scaffold | R1,TRAJEVAL | l5b_noise (9) | governor.py + wrapper | 3/6 delivered | FIXED |
| L6 pre-submit | R6,R7 | l6_actionability (4) | post_edit U-shape | 4/6 delivered | FIXED |
| Claim checker | ENGINEERING_INVARIANT | claim_truth (6) | claim_checker.py | 6/6 clean | VERIFIED |
| MISMATCH | R1 (SWE-agent) | — | mismatch.py _COMMON_KEYWORDS | flexget false positive fixed | FIXED |
| Test discovery | TCTracer | naming_convention (5) | post_edit.py:1371 | flexget test found | FIXED |
| Assertion resolution | TCTracer | — | main.go dynamic threshold | schema v15.2 | FIXED |

## Verified Research Sources

| ID | Title | Authors | Year | Venue | URL | Verification |
|----|-------|---------|------|-------|-----|--------------|
| R1 | SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering | Yang, Jimenez, Wettig, Lieret, Yao, Narasimhan, Press | 2024 | NeurIPS 2024 | https://arxiv.org/abs/2405.15793 | WEB_VERIFIED |
| R2 | Agentless: Demystifying LLM-based Software Engineering Agents | Xia, Deng, Dunn, Zhang | 2024 | arXiv 2407.01489 | https://arxiv.org/abs/2407.01489 | WEB_VERIFIED |
| R3 | Claude Code Best Practices | Anthropic | 2025-2026 | Official docs | https://code.claude.com/docs/en/best-practices | WEB_VERIFIED |
| R4 | Establishing Multilevel Test-to-Code Traceability Links (TCTracer) | White, Krinke, Tan | 2020 | ICSE 2020 | https://dl.acm.org/doi/10.1145/3377811.3380921 | WEB_VERIFIED |
| R5 | Lost in the Middle: How Language Models Use Long Contexts | Liu, Lin, Hewitt, Paranjape, Bevilacqua, Petroni, Liang | 2024 | TACL vol.12 | https://aclanthology.org/2024.tacl-1.9/ | WEB_VERIFIED |
| R6 | CodeR: Issue Resolving with Multi-Agent and Task Graphs | Chen, Lin, Zeng, Zan et al. | 2024 | arXiv 2406.01304 | https://arxiv.org/abs/2406.01304 | WEB_VERIFIED |
| R7 | Coding Agents Don't Know When to Act | Gloaguen, Mündler, Müller, Raychev, Vechev | 2026 | arXiv 2605.07769 | https://arxiv.org/abs/2605.07769 | WEB_VERIFIED |

---

## Layer: L0 Graph Substrate

### Intent from DOC_OF_HONOR
gt-index Go binary creates graph.db with 7 tables. Pre-indexed before agent starts via GHA workflow.

### OpenHands lifecycle reality
Graph.db is substrate only. Not directly injected. All evidence layers query it.

### Agent need
Agent does not interact with graph.db directly. But all evidence quality depends on graph correctness.

### Research basis
ENGINEERING_INVARIANT: Schema existence and data population are correctness checks, not heuristic behavior.

### Implementation rule
- graph.db must have 7 tables after indexing
- nodes and edges must be non-empty for supported-language repos
- properties table must exist (may be empty for repos without qualifying functions)
- assertions table must exist (may be empty if no tests or linking fails)

### TDD invariant
`tests/invariants/test_path_resolution.py` (shared with path resolver — checks graph.db can be queried)

### Status
SPEC

---

## Layer: Delivery Ledger

### Intent from DOC_OF_HONOR
`_deliver_or_trace()` records every delivery attempt. Three outcomes: DELIVERED, EMPTY, MISMATCH.

### OpenHands lifecycle reality
Delivery into finish handler is a dead write. The ledger does not currently distinguish DELIVERED from DEAD_WRITE at the `_deliver_or_trace()` level (BUG-001 fix handles this in `_emit_structured_event()` separately).

### Agent need
Agent does not see the ledger. But reliable delivery tracking prevents G1 bugs (events lie about delivery).

### Research basis
ENGINEERING_INVARIANT: Delivery truth is a correctness property, not a heuristic.

### Implementation rule
Every delivery attempt must return one of:
- DELIVERED_VISIBLE — content appended/prepended, agent will see it
- SUPPRESSED_REASON — content generated but suppressed with explicit reason
- NOT_APPLICABLE — layer conditions not met, no content generated
- FAILED_REASON — generation or delivery failed with specific error
- DEAD_WRITE — content generated but appended after agent's last step

No silent success. No bare `except: pass` in delivery path.

### TDD invariant
`tests/invariants/test_delivery_truth.py`

### Status
SPEC

---

## Layer: L1 Brief

### Intent from DOC_OF_HONOR
Ranked file list with graph connections at task start. Budget 2000 chars.

### OpenHands lifecycle reality
Injected into the first agent observation (prepended). Agent sees it before any action. This is a reliable injection point — no lifecycle issue.

### Agent need
Orientation before first edit. Agent needs to know which files are relevant and how they connect.

### Research basis
| Source | Finding | Implementation constraint |
|--------|---------|--------------------------|
| R1 (SWE-agent) | Agent-computer interface design affects performance. Custom ACI for repository navigation improves resolution. | Brief must provide navigable structure, not just file names. |
| R2 (Agentless) | Hierarchical localization: files → classes/functions → edit locations. | Brief should rank files, then show key functions per file. |
| R3 (Claude Code best practices) | Plans and specs should be written before code. | Brief serves as the "spec" the agent reads before acting. |
| R5 (Lost in the Middle) | Performance highest when relevant info at beginning or end of context. | Brief appears at context start (primacy position). Keep it concise. |

### Implementation rule
- Brief fires at task start, before agent's first action
- Ranks files by graph connectivity + issue keyword relevance
- Shows key functions, callers, and callees per file
- Budget: 2000 chars max (avoids context noise)
- Appears at primacy position (start of first observation)

### TDD invariant
`tests/invariants/test_l1_visibility.py` (brief presence and content)

### Status
SPEC

---

## Layer: L1 Edit Target

### Intent from DOC_OF_HONOR
Select the most relevant function for the issue and present it as the edit target.

### OpenHands lifecycle reality
Appended to brief in `<gt-edit-target>` tags. Agent sees it at task start.

### Agent need
Root-cause localization. Agent needs to know WHICH function to edit, not just which file.

### Research basis
| Source | Finding | Implementation constraint |
|--------|---------|--------------------------|
| R2 (Agentless) | Hierarchical localization from files to functions to edit locations. | Edit target must narrow from file to specific function. |
| R1 (SWE-agent) | Interface design matters — agents benefit from structured navigation hints. | Edit target should present function with signature and location. |

### Implementation rule
- Evaluate ALL candidate functions from ALL brief files before selecting (no first-match-wins)
- If issue text explicitly names a function, that function wins regardless of caller count
- Caller count is a TIE-BREAKER, not primary signal
- Common verb parts (get, set, add, etc.) filtered from keyword matching

### TDD invariant
`tests/invariants/test_l1_visibility.py` (edit target selection logic)

### Status
SPEC

---

## Layer: L3 Post-Edit

### Intent from DOC_OF_HONOR
After agent edits, show callers, contracts, tests, signature, completeness. Budget 2000 chars. U-shaped ordering (signature first, tests last).

### OpenHands lifecycle reality
Fires on post_edit event. Appended to the edit observation. Agent sees it on the step AFTER editing. Reliable injection point.

### Agent need
Impact awareness. After editing, agent needs to know: who calls this? what contract must be preserved? what tests should pass?

### Research basis
| Source | Finding | Implementation constraint |
|--------|---------|--------------------------|
| R5 (Lost in the Middle) | Relevant info at beginning/end of context performs best. | U-shaped ordering: signature first (primacy), tests last (recency). |
| R4 (TCTracer) | Multi-signal test-to-code traceability (naming, imports, call depth). MAP 78% at method level. | Test evidence linking uses multiple signals, not just name match. Helper files (_common.py) should not outrank direct tests. |

### Implementation rule
- Fires on every edit (not just first)
- U-shaped ordering: [SIGNATURE] first, [TEST] last
- _common.py / conftest.py must not outrank direct test files (Invariant 4)
- [COMPLETENESS] scoped to edited function, not whole class (Invariant 5)
- Dunder methods excluded from [PATTERN] (Invariant 6)
- Budget: 2000 chars

### TDD invariant
`tests/invariants/test_l3_post_edit.py`

### Status
SPEC

---

## Layer: L6 Pre-Submit

### Intent from DOC_OF_HONOR
Before agent submits, show blast radius and test suggestions.

### OpenHands lifecycle reality
**CANNOT fire in finish handler.** OH sets state=FINISHED before run_action. Must fire at post-edit time instead.

### Agent need
Verification before submit. Agent needs to see what callers depend on changed code and what tests to run.

### Research basis
| Source | Finding | Implementation constraint |
|--------|---------|--------------------------|
| R6 (CodeR) | Task graph: explicit test→verify→submit stages. Verification before submission. | L6 must fire before finish, not after. |
| R7 (Coding Agents Don't Know When to Act) | Agents propose undesirable changes 35-65% of time. Explicit verification partially addresses this. | Review evidence must reach agent while it can still act. |

### Implementation rule
- L6 review fires at post-edit time (after first source edit) via L6 early review hook
- Includes caller contracts AND test suggestions from assertions table
- Agent must have at least one step available after receiving review
- Dead writes in finish handler marked `emitted=False, suppressed=True, suppression_reason="finish_handler_dead_write"`

### TDD invariant
`tests/invariants/test_l6_actionability.py`

### Status
SPEC

---

## Layer: Vendor/Dunder Filters

### Intent
Vendor JS, static files, and dunder methods must not appear in evidence.

### Research basis
ENGINEERING_INVARIANT: Filter correctness. Vendor files are not real callers. Dunder methods are not useful sibling patterns.

### Implementation rule
- `_is_vendor_path()` filters `/static/`, `/vendor/`, `/node_modules/`, `/dist/`, `.min.`, `/assets/`
- Dunder filter excludes `__init__`, `__repr__`, `__str__`, `__eq__`, `__hash__`, `__del__` from [PATTERN]
- Applied in post_view.py, governor.py, post_edit.py

### TDD invariant
`tests/invariants/test_vendor_filter.py`, `tests/invariants/test_l3_post_edit.py`

### Status
SPEC

---

## Layer: Claim Checker

### Intent
DOC_OF_HONOR claims must not outrun proof.

### Research basis
ENGINEERING_INVARIANT: Documentation truth is a correctness property.

### Implementation rule
- WORKING/VERIFIED claims require runtime/test/replay/graph proof
- Claims with only code_audit proof are UNVERIFIED
- Claims contradicted by trajectory artifacts are CONTRADICTED
- OPEN_BUG claims are not auto-skipped
- Claim checker fails CI-style on contradictions

### TDD invariant
`tests/invariants/test_claim_truth.py`

### Status
SPEC
