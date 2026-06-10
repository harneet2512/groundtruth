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

## 2026-06-10 PATH B trial - gt_trial.md §4+§5 audit (run 27260307167)

**Arm:** SWE-bench Verified x deepseek-v4-flash (temp=1.0), mini-swe-agent, GT-on, substrate `gt-substrate@sha256:db7bd22d...`. Official eval: **NOT RESOLVED**. Audit method: chronological read of the full `django__django-10097.traj.json` messages array incl. `tool_calls` commands (never grep), per gt_trial.md §4 + the AGENT-OBSERVATION rule. Scorecard (8-dp, §5): `.claude/reports/runs/pathB_verified_trial_27260307167/django__django-10097/scorecard.json`.

**TRAJECTORY (lead):** NOT resolved; gold file+line edited, and the agent CONSIDERED the gold-class regex and REJECTED it. L1 rank 1 = gold `django/core/validators.py`/URLValidator (redundant - the issue names URLValidator and quotes RFC 1738 user:pass rules). At MSG 89 the agent installed `[^\s:@/]` (gold-equivalent); its check against the VISIBLE stale `tests/validators/valid_urls.txt` (which the hidden test patch replaces) failed on `http://-.~_!$&'()*+,;=:%40:80%2f::::::@example.com`; the failure_persisted nudge (MSG 96) fired at that exact moment; MSG 99-106 the agent reverted to `[^\s@/]` (colons allowed) -> hidden invalid-URL cases pass validation -> F2P fails. gt_caused=FALSE.

### (a) PREREQS - substrate P1/P2/P3 (gt_trial.md §1.5 gates, verbatim 8-dp)

| substrate gate | 8-dp REAL numbers (verbatim, foundational_gate_report.json) | GREEN? | HOW it reached the agent |
|---|---|---|---|
| P1 receiver-type resolution | `det_pct=72.04979500` - `name_match=13157` - typing tiers: `type_flow=1960 - impl_method=8281 - inherited=3678` (preds A/B/C all true) | GREEN (pass=true) | `resolved caller: _check_query() in django/contrib/gis/geoip2/base.py:160` / `resolved caller: __init__() in django/forms/formsets.py:35` |
| P2 graph.db depth | `calls_edges=47073.0` - resolution_method breakdown: name_match=13157, impl_method=8281, same_file=7381, import=4840, verified_unique=4795, inherited=3678, lsp=2578, type_flow=1960, unique_method=403 - LSP: `LSP_ACTIVE_VALID`, warm probe `1.35684013 ms`, `resolved_promoted=2578.0`, `graph_lsp_edges=2578` (cert==graph, `stamp_mismatch=""`) | GREEN (pass=true) | same resolved-edge lines + post-view `[WITNESS]`/`[CALLERS]` lines (quoted in the L3b table) |
| P3 embedder | `class=EmbeddingModel` - `is_zero=False` - `cos_related=0.71040983` - `cos_unrelated=0.29940427` - `effective_w_sem=0.50000000` (consumption preds 1/2/3 true) | GREEN (pass=true, mode=present_and_consumption) | indirect only - it ORDERS the L1 candidate list the agent sees (no embedder text reaches the agent) |

Substrate numbers are telemetry-only; they reach the agent ONLY as the brief's resolved-edge lines (quoted in the HOW column). Certs reconciled against the runtime witness per gt_gt S12 (no GRAPH_FAIL_MISSING_HANDOFF false-FAIL present on this run; graph cert + LSP cert + embedder cert all PASS).

### (b) L1 brief / localization (ROLE per gt_gt §12: file RANKER - judge by gold-rank + fair reach)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 1 | `<gt-localization confidence="medium"> 1. django/core/validators.py - URLValidator, _lazy_re_compile, RegexValidator / resolved caller: _check_query() in django/contrib/gis/geoip2/base.py:160 ...` | MSG 2 CMDs: `grep -n "class URLValidator" django/core/validators.py` + `sed -n '1,100p' django/core/validators.py` (issue-driven; brief redundant) | D=Y - C=Y (rank-1 = gold; witnesses real) - C=NO (issue already named it) |

**L1 verdict:** delivered, correct-but-redundant (rank-1 gold); not consumed; leakage 0

### (b) consensus / scope (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 3 | `<gt-scope files="5"> 1. core/validators.py - in scope (you are viewing this) 2. utils/functional.py - graph-connected 3. forms/fields.py - graph-connected 4. tests/test_forms.py - graph-connected...` | agent stayed on validators.py | D=Y - C=Y - C=N |

**SCOPE verdict:** delivered, correct, inert; leakage 0

### (b) L3b post-view (ROLE: contract pillar - judge by bug-locus relevance, gt_gt §12)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 3 | `<gt-evidence kind="post_view" file="django/core/validators.py"> [WITNESS] _check_query called by -> django/contrib/gis/geoip2/base.py:160 'validate_ipv46_address(query)' [CALLERS] __init__() in django/forms/fields.py:1163 ... [SIBLINGS] validate_integer, validate_domain_part...` | agent reads the regex block itself | D=Y - C=Y (real edges) - C=N |

**L3b verdict:** delivered, correct, inert; leakage 0

### (b) L3 post-edit contract (`<gt-contract>` + post_edit evidence)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |
|---|---|---|---|
| MSG 90 | `<gt-contract file="validators.py">` after the `sed -i` regex edit | agent tests against valid_urls.txt fixtures | D=Y - C=Y - C=N |

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
| MSG 96 | `<gt-nudge reason="failure_persisted"> GT: the same test failure has persisted across your edit(s) - your current hypothesis is likely wrong. Re-read the failing assertion and reconsider the root cause / target file.` | MSG 99: agent abandons `[^\s:@/]` (the GOLD character class) for `[^\s@/]` citing the stale-fixture failure. The nudge's 'your hypothesis is likely wrong' pointed AWAY from the gold fix | D=Y - C=N (false positive WITH plausible HARM - it reinforced reverting the gold-equivalent edit; proximate cause remains the stale fixture) - C=AMBIGUOUS (revert followed it immediately) |

**L5/L5b verdict:** 3 nudges; the MSG 96 failure_persisted is the run's worst nudge firing - fired against a gold-equivalent edit; leakage 0

### (b) L6 (REINDEXER - gt_gt §12)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| - | DELIVERED=N/A-BY-DESIGN - L6 is the post-edit REINDEXER; on the substrate path the mounted graph.db is authoritative + read-only (witness-hash parity), so single-file reindex is deliberately OFF (gt_gt S12 update / S6 note). 'L6 fired' is the wrong expectation here. | - | N/A |

**L6 verdict:** gated OFF by design on the substrate path - correct behavior, not a failure.

### (c) Cross-component line

LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = 0 positive (1 possible NEGATIVE consumption: the nudge-adjacent revert). fair-probe: NO (issue names URLValidator + the rules). Context-gap (CLAUDE.md mandatory): what GT needed to send = 'visible fixture files are STALE relative to the hidden grader; do not calibrate the character class against valid_urls.txt' - i.e., contract-level guidance, which no layer carries.

### §5 scorecard (stored 8-dp at `django__django-10097/scorecard.json`)

Tier 1: resolved=False - baseline/flip/regression = **N/A** (no frozen SWE-bench Verified baseline exists; the 87/300 frozen file is OH+SWE-bench-Live - stated, not faked).
Tier 2: delivered=1.00000000 - correct=1.00000000 - consumed=0.00000000 - fair_probe=0.00000000 - right_trajectory=0.00000000 - **gt_caused=0.00000000** (gate broke: consumed=0 + fair_probe=0 (issue names URLValidator + quotes the regex). Rank-1 gold correct but redundant. Agent installed the gold-class regex [^\s:@/] (MSG 89-94) then REVERTED to [^\s@/] after the STALE visible valid_urls.txt contradicted it; the failure_persisted nudge (MSG 96) fired at exactly that moment and plausibly reinforced the revert)
Tier 3: gold_in_brief=True - first_gold_rank=1.0 - gold_edited=True - first_edit_action=43.0 - edit_to_gold_action=44.0 - turns_to_gold_view=1.0
Tier 4: action_count=79.00000000 - gt_injected_tokens=686.00000000 - looped_stuck=False - self_localized=True
Tier 6: foundational_gates=GREEN (all_on=true) - test_names_leaked=0 - fail_to_pass_leaked=false - no_gold_labels=true - telemetry stdout leak=1 (`[gt-patch:loaded]`) - VOID=false
Tier 7: llm_in=2275030.00000000 - llm_out=27015.00000000 - llm_cost_usd=0.00000000 (none_litellm_unmapped) - wall_clock_s=352.88165450 - time_to_gold_view_s=0.00000000


### Tier 3b architectural conformance - 2026-06-10 (PATH B run 27260307167)

- **Substrate (verbatim certs):** graph det_pct=72.04979500 (calls=47073, name_match=13157), FTS5 28260 rows probe ok; LSP `LSP_ACTIVE_VALID`, warm probe 1.35684013 ms, verified/corrected/deleted=1289/1289/7, promoted 2578; embedder gte-768 separating (0.71040983 / 0.29940427), **effective_w_sem=0.5**, sem_mad=0.00000000, pred_2_coverage=False. Graph-cert FAIL verdict = documented FALSE FAIL (par.12).
- **Brief vs gold:** gold `django/core/validators.py` at **rank 1** (MEDIUM). Fair probe bad (issue names URLValidator). The miss is the stale-fixture regex calibration + the `failure_persisted` nudge (an L5 non-harm flag, separate from localization).
- **localization_root_cause = CORRECT. gt_conformant = YES** (localization architecture blameless on this miss).
- Cross-run reference: full table + split in `.claude/reports/runs/pathB_verified_trial_27260307167/TIER3B_ARCHITECTURAL_CONFORMANCE.md`. Run-level split: wrong-localization = 4/4 RERANK_LOGIC, 0 LSP_NOT_WARM, 0 EMBEDDER_OFF, 0 GRAPH_SPARSE - substrate solved, rerank logic is the live lever.
