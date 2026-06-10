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
