# v6-smoke Milestone — GT as Flip Engine

## 1. What v6-smoke Is

v6-smoke is an engineering milestone to make GroundTruth a **flip engine**: a system that creates new task resolutions the base model misses, not just avoids harm.

Prior runs proved GT can deliver evidence (v2: 6/10, +1 delta). But most evidence was generic (caller counts, precedent commits). The one confirmed flip (task 13236) came from SIBLING evidence — a behavioral inconsistency signal. v6-smoke makes these flip-causing signals the primary focus.

## 2. Why It Exists

| Run | Result | Lesson |
|-----|--------|--------|
| v1 | 5/10, delta=0 | GT delivers but no import edges |
| v2 | 6/10, delta=+1 | Import fix + ranking = first flip |
| v3 | 3/9, regression | Wrong localization is worse than none |
| v4 | 4/9 | Disabling briefing partially recovers |
| v5 | 5/10, delta=0 | Confidence gating works, 13236 recovered |

GT needs to go from "sometimes helpful, never harmful" to "reliably flips tasks the base model misses."

## 3. GT Core vs Last-Mile Delivery vs Product Narrative

### GT Core
Structured state and deterministic interfaces:
- **Localization state**: candidate files/symbols with confidence tiers
- **Consistency state**: sibling patterns, peer conventions, local invariants
- **Contract state**: return-shape obligations, exception expectations, string format contracts
- **Coupling state**: changed-here-check-there, completeness across module pairs
- **Validation state**: arity breaks, stale references, import contradictions

### Last-Mile Delivery
How GT reaches the coding agent:
- Hook/state-command outputs (`_state_gt`)
- Confidence-gated briefings (verified/likely/possible tiers)
- Post-edit critique injections
- MCP tool responses
- Prompt template projections

### Product Narrative
External positioning:
- "Prevents hallucinations in AI coding agents"
- "Compiler-grade codebase intelligence"
- "Works with any MCP client"

These three layers must remain distinct in code and documentation.

## 4. Coding-Agent Error Taxonomy

| Error Class | Description | GT Help Strength |
|-------------|-------------|-----------------|
| Wrong localization | Agent edits the wrong file/function | **Strong** — localization state + graph-guided candidates |
| Premature commitment | Agent locks into an approach too early | **Moderate** — confidence gating prevents over-steering |
| Incomplete fix | Agent patches one location, misses related ones | **Strong** — coupling/completeness state |
| Missed semantic contract | Agent breaks a behavioral invariant | **Strong** — contract extraction from caller usage |
| Downstream regression | Fix breaks callers/dependents | **Strong** — post-edit validation + obligation checking |
| Context/search drift | Agent wanders away from relevant code | **Moderate** — localization anchoring |
| Weak validation | Agent doesn't verify the fix adequately | **Strong** — test evidence + critique |
| Infra/runtime failure | Docker, API, timeout issues | **Weak** — not GT's domain |

## 5. Highest-Leverage Evidence Families for Flips

Ranked by proven or expected flip potential:

1. **SIBLING/CONSISTENCY** — proven flip (13236). "7/8 siblings return np.ndarray, this one returns NdarrayMixin."
2. **OBLIGATION/CONTRACT** — highest theoretical value. "3 callers pass return to np.dot() — must remain 2D."
3. **COMPLETENESS/COUPLING** — "changed _cstack, should also check _cdot (same pattern)."
4. **CRITIQUE/VALIDATION** — "added required param; 5 callers break."
5. **CALLER (specific)** — "destructures return value at line 42."
6. **TEST (specific)** — "assert_allclose(result, np.array([[1,1],[0,0]]))"

Low-leverage (context only): IMPACT (generic count), PRECEDENT (history).

## 6. Research Basis

| Source | Key Finding | v6-smoke Application |
|--------|------------|---------------------|
| SWE-bench-Live (OpenReview) | Localization and resolution are separate problems | Localization state as explicit GT core |
| Agentless (HuggingFace) | Localize → repair → validate pipeline is competitive | GT provides structured localize + validate |
| Think-Search-Patch (ACL EMNLP) | Issue analysis → search → narrow → patch | GT's confidence-gated localization follows this |
| LocAgent (ACL 2025) | Graph-guided localization improves resolution | GT's graph.db + import edges enable this |
| CoSIL (HuggingFace) | Graph search + pruning helps localization | GT's admissibility gate + confidence tiers |
| Debug2Fix (Microsoft) | Non-LLM tooling improves coding agents | GT is deterministic tooling, not another LLM |
| Anthropic Claude Code | Explore first, keep context disciplined | GT's thin output + confidence gating |
| OpenAI Codex | Code understanding + dependency tracing | GT's call graph + import resolution |
| Invalidator (SMU) | Richer validation beyond test-pass | GT's post-edit critique + obligation checking |

## 7. In Scope

- Strengthen sibling/consistency evidence (proven flip family)
- Add return-shape contracts from caller usage patterns
- Add completeness/coupling warnings for peer functions
- Improve post-edit validation with new families
- Confidence-gated delivery (verified/likely/possible tiers)
- Smoke test on representative tasks
- Engineering documentation

## 8. Out of Scope

- Full 300/500 task benchmark
- New baseline runs
- SWE-agent scaffold redesign
- New MCP tools
- AI-generated evidence (GT stays deterministic)
- Domain-specific knowledge (astropy astronomy, Django ORM, etc.)

## 9. Success Criteria

1. Sibling evidence shows specific patterns, not just counts
2. At least one contract-bearing obligation fires on smoke tasks
3. At least one completeness warning fires when peer functions share patterns
4. Post-edit validation uses improved families
5. Confidence gating prevents v3-style regressions
6. Smoke traces show intended GT usage
7. One or more tasks show stronger flip conditions than v5

## 10. Risks

1. **Specificity vs noise**: more specific evidence could be wrong-specific
2. **Contract false positives**: caller usage classification may misidentify patterns
3. **Completeness spam**: peer warnings could flood if threshold is too low
4. **Stochasticity**: LLM behavior varies run-to-run; single smoke ≠ proof
