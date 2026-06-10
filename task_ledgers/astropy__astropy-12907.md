# Ledger — astropy__astropy-12907

## 2026-06-10 PATH B trial (run 27260307167)

**Arm:** SWE-bench Verified × deepseek-v4-flash (temp=1.0), mini-swe-agent, GT-on.
Outcome: resolved=**YES** (official eval RESOLVED). Patch = `astropy/modeling/separable.py::_cstack` one-liner (`cright[...] = 1` → `= right`) — **exactly the gold fix**.
8-dp: `wall_clock_s=169.02382708`, `gt_injected_tokens_total=549.0`, `action_count=39.0`, `brief_chars=2197.0`.

**One-line trajectory finding:** resolve is **SELF-LOCALIZED, NOT GT-caused** — the issue text itself names `astropy.modeling.separable.separability_matrix`; the agent reproduced the bug numerically and hand-traced `_cstack` to the `= 1` copy-paste error (MSG 34-38). GT's L1 put the gold file only at **rank 2** (rank 1 = `modeling/core.py`, and EDIT-TARGET CONTRACTS focused `core.py`, the wrong file), with junk "resolved callers" from minified vendored jQuery. gt_caused = **FALSE**.

right_trajectory = **FALSE for GT-causation** (correct fix, agent's own reasoning; GT context was peripheral) · L1-ranked-gold = rank 2 (headline = non-gold core.py) · agent-reached-gold = YES (issue-driven) · failure locus = n/a (resolved).

### L1 brief / localization

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 1 | `<gt-localization confidence="medium">` … `1. astropy/modeling/core.py — inputs, outputs, separable / resolved caller: __init__() in astropy/io/ascii/fastbasic.py:40` / `2. astropy/modeling/separable.py — separability_matrix, is_separable, _compute_n_outputs / resolved call: -> where() in astropy/table/index.py:511` … `4. astropy/table/table.py — … resolved caller: gb() in astropy/extern/jquery/data/js/jquery.dataTables.min.js:9` … plus `EDIT-TARGET CONTRACTS (core.py): inputs -> calls _initialize_unit_support(self)` | MSG 2: "Let me start by understanding the issue and finding the relevant code." → runs the ISSUE's repro snippet (imports `astropy.modeling.separable` from the issue text), spends MSG 3-31 fighting the build env, MSG 32: "Let me look at the separable module directly" | D=Y · C=**PARTIAL** (gold file at rank 2; headline rank 1 + the contracts block point at non-gold `core.py`; "resolved caller" lines for table.py/representation.py are minified-jQuery junk) · C=**NO** (agent's path ran through the issue's own import line, not the brief ranking) |

**L1 verdict:** delivered, half-right, not consumed. Two brief-quality bugs visible to the agent: (a) vendored-JS callers (`gb() in jquery.dataTables.min.js:9`) presented as "resolved caller" facts; (b) edit-target contracts anchored on the wrong candidate (core.py).

### SCOPE (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 15/25 | `<gt-scope files="1"> 1. setup.py — in scope (you are viewing this); GT could not expand scope…` / `[GT] setup.py: also in GT scope.` | Agent was running pip/setup build attempts; setup.py "scope" is build-noise, not localization | D=Y · C=N (setup.py is not task scope) · C=N |
| MSG 41 | `<gt-scope reason="re-anchored"> 1. modeling/separable.py — you have moved here; re-grounding scope 2. tests/test_separable.py — graph-connected …` | Agent was already mid-fix on separable.py (it had identified the bug at MSG 38) | D=Y · C=Y (correct re-anchor + test file) · C=**WEAK** (agent later opened test_separable.py at MSG 61, consistent with scope but also the obvious move) |

**SCOPE verdict:** the re-anchored scope was correct but trailing — it confirmed where the agent already was.

### POST-VIEW evidence (`<gt-evidence kind="post_view">`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 33 | `[WITNESS] where calls -> astropy/table/index.py:511 'def where(self, col_map):'` / `[SIBLINGS] is_separable` (on viewing separable.py) | MSG 34: agent ignores it, starts hand-tracing `_cstack` matrices | D=Y · C=**N** (the `where` witness is an irrelevant/wrong edge for separable.py — table-index `where` has nothing to do with separability) · C=N |
| MSG 61 | `[WITNESS] _coord_matrix calls -> …separable.py:171` / `[WITNESS] _cdot calls -> …separable.py:250` (on viewing test_separable.py) | Agent checked for nested-compound tests, found none, moved to verify | D=Y · C=Y (real edges) · C=N |

### CONTRACT / post-edit (`<gt-contract>` + `kind="post_edit"`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 43 | `[SIGNATURE] def _coord_matrix(model, pos, noutp): [CALLERS] _coord_matrix: 3 verified caller(s) in 1 file(s) — preserve this interface` … BUT also `[CALLEE] _coord_matrix -> def isinstance( self: Self@TableColumns, cls: Any ) -> list (astropy/table/table.py:308)` and `[CALLEE] _separable -> def isinstance(…TableColumns…)` | Agent did not alter any signature (one-line value fix); proceeded to verification | D=Y · C=**PARTIAL** (signatures/caller counts real; the `isinstance -> TableColumns.isinstance` CALLEE lines are FALSE facts — builtin `isinstance` laundered to an unrelated class method, delivered as deterministic `[CALLEE]`) · C=N (fix unchanged by it) |

### NUDGE (`<gt-nudge>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 45 | `<gt-nudge reason="failure_persisted"> GT: the same test failure has persisted across your edit(s) — your current hypothesis is likely wrong. Re-read the failing assertion and reconsider the root cause / target file.` | The "failure" was an ENV import error (`_compiler` C-extension missing), not a test of the fix; the agent's hypothesis was already CORRECT (fix proven right at MSG 55/71). Agent rightly ignored the nudge and fixed the env | D=Y · C=**N** (false positive — misclassified env failure as hypothesis failure; told an agent with the correct fix that it was "likely wrong") · C=N (correctly ignored) |

**Cross-component:** LEAKAGE = **1** (`[gt-patch:loaded]` in MSG 3 tool output — telemetry visible to the agent; must be 0). consumed-count = **0** of ~9 GT firings demonstrably changed an agent decision. fair-probe-count: L1 had a fair probe at MSG 2 (agent chose the issue's module path over the brief's rank-1). Resolve credit: agent + issue text. GT product bugs surfaced: jQuery-vendored junk callers in L1; `isinstance→TableColumns.isinstance` false CALLEE facts; false-positive `failure_persisted` nudge on env errors; `[gt-patch:loaded]` stdout leak.

## 2026-06-10 PATH B trial - gt_trial.md §4+§5 audit (run 27260307167)

**Arm:** SWE-bench Verified x deepseek-v4-flash (temp=1.0), mini-swe-agent, GT-on, substrate `gt-substrate@sha256:db7bd22d...`. Official eval: **RESOLVED**. Audit method: chronological read of the full `astropy__astropy-12907.traj.json` messages array incl. `tool_calls` commands (never grep), per gt_trial.md §4 + the AGENT-OBSERVATION rule. Scorecard (8-dp, §5): `.claude/reports/runs/pathB_verified_trial_27260307167/astropy__astropy-12907/scorecard.json`.

**TRAJECTORY (lead, S4.3):** resolve = SELF-LOCALIZED, NOT GT-caused. The issue names `astropy.modeling.separable.separability_matrix` + contains the repro; the agent reproduced numerically and hand-traced `_cstack` to the `cright[...] = 1` -> `= right` fix itself (MSG 34-38). L1 put gold at rank 2 (headline rank 1 = non-gold `modeling/core.py`, EDIT-TARGET CONTRACTS also anchored core.py). gt_caused=FALSE.

### (a) PREREQS - substrate P1/P2/P3 (gt_trial.md §1.5 gates, verbatim 8-dp)

| substrate gate | 8-dp REAL numbers (verbatim, foundational_gate_report.json) | GREEN? | HOW it reached the agent |
|---|---|---|---|
| P1 receiver-type resolution | `det_pct=71.94909195` - `name_match=9808` - typing tiers: `type_flow=1035 - impl_method=5581 - inherited=697` (preds A/B/C all true) | GREEN (pass=true) | `resolved caller: __init__() in astropy/io/ascii/fastbasic.py:40` / `resolved call: -> where() in astropy/table/index.py:511` |
| P2 graph.db depth | `calls_edges=34965.0` - resolution_method breakdown: name_match=9808, impl_method=5581, verified_unique=5527, same_file=5328, lsp=3940, import=3024, type_flow=1035, inherited=697, unique_method=25 - LSP: `LSP_ACTIVE_VALID`, warm probe `1.35850906 ms`, `resolved_promoted=3906.0`, `graph_lsp_edges=3940` (cert==graph, `stamp_mismatch=""`) | GREEN (pass=true) | same resolved-edge lines + post-view `[WITNESS]`/`[CALLERS]` lines (quoted in the L3b table) |
| P3 embedder | `class=EmbeddingModel` - `is_zero=False` - `cos_related=0.71040983` - `cos_unrelated=0.29940427` - `effective_w_sem=0.25000000` (consumption preds 1/2/3 true) | GREEN (pass=true, mode=present_and_consumption) | indirect only - it ORDERS the L1 candidate list the agent sees (no embedder text reaches the agent) |

Substrate numbers are telemetry-only; they reach the agent ONLY as the brief's resolved-edge lines (quoted in the HOW column). Certs reconciled against the runtime witness per gt_gt S12 (no GRAPH_FAIL_MISSING_HANDOFF false-FAIL present on this run; graph cert + LSP cert + embedder cert all PASS).

### (b) L1 brief / localization (ROLE per gt_gt §12: file RANKER - judge by gold-rank + fair reach)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 1 | `<gt-localization confidence="medium">` ... `1. astropy/modeling/core.py - inputs, outputs, separable / resolved caller: __init__() in astropy/io/ascii/fastbasic.py:40` / `2. astropy/modeling/separable.py - separability_matrix, is_separable, _compute_n_outputs / resolved call: -> where() in astropy/table/index.py:511` / `4. astropy/table/table.py - ... resolved caller: gb() in astropy/extern/jquery/data/js/jquery.dataTables.min.js:9` + `EDIT-TARGET CONTRACTS (core.py): inputs -> calls _initialize_unit_support(self)` | MSG 2 CMD: `python -c "..."` running the ISSUE's repro snippet; MSG 32: "Let me look at the separable module directly" (after 14 env-repair actions) | D=Y - C=PARTIAL (gold rank 2; headline + contracts = wrong core.py; `gb() in jquery.dataTables.min.js:9` junk presented as a resolved-caller fact) - C=NO (path ran through the issue's import line, not the ranking) |

**L1 verdict:** delivered, half-right, not consumed; leakage 0

### (b) consensus / scope (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 15/25 | `<gt-scope files="1"> 1. setup.py - in scope (you are viewing this); GT could not expand scope from the graph...` | agent mid pip/build attempts; setup.py is build-noise, not task scope | D=Y - C=N - C=N |
| MSG 41 | `<gt-scope reason="re-anchored"> 1. modeling/separable.py - you have moved here; re-grounding scope 2. tests/test_separable.py - graph-connected ...` | agent had already identified the bug at MSG 38; later opened test_separable.py (MSG 61) | D=Y - C=Y - C=WEAK (trailing; confirms where the agent already was) |

**SCOPE verdict:** delivered; re-anchor correct but trailing; leakage 0

### (b) L3b post-view (ROLE: contract pillar - judge by bug-locus relevance, gt_gt §12)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 33 | `<gt-evidence kind="post_view" file="astropy/modeling/separable.py"> [WITNESS] where calls -> astropy/table/index.py:511 'def where(self, col_map):' [SIBLINGS] is_separable` | MSG 34: agent ignores it; starts hand-tracing `_cstack` matrices | D=Y - C=N (table-index `where` is an irrelevant/wrong edge for separable.py) - C=N |

**L3b verdict:** delivered, wrong edge, inert; leakage 0

### (b) L3 post-edit contract (`<gt-contract>` + post_edit evidence)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 43 | `<gt-contract file="separable.py"> [SIGNATURE] def _coord_matrix(model, pos, noutp): [CALLERS] _coord_matrix: 3 verified caller(s)...` BUT also `[CALLEE] _separable -> def isinstance( self: Self@TableColumns, cls: Any ) -> list (astropy/table/table.py:308)` | agent proceeds to verification (one-line value fix; no signature at stake) | D=Y - C=PARTIAL (signatures real; `isinstance -> TableColumns.isinstance` CALLEE lines are FALSE facts) - C=N |

**L3 verdict:** delivered; contains false CALLEE facts; inert; leakage 0

### (b) GT_VERIFY

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| - | DELIVERED=NO - the agent-invoked GT_VERIFY surface is not wired on PATH B (mini-swe Verified pipeline); the post-edit `<gt-contract>` + `<gt-evidence kind="post_edit">` injections (tabled above) carry the L3 role. Read from the trajectory: no `gt understand`/`gt verify` invocation occurs in any of the agent's commands. | - | N/A |

**GT_VERIFY verdict:** N/A on this path (no agent-invoked verify surface); not a dead layer.

### (b) L4 (EVENT hook - gt_gt §12: absence = event didn't occur, NOT dead)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| - | DELIVERED=N/A - L4 is an EVENT hook (gt_gt S12); on PATH B the wrapper's view/edit/failure/loop events ARE the hook surface and are tabled above (L3b/L3/L5). No separate L4 event exists on this path - absence = the event surface doesn't exist here, NOT a dead layer. | - | N/A |

**L4 verdict:** N/A-by-path; the event surfaces that DO exist here all fired (see L3b/L3/L5 tables).

### (b) L5 / L5b governor (`<gt-nudge>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 45 | `<gt-nudge reason="failure_persisted"> GT: the same test failure has persisted across your edit(s) - your current hypothesis is likely wrong...` | the "failure" was an ENV import error (`_compiler` C-extension); the agent's fix was already CORRECT (proven MSG 55/71); agent rightly ignored it and fixed the env (MSG 47) | D=Y - C=N (FALSE POSITIVE - env failure misclassified as hypothesis failure) - C=N (correctly ignored) |

**L5/L5b verdict:** 2 nudges delivered; 1 false positive; 0 consumed; leakage 0

### (b) L6 (REINDEXER - gt_gt §12)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| - | DELIVERED=N/A-BY-DESIGN - L6 is the post-edit REINDEXER; on the substrate path the mounted graph.db is authoritative + read-only (witness-hash parity), so single-file reindex is deliberately OFF (gt_gt S12 update / S6 note). 'L6 fired' is the wrong expectation here. | - | N/A |

**L6 verdict:** gated OFF by design on the substrate path - correct behavior, not a failure.

### (c) Cross-component line

LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` in MSG 3). consumed-count = 0/~9 firings. fair-probe: NO (issue pre-localized).

### §5 scorecard (stored 8-dp at `astropy__astropy-12907/scorecard.json`)

Tier 1: resolved=True - baseline/flip/regression = **N/A** (no frozen SWE-bench Verified baseline exists; the 87/300 frozen file is OH+SWE-bench-Live - stated, not faked).
Tier 2: delivered=1.00000000 - correct=0.00000000 - consumed=0.00000000 - fair_probe=0.00000000 - right_trajectory=0.00000000 - **gt_caused=0.00000000** (gate broke: correct (headline=non-gold core.py; jQuery junk callers; isinstance->TableColumns false CALLEEs) + consumed=0 + fair_probe=0 (issue names separability_matrix + repro))
Tier 3: gold_in_brief=True - first_gold_rank=2.0 - gold_edited=True - first_edit_action=7.0 - edit_to_gold_action=21.0 - turns_to_gold_view=16.0
Tier 4: action_count=39.00000000 - gt_injected_tokens=549.00000000 - looped_stuck=False - self_localized=True
Tier 6: foundational_gates=GREEN (all_on=true) - test_names_leaked=0 - fail_to_pass_leaked=false - no_gold_labels=true - telemetry stdout leak=1 (`[gt-patch:loaded]`) - VOID=false
Tier 7: llm_in=512416.00000000 - llm_out=10508.00000000 - llm_cost_usd=0.00000000 (none_litellm_unmapped) - wall_clock_s=169.02382708 - time_to_gold_view_s=56.02960467
