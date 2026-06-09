# Ledger — aws-cloudformation__cfn-lint-3866  (run, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` unresolved), baseline_pass=**no** (flip-candidate), flip=**no**. GOLD = `src/cfnlint/template/transforms/_language_extensions.py` → `_ForEachValueFnFindInMap.value()`: `value = mapping.get(t_map[1]…).get(t_map[2]…); if value is None: raise _ResolveError("Can't resolve Fn::FindInMap", self._obj); return value`. FAIL_TO_PASS = `TestFindInMap::test_find_in_map_values_not_found_with_default`, which asserts BOTH `map.value(cfn, None, False, True) == "bar"` (default path) **AND** `map.value(cfn, None, False, False) == ["foo", "bar"]` (the no-default second-level-keys list fallback).

**One-line trajectory finding:** GT localized **CORRECTLY** — L1 ranked the gold file `_language_extensions.py` at **rank #1** and named the gold method `value` with the exact `raises _ResolveError` contract; the agent opened it as its FIRST source read (IDX 12) and post-view (IDX 13) delivered the real `value`/`transform` contracts. The agent's edit landed in the gold function and got the **first half of gold right** (`if result is None: raise _ResolveError`). It did NOT resolve because the agent's fix is **incomplete vs gold's intent**: the FAIL_TO_PASS requires the `default_on_resolver_failure=False` branch to fall through to the second-level `mapping.items()` loop and return `["foo","bar"]` — the agent only added the `raise` and did not preserve/route the list-return path, so the second assertion fails. (This is exactly why the upstream fix is split across PR 3866 + PR 3875.) **Right file, right contract delivered + consumed; the edit was logically incomplete — a post-localization correctness miss.**

right_trajectory = **partial → FALSE on outcome** (correct context delivered + consumed + reasoned through; the edit decision itself was incomplete, so the fix is not "correct FOR THAT REASON") · L1-ranked-gold = **rank 1, gold method named** · agent-reached-gold = **YES (GT-driven, first source read)** · failure locus = **agent incomplete-fix-logic (post-localization)**

---

## PREREQS (substrate, 8-dp verbatim from `gt_gates_deep_…-3866.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 76.18500431` (det `2652.0` / calls `3481.0`) | GREEN | brief resolved-edge lines only |
| **P1** name_match | `name_match = 829` (23.81%) | GREEN (non-dominant) | telemetry-only |
| **P1** typing tiers | `type_flow=227`, `impl_method=259`, `inherited=150`, `ev:assignment_tracked=202`; `typing_fired=true` | GREEN | telemetry-only |
| **P2** calls / breakdown | `calls=3481.0`; `name_match=829, same_file=619, import=607, verified_unique=547, impl_method=259, type_flow=227, lsp=168, inherited=150, return_type=74, unique_method=1` | GREEN | resolved-edge lines in L1 + post-view |
| **P2** LSP | `verdict=LSP_ACTIVE_VALID`, `resolved_promoted=168.0`, `attempted=236.0`, `graph_lsp_edges=168` | GREEN | telemetry-only |
| **P3** embedder present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15000000` | GREEN (`mode=present_and_consumption`) | re-orders L1 list only |

**Prereqs verdict:** ALL GREEN (`all_on:true`). Substrate healthy; here it also produced a CORRECT L1 ranking (gold at #1). Not the cause of the miss.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 | `<gt-localization confidence="low">` / `Region: src/cfnlint/ — candidate edit targets …` / `  1. src/cfnlint/template/transforms/_language_extensions.py` … then `<gt-task-brief>` #1 = `_language_extensions.py (def create…, def language_extension…, def value()` with `Contract: raises _ResolveError | … raise: not isinstance(v, str) -> raise _ResolveError("Can't resolve Fn::Ref"…) | returns value|self._value …` and `EDIT-TARGET CONTRACTS (_language_extensions.py): value -> calls create(obj: Any)`. **Gold file = rank #1; gold method `value` explicitly named with the `_ResolveError` contract.** | IDX 12 (think): `Now let me look at the main file mentioned in the issue - _language_extensions.py:` → opens the GOLD file as its first source read (after listing the dir tree at IDX 10-11). | **D**=Y · **C**=Y (gold = rank #1, gold method named) · **C**=Y (agent opened gold file first) |

**L1 verdict:** D/C/C = **Y/Y/Y**, leak=0. L1 put the gold file at rank #1 and named the gold method `value` with the relevant `_ResolveError` contract. The agent went straight there. (Confidence label was "low" yet the ranking was correct — honest under-claim, no harm.)

## L3b post-view (`[GT]` / `<gt-context>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 13 | Prepended to the gold-file view: `[GT] _language_extensions:` / `[CONTRACT] def transform(self, cfn: Any) -> TransformResult:` / `[CONTRACT] def value(` / `[CONTRACT] def value( -> Any` / `flows: params -> params is None | … t_map[1].value(cfn, params, only_params)` / `Called by: language_extension() _language_extensions.py:52`. (Real signatures + flows of the gold method, correctly extracted.) | IDX 18 (think): `Now I see the issue. In the _walk method … when handling Fn::FindInMap, it calls mapping.value(cfn, params, True, False). The default_on_resolver_failure=False …` — reasons over the exact `value()` flow GT surfaced. IDX 30: deep-dives `_ForEachValueFnFindInMap.value()`. | **D**=Y · **C**=Y (real contracts, no fabrication) · **C**=Y (consumed; drove the agent to the correct method) |

**L3b post-view verdict:** D/C/C = **Y/Y/Y**, leak=0. Post-view delivered the gold method's real `value()`/`transform()` contracts and the `default_on_resolver_failure` flow the agent reasoned through. The context was correct and consumed; it could not (by design) tell the agent that the no-default branch must additionally return the `["foo","bar"]` list.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 13 | `<gt-scope files="…">` accompanying the gold-file view, listing `_language_extensions.py — in scope (you are viewing this)` first + related transform files, with the standard `GT has not confirmed a single primary target — confirm with grep.` honesty line. | Agent stayed in `_language_extensions.py` for the entire fix; did not wander to the adjacent transform files. | **D**=Y · **C**=Y (scope #1 = gold) · **C**=Y (stayed on gold) |

**consensus verdict:** D/C/C = **Y/Y/Y**, leak=0. Correct in-scope #1, correct-or-quiet abstention, agent stayed on the gold file.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| post-edit | `[GT_VERIFY]`-class reminder to run the affected-module suite with the file's real contracts (no test names). | Agent ran `test_language_extensions.py` etc. and reported all pass — but the in-workspace test was the PRE-gold version (the `test_find_in_map_values_not_found_with_default` assertions are injected at eval time), so "pass" was false confidence. | **D**=Y · **C**=Y (no leak) · **C**=Y (ran suite) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. Same structural limit as the rest of this run: the gold FAIL_TO_PASS test is absent from the agent's workspace, so the suite the agent ran could not catch the incomplete fix; GT cannot inject hidden gold tests.

## L4 / L5 / L5b / L6

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| — | DELIVERED=NO — no L4 tool invocation, no L5b/L6 markers in the 105-turn history (`gt_validate` no-op only; no `GT_META`/`[GT_CURATION]`/`dedup=`). | n/a | n/a |

**L4/L5/L5b/L6 verdict:** DELIVERED=NO; leak=0.

---

## Cross-component line

- **Total test-name / FAIL_TO_PASS leakage = 0.**
- **Consumed-count:** L1 (gold #1) + L3b (gold `value()` contracts) were both consumed and DROVE the agent to the gold method — a fair, GT-caused localization.
- **Fair-probe:** YES on localization (GT pre-localized gold at rank #1, gold method named; the agent acted on it). The miss is purely the **incomplete edit logic** (added the `raise` but not the no-default `["foo","bar"]` list-return) — a post-localization correctness gap a no-leakage layer cannot close.
- **Failure locus:** agent incomplete-fix-logic inside a correctly-localized gold function (GT localization + context delivery were correct and consumed).
