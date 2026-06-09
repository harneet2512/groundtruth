# Ledger — aws-cloudformation__cfn-lint-3821  (run gt-trial 30-task, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-3821"]`), baseline_pass=**no** (flip-candidate), flip=**no**. GOLD = `src/cfnlint/rules/resources/HardCodedArnProperties.py` — gold wraps the existing accountId-mismatch message in `if candidate[2] not in ["cloudfront"]:` so a `cloudfront` accountId segment is exempted.

**One-line trajectory finding (lead with this):** **This is the ONLY task where L1 ranked the gold file #1 — but it is NOT a fair probe and GT was NOT consumed.** The issue title literally says "I3042:accountId check" and the body shows the `I3042` rule firing; `I3042` is the hardcoded `id` of `HardCodedArnProperties`. The agent opened the gold file as its FIRST read (#8) and grep'd `I3042` (#16) — it would have self-localized from the rule code regardless. Its reasoning (#10) is its OWN regex analysis of the ARN, citing no GT content. The agent's fix (added `cloudfront` to the allowed-values regex alternation) is logically close to gold's intent but **did not match gold's exact mechanism** (gold keeps the regex and adds a separate `candidate[2] not in ["cloudfront"]` guard); it was scored unresolved (likely a PASS_TO_PASS interaction from broadening the regex). No leakage.

---

## PREREQS (substrate, 8-dp verbatim from `gt_gates_deep_aws-cloudformation__cfn-lint-3821.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 74.77581718` (det `2585.0` / calls `3457.0`) | GREEN (`pred_A_det_floor=true`) | telemetry-only → resolved-edge lines in L1/post-view |
| **P1** name_match | `name_match_edges = 872` (25.22%) | GREEN (`pred_B_nondominance=true`) | telemetry-only |
| **P1** typing tiers | `typing_fired=true`; `type_flow=226`, `impl_method=250`, `inherited=149`, `ev:assignment_tracked=201` | GREEN (`pred_C_typing=true`) | telemetry-only |
| **P2** calls / breakdown | `calls_edges=3457.0`; `name_match=872, same_file=611, import=608, verified_unique=546, impl_method=250, type_flow=226, inherited=149, lsp=123, return_type=71, unique_method=1` | GREEN (`pass=true`) | telemetry-only |
| **P2** LSP | `resolved_promoted=123.0`, `residual=120.0`, `attempted=186.0`, `graph_lsp_edges=123`, `verdict=LSP_ACTIVE_VALID` | GREEN | telemetry-only |
| **P3** present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** consumption | `effective_w_sem=0.15`, `semantic_signal_count=3`, `sem_max=0.83646600`, `sem_median=0.78552100`, `sem_separation_gap=0.05094500`, `sem_frac=0.50000000`, `pred_2_coverage=true` | GREEN (`pass=true`) | telemetry-only; **coverage prediction TRUE here** (3 semantic signals, separated) — the strongest of the six tasks |

**Prereqs verdict:** ALL GREEN (`all_on:true`). Substrate healthy; semantic coverage actually fired (pred_2=true). The substrate supported a correct L1 #1 ranking on this task.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 (instruction) | `<gt-localization confidence="medium">` / **`1. src/cfnlint/rules/resources/HardCodedArnProperties.py — HardCodedArnProperties, __init__, _match_values`** (the GOLD file, rank #1) / `2. …/Password.py` / `3. …/iam/RefWithPath.py` / `4. …/PrimaryIdentifiers.py` / `5. …/helpers.py` / `6. …/DistributionTargetOriginId.py`. `<gt-task-brief>` #1 = `HardCodedArnProperties.py` with its real `match_values`/`_match_values` contract. | IDX 8: opens **`HardCodedArnProperties.py`** (gold, L1 #1) as its first file read. | **D**=Y · **C**=**Y** (gold ranked #1) · **C**=ambiguous — agent DID open the #1 file first, but the issue's `I3042` rule code independently points there (see fair-probe) |

**L1 verdict:** D/C/C = **Y/Y/(consumed-but-not-fair)**, leak=0. L1 correctly ranked the gold file #1 — the only correct L1 of the six. BUT the issue pre-localizes the same file via its `I3042` rule ID (the agent grep'd `I3042` at #16), so we cannot credit the open to GT.

## L3b post-view

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 9 | Prepended to the GOLD `HardCodedArnProperties.py` view: `[GT] HardCodedArnProperties:` / `[CONTRACT] def __init__(self):` / `[CONTRACT] def _match_values(self, cfnelem, path):` / `[CONTRACT] def match_values(self, cfn):` / `[CONTRACT] flows: cfnelem -> isinstance(cfnelem, dict) | for key in cfnelem …` / `[CONTRACT] flows: cfn -> cfn.template` — then the real `cat -n` body showing `id = "I3042"` and the ARN `regex`. **Does NOT name the `accountId`/`candidate[2]` check (the actual bug locus, ~line 125).** | IDX 10 (`think`): agent reasons over the ARN regex and the issue ("The rule I3042 checks if the account ID is hardcoded … doesn't include `cloudfront`") — its OWN analysis of the body, not GT's `_match_values` contract. | D=Y · C=Y (real contracts, gold reached) · C=partial (file body consumed; the GT contract header was for `match_values`/`_match_values`, not the `match()`+`candidate[2]` bug) |

**L3b verdict:** D/C/C = **Y/Y/partial**, leak=0. Delivered real contracts on the gold file, but the surfaced methods were not the buggy `accountId` regex check; the agent diagnosed that from the body itself.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 9 | `<gt-scope files="6">` / `1. resources/HardCodedArnProperties.py — in scope (you are viewing this)` / `2. rules/_rule.py — graph-connected` / `3. resources/CircularDependency.py — shares match` / `4. resources/Modules.py — shares match` / `5. resources/PreviousGenerationInstanceType.py — shares match` / `These files are related in scope; GT has not confirmed a single primary target — confirm the edit target with grep.` | Agent stayed on `HardCodedArnProperties.py`; did not wander to the 4 "shares match" siblings. | D=Y · C=Y (scope #1 = gold) · C=partial (confirmed the file the agent already opened; no independent value) |

**consensus verdict:** D/C/C = **Y/Y/partial**, leak=0. Listed the gold file #1, abstained from over-claiming, and the agent didn't chase the sibling files. Non-harmful, but did not cause the localization.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| (post-edit) | `[GT_VERIFY] You edited 1 file(s). … confirm your change preserves the behavioral contract:` with `HardCodedArnProperties.py` real contract lines — **no test names**. | IDX 52-59: agent ran `pytest test_hardcodedarnproperties.py` and `test/unit/rules/resources/` to verify. | D=Y · C=Y (no leakage) · C=Y (agent ran the suite) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. The workspace test predates the gold `test_patch`'s new `test_file_positive_with_config` (the FAIL_TO_PASS), so the agent's regex-broadening fix passed local tests but was not validated against the gold case.

## L4 / L5 / L5b / L6

| turn | GT SENT | AGENT DID | D/C/C |
|---|---|---|---|
| — | **DELIVERED=NO** — no `gt_validate`/`gt_navigate` call; no `GT_META`/`[GT_CURATION]`/`dedup=`/L5/L5b/L6 payload in any observation (`l5_telemetry.jsonl` = 1 line, telemetry-only). | n/a | n/a |

**L4/L5/L5b/L6 verdict:** DELIVERED=NO; leak=0.

---

## Cross-component line

leakage=**0** (the `test_hardcodedarnproperties.py` string in history is the agent's own pytest collection output at IDX 55, NOT GT content — verified by chronological read) · delivered-to-agent components=**4** (L1, L3b, consensus, GT_VERIFY) · consumed (GT-specific signal acted on)=**0** (the agent's localization and regex fix cite its own analysis; no GT payload changed a decision) · fair-probe=**UNFAIR** — the issue title "I3042:accountId check" + the I3042-firing traceback uniquely identify `HardCodedArnProperties` (whose `id="I3042"`); the agent grep'd `I3042` independently. **right_trajectory = FALSE** (despite L1's correct #1 ranking, GT did not CAUSE the localization — the issue pre-localized it — and the GT contracts/scope were not the buggy `accountId` check the agent diagnosed itself; the fix was the agent's own and did not match gold). gt_caused = **FALSE** (fails fair_probe and consumed). **Note for Stage-1:** this is the strongest L1 signal of the six (correct #1 ranking, semantic coverage fired) — but on an issue where localization was free, so it proves nothing about GT causation.
