# GT_TRIAL_AUDIT_SUMMARY — PATH B trial (run 27260307167) — gt_trial.md §4 + §5 audit
## 2026-06-10 · SWE-bench Verified × deepseek-v4-flash (temp=1.0) · mini-swe-agent · GT-on · 10 tasks

**Method (per gt_trial.md §4 verbatim):** chronological read of every `<task>.traj.json` `messages` array
INCLUDING the `tool_calls` commands (never grep-scan), per the AGENT-OBSERVATION rule; layers judged by
their gt_gt.md §12 ROLE; certs reconciled against the runtime witness; §5 scorecard computed FROM the
per-component tables and stored 8-dp at `<task>/scorecard.json`. Per-task (a)/(b)/(c) tables appended
(append-only) to `task_ledgers/<task>.md` under heading `## 2026-06-10 PATH B trial — gt_trial.md §4+§5 audit`.

**Baseline statement (Tier 1):** `baseline_pass` / `flip` / `regression` = **N/A for all 10 tasks.** No frozen
SWE-bench **Verified** baseline exists; the frozen 87/300 file
(`full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json`) is OH CodeActAgent on
SWE-bench-**Live** — a different benchmark and harness. Pairing against it would be fabrication. Per §5 logic,
each task is therefore framed as **gt_caused-right-trajectory or not** — never as a flip.

## The 10-row scorecard roll-up

| task | resolved | gt_caused | delivered | correct | consumed | fair_probe | right_traj | turns-to-gold (action #) | test/F2P leakage |
|---|---|---|---|---|---|---|---|---|---|
| astropy-12907 | **YES** | **0** | 1 | 0 | 0 | 0 | 0 | 16 (env-repair first; gold rank 2, headline wrong) | 0 |
| astropy-13453 | **YES** | **0** | 1 | 0 | 0 | 1 | 0 | 3 (own grep `class HTML`; gold absent from brief) | 0 |
| astropy-13579 | **YES** | **1** | 1 | 1 | 1 | 1 | 1 | **1** (FIRST command = `cat` of the exact brief path) | 0 |
| sympy-11618 | **YES** | **0** | 1 | 0 | 0 | 0 | 0 | 7 (issue repro first; gold rank 6/6, conf=low) | 0 |
| sympy-12096 | **YES** | **0** | 1 | 0 | 0 | 0 | 0 | 2 (issue names `Function._eval_evalf`; brief = wrong file at HIGH conf) | 0 |
| astropy-13033 | NO | 0 | 1 | 0 | 0 | 1 | 0 | 4 (post-view `<gt-scope>` named gold 1 turn pre-open — ambiguous) | 0 |
| astropy-13236 | NO | 0 | 1 | 0 | 1* | 0 | 0 | 1 (issue quotes the code; brief rank-1 redundant) | 0 |
| astropy-13398 | NO (eval **ERROR**) | 0 | 1 | 0 | 0 | 0 | 0 | 25 (gold = NEW file, unnamable; submitted patch MALFORMED) | 0 |
| django-10097 | NO | 0 | 1 | 1 | 0 | 0 | 0 | 1 (rank-1 gold, redundant; agent later REVERTED the gold-class regex) | 0 |
| django-10554 | NO | 0 | 1 | 0 | 0 | 1 | 0 | 10 (gold compiler.py rank 2 — read twice, walked away) | 0 |

\* 13236's consumed=1 is the **L5 scaffold_trap nudge** (MSG 49 → immediate pivot to source at MSG 50), not L1.

## Headline (adversarially honest, §5 verdict logic)

- **Resolves: 5/10. gt_caused: 1/10** — only **astropy-13579** clears the full Tier-2 AND
  (delivered ∧ correct ∧ consumed ∧ fair_probe ∧ right_trajectory): high-confidence SINGLE brief target = gold
  `wcs/wcsapi/wrappers/sliced_wcs.py`, the agent's literal FIRST command was `cat` of that exact path with ZERO
  search actions, and the fix flowed through the brief-named helper `_pixel_to_world_values_all`. Caveat stated
  in the ledger: the issue names the class (not the path); a frontier model might map class→path unaided.
- **The other 4 resolves are self-localization** (resolved ∧ NOT gt_caused = luck/self-solve — NOT GT wins, do
  not count them): in each, the ISSUE text named the module/function or contained the repro/implementation, and
  the agent's first decisive action was its own repro/grep (12907 hand-traced `_cstack`; 13453 grepped
  `class HTML`; 11618 ran the issue repro; 12096 followed "the code is in `Function._eval_evalf`").
- **No gt_caused-right-trajectory-without-resolve case exists in this run** (the §5 "still a win" category is
  empty): on all 5 misses at least one of {correct, consumed} failed.
- **Misses (5): zero caused by GT misdirection** — the agent ignored the brief wherever it was wrong. Dominant
  failure mode = **post-localization content divergence from hidden test contracts (3/5)**: 13033 error-message
  wording, 13236 FutureWarning wording (both fail hidden `match=`/exact-string asserts), 10097 regex character
  class calibrated against the STALE visible `valid_urls.txt` (replaced by the hidden test patch). Remaining 2:
  13398 = **malformed submitted patch** ("patch unexpectedly ends in middle of line… line 104", Patch Apply
  Failed — fix content was gold-shaped), 10554 = **wrong mechanism** (visited gold `get_order_by` twice,
  shipped a defensive clone in query.py).
- **Sharpest adversarial finding (10097):** at MSG 89 the agent installed the gold-equivalent regex `[^\s:@/]`;
  the stale visible fixture failed it; the `failure_persisted` nudge (MSG 96: "your current hypothesis is likely
  wrong") fired at exactly that moment; MSG 99–106 the agent reverted to the non-gold `[^\s@/]`. The proximate
  cause is the stale fixture, but a GT nudge plausibly **reinforced reverting a gold-equivalent edit** — the
  run's worst nudge firing, a Cursor-mentality (non-harm) flag.

## Efficiency finding (Tier 4 — GT-localization-consumed vs self-localized turns-to-gold)

- **GT-consumed localization (n=1, 13579): turns-to-gold = 1** (0.0 s from first action).
- **Self-localized (n=9): mean turns-to-gold = 7.67, median = 4** (1, 1, 1, 2, 3, 4, 7, 10, 16, excl. 13398's 25
  → with it, mean 7.67 over 9). Honest decomposition: most pre-gold actions on 12907 (15) and 11618 (6) were
  **environment repair** (erfa/numpy/gcc), which no brief saves; the clean localization-wandering case is
  **10554: ~9 lost turns** exploring query.py while the brief held gold `compiler.py` at rank 2 unconsumed — a
  missed efficiency win, not neutral. 13033's 3 pre-gold actions and 13398's 24 (pattern-reading for a NEW file)
  are partially intrinsic. Net: **where GT localization was consumed it was the fastest possible (1 action); the
  quantifiable unconsumed-gold loss in this run is ≈9–15 turns (10554 primarily, 12907 partially)** — with n=1
  consumed, no statistical claim is made (no n=1 generalization; needs a real sample per gt_gt §12 protocol).
- `looped_stuck` = false on all 10; no GT-induced wandering observed (briefs ignored, not followed astray).

## Legitimacy-gate status (Tier 6)

- **Foundational gates (§1.5): GREEN 10/10, shown 8-dp in every ledger's PREREQS table.** ① embedder
  `EmbeddingModel`, `is_zero=false`, `cos_related=0.71040983 > cos_unrelated=0.29940427`, consumption preds pass
  (`effective_w_sem=0.25` / `0.5` on 10097); ② receiver-type resolution ON — det_pct 68.36441261–74.02780928,
  preds A/B/C true, typing tiers populated; ③ graph.db populated — calls_edges 34,965–57,319, LSP
  `LSP_ACTIVE_VALID`, warm probe >0 ms, `graph_lsp_edges == cert_resolved+stamps` (`stamp_mismatch=""` — no
  GRAPH_FAIL_MISSING_HANDOFF false-FAIL on this run; certs reconcile with the runtime witness). **No task VOID.**
- **test_names_leaked = 0/10. fail_to_pass_leaked = false/10. no_gold_labels = true/10.** Every test name the
  agent touched, it found via its OWN grep (13033 `test_sampled.py`, 13398 `test_straight_overhead`, 12096
  `test_implemented_function_evalf`) — read chronologically, none surfaced by GT.
- **Telemetry stdout leak (product bug, NOT §4 leakage): `[gt-patch:loaded]` appears verbatim in the
  agent-visible MSG-3 tool output in 10/10 trajectories** (1 occurrence each). It is a loader banner, carries no
  test/gold information, and does not void any task — but it must move to stderr.

## Substrate PREREQS snapshot (P1/P2/P3, 8-dp — full per-task tables in the ledgers)

| task | P1 det_pct | P1 name_match | P2 calls_edges | P2 lsp_edges | P3 cos_rel / cos_unrel | all_on |
|---|---|---|---|---|---|---|
| astropy-12907 | 71.94909195 | 9808 | 34965.0 | 3940 | 0.71040983 / 0.29940427 | true |
| astropy-13033 | 71.82873149 | 9856 | 34986.0 | 3910 | 0.71040983 / 0.29940427 | true |
| astropy-13236 | 71.30456910 | 10149 | 35368.0 | 3949 | 0.71040983 / 0.29940427 | true |
| astropy-13398 | 71.30694632 | 10178 | 35472.0 | 3962 | 0.71040983 / 0.29940427 | true |
| astropy-13453 | 71.30608907 | 10174 | 35457.0 | 3960 | 0.71040983 / 0.29940427 | true |
| astropy-13579 | 71.35262847 | 10185 | 35553.0 | 3973 | 0.71040983 / 0.29940427 | true |
| django-10097 | 72.04979500 | 13157 | 47073.0 | 2578 | 0.71040983 / 0.29940427 | true |
| django-10554 | 70.39690184 | 14753 | 49836.0 | 1553 | 0.71040983 / 0.29940427 | true |
| sympy-11618 | 68.36441261 | 17932 | 56683.0 | 2485 | 0.71040983 / 0.29940427 | true |
| sympy-12096 | 74.02780928 | 14887 | 57319.0 | 6776 | 0.71040983 / 0.29940427 | true |

## Product bugs surfaced by the trajectory reads (context-gap analysis, CLAUDE.md mandatory)

1. **Vendored-JS pollution in L1/L3b:** minified jQuery callers (`gb() in jquery.dataTables.min.js:9`, `t() in
   jquery-3.6.0.min.js:2`) delivered as "resolved caller" facts in 4+ briefs and as a raw `[WITNESS]` blob
   (13236). `astropy/extern/` must be excluded/demoted at index or render time.
2. **Builtin laundering survives in delivery:** `isinstance → def isinstance(self: Self@TableColumns…)
   (astropy/table/table.py:308)` rendered as deterministic `[CALLEE]`/`Calls:` facts across astropy tasks — the
   stdlib-shadow class of bug at the consumer surface.
3. **`failure_persisted` nudge cannot distinguish env failures from hypothesis failures:** false-positive on
   12907/13579/11618/13236 (erfa/numpy/gcc import chains), and on 10097 it fired against a gold-equivalent edit.
   Only 10554's firing was substantively correct — and it was ignored.
4. **No patch-integrity guard at submit:** 13398's gold-shaped fix died on a hand-assembled malformed patch.txt.
   A presubmit-verify (L6's companion role) that runs `git apply --check` on patch.txt would have saved a resolve.
5. **Hidden-contract divergence (3/5 misses) is the dominant non-resolution mode** — wording/exact-string
   contracts that no current GT layer carries. Research item, not a layer bug.
6. **`[gt-patch:loaded]` stdout leak** (10/10) — move to stderr.

## Where everything is

- Per-task §4 tables (PREREQS + per-component, verbatim quotes): `task_ledgers/<task>.md`, heading
  `## 2026-06-10 PATH B trial — gt_trial.md §4+§5 audit (run 27260307167)`.
- Per-task §5 scorecards (8-dp JSON): `<task>/scorecard.json` in this directory.
- Generators (reproducible): `_gen_scorecards.py`, `_gen_ledgers.py` in this directory.
- Earlier same-day §4 pass (component tables, first iteration): `SECTION4_SUMMARY.md` (its "Leakage total=10"
  counted the `[gt-patch:loaded]` banner; this audit reclassifies that as a telemetry stdout leak — the §4
  test-name/F2P leakage gate is **0**, consistent in both).
