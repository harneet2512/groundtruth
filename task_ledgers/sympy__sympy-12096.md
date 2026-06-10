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
