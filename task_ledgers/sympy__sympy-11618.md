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
