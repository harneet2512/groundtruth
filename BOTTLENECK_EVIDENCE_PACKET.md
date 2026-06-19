# Bottleneck Evidence Packet

## Selected Bottleneck: Assertion Target Linking (target_node_id = 0)

---

## 1. Source Citations

| Claim | Source file | Exact section / line / quote | Why it matters |
|---|---|---|---|
| 16,971 assertions extracted but never linked | decisions.md line 313 | "16,971 assertions already extracted across 5 test repos — the data EXISTS, just not linked" | Infrastructure exists, single link is missing |
| target_node_id always 0 | decisions.md line 285 | "target_id resolution fails for all... target_node_id is ALWAYS 0. Never populated in cmd/gt-index/main.go" | Go indexer never writes the link |
| 2/2 failed tasks are contract failures | decisions.md line 314 | "2/2 failed tasks are contract failures — cfn-lint-3821 and loguru-1306: agent found correct files, made semantically wrong fixes because no behavioral contract" | This IS the flip bottleneck |
| Minimum viable contract = 1 caller code line | decisions.md line 315 | "Minimum viable contract = 1 caller code line — not full test assertion" | But assertions add BEHAVIORAL SPECIFICATION |
| ~150 LOC Go fix | decisions.md line 316 | "Fix = ~150 LOC in Go — implement LCBA + naming convention resolution (test_X → X, TestX → X)" | Bounded scope |
| L3 consumer already queries assertions | jedi_WORK.md line 311-313 | Session 4: "assertions table exists with target_node_id column in schema, _get_test_assertions_from_graph in post_edit.py queries it" | End-to-end pipeline 90% wired |
| Post_edit.py lines 720-737 | src/groundtruth/hooks/post_edit.py:720-737 | `_get_test_assertions_from_graph` function exists, queries assertions table, returns empty because target_node_id=0 | Python consumer ready, Go producer broken |
| Go indexer assertion extraction site | gt-index/cmd/gt-index/main.go | `resolveAssertionTarget()` function added this session (~150 LOC) with 3 strategies: LCBA, naming convention, same-module match | Code EXISTS but untested against real graphs |
| Run proof: 0 test assertions reach agent | reports/SYSTEM_PROOF_2026_05_16.md line 66 | "assertion_target_linked_rate: 0% [DATA: all target_node_id=0]" | Measured on deployed graphs |

---

## 2. Current Violation

**Decision 1 (line 49):** "Test assertions (bonus only when available — NOT relied upon, NOT benchmaxxing)"

**Decision 13 (line 346):** "Assertion values > test pointers — 'assert get_user(99) raises KeyError' not 'test_get_user references get_user'"

**Current behavior:** `_get_test_assertions_from_graph()` in post_edit.py (line 720) queries the assertions table. The table has 16,971 rows across 5 repos. All have `target_node_id=0`. The function returns empty list → agent never sees test assertions → L3 evidence lacks behavioral contracts.

**The violation:** The architecture says L3 should show assertion values (Priority 4 in Decision 1). The infrastructure to deliver them is built end-to-end. The single broken link is in the Go indexer: it extracts assertions but never resolves which production function they test.

---

## 3. Flip Hypothesis

**How this could create positive flips:**

The 2 BOTH_FAIL tasks (cfn-lint-3821, loguru-1306) both fail because the agent makes a semantically wrong fix despite being in the right file. If L3 showed: "TEST: test_validate asserts validate(bad_input) raises ValueError" — the agent would have a concrete behavioral specification to code against, rather than guessing what the correct behavior should be from reading the function body alone.

This is information the agent CANNOT obtain by reading files one at a time because:
- Test files are often in different directories
- Test assertions reference production functions by name, not by explicit import path
- The agent would need to: find the test file → read it → identify which assertion targets which function → extract the expected value. That's 3-4 actions minimum. GT can provide it in 0 agent actions.

**Expected flip mechanism:**
1. Agent edits function X
2. L3 fires, queries assertions WHERE target_node_id matches X
3. L3 shows: "TEST: test_X asserts X(input) == expected_output"
4. Agent has concrete specification → fixes to satisfy assertion
5. Fix is correct → task resolves → positive flip

**Why this might NOT produce flips:**
- cfn-lint-3821 has 0 graph edges of ANY kind → assertions also unlikely to exist
- loguru-1306 has 1678 callers but may have 0 test assertions targeting the gold function
- DeepSeek V4 Flash may not reliably use assertion evidence even when shown

---

## 4. Research Basis

| Source | Finding | Applicability |
|---|---|---|
| Rompaey & Demeyer (TSE 2009) | Naming convention traceability achieves 90%+ recall for test_X→X patterns | Go implementation uses this as Strategy 2 |
| AutoCodeRover (ISSTA 2024) | Structured context with assertions reduces false starts by providing specification | Exactly the mechanism: assertion = specification |
| Agentless (ICLR 2025) | Validates patches via syntax+regression; no test dependency for PRIMARY filtering | Assertions are bonus evidence, not primary |
| RepoGraph (ICLR 2025) | k-hop ego-graphs with def/ref edges; edge QUALITY > edge DIVERSITY | Assertion links are high-quality edges (naming convention = 0.9 confidence) |
| LCBA (Last-Call-Before-Assert) | Extract function name from assertion expression to identify target | Go implementation uses this as Strategy 1 |

---

## 5. Generalized Implementation

**Repo-agnostic:** Naming convention (test_X→X, TestX→X) works across Python, Go, Java, Rust, JS/TS. Not framework-specific.

**Model-agnostic:** Assertion text is raw code shown in L3 evidence. Any model that reads code benefits.

**Tool-agnostic:** Output goes through existing L3 observation augmentation, not a specific tool.

**Scale-agnostic:** Resolution is O(assertions × functions_in_same_module). Bounded by graph size, not repo size.

**Language-agnostic:** Tree-sitter extracts assertions generically. Resolution strategies are syntactic (naming convention, LCBA expression parsing).

**Benchmark-agnostic:** No task IDs, no gold files, no FAIL_TO_PASS labels in the resolution logic.

---

## 6. Metrics to Move

| Metric | Bucket | Baseline | Target direction | Failure condition | Rollback trigger |
|---|---|:---:|---|---|---|
| assertion_target_linked_rate | GRAPH QUALITY | 0% (all target_node_id=0) | >0% (any linked) | Still 0% after fix | Revert Go changes |
| l3_assertion_evidence_shown | CONTRACT EVIDENCE | 0 (never fires) | >0 on tasks with test files | Still 0 | Check _get_test_assertions_from_graph |
| positive_flip_count | CAUSAL / OUTCOME | 0 | >0 (any flip) | 0 after diverse validation | Assertions not the lever |
| l3_evidence_precision | SAFETY / REGRESSION | N/A | No false assertions shown | False assertion shown | Disable assertion display |

---

## 7. Files Allowed

- `gt-index/cmd/gt-index/main.go` (assertion target resolution — already has `resolveAssertionTarget()`)
- `gt-index/internal/store/sqlite.go` (if schema update needed for assertion storage)

---

## 8. Files Forbidden

- `scripts/swebench/oh_gt_full_wrapper.py` (no wrapper changes)
- `src/groundtruth/hooks/post_edit.py` (consumer already works, do not modify)
- `src/groundtruth/pretask/v1r_brief.py` (localization unrelated)
- Any benchmark-specific file
- Any threshold or weight file

---

## 9. Rollback Rule

**Trigger:** If after Go binary rebuild + reindex:
- assertion_target_linked_rate is still 0% → implementation bug, investigate
- l3 shows false assertions (wrong function targeted) → disable assertion display, revert Go
- negative flip appears on diverse validation → revert Go changes
- action_count increases → assertion evidence flooding, cap tokens

**Rollback command:** `git revert <commit>` on the Go changes only. Python consumer gracefully handles 0 results (already tested).
