# Hypothesis Isolation Plan

Date: 2026-05-16
Purpose: Define isolated causal hypotheses before any graph-creation implementation.
Constraint: This is research design, not feature engineering.

---

## 0. Agent Decision Stages (6-stage model)

Before testing hypotheses, we decompose the agent's coding process into stages. Each hypothesis targets a specific stage failure.

| Stage | Name | What Agent Does | Observable |
|-------|------|-----------------|-----------|
| S1 | File discovery | Find which file(s) to touch | First file opened/read |
| S2 | Region localization | Find which function/class within the file | First edit location within file |
| S3 | Dependency localization | Understand what other code depends on this | Reads of connected files, import tracing |
| S4 | Contract understanding | Know what behavior callers/tests EXPECT | Whether fix respects behavioral contracts |
| S5 | Semantic fix selection | Write a fix that satisfies the contract | Whether patch is semantically correct |
| S6 | Verification correctness | Confirm fix works via the RIGHT test | Whether agent runs FAIL_TO_PASS test |

### 5-Task Stage Failure Matrix (observed)

| Task | S1 | S2 | S3 | S4 | S5 | S6 | Resolved |
|------|----|----|----|----|----|----|----------|
| beancount-931 | OK | OK | OK | N/A | OK | OK | YES |
| beets-5495 | OK | OK | PARTIAL | OK | OK | OK | YES |
| xarray-9760 | SLOW | SLOW | OK | OK | OK | OK | YES |
| cfn-lint-3821 | OK | OK | ? | **FAIL** | **FAIL** | INC | NO |
| loguru-1306 | OK | OK | PARTIAL | **FAIL** | **FAIL** | **FAIL** | NO |

**Causal bottleneck by stage:**
- S1+S2: Bottleneck for SPEED (1/5 tasks slow — large repo)
- S3: Partially involved in 3/5 tasks (unclear causal role)
- **S4+S5+S6: Bottleneck for RESOLUTION (2/2 failures, 100% correlation)**

---

## 1. Hypothesis Inventory

### H1: Speculative edge suppression prevents noise-based decisions

**Claim:** Suppressing confidence < 0.7 edges from consumer queries eliminates noise that inflates irrelevant files/functions in rankings and suggestions.

**Stage targeted:** S1, S2 (file/region discovery — noise pollution)

**Causal mechanism:** If noise edges inflate documentation/utility files in rankings, suppressing them should surface correct files instead.

---

### H2: Caller code extraction at call sites prevents wrong semantic fixes

**Claim:** Showing the agent "how callers actually use this function" (1-3 lines of source at each call site) prevents fixes that satisfy syntax but violate behavioral contracts.

**Stage targeted:** S4 (contract understanding), S5 (semantic fix)

**Causal mechanism:** The agent cannot violate a contract it can see. If the graph stores actual call-site code (not just "file X calls function Y"), the agent can read what arguments callers pass and what return values they expect.

---

### H3: Test-to-source edges with assertion text improve verification correctness

**Claim:** Showing the agent "test T at line L asserts behavior B of function F" causes the agent to run THAT specific test instead of broad/irrelevant test suites.

**Stage targeted:** S6 (verification correctness), S4 (contract understanding)

**Causal mechanism:** The agent runs broad tests because it doesn't know which specific test covers the edited function. If the graph contains TEST_ASSERTS_SYMBOL edges with actual assertion text, the agent can target the relevant test.

---

### H4: Structural precision mainly accelerates large-repo navigation

**Claim:** Better structural edges (cross-file CALLS, verified imports) reduce search time on repos > 5000 files but do not causally contribute to fix correctness.

**Stage targeted:** S1, S2 (file/region discovery on large repos)

**Causal mechanism:** On large repos, the search space is too large for grep alone. Graph edges narrow the candidate set. On small repos (< 500 files), grep suffices and graph edges are irrelevant.

---

### H5: Consumer safety (tier-gating) increases GT suggestion follow-rate

**Claim:** If an agent learns that GT suggestions are unreliable (because some were noise), it ignores ALL suggestions. Removing noise from suggestion paths increases trust and follow-rate.

**Stage targeted:** All stages where GT injects information (S3, S4 primarily)

**Causal mechanism:** Agent trust is binary — if early suggestions are wrong, agent ignores later correct ones. Suppressing noise makes remaining suggestions uniformly high-quality, rebuilding trust.

---

## 2. Per-Hypothesis Analysis

### H1: Speculative Edge Suppression

**Intervention:**
- Add `AND e.confidence >= 0.7` to 4 unfiltered V1R queries
- Raise graph_reach min_confidence from 0.5 to 0.7
- (5 one-line SQL changes in v1r_brief.py)

**Isolated metrics:**
- `v1r_top5_file_change`: Do the top-5 ranked files change after the filter?
- `v1r_noise_file_in_top5`: Does any file with >50% low-confidence inbound edges appear in top-5?
- `l1_gold_file_in_candidates`: Does the actual fix file appear in V1R output? (benchmark-secondary metric)
- `v1r_function_ranking_change`: Do the top-3 functions per file change?

**Expected directional effect:**
- Documentation/utility files drop from V1R rankings: HIGH confidence
- Actual code hubs rise: HIGH confidence  
- Agent reads V1R-suggested file first: UNCERTAIN (agent may still grep independently)
- Resolution improves: LOW confidence (S1/S2 are not the failure bottleneck on current tasks)

**Expected regression risk:** VERY LOW
- Cannot make things worse (0/5 gold files in V1R currently)
- Same-file and import-verified edges (the useful ones) are all conf >= 1.0 — unaffected
- Single-candidate name_match (conf=0.9) survives — unaffected

**Confounders:**
- If agent always greps anyway (ignores V1R), then cleaner V1R doesn't change behavior
- If gold file is unreachable even via certified edges (import coverage gap), filter doesn't help
- BM25 component of V1R scoring is unaffected by this change

**Rollback criteria:**
- V1R produces empty brief on > 1/5 tasks (insufficient certified edges for content)
- Resolution drops below 3/5 on smoke tasks

---

### H2: Caller Code Extraction

**Intervention:**
- When emitting caller evidence for an edited function, read source file at `edge.source_line` and include 1-3 lines
- Store in `evidence_payload.source_code_snippet` field
- Surface in evidence output: "CALLER: test_rule.py:50 → validate_open_close([valid_entry]) == []"

**Isolated metrics:**
- `caller_evidence_has_code`: Fraction of caller edges where actual source line was read and stored
- `caller_code_line_count`: Number of caller code lines surfaced to agent (currently: 0 on 4/5 tasks)
- `agent_follows_caller_with_code_vs_without`: Follow rate for callers WITH source snippet vs without
- `semantic_fix_correctness_with_caller_code`: Does the fix respect the contract visible in caller code?

**Expected directional effect:**
- Agent sees "assert result == []" → knows function must return empty list for valid input: MEDIUM-HIGH confidence
- Agent follows caller suggestions more often: MEDIUM confidence (xarray showed 100% follow when caller code present, N=3)
- Resolution improves on S4-failure tasks: MEDIUM confidence (N=2 failures, both S4-bottlenecked)

**Expected regression risk:** LOW
- Adding information to evidence cannot break existing behavior
- Risk: context bloat if too many caller lines are shown (mitigation: cap at 3 callers, 1 line each)

**Confounders:**
- Agent may not read/use the caller code even if shown (attention/prompt position)
- Caller code may be at wrong abstraction level (test helper calls the function via wrapper — not directly informative)
- WHICH callers are shown matters — if graph picks noise callers (speculative edges), code is from wrong file
- **DEPENDENCY ON H1:** If speculative edges are still used to find callers, caller code comes from wrong files

**Rollback criteria:**
- Follow rate decreases vs baseline (suggests caller code is confusing not helping)
- Token budget exceeded (caller code bloats evidence beyond condenser window)

---

### H3: Test-to-Source Edges with Assertion Text

**Intervention:**
- New edge type: TEST_ASSERTS_SYMBOL
- Builder: for each node where is_test=1, trace its calls to non-test nodes → create edge
- Evidence payload: read assertion lines from the test function (assert statements, assertEqual calls)
- Surface: "TEST: test_format_exception asserts traceback header is present in output"

**Isolated metrics:**
- `test_edge_count`: Number of TEST_ASSERTS_SYMBOL edges produced
- `test_edge_precision`: Fraction of test edges where the test actually tests the claimed function (sampled)
- `agent_ran_specific_test_after_hint`: Did the agent run the TEST from the edge after seeing it?
- `agent_verification_targeted_vs_broad`: Ratio of targeted test runs to broad `pytest .` runs
- `verification_stage_failure_rate`: S6 failure rate before/after (currently: 2/5 = 40%)

**Expected directional effect:**
- Agent runs specific failing test instead of broad suite: MEDIUM confidence
- S6 failure rate decreases: MEDIUM confidence (addresses loguru exactly)
- S4 understanding improves (assertion text IS a behavioral contract): MEDIUM confidence

**Expected regression risk:** LOW-MEDIUM
- New edge type adds data — doesn't remove anything
- Risk: wrong test suggested (if graph connects test to wrong function) → agent wastes iteration
- Risk: test edge precision is low because tests call many functions (graph says test T tests function F, but T tests functions A-Z)

**Confounders:**
- Agent may ignore test hints (already ignores 70-75% of GT suggestions)
- The FAILING test from eval metadata may not be in the graph (test added post-commit)
- is_test=1 tagging accuracy (untested on these repos)
- **DEPENDENCY ON H1:** If test edges are discovered via speculative CALLS edges, they inherit noise

**Rollback criteria:**
- test_edge_precision < 0.50 (more wrong than right)
- Agent verification behavior doesn't change measurably

---

### H4: Structural Precision Mainly Accelerates Large Repos

**Intervention:** None (this is a MEASUREMENT hypothesis, not an implementation)

**Isolated metrics:**
- `repo_size_vs_resolution`: Correlation between repo file count and resolution rate
- `gt_contribution_vs_repo_size`: Does GT follow-rate increase with repo size?
- `agent_search_actions_vs_repo_size`: Do agents search more in larger repos?
- `speed_to_first_correct_file_vs_repo_size`: Iterations to first correct file read

**Expected directional effect:**
- Larger repos → more searching → more opportunity for GT structural edges: HIGH confidence
- Smaller repos → agent greps successfully → GT structural edges add nothing: HIGH confidence
- This is confirmatory, not interventional

**Confounders:**
- Task difficulty is confounded with repo size (larger repos may have harder bugs)
- Agent model capability varies (stronger models need less navigation help)
- Issue text quality varies (clear issue descriptions → easy grep regardless of repo size)

**No rollback needed** — measurement only.

---

### H5: Consumer Safety Increases Follow-Rate

**Intervention:**
- Implement tier-gated query layer: consumers declare minimum trust
- GT suggestions come ONLY from CERTIFIED tier
- Remove all SPECULATIVE edges from suggestion paths

**Isolated metrics:**
- `suggestion_noise_rate_before`: Fraction of GT suggestions based on speculative edges (pre-fix)
- `suggestion_noise_rate_after`: Same, post-fix (should be 0)
- `follow_rate_before_vs_after`: Overall GT suggestion follow rate
- `follow_rate_first_3_suggestions`: Follow rate for first 3 GT suggestions specifically (trust-building)
- `agent_ignores_gt_entirely_rate`: Fraction of tasks where agent follows 0/N suggestions

**Expected directional effect:**
- Noise suggestions eliminated → remaining suggestions are correct → follow rate increases: MEDIUM confidence
- First-suggestion quality determines trust: HIGH confidence (behavioral economics: first impression)
- Resolution improves: LOW confidence (follow-rate improvement → resolution only if suggestions are actionable)

**Expected regression risk:** MEDIUM
- If CERTIFIED edges are sparse (insufficient coverage), agent gets fewer suggestions total
- On repos with very low import resolution (TS/Rust), CERTIFIED may be only same_file edges → nearly useless for cross-file navigation

**Confounders:**
- Follow-rate is confounded with suggestion CONTENT quality (not just trust)
- Agent model may have already learned to ignore GT (from prior runs with noise)
- **INDEPENDENT of H2/H3:** Follow-rate for structural suggestions is different from follow-rate for behavioral suggestions

**Rollback criteria:**
- Total suggestions per task drops below 2 (insufficient coverage)
- Follow-rate does not increase within 3 tasks

---

## 3. Dependency Map

```
H1 (speculative suppression) ─────────┐
     │                                 │
     │ REQUIRED BEFORE                 │ REQUIRED BEFORE
     │                                 │
     ▼                                 ▼
H2 (caller code extraction)     H3 (test-to-source edges)
     │                                 │
     │ INDEPENDENT                     │ INDEPENDENT
     │                                 │
     ▼                                 ▼
H5 (consumer safety)            H5 (consumer safety)
```

**H4 is measurement-only — no dependencies, can run anytime.**

**Critical dependency:** H2 and H3 both depend on H1.
- H2 extracts caller code from edges — if those edges are speculative (wrong file), the code is from the wrong caller
- H3 discovers test edges via CALLS edges — if those CALLS are speculative, tests connect to wrong functions

**H5 depends on H1 + (H2 or H3):**
- Consumer safety requires tier classification (which H1 establishes)
- Follow-rate measurement requires something worth following (which H2/H3 provide)

**H1 is INDEPENDENT of everything — it's a pure filter.**

---

## 4. Minimal First Implementation

### Selection Criteria

| Criterion | H1 | H2 | H3 | H5 |
|-----------|----|----|----|----|
| Expected value for resolution | LOW (not failure bottleneck) | MEDIUM-HIGH | MEDIUM-HIGH | LOW |
| Coupling risk | NONE (pure filter) | LOW (reads files) | MEDIUM (new edge type) | MEDIUM (query layer) |
| Regression probability | VERY LOW | LOW | LOW-MEDIUM | MEDIUM |
| Implementation size | 5 SQL lines | ~50 LOC (file reader) | ~200 LOC (builder + schema) | ~100 LOC (query wrapper) |
| Dependency on other H | NONE | Depends on H1 | Depends on H1 | Depends on H1 |
| Measurement clarity | VERY HIGH (before/after ranking) | HIGH (follow-rate with/without code) | HIGH (verification behavior) | MEDIUM (confounded) |

### Decision: H1 First

**H1 (speculative edge suppression) is the minimal first intervention because:**
1. Zero dependencies — can ship alone
2. Zero coupling risk — pure query-side filter
3. Very low regression probability — removes noise, keeps all valid edges
4. Required before H2/H3 can be meaningful
5. Measurement is immediate and unambiguous (does ranking change?)
6. Addresses a clear metric deficiency (0/5 gold-file-in-candidates)

**However, H1's expected value for RESOLUTION is LOW** because the failure bottleneck is S4+S6 (contract + verification), not S1+S2 (file discovery). H1 is necessary infrastructure but not the highest-value intervention.

### Decision: H3 Second (or H2, but H3 is more isolated)

After H1, the choice between H2 (caller code) and H3 (test edges) depends on isolation:
- H3 produces a NEW edge type → measurement is clean (0 test edges before, N after)
- H2 modifies EXISTING evidence content → measurement requires A/B (harder to isolate)
- H3 directly addresses S6 (verification — 2/2 failures have wrong-test pattern)
- H2 addresses S4 (contract — 2/2 failures, but effect is less direct)

**H3 is preferred second because it's a new observable phenomenon (test edges), not a refinement of existing one (caller code).**

---

## 5. Experimental Ordering

### Experiment 1: H1 — Speculative Edge Suppression

**Scope:** 5 SQL clauses in v1r_brief.py
**Duration:** Implement + measure in one session
**Validation:**
1. Generate V1R brief for dagster-33645 BEFORE and AFTER
2. Compare top-5 file rankings
3. Check: did documentation/example files drop?
4. Check: did core module files rise?
5. (Optional) If local graph.db exists for a 5-task repo: check l1_gold_file_in_candidates

**Success metric:** V1R ranking measurably changes (different files in top-5)
**Decision gate:** If ranking doesn't change → H1 is addressing wrong layer → investigate further

---

### Experiment 2: H4 — Measurement (concurrent with Exp 1)

**Scope:** Compute repo-size vs resolution correlation on available data
**Duration:** One analysis pass
**Validation:**
1. For each of the 5 smoke tasks: compute repo file count
2. Plot: file count vs resolution, file count vs GT follow-rate, file count vs search actions
3. Confirm/reject: "GT structural help scales with repo size"

**Success metric:** Clear correlation (or clear non-correlation) with at least one observable
**Decision gate:** If GT helps only on large repos → focus structural precision on large-repo case only

---

### Experiment 3: H3 — Test-to-Source Edges

**Scope:** New edge type builder (requires schema change + builder code)
**Duration:** Multi-session (schema + builder + indexer + audit)
**Validation:**
1. Build TEST_ASSERTS_SYMBOL edges on dagster graph
2. Sample 50 test edges �� verify: does the test actually test the claimed function?
3. If precision > 0.70: deploy to smoke
4. Measure: does agent run suggested tests?

**Success metric:** test_edge_precision > 0.70 AND agent verification behavior changes
**Decision gate:** If precision < 0.50 → test-to-source via CALLS graph is unreliable → need coverage-based approach instead

---

### Experiment 4: H2 — Caller Code Extraction

**Scope:** Read source file at call site, include in evidence
**Duration:** Single session (small code change)
**Validation:**
1. Re-run L3 evidence generation with caller code lines included
2. Measure follow-rate for callers WITH code vs historical WITHOUT code
3. Check: does agent modify fix to respect visible contract?

**Success metric:** Follow-rate with caller code > follow-rate without caller code (within-task A/B)
**Decision gate:** If follow-rate unchanged → agent doesn't read/use caller code → investigate attention position

---

### Experiment 5: H5 — Consumer Safety (final)

**Scope:** Tier-gated query layer
**Duration:** Multi-session (after H1+H3 proven)
**Validation:**
1. Instrument GT to log trust-tier of every suggested edge
2. Compare follow-rate for CERTIFIED-only suggestions vs mixed suggestions
3. Check: does removing CANDIDATE edges from navigation hurt or help?

**Success metric:** Follow-rate increases when suggestions are tier-gated
**Decision gate:** If follow-rate unchanged → trust is not the mechanism → investigate content quality instead

---

## 6. What This Plan Does NOT Answer (unknowns requiring more data)

1. **Is Stage 3 (dependency localization) causally bottlenecking anything?** The data shows PARTIAL in 3/5 tasks but no clear causal link to failure. Need more failed tasks to determine.

2. **Is the 6-stage model complete?** There may be stages between S2 and S3 (e.g., "understand the bug mechanism" which requires reading the issue differently, not just graph navigation).

3. **Are contract failures (S4) really about missing INFORMATION or about agent REASONING?** The agent might have all the information and still write the wrong fix because its reasoning is flawed. Providing more information to a reasoning-limited agent may not help.

4. **Does N=5 generalize?** All conclusions are directional from 5 tasks. The 6-stage failure pattern needs validation on N >= 30 before committing major engineering effort to H2/H3.

5. **Is the agent's follow-rate a stable metric?** If different model versions follow GT at different rates regardless of quality, then optimizing for follow-rate is chasing a moving target.

---

## 7. Summary

The goal is NOT "make graph better." The goal is: **identify which information substrate causally changes coding-agent decisions.**

- **H1** (noise suppression) is necessary infrastructure but not the causal lever for resolution
- **H3** (test targeting) is the highest-expected-value hypothesis for resolution because it directly addresses the dominant failure mode (S6: wrong test)
- **H2** (caller contracts) is the second-highest because it addresses S4 (wrong semantics)
- **H4** (measurement) tells us WHERE structural precision matters (large repos only?)
- **H5** (trust) is downstream of having something worth trusting (requires H2/H3 first)

Implementation order: H1 → H4 (parallel) → H3 → H2 → H5

Each experiment runs independently. No experiment assumes the previous succeeded. Each has explicit rollback criteria and decision gates.
