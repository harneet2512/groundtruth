# Ledger — aws-cloudformation__cfn-lint-4051  (run 27214152241, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-4051"]`), baseline_pass=**no** (flip-CANDIDATE), flip=**no**. GOLD = TWO files: `src/cfnlint/data/schemas/other/step_functions/statemachine.json` + `src/cfnlint/rules/resources/stepfunctions/StateMachineDefinition.py` (gold adds an `allOf` if/then JSONata-vs-JSONPath branch + a `JSONataChoice` definition to the JSON schema, and a matching `allOf` injection in `_convert_schema_to_jsonata`). Issue = E3601 errors on Step Functions `Choices` with JSONata.

**One-line trajectory finding (lead with this):** **right_trajectory = FALSE for GT.** GT's L1 (LOW) headline ranked the NON-gold `runner.py` / `api.py`, but candidate #3 was the gold `StateMachineDefinition.py` (and the orientation block re-named it). The agent self-localized via `find … | grep -i "step"` (the issue is plainly about Step Functions) at EVENT 8, reaching `StateMachineDefinition.py` — issue-driven, GT-attribution unprovable. The agent edited only the `.py` gold file (missing the JSON-schema gold) AND wrote a DIFFERENT, incorrect change (modifying `requiredXor` to add `"Condition"`, vs gold's `allOf`/`JSONataChoice` construction) → no resolve.

---

## PREREQS (substrate, 8-dp verbatim from gate-deep + certs)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 77.51014885` (deterministic_edges `2864.0` / calls_edges `3695.0`) | GREEN | resolved-edge lines in brief only |
| **P1** name_match | `name_match_edges = 831` (22.5%) | GREEN (non-dominant) | telemetry-only |
| **P1** typing tiers | `type_flow=251`, `impl_method=271`, `inherited=142`, `return_type=66`, `typing_fired=true` | GREEN | telemetry-only |
| **P2** calls / breakdown | `calls_edges=3695`; `name_match=831, import=666, same_file=632, verified_unique=588, impl_method=271, type_flow=251, lsp=247, inherited=142, return_type=66, unique_method=1` | GREEN | call/caller lines only |
| **P2** LSP | `server_launched=true`, `attempted=315`, `verified=128`, `corrected=119`, `deleted=0`, `failed=24`, `residual=200`, verdict `LSP_ACTIVE_VALID` | GREEN | 247 lsp edges fold in |
| **P3** embedder present | `class=EmbeddingModel`, dim=384, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15000000`, `rendered_semantic_nonzero=115/125`, `upstream_semantic_nonzero=346`, mode `present_and_consumption` | GREEN | re-orders L1 list only |

**Prereqs verdict:** gate-deep `all_on:true` — ALL GREEN. **Provenance caveat:** `graph_certificate` verdict `GRAPH_FAIL_MISSING_HANDOFF`. Substrate healthy. **Note:** one gold file is a JSON schema (`statemachine.json`) — outside the Python call-graph's reach, so GT structurally cannot rank it.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 1 | `<gt-localization confidence="low">` / `Region: src/cfnlint/ — candidate edit targets (reason over these, confirm with grep):` / `  1. src/cfnlint/runner.py` / `  2. src/cfnlint/api.py` / `  3. …stepfunctions/StateMachineDefinition.py` (← the .py gold, rank #3) / `  4. …jsonschema/exceptions.py` / `  5. …jsonschema/_keywords_cfn.py` / `  6. …resources/properties/AvailabilityZone.py`. `<gt-task-brief>` HEADLINE #1 = `runner.py` (non-gold). The JSON gold `statemachine.json` ABSENT (not a graph node). | EVENT 8 `find …/rules -type f -name "*.py" | grep -i "step"` → EVENT 9 obs `.../stepfunctions/StateMachineDefinition.py` → EVENT 10 opens it. Domain-keyword driven (Step Functions), not GT's list. | **D**=Y · **C**=partial (the .py gold is at rank #3, but headline #1 = non-gold `runner.py`, and the JSON gold is absent) · **C**=ambiguous (agent reached the file via its own `grep -i step`) |

**L1 verdict:** D/C/C = **Y/partial/ambiguous**, leak=0. L1 grazed the .py gold at #3 but headlined a non-gold file and could not name the JSON gold. Attribution to GT unprovable (issue domain term leads to the same file).

## L3b post-view (`[GT]` enrichment) + consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 11 | Prepended to the gold-file view: `[GT] StateMachineDefinition:` / `[CONTRACT] def _fix_message(self, err) -> ValidationError:` / `[CONTRACT] def _convert_schema_to_jsonata(self):` / `[CONTRACT] def _validate_step( -> ValidationResult` / `[CATCHES] except json.JSONDecodeError -> returns: return` / `Spec: _convert_schema_to_jsonata handles: "InputPath": False, …`; then `<gt-scope files="5">` / `1. stepfunctions/StateMachineDefinition.py — in scope (you are viewing this)` / `2. cfnlint/helpers.py — imported` / `3. jsonschema/CfnLintJsonSchema.py — graph-connected` / `4. helpers/get_value_from_path.py` / `5. jsonschema/Base.py` / `…GT has not confirmed a single primary target — confirm with grep.` | Agent reads the file body, locates `_convert_schema_to_jsonata`, and edits there. The `<gt-scope>` 4 adjacent files do NOT include the gold `statemachine.json` (JSON, not a graph node), so consensus did not surface the missing second gold file. | L3b: **D**=Y · **C**=Y (RIGHT contracts this time — `_convert_schema_to_jsonata`/`_validate_step`, no fabrication) · **C**=Y (agent edited the named function). consensus: **D**=Y · **C**=partial (scope #1 = gold .py, honest abstention; but missing the JSON gold) · **C**=N (agent stayed in .py) |

**L3b/consensus verdict:** L3b D/C/C = **Y/Y/Y** leak=0; consensus D/C/C = **Y/partial/N** leak=0. Post-view correctly summarized the very function the agent edited (`_convert_schema_to_jsonata`). Consensus correctly abstained but its graph-derived adjacency cannot include the JSON-schema gold.

## L3 / GT_VERIFY (post-edit)
GT_VERIFY fired post-edit with real `StateMachineDefinition.py` contracts (no test names leaked); the agent ran the local suite ("All 1279 tests pass" — but the gold `test_patch` cases `test_validate[Invalid…]`/`[JSONAta…]` were absent from its env). **D**=Y · **C**=Y (no leakage) · **C**=Y (suite run). Could not catch the wrong fix.

## L4 / L5 / L5b / L6
Active per telemetry; no observable governor effect changed the trajectory.

---

## Cross-component line
- **Test-name / FAIL_TO_PASS leakage = 0** (chronological read of 111 events; `test_state_machine_definition.py::test_validate[Invalid…/JSONAta…]` never surfaced by GT).
- **Consumed-count = 0** of GT localization drove the edit (post-view contracts were consumed as the function the agent already chose to edit; the file choice was issue-domain-driven).
- **Fair-probe = WEAK/NO.** Issue is explicitly about Step Functions + prints E3601; `grep -i step` / `grep E3601` → the .py gold directly.

## Failure locus (why it did not resolve)
TWO compounding post-localization failures, NOT a substrate failure:
1. **Incomplete scope** — gold requires a substantial rewrite of `statemachine.json` (a new `JSONataChoice` definition + an `allOf` if/then branching on `QueryLanguage`) AND a coordinated `_convert_schema_to_jsonata` change. The agent touched only the `.py` file; the JSON gold was never edited. GT could not name the JSON gold (not a graph node — gt_gt §10 data-file limitation).
2. **Wrong fix logic** — the agent's `.py` edit modifies the choice `Operator` `requiredXor` to prepend `"Condition"`; gold instead injects an `allOf` with a JSONataChoice item shape. Different mechanism → fails the JSONata-choice FAIL_TO_PASS cases.

**right_trajectory (GT) = FALSE** (headline localization wrong, JSON gold unreachable, edit logic wrong, GT-attribution unprovable). gt_caused = FALSE. flip = no.
