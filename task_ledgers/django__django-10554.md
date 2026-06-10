# Ledger — django__django-10554

## 2026-06-10 PATH B trial (run 27260307167)

**Arm:** SWE-bench Verified × deepseek-v4-flash (temp=1.0), mini-swe-agent, GT-on.
Outcome: resolved=**NO** (official eval UNRESOLVED). Patch = `django/db/models/query.py::_combinator_query` — defensively `chain()`s the combined queries. Gold = `django/db/models/sql/compiler.py::get_order_by` (resolve ORDER BY positions against the combined select list). **Wrong mechanism in a non-gold file** — and the agent had been INSIDE the gold function repeatedly (MSG 21, 106: "it converts column references to numeric positions by iterating over `self.select`") and walked away.
8-dp: `wall_clock_s=603.96364403` (longest of the run), `gt_injected_tokens_total=798.0`, `action_count=112.0`, `brief_chars=3190.0`.

**One-line trajectory finding:** a 236-message reproduction death-march: the issue's `ProgrammingError: ORDER BY position 4 is not in select list` is PostgreSQL-specific; on the SQLite testbed the agent could not reproduce the gold symptom (MSG 68: "it seems like the issue doesn't reproduce in this simple case"; MSG 94: SQLite raises a DIFFERENT error), so it anchored on the one pathology it COULD observe locally (shared query objects between original and derived querysets) and shipped the clone fix. GT's L1 had `query.py` rank 1 and **the gold `compiler.py — execute_sql, SQLCompiler` at rank 2**, but nothing GT delivered ever pointed at `get_order_by`'s position-resolution loop as THE defect or counteracted the un-reproducible-locally trap. gt_caused = **FALSE**; classification = **mislocalized FIX (agent), with GT delivery correct-but-unconsumed at the candidate level and silent at the mechanism level**.

right_trajectory = **FALSE** (agent visited the gold region, rejected it, fixed a sibling symptom) · L1-ranked-gold = rank 2 (headline rank 1 = the file the agent wrongly chose) · agent-reached-gold = touched-but-abandoned · failure locus = **wrong mechanism; repro blocked by SQLite-vs-PostgreSQL env**.

### L1 brief / localization

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 1 | `<gt-localization confidence="medium"> 1. django/db/models/query.py — QuerySet, last, values_list / resolved caller: create_default_site() in django/contrib/sites/management.py:20 2. django/db/models/sql/compiler.py — execute_sql, SQLCompiler, __init__ / resolved caller: do_query() in django/db/models/sql/subqueries.py:24 …` + brief #1 anchored on `query.py (def annotate…, def order_by…, def first…)` with `EDIT-TARGET CONTRACTS (query.py): order_by -> calls _chain(self, **kwargs) [django/db/models/query.py:1212]` | MSG 2: "The problem is that when using `order_by()` on a union queryset, it breaks because the ordering references positions that don't exist in the select list." (agent's own correct framing!) → explores query.py `_combinator_query` (MSG 9) AND compiler.py `get_order_by` (MSG 21) | D=Y · C=**PARTIAL** (gold compiler.py present at rank 2; headline rank 1 = query.py, which is where the agent's wrong fix landed — the brief's emphasis reinforced the wrong file) · C=WEAK (agent explored both; chose rank 1's file) |

### SCOPE

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 4/5/17/19/75/77/113/121/211/213 | in-scope confirmations following the agent across query.py / compiler.py / features.py / tests | Agent ping-ponged between query.py and compiler.py for ~200 messages | D=Y · C=Y (tracking correct) · C=N (never altered a decision) |

### POST-VIEW evidence

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 4/11/16/53 | witnesses/callers on query.py + compiler.py views | MSG 21: "In `get_order_by()` at line 345, when `self.query.combinator` is set, it tries to match ORDER BY expressions to columns in the select list" — the agent SAW the gold mechanism; MSG 106 returns to it; MSG 126+ pivots away ("Let me look at this from a different approach") | D=Y · C=Y (real edges) · C=N — **the decisive gap: nothing in the delivered evidence marked `get_order_by`'s position-resolution as the defect; the agent needed mechanism-level disambiguation, not more candidates** |

### CONTRACT / post-edit

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 195 | `<gt-contract …>` + post_edit on query.py after the `_combinator_query` edit (preserve-interface lines for `union/intersection/difference` path) | MSG 200-228: agent ran combinator suites — "All 364 tests pass" (the F2P tests are hidden; local green = false confidence) | D=Y · C=Y (interface intact) · C=N |

### NUDGE

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 43 | `<gt-nudge reason="scaffold_trap"> GT: 25+ actions and no source-file edit yet…make a concrete edit…` | Agent was deep in legitimate multi-file analysis; kept analyzing ~70 more messages before editing | D=Y · C=**PARTIAL-N** (this was analysis, not scaffold-thrash; "edit NOW" pressure on an agent that had not yet found the mechanism pushes toward exactly the premature wrong-mechanism fix that happened) · C=N (not immediately) |
| MSG 151 | `<gt-nudge reason="failure_persisted"> …your current hypothesis is likely wrong. Re-read the failing assertion and reconsider the root cause / target file.` | Fired amid the agent's failed repro attempts. The advice was substantively RIGHT (the hypothesis — clone-the-queries — was indeed wrong) but the agent read the persistence as env-difference (SQLite) and kept course | D=Y · C=**Y in substance** (only nudge this run whose content matched the true failure) · C=N (not consumed) |

**Cross-component:** LEAKAGE = **1** (`[gt-patch:loaded]` at MSG 3). consumed-count = **0** of ~18 firings. fair-probe-count: 2 (L1 candidate choice at MSG 2-9; failure_persisted at MSG 151 — both went against gold). **Context gap (mandatory):** the agent needed (a) "the issue's error is PostgreSQL-only; SQLite raises `ORDER BY not allowed in subqueries` instead — do not require local repro of the exact message" and (b) mechanism-level evidence pinning `compiler.py::get_order_by`'s `self.select` position-matching loop as where combined-query ordering resolution lives. GT's graph has the `get_order_by → setup_query/get_select` edges to say (b); it delivered candidates and trailing scope instead. This is the run's clearest correct-but-unconverted delivery and the strongest argument that candidate LISTS don't flip decisions — committed mechanism-level briefs do.

## 2026-06-10 PATH B trial - gt_trial.md §4+§5 audit (run 27260307167)

**Arm:** SWE-bench Verified x deepseek-v4-flash (temp=1.0), mini-swe-agent, GT-on, substrate `gt-substrate@sha256:db7bd22d...`. Official eval: **NOT RESOLVED**. Audit method: chronological read of the full `django__django-10554.traj.json` messages array incl. `tool_calls` commands (never grep), per gt_trial.md §4 + the AGENT-OBSERVATION rule. Scorecard (8-dp, §5): `.claude/reports/runs/pathB_verified_trial_27260307167/django__django-10554/scorecard.json`.

**TRAJECTORY (lead):** NOT resolved; the agent READ the gold function and walked away. Gold `django/db/models/sql/compiler.py::get_order_by` was rank 2 in the brief (headline rank 1 = query.py, where the wrong fix landed). The agent reached compiler.py at action 10, read get_order_by's combinator position-resolution loop twice (MSG 88-105), could not reproduce the PostgreSQL-only `ORDER BY position` error on the SQLite testbed, and shipped a defensive `query.chain()` clone in query.py::_combinator_query instead. The one substantively-correct nudge of the run (failure_persisted, MSG 151) went unconsumed. gt_caused=FALSE.

### (a) PREREQS - substrate P1/P2/P3 (gt_trial.md §1.5 gates, verbatim 8-dp)

| substrate gate | 8-dp REAL numbers (verbatim, foundational_gate_report.json) | GREEN? | HOW it reached the agent |
|---|---|---|---|
| P1 receiver-type resolution | `det_pct=70.39690184` - `name_match=14753` - typing tiers: `type_flow=2051 - impl_method=9382 - inherited=3870` (preds A/B/C all true) | GREEN (pass=true) | `resolved caller: create_default_site() in django/contrib/sites/management.py:20` / `resolved caller: do_query() in django/db/models/sql/subqueries.py:24` |
| P2 graph.db depth | `calls_edges=49836.0` - resolution_method breakdown: name_match=14753, impl_method=9382, same_file=7701, import=5212, verified_unique=4933, inherited=3870, type_flow=2051, lsp=1553, unique_method=381 - LSP: `LSP_ACTIVE_VALID`, warm probe `1.32012367 ms`, `resolved_promoted=1553.0`, `graph_lsp_edges=1553` (cert==graph, `stamp_mismatch=""`) | GREEN (pass=true) | same resolved-edge lines + post-view `[WITNESS]`/`[CALLERS]` lines (quoted in the L3b table) |
| P3 embedder | `class=EmbeddingModel` - `is_zero=False` - `cos_related=0.71040983` - `cos_unrelated=0.29940427` - `effective_w_sem=0.25000000` (consumption preds 1/2/3 true) | GREEN (pass=true, mode=present_and_consumption) | indirect only - it ORDERS the L1 candidate list the agent sees (no embedder text reaches the agent) |

Substrate numbers are telemetry-only; they reach the agent ONLY as the brief's resolved-edge lines (quoted in the HOW column). Certs reconciled against the runtime witness per gt_gt S12 (no GRAPH_FAIL_MISSING_HANDOFF false-FAIL present on this run; graph cert + LSP cert + embedder cert all PASS).

### (b) L1 brief / localization (ROLE per gt_gt §12: file RANKER - judge by gold-rank + fair reach)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 1 | `<gt-localization confidence="medium"> 1. django/db/models/query.py - QuerySet, last, values_list / resolved caller: create_default_site() in django/contrib/sites/management.py:20 / 2. django/db/models/sql/compiler.py - execute_sql, SQLCompiler, __init__ / resolved caller: do_query() in django/db/models/sql/subqueries.py:24 ...` | MSG 2 CMDs: `ls -la` + `grep -n "def order_by" django/db/models/query.py` + `grep -rn "union" django/db/models/query.py` (issue-driven: union/order_by) | D=Y - C=PARTIAL (gold compiler.py at rank 2; headline query.py = where the WRONG fix landed - the issue also points there, so misdirection is shared with the issue) - C=NO |

**L1 verdict:** delivered, gold at rank 2, not consumed; headline shared the issue's misdirection; leakage 0

### (b) consensus / scope (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 17 | `<gt-scope reason="re-anchored"> 1. sql/compiler.py - you have moved here; re-grounding scope...` | agent reading get_order_by/combinator code | D=Y - C=Y - C=N (trailing) |

**SCOPE verdict:** delivered, trailing; leakage 0

### (b) L3b post-view (ROLE: contract pillar - judge by bug-locus relevance, gt_gt §12)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 16 | `<gt-evidence kind="post_view" file="django/db/models/sql/compiler.py"> [WITNESS] do_query called by -> django/db/models/sql/subqueries.py:24 'cursor = self.get_compiler(using).execute_sql(CURSOR)' [SIBLINGS] setup_query, get_group_by, collapse_group_by, get_select, get_order_by` | MSG 18: agent seds 340-370 + 410-490 (the combinator block) - in-neighborhood but uncited | D=Y - C=Y (siblings include the gold function name) - C=N |

**L3b verdict:** delivered, correct (gold function in SIBLINGS), inert; leakage 0

### (b) L3 post-edit contract (`<gt-contract>` + post_edit evidence)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 195 | `<gt-contract file="query.py">` after the _combinator_query chain() edit | agent verifies clone independence + runs queries suite | D=Y - C=Y - C=N |

**L3 verdict:** delivered, correct, inert; leakage 0

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
| MSG 43 | `<gt-nudge reason="scaffold_trap"> GT: 25+ actions and no source-file edit yet...` | agent continued reading compiler.py/query.py; first real fix attempt much later | D=Y - C=Y (true positive) - C=N |
| MSG 151 | `<gt-nudge reason="failure_persisted"> GT: the same test failure has persisted across your edit(s) - your current hypothesis is likely wrong. Re-read the failing assertion and reconsider the root cause / target file.` | the agent's hypothesis (queryset-clone corruption in query.py) WAS the wrong mechanism - the gold fix lives in compiler.py get_order_by; agent did not change target; at MSG 202 it says 'I'm going to stop trying to understand the exact corruption mechanism' | D=Y - C=Y (SUBSTANTIVELY CORRECT - the only correct failure_persisted of the run) - C=N (ignored) |

**L5/L5b verdict:** 3 nudges; 1 substantively-correct failure_persisted UNCONSUMED (missed save); leakage 0

### (b) L6 (REINDEXER - gt_gt §12)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| - | DELIVERED=N/A-BY-DESIGN - L6 is the post-edit REINDEXER; on the substrate path the mounted graph.db is authoritative + read-only (witness-hash parity), so single-file reindex is deliberately OFF (gt_gt S12 update / S6 note). 'L6 fired' is the wrong expectation here. | - | N/A |

**L6 verdict:** gated OFF by design on the substrate path - correct behavior, not a failure.

### (c) Cross-component line

LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = 0. fair-probe: YES (issue names no file) - gold-at-rank-2 unconsumed = a missed efficiency/causation win.

### §5 scorecard (stored 8-dp at `django__django-10554/scorecard.json`)

Tier 1: resolved=False - baseline/flip/regression = **N/A** (no frozen SWE-bench Verified baseline exists; the 87/300 frozen file is OH+SWE-bench-Live - stated, not faked).
Tier 2: delivered=1.00000000 - correct=0.00000000 - consumed=0.00000000 - fair_probe=1.00000000 - right_trajectory=0.00000000 - **gt_caused=0.00000000** (gate broke: correct (gold compiler.py at rank 2; headline rank 1 = query.py where the wrong fix landed) + consumed=0 (agent read get_order_by twice, MSG 88-105, and walked away; failure_persisted nudge MSG 151 - the one substantively-correct nudge - unconsumed))
Tier 3: gold_in_brief=True - first_gold_rank=2.0 - gold_edited=False - first_edit_action=19.0 - edit_to_gold_action=None - turns_to_gold_view=10.0
Tier 4: action_count=112.00000000 - gt_injected_tokens=798.00000000 - looped_stuck=False - self_localized=True
Tier 6: foundational_gates=GREEN (all_on=true) - test_names_leaked=0 - fail_to_pass_leaked=false - no_gold_labels=true - telemetry stdout leak=1 (`[gt-patch:loaded]`) - VOID=false
Tier 7: llm_in=5069498.00000000 - llm_out=43641.00000000 - llm_cost_usd=0.00000000 (none_litellm_unmapped) - wall_clock_s=603.96364403 - time_to_gold_view_s=10.68186069


### Tier 3b architectural conformance - 2026-06-10 (PATH B run 27260307167)

- **Substrate (verbatim certs):** graph det_pct=70.39690184 (calls=49836, name_match=14753), FTS5 29543 rows probe ok; LSP `LSP_ACTIVE_VALID`, warm probe 1.32012367 ms, verified/corrected/deleted=733/820/1, promoted 1553; embedder gte-768 separating (0.71040983 / 0.29940427), effective_w_sem=0.25, sem_mad=0.00000000, pred_2_coverage=False. Graph-cert FAIL verdict = documented FALSE FAIL (par.12).
- **Brief vs gold:** gold `django/db/models/sql/compiler.py` at **rank 2** (MEDIUM); agent went straight to `query.py` from its own analysis (trajectory-verified first commands), read compiler.py twice and walked away - ~9 lost turns. Delivery/consumption loss, not a ranking failure.
- **localization_root_cause = CORRECT (delivered, unconsumed). gt_conformant = YES.**
- Cross-run reference: full table + split in `.claude/reports/runs/pathB_verified_trial_27260307167/TIER3B_ARCHITECTURAL_CONFORMANCE.md`. Run-level split: wrong-localization = 4/4 RERANK_LOGIC, 0 LSP_NOT_WARM, 0 EMBEDDER_OFF, 0 GRAPH_SPARSE - substrate solved, rerank logic is the live lever.
