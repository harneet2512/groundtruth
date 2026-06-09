# Ledger — aws-cloudformation__cfn-lint-3817  (run gt-trial 30-task, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-3817"]`), baseline_pass=**no** (flip-candidate), flip=**no**. GOLD = `src/cfnlint/rules/functions/GetAtt.py` — gold's `schema()` introduces `resource_name_functions` AND `resource_attribute_functions=["Ref"]`, and under `has_language_extensions_transform()` sets BOTH to `["Ref","Fn::Base64","Fn::FindInMap","Fn::Sub","Fn::If","Fn::Join","Fn::ToJsonString"]`, applying the expanded set to both fn_items positions.

**One-line trajectory finding (lead with this):** **right_trajectory = FALSE — GT did not localize this one.** The issue title literally says "Invalid **E1010**"; the agent `grep E1010` (#8) and went STRAIGHT to the gold `GetAtt.py` (#10) — its first file read — entirely by self-localization. GT's L1 (confidence="low") ranked six WRONG files (`ServerlessTransform.py`, `UpdateReplacePolicy.py`, … — gold absent). GT's post-view fired on the gold file but delivered the generic `_resolve_getatt`/`fn_getatt` contracts, never naming the buggy `schema()`/`resource_functions`. The agent edited the right method (`schema()`'s `resource_functions`) but with an **incomplete list** (added `Fn::FindInMap/If/Select/Sub/ToJsonString` but MISSED `Fn::Base64`/`Fn::Join` and did NOT expand the second `fn_items` position from `["Ref"]`), so it did not match gold semantics → unresolved. No leakage.

---

## PREREQS (substrate, 8-dp verbatim from `gt_gates_deep_aws-cloudformation__cfn-lint-3817.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 74.22888440` (det `2575.0` / calls `3469.0`) | GREEN (`pred_A_det_floor=true`) | telemetry-only → resolved-edge lines in L1/post-view |
| **P1** name_match | `name_match_edges = 894` (25.77%) | GREEN (`pred_B_nondominance=true`) | telemetry-only |
| **P1** typing tiers | `typing_fired=true`; `type_flow=226`, `impl_method=261`, `inherited=149`, `ev:assignment_tracked=201` | GREEN (`pred_C_typing=true`) | telemetry-only |
| **P2** calls / breakdown | `calls_edges=3469.0`; `name_match=894, same_file=611, import=606, verified_unique=546, impl_method=261, type_flow=226, inherited=149, lsp=101, return_type=74, unique_method=1` | GREEN (`gate_resolution.pass=true`) | telemetry-only |
| **P2** LSP | `resolved_promoted=101.0`, `residual=89.0`, `attempted=142.0`, `graph_lsp_edges=101`, `verdict=LSP_ACTIVE_VALID`, `probe_latency_ms=1.46293640` | GREEN | telemetry-only |
| **P3** present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** consumption | `effective_w_sem=0.15`, `semantic_signal_count=2`, `sem_max=0.80794600`, `sem_median=0.00000000`, `sem_separation_gap=0.80794600`, `sem_frac=0.50000000`, `pred_2_coverage=false` | GREEN (`pass=true`) | telemetry-only |

**Prereqs verdict:** ALL GREEN (`all_on:true`). Substrate healthy; the localization failure is not a substrate failure — the resolved-edge lines simply pointed at non-gold files (see L1).

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 (instruction) | `<gt-localization confidence="low">` / `1. …/ServerlessTransform.py` / `2. …/UpdateReplacePolicy.py` / `3. …/BothUpdateReplacePolicyDeletionPolicyNeeded.py` / `4. …/ResourceType.py` / `5. …/config.py` / `6. …/ToJsonString.py` — **gold `GetAtt.py` NOT listed**. `<gt-task-brief>` ranks `ServerlessTransform.py`. (Issue is "GetAtt and Sub"; GT never names GetAtt.py.) | IDX 8: `grep -r "E1010"`; IDX 10: opens **`src/cfnlint/rules/functions/GetAtt.py`** (gold) as its FIRST file read. None of GT's 6 candidates was opened. | **D**=Y · **C**=**N** (gold absent) · **C**=**N** (agent ignored all 6) |

**L1 verdict:** D/C/C = **Y/N/N**, leak=0. Mislocalized (low confidence, all 6 wrong); agent self-localized from the issue's `E1010` rule code. No misdirection harm.

## L3b post-view

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 11 | Prepended to the GOLD `GetAtt.py` view: `[GT] GetAtt:` / `[CONTRACT] def _resolve_getatt( -> ValidationResult` / `[CONTRACT] def fn_getatt( -> ValidationResult` / `[CONTRACT] def __init__(self) -> None:` / `[CONTRACT] flows: validator -> validator.resolve_value | validator.context …` / `Spec: _resolve_getatt handles: f"{attribute_name!r} is not one of "…` — then the real `cat -n` body. **Does NOT mention `schema()` or `resource_functions` (the bug locus).** | IDX 12/28 (`think`): agent reasons over the file body — "Looking at the `GetAtt.schema()` method…" — i.e. it found `schema()` itself from the body, not from the GT contract header. | D=Y · C=Y (real contracts, gold reached) · C=partial (file body consumed; GT contract header not the bug) |
| IDX 115/141 | `<gt-context>` re-prepended on re-reads of `GetAtt.py` (same payload). | IDX 116: edits `schema()`. | D=Y · C=Y · C=N (header not load-bearing) |

**L3b verdict:** D/C/C = **Y/Y/partial**, leak=0. Delivered REAL contracts on the gold file (DELIVERED+CORRECT hold), but the surfaced symbols (`_resolve_getatt`, `fn_getatt`) were not the buggy `schema()`; the agent localized the bug from the file body, not GT's header.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 11 | `<gt-scope files="6">` / `1. functions/GetAtt.py — in scope (you are viewing this)` / `2. context/context.py — verified by language server` / `3. cfnlint/helpers.py — imported` / `4. jsonschema/protocols.py — verified…` / `5. functions/_BaseFn.py — graph-connected` / `These files are related in scope; GT has not confirmed a single primary target — confirm the edit target with grep.` | Agent stayed on `GetAtt.py` (already viewing it); it did read `_BaseFn.py` (#18) — a listed scope file — but reverted to GetAtt.py for the fix. | D=Y · C=Y (scope #1 = gold, since the agent was already viewing it) · C=partial (no misdirection; scope merely confirmed the file the agent already chose) |

**consensus verdict:** D/C/C = **Y/Y/partial**, leak=0. The `<gt-scope>` named the gold file #1 (because the agent was viewing it) and abstained from over-claiming. It neither caused the localization (the agent's grep did) nor harmed it.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 121 | After the edit: `[GT_VERIFY] You edited 1 file(s). … confirm your change preserves the behavioral contract:` / `GetAtt.py: guard_clause = return: errs -> yield from iter(errs)` / `GetAtt.py: return_shape = collection|{ "type": ["string", "array"], "minItems": 2,` / `GetAtt.py: return_shape = none` — **no test names**. | IDX 128-143: agent ran `pytest test_getatt.py`, `test/unit/rules/functions/`, `test/`, even `git stash` to compare — extensive verification. | D=Y · C=Y (real contracts, no leakage) · C=Y (agent ran the suites) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. The agent diligently ran tests; the incomplete `resource_functions` list passed the in-repo tests but not the gold `test_patch`'s new cases (`test_validate`/`test_getatt.py` additions), which the workspace lacked.

## L4 (`gt_validate`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 118 | `gt_validate unknown` → `# gt_validate: unknown` / `(file not in worktree … nothing to validate)` (agent passed literal `unknown`; no-op). | n/a | D=N (no-op arg) |
| IDX 150/151 | `gt_validate src/cfnlint/rules/functions/GetAtt.py` → `# gt_validate: src/cfnlint/rules/functions/GetAtt.py` / `# (no structural flags raised — file looks consistent with graph.db)` | IDX 152: FINISH. | D=Y · C=Y (clean no-flag report) · C=N (no actionable signal; nothing to consume) |

**L4 verdict:** delivered a clean "no structural flags" report (correct: the agent didn't break call structure) but it carried **no localization/fix value** — informational only. leak=0.

## L5 / L5b / L6

| turn | GT SENT | AGENT DID | D/C/C |
|---|---|---|---|
| — | **DELIVERED=NO** — no `GT_META`/`[GT_CURATION]`/`dedup=`/L5/L5b/L6 payload in any observation content across the full history (`l5_telemetry.jsonl` = 48 lines on disk, telemetry-only). | n/a | n/a |

**L5/L5b/L6 verdict:** DELIVERED=NO; nothing to consume; leak=0.

---

## Cross-component line

leakage=**0** (no test name / FAIL_TO_PASS surfaced by GT; the `test_getatt.py` strings in history are the agent's own pytest output) · delivered-to-agent components=**4** (L1, L3b, consensus, GT_VERIFY) + L4 no-flag report · consumed (GT-specific signal acted on)=**0** (agent self-localized via `E1010` grep and found `schema()` from the file body; no GT payload changed a decision) · fair-probe=**N/A (GT did not localize; issue pre-localizes via the E1010 rule code)**. **right_trajectory = FALSE** — correct-but-generic contracts on the gold file, but GT neither pointed to it nor surfaced the `resource_functions` bug; the agent's localization and its incomplete fix were its own. gt_caused = **FALSE**.
