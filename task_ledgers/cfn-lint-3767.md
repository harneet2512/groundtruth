# Ledger — aws-cloudformation__cfn-lint-3767  (run 27107841613, branch gt-trial, 2026-06-07)

Outcome: resolved=**no** (`eval_result.json` → `"resolved_instances": 0`, `"unresolved_ids": ["aws-cloudformation__cfn-lint-3767"]`), baseline_pass=**no** (id NOT in the 87 `resolved_ids` of `.claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json`), flip=**no** (resolved is false → cannot be a flip). GOLD file = `src/cfnlint/data/schemas/other/iam/policy.json` (`.instance.patch` adds exactly one line, `"additionalProperties": false,`, to the `"Condition"` definition).

**Causal headline:** GT NEVER named the gold file. Every GT component (L1 / L3b / consensus) pointed only at Python rule files (`iam/IdentityPolicy.py`, `iam/Policy.py`, `jsonschema/_keywords.py`); the gold is a JSON **schema-data** file (`data/schemas/other/iam/policy.json`). The agent reached gold by its **own** `find … -name "*.json" | xargs grep -l "Condition"` (T16), read it (T20), and reasoned the fix itself (T24/T46/T54). It then over-edited (T88 rewrote every `patternProperties` regex — gold touched none), which is the post-localization implementation error that left the task unresolved. The trajectory toward gold was self-driven, not GT-driven.

## PREREQS (substrate, 8-dp verbatim from gd/gt_gates_deep_aws-cloudformation__cfn-lint-3767.json)

| gate | real value | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1 resolution** | `det_pct=80.80781415`; `name_match_edges=727`; `deterministic_edges=3061`; typing tiers `{type_flow:229, impl_method:483, inherited:149, ev:assignment_tracked:204}`; `pred_A_det_floor=true`, `pred_B_nondominance=true`, `pred_C_typing=true`, `pass=true` | **YES** | telemetry-only; reached the agent ONLY as the brief's resolved-edge lines, e.g. T1 `gt-localization`: `"resolved call: -> __init__() in src/cfnlint/rules/resources/iam/Policy.py:20"` — all 6 resolve to `__init__` constructors, none to gold |
| **P2 graph.db** | `calls_edges=3788.0`; resolution_method breakdown `{name_match:727, same_file:605, import:599, verified_unique:546, impl_method:483, lsp:368, type_flow:229, inherited:149, return_type:78, unique_method:4}` | **YES** (name_match `727` is non-dominant vs `3061` deterministic) | telemetry-only; the brief's `gt-graph-map` (T1) exposed exactly one resolved fan-out: `src/cfnlint/jsonschema/_keywords.py :: format` → `calls: ValidationError …, check …`; `called by: format (…/rules/formats/Format.py)` |
| **P3 embedder** | `class=EmbeddingModel`; `cos_related=0.8605328`; `cos_unrelated=0.76078654`; `effective_w_sem=0.15`; `is_zero=false`; `sem_max=0.758102`; `pred_1_weight=true`, `pred_2_coverage=false` (`semantic_signal_count=1`, `sem_median=0.0`), `pred_3_dispersion=true`, `pass=true` | **YES** (overall `gate_embedder.pass=true`; `cos_related 0.86 > 0.76 cos_unrelated`) | telemetry-only; embedding ranks the L1 candidate order but surfaces no number to the agent — only the ordered `gt-localization` list in T1 |

**Prereqs verdict:** all three substrate gates GREEN (`"verdict":{"resolution_jarvis":true,"lsp_enrichment":true,"embedder":true,"all_on":true}`). Substrate is correct-and-quiet: it produced honest, non-laundered resolution/embedding facts. But a healthy substrate does NOT imply a correct localization claim — here the resolved edges all point at constructor `__init__`/Python-rule symbols, and the gold lives in a JSON data file the call-graph never reaches. The substrate is GREEN; the downstream localization is WRONG-on-this-task (not laundered, just irrelevant to the gold). LEAK: none.

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| T1 (instruction) | `<gt-localization confidence="medium">`  `Candidate edit targets (reason over these):`  `1. src/cfnlint/rules/resources/iam/IdentityPolicy.py — IdentityPolicy, __init__`  `   resolved call: -> __init__() in src/cfnlint/rules/resources/iam/Policy.py:20`  `2. src/cfnlint/rules/jsonschema/CfnLintJsonSchema.py …`  `3. … ServiceFargate.py …`  `4. src/cfnlint/runner.py …`  `5. … ServiceNetworkConfiguration.py …`  `6. … BothUpdateReplacePolicyDeletionPolicyNeeded.py …` — plus `<gt-task-brief>` headed `1. src/cfnlint/jsonschema/_keywords.py (def items(, def patternProperties(, def enum()` with `Expected behavior: The missing operator should be detected.` | T8 `find …/src/cfnlint -name "*.py" | xargs grep -l "IdentityPolicy"`; T12 `read … iam/IdentityPolicy.py` (L1 candidate #1); T14 `read … iam/Policy.py`. Then **abandoned the Python files** and ran its OWN T16 `find …/src/cfnlint/data -name "*.json" | xargs grep -l "Condition"` → T20 `read … data/schemas/other/iam/policy.json` (the GOLD file, which L1 never named) | **D=YES** (delivered in the instruction, agent-visible) · **C=WRONG** (gold `data/schemas/other/iam/policy.json` is NOT among the 6 candidates; all 6 are `.py`) · **C=PARTIAL** (agent consumed candidate #1 `IdentityPolicy.py` as an entry read, but reached gold via its own JSON grep, not via L1) |

**L1 verdict:** DELIVERED=YES · CORRECT=WRONG (gold not ranked; the schema-data file is structurally invisible to a Python call-graph localizer) · CONSUMED toward gold=NO (self-localized via T16 grep) · leak count = 0.

## L3b post-view

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| T13 (read obs, IdentityPolicy.py) | `[GT] IdentityPolicy:`  `Spec: __init__ handles: ... | ... | ... | ...` (placeholder ellipses, no real body) | continued reading; T14 `read … iam/Policy.py` (next L1/scope file) — no edit driven by it | D=YES · C=WEAK (placeholder `...` content, no guards/returns) · C=NO |
| T15 (read obs, Policy.py) | `[GT] Policy:`  `[CATCHES] except json.JSONDecodeError -> returns: return`  `Spec: validate handles: iam_validator = validator.evolve( | iam_validator = validator.evolve(`  `[GT] Policy.py: also in scope.` | T16 ran its OWN `find …/data -name "*.json" | grep -l "Condition"` — pivoted AWAY from the Python files this post-view annotated | D=YES · C=PARTIAL (real contract bytes, but on a non-gold file) · C=NO |
| T49 (read obs, _keywords.py) | `[GT] _keywords:`  `[CONTRACT] def properties( -> ValidationResult`  `[CONTRACT] def type( -> ValidationResult`  `[CONTRACT] def format( -> ValidationResult`  `[CONTRACT] flows: validator -> validator.is_type | validator.descend`  `Called by: …NumberRange.py:44 yield from minimum(…)`, `…Metadata.py:48 yield from properties(…)`, `…Configuration.py:48 yield from patternProperties(…)`  `[CATCHES] except ValueError -> returns: return …`  `Spec: additionalProperties handles: for extra in extras: …` | T50 `read … _keywords.py` again, T52 `read … test_resource_policy.py`; T54 `think` returned to `policy.json` ("add additionalProperties: false to the Condition definition in policy.json") — `_keywords.py` was a dead-end the agent self-exited | D=YES · C=PARTIAL (rich real contracts, but `_keywords.py` is not the gold; it is the jsonschema validator, not the schema-data file) · C=NO |

**L3b post-view verdict:** DELIVERED=YES (3 firings) · CORRECT=mixed (T15/T49 real contract bytes; T13 placeholder ellipses) but on **non-gold** files in all 3 · CONSUMED=NO (no edit/navigation toward gold was driven by any post-view; the agent independently grepped the data dir) · leak count = 0 (no test name / FAIL_TO_PASS / assertion surfaced).

## consensus <gt-scope>

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| T13 (read obs, IdentityPolicy.py) | `<gt-scope files="2">`  `1. iam/IdentityPolicy.py — in scope (you are viewing this)`  `2. iam/Policy.py — graph-connected`  `These files are related in scope; GT has not confirmed a single primary target — confirm the edit target with grep.`  `</gt-scope>` | T14 `read … iam/Policy.py` (scope file #2), then T16 ran its own `grep -l "Condition"` over `…/data/*.json` — exactly the "confirm with grep" the scope advised, landing on gold `policy.json` (which scope never named) | D=YES · C=WRONG-on-gold but HONEST (scope correctly abstained: "GT has not confirmed a single primary target — confirm with grep"; it named only the 2 Python files, never the gold JSON) · C=NO toward gold |

**consensus verdict:** DELIVERED=YES · CORRECT=correct-or-quiet (named 2 Python files, explicitly declined to assert a primary target, told the agent to grep) — but it did NOT name the gold JSON schema file, so it did not localize the fix · CONSUMED=NO (agent's grep self-localized; the scope's value was the honest abstain, not a target) · leak count = 0.

## L3/GT_VERIFY

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| — | **DELIVERED=NO** — no `[GT_VERIFY]` post-edit block appears in any observation in output.jsonl. The two edits (T60, T88) targeted `policy.json` (a `.json` schema-data file); the L3/router_v2 `on_edit` post-edit verifier emitted no agent-visible block (telemetry shows no `L3_router_v2 on_edit` event for this task — consistent with a non-Python edit target) | n/a | DELIVERED=NO |

**L3/GT_VERIFY verdict:** DELIVERED=NO (never reached the agent; edit target was `.json`, not a Python symbol the post-edit verifier annotates) · leak count = 0.

## L4/L5/L5b/L6

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| **L4** T99 (run obs) | only the named `gt_validate` tool was callable; agent invoked it bare → returned `# gt_validate: unknown`  `# (file not in worktree at /workspace/aws-cloudformation__cfn-lint-3767/unknown; nothing to validate)` | T100 continued with its own `cfn-lint` CLI repro on a `/tmp` template — ignored the empty L4 reply | L4: D=PARTIAL (only the empty `unknown` echo; no prefetch evidence block ever delivered) · C=n/a · C=NO |
| **L5** T63 (post-edit run obs) | `[GT L5: No Source Edits]`  `Iteration: 33/100`  `You have run 33 actions with 0 source file edits.` | T64 ran `pytest test/unit/rules/resources/iam/` — no behavior change attributable to the nudge | L5: D=YES · **C=WRONG** (the claim "0 source file edits" is FALSE — the agent had already edited `policy.json` at T60, 3 turns earlier) · C=NO (inert; agent's flow unchanged) |
| **L5b** | **DELIVERED=NO** — no L5b intervention block in any observation (telemetry-only intervention) | n/a | L5b: DELIVERED=NO |
| **L6** | **DELIVERED=NO** — no `[GT_VERIFY]` reindex/pre-submit payload in any observation; `gt_advisory`/`[GT_GATE] Pre-submit review` exists only in the `.instance.gt_advisory` field (`Files edited: 0 … Files explored but not edited: …_keywords.py, …IdentityPolicy.py, …Policy.py`), never injected into agent history | n/a | L6: DELIVERED=NO |

**L4/L5/L5b/L6 verdict:** L4 DELIVERED=PARTIAL (empty `unknown` echo only, no evidence block) / CONSUMED=NO; L5 DELIVERED=YES but its single block is factually WRONG ("0 edits" after T60) and INERT / CONSUMED=NO; L5b DELIVERED=NO; L6 DELIVERED=NO. leak count = 0 across all four (no test name / FAIL_TO_PASS / assertion surfaced).

## Cross-component line

leakage=**0** · delivered components=**5** (L1, L3b, consensus, L4[empty], L5) · consumed=**0** (no component drove the agent to gold or to the fix; gold reached via the agent's own T16 grep, fix reasoned at T24/T46/T54, both edits self-driven T60/T88) · fair-probe=**FAIR** (issue text "Condition syntax error not detected" names only the symptom — no resolution_method / edge-fact / gold path is pre-nameable from it; substrate not self-localizable by the issue) — but note the localization components (L1/L3b/consensus) are **PRE-NAMED on Python rule files only**, all of which the issue's "IAM … Condition" keywords trivially surface; none named the gold JSON schema file, so no GT component is creditable for reaching gold on this task.

---

# §4 DEEP AUDIT — run /tmp/gt_30_artifacts (deepseek-v4-flash, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` `resolved_ids=[]`, `unresolved_ids=["aws-cloudformation__cfn-lint-3767"]`), baseline_pass=**no**, flip=**no**. GOLD = `src/cfnlint/data/schemas/other/iam/policy.json` — gold patch adds exactly one line `"additionalProperties": false,` to the `"Condition"` definition.

**Causal headline (this run):** GT mislocalized (gold JSON schema NOT in the brief — see L1). Agent SELF-localized to gold and made a **byte-identical-to-gold edit** at T50 (`+ "additionalProperties": false,`), yet the task is UNRESOLVED. Root cause of the non-resolve is therefore NOT the gold hunk — it is **patch pollution**: the final `git_patch` carries `.openhands/TASKS.md` (scaffold) and a stray empty `test/fixtures/templates/good/generic.yaml.dot`, and the FAIL_TO_PASS unit test (`test/unit/rules/resources/iam/test_identity_policy.py`) asserts behaviour the single-line change does not fully satisfy. right_trajectory=**FALSE** for GT (GT did not deliver the gold; the agent reached it on its own).

## PREREQS (substrate, 8-dp verbatim from gt_gates_deep_aws-cloudformation__cfn-lint-3767.json)

| gate | real value | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1 resolution** | `det_pct=77.84239605`; `name_match_edges=762`; `deterministic_edges=2677`; `calls_edges=3439`; typing `{type_flow:223, impl_method:243, inherited:149, ev:assignment_tracked:198}`; pred_A/B/C all true; pass=true | **YES** (name_match 762 non-dominant vs 2677 det) | telemetry-only; reaches agent only as the brief's resolved-edge lines |
| **P2 graph.db** | `resolution_method_breakdown {name_match:762, same_file:605, import:599, verified_unique:546, impl_method:243, lsp:234, type_flow:223, inherited:149, return_type:77, unique_method:1}`; LSP `LSP_ACTIVE_VALID` promoted=234, closure rebuilt | **YES** | telemetry-only; surfaced as `<gt-graph-map>` (T1) `_keywords.py :: format` fan-out |
| **P3 embedder** | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280 > cos_unrelated=0.76078654`, `effective_w_sem=0.15`, present.pass=true, consumption.pass=true | **YES** | telemetry-only; orders the L1 candidate list |

**Prereqs verdict:** `verdict.all_on=true` — all three substrate gates GREEN. Substrate is correct-and-quiet. The wrong localization downstream is NOT a substrate defect — the gold is a JSON data file structurally invisible to a Python call-graph localizer. LEAK: 0.

## L1 localizer

| turn | GT SENT (verbatim bytes agent saw) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| T1 (user message) | `<gt-task-brief>` `1. src/cfnlint/rules/resources/iam/IdentityPolicy.py (def __init__(self):) … 2. src/cfnlint/jsonschema/_keywords.py (def items(, def patternProperties(, def enum() …` `Expected behavior: The missing operator should be detected.` `Scope chain … runner.py → template.py → conditions.py`. **Gold `policy.json` / `data/schemas` string: ABSENT (verified `'policy.json' in content == False`, `'data/schemas' in content == False`).** | T4-T18 read dir tree; T10 read `IdentityPolicy.py` (L1 #1); T16 read `data/schemas/other/iam` dir; **T20 read GOLD `policy.json` via its own dir navigation** (L1 never named it); T50 EDIT gold | **D=YES** · **C=WRONG** (gold not ranked; both #1 `IdentityPolicy.py` and #2 `_keywords.py` are wrong files) · **C=NO** toward gold (self-localized via dir nav at T16→T20) |

**L1 verdict:** DELIVERED=YES · CORRECT=WRONG · CONSUMED toward gold=NO · leak=0.

## L3b post-view

| turn | GT SENT (verbatim) | AGENT DID | D/C/C |
|---|---|---|---|
| T11 (read obs IdentityPolicy.py) | `<gt-scope>` + `[CONTRACT]` block on `IdentityPolicy.py` | T12 read `Policy.py` (next scope file), not gold | D=YES · C=PARTIAL (real bytes, non-gold file) · C=NO |
| T15 (read obs CfnLintJsonSchema.py) | `[CONTRACT]` + `Called by:` lines | T16 pivoted to its OWN `data/*.json` dir read | D=YES · C=PARTIAL · C=NO |
| T49 (read obs Condition.py) | `[CONTRACT]` block on `Condition.py` (resources rule, not gold) | T50 EDIT gold `policy.json` — driven by its OWN T38 think, not this post-view | D=YES · C=PARTIAL (non-gold) · C=NO |

**L3b verdict:** DELIVERED=YES (8 emitted per `gt_run_summary`) · CORRECT=real contract bytes but all on NON-gold files · CONSUMED=NO (`utilization_reason=structured_gt_side_but_no_agent_reaction`, `reactions_total=0`) · leak=0.

## consensus / `<gt-scope>`

| turn | GT SENT | AGENT DID | D/C/C |
|---|---|---|---|
| T11/T15/T49 | `<gt-scope>` listing graph-connected files + `These files are related in scope; GT has not confirmed a single primary target — confirm the edit target with grep.` | agent grepped `data/*.json` (the advised grep), landed on gold which scope never named | D=YES · C=HONEST-abstain but did NOT name gold · C=NO |

**consensus verdict:** correct-or-quiet (honestly abstained) but did not localize the gold JSON · CONSUMED=NO · leak=0.

## L3/GT_VERIFY · L4 · L5 · L5b · L6

| layer | status (from output.jsonl + gt_run_summary) |
|---|---|
| **L3_router_v2 (post-edit)** | eligible=8 emitted=8 `next_action_count=0` `utilization_reason=structured_gt_side_but_no_agent_reaction` — DELIVERED=YES, CONSUMED=NO |
| **L4 (tool)** | T66 agent ran bare `gt_validate unknown` → `# gt_validate: unknown # (file not in worktree …)`; ignored. DELIVERED=PARTIAL (empty echo), CONSUMED=NO |
| **L5 (governor)** | eligible=1 emitted=1 rendered_tokens=0; no agent-visible nudge text consumed; CONSUMED=NO |
| **L5b** | eligible=1 emitted=1 rendered_tokens=23; telemetry-only, not consumed |
| **L6** | not present in `per_layer` (no L6 events this task) — DELIVERED=NO |

## Cross-component line

leakage=**0** (zero test names / FAIL_TO_PASS / assertions in any GT-injected bytes — verified by leakage scan over brief + all GT-hook blocks) · delivered=**4** (L1, L3b, consensus, L3_router_v2) · consumed=**0** · fair-probe=**FAIR** · **right_trajectory=FALSE** (GT never delivered the gold file; agent self-localized + made the correct gold edit; non-resolve is patch-pollution / insufficient single-line change). **Failure locus: MISLOCALIZATION (GT) — gold JSON schema invisible to the Python call-graph; compounded by patch pollution post-localization.**


---

