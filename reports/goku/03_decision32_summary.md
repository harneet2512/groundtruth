# Decision 32 Summary — Structural Next-Action Architecture

**Date:** 2026-05-15
**Status:** LOCKED
**Dependencies:** 01_layer_audit.md (smoke-1 evidence), 02_research_ledger.md (research citations)

---

## 1. Why Current GT Is Not Enough

The layer audit (01_layer_audit.md, smoke-1 cfn-lint-3862) provides five hard failure observations that collectively prove the current GT stack is structurally unable to influence agent behavior:

### 1a. Zero next_action across all 12 events

The reaction joiner (`reaction_joiner.py` line 25) requires `next_action_type` to be populated on a GTLayerEvent before it can produce a reaction record. In smoke-1, **0 of 12 GTLayerEvents had `next_action_type` populated** (audit §Meta/Reaction, item 10). The layers that never populate it: L1, L3b, L4, L6, HYGIENE. L3 populates it only when an `l3_targeted_verification` structured item is found — which did not occur in smoke-1 (audit §L3, item 10: "0 out of 1 L3 fires had next_action populated"). L5b populates it when fired — but L5 never triggered (audit §L5, item 10: "0 L5 hooks fired").

**Consequence:** The entire reaction measurement pipeline is structurally dead. GT cannot distinguish "agent followed GT suggestion" from "agent ignored GT entirely." Without reaction data, no layer can be tuned, gated, or validated against agent behavior.

### 1b. L3b bloat: 1,810 average chars per fire

L3b (post-view navigation context) has no hard token cap in `graph_navigation()` (audit §L3b, item 6). Smoke-1 observed **14,486 total chars across 8 fires = 1,810 avg chars/fire (~452 tokens/fire)**. With 8 fires in a single task, L3b injected ~3,600 tokens total — more than the entire L1 brief, all L3 evidence, and all L4 prefetch combined.

SWE-Pruner [A3] proves that 23-54% token reduction improves task success rate (64% pass vs 62% vanilla). L3b is the single largest bloat vector, injecting graph dumps without goal-conditioned selection. Each fire dumps up to 5 caller edges, 5 callee edges, and importer edges — most of which are structural noise for the current task.

### 1c. L5 zero fires on collapsed trajectory

The L5 governor did not trigger a single anti-pattern detection in smoke-1, despite the task ending with `behavior_class=collapsed` (audit §L5, item 10). The agent went from exploration to collapsed diff without triggering `no_durable_source_progress`, `premature_commitment`, or `unverified_patch`. This means L5's detection heuristics have a blind spot for the most common failure mode: the agent silently produces an empty or near-empty patch.

The specific gap: L5 tracks explicit failures (test failures, repeated errors) but does not detect **absence of structural verification** — the agent editing files without ever inspecting callers, without running any targeted test, and without the diff containing meaningful source changes. A collapsed trajectory is the signature failure mode where L5 should fire but does not.

### 1d. L4 tools installed but unused (0 tool calls)

L4 installs four tools (`gt_query`, `gt_search`, `gt_navigate`, `gt_validate`) into the container PATH (audit §L4, item 2: `install_l4_tools()` lines 1327-1385). In smoke-1, the agent invoked **zero** of these tools (audit §L4, item 10: "0 L4 tool usages observed"). The `render_l4_tool_footer()` function returns an empty string — the tool availability is never advertised to the agent.

Tool registration without agent usage is not wiring (per project memory: `"Wiring" = agent actually uses the layer, not just registered`). L4 as currently implemented is dead weight.

### 1e. Reaction chain dead end-to-end

Combining 1a through 1d: GT fires 12 events, the agent receives tokens from L1+L3+L3b+L4, but GT has no mechanism to observe whether any injection changed agent behavior. The reaction joiner produces 0 reactions. L5 produces 0 detections. L4 tools produce 0 usage. The full GT stack operates as a write-only system: it emits, but never observes the result of its emissions.

---

## 2. Research-Backed Principles

Each design rule below is grounded in a verified citation from the research ledger (02_research_ledger.md).

### 2a. Callers are the primary structural witness

**Source:** RepoGraph [A1] (ICLR 2025). "The primary navigation signal is callers/callees, not file-level similarity."

GT's graph.db already contains the call edges that RepoGraph constructs per-task. The audit confirms that L3 surfaces callers (§L3, item 3: "CALLERS (2 unseen)") and L3b surfaces callers/callees (§L3b, item 3: "Called by: ..."). The gap is that callers are rendered as flat lists instead of k-hop ego-subgraphs, and L3b dumps all edges without task-relevance filtering.

**Design rule:** Every next_action that references "read callers" must surface the actual caller code line and call context, not just the file path. The caller is the primary structural witness for contract preservation.

### 2b. Selective rendering beats comprehensive dumps

**Source:** SWE-Pruner [A3] (arXiv 2026). "Task-aware pruning outperforms fixed compression. 23-54% token reduction while maintaining or improving task success."

L3b's 1,810 avg chars/fire is the anti-pattern SWE-Pruner warns against. Injecting all structural context regardless of task relevance is noise. The fix is goal-conditioned selection: render only the edges that connect to task-relevant symbols, suppress the rest.

**Design rule:** L3b renders one primary edge per type (highest task-relevance), with alternatives available as structured-only metadata (not rendered to agent). Total L3b rendered text must decay over iterations.

### 2c. No-test validation is a proven path

**Source:** Agentless [A5] (arXiv 2024). "A three-phase pipeline (localization, repair, patch validation via syntax + regression checks without test execution) achieved 32% on SWE-bench Lite."

The current L5b hooks default to "run one targeted test" as the next_action (audit §L5b, item 3: "Next action: revise the edit that produces the wrong result"). This assumes test infrastructure is available and that the test is the right verification. Agentless proves that syntax check, import check, and caller contract check constitute a viable validation hierarchy without test execution.

**Design rule:** Tests are optional bonus, not primary next_action. The structural validation hierarchy (callers, consumers, signatures, static analysis) fires first. Tests supplement when available.

### 2d. Concise, structured feedback at the interface boundary

**Source:** SWE-agent [A6] (arXiv 2024). "Custom agent-computer interfaces that provide concise, well-structured feedback to the LLM significantly improve autonomous software engineering."

**Source:** FeedbackEval [A9] (arXiv 2025). "Mixed structured feedback yields the highest repair success (63.6%). Diminishing returns observed after multiple feedback iterations."

GT's observation-boundary injection is the ACI surface. Every GT message the agent sees must be terse and actionable. The diminishing-returns finding from FeedbackEval means GT should rate-limit after the first few substantive responses — which the L3b decay mechanism attempts but does not enforce (no hard token cap).

**Design rule:** Every rendered GT message has a hard token cap. L3b decays from 250 tokens (early) to 0 (final). Repetitive evidence is deduped, not re-injected.

### 2e. Richer relationship taxonomy

**Source:** CodexGraph [A2] (NAACL 2025). "The graph schema goes beyond call edges to include containment, inheritance, and usage relationships."

graph.db currently tracks CALLS and IMPORTS edges only (audit §Relationship Indexer, item 2: `_EDGE_TYPE_TO_REF` covers CALLS, IMPORTS, DEFINES, INHERITS, IMPLEMENTS). CodexGraph validates that CONTAINS (parent-child), INHERITS, and USES (decorator/config consumer) edges enable more precise navigation. GT should index these relationships to support richer next_action types (e.g., "check all classes that inherit from BaseRule" or "check all routes that use this decorator").

**Design rule:** The relationship indexer adds decorator, route, config consumer, event, and inheritance edges. These feed next_action generation but do not increase rendered token count.

---

## 3. Locked Design Rules

### Rule 1: Tests are optional bonus, not primary next_action

When GT suggests a next action after an edit, the priority is structural verification first:

```
READ_CALLER_CONTRACT   → Read the code line where callers invoke the edited function
READ_CONSUMER          → Read downstream consumers that depend on the edited return type/API
CHECK_SIGNATURE        → Verify function signature compatibility with callers
RUN_STATIC_SANITY      → Run syntax/import/type check (no test execution)
RUN_TARGETED_TEST      → Run a specific test file that imports the edited module
NONE                   → No actionable next step (evidence is informational only)
```

Rationale: Agentless [A5] proves no-test validation works. The audit shows L5b defaults to test suggestions that may be empty (§L5b, item 11: "When `test_file_suggestions` is empty, the agent gets direction without destination"). Structural verification always has a target (callers exist in graph.db); test verification may not.

### Rule 2: L3b renders one primary edge, alternatives are structured-only

For each edge type (callers, callees, importers), L3b selects the single highest task-relevance edge and renders it. Remaining edges are emitted as structured telemetry items but NOT rendered to the agent.

Hub suppression: edges from/to hub files (in-degree > p90 of the graph) are demoted to structured-only unless they are the only edge of that type.

Token caps with iteration decay:
- Early (iterations 0-30%): max 250 rendered tokens
- Mid (iterations 30-60%): max 160 rendered tokens
- Late (iterations 60-85%): max 80 rendered tokens
- Final (iterations 85-100%): silent (0 rendered tokens)

Rationale: SWE-Pruner [A3] on selective rendering. The audit shows L3b is the biggest bloat vector at 1,810 avg chars/fire (§L3b, item 6).

### Rule 3: L5 detects ignored witnesses, not just failing tests

New detection hooks beyond the existing set:

- **`ignored_next_action`**: GT emitted a next_action with type != NONE, and the agent's subsequent action did not match the suggestion within 3 iterations. Requires reaction joiner to be functional (depends on Rule 1 populating `next_action_type`).
- **`structural_unverified_patch`**: Agent is at iteration >= 60% with source edits but has never inspected a caller of any edited function (no `post_view` on a caller file, no `gt_query` for a caller).
- **`collapsed_diff`**: Agent's cumulative diff has fewer than 5 meaningful lines (excluding whitespace, comments, imports-only) at iteration >= 50%.
- **`broad_only_verification`**: Agent ran broad tests (e.g., `pytest`, `make test`) but never ran a targeted test (single file or single test function) for any edited module.

Rationale: Smoke-1 shows L5 fired 0 times on a collapsed trajectory (§L5, item 10). The governor detects explicit failures but misses silent non-engagement.

### Rule 4: L5b emits one concrete action, no restart, no late exploration

Every L5b message must:
1. Name one specific file or symbol the agent should act on next
2. Specify the action type from the Rule 1 hierarchy
3. Not contain restart/start-over language (existing L5bSafetyChecker enforces this)
4. Not suggest broad exploration ("look at the codebase", "search for related files") after iteration 50%

Token cap: 180 tokens (existing, enforced by L5bSafetyChecker).

### Rule 5: Every next_action produces a reaction or NOT_MEASURABLE

For every GTLayerEvent that populates `next_action_type`:
- The reaction joiner MUST produce a GTAgentReactionEvent classifying the outcome as one of: `FOLLOWED_EXACT`, `FOLLOWED_PARTIAL`, `IGNORED`, `CONTRADICTED`, `NOT_MEASURABLE`.
- `NOT_MEASURABLE` is used when the agent's subsequent actions cannot be classified (e.g., agent finished immediately after GT event).
- Events without `next_action_type` do not enter the reaction pipeline (existing behavior, correct).

Layers that must populate `next_action_type` after this decision:
- L3 (always, using Rule 1 hierarchy — not just when `l3_targeted_verification` found)
- L5b (always when fired — existing behavior, correct)
- L3b (when the primary rendered edge implies a navigation action — new)

Layers that remain without `next_action_type`:
- L1 (inject-once, no follow-up expected)
- L4 (prefetch, informational)
- L6 (invisible to agent)
- HYGIENE (invisible to agent)

### Rule 6: Anti-overfitting constraints

- **No task IDs in code.** No `if task_id == "cfn-lint-3862"` or equivalent. All logic must be structural properties of the code/issue, not per-task patterns.
- **No repo-specific logic.** No `if "cfnlint" in repo_name` or equivalent. All heuristics must work on any repo indexed by gt-index.
- **No benchmark-specific thresholds.** Numeric thresholds (token caps, decay rates, confidence floors) must be derived from measured distributions across multiple repos, not tuned on the 15 SWE-bench-Live Lite tasks.
- **Verify generalization first.** Any new capability must be tested on at least 3 repos in different languages before measuring on the benchmark tasks.

---

## 4. Implementation Veto List

The following approaches are explicitly vetoed. Do not propose, implement, or spend time on these:

| Vetoed Approach | Reason |
|----------------|--------|
| Adding an LLM/reranker step to any GT layer | Per project constraint: "GT must stay LLM-free in its core pipeline." All evidence generation is deterministic, $0 AI cost. |
| Making L4 tools the primary interaction mechanism | Smoke-1 shows 0 tool usage. Agent tool adoption requires model-level fine-tuning or system prompt changes outside GT's control. L4 tools remain available but are not the primary delivery channel. |
| Per-task prompt engineering | Anti-overfitting Rule 6. The brief and hooks must work identically across all tasks. |
| Increasing L3b edge limits to show more context | Directly contradicts SWE-Pruner [A3] findings and the audit's identification of L3b as the primary bloat vector. Direction is fewer, better-selected edges. |
| Adding a daemon or persistent process | Per project constraint: "No daemon process (MCP stdio)." All GT operations are stateless per-invocation. |
| Building custom eval harness | Per project memory: "use Microsoft's SWE-bench-Live harness ONLY." |
| Kernel/steering work | Per project constraint: "kernel is shelved until product is shipping." |
| Vector embeddings for retrieval | Per project constraint: "No vector embeddings (FTS5 + graph queries are sufficient)." |
| Cross-repo blast radius | Per Blast Radius [B1] finding: the cross-repo version is hard to sell and outside GT's single-repo scope. |
| Human-calendar time estimates for implementation | Per project memory: "No human-calendar time estimates." |
