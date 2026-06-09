# Ledger — aws-cloudformation__cfn-lint-4032  (run 27214152241, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-4032"]`), baseline_pass=**no** (flip-CANDIDATE), flip=**no**. GOLD = `src/cfnlint/rules/resources/iam/StatementResources.py` (I3510; gold restructures the inner ARN-format loop into a `for…else` with `break` so the rule yields the "requires a resource of {arn_formats!r}" error only when NO arn_format matched). Issue = "I3510 false positives for ec2:CreateTags" (rule code I3510 in the title).

**One-line trajectory finding (lead with this):** **right_trajectory = FALSE for GT.** GT MISLOCALIZED (L1 LOW ranked `runner.py`, `PrimaryIdentifiers.py`, `_rules.py`, `_rule.py`, `helpers.py`, `config.py` — **none is `StatementResources.py`**). The agent self-localized via `grep -rn "I3510"` (the code is in the issue title) at EVENT 8, landed on the gold file in ONE action, identified the correct root cause (ALL→ANY ARN matching), but wrote a **structurally divergent fix** (a `resource_match_found` flag + a concatenated `all_arn_formats` error message) vs the gold's compact `for…else`/`break` with per-resource `arn_formats` message → no resolve. GT was inert and wrong; the failure is post-localization implementation correctness.

---

## PREREQS (substrate, 8-dp verbatim from gate-deep + certs)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 76.70485543` (deterministic_edges `2812.0` / calls_edges `3666.0`) | GREEN | resolved-edge lines in brief only |
| **P1** name_match | `name_match_edges = 854` (23.3%) | GREEN (non-dominant) | telemetry-only |
| **P1** typing tiers | `type_flow=250`, `impl_method=257`, `inherited=142`, `return_type=72`, `typing_fired=true` | GREEN | telemetry-only |
| **P2** calls / breakdown | `calls_edges=3666`; `name_match=854, import=663, same_file=629, verified_unique=586, impl_method=257, type_flow=250, lsp=212, inherited=142, return_type=72, unique_method=1` | GREEN | call/caller lines only |
| **P2** LSP | `server_launched=true`, `attempted=312`, `verified=83`, `corrected=129`, `deleted=0`, `failed=47`, `residual=230`, verdict `LSP_ACTIVE_VALID` | GREEN | 212 lsp edges fold in |
| **P3** embedder present | `class=EmbeddingModel`, dim=384, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15000000`, `rendered_semantic_nonzero=110/122`, `upstream_semantic_nonzero=345`, mode `present_and_consumption` | GREEN | re-orders L1 list only |

**Prereqs verdict:** gate-deep `all_on:true` — ALL GREEN. **Provenance caveat:** `graph_certificate` verdict `GRAPH_FAIL_MISSING_HANDOFF`. Substrate healthy.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 1 | `<gt-localization confidence="low">` / `Region: src/cfnlint/ — candidate edit targets (reason over these, confirm with grep):` / `  1. src/cfnlint/runner.py` / `  2. …resources/PrimaryIdentifiers.py` / `  3. src/cfnlint/rules/_rules.py` / `  4. src/cfnlint/rules/_rule.py` / `  5. src/cfnlint/helpers.py` / `  6. src/cfnlint/config.py` (gold `iam/StatementResources.py` ABSENT) | EVENT 8 `grep -rn "I3510" .../src/cfnlint/` → EVENT 9 obs `.../iam/StatementResources.py:79: id = "I3510"` → EVENT 10 opens GOLD. None of GT's 6 candidates opened. | **D**=Y · **C**=N (0/6 gold; LOW conf, honest) · **C**=N (self-localized via I3510) |

**L1 verdict:** D/C/C = **Y/N/N**, leak=0. MISLOCALIZED; honest LOW + "confirm with grep". Inert.

## L3b post-view (`[GT]` enrichment)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 11 | Prepended to the gold-file view: `[GT] StatementResources:` / `[CONTRACT] def __init__(self, full_arn: str):` / `[CONTRACT] def __repr__(self):` / `[CONTRACT] def parts(self):` / `[CONTRACT] flows: full_arn -> full_arn.split` | EVENT 11+: agent reads the full file body and reasons over the `validate` ARN-matching loop. | **D**=Y · **C**=partial (the 3 contracts shown are the `_Arn` HELPER class's `__init__`/`__repr__`/`parts`, NOT the actual `StatementResources.validate` rule contract — incomplete but not fabricated) · **C**=N (agent reasoned from the file body, not these helper headers) |
| ~EVENT (line 1106) | `[GT] StatementResources.py was confirmed earlier. Key evidence: [CONTRACT] def __init__(self, full_arn: str):` | re-view; same helper contract; not cited. | **D**=Y · **C**=partial · **C**=N |

**L3b verdict:** D/C/C = **Y/partial/N**, leak=0. Post-view fired on the gold file but surfaced the `_Arn` helper's contracts rather than the rule's `validate` contract (a granularity/selection miss — the most relevant function was not the one summarized). No fabrication, no leak; inert.

## consensus (`<gt-scope>`)
`<gt-scope>` not observed as an agent-visible redirect; agent already on gold. No consumption.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 61 | `[GT_VERIFY] You edited 1 file(s). Before finishing, run the project's own test suite for the affected modules…:` / `  StatementResources.py: guard_clause = return: not isinstance(value, _Arn) -> return False` / `  StatementResources.py: guard_clause = return: self.parts[5] == "*" -> return True` / `  StatementResources.py: guard_clause = return: not validator.is_type(instance, "object") -> return` / `  StatementResources.py: return_shape = value|":".join(self._parts)` | EVENT 62+: agent ran `cfn-lint -c I3510` repro (passed) and verified a wrong-resource still errored; later ran the suite. | **D**=Y · **C**=Y (real guard_clauses from the gold file; **no FAIL_TO_PASS / test name leaked**) · **C**=Y (agent ran verification) |
| EVENT (line 1921) | `CALLER-BLIND-EDIT    symbol=validate  callers=5  (no test file edited alongside this change)` | EVENT 1926 `[thought] The CALLER-BLIND-EDIT is informational because the test file already has tests…` — read, declined to edit tests. | **D**=Y · **C**=Y (5 callers real, no leak) · **C**=Y |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. Correct affected-module advisory + real contracts, no leakage, agent complied. Could not detect the wrong fix (no leakage allowed; the gold test is injected later).

## L4 / L5 / L5b / L6
Active per telemetry; no observable governor effect changed the trajectory.

---

## Cross-component line
- **Test-name / FAIL_TO_PASS leakage = 0** (chronological read of 99 events; `test_statement_resources.py::test_rule[instance11-expected11]` never surfaced).
- **Consumed-count = 0** of GT localization/evidence drove the fix logic. GT_VERIFY's contracts were read but the edit decision came from the agent's own analysis of the ARN loop.
- **Fair-probe = NO (BAD PROBE).** Issue title prints `I3510`; `grep I3510` → gold in one action.

## Failure locus (why it did not resolve)
Post-localization **implementation-correctness / wrong-fix**. The agent reached the gold file and the right root cause (the loop required ALL arn_formats to match; should be ANY), but its fix is structurally different from gold:
- **Gold:** converts the inner `for arn_format` loop to a `for…else` — `if arn in all_resource_arns: break` / `else: yield ValidationError(... {arn_formats!r} ...)` — error uses the per-resource `arn_formats`.
- **Agent:** introduces a `resource_match_found` flag, restructures both the resource and arn_format loops, and changes the error message to a concatenated `all_arn_formats!r`. Different control flow + different error string.
The FAIL_TO_PASS `test_rule[instance11-expected11]` almost certainly asserts the exact match count / error shape the gold's per-resource `for…else` produces; the agent's divergent logic and message likely fail it. This is downstream of every prereq and of localization — a context layer with no leakage cannot determine which valid-looking refactor matches the hidden test. (Same run-level env caveat as cfn-lint-4023: agent tested under py3.12 vs repo cpython-39.)

**right_trajectory (GT) = FALSE** (localization wrong, not consumed). gt_caused = FALSE. flip = no.
