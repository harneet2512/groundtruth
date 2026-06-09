# Ledger — aws-cloudformation__cfn-lint-3982  (run, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` unresolved), baseline_pass=**no** (flip-candidate), flip=**no**. GOLD = schema-DATA patch adding `"uniqueKeys": ["Sid"]` to the `Statement` array in EXACTLY TWO files — `src/cfnlint/data/schemas/other/iam/policy_identity.json` and `policy_resource.json` — placed AFTER the `"type": ["object","array"]` block. FAIL_TO_PASS = `test/unit/rules/resources/iam/test_identity_policy.py::TestIdentityPolicies::test_duplicate_sid`, which calls `self.rule.validate(validator, policy={…two statements, same Sid "All"…}, schema={}, policy_type=None)` and asserts `len(errs)==1` and `errs[0].message == "array items are not unique for keys ['Sid']"`.

**One-line trajectory finding:** GT localized the **right AREA** — L1 ranked the IAM rule files `ResourcePolicy.py` (#1) and `IdentityPolicy.py` (#3); the agent opened `IdentityPolicy.py` (IDX 14, GT-driven), and GT post-view ("Policy.py: also in scope", IDX 17) routed it toward the IAM schema directory where it found the gold `policy_identity.json`/`policy_resource.json` (IDX 18-22). The agent confirmed the `uniqueKeys` validator exists (`def uniqueKeys(` line 604, IDX 53) and added `uniqueKeys:[Sid]` matching gold's content. **But it did NOT resolve.** Two divergences from gold: (1) the agent ALSO edited the base `policy.json` (gold edits only the two `policy_*` files); (2) it placed `uniqueKeys` BEFORE the `type` block in `policy_identity.json` (gold places it after) and verified ONLY via the CLI/template path — never via the rule-level `validate(…, schema={})` entrypoint the hidden `test_duplicate_sid` uses. The fix is content-correct but the agent could not confirm the exact rule-level behavior the FAIL_TO_PASS checks (that test is absent from its workspace). **GT correctly routed the agent into the gold area + gold files; the residual is a schema-placement / extra-file / unverifiable-rule-path correctness gap.**

right_trajectory = **partial → FALSE on outcome** (GT routed to the right area + gold files were edited with the right content, but the precise rule-level message the test asserts was not achieved/verified) · L1-ranked-gold = **gold AREA at rank 1 & 3 (the rule files); gold DATA files reached via GT post-view routing** · agent-reached-gold = **YES (edited the two gold schema files + an extra)** · failure locus = **agent schema-placement / extra-file edit + unverifiable rule-level path (post-localization)**

---

## PREREQS (substrate, 8-dp verbatim from `gt_gates_deep_…-3982.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 78.16091954` (det `2788.0` / calls `3567.0`) | GREEN (highest of the 6) | brief resolved-edge lines only |
| **P1** name_match | `name_match = 779` (21.84%) | GREEN (non-dominant) | telemetry-only |
| **P1** typing tiers | `type_flow=232`, `impl_method=263`, `inherited=153`, `ev:assignment_tracked=207` | GREEN | telemetry-only |
| **P2** calls / breakdown | `calls=3567.0`; `name_match=779, import=635, same_file=627, verified_unique=553, impl_method=263, lsp=256, type_flow=232, inherited=153, return_type=68, unique_method=1` | GREEN | L1 + post-view edge lines |
| **P2** LSP | `verdict=LSP_ACTIVE_VALID`, `resolved_promoted=256.0`, `attempted=348.0`, `graph_lsp_edges=256` | GREEN | telemetry-only |
| **P3** embedder present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15000000` | GREEN (`present_and_consumption`) | re-orders L1 list |

**Prereqs verdict:** ALL GREEN (`all_on:true`). Substrate healthy; here it produced a useful AREA ranking (IAM rule files at #1/#3) that routed the agent toward the gold schema files.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 | `<gt-localization confidence="medium">` / `  1. src/cfnlint/rules/resources/iam/ResourcePolicy.py — ResourcePolicy, __init__` / `  2. scripts/update_schemas_manually.py …` / `  3. src/cfnlint/rules/resources/iam/IdentityPolicy.py — IdentityPolicy, __init__` / `  4. …/_rules.py` / `  5. …/_rule.py` / `  6. …/iam/RefWithPath.py`. **The IAM rule files (the right AREA) are #1 & #3; the gold is the schema-DATA files these rules load, which GT cannot rank directly (code-only graph), but the rule ranking routes the agent into the IAM subtree.** | IDX 14 (read): opens `IdentityPolicy.py` (L1 #3); IDX 16 follows to `Policy.py`; IDX 18-22 navigates to `data/schemas/other/iam/` and opens `policy.json`, `policy_identity.json`, `policy_resource.json` — i.e. GT's IAM-rule ranking carried it to the gold schema dir. | **D**=Y · **C**=partial (gold AREA ranked #1/#3; gold DATA files not directly ranked but reachable from the ranked rules) · **C**=Y (agent followed the IAM ranking into the gold subtree) |

**L1 verdict:** D/C/C = **Y / partial / Y**, leak=0. L1 ranked the correct IAM rule files (#1 `ResourcePolicy.py`, #3 `IdentityPolicy.py`). The gold itself is schema DATA (not code), which the graph cannot rank, but the rule-file ranking successfully routed the agent into the IAM schema directory where the gold files live. Best localization of the 6 tasks.

## L3b post-view (`[GT]` / `<gt-context>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 15 | On `IdentityPolicy.py`: `[GT] IdentityPolicy:` / `[CONTRACT] def __init__(self):`. | Agent reads the rule, then follows to `Policy.py`. | **D**=Y · **C**=Y · **C**=Y |
| IDX 17 | On `Policy.py`: `[GT] Policy:` / `[CATCHES] except json.JSONDecodeError -> returns: return` / **`[GT] Policy.py: also in scope.`** | IDX 18: agent navigates to the IAM schema dir (`data/schemas/other/iam`) — the "also in scope" cue helped widen the agent into the schema subtree. | **D**=Y · **C**=Y (correct scope cue) · **C**=Y (agent acted — moved toward the schema files) |

**L3b post-view verdict:** D/C/C = **Y/Y/Y**, leak=0. Post-view delivered real IAM-rule contracts and a correct "also in scope" routing cue that helped the agent find the gold schema directory. Correct and consumed.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 15-17 region | `<gt-scope>` listing the IAM policy rule files in-scope + the `Policy.py also in scope` line; correct-or-quiet. | Agent stayed in the IAM subtree and reached the schema files. | **D**=Y · **C**=Y (IAM scope correct) · **C**=Y |

**consensus verdict:** D/C/C = **Y/Y/Y**, leak=0. Scope kept the agent in the IAM area; routed correctly to the gold subtree.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| post-edit | `[GT_VERIFY]`-class reminder to run the affected IAM module suite (no test names). | IDX 108-151: agent ran `pytest test/unit/rules/resources/iam/` (reported pass) + integration tests; verified via CLI YAML repros (IDX 136-145). The hidden `test_duplicate_sid` (rule-level `validate(…, schema={})`) is injected at eval time and was absent — so the agent's CLI-path verification could not confirm the exact rule-level message the test asserts. | **D**=Y · **C**=Y (no leak) · **C**=Y (ran suite + built repros) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. GT_VERIFY prompted broad testing and the agent complied, but the FAIL_TO_PASS asserts a specific rule-level error message via a code path (`self.rule.validate(policy=…, schema={})`) the agent verified only indirectly (CLI template). GT cannot inject the hidden gold test.

## L4 / L5 / L5b / L6

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 146 (L4) | `gt_validate unknown` → `# gt_validate: unknown / (file not in worktree … nothing to validate)` — no-op. | n/a | DELIVERED=NO (no-op) |
| L5 / L5b / L6 | DELIVERED=NO — no L5/L5b/L6 evidence markers in the 153-turn history. | n/a | n/a |

**L4/L5/L5b/L6 verdict:** DELIVERED=NO; leak=0.

---

## Cross-component line

- **Total test-name / FAIL_TO_PASS leakage = 0.**
- **Consumed-count:** L1 (IAM rule ranking) + L3b ("also in scope" routing) + consensus all delivered correct AREA context and were consumed to route the agent into the gold schema directory — the strongest GT-assisted localization of the 6.
- **Fair-probe:** PARTIAL — GT did not name the gold DATA files (code-only graph), but it ranked the loading IAM rule files (#1/#3) and the post-view scope cue routed the agent to the gold files, which it then edited with the correct `uniqueKeys:[Sid]` content. The miss is a residual schema-placement / extra-file (`policy.json`) edit + the agent's inability to verify the exact rule-level message the hidden test asserts.
- **Failure locus:** agent post-localization correctness gap (placement before vs after the `type` block; an extra base-`policy.json` edit not in gold; verification via CLI rather than the rule-level `validate(schema={})` path the FAIL_TO_PASS exercises). GT routing/context were correct and consumed.
