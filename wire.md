# wire.md — Wiring audit: built vs wired-to-live-path vs reached-agent

> Audit 2026-05-29 (workflow `wf_7ea2cd24`, 8 agents, **adversary-survived**:
> no false-wired claims, no missed gaps). Source of truth = the
> `beancount-931` canary trajectory (`output.jsonl`) + the live code, NOT
> telemetry. Trigger: `curation_map` was wired to `v22_brief` while the eval
> runs `v1r_brief` — so we audited everything the same way.
>
> **Headline: ~46 intended pieces; only ~37% genuinely work on the LIVE eval
> path. The session's most expensive build (`curation_map`) reached the agent 0%.**

---

## The LIVE eval path (proven)

```
canary_3arm.yml (arm v2_live)
  -> oh_gt_full_wrapper.py
     -> generate_v1r_brief        (wrapper:5800 import, :5815 invoke, IN-CONTAINER)
        -> instance['gt_brief']   (wrapper:5985)
     -> generate_task_brief       (wrapper:5996) returns instance['gt_brief'] FIRST (:5999-6006)
        => v22_brief.generate_brief (wrapper:6023) is NEVER reached
           (also gated on GT_PREBUILT_INDEXES_ROOT/GT_REPO_EXTRACTS_ROOT the canary never sets)
     -> in-container hooks: groundtruth.hooks.post_edit (:1004) / post_view (:976)
```

- Agent instruction (len 7148) was **v1r format**: `<gt-task-brief>` + numbered
  files `1. beancount/ops/balance.py …` + `Callers:`/`Calls:`/`Tests:`/`Spec:`/`Context:`.
- Agent-history marker census: `gt-graph-map`=0, `## Focus files`=0,
  `gt-focus-functions`=0, `curation`=0.
- **Three briefs documented, one fires:** DOC_OF_HONOR §2.1 names
  `brief/graph_map.py` (never imported, Grep=0); we_did §489-495 + DOC §1703-1709
  name `v22_brief`+`curation_map` "the production brief"; the canary runs **v1r**.

---

## The 3 findings that matter most

### 1. Headline build reached the agent 0% — `curation_map` → dead `v22`
`curation_map.py` (build_function_map/render_map, `_DETERMINISTIC_METHODS` gate,
`_NAME_MATCH_FLOOR=0.5`, agreement-guard) is imported **only** by `v22_brief.py:178`.
`v22.generate_brief` never runs (pre-empted by v1r's `instance['gt_brief']`). So
the entire Session-2026-05-28b headline (curation_map + v22 rank-tier removal +
RC-08 silent_failures) is **invisible to the agent**.

### 2. The live `v1r` brief launders `name_match` as fact — PROVEN
`v1r_brief._caller_contract_for_file` (:259-291) gates HI callers on
`confidence >= 0.9` **only — no `resolution_method` check** (`resolution_method`
appears in v1r only at :1073, an unrelated scope-hint). A single-candidate
`name_match` scores 0.9 → rendered as a confident `Callers:` fact with a code
snippet. **PROVEN:** the brief's file-2 (`account.py::walk`) showed stdlib
`os.walk` calls (`find_files`/`find_python_files`) as callers of beancount's
`account.walk`. `curation_map`'s `is_fact` gate would suppress this — but it's
on dead v22.

### 3. The actual reason for NO FLIP — `v1r` MISLOCALIZED
Gold = `beancount/plugins/leafonly.py::validate_leaf_only`
(FAIL_TO_PASS = `leafonly_test.py::test_leaf_only3`). The v1r brief ranked
`balance.py`/`account.py`/`options.py` and **never named `leafonly.py`**. (The
GT-curated `<gt-task-brief>` block had leafonly=0; the agent surfaced it itself
at history[5].) Every downstream layer (verify, scope) inherited the wrong scope.
Correct context missing → wrong code → no flip.

---

## Coverage matrix (~46 pieces)

### WORKING_AS_INTENDED (~17) — genuinely live + reached agent
- L3 `post_edit` hook — 13 `<gt-evidence trigger=post_edit>` blocks reached agent.
- L3b `post_view` graph_navigation — 27 views, emitted 3.
- **L3b Contract-pillar ALWAYS-FIRE** (`post_view.py:42-86`, insert :801-810) —
  `[CONTRACT]` ×40 incl. the 0-caller gold `validate_leaf_only`. **The real
  2026-05-28 win on the live path** (delivered G7's value on the VIEW side).
- **Correct-or-quiet at the agent boundary** — agent content has
  `[VERIFIED]`=0, `[WARNING]`=0, `[INFO]`=0, `name_match`=0, `dedup="true"`=0,
  `[GT_META]`=0. Tier/telemetry strings live only in host stderr.
- L1+ orientation composite (`composite.py`, wired :6264-6316) — `<gt-orientation>` reached agent.
- `[GT KEY CONTRACTS]` block; L5b late reminder; L6 incremental reindex (×7);
  L6 pre-submit verify (fired iter 52, mid-trajectory); router_v2 (40 ev / 32 emitted);
  GT_META-to-stderr; no empty-dedup noise; scope consensus; v1r max_files clamp;
  RF-1 `GT_LSP_VERIFY=0` (verifier never armed); RF-3 step-4 db-upload PLUMBING
  ("prebuilt_db uploaded OK … in-container build skipped", brief read it: files=102).

### BUILT_NOT_WIRED (~9) — built, NOT on the live path
- **`curation_map` → v22 only** (THE headline; agent got 0 `<gt-graph-map>`).
- v22 rank-tier removal — correct change, on dead v22; v1r already tier-as-filter → no-op.
- RC-08 `silent_failures.record('v22_brief.rank_files')` — on dead v22.
- **`path_resolver.resolve_to_stored_path`** (17 unit tests) — **ZERO consumers**;
  wrapper/post_edit/post_view still inline-normalize. we_did:170 admits "not swept";
  DOC §1.1 "FIXED" overstates.
- **C4** LSPManager `progress_timeout=5.0` — MCP-server path only; canary registered
  **0 MCP tools** (`_tools_count:0` ×3). Protects nobody this eval.
- **C7** transitive-closure sidecar (Go + graph.py reader) — **no live consumer**:
  impact/trace are MCP-only (0 calls); the live `ImportGraph` (post_edit:3677) feeds
  `run_obligations`, which never calls find_callers/find_callees. Dead weight this run.
- G7 always-fire on `post_edit` — wired (:2669) but never triggered (edited funcs had callers; `g7_gate`=0).
- `brief/graph_map.py` — documented L1 module, never imported.

### WIRED_WRONG_VERSION (~7) — live but wrong content/version
- **v1r caller gate launders name_match** (finding #2) — fix is in curation_map (dead v22).
- **v1r mislocalization** (finding #3) — never surfaced the gold `leafonly.py`.
- **False `[MISMATCH]`** — 3 distinct symbols (`entry`@h89, `BalanceError`@h93 vs
  balance_test; `meta`@h95 vs validation_test), each printed twice (×6 lines). The
  agent removed none; total_reactions=0. Correct-or-quiet violation.
- **Wrong-file post-edit label** — `<gt-post-edit file="validation.py">` carried
  `balance.py`'s `check()` contract (`[SIGNATURE] def check(entries, options_map)`)
  (history[130]). Cross-file labeling bug.
- L6 `[GT_VERIFY]` pointed at `balance_test.py` (×93), never `leafonly_test.py` —
  inherited the mislocalization.
- DOC §2.1 graph_map format (`## Task:`/`Called by:`/`Contract:`) vs live v1r format — stale.
- DOC Run-3 "VERIFIED WORKING" table (:1664-1675) — stale 2-task replay; cells
  contradict current code.

### PARTIAL (~6)
- categorical edge filter — outcome OK (no name_match to agent) but clause selection unproven (no log).
- C5 read pragmas — only the post_edit read slice confirmed.
- C6 LSP promotion — unobservable + the name_match false positive survived to the agent.
- RF-3(d) re-promotion — no-op (`lsp_absent(pyright-langserver)` ×6 in-container).
- L3b ego-graph — 0/13 (3 conjunctive gates at post_view:686-694); the `[CONTRACT]`
  that reached the agent is the DIFFERENT `_contract_pillar`, not `ego_graph().render()`.
- L4 prefetch — no results.
- Overall product contract — fired-not-delivered, **no flip** this task.

### CLAIMED_NOT_BUILT (1)
- CLAUDE.md TTD section (lines 277-291) — references the retired SWE-agent steering
  apparatus (`material_edit_total`/`ack_armed_total`/`delivery_rate`/`verify_report`);
  modules archived (`tests/_archive_swe_agent/`). DOC §1733-1734 self-flags it stale.

---

## Prioritized fixes (do on the LIVE v1r path)

1. **[DONE — commit d1e220e8]** Wire the curation map onto `v1r.render_brief` —
   `render_brief(graph_db=...)` now appends a `<gt-graph-map>` sibling block via
   `curation_map.build_function_map/render_map` for the top focus functions
   (top-3 shown files × top-1 issue-prioritized function, 1-hop, max 3 neighbors).
   Correct-or-quiet: empty when no edge clears the bar. Agent got zero graph-map
   before. Threaded through both `render_brief` call sites in `generate_v1r_brief`.
2. **[PARTIAL — gate code correct, laundering STILL ALIVE at runtime]** Replaced
   v1r's `confidence>=0.9` caller gate with `curation_map`'s categorical rule.
   `_caller_contract_for_file` imports `_DETERMINISTIC_METHODS`/`_NAME_MATCH_FLOOR`:
   a caller is a FACT only for deterministic `resolution_method`; name_match is
   never a fact (suppress <0.5, `file:line (unverified)` ≥0.5). TTD red-before-green
   on synthetic name_match edges (4 tests fail pre-fix, pass after); 1466 tests pass.
   **BUT the live beancount-931 run (canary 26619606504) STILL rendered
   `find_files() in tools/check_num_args.py:18 \`...os.walk(rootdir)...\`` as a
   confident caller FACT of `account.walk`** — `(unverified)` appears 0× in the
   brief. The gate is correct; it never fired because the graph.db tags those
   `os.walk`→`account.walk` edges with a DETERMINISTIC `resolution_method`
   (same_file/import), not name_match. **Fix locus = the Go indexer/name-match
   resolver (provenance is a lie), NOT v1r.** My commit message + the docs that
   said "kills the laundering" were an OVER-CLAIM — corrected. See RUN VERDICT below.
3. **[BLOCKED on data — do NOT ship a speculative fix]** The localization miss.
   See "#31 root-cause" below. The mechanism is hypothesized but UNCONFIRMED, and
   the candidate fixes (cap `W_REACH`, boost isolated trusted anchors) are weight
   tunings that risk regressing other repos on an N=1 beancount theory. Gate any
   fix on the experiment below.
4. **[DONE — commit pending]** Demoted v22 + `brief/graph_map.py` (deprecation
   headers pointing at v1r; not deleted — the wrapper still imports v22 as the
   prebuilt-index fallback). Fixed DOC §2.1 (now names `v1r_brief` as the LIVE
   module, shows the real format + the categorical caller rule) and we_did (the
   "production brief" claim corrected; v22 work flagged as reached-agent-0%).
5. Fix post-edit wrong-file labeling + false `[MISMATCH]` (per-file scoping assertion +
   verify removed-symbol detection against the actual diff).
6. Wire `path_resolver` into consumers (post_edit/post_view/wrapper/v1r) **or** retract DOC §1.1 "FIXED".
7. Add an in-container `[GT_META] edge_filter_mode=categorical|legacy` log line (observability).
8. Install `pyright-langserver` in the eval container, or stop claiming LSP-verified edges
   (RF-3(d) re-promotion is a no-op without it; post-edit edges revert to name_match).
9. Give C7 closure a live consumer (have `run_obligations` use `ImportGraph.find_callers`)
   or stop building it default-on.
10. Update CLAUDE.md TTD section — drop the retired steering apparatus; re-anchor on the
    live OpenHands+v1r path.
11. Create tasks that exercise G7-on-post_edit (isolated-function edit) and L5
    scaffold-advisory (scratch-file-first edit) — built+wired but unexercised in beancount-931.

---

## Adversary corrections folded in
- The whole-trajectory `leafonly` counts are agent-driven exploration, NOT GT
  localization — the GT-curated brief block had leafonly=0 (this is the real claim).
- `[MISMATCH]` = 3 distinct symbols printed twice (×6 lines), not 6 distinct claims;
  ≥1 targets `validation_test.py`, consistent with the cross-file labeling bug.
- v1r was confirmed the brief that actually fired (`GT_BRIEF_DIAG db=/tmp/gt_index.db
  files=102`, `GT_BRIEF_FAILED=0`) — no fallback brief was substituted.

---

## #31 root-cause: why v1r never surfaced `beancount/plugins/leafonly.py`

> Investigated 2026-05-29 (background agent `a4d80bdd`, read of `v7_4_brief.py`,
> `anchor_select.py`, `graph_reach.py`, `hybrid.py`, `v1r_brief.py`). Status:
> **mechanism HYPOTHESIS, not confirmed.** Verdict deliberately withheld — the
> local data cannot settle it and a speculative fix is forbidden here.

**The hypothesis (agent verdict: CANDIDATE_BUT_UNDERRANKED).** `leafonly.py` is an
isolated plugin module: SWE-bench-Live registers plugins by string name in config,
so the static call graph has **zero incoming edges** to `validate_leaf_only`.
The v7.4 ranker score is `W_SEM·sem + W_LEX·lex + W_REACH·reach + W_PROX·prox +
W_HUB·hub + W_PATH·path`. With zero incoming edges, leafonly.py gets **reach=0 and
no anchor-proximity boost to itself** (it can be an anchor that expands outward,
but BFS never reaches *it*). Core files (`account.py`, `realization.py`,
`options.py`) carry high reach and dominate the top-5; the `min_k=min(5,…)` clamp
then drops leafonly.py.

**Why I will NOT ship a fix on this:**
- **The hypothesis conflicts with v1r's own rescue paths.** `generate_v1r_brief`
  ALREADY has (a) a path-rescue that pulls any candidate with
  `components["path"] >= 0.5` into the top-5, and (b) an issue-keyword boost that
  re-ranks `top_records` by path/function overlap with issue terms. If leafonly.py
  were a candidate with the ~0.7–1.0 path score the agent *assumes*, BOTH would
  have surfaced it — yet it never appeared. So either (i) leafonly.py was
  **NEVER in `v74.ranked_full`** (candidate-universe/seeding gap, a different root
  cause than the agent's verdict), or (ii) its **path-component score was <0.5**
  so path-rescue correctly skipped it (a path-scorer undercrediting bug). The
  agent did not measure `components["path"]`, so the verdict is unresolved.
- The candidate fixes (lower/cap `W_REACH`; bidirectional closure; boost
  isolated trusted anchors) are weight/architecture tunings. Shipping them on one
  task's reasoning is the "causal overfitting from small data" failure mode and
  violates "principle not benchmaxxing / verify on held-out repos first."

**The experiment that settles it (free, no API).** The local artifacts only log
`ranked_count`, never the ranked list with components — that is the missing datum.
Settle it one of two ways:
1. **Instrumented rerun** — dump `v74.ranked_full` (path + per-signal `components`
   + score) for the top ~20, plus a `path_rescue_eligible` flag, behind the debug
   path. Then a single canary tells us exactly: is leafonly.py absent (→ fix the
   candidate seeding) or present-but-low (→ inspect path-component score / reach
   dominance). This is the right next step before any ranker change.
2. **Local reproduction** — needs Go+gcc (absent on this box), a built `gt-index`,
   and a beancount checkout at the task `base_commit`; then run `run_v74` on the
   resulting `graph.db`. Heavier; container-vs-Windows index may differ.

**Generalized fix direction (only after the experiment confirms which gap):**
- If NEVER_A_CANDIDATE → seed the candidate universe from issue-text symbol/path
  tokens via FTS over **all** files, not just import-graph-reachable ones (isolated
  plugins/modules are reachable by name, not by edges). Repo/language-agnostic.
- If path-score-<0.5 → fix the path scorer to credit a basename stem that is a
  substring union of issue tokens (`leafonly` ⊇ `leaf`+`only`). Still generalized.
- Reach-dominance saturation (cap reach contribution) is a candidate ONLY if the
  experiment shows leafonly.py present with a strong lexical/path score that reach
  overpowered — and must be validated on holdout before the 6-task gate.

---

## RUN VERDICT — beancount-931 v1r-fix canary (run 26619606504, 2026-05-29)

> Verified against `eval_result.json`, the agent's `output.jsonl` observation, and
> the live source (audit workflow `wf_1eafd4e6`, adversary-corrected: one finder
> falsely claimed "curation_map not wired / render_brief has no callers" — REFUTED
> against the live code; the map IS wired and the brief IS from v1r.render_brief).

**OUTCOME: resolved, NOT a flip.** `resolved=true`, FAIL_TO_PASS `test_leaf_only3`
passed — but baseline also resolves beancount-931. No regression; not a win. GT was
**net-neutral**: the agent localized `leafonly.py` from the **issue text** ("leafonly
plugin"), not from GT's brief — leafonly.py appeared **0× in the visible numbered
brief** (ranked #3 in RANK_DIAG, dropped by the [INFO]-filter/cut). The fix was
100% agent-discovered. L3b `[CONTRACT]` on view delivered the signature *after* the
agent opened the file — confirmation, not localization.

**#30 — split verdict:**
- `<gt-graph-map>` wiring: **WORKS** (runtime block present, correctly focused on
  `leaf`, real callers — `_with_graph_map` v1r_brief.py:716-745 is live).
- caller laundering: **STILL ALIVE.** The gate CODE is correct (FACT form only for
  deterministic methods; `(unverified)` 0× in brief) — so the `os.walk`→`account.walk`
  edges are tagged DETERMINISTIC in graph.db, not name_match. **Fix locus = Go
  indexer/resolver provenance, NOT v1r.** Secondary defense: a stdlib-shadow guard in
  `_caller_contract_for_file` (drop a caller whose rendered code is `<stdlib>.<name>(`
  where `<name>`==target — general: project `walk`/`join`/`split`/`open` collide with
  stdlib on any repo).

**#31 — NOT settled, real and unfixed.** leafonly.py (`reach=0.0`, `lex=0.453`,
`path_comp=1.0`) was dropped from the numbered brief: the ranker + issue-keyword
re-rank + [INFO]-drop filter prefer **connected-wrong (account.py) over
isolated-right (leafonly.py)** — the constitution inversion. Saved only by the issue
text naming the plugin. On a task whose gold is NOT named in the issue, GT contributes
nothing here. Fix (do not special-case plugins): a high-`path_comp` + strong
issue-overlap candidate must not be filtered/cut purely for `reach=0`.

**Prioritized next actions (smallest first; do NOT ship a fix on an unverified
provenance hypothesis):**
1. **Persist graph.db on one canary run** + dump `resolution_method,confidence` for
   `*→walk@account.py`. Confirms/kills the indexer-mistag hypothesis (currently
   moderate confidence — the run's `/tmp/gt_index.db` is ephemeral, 0 `.db` in artifacts).
2. **stdlib-shadow guard** in `_caller_contract_for_file` (small, general, reversible;
   red-before-green test from this exact `os.walk` artifact).
3. **Go resolver fix**: set `resolution_method='name_match'` on any cross-module
   name-matched edge, overriding callsite-local provenance. Gate behind #1.
4. **#31 localization inversion** (largest): don't filter/cut a high-path_comp +
   strong-issue-overlap candidate just for `reach=0`. Validate on holdout_v1, not beancount.

**Over-claims corrected (this file, we_did, DOC §2.1, memory):** "categorical gate
kills the os.walk laundering" was FALSE at runtime; "#30 done" → map-wired ✓ but
laundering ✗; "#31 settled" → NOT settled. Commit d1e220e8's message stands in git
history but is corrected here.

### RESOLVED 2026-05-29 — laundering root cause found + fixed (CI-confirmed, free)

Empirical RED→GREEN in free CI (no graph.db needed). Root cause pinpointed by a Go
resolver test, NOT a guess:
- **RED** (CI 26622667818): `TestResolve_QualifiedStdlibCall_NotDeterministic`
  failed — `os.walk resolved to project walk with DETERMINISTIC method
  "verified_unique" (0.95)`. Mis-tag is **Strategy 1.9** (resolver.go:616): it tags
  any globally-unique bare callee name as `verified_unique` without checking the
  qualifier. (First investigation's "import stage" guess was WRONG; the trajectory
  verdict's "graph provenance mis-tag" was right.)
- **FIX** (44947d75 → c7e7e5d0): a qualified `X.attr(...)` reaching Strategy 1.9 has
  an unresolved receiver (stdlib/external) → DEMOTE the single match from
  `verified_unique` (deterministic) to `name_match` (low trust). First attempt
  *skipped* it and dropped the edge (broke `PreservesNameMatch` — Strategy 2 needs
  2+ candidates); corrected to demote-not-drop.
- **GREEN** (CI 26622950466): all 5 resolver tests pass — probe + PreservesNameMatch
  + bare-unique-stays-verified_unique (ACG/ECOOP-2022 preserved for unqualified).

**Laundering fixed at BOTH layers:** (a) source — resolver tags `os.walk` as
`name_match`, never a fact; (b) consumer — v1r stdlib-shadow guard (ccdd6aa7) drops
it regardless. The graph.db-dump diagnostic (action #1) is now MOOT. Remaining: paid
canary re-run for end-to-end proof on the live agent path.
