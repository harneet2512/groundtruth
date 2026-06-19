# gt_audit — desired-state vs current-state parity ledger

Per-task audit of every GT component (the ~52-component inventory) against its desired state from
gt_main (gt_gt.md / gt_new.md). CURRENT STATE is **read from the run's values** (certs + brief bytes +
trajectory observation bytes), never grepped from marker counts. Each row flagged **OK / BUG / GAP /
WEAK**. The aggregate yields a **ready-for-trial / not-ready** verdict.

**Rule (standing):** after EVERY run, append that task's table here + show it in chat + state readiness.
Read the values; "fired" ≠ "delivered". Delivery rows come from the chronological trajectory read.

---

## Run batch 2026-06-15 — post-33ccc1d5 clean witnesses (brief-cache contamination fixed)

HEAD 82af96c3. Substrate-consume path (GT_HOST_GRAPH_DB + GT_CERT_DIR). Model deepseek-v4-flash.
Brief-correctness CONFIRMED both: ts brief → awilix TS symbols (not geo/s2); js brief → csstree JS.

### TASK: awilix-async-container-initialization (TypeScript)

| Component | Desired (gt_main) | Current (read from run) | Flag |
|---|---|---|---|
| graph.db substrate | built, edges resolved | 295 nodes / 526 edges; assertions 345/78; fts5 295 rows, 5 hits | OK |
| resolver / det_pct | facts ≥ floor | verified_ratio **0.95057034** (95% resolved) | OK |
| LSP precision pass | converts issue-residual method calls | attempted 12, **corrected 4**, verified 0, deleted 0, failed 3 (all "empty"), skipped 5, effective_work 4; warm_probe_ok=true, degraded=false, **scoped_source_files=0**, verdict LSP_ACTIVE_VALID | GAP (scoped=0; 3/12 empty) |
| embedder | 768-d semantic rank | dim **768**, semantic_candidate 31, rendered 31, semantic=True | OK |
| localization header | gold in candidate set | medium conf; 6 candidates; gold src/container.ts ranked **#3** (resolvers.ts #1) | OK (gold present, not #1) |
| brief (4 pillars) | contract+consistency+callers+completeness, no name_match laundered | full: graph-map, Contract (raises AwilixTypeError, return), Callers (container.ts:659 verbatim), edit-target contracts (createBuildResolver…), Scope chain, "Also changes: src/container.ts" | OK (laundering check owed to trajectory read) |
| L1 brief | gold ranked, right anchors | right 3 files ranked #1/#2/#3, but anchored on EXISTING symbols (asFunction/AwilixResolutionError) not the new initialize/initializer API; omits 4th gold awilix.ts | PARTIAL |
| L3b post_view | verified callers only | verified, no name_match laundered; idx35 surfaced an irrelevant `examples/**` witness | OK\* (BUG-C) |
| L3 contract | real sigs preserved | all real verified sigs/caller-counts; every interface preserved (158/158 agent tests pass) | OK |
| consensus scope | in-scope source only | **names forbidden `__tests__/awilix.test.ts` as scope member** | BUG-A |
| cochange | co-edit gold | container.ts + resolvers.ts (the other 2 gold) | OK |
| L5/L5b steer | accurate, consumed | 4 steers, diagnoses accurate; agent IGNORED all 4 (ran own loop) | OK-not-consumed |
| leakage gate | 0 | **0** (no test-name target, no F2P/P2P, no gold hint) | OK |
| gt_caused | n/a | **FALSE** — agent self-localized from issue text (names every symbol), GT corroborated not caused | (expected) |
| OUTCOME | right trajectory | reward 0; f2p **13/24**; p2p **162/162 (1.0)**; partial **0.9409**; edited the gold src/container.ts | RIGHT-TRAJECTORY |

**Read:** broke ZERO existing behavior (p2p=1.0), passed 54% of new tests, edited the gold `container.ts`.
The miss is agent implementation-completeness (it punted dependency-aware level ordering — all initializers
→ level 0), NOT a context failure. See OBSERVATION-E: GT HAD the forcing fact (param-parser.ts) in-graph
and didn't deliver it at the decision point — the one place GT could have converted this task.

### TASK: csstree-shorthand-expansion-compression (JavaScript)

| Component | Desired (gt_main) | Current (read from run) | Flag |
|---|---|---|---|
| graph.db substrate | built, edges resolved | 900 nodes / 1857 edges; assertions 506/183; fts5 900 rows, 10 hits | OK |
| resolver / det_pct | facts ≥ floor | verified_ratio **0.46311255** (54% name_match — JS dynamic-typing ceiling) | WEAK (expected for JS) |
| LSP precision pass | converts issue-residual | demand 338, attempted 500 (**cap hit**), corrected 2, verified 1, **deleted 104**, failed 319 (all "empty"), effective_work 107; warm, degraded=false, **scoped_source_files=0** | GAP (cap + scoped=0; 319 empty) |
| embedder | 768-d semantic | dim **768**, semantic_candidate 46, rendered 46, True | OK |
| localization header | gold in set | medium conf; 6 candidates (Lexer.js #1 …) | OK (gold-confirm owed) |
| brief (4 pillars) | no laundering | full: graph-map, Contract (preserve return), Spec, Callers (SyntaxError.js:25), edit-target contracts (buildMatchResult…), Scope chain, "Also changes: lib/lexer/match.js, **test/lexer.js**, lib/lexer/generic.js" | NOTE (cochange surfaced a test file — verify agent didn't edit it) |
| L1 brief | right anchors | names Lexer.js (right), but anchors `match`/contracts the additive task never modifies | PARTIAL |
| L3b post_view | facts redirect | 7/7 delivered, ~6/7 true but orthogonal to an additive task; 0–1 redirected the agent | WEAK |
| L3 contract | preserved | not delivered (agent edited via python/sed, no post-edit hook fired) | N/A |
| consensus scope | source only | folded into L1 scope-chain; ignored (correctly) | OK |
| cochange | co-edit gold | "match.js, test/lexer.js, generic.js" — **0/3 hit** (agent edited Lexer.js + new shorthand.js); names a test file | BUG-A + miss |
| L5 nudge | accurate, consumed | **caught the 3× blind `cat > shorthand.js` rewrite loop; agent's next action = a verification probe** | **OK ✓ (consumed!)** |
| L5b verify | scoped to diff | **"Highest-impact unverified change: push (71 dependents)" — agent never touched `push` (global hub)** | BUG-B |
| leakage gate | 0 | **0** (no test-name, no F2P/P2P, no gold hint; 0 GT_META leak) | OK |
| gt_caused | n/a | **FALSE** — agent self-discovered the mdn-data longhand approach via ~35 probes; GT never named it | (expected) |
| OUTCOME | right trajectory | reward 0; f2p **71/79**; p2p **16715/16715 (1.0)**; partial **0.9995** | RIGHT-TRAJECTORY |

**Read:** near-perfect — broke ZERO of 16,715 existing tests, passed 71/79 new. The L5 nudge is the one
clean POSITIVE GT footprint this batch: it correctly broke a blind-rewrite loop and the agent consumed it.

---

## BUGS surfaced this batch (read from the values, before the trajectory read completes)

1. **[BUG · telemetry · P1] deep_metrics reports `lsp=not_observed_in_log` while the cert shows
   effective_work=4 (ts) / 107 (js).** The deep-metrics LSP field scrapes the run log instead of
   reading lsp_certificate.json. Consequence: a 113-task dashboard would call LSP "dark" when it
   converted/deleted real edges. Fix: deep_metrics consumes the cert (corrected+deleted+verified),
   not the log scrape. (This corrupts gt_audit itself — fix before trusting the trial dashboard.)
2. **[GAP · LSP demand-scoping · DIAGNOSED, generalization] scoped_source_files=0 on BOTH runs.**
   Root cause (LIPI integration/plumbing): `codespace_deepswe_run.sh:152` invokes
   `python -m groundtruth.resolve --db … --root … --resolve --lang $LANG` with **no `--source-files`**,
   so `resolve.py:1446` sets `_scoped_n = 0` and `_get_ambiguous_edges` runs **whole-graph, capped at
   max_edges** (js attempted=500 = the cap). The desired design (CLAUDE.md: demand-driven, scoped to the
   issue subgraph) is never engaged. NOT a correctness break — both runs were right-trajectory with
   scoped=0, and the LSP still did real work (4 ts / 107 js). It is **precision misallocation that bites
   at SCALE**: on a big repo the cap converts arbitrary edges and starves the issue-relevant residual.
   Fix (generalized): compute the issue candidate files (the localizer already does) BEFORE the LSP pass
   and pass them as `--source-files`, making the pass demand-driven per design. Reordering, needs a test;
   defer behind the delivery audit. The "empty" failures (js 319/500) are mostly the JS/TS dynamic
   method-call ceiling (correct-or-quiet: LSP returns empty rather than guessing) — name_match→fact
   matcher (task #3) territory, not an LSP defect.
3. **[WEAK · not a code bug] JS verified_ratio 0.46** — 54% name_match is the documented JS dynamic-
   typing ceiling; LSP deleted 104 garbage. The lever is the name_match→fact matcher (CHA/XTA/PyCG),
   not a defect. The readiness question is whether the brief LAUNDERS any name_match as a fact — owed
   to the trajectory read (the pending _edge_conf_clause / _distinct_files gate findings).

### TASK: fastapi-implicit-head-options (Python) — added to batch 2026-06-15

| Component | Desired | Current (read from run) | Flag |
|---|---|---|---|
| graph.db substrate | built, resolved | 5140 nodes / 5119 edges; assertions 4528/1721; fts5 5140 rows, 963 hits | OK |
| resolver / det_pct | facts ≥ floor | verified_ratio **0.77690955** | OK |
| LSP precision pass | convert residual | **effective_work 183, corrected 25**, deleted 0; scoped_source_files **0**; verdict LSP_ACTIVE_VALID | GAP (scoped=0; B3) |
| embedder | 768-d semantic | dim **768**, semantic=True | OK |
| localization | gold in set | medium; fastapi/applications.py #1, fastapi/routing.py #2 (correct-task) | OK |
| brief | correct-task | fastapi symbols (FastAPI/add_api_route/APIRouter), not contaminated | OK |
| delivery layers | per-turn, consumed | §4.1 read OWED (trajectory archived py_substrate.tgz) | PENDING |
| OUTCOME | right trajectory | reward 0; f2p **18/43**; p2p **3134/3134 (1.0)**; partial **0.9921**; 251 steps; has_patch=True | RIGHT-TRAJECTORY |

**Read:** third right-trajectory witness — broke ZERO of 3134 existing tests, passed 42% of the new tests,
on the correct-task brief. Telemetry note: deep_metrics logged `lsp=pyright/not_observed_in_log` but the
cert records effective_work=183 / corrected=25 — the B2 telemetry fix (committed 64e71394) corrects exactly
this; curly ran on the pre-fix gt_deep_metrics.py.

## DELIVERY-LAYER BUGS (from the chronological trajectory read — the real payload)

- **BUG-A [P1, generalized, BOTH langs] scope/cochange surface FORBIDDEN test files.** TS consensus
  `<gt-scope>` names `__tests__/awilix.test.ts`; JS cochange names `test/lexer.js`. The agent is told
  "DO NOT MODIFY tests." Fix: exclude test paths (`__tests__/**`, `*.test.*`, `*_test.*`, `test/**`,
  `tests/**`, `spec/**`) from scope + cochange delivery. Easy, generalized, correct-or-quiet.
- **BUG-B [P1, generalized] `<gt-verify>` edit-risk selects the repo's highest-degree HUB, not the
  agent's CHANGED symbols, then launders it as fact.** JS msg123: "Highest-impact unverified change:
  push (71 verified dependent(s))" — the agent never touched `push` (List.prototype.push). This is the
  GT_VERIFY_STRUCTURAL_RISK lever (LIVE, forwarded). It must scope to the DIFF (the agent's edited
  symbols), not global max-degree. Exactly the "confident on irrelevant signal" failure CLAUDE.md warns of.
- **BUG-C [P2] L3b post_view surfaces `examples/**` witnesses.** TS idx35: `[WITNESS] getStuff called by
  examples/simple/services/functionalService.js`. Demote/suppress non-source dirs (examples/, fixtures/, docs/).
- **BUG-D [P2/research] L1 anchors additive tasks on EXISTING symbols.** Both langs: an "add a method/file"
  task is anchored on the call-graph/contracts of a pre-existing symbol (match / asFunction), which is less
  useful than the new surface. Additive-task relevance gap.
- **OBSERVATION-E [research, the deepest finding] GT withheld a forcing implementation fact it HAD.** TS:
  the agent punted dependency-aware level ordering (all initializers → level 0 — 4th recurrence of the
  identical punt across 2026-06-10/11/15). `param-parser.ts::parseDependencies` is IN-GRAPH (even in the
  L1 `Calls:` line), but GT never delivered "to order levels, parse constructor params via param-parser.ts"
  at the implementation decision point. The one place GT could have CONVERTED this task and didn't. This is
  the gt_caused lever: orientation is delivered; the implementation-critical fact is not.

## READINESS — verdict: legitimate to trial; fix 2 P1 delivery bugs + telemetry first

STRONG (the Stage-1 bar — MET on TS+JS): right-trajectory both (p2p=1.0, partial 0.94/0.9995); correct-task
brief (contamination closed); substrate/embedder/LSP active+valid; **leakage=0 both**; **no name_match
laundered as fact** (brief gate fixes #12–#17 confirmed working in the delivered bytes); zero regressions.

NOTHING ILLEGITIMATE OR HARMFUL was delivered — a trial would not be benchmaxxing and would not misdirect
via laundered facts. But before the full 113-task leaderboard:
- B2 [FIX] telemetry LSP bug — deep_metrics scrapes the log (`not_observed_in_log`) instead of reading the
  cert (4 ts / 107 js real conversions). Corrupts the metric the trial is scored on. Low-risk fix.
- BUG-A [FIX] test-file scoping (P1, generalized) — quick, removes forbidden-file noise on every task.
- BUG-B [FIX] edit-risk diff-scoping (P1, generalized) — stop shipping a confident-wrong hub steer.
- B3 [DEFER] LSP demand-scoping (`--source-files`) — generalization/scale gap, not a correctness break.
- B4 [CLEARED] brief name_match-gate findings #12–#17 verified FIXED in code (stale task status).
- B5 [PENDING] py still running; go/rust witnesses predate 33ccc1d5 — re-confirm clean post-fix.

gt_caused=FALSE on both is EXPECTED (self-localizing tasks name every symbol) and is NOT a blocker — it
means these two tasks prove PARITY (correct context, equal across langs), not CAUSATION. To PROVE causation
the trial must include baseline-FAILS task ids where the issue text does NOT hand the agent the localization.
