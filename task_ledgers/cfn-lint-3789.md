# Ledger -- aws-cloudformation__cfn-lint-3789  (run 27107841613, branch gt-trial, 2026-06-07)

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

# §4 DEEP AUDIT — run /tmp/gt_30_artifacts (deepseek-v4-flash, 2026-06-09, 159 events — NOW AUDITABLE)

Outcome: resolved=**no** (`eval_result.json` `resolved_ids=[]`, `unresolved_ids=["aws-cloudformation__cfn-lint-3789"]`), baseline_pass=**no**, flip=**no**. GOLD = `scripts/boto/update_schemas_from_boto.py` (adds a `case_insensitive_services=["batch"]` mechanism that rewrites `enum`→`enumCaseInsensitive` at schema-generation time) PLUS the regenerated provider/patch `boto.json` files. The fix introduces a proper case-insensitive-enum CONCEPT in the codegen pipeline.

**Causal headline (this run, was UNAUDITABLE before):** GT mislocalized (gold codegen script ABSENT from brief; GT ranked `conditions.py` #1). The agent self-localized to the SYMPTOM files (read `aws-batch-computeenvironment.json` T18, the `boto.json` patch T36) and patched them directly — adding lowercase `"managed"`/`"unmanaged"` to the enum values (T86, T114). It NEVER reached the gold root-cause file `scripts/boto/update_schemas_from_boto.py`. The agent EXPLICITLY considered the right concept at T80 ("Modify the enum function to be case[-insensitive]") but chose "the simplest fix… Modify boto.json to include both cases" instead. right_trajectory=**FALSE**; failure locus = **mislocalization (GT) + post-localization wrong-approach + scope miss (gold root file never reached/edited)**.

## PREREQS (substrate, 8-dp from gt_gates_deep_aws-cloudformation__cfn-lint-3789.json)

| gate | real value | GREEN? |
|---|---|---|
| **P1 resolution** | `det_pct=79.03412377` (highest of the 6); name_match=725/3458; det=2733; typing `{type_flow:224, impl_method:187, inherited:149, ev:assignment_tracked:199}`; pass=true | YES |
| **P2 graph.db** | breakdown `{name_match:725, same_file:608, import:601, verified_unique:546, lsp:365, type_flow:224, impl_method:187, inherited:149, return_type:52, unique_method:1}`; LSP promoted=365 (most of the 6) | YES |
| **P3 embedder** | `EmbeddingModel`, is_zero=false, cos_related=0.86053280 > cos_unrelated=0.76078654, pass=true | YES |

**Prereqs verdict:** `all_on=true`. Substrate GREEN. LEAK=0.

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID | D/C/C |
|---|---|---|---|
| T1 | `<gt-task-brief>` `1. src/cfnlint/conditions/conditions.py (def satisfiable(, def build_scenarios(, def check_implies(…))` `Witness: _init_conditions calls get [CALLS]` … `Callers: create() in …/validators.py:178 …`. **Gold `scripts/boto/update_schemas_from_boto.py` ABSENT** (`'update_schemas_from_boto.py' in content == False`). | T18 read symptom `aws-batch-computeenvironment.json`; T36 read `boto.json` patch; T86 EDIT `boto.json`; T114 EDIT `aws-batch-computeenvironment.json`; **gold script NEVER read or edited** | **D=YES · C=WRONG** (gold absent; GT pinned `conditions.py` — unrelated) · **C=NO** |

**L1 verdict:** D=YES, C=WRONG, CONSUMED-toward-gold=NO, leak=0. Worst-quality localization of the 6 — `conditions.py` is unrelated to a Batch enum-case issue; the gold is a build-time codegen script, doubly invisible (not a runtime call-graph node).

## L3b / consensus / L3_router_v2 / L5 / L4 / L6

| layer | status |
|---|---|
| **L3b post-view** | eligible=11 emitted=9 suppressed=2 (already_delivered); `next_action_count=1`, `FOLLOWED_RELATED_FILE=1` util=1.0 — DELIVERED=YES, one related-file follow on the symptom files (not gold) |
| **consensus** | honest correct-or-quiet abstain; CONSUMED=NO |
| **L3_router_v2 (post-edit)** | eligible=15 emitted=12 suppressed=3 (duplicate); `next_action_count=0` util=0.5 — edits were to `.json` data files; DELIVERED=YES, CONSUMED=NO |
| **L5 (governor)** | eligible=23 emitted=2 suppressed=21 (`goku_handles_injection`×2, `debounce:NO_DURABLE_PROGRESS`, `max_emissions_reached`…) — heavily suppressed |
| **L4 / L6** | L4: 0 tool invocations (DELIVERED=NO); L6: not in per_layer |

## Cross-component line

leakage=**0** · delivered=**3** (L1, L3b, L3_router_v2) · consumed=**0** toward gold · fair-probe=**FAIR** (issue names error E3030 + the Batch enum — symptom only; gold codegen script not pre-nameable) · **right_trajectory=FALSE**. **Failure locus: MISLOCALIZATION (GT pinned unrelated `conditions.py`) + POST-LOCALIZATION WRONG-APPROACH + SCOPE MISS — the agent patched the generated symptom schemas (lowercase enum values) instead of the gold's `enumCaseInsensitive` codegen mechanism, and never reached the gold root-cause file `scripts/boto/update_schemas_from_boto.py`.** This is the only one of the 6 where the agent never even read/edited the gold file.

