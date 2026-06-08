# Ledger — aws-cloudformation__cfn-lint-3749  (run 27107841613, branch gt-trial, 2026-06-07)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-3749"]`), baseline_pass=**no** (NOT in `full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json` `resolved_ids`; baseline=NO, so this id is a flip-CANDIDATE), flip=**no** (not resolved → no flip). GOLD file = `src/cfnlint/template/transforms/_language_extensions.py` (gold adds module-global `_ACCOUNT_ID`, captures the matched mapping key when `t_map[1]` is a `_ForEachValueRef` to `AWS::AccountId`, and returns `_ACCOUNT_ID` instead of the hardcoded `account_id`).

**One-line trajectory finding (lead with this, per CLAUDE.md):** GT localized PERFECTLY — the gold file is L1 candidate #1, the agent opened it on turn 1, and consensus/post-view fired correctly on it. The task did NOT resolve because the agent wrote the **wrong fix logic** *inside the correctly-localized gold file*: it made `Fn::FindInMap` raise `_ResolveError` so the ForEach degrades to **synthetic random values**, which makes its own reproduction "pass" but does NOT make `AWS::AccountId` resolve to the real mapping key `12345678901` — so the gold's new FAIL_TO_PASS tests (`TestFindInMap::test_account_id`, `TestTransformValueAccountId::test_transform`) fail. This is a **post-localization implementation-correctness** miss, which a no-leakage context layer cannot determine. GT did its job (right file, right contracts, no harm, no leak) and the trajectory through GT's context was correct up to the edit decision; the edit itself was wrong.

---

## PREREQS (substrate, 8-dp verbatim from `gd/gt_gates_deep_aws-cloudformation__cfn-lint-3749.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 80.77228246` (deterministic_edges `3054.0` / calls_edges `3781.0`) | GREEN (`pred_A_det_floor=true`, floor 15.0) | telemetry-only; reached the agent ONLY as the brief's resolved-edge lines (e.g. IDX 1 `resolved caller: _get_new_status_message() in …exceptions.py:14`; IDX 11/13 `Called by: language_extension() _language_extensions.py:50`) |
| **P1** name_match | `name_match_edges = 727` (of 3781 → 19.22% name_match) | GREEN (`pred_B_nondominance=true`) | telemetry-only; never rendered as a number; only resolved (non-name_match) edges become brief "Called by:" / "Calls:" lines |
| **P1** typing tiers | `typing_fired=true`; `type_flow=229`, `impl_method=488`, `inherited=149`, `ev:assignment_tracked=204` | GREEN (`pred_C_typing=true`) | telemetry-only |
| **P2** calls_edges / resolution breakdown | `calls_edges=3781.0`; breakdown `name_match=727, same_file=604, import=595, verified_unique=540, impl_method=488, lsp=367, type_flow=229, inherited=149, return_type=78, unique_method=4` | GREEN (`gate_resolution.pass=true`) | telemetry-only; surfaces to the agent only as concrete resolved call/caller lines in L1 brief + post-view `[GT]`/`<gt-context>` headers |
| **P2** LSP enrichment | `gate_lsp`: `resolved=367.0`, `residual=1023.0`, `resolve_frac=0.35874878`, `scoped_source_files=0`, `scoped_degraded=true` | GREEN (`gate_lsp.pass=true`, floor 0.1) | telemetry-only; the 367 `lsp` edges are folded into the same resolved-edge lines |
| **P3** embedder present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN (`gate_embedder.present.pass=true`) | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15`, `semantic_signal_count=4`, `sem_max=0.84661600`, `sem_median=0.83557800`, `sem_mad=0.01103800`, `sem_separation_gap=0.01103800`, `sem_separation_threshold=0.01103800`, `sem_frac=0.50000000`, `k_mad=1.00000000` | GREEN (`mode=present_and_consumption`, `pass=true`; pred_1/2/3 all true) | telemetry-only; the semantic weight only re-orders the L1 candidate list the agent saw at IDX 1 |

**Prereqs verdict:** ALL GREEN (`verdict: {resolution_jarvis:true, lsp_enrichment:true, embedder:true, all_on:true}`). Substrate was healthy: 80.77% deterministic edges, name_match non-dominant (19%), typing fired, embedder live and consumed. **None of these numbers reached the agent as numbers** — they reached it only as the resolved-edge lines inside the L1 brief and post-view headers, which correctly pointed at the gold file. The substrate did not fail this task; localization was correct. The miss is downstream of every prereq.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 | `<gt-localization confidence="medium">` … `  1. src/cfnlint/template/transforms/_language_extensions.py — values, _ResolveError, _ValueError` … (candidate **#1 is the GOLD file**; 6 candidates total; followed by `<gt-task-brief>` ranking `transform.py` and `<gt-orientation>` listing `values() in _language_extensions.py (25 callers)`) | IDX 4: `[args.thought] I'll start by understanding the issue and exploring the codebase systematically.` → IDX 10/11: agent opens **`/workspace/aws-cloudformation__cfn-lint-3749/src/cfnlint/template/transforms/_language_extensions.py`** — the L1 #1 candidate / GOLD file — on its FIRST file read after listing the tree. | **D**elivered=Y · **C**orrect=Y (gold is candidate #1) · **C**onsumed=Y (agent opened gold file turn ~11, did not wander) |

**L1 verdict:** D/C/C = **Y/Y/Y**, leak=0. L1 put the gold file at rank #1 and the agent went straight to it. (Caveat, not a harm: the `<gt-task-brief>` + `<gt-graph-map>` sub-blocks ranked `transform.py` — a non-gold but graph-adjacent file — as the primary edit target; the agent ignored that sub-ranking and correctly followed the `<gt-localization>` #1 ordering to the gold file. No misdirection occurred.)

## L3b post-view (`<gt-context>` / `[GT]` file-view enrichment)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 11 | Prepended to the gold-file `cat -n` view: `[GT] _language_extensions:` / `[CONTRACT] def transform(self, cfn: Any) -> TransformResult:` / `[CONTRACT] def values( -> Iterator[str \| dict[Any, Any]]` / `[RAISES] WHEN not isinstance(obj, list): raise _TypeError("Fn::FindInMap should be a list", obj)` … (real signatures + raises from the gold file, correctly extracted). | IDX 16 (`[args.thought]`): agent reasons over the file body it just received — `Now let me understand the issue better. The problem is in _ForEachValueFnFindInMap.value(). When resolving Fn::FindInMap with a Ref to AWS::AccountId, the account ID 123456789012 doesn't match 12345678901 in the mapping, so it fails.` — i.e. it consumed the file content GT enriched. | **D**=Y · **C**=Y (real contracts, no fabrication) · **C**=Y (agent reasoned over the enriched view) |
| IDX 13 | `<gt-context file="_language_extensions.py">` block repeated (same `[CONTRACT]`/`[RAISES]` payload) on the second view attempt; followed by `ERROR: Invalid view_range parameter: [400, 600]…` (the error is the editor's, not GT's). | IDX 14: agent re-issues the read with a valid range; no misdirection from the `<gt-context>` header. | **D**=Y · **C**=Y · **C**=Y |
| IDX 37 | After re-viewing lines 110-195: `[GT] _language_extensions.py was confirmed earlier. Key evidence: [CONTRACT] def transform(self, cfn: Any) -> TransformResult: Scope: _language_extensions.py, _utils.py, transform.py` | IDX 36/38 (`[args.thought]`): agent has already pinned line 123 (`if re.match(FUNCTION_FOR_EACH, k)`) as a bug site; the `[GT]` confirmation header did not change its trajectory (neither helped nor harmed the actual logic decision). | **D**=Y · **C**=Y (accurate scope) · **C**=partial (read but not acted-on; harmless) |
| IDX 51 | On a later `value()`-region view: `[GT] _language_extensions:` + the same `[CONTRACT]`/`[RAISES]` header re-prepended (e.g. `[CONTRACT] def values( -> Iterator[str \| dict[Any, Any]]`). | IDX 52 (`[args.thought]`): agent deep-dives `_ForEachValueFnFindInMap.value()` line 380-382 and decides to raise `_ResolveError` — consuming the file body GT delivered, though its conclusion is the WRONG fix. | **D**=Y · **C**=Y (correct contracts) · **C**=Y (consumed; led to a wrong-but-not-GT-caused edit) |

**L3b post-view verdict:** D/C/C = **Y/Y/Y**, leak=0. Post-view consistently delivered the gold file's REAL contracts (`transform`, `values`, the `_TypeError`/`_ValueError`/`_ResolveError` raises) with no fabrication and no test leakage. The agent consumed the enriched views. The contracts were correct; they could not (and are not designed to) tell the agent which of two valid-looking branch fixes resolves the gold tests.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 11 | `<gt-scope files="3">` / `1. transforms/_language_extensions.py — in scope (you are viewing this)` / `2. conditions/_utils.py — imported` / `3. transforms/transform.py — shares transform` / `These files are related in scope; GT has not confirmed a single primary target — confirm the edit target with grep.` | Agent was already viewing the gold file (`you are viewing this`); it did NOT wander to `_utils.py` or `transform.py` — it stayed in `_language_extensions.py` for the entire fix. The honest "GT has not confirmed a single primary target" did not cause it to second-guess the correct file. | **D**=Y · **C**=Y (scope #1 = gold; correct-or-quiet honesty about no single target) · **C**=Y (agent stayed on gold) |

**consensus verdict:** D/C/C = **Y/Y/Y**, leak=0. The `<gt-scope>` correctly listed the gold file first/in-scope and explicitly abstained from over-claiming a single primary target (correct-or-quiet, per the pillar). The agent neither got misdirected to the two adjacent files nor abandoned the gold file. Consensus behaved exactly as designed and contributed to the correct localization trajectory.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 47 | Appended after the agent's first edit + a re-run: `[GT_VERIFY] You edited 1 file(s). Before finishing, run the project's own test suite for the affected modules and confirm your change preserves the behavioral contract:` / `  _language_extensions.py: exception_type = _ValueError` / `  _language_extensions.py: exception_type = _TypeError` / `  _language_extensions.py: exception_type = _ResolveError` / `  _language_extensions.py: guard_clause = raise: isinstance(obj, dict) -> if params:` | IDX 64-99: agent DID run the project test suite as instructed — `python -m pytest test/unit/module/template/transforms/test_language_extensions.py` (IDX 70-71: **23 passed**), `test_for_each.py` (2 passed), `test_find_in_map.py` (11 passed), `…/transforms/ …/functions/` (IDX 99: **209 passed**). It complied with GT_VERIFY's instruction. | **D**=Y · **C**=Y (correct affected-module list + real exception contracts; no test names leaked) · **C**=Y (agent ran the suite) — BUT the local suite did NOT contain the gold's new `test_account_id`/`TestTransformValueAccountId::test_transform` (the eval injects those later via the gold test_patch), so "209 passed" gave the agent false confidence. |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. GT_VERIFY correctly named the affected module and its real behavioral contracts, surfaced **no** FAIL_TO_PASS test names (it listed exception *types* and a *guard_clause*, not test identifiers), and the agent complied by running the suite. The failure mode is NOT a GT_VERIFY defect: the workspace test file predates the gold test_patch, so the very tests that would have caught the wrong fix (`test_account_id`) were absent from the agent's environment — GT cannot inject hidden gold tests (that would be leakage). GT stayed correct-or-quiet.

## L4 / L5 / L5b / L6

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| — | DELIVERED=NO — no L4 (no `gt_l4_tools` invocation in history; the agent's one `gt_validate unknown` at IDX 104-105 returned `# gt_validate: unknown` / `(file not in worktree … nothing to validate)` — a no-op, not an L4 evidence delivery). No L5/L5b/L6 markers appear anywhere in the 111-turn history (scanned full chronologically: no `GT_META`, no `[GT_CURATION]`, no `dedup=`, no L5/L6 payload). | n/a | n/a — not delivered |

**L4/L5/L5b/L6 verdict:** DELIVERED=NO for all four; nothing to consume, leak=0. (The lone `gt_validate unknown` call was the agent passing a literal `unknown` arg and got a clean no-op back — no evidence, no harm.)

---

## Cross-component line

leakage=**0** · delivered components=**4** (L1, L3b post-view, consensus `<gt-scope>`, L3/GT_VERIFY) · consumed=**4** (all four reached and were acted on by the agent; the gold file was opened turn 1, contracts read, scope respected, verify-suite run) · fair-probe=**FAIR** (GT pre-named the gold file as candidate #1 via the localizer, but the agent independently opened, reasoned over, and edited it — and the non-resolution is a post-localization implementation-logic error inside the correctly-named file, NOT a probe that pre-fed the answer; the gold's actual fix — the `_ACCOUNT_ID` global — was NOT surfaced by any GT component, so GT did not leak the solution).
