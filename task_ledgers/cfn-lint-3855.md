# Ledger — aws-cloudformation__cfn-lint-3855  (run gt-trial 30-task, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` for this id → `error_instances: 1` / not in `resolved_ids`; the agent finished with a patch but the run is unresolved), baseline_pass=**no** (flip-candidate), flip=**no**. GOLD = `src/cfnlint/rules/conditions/EqualsIsUseful.py` — gold splits the message into "will always return True" vs adds a NEW always-FALSE branch inside the `try` using the boolean-normalized `first`/`second` and `isinstance(instance[0/1], (str,float,int,bool))`.

**One-line trajectory finding (lead with this):** **right_trajectory = FALSE — GT did not localize this one.** The agent self-localized: `grep "W8003|will always return"` (#8) → reached gold `EqualsIsUseful.py` (#12). GT's L1 (confidence="low") ranked six WRONG files (`PermissionSourceAccount.py`, `Equals.py`, `config.py`, … — gold absent; `Equals.py` is a sibling, not `EqualsIsUseful.py`). GT's post-view delivered the correct `equals_is_useful` contract on the gold file. The agent invoked the L4 `gt_validate` tool (#103) which returned a NO-OP ("no structural flags raised"). The agent's fix modified the WRONG branch (added `type(instance[0]) is type(instance[1])` to the literal-inequality check) instead of gold's normalized always-FALSE branch — and the gold `test_patch` CHANGED existing expected values (`[1,"1.1"]` 0→1), so the agent's logic could never pass. No leakage.

---

## PREREQS (substrate, 8-dp verbatim from `gt_gates_deep_aws-cloudformation__cfn-lint-3855.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 77.66906475` (det `2699.0` / calls `3475.0`) | GREEN (`pred_A_det_floor=true`) | telemetry-only → resolved-edge lines |
| **P1** name_match | `name_match_edges = 776` (22.33%) | GREEN (`pred_B_nondominance=true`) | telemetry-only |
| **P1** typing tiers | `typing_fired=true`; `type_flow=227`, `impl_method=233`, `inherited=150`, `ev:assignment_tracked=202` | GREEN (`pred_C_typing=true`) | telemetry-only |
| **P2** calls / breakdown | `calls_edges=3475.0`; `name_match=776, same_file=617, import=609, verified_unique=547, lsp=246, impl_method=233, type_flow=227, inherited=150, return_type=69, unique_method=1` | GREEN (`pass=true`) | telemetry-only |
| **P2** LSP | `resolved_promoted=246.0`, `residual=193.0`, `attempted=330.0`, `graph_lsp_edges=246`, `verdict=LSP_ACTIVE_VALID` | GREEN | telemetry-only |
| **P3** present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** consumption | `effective_w_sem=0.15`, `semantic_signal_count=3`, `sem_max=0.84939500`, `sem_median=0.78035400`, `sem_separation_gap=0.06904100`, `sem_frac=0.50000000`, `pred_2_coverage=true` | GREEN (`pass=true`) | telemetry-only |

**Prereqs verdict:** ALL GREEN (`all_on:true`). Highest LSP promotion of the six (246 edges). Substrate healthy; localization failure is not a substrate failure (resolved-edge lines pointed at non-gold files).

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 (instruction) | `<gt-localization confidence="low">` / `1. …/lmbd/PermissionSourceAccount.py` / `2. …/conditions/Equals.py` / `3. …/config.py` / `4. …/jsonschema/protocols.py` / `5. …/properties/Properties.py` / `6. …/certificatemanager/DomainValidationOptions.py` — **gold `EqualsIsUseful.py` NOT listed** (note #2 `Equals.py` is a SIBLING, not the gold). `<gt-task-brief>` #1 = `PermissionSourceAccount.py`. | IDX 8: `grep "W8003\|will always return"`; IDX 10: opens `Equals.py`; IDX 12: opens **`EqualsIsUseful.py` (gold)** — found by the grep, not GT. | **D**=Y · **C**=**N** (gold absent) · **C**=**N** (agent self-localized via grep) |

**L1 verdict:** D/C/C = **Y/N/N**, leak=0. Mislocalized (low confidence). The `Equals.py` near-miss is the classic name-adjacency trap (`Equals` vs `EqualsIsUseful`); GT ranked the wrong one.

## L3b post-view

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 13 | Prepended to the GOLD `EqualsIsUseful.py` view: `[GT] EqualsIsUseful:` / `[CONTRACT] def equals_is_useful(self, validator, s, instance, schema):` / `[CONTRACT] flows: validator -> validator.is_type` / `[CATCHES] except:  # noqa: E722 -> handles` / `Spec: equals_is_useful handles: yield ValidationError(…)` — then the real `cat -n` body. | IDX 16/24 (`think`): agent reasons over the body — "look at `equals_is_useful` … the check only catches cases where the two values…" — consuming the file content GT enriched; the GT contract header (`equals_is_useful`) IS the right method here, but the agent already had the body. | D=Y · C=Y (real contract, gold reached) · C=partial (file body consumed; the single contract line added little beyond the body) |

**L3b verdict:** D/C/C = **Y/Y/partial**, leak=0. Delivered the correct `equals_is_useful` contract on the gold file (DELIVERED+CORRECT), but it was a one-method file the agent was already reading in full — the GT header was confirmatory, not decisive.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 13 (within the gold view) | `<gt-scope>` listing scoped/graph-connected files + `… GT has not confirmed a single primary target — confirm the edit target with grep.` | Agent stayed on `EqualsIsUseful.py`. | D=Y · C=Y (scope includes the gold the agent is viewing) · C=partial |

**consensus verdict:** D/C/C = **Y/Y/partial**, leak=0. Correct-or-quiet; no misdirection.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| (post-edit, after #53/#63) | `[GT_VERIFY] You edited 1 file(s). … confirm your change preserves the behavioral contract:` with `EqualsIsUseful.py` real contracts — **no test names**. | IDX 59-90: agent ran `pytest test_equals_is_useful.py` repeatedly; saw `[1,"1.1"]`/`[True,"True"]` fail (#61) and tried to refine — but those expected values are exactly what the gold test_patch FLIPS. | D=Y · C=Y (no leakage) · C=Y (ran the suite) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. The agent ran tests and even SAW the conflicting cases, but the workspace test still had the OLD expectations (`[1,"1.1"]`→0); the gold `test_patch` (`[1,"1.1"]`→1) was absent, so the agent "fixed" its code to match the wrong (old) expectations. GT cannot inject the gold test.

## L4 (`gt_validate`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 101/102 | `gt_validate unknown` → `# gt_validate: unknown` / `(file not in worktree … nothing to validate)` (no-op). | IDX 103 → re-issues with real path. | D=N (no-op arg) |
| IDX 103/104 | `gt_validate src/cfnlint/rules/conditions/EqualsIsUseful.py` → `# gt_validate: src/cfnlint/rules/conditions/EqualsIsUseful.py` / `# (no structural flags raised — file looks consistent with graph.db)` | IDX 105+: continues; no behavior change. | D=Y · C=Y (clean no-flag report) · C=N (no actionable signal) |

**L4 verdict:** delivered a clean "no structural flags" report (correct: the agent's edit didn't break call structure) but **zero localization/fix value** — informational. leak=0.

## L5 / L5b / L6

| turn | GT SENT | AGENT DID | D/C/C |
|---|---|---|---|
| — | **DELIVERED=NO** — no `GT_META`/`[GT_CURATION]`/`dedup=`/L5/L5b/L6 payload in any observation (`l5_telemetry.jsonl` = 27 lines, telemetry-only). | n/a | n/a |

**L5/L5b/L6 verdict:** DELIVERED=NO; leak=0.

---

## Cross-component line

leakage=**0** (no test name / FAIL_TO_PASS surfaced by GT) · delivered-to-agent components=**4** (L1, L3b, consensus, GT_VERIFY) + L4 no-flag report · consumed (GT-specific signal acted on)=**0** (agent self-localized via `W8003` grep; the GT contract was confirmatory of a one-method file the agent already read) · fair-probe=**N/A (GT did not localize; issue pre-localizes via the W8003 rule code)**. **right_trajectory = FALSE** — GT did not point to the gold file (ranked the sibling `Equals.py`), and the agent's fix modified the wrong branch with logic that contradicts the gold test_patch's flipped expectations. gt_caused = **FALSE**.
