# Ledger -- aws-cloudformation__cfn-lint-3770  (run 27107841613, branch gt-trial, 2026-06-07)

Outcome: **CANCELLED (timeout ~67min)** . resolved=0 . baseline_pass=no . flip=no

## UNAUDITABLE -- no agent-observation trajectory
- agent job: CANCELLED (timeout ~67min)
- artifacts uploaded: none
- **output.jsonl: ABSENT** -> per gt_trial.md section 4, the per-component audit (GT SENT / AGENT DID) CANNOT be performed: there is no agent-observation record to READ.
- reason: looped_stuck signature; job cancelled before artifact upload

| component | verdict |
|---|---|
| PREREQS . L1 . L3b . consensus . L3/GT_VERIFY . L4 . L5 . L5b . L6 | **UNAUDITABLE -- no output.jsonl** |

**Cross-component line:** leakage=unverifiable . delivered=unverifiable . consumed=unverifiable.
**FINDING:** no auditable trajectory -- part of the artifact gap (5/10 tasks this run left no readable output.jsonl: 3 no_patch eval-only + 2 cancelled). Fix: per-job timeout that uploads the partial trajectory on cancel/failure so every task is auditable.

---

# §4 DEEP AUDIT — run /tmp/gt_30_artifacts (deepseek-v4-flash, 2026-06-09, 191 events — FULL TRAJECTORY, NOW AUDITABLE)

Outcome: resolved=**no** (`eval_result.json` `resolved_ids=[]`, `unresolved_ids=["aws-cloudformation__cfn-lint-3770"]`), baseline_pass=**no**, flip=**no**. GOLD = `src/cfnlint/rules/resources/stepfunctions/StateMachineDefinition.py` — gold adds `from cfnlint.helpers import is_function` and, at the TOP of the `for k in definition_keys` loop (before `if not value: continue`), `fn_name, _ = is_function(value); if fn_name: continue`.

**Causal headline — THIS IS THE KEY TASK: GT LOCALIZED CORRECTLY.** The gold file `StateMachineDefinition.py` is **rank #1 in the L1 brief** (`'StateMachineDefinition.py' in content == True`), and the agent opened it FIRST (T6). So localization is a GT WIN here. Yet the task did NOT resolve, because the agent's fix — `if is_function(value)[0]: continue` placed in the **`else` branch** (after the string-type check) — is in the WRONG location vs gold (gold puts the `is_function` guard at the TOP of the loop, so it catches the intrinsic before the string-type branch). This is a **pure post-localization implementation miss** on a task GT localized correctly. right_trajectory=**partially TRUE for localization, FALSE for the fix** — GT delivered the correct file and correct contract; the agent wrote a subtly wrong implementation that a no-leakage context layer cannot dictate.

## PREREQS (substrate, 8-dp from gt_gates_deep_aws-cloudformation__cfn-lint-3770.json)

| gate | real value | GREEN? |
|---|---|---|
| **P1 resolution** | `det_pct=77.19094602`; name_match=786/3446; det=2660; typing `{type_flow:224, impl_method:242, inherited:149, ev:assignment_tracked:199}`; pass=true | YES |
| **P2 graph.db** | breakdown `{name_match:786, same_file:605, import:598, verified_unique:546, impl_method:242, lsp:233, type_flow:224, inherited:149, return_type:62, unique_method:1}`; LSP `LSP_ACTIVE_VALID` promoted=233 | YES |
| **P3 embedder** | `EmbeddingModel`, is_zero=false, cos_related=0.86053280 > cos_unrelated=0.76078654, w_sem=0.15, pass=true | YES |

**Prereqs verdict:** `all_on=true`. Substrate GREEN. LEAK=0.

## L1 localizer — THE WIN

| turn | GT SENT (verbatim) | AGENT DID | D/C/C |
|---|---|---|---|
| T1 | `<gt-task-brief>` `1. src/cfnlint/rules/resources/stepfunctions/StateMachineDefinition.py (def _fix_message(self, err: ValidationError) -> ValidationError:, def validate(, def __init__(self):)` `Witness: validate called by _fix_message [CALLS]` `Contract: returns value|err | flows err -> err.path | err.message …` `EDIT-TARGET CONTRACTS (StateMachineDefinition.py): validate -> calls _fix_message …; validate -> calls _clean_error …; validate -> calls evolve(…)`. **GOLD is rank #1.** | **T6 read GOLD `StateMachineDefinition.py` FIRST** (the L1 #1 candidate); T8 think reasons over `validate()` line 71 `if validator.is_type(value, "string")` | **D=YES · C=CORRECT** (gold ranked #1; contract accurate) · **C=YES** (agent opened the #1 candidate first and reasoned over it) |

**L1 verdict:** D=YES, **C=CORRECT**, C=YES. **This is a genuine L1 localization win** — gold file #1, agent consumed it first. Fair-probe caveat: the issue text references PR #3768 + `StateMachineDefinition`, so the gold is partly self-nameable from the issue — but GT independently ranked it #1 with an accurate contract, no leakage.

## L3b post-view + consensus

| turn | GT SENT | AGENT DID | D/C/C |
|---|---|---|---|
| T7 (gold-file view) | `[GT] StateMachineDefinition:` `[CATCHES] except json.JSONDecodeError -> returns: return` `<gt-scope files="4"> 1. StateMachineDefinition.py — in scope (you are viewing this) 2. CfnLintJsonSchema.py 3. Base.py 4. protocols.py … GT has not confirmed a single primary target — confirm the edit target with grep.` | T8 think reasoned over `validate()` directly | D=YES · C=CORRECT (real catch contract; scope honest) · C=partial (read; not the decisive driver) |
| T24-T122 | repeated `[GT]`/`[CONTRACT]` blocks on the ~10 files agent explored (`_resolvers_cfn.py`, `_types.py`, `_filter.py`, `helpers.py`, `context.py`, `Join.py`, `_keywords_cfn.py`, `CfnLintJsonSchema.py`) — all real contracts | agent explored widely (191 events), 10 reactions logged | D=YES · C=real bytes · C=mixed (`follow_type_distribution`: FOLLOWED_EXACT=1, IGNORED=3, NOT_MEASURABLE=1) |

**L3b verdict:** D=YES (22 emitted), CORRECT=real bytes, CONSUMED=partial (1 FOLLOWED_EXACT, 3 IGNORED). consensus = honest correct-or-quiet abstain. leak=0.

## L3_router_v2 / L4 / L5 / L5b / L6

| layer | status |
|---|---|
| **L3_router_v2 (post-edit)** | eligible=32 emitted=20 suppressed=12 (duplicate×11, budget×1); `next_action_count=0` util=0.5 (`structured_gt_side_but_no_agent_reaction`) — DELIVERED=YES, CONSUMED=NO |
| **L4** | T188 think notes `gt_validate` flagged `CALLER-BLIND-EDIT warning` (validate has 5 callers, no test edited) — agent acknowledged but did not edit a test. L4 advisory delivered; CONSUMED=acknowledged-not-acted |
| **L5 (governor)** | eligible=47 emitted=1 suppressed=46 (`goku_handles_injection`×5, `structured_only:band=mid_commitment`×13, `max_emissions_reached`…) — heavily/correctly suppressed |
| **L5b** | eligible=1 emitted=1 — no distinct consumed payload |
| **L6** | present in per_layer (reindex events on this longer run) |

**Note on L4 CALLER-BLIND-EDIT:** GT correctly warned that `validate` has 5 callers and no test was edited alongside the change. The agent (T176) concluded "All 6 failures are pre-existing… My change introduces no new test failures" and submitted. The warning was accurate but did not change the outcome (the issue was the fix placement, not caller-breakage).

## Cross-component line

leakage=**0** · delivered=**5** (L1 [CORRECT/#1], L3b, consensus, L3_router_v2, L4) · consumed=**partial** (agent opened the L1 #1 gold file first; L3b 1 FOLLOWED_EXACT) · fair-probe=**WEAK-FAIR** (issue references `StateMachineDefinition` + PR #3768, partly self-nameable; GT still independently ranked gold #1, no leak) · **right_trajectory=localization TRUE, fix FALSE**. **Failure locus: POST-LOCALIZATION IMPLEMENTATION MISS — GT delivered the correct gold file (#1) + accurate contract; the agent placed the `is_function` guard in the wrong branch (else, after string-check) vs gold (top of loop). A no-leakage context layer cannot determine the correct guard placement; this is exactly the "implementation correctness" bottleneck.** Secondary: scaffold pollution (`generic.yaml.dot`, `.openhands/TASKS.md`) in the patch.
