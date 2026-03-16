# GroundTruth — New TODOs (gt-theory.md EXPANSION Review)

> Generated from CEO Plan Review (2026-03-16, EXPANSION mode on gt-theory.md)

## P1 — Must Do

### 1. Redesign proof.py for Passive Mode
- **What:** Proof tracks host-side GT usage: (1) injection hash (proves context was injected), (2) validation count per task, (3) grounding record attached. Replaces agent tool call counting.
- **Why:** Current proof.py validates GT usage by counting agent MCP tool calls. In passive mode the agent never calls GT tools — proof rejects every GT task. Blocking incompatibility.
- **Pros:** Enables passive mode benchmark. Proof becomes more meaningful (measures actual GT activity, not agent behavior).
- **Cons:** Breaks backward compatibility with active-mode proof validation.
- **Context:** `benchmarks/swebench/proof.py` — MCPProof class, validity rules check "substantive tool count." Passive mode has zero agent tool calls by design.
- **Effort:** S
- **Priority:** P1
- **Depends on:** Nothing

### 2. Pre-Index SWE-bench Repos as Offline Artifacts
- **What:** Index all ~20 unique SWE-bench Lite repos once offline. Store SQLite artifacts (one per repo+commit SHA). Load at task start (<1s). Zero runtime indexing.
- **Why:** Index timeouts caused >100% failure rate in early benchmark (48 timeouts out of 38 completed tasks). This is the #1 cause of the original regression. Pre-indexing mirrors the production model (index once, use forever) and enables richer indexes with contract data.
- **Pros:** Eliminates all indexing latency from benchmark. Enables pre-computing contracts and risk scores. Reusable across runs.
- **Cons:** Upfront cost (~5 hours for all repos). Need versioning (see TODO 13). Need storage for artifacts.
- **Context:** SWE-bench Lite has ~20 unique repos (Django appears in many tasks). Current indexing: 8-15s per repo from scratch. Many tasks share repos at different commits.
- **Effort:** M
- **Priority:** P1
- **Depends on:** Contract extractor design (TODO 3) — artifacts should include contracts from the start

### 3. Build Contract Extractor
- **What:** Derive behavioral contracts from docstrings + usage patterns + call graph. Examples: "filter() is pure, returns new QuerySet" (from docstring + 47 callers using return value), "initialize() must be called before process()" (from call ordering patterns). Confidence-gated: only contracts above 0.8 threshold are included.
- **Why:** gt-theory.md Section 6: "The model doesn't lack context. It lacks understanding." Contracts carry understanding. This is the 10x differentiator — what makes GT irreplaceable vs "just another RAG pipeline." RAG gives file paths and signatures. GT gives behavioral contracts and blast radius.
- **Pros:** Addresses the 55% of hallucinations that are logic/semantic errors (taxonomy #11-18). Unique capability no other tool provides.
- **Cons:** Risk of wrong contracts (mitigated by confidence gating). Requires Haiku for ambiguous cases. L-sized effort.
- **Context:** Data already exists in symbol graph — call graph, usage counts, docstrings, signatures, LSP hover info. New module: `src/groundtruth/analysis/contract_extractor.py`. CLAUDE.md already designs `analysis/` directory for this purpose.
- **Effort:** L
- **Priority:** P1
- **Depends on:** Nothing (uses existing symbol store)

### 4. Define Understanding Schema for Context Injection
- **What:** Concrete format spec for the ~400 tokens injected into the system prompt. Must include: relevant files (5), behavioral contracts (confidence-gated), blast radius warnings ("changing X breaks 14 files"), fix location guidance ("root cause likely in queries/, not routes/"). Structured text or JSON.
- **Why:** The ~400 tokens are the entire product. gt-theory.md says "~400 tokens of structural codebase context" but doesn't define content. The difference between "file paths + signatures" and "contracts + blast radius + fix guidance" is the difference between marginal improvement and transformative impact.
- **Pros:** Makes the injection reviewable, testable, and measurable. Enables A/B testing different schemas.
- **Cons:** Schema lock-in — changing it later requires re-running benchmarks.
- **Context:** Current `groundtruth_brief` + `groundtruth_find_relevant` produce raw data. Need a `BriefingDistiller` that ranks facts by relevance to the task and fills the 400-token budget optimally.
- **Effort:** M
- **Priority:** P1
- **Depends on:** Contract extractor (TODO 3) — schema must accommodate contract data

### 5. Async Validation Contract
- **What:** Post-edit validation is async with 2s hard cap. If GT doesn't respond in 2s, edit result goes through unvalidated (agent never blocks on GT). Includes incremental re-index of edited file before validation (with contract extraction for new symbols).
- **Why:** The benchmark proved latency kills — the agent spending turns waiting for GT is the mechanism that caused the 73.7% regression. Stale index after edits causes false positives (agent creates function in edit 1, imports it in edit 2, GT flags it as missing).
- **Pros:** Eliminates latency as a failure mode. Incremental re-index prevents false positives on new symbols.
- **Cons:** 2s cap means some validations may be skipped (especially if AI fallback fires).
- **Context:** gt-theory.md Section 12 claims <100ms but Haiku API calls are 500ms-2s. Re-index of one file via LSP is <100ms. Validation latency budget: deterministic <100ms, AI fallback ~1s.
- **Effort:** S
- **Priority:** P1
- **Depends on:** Nothing

### 6. Validation Precision Policy
- **What:** Validation only reports errors at high confidence (>0.9). Unknown symbols (not in index) are NOT reported as errors — skipped with a note. Confidence-gated contracts (>0.8). False positive rate target: <5%. Distinguish "definitely wrong" vs "might be wrong" vs "unknown."
- **Why:** False positives are the highest-risk failure mode. GT telling the agent to "fix" correct code is exactly the mechanism that caused the 73.7% regression. Precision > recall: a missed error costs one bug, a false positive costs multiple wasted turns.
- **Pros:** Eliminates the #1 risk. Measurable target (<5% FP rate).
- **Cons:** May miss some real errors at the margin. Need to tune thresholds.
- **Context:** Current AstValidator has no confidence thresholds. Every detected issue is reported equally. Needs per-error-type confidence scoring.
- **Effort:** S
- **Priority:** P1
- **Depends on:** Nothing

### 7. Three-Tier Smoke Test Suite
- **What:** Must pass before full 300-task benchmark. Tier 1: Zero false positives on 50 known-correct SWE-bench patches. Tier 2: GT doesn't reduce resolve rate on 10 easy tasks vs baseline. Tier 3: Chaos tests — kill LSP mid-index, corrupt artifact, return garbage from Haiku, create new file in edit 1 and import in edit 2.
- **Why:** A full 300-task run costs real money and time. EXPANSION adds new components (contracts, risk scores) — more code, more risk of integration bugs. Smoke suite catches them for ~3% of the cost.
- **Pros:** Prevents wasting a full benchmark run. Tests graceful degradation. Validates the false positive target.
- **Cons:** Need to select representative tasks. Chaos tests require test infrastructure.
- **Context:** `runner.py` and `agent.py` support running subsets. Need task selection criteria and pass/fail definitions.
- **Effort:** M
- **Priority:** P1
- **Depends on:** Pre-index artifacts (TODO 2), Async validation (TODO 5), Precision policy (TODO 6)

### 8. Full Research Instrumentation
- **What:** Per-task structured JSON: contracts injected (with confidence scores), contracts followed/ignored by agent, risk scores vs actual hallucination rates, validation FP/TP breakdown, validation latency (p50/p95), context injection size (tokens), index load time.
- **Why:** Without this, the benchmark produces one number with no explanatory power. Can't answer "do contracts help?" or "which tasks did GT help?" or "what caused the regression?" This data is what makes the benchmark a research contribution, not just a leaderboard entry.
- **Pros:** Enables per-category analysis (TODO D4), contract compliance measurement (CLAUDE.md Layer 1), risk score validation.
- **Cons:** Storage overhead (~10KB per task JSON = ~3MB total — trivial).
- **Context:** `interventions` + `validation_exhibits` schema exists in `schema.sql`. `stats/tracker.py` has logging infrastructure. Need to wire into passive integration path and add contract-specific fields.
- **Effort:** M
- **Priority:** P1
- **Depends on:** Contract extractor (TODO 3), async validation (TODO 5)

## P2 — Should Do

### 9. Phase Map + Passive/Active Reconciliation in gt-theory.md
- **What:** Add to gt-theory.md: Phase 1 (passive, benchmark), Phase 2 (passive + selective active), Phase 3 (adaptive briefing), Phase 4 (platform SDK). Reconcile Sections 2-3 (agent never sees GT) with Section 8 (6 tools agent calls directly).
- **Why:** The document has two contradicting theses. Anyone reading it cold will be confused about whether GT is passive or active.
- **Context:** Section 8 "Lifecycle Categories" lists mid-task tools (explain, impact, trace, symbols, context, validate) as agent-callable, contradicting the passive-only benchmark design in Section 11.
- **Effort:** S
- **Priority:** P2
- **Depends on:** Nothing

### 10. Reproducibility Checklist
- **What:** Add to gt-theory.md Section 11: pinned model ID (not "GPT-5-mini"), GT commit SHA, SWE-bench Lite dataset version, Python/OS/hardware, LSP server versions, pre-index artifact hashes.
- **Why:** Standard benchmark methodology. Without it, results aren't reproducible or publishable.
- **Context:** Section 11 says "Full integration code: github.com/you/groundtruth/benchmarks/" — right instinct but insufficient.
- **Effort:** S
- **Priority:** P2
- **Depends on:** Nothing

### 11. Separate Intelligence Layer from Delivery Layer
- **What:** Architecture: ContractExtractor, RiskScorer, BriefingRanker are host-agnostic (intelligence layer). SystemPromptInjector, PostEditValidator, GroundingRecordBuilder are host-specific (delivery layer). Clean separation.
- **Why:** Prevents the tools.py megafile anti-pattern from spreading. Enables Phase 4 platform trajectory (any host plugs into intelligence layer). Makes testing easier (test intelligence without delivery).
- **Context:** Currently `groundtruth_brief` mixes "what to know" and "how to deliver it." Cursor would format differently than Claude Code.
- **Effort:** M
- **Priority:** P2
- **Depends on:** Contract extractor (TODO 3), understanding schema (TODO 4)

### 12. Docstring Sanitization for Prompt Injection Defense
- **What:** Strip non-code characters from extracted docstrings. Truncate to 200 chars. Reject patterns that look like instructions ("ignore", "forget", "instead", "do not follow"). Basic defense before contract data enters the system prompt.
- **Why:** Contract extraction from docstrings creates a prompt injection vector. A malicious docstring gets extracted as a "contract" and injected into the agent's system prompt. Low risk for SWE-bench (trusted repos), high risk for production.
- **Context:** Existing `sanitize_for_prompt` in `utils/` may be reusable. This is a cheap first defense; proper classification is a production concern.
- **Effort:** S
- **Priority:** P2
- **Depends on:** Contract extractor (TODO 3)

### 13. Pre-Index Artifact Versioning
- **What:** Add `gt_artifact_version` field to SQLite metadata table. On load: if version mismatch, re-index. Prevents stale artifacts when contract extractor or schema changes.
- **Why:** Updating the contract extractor or risk scorer makes all existing artifacts stale. Without versioning, get subtle bugs from mismatched data.
- **Context:** `index_metadata` table already exists in `schema.sql`. Add one column.
- **Effort:** XS
- **Priority:** P2
- **Depends on:** Pre-index pipeline (TODO 2)

## P3 — Delight Opportunities (Vision)

### D1. Confidence-Colored Validation Output
- **What:** Validation errors color-coded by confidence: red (>0.95, certain), yellow (0.7-0.95, likely), gray (uncertain, skipped). Agent prioritizes red, ignores gray.
- **Why:** Reduces false positive impact. Agent knows what to trust.
- **Effort:** S (~20 min)

### D2. "GT Caught This" Annotations on Patches
- **What:** After benchmark, annotate each resolved task with what GT prevented. "Task django-12345: GT prevented wrong import path (L8) and stale function name (L23)."
- **Why:** Makes the benchmark result a story for blog posts and Cursor CTO pitches. Not just a number.
- **Effort:** S (~30 min)

### D3. Hallucination Hotspot Warnings in Context
- **What:** Include top-3 confusing symbols in context injection: "This repo has 3 different validate() functions — be specific about which module."
- **Why:** Directly addresses error taxonomy #11 (wrong function, right name). Cheap, high-value.
- **Effort:** S (~30 min)

### D4. Task-Type Classification for Per-Category Analysis
- **What:** Tag each SWE-bench task by error taxonomy category (surface/structural/logic). Report resolve rate per category: "GT +15% on surface errors, +2% on logic errors."
- **Why:** The finding that makes the benchmark publishable as research, not just a leaderboard entry.
- **Effort:** S (~30 min)

### D5. Terminal Progress Dashboard During Benchmark
- **What:** Real-time terminal display: progress bar, resolve rates, contract compliance, FP rates, top catches, top contracts used. The artifact you screenshot for the blog.
- **Why:** Transforms "run overnight, check one number" into a live experience. Also catches issues early (FP rate spiking = stop the run).
- **Effort:** M (~2 hours)

## Error & Rescue Registry

```
  METHOD                    | EXCEPTION            | RESCUED? | ACTION              | USER SEES
  --------------------------|----------------------|----------|---------------------|------------------
  load_artifact             | FileNotFoundError    | NEEDED   | Inline re-index     | Delayed start
  load_artifact             | sqlite3.DatabaseError| NEEDED   | Inline re-index     | Delayed start
  load_artifact             | VersionMismatch      | NEEDED   | Re-index            | Delayed start
  contract_extract          | No docstrings        | OK       | Skip contracts      | Facts-only context
  contract_extract          | Wrong contract       | CRITICAL | Confidence gate     | Misleading context
  briefing_distill (Haiku)  | APIError/Timeout     | NEEDED   | Facts-only fallback | Weaker context
  briefing_distill (Haiku)  | Malformed response   | NEEDED   | Validate, skip bad  | Facts-only context
  briefing_distill (Haiku)  | >400 tokens          | NEEDED   | Hard truncate       | Partial context
  briefing_distill          | No relevant symbols  | NEEDED   | Skip injection      | Vanilla prompt
  post_edit_reindex         | LSP died             | NEEDED   | Skip re-index+valid | No feedback
  post_edit_reindex         | TimeoutError (>2s)   | NEEDED   | Validate stale      | May have FPs
  post_edit_reindex         | Syntax error in file | NEEDED   | Skip AST parse      | No feedback
  post_edit_validate        | False positive       | CRITICAL | Confidence thresh   | Wrong advice
  post_edit_validate        | AstValidator crash   | NEEDED   | Catch, skip, log    | No feedback
  post_edit_validate        | sqlite3.Error        | NEEDED   | Skip, log           | No feedback
  final_validate            | Compound failures    | NEEDED   | Best-effort record  | Partial record
```

## Failure Modes Registry

```
  CODEPATH          | FAILURE MODE          | RESCUED? | TEST? | USER SEES?    | LOGGED?
  ------------------|-----------------------|----------|-------|---------------|--------
  Load artifact     | Missing artifact      | NEEDED   | T1    | Delayed start | NEEDED
  Load artifact     | Corrupt/stale version | NEEDED   | T3    | Delayed start | NEEDED
  Contract extract  | Wrong contract        | CONF.GAT | T1    | Bad context   | NEEDED  <- CRITICAL
  Briefing distill  | Haiku fails           | NEEDED   | T3    | Weak context  | NEEDED
  Briefing distill  | Garbage response      | NEEDED   | T3    | Bad context   | NEEDED  <- CRITICAL
  Post-edit reindex | LSP died              | NEEDED   | T3    | No feedback   | NEEDED
  Post-edit validate| False positive        | CONF.GAT | T1    | Wasted turns  | NEEDED  <- CRITICAL
  Post-edit validate| Timeout               | NEEDED   | T3    | No feedback   | NEEDED
  Final validate    | Compound failures     | NEEDED   | T3    | Partial record| NEEDED
```

T1 = Smoke Tier 1, T3 = Smoke Tier 3 (chaos). CONF.GAT = Confidence gating mitigates but doesn't eliminate.
3 CRITICAL rows.

## NOT in Scope

| Item | Rationale |
|------|-----------|
| Behavioral contract *validation* in post-edit hook | Phase 2 — extraction first, validation later |
| Adaptive briefing from intervention history | Phase 3 — needs data from Phase 1 runs |
| Host SDK / platform API | Phase 4 — needs two successful hosts first |
| Multi-model benchmarking (Claude, GPT-4, etc.) | One model first, expand after |
| Web dashboard for results | Terminal dashboard (D5) is sufficient |
| Active mid-task tools in benchmark | Passive-only for this benchmark |

## What Already Exists

| Sub-problem | Existing code | Reused? |
|-------------|--------------|---------|
| Symbol indexing | `index/indexer.py`, `store.py`, `schema.sql` | Yes — base for pre-index artifacts |
| Import graph | `index/graph.py` | Yes — BFS for relevant files, blast radius |
| AST validation | `validators/ast_validator.py` (Py, TS, Go) | Yes — surface error detection |
| Briefing generation | `ai/briefing.py` | Yes — base for distiller |
| Task parsing | `ai/task_parser.py` | Yes — symbol extraction from task desc |
| Intervention tracking | `stats/tracker.py`, `interventions` table | Yes — wire into passive path |
| Grounding records | `grounding/record.py` | Yes — for Layer 3 final gate |
| Benchmark harness | `runner.py`, `agent.py`, `config.py` | Rework for passive mode |
| Proof system | `proof.py` | Redesign (TODO 1) |
| Risk scorer design | CLAUDE.md `analysis/risk_scorer.py` | Not built, design exists |
| Contract extraction design | CLAUDE.md `analysis/` | Not built, concept exists |

## Dream State Delta

```
  12-MONTH IDEAL                           THIS PLAN GETS US
  -------------------------------------------  ----------------------------------
  Catches logic errors (55%)               Contracts extracted + injected
                                           (detection, not validation yet)
  Adaptive briefing based on risk          Risk scores computed, not yet adaptive
  Host SDK for Cursor/Claude Code          Intelligence/delivery separation (P2)
  Published paper with ablation study      Research dataset with per-task data
  Behavioral contract validation           Phase 2 (designed, not built)
  Continuously improving prevention        Phase 3 (designed, not built)
```

**Gap vs prior HOLD SCOPE review:** This EXPANSION plan addresses the 55% logic error gap through contract extraction and understanding-based context injection. HOLD SCOPE left that for "after Lite results." EXPANSION builds the foundation now.

## Diagrams Produced

1. System architecture (Section 1) — full passive flow with NEW components
2. Data flow with shadow paths (Section 4)
3. Error/rescue table (Section 2)
4. Phase map (Section 10)
5. Rollout sequence (Section 9)
