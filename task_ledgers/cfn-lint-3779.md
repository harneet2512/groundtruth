# Ledger -- aws-cloudformation__cfn-lint-3779  (run 27107841613, branch gt-trial, 2026-06-07)

Outcome: **no_patch (agent job=failure)** . resolved=0 . baseline_pass=no . flip=no

## UNAUDITABLE -- no agent-observation trajectory
- agent job: no_patch (agent job=failure)
- artifacts uploaded: eval_result.json only
- **output.jsonl: ABSENT** -> per gt_trial.md section 4, the per-component audit (GT SENT / AGENT DID) CANNOT be performed: there is no agent-observation record to READ.
- reason: agent produced no patch; no output.jsonl uploaded

| component | verdict |
|---|---|
| PREREQS . L1 . L3b . consensus . L3/GT_VERIFY . L4 . L5 . L5b . L6 | **UNAUDITABLE -- no output.jsonl** |

**Cross-component line:** leakage=unverifiable . delivered=unverifiable . consumed=unverifiable.
**FINDING:** no auditable trajectory -- part of the artifact gap (5/10 tasks this run left no readable output.jsonl: 3 no_patch eval-only + 2 cancelled). Fix: per-job timeout that uploads the partial trajectory on cancel/failure so every task is auditable.

---

# §4 DEEP AUDIT — run /tmp/gt_30_artifacts (deepseek-v4-flash, 2026-06-09, 109 events — NOW AUDITABLE)

Outcome: resolved=**no** (`eval_result.json` `resolved_ids=[]`, `unresolved_ids=["aws-cloudformation__cfn-lint-3779"]`), baseline_pass=**no**, flip=**no**. GOLD = `src/cfnlint/data/schemas/other/iam/policy.json` (same JSON-schema gold family as 3767) — gold REPLACES the whole `Condition` `patternProperties` block with consolidated patterns `^(ForAnyValue:|ForAllValues:)?(Not)?IpAddress(Exists)?(IfExists)?$` → `#/definitions/ConditionValue`.

**Causal headline (this run, was UNAUDITABLE before):** GT mislocalized (gold JSON schema ABSENT from brief; GT ranked `api.py` #1). The agent self-localized to gold (read `policy.json` T26 via dir nav), correctly diagnosed the `^`-anchor bug + the `8e463fb9a` `additionalProperties: false` commit (T62 think), and edited the gold file at T68 — but with a per-pattern fix (strip `^`, add `(IfExists)?`) that DIVERGES from gold's consolidation. right_trajectory=**FALSE** for GT; failure locus = **mislocalization (GT) + post-localization implementation miss (agent's per-pattern edit ≠ gold's pattern consolidation)**.

## PREREQS (substrate, 8-dp from gt_gates_deep_aws-cloudformation__cfn-lint-3779.json)

| gate | real value | GREEN? |
|---|---|---|
| **P1 resolution** | `det_pct=77.17926441`; name_match=788/3453; det=2665; typing `{type_flow:224, impl_method:226, inherited:149, ev:assignment_tracked:199}`; pass=true | YES |
| **P2 graph.db** | breakdown `{name_match:788, same_file:608, import:600, verified_unique:546, lsp:243, impl_method:226, type_flow:224, inherited:149, return_type:68, unique_method:1}`; LSP `LSP_ACTIVE_VALID` promoted=243 | YES (resolution+lsp) |
| **P3 embedder** | `EmbeddingModel`, is_zero=false, cos_related=0.86053280 > cos_unrelated=0.76078654 (present.pass=true); **consumption.pass=FALSE** (`pred_2_coverage=false`, `pred_3_dispersion=false`) | present=YES, **consumption=NO** |

**Prereqs verdict:** `verdict.all_on=FALSE` — the ONLY task of the 6 with a substrate gate not fully green. The embedder is present and non-zero (cos_related 0.86 > cos_unrelated 0.76) but was NOT consumed in a separating way for THIS task (coverage + dispersion predicates failed). This is a per-task consumption weakness, not a dead embedder — but it means semantic ranking contributed little to this brief, which is consistent with the gold (a JSON data file with no call-graph signal) being unrankable regardless. LEAK=0.

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID | D/C/C |
|---|---|---|---|
| T1 | `<gt-task-brief>` `1. src/cfnlint/api.py (def lint(, def lint_all(s: str) -> list[Match]:)` … `EDIT-TARGET CONTRACTS (api.py): lint -> calls ConfigMixIn …` `Scope chain … api.py → runner.py → _rules.py → transform.py → match.py`. **Gold `policy.json` / `data/schemas` ABSENT** (`'policy.json' in content == False`). | T10 read `IdentityPolicy.py`; T20 read `Policies.json`; **T26 read GOLD `policy.json`** via its own dir navigation; T68 EDIT gold | **D=YES · C=WRONG** (gold absent; GT pinned `api.py`) · **C=NO** toward gold (self-localized) |

**L1 verdict:** D=YES, C=WRONG, CONSUMED-toward-gold=NO, leak=0. Localization MISS (same structural cause as 3767 — gold is a JSON data file invisible to the Python call-graph).

## L3b post-view / consensus / L3_router_v2 / L4 / L5 / L5b / L6

| layer | status |
|---|---|
| **L3b post-view** | eligible=3 emitted=3 `next_action_count=0` util=0.5 (`structured_gt_side_but_no_agent_reaction`) — fired on the Python files agent opened early (IdentityPolicy.py, Policy.py), all non-gold; DELIVERED=YES, CONSUMED=NO |
| **consensus** | honest correct-or-quiet abstain on non-gold views; CONSUMED=NO |
| **L3_router_v2 (post-edit)** | eligible=4 emitted=4 `next_action_count=0` util=0.5 — note: agent's edit was to `policy.json` (a `.json` data file), so the post-edit verifier had limited source-symbol to annotate; DELIVERED=YES, CONSUMED=NO |
| **L5/L5b** | eligible=1 emitted=1 each, rendered_tokens 0/23; telemetry-only, not consumed |
| **L4 / L6** | L4: 0 tool invocations (DELIVERED=NO); L6: not in per_layer (DELIVERED=NO) |

## Cross-component line

leakage=**0** · delivered=**3** (L1, L3b, L3_router_v2) · consumed=**0** toward gold · fair-probe=**FAIR** (issue names error codes E3510/operator regexes — the symptom — not the gold file path; GT did not pre-name gold) · **right_trajectory=FALSE**. **Failure locus: MISLOCALIZATION (GT, gold JSON schema absent from brief — structurally invisible to Python call-graph) + POST-LOCALIZATION IMPLEMENTATION MISS (agent edited gold but with a per-pattern `^`-strip+`(IfExists)?` fix, not gold's pattern consolidation to `ConditionValue`).** Sole substrate caveat of the 6: embedder consumption gate FALSE (present but non-separating on this task).

