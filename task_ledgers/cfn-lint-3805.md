# Ledger — aws-cloudformation__cfn-lint-3805  (run gt-trial 30-task, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-3805"]`; run hit `RuntimeError: Agent reached maximum iteration … 100`), baseline_pass=**no** (NOT in `full300_baseline_ohdeepseek_20260531` `resolved_ids`; flip-candidate), flip=**no**. GOLD = `src/cfnlint/rules/resources/properties/StringLength.py` — gold rewrites `maxLength`/`minLength` to use `is_function(instance)` (not `is_type==object and len==1`) AND `if "object" in ensure_list(schema.get("type"))` (not `schema.get("type")=="object"`).

**One-line trajectory finding (lead with this):** **right_trajectory = FALSE — and GT did not localize this one.** The agent **self-localized AND self-diagnosed**: it ignored all six L1 candidates (none was the gold), found `StringLength.py` by its own `grep maxLength` (#20/#76), and discovered the actual bug by RUNNING the code (#180: "`schema.get("type")` returns `['object','string']`… so `== "object"` is False"). GT's post-view DID fire on the gold file with correct-but-generic contracts (`_non_string_max_length`/`_serialize_date`) that never named the `schema.get("type")` string-vs-list bug. The agent then wrote a **partial fix** (added the `ensure_list`-equivalent `type` normalization but MISSED the gold's `is_function` refactor of the `len(instance)==1` branch), so it did not resolve. Post-localization implementation miss, on a file GT did not point to. No leakage anywhere.

---

## PREREQS (substrate, 8-dp verbatim from `gt_gates_deep_aws-cloudformation__cfn-lint-3805.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 73.84259259` (deterministic_edges `2552.0` / calls_edges `3456.0`) | GREEN (`pred_A_det_floor=true`, floor 15.0) | telemetry-only; reaches the agent ONLY as resolved-edge lines inside the L1 brief + `[GT]`/`<gt-context>` post-view headers |
| **P1** name_match | `name_match_edges = 904` (of 3456 → 26.16% name_match) | GREEN (`pred_B_nondominance=true`) | telemetry-only; never rendered as a number |
| **P1** typing tiers | `typing_fired=true`; `type_flow=225`, `impl_method=219`, `inherited=149`, `ev:assignment_tracked=200` | GREEN (`pred_C_typing=true`) | telemetry-only |
| **P2** calls_edges / breakdown | `calls_edges=3456.0`; `name_match=904, same_file=611, import=603, verified_unique=545, type_flow=225, impl_method=219, inherited=149, lsp=133, return_type=66, unique_method=1` | GREEN (`gate_resolution.pass=true`) | telemetry-only; surfaces only as concrete call/caller lines in L1 + post-view |
| **P2** LSP enrichment | `gate_lsp`: `resolved_promoted=133.0`, `residual=88.0`, `attempted_edges=181.0`, `graph_lsp_edges=133`, `verdict=LSP_ACTIVE_VALID`, `probe_latency_ms=1.40953064` | GREEN (`gate_lsp.pass=true`) | telemetry-only; the 133 `lsp` edges fold into the resolved-edge lines |
| **P3** embedder present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN (`present.pass=true`) | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15`, `semantic_signal_count=2`, `sem_max=0.83439100`, `sem_median=0.00000000`, `sem_separation_gap=0.83439100`, `sem_frac=0.50000000`, `pred_2_coverage=false` | GREEN (`mode=present_and_consumption`, `pass=true`) | telemetry-only; reorders the L1 candidate list only |

**Prereqs verdict:** ALL GREEN (`verdict: {resolution_jarvis:true, lsp_enrichment:true, embedder:true, all_on:true}`). Substrate healthy (73.8% deterministic, name_match non-dominant, embedder live+consumed). These numbers reached the agent ONLY as resolved-edge lines — and on this task those lines pointed at the WRONG files (see L1). The substrate is not the failure; localization is.

---

## L1 localizer (`<gt-localization>` + `<gt-task-brief>` + `<gt-graph-map>` + `<gt-orientation>` + `[GT KEY CONTRACTS]`, in the instruction)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 (instruction) | `<gt-localization confidence="medium">` / `1. src/cfnlint/rules/_rule.py` / `2. …/_sam.py` / `3. …/_language_extensions.py` / `4. …/_rules.py` / `5. …/match.py` / `6. …/NumberRange.py` — **gold `StringLength.py` is NOT in the list**. `<gt-task-brief>` ranks `_rule.py` primary; `[GT KEY CONTRACTS]` Preserve `conditional_return: if isinstance(obj, _SCALAR_TYPES): return _ForEachValue` (from `_language_extensions.py`, not the gold). | IDX 6: `find … -name "*.py"`; IDX 10-30: agent reads `iam/Policy.py`, `iam/PolicyVersion.py`, `ApproachingMaxProperties.py`, `MaxProperties.py`, then `grep -l "MaxLength\|maxLength…"` (#20) → reaches **`StringLength.py` (#30)** by its OWN grep. It NEVER opened any of GT's 6 candidates. | **D**=Y · **C**=**N** (gold not among the 6 candidates) · **C**=**N** (agent ignored all 6, self-localized) |

**L1 verdict:** D/C/C = **Y/N/N**, leak=0. L1 mislocalized (medium confidence, all 6 wrong) and the agent disregarded it entirely. Non-harmful (the agent did not chase any wrong candidate to wasted depth — it went to IAM files from the issue then grep'd `maxLength`), but L1 added zero localization value here.

## L3b post-view (`[GT]` / `<gt-context>` file-view enrichment)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 13 | Prepended to the `iam/Policy.py` view: `[GT] Policy:` / `[CATCHES] except json.JSONDecodeError -> returns: return` (real, from Policy.py). | IDX 14-29: agent keeps reading other IAM files; the Policy.py contract did not redirect it (Policy.py is not the bug). | D=Y · C=Y (accurate contract for a non-gold file) · C=partial (read, not load-bearing) |
| IDX 31 | Prepended to the **GOLD** `StringLength.py` view: `[GT] StringLength:` / `[CONTRACT] def _non_string_max_length(self, instance, mL):` / `[CONTRACT] def _non_string_min_length…` / `[CONTRACT] def _serialize_date…` / `[CONTRACT] flows: instance -> self._remove_functions(instance)` / `Spec: _remove_functions handles…` — then the full real `cat -n` body. **Does NOT mention `maxLength`/`minLength` or the `schema.get("type")=="object"` bug.** | IDX 54 (`think`): "the maxLength validation using `StringLength` (E3033) doesn't correctly handle the case when PolicyDoc…" — agent reasons over the BODY it received, not the GT contract header. | D=Y · C=Y (real contracts, gold file reached, no fabrication) · C=partial (agent consumed the file body; the GT contract HEADER was generic and not the bug locus) |
| IDX 183 | On a re-view: `<gt-context file="StringLength.py">` with the same `[CONTRACT]` payload, then editor `ERROR: Invalid view_range … 123 should be smaller than … 122` (editor error, not GT). | IDX 184-185: agent re-reads with a valid range. | D=Y · C=Y · C=N (header not acted on) |

**L3b verdict:** D/C/C = **Y/Y/partial**, leak=0. Post-view DID deliver the gold file's REAL contracts with no fabrication and no test leakage — so DELIVERED+CORRECT hold for the gold file. But the contracts surfaced (`_non_string_max_length`, `_serialize_date`, `_remove_functions`) were the *helper* methods, NOT the buggy `maxLength`/`minLength` type-check; the agent localized the bug by RUNNING the code (IDX 180), not from GT's header. CONSUMED of the GT-specific signal = NO.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 13 | `<gt-scope files="6">` / `1. iam/Policy.py — in scope (you are viewing this)` / `2. context/context.py — verified by language server` / `3. cfnlint/helpers.py — imported` / `4. jsonschema/protocols.py — verified…` / `5. …CfnLintJsonSchema.py — graph-connected` / `These files are related in scope; GT has not confirmed a single primary target — confirm the edit target with grep.` | Agent did NOT treat any of these as the target; it grep'd `maxLength` and found `StringLength.py` (not in the scope list). The honest "no single primary target — confirm with grep" was respected. | D=Y · C=partial (the 5 scoped files do NOT include the gold; but it explicitly abstains) · C=N (agent self-localized elsewhere) |

**consensus verdict:** D/C/C = **Y/partial/N**, leak=0. Correct-or-quiet held (explicit "GT has not confirmed a single primary target"), so no misdirection — but the scope set missed the gold and contributed no localization.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 193 | After the `StringLength.py` edits: `[GT_VERIFY] You edited 1 file(s). Before finishing, run the project's own test suite … and confirm your change preserves the behavioral contract:` / `StringLength.py: guard_clause = return: isinstance(obj, datetime.date) -> return obj.isoformat()` / `… isinstance(obj, dict) -> new_obj = {}` / `… len(json.dumps(j, separators=(",", ":"), default=self._serialize_date))` — **no test names**. | IDX 194: agent ran `pytest … test_string_length.py`; then continued investigating `aws_iam_policy` schemas (IDX 196-203) until iteration cap. | D=Y · C=Y (real guard-clause contracts, zero FAIL_TO_PASS leakage) · C=Y (agent ran the suite) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. Surfaced real behavioral contracts (guard clauses), no test identifiers. The agent complied. It could not catch the incomplete fix because the workspace test file predates the gold `test_patch` (`test_max_length`/`test_min_length` cases) — GT cannot inject hidden gold tests (that would be leakage).

## L4 / L5 / L5b / L6

| turn | GT SENT (verbatim) | AGENT DID | D/C/C |
|---|---|---|---|
| — | **DELIVERED=NO** — no `gt_validate`/`gt_navigate` invocation in this trajectory; no `GT_META`, `[GT_CURATION]`, `dedup=`, or L5/L5b/L6 payload appears in any of the 204 history events' observation content (`l5_telemetry.jsonl` has 46 lines on disk but is telemetry-only — never reached the agent per the AGENT-OBSERVATION rule). | n/a | n/a — not delivered to the agent |

**L4/L5/L5b/L6 verdict:** DELIVERED=NO for all four; nothing to consume; leak=0.

---

## Cross-component line

leakage=**0** (GT surfaced no test name / FAIL_TO_PASS — verified by chronological read; the only `test_*.py` strings in history are the agent's own `pytest` collection output) · delivered-to-agent components=**4** (L1, L3b, consensus, GT_VERIFY) · consumed (GT-specific signal acted on)=**0** (agent self-localized via grep + self-diagnosed by running the code; no GT payload changed a decision) · fair-probe=**N/A (GT did not localize)** — GT's L1/scope did not name the gold, so there is no GT-localization to test for fairness; the agent reached gold from the issue's `maxLength`/`6144`/IAM cues. **right_trajectory = FALSE** (GT delivered correct-but-generic contracts on the gold file but did not point to it or to the bug; the agent's localization and the partial fix were its own). gt_caused = **FALSE**.
