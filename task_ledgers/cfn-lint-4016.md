# Ledger — aws-cloudformation__cfn-lint-4016  (run 27214152241, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-4016"]`), baseline_pass=**no** (flip-CANDIDATE), flip=**no**. GOLD = THREE files: `src/cfnlint/data/schemas/other/iam/policy.json` + `src/cfnlint/data/schemas/other/iam/policy_identity.json` + `src/cfnlint/rules/resources/iam/IdentityPolicy.py` (gold adds `pattern "^[A-Za-z0-9]+$"` to Sid in `policy_identity.json`, `minItems:1` in `policy.json`, and registers 3 new resource types in `IdentityPolicy.py`). Issue = "Statement IDs (SID) must be alpha-numeric" (no E/W code).

**One-line trajectory finding (lead with this):** **right_trajectory = FALSE for GT.** GT MISLOCALIZED (L1 confidence=LOW ranked `_keywords_cfn.py`, `runner.py`, `validators.py`, `config.py`, `exceptions.py`, `_utils.py` — **none is any of the 3 gold files**). The agent self-localized via `grep "Sid|sid"` in the iam dir (the literal term is in the issue) and edited `policy.json` — but it picked the WRONG schema file (gold puts the Sid pattern on `policy_identity.json`, and the FAIL_TO_PASS test `test_identity_policy.py::test_pattern_sid` exercises the IdentityPolicy path). GT was inert and wrong; the agent partially self-localized to a sibling schema.

---

## PREREQS (substrate, 8-dp verbatim from gate-deep + certs)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 77.46092679` (deterministic_edges `2825.0` / calls_edges `3647.0`) | GREEN | resolved-edge lines in the brief only |
| **P1** name_match | `name_match_edges = 822` (22.5%) | GREEN (non-dominant) | telemetry-only |
| **P1** typing tiers | `type_flow=247`, `impl_method=257`, `inherited=141`, `return_type=73`, `typing_fired=true` | GREEN | telemetry-only |
| **P2** calls / breakdown | `calls_edges=3647`; `name_match=822, import=656, same_file=627, verified_unique=581, impl_method=257, type_flow=247, lsp=242, inherited=141, return_type=73, unique_method=1` | GREEN | call/caller lines only |
| **P2** LSP | `server_launched=true`, `attempted=342`, `verified=129`, `corrected=113`, `deleted=0`, `failed=37`, `residual=213`, verdict `LSP_ACTIVE_VALID` | GREEN | 242 lsp edges fold in |
| **P3** embedder present | `class=EmbeddingModel`, dim=384, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15000000`, `rendered_semantic_nonzero=132/142`, `upstream_semantic_nonzero=343`, mode `present_and_consumption` | GREEN | re-orders L1 list only |

**Prereqs verdict:** gate-deep `all_on:true` — ALL GREEN. **Provenance caveat:** `graph_certificate` verdict `GRAPH_FAIL_MISSING_HANDOFF`. Substrate healthy; not the cause. **Note:** the gold files are TWO JSON schemas + one rule file; GT's graph indexes Python, so the JSON schema gold (`policy.json`/`policy_identity.json`) is structurally outside the call-graph's reach — a recall gap for data-file-driven fixes.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 1 | `<gt-localization confidence="low">` / `Region: src/cfnlint/ — candidate edit targets (reason over these, confirm with grep):` / `  1. src/cfnlint/jsonschema/_keywords_cfn.py` / `  2. src/cfnlint/runner.py` / `  3. src/cfnlint/jsonschema/validators.py` / `  4. src/cfnlint/config.py` / `  5. src/cfnlint/jsonschema/exceptions.py` / `  6. src/cfnlint/jsonschema/_utils.py` (NONE of the 3 gold files present) | EVENT 6-9: agent greps; EVENT 12 `grep -rn "Sid|sid" .../rules/resources/iam/` (the term is from the issue); EVENT 13 → reads `PolicyVersion.py`, then iam siblings; ends editing `policy.json` (a gold file, but NOT the one the FAIL_TO_PASS test targets). | **D**=Y · **C**=N (0/6 candidates are gold; LOW confidence, honest) · **C**=N (agent ignored GT, self-localized via the `Sid` term) |

**L1 verdict:** D/C/C = **Y/N/N**, leak=0. MISLOCALIZED; correctly flagged LOW confidence + "confirm with grep" (correct-or-quiet honesty), so it did not falsely assert. Inert.

## L3b post-view (`[GT]` enrichment) + consensus (`<gt-scope>`)
Post-view `[GT]` headers fired on the iam files the agent opened (`PolicyVersion.py`, `StatementResources.py`, etc.) with real contracts — none is `IdentityPolicy.py` (the rule gold), and post-view cannot enrich a JSON schema (non-graph node). `<gt-scope>` count = 2 firings; neither surfaced `IdentityPolicy.py` or the JSON gold. **D**=Y (on opened files) · **C**=Y (real contracts, no leak) · **C**=N (did not redirect to gold). No GT_VERIFY/`gt_validate` mentions in this trajectory (the agent edited a JSON file → the post-edit Python-contract hook had nothing to assert).

## L4 / L5 / L5b / L6
Active per telemetry; no agent-observable consumption.

---

## Cross-component line
- **Test-name / FAIL_TO_PASS leakage = 0** (chronological read of 98 events; `test_identity_policy.py::test_pattern_sid` never surfaced).
- **Consumed-count = 0** of GT localization/evidence drove the edit.
- **Fair-probe = partial.** No E/W code, but the literal `Sid` term in the issue lets the agent grep to the iam schema area. GT got no causal credit (wrong files, not consumed).

## Failure locus (why it did not resolve)
**Wrong-file / incomplete-fix**, downstream of a localization-recall gap. The gold fix targets the IDENTITY-policy path (`policy_identity.json` Sid pattern + `IdentityPolicy.py` registering `IAM::UserPolicy/RolePolicy/GroupPolicy`); the FAIL_TO_PASS test is `IdentityPolicy::test_pattern_sid`. The agent added the Sid pattern to `policy.json` (base/resource schema) only — which does NOT propagate to the identity-policy validation the test exercises. The agent never reached `IdentityPolicy.py` or `policy_identity.json`. **GT's contribution gap:** GT's call-graph indexes Python; the two JSON-schema gold files are not graph nodes, so GT structurally cannot rank them — a recall limitation for data-file-driven fixes (per gt_gt §10 "relationship edges language-uneven / data-file fixes out of scope").

**right_trajectory (GT) = FALSE** (localization wrong, not consumed, gold partly outside graph reach). gt_caused = FALSE. flip = no.
