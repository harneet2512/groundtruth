# TIER 3B — ARCHITECTURAL CONFORMANCE AUDIT — PATH B trial (run 27260307167)
## 2026-06-10 · SWE-bench Verified × deepseek-v4-flash · mini-swe-agent · GT-on · 10 tasks
### Per gt_trial.md Tier 3b: walk GT's actual pipeline execution (CERTS + trajectory) against the gt_gt.md spec (§2 graph → §3 LSP → §4/§4.2 localization/rerank/fusion → §5 embedder → brief) and attribute every wrong localization to ONE architectural root cause.

**Method.** Per task: (1) `gt_artifacts/graph_certificate.json` + `foundational_gate_report.json` →
graph base (§2); (2) `lsp_certificate.json` (+ per-language certs) → LSP warm/applied-before-scoring
(§3, §12); (3) `embedder_certificate.json` + the gate report's consumption block → semantic alive and
*separating* (§5, §11.2); (4) `brief.txt` + `gt_issue_anchors.json` vs the gold file (cross-checked
against `results/predictions.jsonl` patch targets AND the §4-audit ledgers' `first_gold_rank`, which
used the true SWE-bench gold — for the 2 miss-tasks where the model patched a non-gold file, the gold
is the §4-established one, stated per task); (5) trajectory spot-verification from
`results/<task>/<task>.traj.json` (chronological tool_calls) for every claim the attribution rests on.
All numbers below are quoted verbatim from the certs (8-dp where the cert stores 8-dp).

**Cert-reading note (applies to all 10 tasks, stated once):**
- `graph_certificate.json` `"verdict": "GRAPH_FAIL_MISSING_HANDOFF"` appears on **all 10 tasks** and is
  the documented **FALSE FAIL** (gt_gt §12 gates/certs row: cert is written pre-agent; `hook_graph_hash:
  null` at cert-write time). The runtime witness reconciles it on every task: gate report
  `stamp_mismatch: ""`, `graph_lsp_edges == cert_resolved + stamps`, and the graph cert itself records
  `lsp_warm_from_same_graph: true` + `graph_hash_after_lsp == graph_hash` (the brief was generated from
  the post-LSP graph). Not a real graph failure on any task.
- `gate_embedder.present.cos_related = 0.71040983` / `cos_unrelated = 0.29940427` are identical across
  all 10 tasks because the §1.5 gate ① probe uses a **fixed probe pair** (presence/separation of the
  model itself, not per-task). The per-task semantic signal is the gate's *consumption* block
  (`sem_mad`, `sem_scored_count`, `pred_2_coverage`) — quoted per task below; it is NOT uniform.

---

## The 10-row conformance table

| task | resolved | gold_first_rank | LSP_warm | embedder separating (cos_rel / cos_unrel / eff_w_sem) | graph det% | localization_root_cause | gt_conformant | one-line architectural finding |
|---|---|---|---|---|---|---|---|---|
| astropy-12907 | **YES** | **2** (MEDIUM, gold `modeling/separable.py`) | YES (`LSP_ACTIVE_VALID`, probe 1.35850906 ms, promoted 3906) | YES (0.71040983 / 0.29940427 / 0.25; sem_mad 0.218922) | 71.94909195 | **CORRECT** | YES | Gold at rank 2; the rank-1 headline `modeling/core.py` is a §4.2 mis-order nit (issue literally imports from `separable`) and witness render pulled `extern/jquery/*.min.js` junk callers — noise, not misdirection. |
| astropy-13033 | NO | **absent** (gold `timeseries/core.py`; 6 files listed, none timeseries) | YES (`LSP_ACTIVE_VALID`, probe 1.46603584 ms, promoted 3876) | YES (0.71040983 / 0.29940427 / 0.25; sem_mad 0.00000000, pred_2_coverage=False) | 71.82873149 | **RERANK_LOGIC** | YES (substrate); localization NO | Anchors held `TimeSeries` + `remove_column`, FTS5 green (17582 rows, probe ok) — yet the candidate union/fusion put `io/votable/tree.py`, `time/core.py` etc. in all 6 slots; dense signal flat (mad 0.0) at the fusion level. |
| astropy-13236 | NO | **1** (gold `table/table.py`) | YES (`LSP_ACTIVE_VALID`, probe 1.39570236 ms, promoted 3915) | YES (0.71040983 / 0.29940427 / 0.25; sem_mad 0.054196) | 71.30456910 | **CORRECT** (fair-probe bad: issue quotes the code block) | YES | Rank-1 gold; the miss is post-localization content (FutureWarning wording vs hidden `match=` assert), not localization — GT pointed right, redundantly. |
| astropy-13398 | NO (Patch Apply Failed — malformed patch) | **absent — gold is a NEW file** (`builtin_frames/itrs_observed_transforms.py`) | YES (`LSP_ACTIVE_VALID`, probe 1.22570992 ms, promoted 3926) | YES (0.71040983 / 0.29940427 / 0.25; sem_mad 0.00000000, pred_2_coverage=False) | 71.30694632 | **N/A** (new-file — unrankable by any retrieval over existing nodes) | YES | Brief was region-adjacent (`coordinates/*`, incl. a `builtin_frames/itrs.py` caller line); no architecture stage can rank a file that does not exist; the failure was a malformed submitted patch, not GT. |
| astropy-13453 | **YES** | **absent** (gold `io/ascii/html.py`; absent from all 6 ranks AND the whole brief) | YES (`LSP_ACTIVE_VALID`, probe 1.07836723 ms, promoted 3924) | YES (0.71040983 / 0.29940427 / 0.25; sem_mad 0.023804) | 71.30608907 | **RERANK_LOGIC** | YES (substrate); localization NO | The sharpest recall/rank defect: anchor set held title-symbol `HTML`, FTS5 green (17770 rows, probe ok), gold defines `class HTML` — a one-token lookup reaches it; agent's own `grep "class HTML"` found it in 1 action while GT's 6 slots went to table/fits/votable/wcs. |
| astropy-13579 | **YES** | **1** (HIGH: `Edit target: astropy/wcs/wcsapi/wrappers/sliced_wcs.py`) | YES (`LSP_ACTIVE_VALID`, probe 1.54972076 ms, promoted 3937) | YES (0.71040983 / 0.29940427 / 0.25; sem_mad 0.210425) | 71.35262847 | **CORRECT** | YES | The exemplar: HIGH single-target steer = gold; agent's literal first command was `cat astropy/wcs/wcsapi/wrappers/sliced_wcs.py`, zero search actions; fix flowed through brief-named `_pixel_to_world_values_all`. The §4 pipeline working exactly as specified. |
| django-10097 | NO | **1** (gold `core/validators.py`) | YES (`LSP_ACTIVE_VALID`, probe 1.35684013 ms, promoted 2578) | YES (0.71040983 / 0.29940427 / **0.5**; sem_mad 0.00000000, pred_2_coverage=False) | 72.04979500 | **CORRECT** (fair-probe bad: issue names URLValidator) | YES | Rank-1 gold; the miss is the stale-fixture regex calibration + the `failure_persisted` nudge (L5, a separate non-harm flag from the §4 audit) — localization architecture blameless. |
| django-10554 | NO | **2** (gold `db/models/sql/compiler.py`; agent patched non-gold `query.py`) | YES (`LSP_ACTIVE_VALID`, probe 1.32012367 ms, promoted 1553) | YES (0.71040983 / 0.29940427 / 0.25; sem_mad 0.00000000, pred_2_coverage=False) | 70.39690184 | **CORRECT** (delivered, unconsumed) | YES | Gold at rank 2, read twice and walked away from — the run's quantifiable unconsumed-gold loss (~9 turns in query.py); a delivery/consumption problem, not a ranking one. |
| sympy-11618 | **YES** | **6** of 6 (LOW, gold `geometry/point.py`) | YES (`LSP_ACTIVE_VALID`, probe 1.23929977 ms, promoted 2485) | YES (0.71040983 / 0.29940427 / 0.25; sem_mad 0.00000000, pred_2_coverage=False) | 68.36441261 | **RERANK_LOGIC** | YES (substrate); localization NO | Title anchor `distance` is DEFINED in gold `point.py` (the brief even renders `point.py — distance, Point, __new__`), yet fusion ordered `ellipse.py/line.py/polygon.py` above it; honest LOW label = conformant correct-or-quiet, but the ORDER is a §4.2 fusion defect with a flat dense signal (mad 0.0). |
| sympy-12096 | **YES** | **absent** — HIGH steer to the WRONG file (`Edit target: sympy/utilities/lambdify.py`; gold `core/function.py`) | YES (`LSP_ACTIVE_VALID`, probe 1.27863884 ms, promoted 6776) | YES (0.71040983 / 0.29940427 / 0.25; sem_mad 0.083738) | 74.02780928 | **RERANK_LOGIC** | **NO** — §4 anchor-extraction + §4.1 HIGH-gate deviation | The run's worst architectural behavior: the issue says verbatim ``The code for this is in `Function._eval_evalf`.`` yet `gt_issue_anchors.json` = `{composition, evaluate, functions, implemented_function, lambdify}` — the dotted backtick symbol was DROPPED, so W_CODE_DEF=0.70 (the strongest weight) never engaged and the repro-API anchors pulled a confident-wrong HIGH steer that survived gates (d)/(e). Agent ignored it (followed the issue), so no harm — but confident-wrong is the documented worst failure mode (§4.1). |

---

## Pipeline-stage conformance (the gt_gt walk, summarized across 10 tasks)

**§2 graph base — GREEN 10/10.** `fts5_exists=true`, `fts5_match_probe_ok=true`, FTS5 rows
17,579–29,543; calls_edges 34,965–57,319; det_pct **68.36441261–74.02780928** (all preds A/B/C true,
typing tiers populated, e.g. 12907: `type_flow 1035 · impl_method 5581 · inherited 697 ·
ev:assignment_tracked 820`). No sparse graph anywhere. The all-10 `GRAPH_FAIL_MISSING_HANDOFF` cert
verdict is the documented FALSE FAIL (header note) — reconciled by `stamp_mismatch:""` +
`lsp_warm_from_same_graph:true` on every task.

**§3 LSP — WARM AND APPLIED BEFORE SCORING, 10/10.** Every task: `server_launched=true`,
`warm_probe_ok=true`, `verdict=LSP_ACTIVE_VALID`, probe 1.07836723–1.54972076 ms, real edge work
(e.g. 12907: `verified=1659, corrected=2247, deleted=1`; 12096: `verified=2883, corrected=3893,
deleted=179`), `graph_hash_before_lsp != graph_hash_after_lsp`, `closure_rebuilt_after_lsp=true`,
and the brief generated from the post-LSP graph (graph-cert hash == post-LSP hash). **Zero
LSP_NOT_WARM cases.** The residual (2,015–5,735 still-unresolved demand edges) is the §3 known
demand-scope bound, not a warmth failure.

**§5 embedder — ALIVE 10/10, but NOT uniformly *discriminating*.** `embedder_class=EmbeddingModel`
(gte-modernbert-base, dim 768), `is_zero=false`, fixed-pair separation 0.71040983 > 0.29940427,
`effective_w_sem=0.25` (the §4.2 floor; 0.5 on 10097) — the §11.6 `forbid_no_sem_config` invariant
held everywhere. **Zero EMBEDDER_OFF cases.** However the per-task consumption block shows
`sem_mad=0.00000000` AND `pred_2_coverage=False` (dense scored only 1–2 of the 5 rendered files) on
**5/10 tasks** (13033, 13398, 10097, 10554, 11618) — and 2 of the 4 RERANK_LOGIC failures (13033,
11618) sit exactly there, with 13453 nearly flat (0.023804). This is the §11.2 granularity/coverage
symptom INSIDE the fusion — the dense signal reaching the linear sum was flat/absent on the tasks GT
mis-ranked — which per gt_trial's own bucket definition (fusion/weights/**granularity** §11.2) is
RERANK_LOGIC territory, not substrate. Secondary flag: the consumption gate still reports
`pass=true` with `pred_2_coverage=False` — the gate's pass-logic tolerates a coverage-pred failure,
so a flat-dense fusion never voids a run; worth a gate-logic review.

**§4/§4.2 localization/rerank — the ONLY failing stage.** Every wrong brief in this run happened with
all three substrate gates GREEN.

---

## The root-cause split (the user's key question)

Wrong-localization tasks (gold absent or ranked low): **13033, 13453, 11618, 12096** — 4 tasks.

| root cause | count | tasks |
|---|---|---|
| LSP_NOT_WARM | **0** | — |
| EMBEDDER_OFF | **0** | — |
| GRAPH_SPARSE | **0** | — |
| **RERANK_LOGIC** | **4** | 13033 (gold absent, anchors present), 13453 (gold absent, title-anchor `HTML` IS the gold class), 11618 (gold rank 6/6, title-anchor `distance` defined in gold), 12096 (HIGH-conf wrong steer, dropped backtick anchor) |
| CORRECT | 5 | 12907 (r2), 13236 (r1), 13579 (r1 HIGH=gold), 10097 (r1), 10554 (r2) |
| N/A | 1 | 13398 (gold = new file, unrankable) |

**Sub-attribution within RERANK_LOGIC (the fixable levers, per stage of §4):**
- **Anchor/code-symbol extraction (§4 ① `select_anchors` input):** 12096 — dotted backtick symbol
  `Function._eval_evalf` dropped from `gt_issue_anchors.json` despite the issue naming the defect
  site verbatim → W_CODE_DEF=0.70 never fired → HIGH steer went to the repro-API file. One
  extraction fix converts the run's only confident-wrong steer into a likely rank-1 CORRECT.
- **Candidate-union recall + fusion order (§4 ②/§4.2):** 13453 + 13033 — anchors and FTS5 both held
  the gold's exact symbols, yet gold made none of the 6 slots; 11618 — gold in the union but ordered
  last. All three coincide with a flat/under-covering dense signal at the fusion (sem_mad 0.0–0.024,
  pred_2_coverage False on 2 of 3) — the §11.2 granularity/coverage lever as it actually reaches
  `_total_score`.
- **HIGH-gate calibration (§4.1 (d)/(e)):** 12096's wrong steer PASSED the two anti-confident-wrong
  gates (witnesses genuinely converge on `lambdify`: `lambdify called by _import [CALLS]`,
  `implemented_function` 3 callers) — the gates measure convergence on the anchored file, and when the
  anchors themselves are wrong (extraction defect), convergence is no protection. Gate inputs, not
  gate logic.

---

## VERDICT

**GT's localization failures on this run are 100% rerank-logic (4/4), 0% substrate.** The substrate
the recent fixes targeted is **confirmed solved and green on all 10 tasks**: LSP warm + applied before
scoring 10/10 (`LSP_ACTIVE_VALID`, probes 1.08–1.55 ms, closure rebuilt post-LSP), embedder real and
separating 10/10 (gte-768, 0.71040983 vs 0.29940427, W_SEM floored at 0.25), graph dense 10/10
(det_pct 68.36–74.03, FTS5 probe ok). Not one wrong brief is attributable to a dead gate. What
remains live is exactly the §4.2/§11.2 lever set: issue-anchor extraction (dotted/backtick symbols),
candidate-union recall when the anchor names the gold class, fusion ordering under a flat dense
signal, and the per-task dense dispersion that goes to 0.00000000 on half the tasks. Conversely, GT
conformance to gt_gt is otherwise high: 9/10 tasks executed every stage as specified (the 1 deviation
is 12096's extraction-fed HIGH steer), the abstain/tier ladder behaved honestly (LOW on 11618, MEDIUM
elsewhere, HIGH only twice — once exactly right, once exactly wrong), and where the brief was right
AND consumed (13579) it produced the fastest possible trajectory (gold opened on action 1).

*Computed from: graph_certificate.json, lsp_certificate.json (+per-language), embedder_certificate.json,
foundational_gate_report.json, brief.txt, gt_issue_anchors.json, issue.txt, predictions.jsonl,
outcome.json, <task>.traj.json (chronological tool_calls spot-verification), and the 2026-06-10 §4
audit ledgers (task_ledgers/<task>.md) for first_gold_rank cross-checks.*
