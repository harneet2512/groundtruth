# Ledger — aws-cloudformation__cfn-lint-4009  (run 27214152241, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-4009"]`), baseline_pass=**no** (flip-CANDIDATE), flip=**no**. GOLD = THREE files: `src/cfnlint/rules/conditions/Used.py` + `src/cfnlint/template/template.py` + `src/cfnlint/template/transforms/_language_extensions.py` (gold registers `transform_pre["Fn::If"] = self.search_deep_keys("Fn::If")` in `template.py`, switches `Used.py` to read `cfn.transform_pre["Fn::If"]`, and adds a `_ForEachValueFnIf` class in `_language_extensions.py`). Issue = E0001 on `Fn::ForEach` + `Fn::If`.

**One-line trajectory finding (lead with this):** **right_trajectory = FALSE for GT.** The agent reached ONE of the three gold files (`_language_extensions.py`) early, but by its own issue-driven reasoning (EVENT 10 `[thought] Now let me look at the key files related to transforms, especially the language extensions transform where Fn::ForEach is processed`), not from GT. GT's L1 (medium) *did* list `_language_extensions.py` as candidate #3, but the brief's PRIMARY ranking (`<gt-task-brief>` #1, `<gt-graph-map>`, EDIT-TARGET CONTRACTS) all pointed at the NON-gold `context.py` — so GT's headline target was wrong, and the gold's other two files (`template.py`, `Used.py`) were never named. **Attribution to GT is not establishable** (the issue's `Fn::ForEach`/`Fn::If` mechanism leads to the same file). The agent edited only `_language_extensions.py` (1 of 3 gold) and wrote a structurally divergent fix → no resolve.

---

## PREREQS (substrate, 8-dp verbatim from gate-deep + certs)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 76.38386648` (deterministic_edges `2746.0` / calls_edges `3595.0`) | GREEN | telemetry-only; only as resolved-edge lines in the brief |
| **P1** name_match | `name_match_edges = 849` (23.6%) | GREEN (non-dominant) | telemetry-only |
| **P1** typing tiers | `typing_fired=true`; `type_flow=244`, `impl_method=266`, `inherited=141`, `return_type=77` | GREEN | telemetry-only |
| **P2** calls / breakdown | `calls_edges=3595`; `name_match=849, import=650, same_file=622, verified_unique=561, impl_method=266, type_flow=244, lsp=184, inherited=141, return_type=77, unique_method=1` | GREEN | only as call/caller lines |
| **P2** LSP | `server_launched=true`, `warm_probe_ok=true`, `attempted=257`, `verified=118`, `corrected=66`, `deleted=0`, `failed=27`, `residual=151`, verdict `LSP_ACTIVE_VALID` | GREEN | 184 lsp edges fold into resolved lines |
| **P3** embedder present | `class=EmbeddingModel`, dim=384, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15000000`, `rendered_semantic_nonzero=109/117`, `upstream_semantic_nonzero=342`, mode `present_and_consumption` | GREEN | re-orders L1 list only |

**Prereqs verdict:** gate-deep `all_on:true` — ALL GREEN. **Provenance caveat:** `graph_certificate` verdict `GRAPH_FAIL_MISSING_HANDOFF` (`hook_graph_hash=null`). Substrate healthy; the brief delivered. Not the cause of the miss.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 1 | `<gt-localization confidence="medium">` / `  1. src/cfnlint/context/context.py — Transforms, __post_init__, has_language_extensions_transform` / `  2. …elasticloadbalancingv2/ListenerCertificate.py` / `  3. src/cfnlint/template/transforms/_language_extensions.py — value` (← one of 3 gold files) / `  4. src/cfnlint/api.py` / `  5. …decode/cfn_yaml.py` / `  6. …jsonschema/CfnLintJsonSchema.py`. The `<gt-task-brief>` HEADLINE #1 = `context.py` (non-gold) and the EDIT-TARGET CONTRACTS are all for `context.py`. `template.py` and `Used.py` (the other 2 gold files) are ABSENT. | EVENT 8-11: agent lists `src/cfnlint`, then `template/transforms/`; EVENT 10 `[thought] …especially the language extensions transform where Fn::ForEach is processed` → EVENT 12 opens `_language_extensions.py`. The thought cites the issue mechanism (`Fn::ForEach`), not GT's candidate list. | **D**=Y · **C**=partial (1 of 3 gold files appears at rank #3; headline #1 + EDIT-TARGET CONTRACTS point at non-gold `context.py`; 2/3 gold files missing) · **C**=ambiguous (agent reached the file but cites issue-derived reasoning, not GT) |

**L1 verdict:** D/C/C = **Y/partial/ambiguous**, leak=0. L1 grazed one gold file but its headline ranking was wrong and it missed two-thirds of the gold scope. Not a clean GT win; attribution unprovable.

## L3b post-view (`[GT]` enrichment) + consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 13 | Prepended to the `_language_extensions.py` view: `[GT] _language_extensions:` / `[CONTRACT] def transform(self, cfn: Any) -> TransformResult:` / `[CONTRACT] def value(` / `[RAISES] WHEN len(obj) != 1: raise _ValueError(...) | … | WHEN not isinstance(obj, list): raise _TypeError("Fn::FindInMap should be a list", obj)` / `Spec: value handles: except _ResolveError:` …; then the consensus block `<gt-scope files="3">` / `1. transforms/_language_extensions.py — in scope (you are viewing this)` / `2. conditions/_utils.py — imported` / `3. transforms/transform.py — shares transform` / `…GT has not confirmed a single primary target — confirm the edit target with grep.` | EVENT 14-15: agent greps for `_ForEachValue`/`Fn::If` classes WITHIN the file it is viewing; it reasons from the file body. The `<gt-scope>` adjacent files (`_utils.py`, `transform.py`) are NOT the missing gold files (`template.py`, `Used.py`) — so consensus did not surface the missing scope. | L3b: **D**=Y · **C**=Y (real contracts/raises, no fabrication) · **C**=Y (agent consumed the file). consensus: **D**=Y · **C**=partial (scope #1 = the file the agent is on, but the 2 adjacent files are NOT the missing gold; correct-or-quiet honesty) · **C**=N (agent stayed in-file; consensus did not expand to gold scope) |

**L3b/consensus verdict:** L3b D/C/C = **Y/Y/Y** leak=0; consensus D/C/C = **Y/partial/N** leak=0. Post-view gave correct in-file contracts. Consensus correctly abstained from a single primary target but its adjacency set did not include the two gold files the agent needed (`template.py`, `Used.py`) — a localization-recall gap, not a harm.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| (GT_VERIFY block fires post-edit; agent also runs the local suite) | `[GT_VERIFY] You edited 1 file(s). Before finishing, run the project's own test suite for the affected modules…` listing real `_language_extensions.py` contracts (no test names). | Agent ran `pytest test_language_extensions.py` (passed locally) — but that suite predates the gold `test_patch` (`TestFnIf::test_fn_if`), so it gave false confidence. | **D**=Y · **C**=Y (no FAIL_TO_PASS leaked) · **C**=Y (agent ran suite) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. Correct affected-module advisory, no leakage. Could not catch the wrong fix (the gold test is injected later by the harness).

## L4 / L5 / L5b / L6
Active per `gt_run_summary` (telemetry). No agent-observable governor text changed the trajectory. No observable consumption.

---

## Cross-component line
- **Test-name / FAIL_TO_PASS leakage = 0** (full chronological read of 113 events; `test_language_extensions.py::TestFnIf::test_fn_if` never surfaced by GT).
- **Consumed-count = 0** of GT's localization drove the edit decision (post-view contracts were consumed as file content, but the localization headline was wrong and the agent's file choice was issue-driven).
- **Fair-probe = WEAK/NO.** The issue names the `Fn::ForEach`/`Fn::If` mechanism, which leads to `_language_extensions.py` directly; GT-vs-self attribution is unprovable.

## Failure locus (why it did not resolve)
TWO compounding failures, both post/around-localization, NOT a substrate failure:
1. **Incomplete scope** — gold needs 3 coordinated files; the agent (and GT) only touched `_language_extensions.py`. `template.py` (`transform_pre["Fn::If"]`) and `Used.py` (read from `transform_pre`) were never reached. GT named neither.
2. **Wrong fix logic** — even within `_language_extensions.py`, the agent's `_ForEachValueFnIf` diverges from gold: agent adds `isinstance(obj, (list, _SCALAR_TYPES))` to the scalar branch (NOT in gold) and stores `self._condition = obj[0]` + true/false values; gold does `_ForEachValue.create(obj[0])` and stores only the condition. Structurally different → likely fails `TestFnIf::test_fn_if`.

**right_trajectory (GT) = FALSE** (headline localization wrong, 2/3 gold scope missed, GT-attribution unprovable). gt_caused = FALSE. flip = no.
