# HONORED_ARCHITECTURE.md

Research-backed architecture for GroundTruth. No production code without a section here.

## Global Product Invariant

GroundTruth is an assistive context layer, not a controller.
Default safe behavior is silence.
GT must never invent targets, spam, mislead, block, or override the model.

When GT lacks high-confidence, actionable, task-relevant evidence,
it must suppress agent-visible output and log why.
Correct silence is success, not failure.

## Implementation Status

| Layer | Research verified | Invariant test | Production code | Agent-visible proof | Status |
|-------|-------------------|----------------|-----------------|---------------------|--------|
| L0 substrate | ENGINEERING_INVARIANT | pending | pending | pending | SPEC |
| Path resolver | ENGINEERING_INVARIANT | pending | pending | pending | SPEC |
| Delivery ledger | ENGINEERING_INVARIANT | pending | pending | pending | SPEC |
| L1 brief | pending | pending | pending | pending | SPEC |
| L1 edit target | pending | pending | pending | pending | SPEC |
| L1 key contracts | pending | pending | pending | pending | SPEC |
| L3 post-edit | pending | pending | pending | pending | SPEC |
| L3b post-view | pending | pending | pending | pending | SPEC |
| L4a auto-query | pending | pending | pending | pending | SPEC |
| L5 scaffold | pending | pending | pending | pending | SPEC |
| L6 pre-submit | pending | pending | pending | pending | SPEC |
| Claim checker | ENGINEERING_INVARIANT | pending | pending | pending | SPEC |

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
