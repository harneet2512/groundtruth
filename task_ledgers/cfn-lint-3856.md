# Ledger — aws-cloudformation__cfn-lint-3856  (run gt-trial 30-task, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-3856"]`), baseline_pass=**no** (flip-candidate), flip=**no**. GOLD = **3 files**: `src/cfnlint/context/context.py`, `src/cfnlint/jsonschema/_resolvers_cfn.py`, `src/cfnlint/jsonschema/validators.py` — a multi-file fix so `!FindInMap` with a `!Sub` referencing `AWS::AccountId` (and other pseudo-params) does not spuriously raise E1011.

**One-line trajectory finding (lead with this):** **right_trajectory = FALSE — GT did not localize; the agent under-scoped a 3-file fix.** The agent self-localized: `grep E1011` (#14) → reached `_resolvers_cfn.py` (#20, gold file 2/3) and `context.py` (#24, gold file 1/3). GT's L1 (confidence="medium") ranked six WRONG files (`_rule.py`, `runner.py`, `config.py`, … — NONE of the 3 gold files). The agent edited only ONE gold file (`_resolvers_cfn.py`, #76, adding a `Fn::Sub` PSEUDOPARAMS guard) and never touched `context.py` or `validators.py` — a single-file fix to a 3-file problem. GT's L4 `gt_validate` (#116) raised a CALLER-BLIND-EDIT warning the agent explicitly dismissed (#118, correctly — informational). No test-name leakage.

---

## PREREQS (substrate, 8-dp verbatim from `gt_gates_deep_aws-cloudformation__cfn-lint-3856.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 75.91366906` (det `2638.0` / calls `3475.0`) | GREEN (`pred_A_det_floor=true`) | telemetry-only → resolved-edge lines |
| **P1** name_match | `name_match_edges = 837` (24.09%) | GREEN (`pred_B_nondominance=true`) | telemetry-only |
| **P1** typing tiers | `typing_fired=true`; `type_flow=227`, `impl_method=253`, `inherited=150`, `ev:assignment_tracked=202` | GREEN (`pred_C_typing=true`) | telemetry-only |
| **P2** calls / breakdown | `calls_edges=3475.0`; `name_match=837, same_file=617, import=609, verified_unique=547, impl_method=253, type_flow=227, lsp=171, inherited=150, return_type=63, unique_method=1` | GREEN (`pass=true`) | telemetry-only |
| **P2** LSP | `resolved_promoted=171.0`, `residual=141.0`, `attempted=234.0`, `graph_lsp_edges=171`, `verdict=LSP_ACTIVE_VALID` | GREEN | telemetry-only |
| **P3** present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** consumption | `effective_w_sem=0.15`, `semantic_signal_count=4`, `sem_max=0.82679100`, `sem_median=0.80307100`, `sem_separation_gap=0.02372000`, `sem_frac=0.50000000`, `pred_2_coverage=true` | GREEN (`pass=true`) | telemetry-only; most semantic signals of the six (4) |

**Prereqs verdict:** ALL GREEN (`all_on:true`). Substrate healthy; the resolved-edge lines pointed at non-gold files (see L1).

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 (instruction) | `<gt-localization confidence="medium">` / `1. src/cfnlint/rules/_rule.py — configure, _rule_is_enabled, RuleMatch` / `2. …/runner.py` / `3. …/config.py` / `4. …/PreviousGenerationInstanceType.py` / `5. …/functions/ForEach.py` / `6. …/HardCodedArnProperties.py` — **NONE of the 3 gold files** (`context.py`, `_resolvers_cfn.py`, `validators.py`) listed. `<gt-task-brief>` #1 = `_rule.py`. | IDX 6 (`think`): restates the E1011/FindInMap/Sub/AccountId problem; IDX 14: `grep E1011` → IDX 16: `FindInMap.py`; IDX 20: **`_resolvers_cfn.py` (gold 2/3)**; IDX 24: **`context.py` (gold 1/3)**. Never opened any L1 candidate as the target. | **D**=Y · **C**=**N** (all 3 gold files absent) · **C**=**N** (agent self-localized via grep) |

**L1 verdict:** D/C/C = **Y/N/N**, leak=0. Mislocalized (medium confidence, all 6 wrong). The agent reached 2 of 3 gold files purely from the `E1011` rule code + tracing the resolver.

## L3b post-view

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 21 | Prepended to the GOLD `_resolvers_cfn.py` view: `[GT] _resolvers_cfn:` / `[CONTRACT] def sub(validator, instance) -> ResolutionResult:` / `[CONTRACT] def unresolvable(…)` / `[CONTRACT] def ref(…)` / `[CONTRACT] flows: validator -> validator.is_type | _sub_parameter_expansion(validator, parameters) …` / `Spec: sub handles: for resolved_parameters…` — real, including the `sub` function (relevant to the agent's eventual edit site). | IDX 26/40/74 (`think`): agent traces `find_in_map` resolver, `is_function(instance[2])`, `Ref … PSEUDOPARAMS` — its own analysis of the body; decides to add a `Fn::Sub` PSEUDOPARAMS guard. | D=Y · C=Y (real contracts, gold file 2/3 reached) · C=partial (body consumed; the `sub` contract was adjacent but the agent localized the `find_in_map` edit site itself) |
| IDX 25 | Prepended to the GOLD `context.py` view: `[GT] context:` / `[CONTRACT] def has_language_extensions_transform(self):` / `[CONTRACT] def _get_pseudo_value_by_region(parameter, region) …` / **`Called by: src/cfnlint/jsonschema/validators.py:95 'self.context = create_context_for_template(self.cfn)' [model], …`** ← this caller line names `validators.py` (gold file 3/3!), buried in a multi-caller list. `[GT] context.py: also in scope.` | IDX 26 (`think`): agent reasons about the `find_in_map` resolver flow; it did NOT pick up the `validators.py` reference and never edited `context.py` or `validators.py`. | D=Y · C=Y (real contracts; the `validators.py:95` caller is accurate) · **C=N** (the one GT line that touched the 3rd gold file was not consumed) |

**L3b verdict:** D/C/C = **Y/Y/partial**, leak=0. Delivered REAL contracts on 2 of the 3 gold files; CORRECT (incl. an accurate `validators.py` caller edge buried in `context.py`'s view). But the agent consumed only the file bodies for its single-file fix — it never acted on `context.py`'s contract or the `validators.py` caller hint, so the multi-file scope was missed.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 21/25 | `<gt-scope>` on the resolver/context views + `… GT has not confirmed a single primary target — confirm the edit target with grep.` | Agent edited only `_resolvers_cfn.py`. | D=Y · C=partial (scope did not assemble the full 3-file gold set as "the" target) · C=N |

**consensus verdict:** D/C/C = **Y/partial/N**, leak=0. Correct-or-quiet (no over-claim), but it did not steer the agent to the full multi-file scope it needed.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| (post-edit, after #76) | `[GT_VERIFY] You edited 1 file(s). … confirm your change preserves the behavioral contract:` with `_resolvers_cfn.py` real contracts — **no test names**. | IDX 82-108: agent ran `pytest test_resolvers_cfn.py`, `test_find_in_map.py`, `test/unit/module/`, integration tests. | D=Y · C=Y (no leakage) · C=Y (ran the suites) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. Note GT_VERIFY says "You edited 1 file(s)" — factually true and arguably a missed chance to flag that a 3-file gold needs the other two, but GT cannot know the gold scope (that would be label leakage). Correct-or-quiet held.

## L4 (`gt_validate` / `gt_navigate`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 114/115 | `gt_validate unknown` → `# gt_validate: unknown` / `(… nothing to validate)` (no-op). | re-issues with real path. | D=N (no-op arg) |
| IDX 116/117 | `gt_validate src/cfnlint/jsonschema/_resolvers_cfn.py` → `# gt_validate: …_resolvers_cfn.py` / **`CALLER-BLIND-EDIT    symbol=sub  callers=5  (no test file edited alongside this change)`** / `# (total flags: 1 — informational; the pre-finish gate is authoritative)` | IDX 118 (`think`): "The CALLER-BLIND-EDIT warning is informational … we didn't change the `sub` function (we only added new code), so this warning is not relevant to our change." → dismisses it. | D=Y · C=Y (the `sub` symbol does have 5 callers; warning is accurate) · C=Y-but-correctly-dismissed (agent reasoned about it; the warning was a false alarm for this edit) |
| IDX 120/121 | `gt_navigate src/cfnlint/jsonschema/_resolvers_cfn.py` → `usage: gt_navigate <symbol> <mode> / mode = one of: trace, impact, relevant` (agent passed a path, not `<symbol> <mode>`; usage error / no-op). | IDX 122: proceeds to final tests. | D=N (no-op — wrong arg shape) |

**L4 verdict:** the ONE substantive L4 firing of the six (CALLER-BLIND-EDIT) was DELIVERED + CORRECT and the agent engaged with it — but it was a false alarm for an add-only edit, so correctly dismissed; net localization/fix value = 0. `gt_navigate` was a usage no-op. leak=0.

## L5 / L5b / L6

| turn | GT SENT | AGENT DID | D/C/C |
|---|---|---|---|
| — | **DELIVERED=NO** — no `GT_META`/`[GT_CURATION]`/`dedup=`/L5/L5b/L6 payload in any observation (`l5_telemetry.jsonl` = 31 lines, telemetry-only). | n/a | n/a |

**L5/L5b/L6 verdict:** DELIVERED=NO; leak=0.

---

## Cross-component line

leakage=**0** (the `test_resolvers_cfn.py`/`test_valid_functions` strings in history are the agent's own pytest output at IDX 83, NOT GT content — verified by chronological read) · delivered-to-agent components=**4** (L1, L3b, consensus, GT_VERIFY) + L4 (CALLER-BLIND warning + a navigate no-op) · consumed (GT-specific signal acted on)=**0** (agent self-localized via `E1011` grep; engaged the CALLER-BLIND warning only to dismiss it; ignored the `validators.py` caller hint inside `context.py`'s view) · fair-probe=**N/A (GT did not localize; issue pre-localizes via the E1011 rule code)**. **right_trajectory = FALSE** — GT named none of the 3 gold files in L1, and although post-view reached 2/3 (and incidentally surfaced the 3rd via a caller line), the agent under-scoped to a single-file fix. **Dominant failure locus here = multi-file SCOPE miss** (the agent edited 1 of 3 required files), compounded by GT not assembling the multi-file gold scope. gt_caused = **FALSE**.
