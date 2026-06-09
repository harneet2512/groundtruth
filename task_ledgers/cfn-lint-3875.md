# Ledger — aws-cloudformation__cfn-lint-3875  (run, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` unresolved), baseline_pass=**no** (flip-candidate), flip=**no**. **PATCH IS EMPTY of source edits — the final `git_patch` contains ONLY `.openhands/TASKS.md`; the agent never edited a single source file in 206 history turns (hit maxiter=100 actions).** GOLD = `src/cfnlint/template/transforms/_language_extensions.py::value()`: guard the no-default second-level fallback with `if isinstance(t_map[2], (_ForEachValueRef, _ForEachValueFnFindInMap)):` before iterating `mapping.items()`. FAIL_TO_PASS = `TestFindInMap::test_find_in_map_values_not_found_with_default` + `test_find_in_map_values_strings_without_default`.

**One-line trajectory finding:** GT localized **CORRECTLY** (gold file = L1 rank #2; post-view at IDX 13 delivered the real `value`/`values`/`transform` contracts; the agent opened the gold file at IDX 12 as its first source read — GT-driven). But the agent then **descended into a ~100-action debugging loop** probing `PROVIDER_SCHEMA_MANAGER.get_resource_schemas_by_regions` for the `BucketName` schema (the last ~14 actions are near-identical repeats of the same `python -c "from cfnlint.schema.manager import PROVIDER_SCHEMA_MANAGER …"` probe), **exhausted its iteration budget, and never wrote a fix.** Empty source patch → unresolved. **GT delivered correct context that was consumed for localization; the failure is the agent looping/never-editing — a non-harm/efficiency failure GT's nudge (1× L5 "No Source Edits" at IDX 47) did not break.**

right_trajectory = **FALSE** (correct context delivered + consumed, but no edit ever produced → cannot be a correct fix) · L1-ranked-gold = **rank 2, gold method named** · agent-reached-gold = **YES (GT-driven), but never edited it** · failure locus = **agent loop / empty patch (post-localization, never reached the edit)**

---

## PREREQS (substrate, 8-dp verbatim from `gt_gates_deep_…-3875.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 77.14285714` (det `2700.0` / calls `3500.0`) | GREEN | brief resolved-edge lines only |
| **P1** name_match | `name_match = 800` (22.86%) | GREEN (non-dominant) | telemetry-only |
| **P1** typing tiers | `type_flow=227`, `impl_method=261`, `inherited=150`, `ev:assignment_tracked=202` | GREEN | telemetry-only |
| **P2** calls / breakdown | `calls=3500.0`; `name_match=800, same_file=620, import=616, verified_unique=549, impl_method=261, type_flow=227, lsp=206, inherited=150, return_type=70, unique_method=1` | GREEN | L1 + post-view edge lines |
| **P2** LSP | `verdict=LSP_ACTIVE_VALID`, `resolved_promoted=206.0`, `attempted=294.0`, `graph_lsp_edges=206` | GREEN | telemetry-only |
| **P3** embedder present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15000000` | GREEN (`present_and_consumption`) | re-orders L1 list |

**Prereqs verdict:** ALL GREEN (`all_on:true`). Substrate healthy and produced a correct L1 ranking. Not the cause.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 | `<gt-localization confidence="medium">` / `  1. src/cfnlint/rules/functions/FindInMap.py — FindInMap, __init__, schema` / `  2. src/cfnlint/template/transforms/_language_extensions.py — values, _ResolveError, __init__` … (**gold file = rank #2**; the issue is FindInMap-with-DefaultValue, so #1 `FindInMap.py` is a plausible adjacent and #2 is the gold transform file). | IDX 12 (think): `Now let me look at … _language_extensions.py:` → opens the GOLD file (after grepping `FindInMap` at IDX 16 it also confirms the area). First source read is the gold file. | **D**=Y · **C**=Y (gold = rank #2, gold method-region named via `values`/`_ResolveError`) · **C**=Y (agent opened gold file) |

**L1 verdict:** D/C/C = **Y/Y/Y**, leak=0. Gold at rank #2, gold transform file named, agent went there. The #1 `FindInMap.py` is a non-gold but topically-adjacent rule file; it did not block the agent from the gold file.

## L3b post-view (`[GT]` / `<gt-context>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 13 | On the gold-file view: `[GT] _language_extensions:` / `[CONTRACT] def transform(self, cfn: Any) -> TransformResult:` / `[CONTRACT] def values( -> Iterator[str \| dict[Any, Any]]` / `[CONTRACT] def value(` / `flows: cfn -> … item.value(cfn, {}, False) | self._fn.value(cfn, params, False)` / `Called by: language_extension() _language_extensions.py:52`. | The agent read & reasoned over the gold `value`/`values` region early; it understood the FindInMap path. But it never converted that understanding into an edit — it pivoted to schema-manager debugging. | **D**=Y · **C**=Y (real contracts) · **C**=partial (consumed for understanding, not for an edit) |

**L3b post-view verdict:** D/C/C = **Y/Y/partial**, leak=0. Correct contracts delivered on the gold file. Consumed for comprehension but the agent never acted with an edit.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 13-region | `<gt-scope>` listing the gold transform file in-scope + the standard correct-or-quiet "confirm with grep" line. | Agent grepped `FindInMap` (IDX 16) confirming the scope; stayed in the transform/schema area. | **D**=Y · **C**=Y (gold in scope) · **C**=Y (agent stayed in area) |

**consensus verdict:** D/C/C = **Y/Y/Y**, leak=0. Scope correct, no misdirection.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| — | **DELIVERED=NO** — GT_VERIFY is a *post-edit* hook; the agent made **zero edits**, so GT_VERIFY never fired. | n/a | DELIVERED=NO (no edit ever occurred) |

**L3/GT_VERIFY verdict:** DELIVERED=NO — correctly never fired (nothing was edited). leak=0.

## L4 / L5 / L5b / L6

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 47 (L5) | `[GT L5: No Source Edits]` nudge (fired ONCE) reminding the agent it had run many actions with 0 source edits. | The agent did NOT change course — it continued exploring/probing for ~50 more actions and still never edited. The single nudge did not break the loop. | **D**=Y · **C**=n/a (nudge) · **C**=NO (ignored — agent kept looping) |
| L4 / L5b / L6 | DELIVERED=NO — no L4 tool calls, no L5b/L6 markers in history. | n/a | n/a |

**L4/L5/L5b/L6 verdict:** Only L5's "No Source Edits" nudge delivered (once, IDX 47); it was not consumed (agent looped on). L4/L5b/L6 not delivered. leak=0. **This is the one component with a clear improvement signal: the no-edit nudge fired only once and far too weakly to break a 100-action exploration loop.**

---

## Cross-component line

- **Total test-name / FAIL_TO_PASS leakage = 0.**
- **Consumed-count:** L1 + L3b + consensus all delivered correct gold-file context and were consumed for localization/comprehension — but NONE produced an edit.
- **Fair-probe:** YES on localization (GT pre-localized gold at rank #2 + correct contracts; agent acted on it to reach the file). The miss is downstream: the agent never edited and ran out of budget — a **non-harm/efficiency loop failure**, not a context failure.
- **Failure locus:** agent looped on schema-manager probing and submitted an **empty source patch** despite correctly-localized, correctly-delivered, consumed GT context. The L5 no-edit nudge (1×) was too weak to redirect.
