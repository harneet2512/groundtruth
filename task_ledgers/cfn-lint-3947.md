# Ledger — aws-cloudformation__cfn-lint-3947  (run, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` unresolved), baseline_pass=**no** (flip-candidate), flip=**no**. GOLD = schema-DATA patch: add `"format": "json"` to `PolicyDocument` in `aws-iam-managedpolicy.json` (all regions) + a new `aws_iam_managedpolicy/policydocument.json` patch, REMOVING the `"type":["object","string"]` ambiguity. The StringLength rule (E3033) already keys on `format=="json"` to count COMPACT (whitespace-stripped) JSON length. FAIL_TO_PASS = `test_string_length.py::test_min_length[{"foo":…` + `test_max_length[…` — parametrized cases that pass schema `{"type":["string","object"],"format":"json"}`.

**One-line trajectory finding:** GT **MIS-localized** — L1 ranked `cfn_yaml.py::load`, `_language_extensions.py`, `_sam.py`, `decode.py`, `_rules.py`, `match.py`; the StringLength rule and the schema files are absent. The agent **self-localized** by grepping the issue's literal magic number `"6144"` (IDX 8) and chasing the `E3033` error to `StringLength.py` (IDX 22) — NOT from GT. The agent edited `StringLength.py` with a `_validate_json_string` helper, but **gated on the WRONG schema field**: it triggers when `"object" in schema.get("type")`, whereas gold triggers on `format=="json"`; and it **never added `"format":"json"` to the schema data files** (the gold's actual change). The FAIL_TO_PASS cases pass a schema carrying `format:json`, so the agent's `"object" in type` gate does not match the gold mechanism the test exercises. **GT did not localize this (gold is non-code schema data + a rule the agent reached on its own); the agent's fix is wrong-mechanism / wrong-gate-field.**

right_trajectory = **FALSE** (GT mislocalized; self-localized; wrong mechanism) · L1-ranked-gold = **NOT in top-6 (mislocalized — gold is schema data + StringLength.py, neither ranked)** · agent-reached-gold-area = **partial (reached StringLength.py via grep on "6144", but NOT the schema files / not the `format` gate)** · failure locus = **GT localization wrong + agent wrong-gate-field / wrong-mechanism fix**

---

## PREREQS (substrate, 8-dp verbatim from `gt_gates_deep_…-3947.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 74.03330511` (det `2623.0` / calls `3543.0`) | GREEN (floor 15.0) | brief resolved-edge lines only |
| **P1** name_match | `name_match = 920` (25.97%) | GREEN (non-dominant) | telemetry-only |
| **P1** typing tiers | `type_flow=229`, `impl_method=265`, `inherited=152`, `ev:assignment_tracked=204` | GREEN | telemetry-only |
| **P2** calls / breakdown | `calls=3543.0`; `name_match=920, import=634, same_file=626, verified_unique=549, impl_method=265, type_flow=229, inherited=152, lsp=91, return_type=76, unique_method=1` | GREEN | L1 + post-view edge lines |
| **P2** LSP | `verdict=LSP_ACTIVE_VALID`, `resolved_promoted=91.0`, `attempted=128.0`, `graph_lsp_edges=91` | GREEN | telemetry-only |
| **P3** embedder present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15000000` | GREEN (`present_and_consumption`) | re-orders L1 list |

**Prereqs verdict:** ALL GREEN (`all_on:true`). Substrate healthy. Irrelevant to the miss: this task's fix is in **schema JSON data**, which GT's graph (code-only) cannot rank at all — a structural blind spot of the localizer for data-driven rules.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 | `<gt-localization confidence="low">` / `  1. src/cfnlint/decode/cfn_yaml.py` / `  2. …/_language_extensions.py` / `  3. …/transforms/_sam.py` / `  4. …/decode/decode.py` / `  5. src/cfnlint/rules/_rules.py` / `  6. src/cfnlint/match.py` … `<gt-task-brief>` #1 = `cfn_yaml.py (def load…)`. **Neither the gold schema files nor `StringLength.py` (E3033) appear anywhere.** | IDX 8 (run): `grep -r "6144" …/src/ --include="*.py" -l` — the agent ignores GT's decode/yaml ranking and greps the issue's literal limit to find the size-check, eventually opening `rules/resources/properties/StringLength.py` at IDX 22. | **D**=Y · **C**=NO (gold = schema data + StringLength.py; GT ranked unrelated decode/transform files) · **C**=NO (agent ignored GT ranking; self-localized via grep on issue literal) |

**L1 verdict:** D/C/C = **Y / NO / NO**, leak=0. L1 delivered a list that contained neither the gold schema files (GT indexes code, not schema JSON) nor the `StringLength` rule. The agent self-localized to the rule via the issue's `6144` literal — independent of GT.

## L3b post-view (`[GT]` / `<gt-context>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 23 | On the (self-found) `StringLength.py` view: `[GT] StringLength:` / `[CONTRACT] def _fix_sub_string(self, instance):` / `[CONTRACT] def _non_string_max_length(self, instance, mL):` / `[CONTRACT] def _non_string_min_length(self, instance, mL):` / `flows: instance -> re.sub(r"\${…}", "", instance)`. (Real contracts for the rule the agent opened.) | IDX 90/91: agent adds `_validate_json_string` + edits `maxLength`/`minLength` to gate on `"object" in type`. It used the file's existing `_non_string_*` helpers as a template — consumed the post-view content. | **D**=Y · **C**=Y (real contracts for the opened file) · **C**=Y (consumed, modeled the new helper on the delivered `_non_string_*` contracts) |

**L3b post-view verdict:** D/C/C = **Y/Y/Y**, leak=0. Post-view correctly enriched the self-found rule file. But it could not steer the agent to the GOLD gate (`format=="json"` + the schema-data change) — it surfaced the rule's existing structure, which the agent extended along the wrong axis.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 23-region | `<gt-scope>` listing StringLength.py in-scope (viewing) + correct-or-quiet line; does NOT surface the schema files. | Agent stayed in StringLength.py. | **D**=Y · **C**=partial (in-scope file = the rule, but the gold schema-data half is absent) · **C**=Y |

**consensus verdict:** D/C/C = **Y/partial/Y**, leak=0. Scope reinforced the rule file but had no path to the schema-data half of the gold fix.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| post-edit | `[GT_VERIFY]`-class reminder to run the affected module's suite (StringLength contracts; no test names). | Agent ran `pytest test_string_length.py` (IDX 68/72/76/94) and reported pass — but the PRE-gold workspace test lacks the new `format:json` parametrized cases, so "pass" was false confidence. | **D**=Y · **C**=Y (no leak) · **C**=Y (ran suite) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. Same structural limit: the gold parametrized cases (schema with `format:json`) are injected at eval time; the agent's local suite couldn't catch the wrong gate field.

## L4 / L5 / L5b / L6

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| — | DELIVERED=NO — no L4 tool calls (`gt_validate` no-op only), no L5b/L6 markers in the 135-turn history. | n/a | n/a |

**L4/L5/L5b/L6 verdict:** DELIVERED=NO; leak=0.

---

## Cross-component line

- **Total test-name / FAIL_TO_PASS leakage = 0.**
- **Consumed-count:** L3b enriched the self-found rule file (consumed), but GT never surfaced the gold mechanism (`format=="json"` gate + schema-data `format:json`).
- **Fair-probe:** NO on localization (GT mislocalized; gold is schema DATA the code-only graph cannot rank; the agent self-localized the rule via grep on `"6144"`). The fix miss is wrong-gate-field (`"object" in type` vs `format=="json"`) + never touching the schema files — a wrong-mechanism fix.
- **Failure locus:** (1) GT localizer blind to schema-data fixes (structural); (2) agent wrong-mechanism / wrong-gate-field fix in a self-found rule file.
