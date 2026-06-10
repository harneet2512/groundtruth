# Ledger — django__django-10097

## 2026-06-10 PATH B trial (run 27260307167)

**Arm:** SWE-bench Verified × deepseek-v4-flash (temp=1.0), mini-swe-agent, GT-on.
Outcome: resolved=**NO** (official eval UNRESOLVED). Patch = `django/core/validators.py::URLValidator.regex` — `r'(?:\S+(?::\S*)?@)?'` → `r'(?:[^\s@/]+(?::[^\s@/]*)?@)?'`. Gold = same file, same line, but gold excludes `:` from the user field as well (`[^\s:@/]+`); the hidden test patch ALSO updates `tests/validators/valid_urls.txt` / `invalid_urls.txt`, and the agent calibrated its regex against the STALE in-workspace `valid_urls.txt` (which still contained `http://-.~_!$&'()*+,;=:%40:80%2f::::::@example.com` with colons in the password) — so it deliberately kept `:` legal and diverged from gold. Right file+line, wrong character-class decision driven by stale visible test data.
8-dp: `wall_clock_s=352.88165450`, `gt_injected_tokens_total=686.0`, `action_count=79.0`, `brief_chars=2744.0`.

**One-line trajectory finding:** localization was never in question — the issue names `core.validators.URLValidator`, the agent's first action was `grep -n "class URLValidator" django/core/validators.py`, and GT's L1 ALSO had `django/core/validators.py — URLValidator` at **rank 1** (correct, for once — but redundant with the issue). The miss is a hidden-contract divergence: the agent explicitly considered the gold regex (`[^\s:@/]`, MSG 81: "If we exclude `:` from the password character class, this URL would break") and REJECTED it because the stale valid_urls.txt contradicted it. GT delivered nothing that arbitrated this decision. gt_caused = **FALSE**; classification = **delivery-correct (rank-1) but redundant; agent miss against an unknowable updated test fixture**.

right_trajectory = **PARTIAL** (gold file+line reached; the decisive character-class choice went against gold for a defensible reason) · L1-ranked-gold = **rank 1** · agent-reached-gold = YES (issue + own grep, action 1) · failure locus = **fix detail vs hidden updated test fixtures**.

### L1 brief / localization

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 1 | `<gt-localization confidence="medium"> 1. django/core/validators.py — URLValidator, _lazy_re_compile, RegexValidator / resolved caller: _check_query() in django/contrib/gis/geoip2/base.py:160 2. django/forms/fields.py … 6. django/contrib/admin/utils.py …` + brief #1 contract `raises ValidationError,e | preserve raise: invalid_input -> raise ValidationError(self.message, code=self.code); raise: scheme not in self.schemes -> …` | MSG 2 (tool_calls): `grep -n "class URLValidator" django/core/validators.py` + `sed -n '1,100p' django/core/validators.py` — direct, issue-driven | D=Y · C=**Y at rank 1** (gold file + gold class named; the `__init__ -> calls super(self: Self@BlockNode) [django/template/loader_tags.py:67]` contract line is a false cross-app fact) · C=NO-as-cause (issue named the same target; agent's grep needed no brief) |

### SCOPE

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 3 | `<gt-scope files="5"> 1. core/validators.py — in scope (you are viewing this) 2. utils/functional.py — graph-connected 3. forms/fields.py — graph-connected 4. tests/test_forms.py … 5. migrations/test_autodetector.py …` | Agent stayed in validators.py + tests/validators/ fixtures (its own find at MSG 9) | D=Y · C=PARTIAL (in-scope right; the genuinely needed companion was `tests/validators/*.txt`, which scope did not name — `migrations/test_autodetector.py` is junk) · C=N |

### POST-VIEW evidence

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 3/16 | `[WITNESS] _check_query called by -> django/contrib/gis/geoip2/base.py:160 …` etc. on validators.py | Agent read the regex block and `__call__` IPv6 path | D=Y · C=Y (real but peripheral) · C=N |

### CONTRACT / post-edit

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 90 | `<gt-contract …>` + post_edit evidence after the regex edit | Agent ran validator tests against the STALE fixtures → all pass → false confidence | D=Y · C=Y (interface intact) · C=N — and structurally unable to flag that the fixture files themselves change at eval time |

### NUDGE

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| MSG 48 | `<gt-nudge reason="scaffold_trap"> GT: 25+ actions and no source-file edit yet…` | Agent was mid-analysis of fixture files; edit followed later (MSG 89-93) | D=Y · C=PARTIAL (agent was analyzing, not thrashing) · C=N |
| MSG 96 | `<gt-nudge reason="failure_persisted"> …your current hypothesis is likely wrong…` | Failure was a Django-setup error in the agent's scratch test, then a deliberate revert from `[^\s:@/]` back to `[^\s@/]` (MSG 99-103) | D=Y · C=**ironic** (the nudge fired adjacent to the exact moment the agent abandoned the GOLD regex — but for the stale-fixture reason, not the nudge) · C=N |

**Cross-component:** LEAKAGE = **1** (`[gt-patch:loaded]` at MSG 3). consumed-count = **0** demonstrable of ~7 firings. fair-probe-count: 1 (L1 — correct but redundant). **Context gap (mandatory):** the decision-relevant fact was "the valid/invalid URL fixtures are UPDATED by the hidden test patch; do not calibrate against the stale ones" — unknowable to GT under SWE-bench rules. The in-distribution lesson: when visible fixtures contradict the RFC the issue cites, the issue's normative source (RFC 1738: ":", "@", "/" must be encoded — including in the user field) should outrank stale fixtures; a GT layer that surfaces the issue's normative quote at edit time is the only honest lever this task exposes.
