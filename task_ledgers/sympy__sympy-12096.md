# Ledger — sympy__sympy-12096

## 2026-06-10 PATH B trial (run 27260307167)

**Arm:** SWE-bench Verified × deepseek-v4-flash (temp=1.0), mini-swe-agent, GT-on.
Outcome: resolved=**YES** (official eval RESOLVED). Patch = `sympy/core/function.py::Function._eval_evalf` — evaluates args (`arg.evalf()`) before calling `self._imp_(*args)`, fixing non-recursive evalf of composed `implemented_function`s. Gold file, gold function.
8-dp: `wall_clock_s=150.46497965`, `gt_injected_tokens_total=578.0`, `action_count=51.0`, `brief_chars=2310.0`.

**One-line trajectory finding:** resolve is **SELF-LOCALIZED, NOT GT-caused — and L1's only OTHER high-confidence call this run was WRONG.** The issue text states outright: "The code for this is in `Function._eval_evalf`." GT's L1 fired `confidence="high"` single-target mode at **`sympy/utilities/lambdify.py :: lambdify`** — NOT the gold file (`sympy/core/function.py`). The agent's first turn grepped BOTH files (`_eval_evalf|_imp_` in lambdify.py per the brief's pull, and in core/function.py per the issue) and the issue won: by MSG 5 it was reading `_eval_evalf` at function.py:500 and the fix landed there. The brief's only correct pointer was the buried `Calls: sympy/core/function.py` line. gt_caused = **FALSE**; and this task is the counterweight to 13579 — high-confidence single-target localization fired twice in this run and went **1/2**.

right_trajectory = **FALSE for GT-causation** (issue named the function; L1's high-confidence target was the wrong file) · L1-ranked-gold = **WRONG single target (lambdify.py); gold only in a secondary `Calls:` line** · agent-reached-gold = YES (issue-driven, MSG 2) · failure locus = n/a (resolved).

### L1 brief / localization

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 1 | `<gt-localization confidence="high"> Edit target: sympy/utilities/lambdify.py :: lambdify / guard/return to update: null_check|modules is None => module_provided = False [L350] / reason: lambdify called by _import [CALLS]` + brief #1 `sympy/utilities/lambdify.py (def implemented_function(symfunc, implementation):, def lambdify(…), def _import(…))` … `Calls: sympy/core/function.py, sympy/utilities/autowrap.py, sympy/core/basic.py` | MSG 2 (tool_calls): `grep -n "_eval_evalf\|_imp_\|evalf" sympy/utilities/lambdify.py` AND `grep -n "_eval_evalf\|_imp_" sympy/core/function.py`; MSG 5: "Now I see the issue. Let me look at `_eval_evalf` in `sympy/core/function.py`:" | D=Y · C=**NO on the headline** (high-confidence single target = lambdify.py; gold = core/function.py; the proposed "guard/return to update L350" is unrelated to the bug) — gold reachable only via the tertiary `Calls:` line · C=**PARTIAL** (the lambdify.py grep in turn 1 mirrors the brief; the DECISION followed the issue's `Function._eval_evalf` pointer) |

**L1 verdict:** a high-confidence MISLOCALIZATION delivered as a single edit target. Harmless here only because the issue out-shouted it; under the Cursor-mentality rule this is the worst category (confident + wrong).

### SCOPE

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 3/8/10/72/74 | scope confirmations on lambdify.py / function.py as the agent moved | Agent worked in function.py; checked `_imp_namespace` in lambdify.py for context | D=Y · C=Y (followed the agent correctly) · C=N |

### POST-VIEW evidence

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 4 | `[WITNESS] _eval_conjugate called by -> sympy/functions/special/mathieu_functions.py:23 …` (on function.py grep) | Agent proceeded to read `_eval_evalf` body | D=Y · C=Y (real but off-target symbols) · C=N |
| MSG 22/58/94 | evidence on function.py/test views | repro + regression | D=Y · C=Y · C=N |

### CONTRACT / post-edit

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 28/34/50 | `<gt-contract>` + `kind="post_edit"` on function.py after the `_eval_evalf` edit (signatures + preserve-interface lines) | Agent kept the signature, added arg-evalf preamble; ran evalf tests | D=Y · C=Y · C=N (fix predates; no observable influence) |

### NUDGE

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 123-equivalent (MSG 94 region) | one nudge delivered late in verification | no course change needed | D=Y · C=N · C=N |

**Cross-component:** LEAKAGE = **1** (`[gt-patch:loaded]` at MSG 3). consumed-count = **0** demonstrable of ~13 firings (the lambdify grep echo is the closest, and it led nowhere). fair-probe-count: 1 (L1 at MSG 2: issue pointer vs brief pointer — issue won, and the brief was wrong). Resolve credit: issue text + agent. **GT product bug logged: high-confidence single-target localization selected the CALLER-side file (lambdify.py, where `implemented_function` lives) instead of the callee-side gold (`Function._eval_evalf`) — the witness chain (`lambdify called by _import`) rewarded graph centrality over issue semantics.**

## 2026-06-10 PATH B trial - gt_trial.md §4+§5 audit (run 27260307167)

**Arm:** SWE-bench Verified x deepseek-v4-flash (temp=1.0), mini-swe-agent, GT-on, substrate `gt-substrate@sha256:db7bd22d...`. Official eval: **RESOLVED**. Audit method: chronological read of the full `sympy__sympy-12096.traj.json` messages array incl. `tool_calls` commands (never grep), per gt_trial.md §4 + the AGENT-OBSERVATION rule. Scorecard (8-dp, §5): `.claude/reports/runs/pathB_verified_trial_27260307167/sympy__sympy-12096/scorecard.json`.

**TRAJECTORY (lead):** resolve = SELF-LOCALIZED; the HIGH-confidence brief was WRONG. Brief single target = `sympy/utilities/lambdify.py :: lambdify`; gold = `sympy/core/function.py::Function._eval_evalf`, which the ISSUE names verbatim ('the code returns ... in Function._eval_evalf'). The agent's first probe grepped BOTH files (one wasted probe on the brief target), then followed the issue to function.py:500 and fixed `_imp_` arg evaluation. gt_caused=FALSE.

### (a) PREREQS - substrate P1/P2/P3 (gt_trial.md §1.5 gates, verbatim 8-dp)

| substrate gate | 8-dp REAL numbers (verbatim, foundational_gate_report.json) | GREEN? | HOW it reached the agent |
|---|---|---|---|
| P1 receiver-type resolution | `det_pct=74.02780928` - `name_match=14887` - typing tiers: `type_flow=1179 - impl_method=3955 - inherited=868` (preds A/B/C all true) | GREEN (pass=true) | (no resolved-edge lines in brief) |
| P2 graph.db depth | `calls_edges=57319.0` - resolution_method breakdown: name_match=14887, import=10835, same_file=9859, verified_unique=8847, lsp=6776, impl_method=3955, type_flow=1179, inherited=868, unique_method=113 - LSP: `LSP_ACTIVE_VALID`, warm probe `1.27863884 ms`, `resolved_promoted=6776.0`, `graph_lsp_edges=6776` (cert==graph, `stamp_mismatch=""`) | GREEN (pass=true) | same resolved-edge lines + post-view `[WITNESS]`/`[CALLERS]` lines (quoted in the L3b table) |
| P3 embedder | `class=EmbeddingModel` - `is_zero=False` - `cos_related=0.71040983` - `cos_unrelated=0.29940427` - `effective_w_sem=0.25000000` (consumption preds 1/2/3 true) | GREEN (pass=true, mode=present_and_consumption) | indirect only - it ORDERS the L1 candidate list the agent sees (no embedder text reaches the agent) |

Substrate numbers are telemetry-only; they reach the agent ONLY as the brief's resolved-edge lines (quoted in the HOW column). Certs reconciled against the runtime witness per gt_gt S12 (no GRAPH_FAIL_MISSING_HANDOFF false-FAIL present on this run; graph cert + LSP cert + embedder cert all PASS).

### (b) L1 brief / localization (ROLE per gt_gt §12: file RANKER - judge by gold-rank + fair reach)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 1 | `<gt-localization confidence="high"> Edit target: sympy/utilities/lambdify.py :: lambdify / guard/return to update: null_check|modules is None => module_provided = False [L350] / reason: lambdify called by _import [CALLS]` | MSG 2 CMDs: `grep -n "_eval_evalf\|_imp_\|evalf" sympy/utilities/lambdify.py` AND `grep -n "_eval_evalf\|_imp_" sympy/core/function.py`; MSG 5: "Let me look at _eval_evalf in sympy/core/function.py" | D=Y - C=NO (high-confidence single target = wrong file; the guard/return hint [L350] is unrelated to the bug) - C=PARTIAL-NEGATIVE (one probe spent on the wrong target; decision followed the issue) |

**L1 verdict:** delivered, WRONG at high confidence - the worst L1 failure mode of the run (confident misdirection; agent escaped via the issue)

### (b) consensus / scope (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 3 | `<gt-scope files="5"> 1. utilities/lambdify.py - in scope (you are viewing this) 2. core/compatibility.py - graph-connected ... 5. tests/test_numeric.py - graph-connected` | agent moved to core/function.py next action | D=Y - C=N (anchored on the wrong file) - C=N |
| MSG 8 | `<gt-scope reason="re-anchored"> 1. core/function.py - you have moved here; re-grounding scope 2. elementary/exponential.py - graph-connected...` | agent already reading _eval_evalf body | D=Y - C=Y - C=N (trailing) |

**SCOPE verdict:** delivered; initial anchor wrong, re-anchor trailing; leakage 0

### (b) L3b post-view (ROLE: contract pillar - judge by bug-locus relevance, gt_gt §12)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 4 | `<gt-evidence kind="post_view" file="sympy/core/function.py"> [WITNESS] _eval_conjugate called by -> sympy/functions/special/mathieu_functions.py:23 ... [SIBLINGS] nargs` | MSG 5-7: agent seds lines 495-535 itself | D=Y - C=Y (real edges, none bug-relevant) - C=N |

**L3b verdict:** delivered, correct, inert; leakage 0

### (b) L3 post-edit contract (`<gt-contract>` + post_edit evidence)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 50 | `<gt-contract file="function.py"> [SIGNATURE]/[CALLERS] lines on the edited _eval_evalf region` | MSG 51-54: agent tests `f(g(2)).evalf()` -> 16.0 | D=Y - C=Y - C=N |

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
| MSG - | only 1 nudge fired this task (deep metrics nudge_delivered=1.0); no false-positive nudge observed in the read | - | D=Y - C=- - C=N |

**L5/L5b verdict:** 1 nudge, inert; leakage 0

### (b) L6 (REINDEXER - gt_gt §12)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| - | DELIVERED=N/A-BY-DESIGN - L6 is the post-edit REINDEXER; on the substrate path the mounted graph.db is authoritative + read-only (witness-hash parity), so single-file reindex is deliberately OFF (gt_gt S12 update / S6 note). 'L6 fired' is the wrong expectation here. | - | N/A |

**L6 verdict:** gated OFF by design on the substrate path - correct behavior, not a failure.

### (c) Cross-component line

LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = 0 (one wasted probe on the wrong brief target). fair-probe: NO (issue names the gold function).

### §5 scorecard (stored 8-dp at `sympy__sympy-12096/scorecard.json`)

Tier 1: resolved=True - baseline/flip/regression = **N/A** (no frozen SWE-bench Verified baseline exists; the 87/300 frozen file is OH+SWE-bench-Live - stated, not faked).
Tier 2: delivered=1.00000000 - correct=0.00000000 - consumed=0.00000000 - fair_probe=0.00000000 - right_trajectory=0.00000000 - **gt_caused=0.00000000** (gate broke: correct (HIGH-confidence single target = WRONG file lambdify.py; gold function.py only via the issue) + fair_probe=0 (issue: the code is in Function._eval_evalf))
Tier 3: gold_in_brief=False - first_gold_rank=absent (no abstain taken) - gold_edited=True - first_edit_action=11.0 - edit_to_gold_action=24.0 - turns_to_gold_view=2.0
Tier 4: action_count=51.00000000 - gt_injected_tokens=578.00000000 - looped_stuck=False - self_localized=True
Tier 6: foundational_gates=GREEN (all_on=true) - test_names_leaked=0 - fail_to_pass_leaked=false - no_gold_labels=true - telemetry stdout leak=1 (`[gt-patch:loaded]`) - VOID=false
Tier 7: llm_in=609334.00000000 - llm_out=7725.00000000 - llm_cost_usd=0.00000000 (none_litellm_unmapped) - wall_clock_s=150.46497965 - time_to_gold_view_s=0.00000000


### Tier 3b architectural conformance - 2026-06-10 (PATH B run 27260307167)

- **Substrate (verbatim certs):** graph det_pct=74.02780928 (calls=57319, name_match=14887), FTS5 21750 rows probe ok; LSP `LSP_ACTIVE_VALID`, warm probe 1.27863884 ms, verified/corrected/deleted=2883/3893/179, promoted 6776; embedder gte-768 separating (0.71040983 / 0.29940427), effective_w_sem=0.25, sem_mad=0.083738. Graph-cert FAIL verdict = documented FALSE FAIL (par.12).
- **Brief vs gold:** **HIGH-confidence WRONG steer** - `Edit target: sympy/utilities/lambdify.py :: lambdify`; gold `sympy/core/function.py::_eval_evalf` absent from every rank (only a 2-hop scope-chain mention `function.py -> numbers.py (_eval_evalf -> Float)`). Root: the issue says verbatim "The code for this is in `Function._eval_evalf`." but `gt_issue_anchors.json` = `{composition, evaluate, functions, implemented_function, lambdify}` - the dotted backtick symbol was DROPPED by anchor extraction, so W_CODE_DEF=0.70 never engaged and repro-API anchors pulled the HIGH steer; par.4.1 gates (d)/(e) passed on genuinely-converging-but-wrong witnesses (gate inputs, not gate logic). Agent ignored the steer (its first command greps BOTH lambdify.py and function.py, then follows the issue) - no harm done, but confident-wrong is the par.4.1 documented worst failure mode.
- **localization_root_cause = RERANK_LOGIC** (anchor-extraction sub-stage). **gt_conformant = NO** - the par.4 (1) `select_anchors` input deviated from spec intent (issue's explicit definition-site symbol not extracted), producing the run's only confident-wrong HIGH.
- Cross-run reference: full table + split in `.claude/reports/runs/pathB_verified_trial_27260307167/TIER3B_ARCHITECTURAL_CONFORMANCE.md`. Run-level split: wrong-localization = 4/4 RERANK_LOGIC, 0 LSP_NOT_WARM, 0 EMBEDDER_OFF, 0 GRAPH_SPARSE - substrate solved, rerank logic is the live lever.
