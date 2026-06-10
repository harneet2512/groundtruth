# Ledger — sympy__sympy-11618

## 2026-06-10 PATH B trial (run 27260307167)

**Arm:** SWE-bench Verified × deepseek-v4-flash (temp=1.0), mini-swe-agent, GT-on.
Outcome: resolved=**YES** (official eval RESOLVED). Patch = `sympy/geometry/point.py::distance` — pads the shorter point's args with `S.Zero` before zipping (fixes the dimension-truncating `zip`).
8-dp: `wall_clock_s=225.21403694`, `gt_injected_tokens_total=672.0`, `action_count=68.0`, `brief_chars=2688.0`.

**One-line trajectory finding:** resolve is **SELF-LOCALIZED, NOT GT-caused.** The 250-char issue ("`Point(2,0).distance(Point(1,0,2))` → 1 … 3rd dimension is being ignored when the Points are zipped") names class, method, and root cause. The agent reproduced it, then `grep -n "def distance" sympy/geometry/point.py` (MSG 14) — its own move; `Point` → `geometry/point.py` is trivial. GT's L1 hedged at `confidence="low"` with the gold file at **rank 6 of 6** (rank 1 = `ellipse.py`, with the brief's contract block anchored on `ellipse.py::equation` — wrong file). Notable detour: the agent had to patch py3.11 compat (`from collections import Mapping/Callable`) in `basic.py`/`plot.py` just to run sympy, and GT's post-edit contract fired on those throwaway env edits (delivering `plot.py show()` contracts — correct behavior mechanically, useless contextually). gt_caused = **FALSE**.

right_trajectory = **FALSE for GT-causation** (correct fix from the issue's own diagnosis) · L1-ranked-gold = rank 6/6, confidence=low (honest hedge, wrong headline) · agent-reached-gold = YES (issue-driven) · failure locus = n/a (resolved).

### L1 brief / localization

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 1 | `<gt-localization confidence="low"> 1. sympy/geometry/ellipse.py — Ellipse, __new__, ambient_dimension … 6. sympy/geometry/point.py — distance, Point, __new__ / resolved caller: __new__() in sympy/geometry/line3d.py:45` + brief #1 anchored on ellipse.py with `EDIT-TARGET CONTRACTS (ellipse.py): … encloses_point -> calls distance(self, p) [sympy/geometry/point.py:237]` | MSG 2: "Let me start by understanding the issue… Let me reproduce the issue first." → runs the issue snippet; MSG 14: `grep -n "def distance" sympy/geometry/point.py` → `237:` | D=Y · C=**PARTIAL** (gold file present but LAST at rank 6; headline + contracts on wrong ellipse.py; one buried contract line does carry the exact gold coordinates `distance(self, p) [sympy/geometry/point.py:237]`) · C=NO (agent grepped point.py from the issue; no evidence it lifted `:237` from the brief) |

### SCOPE

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 15 | `<gt-scope files="5"> 1. geometry/point.py — in scope (you are viewing this) 2. geometry/entity.py — graph-connected 3. tests/test_args.py … 4. geometry/curve.py … 5. geometry/ellipse.py …` | Agent stayed in point.py and fixed `distance` | D=Y · C=Y · C=N (trailing) |
| MSG 17/99 | in-scope confirmations | — | D=Y · C=Y · C=N |

### POST-VIEW evidence

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 15 | `[WITNESS] __new__ called by -> sympy/geometry/line3d.py:45 'p1 = Point3D(p1)' / [WITNESS] __new__ called by -> sympy/geometry/plane.py:52 'p1 = Point3D(p1)'` (on point.py) | Agent examined `Point.__new__` dimension handling as part of fix design | D=Y · C=Y (real callers) · C=WEAK |
| MSG 47/125 | further point.py / test evidence | regression runs | D=Y · C=Y · C=N |

### CONTRACT / post-edit

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 13 | `<gt-contract file="plot.py"> [SIGNATURE] def show(self): [CALLERS] show: 6 verified caller(s) in 2 file(s) — preserve this interface …` — fired on the agent's py3.11-compat sed of `plotting/plot.py` | Agent ignored it (env shim, not task code) | D=Y · C=Y-mechanically / N-contextually (contracts for an env-compat edit are noise; +tokens, no value) · C=N |
| MSG 63/89 | `<gt-contract>` + post_edit on point.py after the distance fix | Agent ran geometry tests; interface unchanged | D=Y · C=Y · C=N |

### NUDGE

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 123 | `<gt-nudge …>` (late) | Fix already verified; no change | D=Y · C=N (late) · C=N |

**Cross-component:** LEAKAGE = **1** (`[gt-patch:loaded]` at MSG 3). consumed-count = **0** demonstrable of ~11 firings. fair-probe-count: 1 (L1 at MSG 2 — agent took the issue's path; brief headline would have sent it to ellipse.py). Resolve credit: issue text + agent. GT note: post-edit contracts firing on environment-shim edits (basic.py/plot.py compat seds) is wasted injection — an edit-classifier gap (env-shim edits vs task edits).

## 2026-06-10 PATH B trial - gt_trial.md §4+§5 audit (run 27260307167)

**Arm:** SWE-bench Verified x deepseek-v4-flash (temp=1.0), mini-swe-agent, GT-on, substrate `gt-substrate@sha256:db7bd22d...`. Official eval: **RESOLVED**. Audit method: chronological read of the full `sympy__sympy-11618.traj.json` messages array incl. `tool_calls` commands (never grep), per gt_trial.md §4 + the AGENT-OBSERVATION rule. Scorecard (8-dp, §5): `.claude/reports/runs/pathB_verified_trial_27260307167/sympy__sympy-11618/scorecard.json`.

**TRAJECTORY (lead):** resolve = SELF-LOCALIZED. The 250-char issue contains the exact repro `Point(2,0).distance(Point(1,0,2))` and names the root cause (zip truncation). Agent's action 1 = the issue repro; MSG 14 = its own `grep -n "def distance" sympy/geometry/point.py`. Gold point.py sat at rank 6/6 of a confidence=low list whose headline was wrong (ellipse.py). gt_caused=FALSE.

### (a) PREREQS - substrate P1/P2/P3 (gt_trial.md §1.5 gates, verbatim 8-dp)

| substrate gate | 8-dp REAL numbers (verbatim, foundational_gate_report.json) | GREEN? | HOW it reached the agent |
|---|---|---|---|
| P1 receiver-type resolution | `det_pct=68.36441261` - `name_match=17932` - typing tiers: `type_flow=1144 - impl_method=4865 - inherited=814` (preds A/B/C all true) | GREEN (pass=true) | `resolved caller: control_point() in sympy/physics/quantum/circuitplot.py:244` / `resolved caller: __new__() in sympy/geometry/parabola.py:75` |
| P2 graph.db depth | `calls_edges=56683.0` - resolution_method breakdown: name_match=17932, import=10858, same_file=9756, verified_unique=8708, impl_method=4865, lsp=2485, type_flow=1144, inherited=814, unique_method=121 - LSP: `LSP_ACTIVE_VALID`, warm probe `1.23929977 ms`, `resolved_promoted=2485.0`, `graph_lsp_edges=2485` (cert==graph, `stamp_mismatch=""`) | GREEN (pass=true) | same resolved-edge lines + post-view `[WITNESS]`/`[CALLERS]` lines (quoted in the L3b table) |
| P3 embedder | `class=EmbeddingModel` - `is_zero=False` - `cos_related=0.71040983` - `cos_unrelated=0.29940427` - `effective_w_sem=0.25000000` (consumption preds 1/2/3 true) | GREEN (pass=true, mode=present_and_consumption) | indirect only - it ORDERS the L1 candidate list the agent sees (no embedder text reaches the agent) |

Substrate numbers are telemetry-only; they reach the agent ONLY as the brief's resolved-edge lines (quoted in the HOW column). Certs reconciled against the runtime witness per gt_gt S12 (no GRAPH_FAIL_MISSING_HANDOFF false-FAIL present on this run; graph cert + LSP cert + embedder cert all PASS).

### (b) L1 brief / localization (ROLE per gt_gt §12: file RANKER - judge by gold-rank + fair reach)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 1 | `<gt-localization confidence="low"> 1. sympy/geometry/ellipse.py - Ellipse, __new__, ambient_dimension / resolved caller: control_point() in sympy/physics/quantum/circuitplot.py:244 ... 6. sympy/geometry/point.py - distance, Point, __new__ / resolved caller: __new__() in sympy/geometry/line3d.py:45` | MSG 2 CMD: `python -c "from sympy import Point; print(Point(2,0).distance(Point(1,0,2)))"` (the issue's repro); MSG 14 CMD: `grep -n "def distance" sympy/geometry/point.py` | D=Y - C=PARTIAL (gold present but LAST, headline wrong; confidence=low honestly stated) - C=NO |

**L1 verdict:** delivered, gold-at-rank-6/low-confidence, not consumed; leakage 0

### (b) consensus / scope (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 15 | `<gt-scope files="5"> 1. geometry/point.py - in scope (you are viewing this) 2. geometry/entity.py - graph-connected 3. tests/test_args.py - graph-connected...` | agent already localized to point.py one action earlier | D=Y - C=Y - C=N (trailing) |

**SCOPE verdict:** delivered, trailing; leakage 0

### (b) L3b post-view (ROLE: contract pillar - judge by bug-locus relevance, gt_gt §12)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 15 | `<gt-evidence kind="post_view" file="sympy/geometry/point.py"> [WITNESS] __new__ called by -> sympy/geometry/line3d.py:45 'p1 = Point3D(p1)' ... [SIBLINGS] is_concyclic, is_collinear, is_scalar_multiple, length, origin` | MSG 16: agent seds the distance method body; witnesses uncited | D=Y - C=Y (real edges) - C=N |

**L3b verdict:** delivered, correct, inert; leakage 0

### (b) L3 post-edit contract (`<gt-contract>` + post_edit evidence)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 11/13 | `<gt-contract file="basic.py"> [SIGNATURE] def __new__(cls, *args): [CALLERS] __new__: 106 verified caller(s) in 51 file(s) - preserve this interface ...` (fired on the agent's py3.11-compat sed edits to basic.py/plot.py - NOT the task fix) | agent continued env compat repair | D=Y - C=Y (real, but for env-repair edits) - C=N |

**L3 verdict:** delivered (on env-repair edits), inert; leakage 0

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
| MSG 123 | `<gt-nudge reason="failure_persisted"> GT: the same test failure has persisted across your edit(s) - your current hypothesis is likely wrong...` | failure = `from collections import Mapping` ImportError (py3.11 env), which the agent had deliberately git-checkout-reverted at MSG 118; its task hypothesis was already proven right (sqrt(5) at MSG 131) | D=Y - C=N (FALSE POSITIVE on an env-compat error) - C=N |

**L5/L5b verdict:** 2 nudges; 1 false positive; 0 consumed; leakage 0

### (b) L6 (REINDEXER - gt_gt §12)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| - | DELIVERED=N/A-BY-DESIGN - L6 is the post-edit REINDEXER; on the substrate path the mounted graph.db is authoritative + read-only (witness-hash parity), so single-file reindex is deliberately OFF (gt_gt S12 update / S6 note). 'L6 fired' is the wrong expectation here. | - | N/A |

**L6 verdict:** gated OFF by design on the substrate path - correct behavior, not a failure.

### (c) Cross-component line

LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = 0. fair-probe: NO (issue contains the repro).

### §5 scorecard (stored 8-dp at `sympy__sympy-11618/scorecard.json`)

Tier 1: resolved=True - baseline/flip/regression = **N/A** (no frozen SWE-bench Verified baseline exists; the 87/300 frozen file is OH+SWE-bench-Live - stated, not faked).
Tier 2: delivered=1.00000000 - correct=0.00000000 - consumed=0.00000000 - fair_probe=0.00000000 - right_trajectory=0.00000000 - **gt_caused=0.00000000** (gate broke: correct (gold point.py at rank 6/6 of a confidence=low list; headline = wrong ellipse.py) + fair_probe=0 (250-char issue contains the repro naming Point.distance))
Tier 3: gold_in_brief=True - first_gold_rank=6.0 - gold_edited=True - first_edit_action=5.0 - edit_to_gold_action=27.0 - turns_to_gold_view=7.0
Tier 4: action_count=68.00000000 - gt_injected_tokens=672.00000000 - looped_stuck=False - self_localized=True
Tier 6: foundational_gates=GREEN (all_on=true) - test_names_leaked=0 - fail_to_pass_leaked=false - no_gold_labels=true - telemetry stdout leak=1 (`[gt-patch:loaded]`) - VOID=false
Tier 7: llm_in=991948.00000000 - llm_out=9886.00000000 - llm_cost_usd=0.00000000 (none_litellm_unmapped) - wall_clock_s=225.21403694 - time_to_gold_view_s=14.30868769
