# Ledger — aws-cloudformation__cfn-lint-3798

---

# §4 DEEP AUDIT — run /tmp/gt_30_artifacts (deepseek-v4-flash, 2026-06-09, 121 events)

Outcome: resolved=**no** (`eval_result.json` `resolved_ids=[]`, `unresolved_ids=["aws-cloudformation__cfn-lint-3798"]`), baseline_pass=**no**, flip=**no**. GOLD = `src/cfnlint/jsonschema/_keywords.py` — gold simply CHANGES THE LITERAL ERROR-MESSAGE STRINGS in `maxItems`/`maxLength`/`maxProperties`/`minItems`/`minLength`/`minProperties` (e.g. `f"{instance!r} is too long ({mI})"` → `f"expected maximum item count: {mI}, found: {len(instance)}"`). It is a pure message-wording change; no logic change.

**Causal headline:** GT's brief ranked `_utils.py` #1 (WRONG), but the gold file `_keywords.py` IS referenced in the brief — as a SECONDARY mention inside the `Callers:`/`Calls:` lines (`const() in src/cfnlint/jsonschema/_keywords.py:130`, and in the `Calls:` list). So gold was present-but-not-primary. The agent reached + edited the gold `_keywords.py` (T70), but with a WRONG implementation: a `maxItems_error` schema-override hack (`if "maxItems_error" in schema: yield ValidationError(schema["maxItems_error"])`) — completely different from gold's literal-string change. The agent ALSO edited the test file `test/unit/rules/functions/test_find_in_map.py` (T80) and `FindInMap.py` — chasing the wrong design. right_trajectory=**FALSE**; failure locus = **post-localization implementation miss (wrong fix design) — GT had gold as a secondary mention, not as the ranked primary target.**

## PREREQS (substrate, 8-dp from gt_gates_deep_aws-cloudformation__cfn-lint-3798.json)

| gate | real value | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1 resolution** | `det_pct=78.73134328`; name_match=741/3484; det=2743; typing `{type_flow:227, impl_method:240, inherited:150, ev:assignment_tracked:202}`; pred_A/B/C true; pass=true | **YES** | telemetry-only; resolved-edge lines in the brief |
| **P2 graph.db** | breakdown `{name_match:741, same_file:617, import:609, verified_unique:547, lsp:292, impl_method:240, type_flow:227, inherited:150, return_type:60, unique_method:1}`; LSP `LSP_ACTIVE_VALID` promoted=292, closure rebuilt | **YES** | telemetry-only |
| **P3 embedder** | `class=EmbeddingModel`, is_zero=false, cos_related=0.86053280 > cos_unrelated=0.76078654, effective_w_sem=0.15, present.pass=true, consumption.pass=true | **YES** | telemetry-only; orders the L1 candidate list |

**Prereqs verdict:** `verdict.all_on=true` — all three substrate gates GREEN. Substrate correct-and-quiet. LEAK=0.

## L1 localizer

| turn | GT SENT (verbatim bytes agent saw) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| T1 (user message) | `<gt-task-brief>` `1. src/cfnlint/jsonschema/_utils.py (def equal(one, two):, def unbool(…), def uniq(container):)` `Witness: _mapping_equal called by equal [CALLS]` `Callers: find_in_map() in …/_resolvers_cfn.py:85 …; _validate_resource_type_uniqueness() in …/PrimaryIdentifiers.py:88 …; const() in src/cfnlint/jsonschema/_keywords.py:130 'if not equal(instance, const):'` `Calls: …, src/cfnlint/jsonschema/_keywords.py, …` `EDIT-TARGET CONTRACTS (_utils.py): equal -> calls _mapping_equal …`. **GOLD `_keywords.py` is NOT ranked #1 — it appears only as a SECONDARY mention** (`'_keywords.py' in content == True`, but inside the Callers/Calls lines, not as the primary edit target). | T16 read `FindInMap.py`; **T18 read GOLD `_keywords.py`** (reached via its own reasoning + the secondary mention); T70 EDIT gold `_keywords.py`; T72/T76 EDIT `FindInMap.py`; T80 EDIT test file | **D=YES** · **C=PARTIAL** (gold present as secondary mention, but the RANKED primary `_utils.py` is wrong; GT did not point at `_keywords.py` as THE target) · **C=PARTIAL** (agent reached + edited gold, but its decision was driven by its own T44/T50 reasoning about `fn_findinmap`/`maxItems`, not by the L1 ranking) |

**L1 verdict:** D=YES · CORRECT=PARTIAL (gold present but not primary; #1 `_utils.py` wrong) · CONSUMED=PARTIAL (gold reached + edited, but agent-driven not L1-driven) · leak=0.

## L3b post-view

| turn | GT SENT | AGENT DID | D/C/C |
|---|---|---|---|
| T18/T22/T42/T96 (gold `_keywords.py` views) | `[GT]` + `[CONTRACT]` blocks on `_keywords.py` (real `maxItems`/keyword contracts) | T26/T34/T40/T44 think reasoned about `maxItems`/`fn_findinmap` and the test expectations; chose the `maxItems_error` schema-override design | D=YES · C=Y (real bytes on the gold file) · C=partial (read into reasoning, but informed the WRONG design) |

**L3b verdict:** D=YES (10 emitted), CORRECT=real bytes on gold, CONSUMED=partial (`follow_type_distribution.IGNORED=1`, util=0.75, reactions_total=1). leak=0.

## consensus / L3_router_v2 / L4 / L5 / L5b / L6

| layer | status |
|---|---|
| **consensus `<gt-scope>`** | honest correct-or-quiet abstain on the views; CONSUMED=NO |
| **L3_router_v2 (post-edit)** | eligible=20 emitted=18 suppressed=2 (duplicate); `next_action_count=0` util=0.5 (`structured_gt_side_but_no_agent_reaction`) — DELIVERED=YES, CONSUMED=NO |
| **L4** | 0 tool invocations — DELIVERED=NO |
| **L5 (governor)** | eligible=33 emitted=1 suppressed=32 (`goku_handles_injection`, `structured_only:band=early_exploration`×3, `…mid_commitment`×21, `max_emissions_reached`…) — heavily/correctly suppressed |
| **L5b** | eligible=1 emitted=1 — no distinct consumed payload |
| **L6** | present in per_layer (reindex events; agent edited `.py` source so reindex could fire) |

## Cross-component line

leakage=**0** (zero test names / FAIL_TO_PASS / assertions in any GT-injected bytes — verified by leakage scan over brief + all GT-hook blocks. NOTE: the agent itself edited the test file `test_find_in_map.py` at T80 — that is the AGENT's own action, NOT GT leakage; GT never surfaced a test name) · delivered=**3** (L1, L3b, L3_router_v2) · consumed=**partial** (agent reached + edited the gold file, but via its own reasoning) · fair-probe=**FAIR** (issue describes the E1011 "is too long (3)" message — the symptom; GT did not pre-name the gold as primary) · **right_trajectory=FALSE**. **Failure locus: POST-LOCALIZATION IMPLEMENTATION MISS — the agent reached gold `_keywords.py` but wrote a `maxItems_error` schema-override hack instead of gold's literal error-message-string change, and additionally edited `FindInMap.py` + the test file, chasing the wrong design. GT had gold only as a secondary caller-line mention, not as the ranked #1 target.**
