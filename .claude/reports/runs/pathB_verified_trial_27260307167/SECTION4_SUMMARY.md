# SECTION 4 SUMMARY — PATH B trial (run 27260307167) — gt_trial.md §4 audit
## 2026-06-10 · SWE-bench Verified × deepseek-v4-flash (temp=1.0) · mini-swe-agent · GT-on · 10 tasks

Method: chronological read of every `*.traj.json` messages array (never grep-scan), per the
AGENT-OBSERVATION rule. Per-task ledgers: `task_ledgers/<task>.md` (heading "2026-06-10 PATH B trial").

## The 10-row table

| task | resolved | delivered | correct (brief→gold) | consumed | gt_caused | one-line trajectory finding |
|---|---|---|---|---|---|---|
| astropy-12907 | **YES** | Y | PARTIAL (gold separable.py rank 2; headline+contracts = wrong core.py; jQuery junk callers) | N | **FALSE** | Issue names `separable.separability_matrix`; agent reproduced + hand-traced `_cstack` to the `=1`→`=right` fix itself. |
| astropy-13453 | **YES** | Y | **NO** (gold ascii/html.py absent from all 6 candidates) | N | **FALSE** | Agent's action 1 = own `grep "class HTML"` → html.py; fix self-derived (`_set_col_formats`). GT scope trailed it. |
| astropy-13579 | **YES** | Y | **YES** (high-confidence SINGLE target = gold file; gold helper `_pixel_to_world_values_all` named) | **Y (action 1)** | **TRUE** | Agent's FIRST command = `cat` of the exact brief path (issue names only the class, not the `wrappers/` path); fix flowed through the brief-named helper. The one GT-caused resolve. |
| sympy-11618 | **YES** | Y | PARTIAL (gold point.py at rank 6/6, confidence=low; headline = wrong ellipse.py) | N | **FALSE** | 250-char issue names class+method+root cause; agent grepped point.py itself. |
| sympy-12096 | **YES** | Y | **NO** (high-confidence single target = WRONG file lambdify.py; gold function.py only in a `Calls:` line) | N | **FALSE** | Issue says "the code is in `Function._eval_evalf`"; agent followed the issue, not the (wrong) brief. |
| astropy-13033 | NO | Y | **NO** (all 6 candidates wrong; "Related files to inspect: jquery.dataTables.js…") | PARTIAL (post-view `<gt-scope>` named gold timeseries/core.py 1 turn before the agent opened it; scaffold nudge → env pivot) | FALSE | Gold file+function edited; miss = error-message WORDING vs hidden F2P assertion. Delivery win at scope level, L1 product bug, agent miss on hidden contract. |
| astropy-13236 | NO | Y | PARTIAL (gold table.py rank 1, but contracts anchored on artifact symbol `isinstance`; jQuery junk) | PARTIAL (scaffold_trap nudge at MSG 49 → immediate edit pivot) | FALSE | Issue quotes the exact code block; agent added the FutureWarning at the gold line with NON-gold wording → hidden `pytest.warns(match=…)` fails. |
| astropy-13398 | NO (eval **ERROR**) | Y | PARTIAL (right neighborhood; gold = NEW FILE, unnamable; `__init__.py` in Related-files) | WEAK | FALSE | Fix content = gold approach (issue embeds the full implementation) but the submitted patch is **malformed — "patch unexpectedly ends in middle of line… line 104" — Patch Apply Failed**. Submission artifact, not localization. |
| django-10097 | NO | Y | **YES rank 1** (validators.py/URLValidator) — redundant with the issue | N | FALSE | Gold file+line edited; agent considered the gold regex `[^\s:@/]` and REJECTED it because the STALE visible valid_urls.txt contradicted it (fixtures are replaced by the hidden test patch). |
| django-10554 | NO | Y | PARTIAL (gold compiler.py rank 2; headline rank 1 = query.py where the wrong fix landed) | N | FALSE | Agent read the gold `get_order_by` position-resolution loop twice and walked away; couldn't reproduce the PostgreSQL-only error on SQLite; shipped a defensive-clone fix in query.py. The only substantively-correct nudge of the run (`failure_persisted` MSG 151) went unconsumed. |

## Headline (adversarially honest)

- **Resolves: 5/10. GT-caused: 1 of 5** (astropy-13579 — and even that carries the caveat that a frontier
  model might guess the path from the class name; the chronology shows zero search actions, the
  non-obvious `wrappers/` path segment existed only in the brief, and the fix used the brief-named
  helper). **The other 4 resolves are self-localization**: in every one, the ISSUE TEXT named the
  module/class/function (or contained the full implementation), and the agent's first decisive action
  was its own grep/cat/repro — exactly the ~88% self-localization baseline. Do not claim them.
- **Misses: 5/10, zero caused by GT misdirection** (the agent ignored the brief everywhere it was
  wrong). Dominant failure mode on the misses: **post-localization fix-content divergence from hidden
  test contracts — 3 of 5** (13033 message wording, 13236 warning wording, 10097 regex character class
  calibrated against stale fixtures). The other two: 13398 = **malformed submitted patch** (eval ERROR,
  fix content was right), 10554 = **wrong mechanism** (agent visited the gold function and left;
  PostgreSQL-only symptom unreproducible on SQLite testbed).
- **Correct-but-unconsumed deliveries:** 13033 (scope named gold pre-open), 10554 (gold file at rank 2;
  substantively-correct failure_persisted nudge ignored), 10097 (rank-1 correct but redundant).

## Leakage (must be 0 — it is NOT)

**Total = 10.** `[gt-patch:loaded]` appears verbatim in the agent-visible tool output at MSG 3 of
**every one of the 10 trajectories** (one occurrence each). GT_META = 0, empty-dedup tags = 0,
GT_COST = 0. The loader banner must move to stderr.

## L1 localization scorecard (the product picture)

| L1 mode | fired | gold in candidates | gold at headline/#1 | consumed |
|---|---|---|---|---|
| high-confidence single target | 2 (13579, 12096) | 1/2 | 1/2 | 1/2 (13579) |
| medium/low multi-candidate (6-item) | 8 | 5/8 anywhere in list (12907 #2, 13236 #1, 11618 #6, 10097 #1, 10554 #2) | 2/8 (13236, 10097 — both also issue-named) | 0/8 |

**The agent consumed L1 exactly once — the one time it COMMITTED to a single high-confidence target.
The 6-candidate hedged lists were ignored in all 8 firings.** Candidate lists do not convert decisions.

## Most actionable product bugs (ranked)

1. **`[gt-patch:loaded]` stdout leak** — 10/10 tasks, trivially fixable, violates the leakage=0 invariant.
2. **Vendored/minified-JS edges pollute astropy briefs as "resolved caller" facts** — `gb() in
   astropy/extern/jquery/data/js/jquery.dataTables.min.js:9` (and a full line of minified jQuery
   delivered as a `[WITNESS]` in 13236 MSG 3); 13033 even lists `jquery.dataTables.js` under "Related
   files to inspect". Vendored/extern/minified paths must be excluded from the graph or demoted below
   delivery threshold — this is the single largest source of agent-visible garbage this run.
3. **Builtin-shadow laundering at the consumer**: `isinstance -> def isinstance(self: Self@TableColumns…)
   (astropy/table/table.py:308)` delivered as `[WITNESS]`/`[CALLEE]` fact in ≥6 tasks, peaking as
   `[CALLERS] isinstance: 1048 verified caller(s) in 226 file(s) — preserve this interface` (13236
   MSG 93). A builtin resolved to an arbitrary same-named method is a name guess, not a fact.
4. **False-positive `failure_persisted` nudges**: fired on environment/build errors (missing erfa,
   gcc/longintrepr.h, scratch-script bugs) in 12907/13033/13236 telling agents with CORRECT fixes
   "your current hypothesis is likely wrong." Misclassifies env failure as hypothesis failure; in
   10554 — the one task where the message was substantively right — it was ignored anyway.
5. **L1 mislocalization, including one HIGH-confidence wrong single target** (12096 → lambdify.py via
   the `lambdify called by _import` witness chain — graph centrality beat issue semantics; gold
   `Function._eval_evalf` was IN the issue text and only in the brief's tertiary `Calls:` line).

## Bottom line for Stage 1

Delivery plumbing works (every layer fired, content reached the agent, 0 GT_META). But on this slice
GT's correct-context rate at the moment of decision was: L1 1/10 consumed-and-correct, scope ~2 weak
assists, nudges 1 clean conversion (13236 scaffold_trap) vs 3+ false positives. The honest summary:
**1 GT-caused resolve, 10 leakage hits, and the brief was ignored whenever it hedged.** The 13579/12096
pair is the experiment to run at scale: single-committed-target briefs convert; 6-item hedges never did.
