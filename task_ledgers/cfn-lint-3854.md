# Ledger — aws-cloudformation__cfn-lint-3854  (run gt-trial 30-task, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-3854"]`), baseline_pass=**no** (flip-candidate), flip=**no**. GOLD = `src/cfnlint/template/transforms/_language_extensions.py` — gold threads `params` through `foreach.items(cfn, params)` → `_ForEachCollection.values(cfn, collection_cache, params)` → `self._fn.value(cfn, params, False)` so nested `Fn::ForEach` collections resolve with outer-loop params (fixing the spurious W8001).

**One-line trajectory finding (lead with this):** **right_trajectory = FALSE (GT did not localize), but this is the closest the agent came to the gold mechanism.** The agent self-localized: `grep W8001` (#10) → `Used.py` → `grep ForEach` (#16) → `_language_extensions.py` (#18, the gold). GT's L1 (confidence="low") ranked six WRONG files (`runner.py`, `ForEach.py`, `_rule.py`, … — gold absent). The agent then independently traced the `_walk → items → values → _fn.value` chain (#88: "propagate `params` from `_walk()` down to where the collection values are resolved") and wrote a fix that is **functionally the SAME mechanism as gold** (`items(cfn, params)` → `values(..., params)` → `item.value(cfn, params, False)`). Despite this, it was scored unresolved — a subtle mismatch with the gold's exact param-threading / the FAIL_TO_PASS `test_valid` case. GT's post-view delivered correct contracts on the gold file but also **leaked one garbage caller line** (`Called by: …_rule.py:119 'return hash(...)'` — a `__hash__` body, not a caller of `transform`). No test-name leakage.

---

## PREREQS (substrate, 8-dp verbatim from `gt_gates_deep_aws-cloudformation__cfn-lint-3854.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 77.09111814` (det `2682.0` / calls `3479.0`) | GREEN (`pred_A_det_floor=true`) | telemetry-only → resolved-edge lines |
| **P1** name_match | `name_match_edges = 797` (22.91%) | GREEN (`pred_B_nondominance=true`) | telemetry-only |
| **P1** typing tiers | `typing_fired=true`; `type_flow=227`, `impl_method=255`, `inherited=150`, `ev:assignment_tracked=202` | GREEN (`pred_C_typing=true`) | telemetry-only |
| **P2** calls / breakdown | `calls_edges=3479.0`; `name_match=797, same_file=617, import=610, verified_unique=547, impl_method=255, type_flow=227, lsp=200, inherited=150, return_type=75, unique_method=1` | GREEN (`pass=true`) | telemetry-only |
| **P2** LSP | `resolved_promoted=200.0`, `residual=196.0`, `attempted=274.0`, `graph_lsp_edges=200`, `verdict=LSP_ACTIVE_VALID` | GREEN | telemetry-only |
| **P3** present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** consumption | `effective_w_sem=0.15`, `semantic_signal_count=3`, `sem_max=0.83713900`, `sem_median=0.77721300`, `sem_separation_gap=0.05992600`, `sem_frac=0.50000000`, `pred_2_coverage=true` | GREEN (`pass=true`) | telemetry-only |

**Prereqs verdict:** ALL GREEN (`all_on:true`). Highest deterministic fraction of the six (77%); semantic coverage fired. Substrate is not the failure; the resolved-edge lines pointed at non-gold files (see L1).

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 (instruction) | `<gt-localization confidence="low">` / `1. src/cfnlint/runner.py` / `2. …/ForEach.py` / `3. …/_rule.py` / `4. …/template.py` / `5. …/PrimaryIdentifiers.py` / `6. …/match.py` — **gold `_language_extensions.py` NOT listed.** `<gt-task-brief>` #1 = `runner.py`; a `Scope chain` lists `runner.py → template.py → _rules.py → config.py → _rule.py → match.py` (none gold). | IDX 4 (`think`): restates the W8001/ForEach problem; IDX 10: `grep W8001` → `Used.py` (#12); IDX 16: `grep ForEach` → **`_language_extensions.py` (#18, gold)**. Did not open any L1 candidate as the fix target. | **D**=Y · **C**=**N** (gold absent) · **C**=**N** (agent self-localized via grep) |

**L1 verdict:** D/C/C = **Y/N/N**, leak=0. Mislocalized (low confidence, all 6 wrong). Note `ForEach.py` (#2) is topically adjacent but is the rule, not the transform that holds the bug — the agent correctly went past it to `_language_extensions.py`.

## L3b post-view

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 19 | Prepended to the GOLD `_language_extensions.py` view: `[GT] _language_extensions:` / `[CONTRACT] def transform(self, cfn: Any) -> TransformResult:` / `[CONTRACT] def language_extension(cfn: Any) -> TransformResult:` / `[CONTRACT] flows: cfn -> cfn.template | self._walk(cfn.template, {}, cfn)` / `transform() … Called by: language_extension() …:52` / **`Called by: src/cfnlint/rules/_rule.py:119 'return hash((self.path, self.message))'`** ← GARBAGE caller (a `__hash__` body, NOT a caller of `transform`). | IDX 26/34/40/88 (`think`): agent traces `runner.run → cfn.transform → language_extension → _Transform().transform → _walk` from the BODY it read, then targets `foreach.items(cfn)` for param-threading — its own trace, not the GT caller line. | D=Y · **C=partial** (contracts for `transform`/`language_extension` are real, but the `_rule.py:119` "Called by" line is a FALSE caller edge — the one CORRECTNESS defect found) · C=partial (file body consumed; the garbage caller line was ignored, so no harm) |

**L3b verdict:** D/C/C = **Y/partial/partial**, leak=0 (no test names). DELIVERED held; CORRECT is partial because of the bogus `_rule.py:119 __hash__` "Called by" line — a name_match/hash artifact rendered as a real caller. The agent ignored it (no harm), but it is a real correctness blemish to log.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 19 | `<gt-scope files="N">` listing `_language_extensions.py — in scope (you are viewing this)` + graph-connected siblings + `… GT has not confirmed a single primary target — confirm the edit target with grep.` | Agent stayed on `_language_extensions.py`; read `template.py`/`transform.py`/`_rules.py` (#20-39) to understand the call chain, then returned to gold to edit. | D=Y · C=Y (scope #1 = gold, since the agent was viewing it) · C=partial (confirmed the agent's own choice) |

**consensus verdict:** D/C/C = **Y/Y/partial**, leak=0. Named the gold file in-scope, abstained from over-claiming; no misdirection.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| (post-edit, after #96) | `[GT_VERIFY] You edited 1 file(s). … confirm your change preserves the behavioral contract:` with `_language_extensions.py` real exception/guard contracts — **no test names**. | IDX 104-129: agent ran `pytest test_used.py`, `test_for_each.py`, `test_language_extensions.py`, broad `test/unit/` sweeps; flagged a pre-existing integration failure (#120) and dismissed it. | D=Y · C=Y (no leakage) · C=Y (ran the suites) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. Agent verified extensively. The FAIL_TO_PASS `test_language_extensions.py::TestForEachCollection::test_valid` was not in the agent's pre-`test_patch` workspace, so the param-threading detail that distinguishes the agent's fix from gold went uncaught.

## L4 (`gt_validate`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 142/144/146 | `gt_validate unknown` × 3 → each `# gt_validate: unknown` / `(file not in worktree … nothing to validate)` (agent repeatedly passed literal `unknown`; all no-ops). | IDX 148+: agent re-reads the gold file. | D=N (no-op arg; never validated a real file) |

**L4 verdict:** DELIVERED=NO useful payload — the agent never supplied a real path to `gt_validate` (3 wasted `unknown` calls). leak=0.

## L5 / L5b / L6

| turn | GT SENT | AGENT DID | D/C/C |
|---|---|---|---|
| — | **DELIVERED=NO** — no `GT_META`/`[GT_CURATION]`/`dedup=`/L5/L5b/L6 payload in any observation (`l5_telemetry.jsonl` = 41 lines, telemetry-only). | n/a | n/a |

**L5/L5b/L6 verdict:** DELIVERED=NO; leak=0.

---

## Cross-component line

leakage=**0** (no test name / FAIL_TO_PASS surfaced by GT) · delivered-to-agent components=**4** (L1, L3b, consensus, GT_VERIFY); L4 fired but no-op · consumed (GT-specific signal acted on)=**0** (agent self-localized via grep and self-traced the param chain; no GT payload changed a decision) · fair-probe=**N/A (GT did not localize)**; the agent reached gold from the issue's `W8001`+`ForEach` cues. **CORRECTNESS defect logged:** L3b "Called by: `_rule.py:119 'return hash(...)'`" is a FALSE caller edge (name_match/hash artifact). **right_trajectory = FALSE** — GT did not point to the gold file; the agent's correct-mechanism fix was its own and still missed the exact gold semantics. gt_caused = **FALSE**. (Notable: the agent's fix is the closest of the six to the true gold mechanism — evidence the bottleneck is post-localization implementation precision, not the context layer.)
