# gt_new.md — what changed since gt_gt (this session's deltas, mapped to the architecture)

> Companion to `gt_gt.md`. gt_gt describes the architecture; **gt_new catalogues what
> the code now does that gt_gt did not** — per component, with the new file:line, the
> commit, the gt_gt section it extends, and an honest TESTED-vs-WITNESSED status.
> Branch `gt-trial`, local, unpushed. Witness gate = Task #6 (live run on a re-indexed
> binary, read from `output.jsonl`).

Status legend: **PROVEN** = measured behavioral delta on real input. **TESTED** =
unit/integration red→green, no live agent yet. **WITNESS-OWED** = the live run converts
TESTED→PROVEN. **PARTIAL** = work explicitly owed.

---

## 1. Substrate — graph.db depth (gt_gt §2.6)

| what gt_gt said | what the code now does | evidence | status |
|---|---|---|---|
| property kinds stored as strings, "schema-present-but-dead" (§2.5); promote was an offline copies script | **Pass 4f** runs IMPORTS + `PromotePropertyEdges` in production (`gt-index/cmd/gt-index/main.go`, `internal/resolver/promote.go` 956 LoC, `imports.go` 335 LoC). READS/WRITES/IMPORTS/DATA_FLOW/PRECEDES/CO_SERIALIZES become traversable edges | `b5ceaf5d`,`9860ff7e`; measured 5 langs (go 2→7 edge kinds/0→77 rel edges; py 2→5/0→23; ts 3→7/13→86; rust 2→4/0→14; js 2→5/0→5) | **PROVEN** (0 orphans, 0 laundered) |

## 2. Substrate — naming (gt_gt §2.3, the method-call gap)

| gt_gt | now | evidence | status |
|---|---|---|---|
| method calls fall to `name_match` (guesses) | **CHA/XTA rung-2b** matcher resolves declared-field-type receivers (Py+Rust→Go+TS); package-qualified Go abstains | `ec20d603`,`71d66378` (`resolver.go` +470); `resolver_fieldtype_test.go` | **TESTED**; on the 5 real repos it produced **0 measured lift** — the residual `name_match` are all matcher-ineligible (rust free-fns, ts builtins) → correct-or-quiet, but **unwitnessed-for-lift** |

## 3. Localization + brief (gt_gt §4, §11)

| gt_gt | now | evidence | status |
|---|---|---|---|
| symbol naming from defines_anchor only → empty on behavior-described issues | **R1 leaf-naming bridge** (`v1r_brief._semantic_leaf_names`): per-symbol MaxSim (was discarded) + per-symbol FTS5, RRF-fused, symbol-hub-demoted; fires ONLY when the anchor named nothing | `74e90817` | **TESTED** (11 tests), correct-or-quiet |
| scope chains "verified edges only", BFS | **union-find** over CALLS/IMPORTS + promoted edges; per-edge **trust tiers** rendered (`(CANDIDATE)`/`(unverified)`) so a `name_match` scope edge is never laundered as a fact (finding A) | `74e90817` | **TESTED** (3 tests) |
| `n_components` computed, **zero consumers** (dead nerve) | wired to the GT_META/8-dp telemetry (`SCOPE_COMPONENTS n_components=…`) | `d7e64a8f` | **TESTED** (regression-locked) |
| degree mixes edge types | D5 fan-in/fan-out **type-scoped** so promoted edges don't skew the degree prior | `9860ff7e` | **TESTED** |

## 4. The oracle + delivery (gt_gt §15)

| gt_gt | now | evidence | status |
|---|---|---|---|
| per-turn gate stubbed; monolithic delivery (oracle crash → agent gets nothing) | **hybrid data-plane/control-plane bulkhead**: Lane A (context, always-on) delivers BEFORE Lane B (steers, isolated try/except) → a steer crash can't undo context (0/8 stub-crash closed) | `35a3fb17`,`32e4e313` | **TESTED** (fault-injection) |
| edit-credit blind to patch channels | **RC5 hybrid edit-credit** (apply_patch/git-apply/patch + staged diffs, ≥3-signal FACT-tier); dry-run flags excluded | `a7a4be87`,`e6ddc06e` | **TESTED** |
| in-container ran a divergent inline fallback (≠ Product) | **F2+F5**: ships real `runtime`/`delivery`/`pretask` packages with fail-closed import-coverage | `95dff1d9` | **TESTED** |
| edit detection = closed verb whitelist | **F3** structured-action-first (any harness schema); replay parity in `gt_oracle_sense` | `d8cfd3d1`,`74e90817` | **TESTED** |
| agent-container OOM unclassified | **F4** `GT_AGENT_OOM` classification + grep-hardening | `a5747a9c` | **PARTIAL** — the memory **cap injection** is still inert (CI pip-installs pier) |

## 5. NEW component — structural edit-risk verification (no gt_gt section yet; extends §15 verification horizon)

The 2026-research-synthesized verification-timing signal. gt_gt's horizon was
budget-fraction-anchored; the field (RisCoSet/Anytime-Verified-Agents/PreFlect 2026)
moved to **risk-targeted adaptive** verification, and Mirror 2026 proved external
deterministic control beats agent self-assessment (76% vs ~0%).

| now | evidence | status |
|---|---|---|
| `runtime/edit_risk.py`: verification risk = **verified caller fan-in (blast radius)** of edited-but-untested symbols, scored RELATIVE to the repo's own fan-in distribution (dynamic, repo-relative); correct-or-quiet; name_match never counts. The white space no frontier paper occupies (they target MODEL uncertainty; GT targets CODE structural risk) | `437790dc` (7 tests) | **TESTED** |
| wired into the verify producer behind `GT_VERIFY_STRUCTURAL_RISK` (default OFF): a high-blast-radius untested edit earns a **budget-free** advisory that NAMES the risk | `437790dc` (5 wire tests) | **TESTED, default-off** |
| verify wording de-benchmarked: `submit/submission` → `finalize` | `437790dc` | **TESTED** |

---

## 6. Depth trickle-down — what consumes the new depth, and what's still owed

The depth edges (§1) must "trickle down" to make lower components richer **without
over-promoting hubs** (the BRIEFING invariant: depth feeds SCOPE/COMPLETENESS, never
RANK — reach over-promotes hubs, the architecture subordinates it on purpose).

| consumer | consumes depth today? | owed |
|---|---|---|
| scope chains (completeness) | **YES** — union-find over CALLS/IMPORTS + promoted edges | — |
| edit-risk (verification) | partial — CALLS fan-in only | could add READS/WRITES blast radius (a symbol written-by-many is also risky) |
| localizer rank | **deliberately NO** | correct — depth must not enter `_rrf3`/degree (hub over-promotion) |
| impact / co-change families | NO | could read promoted CO_SERIALIZES/co-change edges for richer blast radius |
| brief contract pillar | NO | could surface READS/WRITES of the edit target |

**The owed trickle-down is a careful wiring task** (each new consumer must be
correct-or-quiet + must not feed rank). Not done in this session.

---

## 7. Trial readiness — what is actually left

1. **Re-index** a real repo with the current binary so the depth/naming the agent sees
   matches the binary (the last witness DBs are stale — promoted edges absent).
2. **Live witness (Task #6):** run on the fresh index, read `output.jsonl`
   chronologically on a baseline-fails id, paired vs the frozen baseline. This converts
   every **TESTED** row above to PROVEN-or-not.
3. **Depth trickle-down wiring** (§6) — careful, correct-or-quiet, rank-safe.
4. **Full-surface LIPI** of GT + the mini-swe-agent surface before the paid run.
5. **F4 cap injection** (§4) — still owed.

Everything in §§1–5 is committed + tested + LIPI'd-per-commit. The gate that makes any
of it "done" (DEFINITION OF DONE: metrics changed) is **#2, the live witness** — the one
thing this session cannot self-certify.

---

## 8. Local witness — gt_new running on a real re-indexed graph (2026-06-14)

Re-indexed `src/groundtruth` (305 files, 2502 nodes, 5462 edges, **5.5s**) with the
current binary, then ran the pipeline end-to-end:

- **Reindex (prerequisite #1): coded + working.** Full Pass 4f (`main.go:623-643`) +
  incremental re-promote (`main.go:1114-1124`). Produced the depth + naming below.
- **Substrate.** Depth = `CALLS 3889 + CONTAINS 689 + READS 428 + PRECEDES 164 +
  WRITES 164 + DATA_FLOW 74 + EXTENDS 46 + RAISES 4 + CO_SERIALIZES 2 + IMPORTS 2`.
  Naming = **92.1% facts / 7.9% name_match**. **0 orphans, 0 laundered.** (IMPORTS: 1965
  import statements → 2 edges — the rest stdlib/3rd-party, abstained, correct-or-quiet.)
- **Brief** (`generate_v1r_brief` on a verify-horizon issue): localized candidate #2 to
  `runtime/verification_horizon.py :: verify_horizon_band` — the **correct gold function**
  — with its contract + emission spec (incl. the de-benchmarked "Do not finalize unverified
  work"). `L1_SCOPE=low` + grep-fallback note (**correct-or-quiet, no over-claimed HIGH
  steer**). Wires fired live: `SCOPE_COMPONENTS n_components`, `BUG3_ANCHOR_PROX`.
- **Verification** (structural edit-risk on the real graph): editing `set` (209 deep
  verified dependents) → risk **0.9905**; a leaf → **quiet**. The agent-facing advisory
  NAMES the specific risk — *"find_symbol_by_name (28 verified dependent(s) in the graph)
  — no test has exercised your change"* — budget-free, de-benchmarked.

**STATUS: PROVEN-LOCAL.** The reindex → substrate → brief → verification path runs
end-to-end on a real re-indexed graph and produces relevant + honest output. This is NOT
the live agent witness — whether the AGENT consumes this and flips is still owed (Task #6).
The DEFINITION OF DONE (metrics changed on a billed run) remains the one unconverted gate.

---

## Appendix A — DEPTH before/after (gt_gt §2.6 spec vs the current code, read line by line)

**Framing.** gt_gt §2.6 was updated this session to record the landing (it cites
`b5ceaf5d`, "Pass 4f LANDED"), so it is now a spec **+** landing-record, not a clean
"before". The true BEFORE is the pre-session state §2.6 describes; the AFTER is the code
as it actually runs — `promote.go` (991 lines) + `imports.go` (335 lines), both read in
full. **Verdict: the code faithfully implements the §2.6 spec; no drift found.** The
difference is the substrate transformation + precision details the prose under-states.

### Per-edge-class: gt_gt spec (BEFORE) → code reality (AFTER)
| edge class | gt_gt §2.6 says | code does (file:line) | confidence | match |
|---|---|---|---|---|
| **CO_SERIALIZES** | PROMOTE-NOW, 100% resolvable, value carries `@file:line`, undirected | `promoteSerde` parses `partner:<name>@file:<line>`, exact `(file,name,line)` key + any-file fallback, undirected dedup on min/max id (`promote.go:437-472, 230-236`) | **1.0 CERTIFIED** | ✓ |
| **READS** | PROMOTE-PARTIAL, reader → owning Class via parent_id; field-only stays property | `promoteFieldReads` → owning Class via `src.ParentID` (`label=Class`) (`:478-505`) | 0.6, **lifts to 0.9 if a declared `class_field`** (`:486`) | ✓ + precision |
| **WRITES** | side_effect write → owning Class | `promoteWrites`, same owning-Class resolve (`:511-534`) | 0.6 / 0.9 (declared field) | ✓ |
| **RAISES** | PROMOTE-PARTIAL, internal classes only, builtins stay property | `promoteRaises`: **drops dotted tokens** (`errors.New`, D3, `:545`), `cleanExceptionBase`, **105-name builtin denylist** (`:87-109`), polyglot `{Class,Struct,Type,Enum,Interface}` (`:540-597`) | **0.9** | ✓ |
| **PRECEDES** | PROMOTE-CAUTIOUS, distinct internal nodes only | `promotePrecedes` parses `a→b→c`, requires distinct internal func/method nodes (`:789-841`) | **0.5** (lowest — "cautious") | ✓ |
| **DATA_FLOW** | CALLS.metadata annotation; standalone only for no-CALLS hops | `forEachDataFlowTarget` + `promoteDataFlowStandalone` (mint only when no CALLS edge, `:653`) + `promoteDataFlowAnnotations` (append `dataflow=` tag, `:675`) | candidate count 1→0.8, 2→0.6, ≤5→0.4, **>5 suppressed** (`:751`) | ✓ |
| **USES** | CALLS.metadata annotation from caller_usage | `promoteUsesAnnotations`: append `usage=` to matching CALLS edge, **never creates an edge** (`:817-905`) | (annotation) | ✓ |
| **IMPORTS** | PRESENT, single→1.0 CERTIFIED, >1→0.6 CANDIDATE, stdlib→no edge | `imports.go ResolveImports`: reuses `buildImportIndex`/`resolveModulePath`, external→no edge, FILE→SYMBOL then FILE→FILE, `verification_status='verified'` (`:54-137`); **+ incremental `ResolveImportsTx`** (`:204-296`) | 1.0 / 0.6 | ✓ |

### The actual depth change (pre-session → now)
- **BEFORE:** graph = **CALLS + CONTAINS + EXTENDS + IMPLEMENTS** only. IMPORTS *declared
  in a schema comment, never emitted*. READS/WRITES/RAISES/CO_SERIALIZES/PRECEDES
  **trapped as property strings** (readable as text for one node, **un-traversable** —
  no `A→B` hop). Promote pass existed only as a **Python copies-only reference**.
- **AFTER:** `promote.go` is the **production Go pass, wired as Pass 4f**; 5 standalone
  edge classes + 2 CALLS-metadata annotations from the existing properties; `imports.go`
  fresh-extracts IMPORTS (full + incremental). All four §2.6 sub-conditions enforced in
  code: **non-invention** (`addEdge` rejects `target_id=0`/self, `:226`), **idempotent**
  (`DELETE … promote_%` first, `:946`), **additive** (properties untouched),
  **trust-tiered** (`tierFor(conf)`, `:250`).

### Precision the prose under-states (found only by reading the code)
1. READS/WRITES confidence **lifts 0.6→0.9** for a declared `class_field` (`:486, :514`).
2. RAISES **drops dotted tokens entirely** rather than reducing to the module prefix
   (`:545`) — prevents minting a wrong edge onto a same-named project class.
3. DATA_FLOW **suppresses >5-candidate** hops (`:772-783`) — correct-or-quiet at the
   ambiguity boundary.
4. The incremental `-file` path has its **own IMPORTS Tx variant** (`:204`) — depth
   survives a single-file reindex, not just a full rebuild.

### The one honest gap
gt_gt's **per-language counts** (py CO_SERIALIZES 390, READS 658…) are claims from the
**Python reference port on copies**, not re-verified on the Go binary's output. The Go
binary's *actual* 5-language output measured this session: go 0→77 relationship edges,
py 0→23, ts 13→86, rust 0→14, js 0→5. **The mechanism matches the spec; the counts are
repo-specific and the gt_gt numbers were never re-confirmed against the Go output** —
that reconciliation lands with the live witness on a re-indexed graph.

---

## Appendix B — NAMING (the resolver ladder, gt_gt §2.3) → resolver.go

**BEFORE (pre-session ladder).** The CALLS ladder ran rungs 1 → 1.98 → name_match. A
method call with a **declared-field-type receiver** — `self.<field>.method()` where
`<field>` is type-annotated but never locally assigned (injected/inherited/annotation-
only) — **missed every receiver-typing rung** (1.75 keys on bare `self`; 1.94a keys on
`param`; 1.96 needs a local assignment) and fell to `name_match` (a guess).

**AFTER (code: `resolver.go`, commits `ec20d603` py/rust, `71d66378` go/ts).** A new
**rung 2b declared-FIELD-type receiver** resolves the receiver from the parser-written
declared field type (`BuildFieldTypeIndex`, walked up the inheritance chain) → finds the
method via the shared `lookupMethodWithInheritance` CHA primitive → emits
`type_flow / 0.9 / field_type / CERTIFIED`, and **ABSTAINS** on ambiguity/unknown/builtin
(correct-or-quiet, falls to name_match). Coverage: Python+Rust colon-annotation fields
(`ec20d603`), then Go struct fields + TS access-modifier fields (`71d66378`);
for **package-qualified Go field types (`Cache *cache.Store`)** `goStructField` →
`stripPkgQualifier` **strips the package qualifier to the tail type name, resolves if an
internal class matches, else abstains** (correct-or-quiet under-resolution). Research: XTA
(Tip & Palsberg OOPSLA'00) + CHA (Dean/Grove/Chambers ECOOP'95).

**THE CHANGE:** one specific `name_match` method-edge class → a FACT (`type_flow`).
gt_gt §2.3 was updated this session to document 2b; the genuine before is the ladder
without it. **Measured this session on 5 real repos: 0 net lift** — the residual
`name_match` were all matcher-ineligible (rust free-fns, ts builtins). So: implemented +
unit-tested (`resolver_fieldtype_test.go`), correct-or-quiet, **no witnessed lift**.

**Depth interaction:** a resolved 2b edge enters the closure and **drops the §3 residual
denominator** — and because it now passes consumers' confidence gates, it AUTOMATICALLY
enriches every confidence-gated decision read (the "automatic" trickle-down channel).

---

## Appendix C — LOCALIZATION / RANKING (gt_gt §4, §11) → graph_localizer.py

**BEFORE (gt_gt §4/§11).** Scope chains were "verified edges only, BFS" (§4 box). Symbol
naming came from `defines_anchor` witnesses only — **empty on behavior-described issues**
(the gold leaf shares no token with a named anchor). Per-symbol MaxSim cosines were
computed for the file score then **discarded** (§11.2). `n_components` did not exist.
Degree (fan-in/fan-out) mixed all edge types. **Rank deliberately excludes reach/closure**
(§4.2: `W_CLOSURE` absent, "reach over-promotes hubs" — the hard invariant).

**AFTER (code: `graph_localizer.py`, commits `9860ff7e`, `74e90817`, `d7e64a8f`).**
| change | code | gt_gt §ref |
|---|---|---|
| scope chains BFS → **union-find** over CALLS/IMPORTS **+ promoted edges**, per-edge **trust tiers** (`_scope_edge_trust`: CERTIFIED/CANDIDATE/SPECULATIVE) | `_build_scope_chains` (`:1379-1524`, `_SCOPE_EDGE_TYPES`) | §4 / §2.6 |
| **R1 symbol-semrank** — capture the previously-discarded per-symbol MaxSim cosines (`symbol_scores_out`) | `_semantic_score_by_file` (`:1681-1890`), `LocalizerResult.symbol_semrank_by_file` | §11.2 |
| **n_components** WIDE edit-set telemetry | `localize` (`:2649-2661`), `LocalizerResult.n_components` | new |
| D5 degree **type-scoped** so promoted edges don't skew the degree prior | `graph_localizer.py` (`9860ff7e`) | §4.2 |

**THE CHANGE & RANK-SAFETY:** the depth feeds **scope/completeness** (the union-find edit-
set) and the **symbol-naming substrate** (R1) — it does **NOT** enter `_rrf3` (file rank)
or `_degree_edge_filter` as reach. The §4.2 invariant ("reach over-promotes hubs") is held:
the promoted edges only ADD scope reach from a confident seed, never rank-by-centrality.
On graphs with no promote edges, the union-find reproduces today's components byte-identical.

**Generated/test demote (no `W_GEN` constant — that token is stale comment-only at `:229`):**
the generated-file demote is the inline `score -= 0.5` via `_is_generated` (`:2465-2466`) and
the test-file demote is `score -= 0.4` via `_is_test_file` (`:2467-2468`) — penalty, not hard
drop. `ppr.py` (PPR) and `recency.py` are reachable only via the legacy `brief_v5` entry
(`pretask.__init__.generate_brief`), NOT the live v1r path — dead on the live localization path,
like v22_brief.

---

## Appendix D — BRIEF (gt_gt §4) → v1r_brief.py

**BEFORE.** The scope-chain render read only `files`/`description`/`confidence` and
**dropped `edge_tiers`** — a `name_match` scope edge at conf ≥ floor rendered as a plain
graph **fact** (the laundering finding A). `_localization_header` named leaves from
`defines_anchor` only (empty on behavior issues). No `n_components` telemetry.

**AFTER (code: `v1r_brief.py`, `74e90817`, `d7e64a8f`).**
- **Finding (A) closed:** the scope render now tags **SPECULATIVE → `(unverified)`**,
  **CANDIDATE → `(CANDIDATE)`**, CERTIFIED → bare (`graph_localizer.py:1504-1521` producer
  tags the description; the contract `_scope_edge_trust` promised it). A name_match scope
  edge is never laundered as a fact again. Regression: `test_scope_chain_trust_tags`.
- **R1 leaf-naming bridge** (`_semantic_leaf_names`, `:2158-2206`): fires ONLY when
  `defines_anchor` named nothing; ranks within-file leaves by per-symbol MaxSim + per-symbol
  FTS5, **RRF-fused** (Cormack SIGIR'09), **symbol-hub-demoted**; `[]` when no signal.
- **n_components wired** to the GT_META/8-dp telemetry (`SCOPE_COMPONENTS …`, `:3361` area)
  — the dead nerve, now a consumer. Regression: `test_n_components_signal_is_consumed`.

**THE CHANGE:** the brief OUTPUT volume is unchanged; the depth makes its scope-chain trust
**honest** and its leaf-naming **relevant** (the per-symbol semantic signal that reached gold).

**Code-file mapping (live vs dead):** the LIVE v1r path is `v1r_brief.py` and it imports only
`contract_map.py` (`:32`) + `spec.py` (`:2830`, lazy). `render.py`, `contract.py`, `test_link.py`,
and `cochange.py` are LEGACY-brief code — imported only by the DEAD briefs (`render` ← `brief_v5.py`/
`v7_brief.py`; `contract` ← `v7_brief.py`; `test_link` ← `v2_ranker.py`; `cochange` ← `v7_brief.py`)
— dead on the v1r path.

---

## Appendix E — ORACLE / DELIVERY (gt_gt §15) → gt_mini_patch.py

**BEFORE (gt_gt §15).** `HorizonThresholds` stubbed (per-turn gate dead); edit-credit saw
only direct writes (`apply_patch`/`git apply` = 0 coverage); **monolithic** delivery (one
oracle exception lost the entire delivery, including always-on context — the 0/8 stub-crash);
in-container ran a divergent inline fallback; edit detection = closed verb whitelist.

**AFTER (code: `gt_mini_patch.py` + `gt_agent.py`).**
| change | code | commit |
|---|---|---|
| **hybrid bulkhead** — Lane A (context, always-on) delivers BEFORE Lane B (steers, one isolated try/except) → a steer crash can't undo context | `_lane_a_deliver` (`:4411`), Lane B (`:4655-4830`) | `35a3fb17`,`32e4e313` |
| **RC5 hybrid edit-credit** — apply_patch/git-apply/patch + staged diffs, ≥3-signal FACT-tier; dry-run flags excluded | `_classify`, `_is_patch_apply` (`:460`), `edit_coverage_ratio` | `a7a4be87`,`e6ddc06e` |
| **F3 structured-action edit detection** (retire the verb whitelist) + replay parity | `_classify_action`, `_structured_edit`; `gt_oracle_sense.py` | `d8cfd3d1`,`74e90817` |
| **F2+F5 in-container Product parity** — ship real `runtime`/`delivery`/`pretask` packages, fail-closed import-coverage | `gt_agent._PRODUCT_PACKAGE_MODULES` (`:207`) | `95dff1d9` |

**THE CHANGE:** delivery is crash-survivable (context always reaches the agent), edit-aware
across every patch channel + harness, and in-container == Product. ~1150 net lines.

---

## Appendix F — VERIFICATION CONTROLLER (NEW; extends gt_gt §15.4 horizon) → runtime/edit_risk.py

**BEFORE (gt_gt §15.4).** Verification timing was **budget-fraction-anchored**
(`verify_horizon_band`: bands on `action_count/step_limit`); agent-visible wording said
"steps remain", "submit unverified work" (SWE-bench-shaped). No structural-risk signal.

**AFTER (code: `runtime/edit_risk.py` + the producer wire, commits `437790dc`,`be774ddb`).**
- `structural_edit_risk` = verification risk timed by **blast radius** — verified
  **dependency** fan-in (CALLS **+ READS + WRITES + DATA_FLOW**, the deep graph) of
  edited-but-untested symbols, scored **relative to the repo's own fan-in distribution**
  (dynamic, repo-relative, not a magic caller threshold); correct-or-quiet; name_match
  never counts. **The white space no 2026 paper occupies** — they target MODEL uncertainty
  (RisCoSet/PreFlect/Anytime-Verified-Agents 2026); GT targets CODE structural risk
  (Mirror 2026: external deterministic control > self-assessment, 76% vs ~0%).
  **Caveat (G07):** READS/WRITES blast radius is realized in full only for CLASS targets; for
  method/function targets it currently degrades to CALLS-only fan-in (field-level READS/WRITES
  targets are not yet in the graph) — `_structural_risk_note` honesty note, `gt_mini_patch.py:3968-3979`.
- Wired into the verify producer behind `GT_VERIFY_STRUCTURAL_RISK` (**default OFF** →
  byte-identical to today): a high-blast-radius untested edit earns a **budget-free**
  advisory that NAMES the risk (`_structural_risk_note`, `gt_mini_patch.py`).
- Wording de-benchmarked: `submit/submission` → `finalize` (`verification_horizon.py`).

**THE CHANGE & DEPTH:** verification is timed by *what was edited*, not a step clock; and
the risk reads the **deep** graph (READS/WRITES dependents, not calls-only) — depth into the
decision substrate, output unchanged. Tests: 8 edit_risk + 5 wire + 5 render. Flag-gated;
the timing re-anchor (R driving the fire rule) + the live witness are still owed.

---

## Appendix G — LSP RESIDUAL + INFRA (gt_gt §3, §7)

**BEFORE.** `resolve.py:190` counted the LSP residual with `AND tgt.label='Method'` — a
**Python-only** label → undercounted residual on go/rust/ts/js (language-blind diagnostic).
Agent-container OOM was unclassified → mislabeled as a generic failure.

**AFTER.**
- `resolve.py` (`9db1fe44`): dropped the Python-only clause → **language-agnostic** residual
  count.
- `deepswe_full.yml` + `deepswe_outcome.py` (`a5747a9c`): **`GT_AGENT_OOM` classification**
  (`OOMKilled`/exit 137) + grep-hardening. **PARTIAL** — the actual memory **cap injection**
  is still inert (CI pip-installs pier), so the local compose `mem_limit` isn't used.

---

## Appendix H — the trickle-down, stated once (why this is ONE change, not eight)

Every appendix above is the SAME move: **graph.db got deeper (B naming + A depth), and that
depth flows into each decision substrate so the OUTPUT is more RELEVANT, not larger.** Two
channels: **(1) automatic** — any read that confidence-gates (≥0.5) now sees the resolved
naming for free (the 2b edges + promoted edges pass the gate); **(2) manual** — reads that
filtered `type='CALLS'` are extended to the promoted edges WHERE it improves a
SCOPE/RELEVANCE/RISK decision (edit-risk done; localizer scope done), **never** where it
would feed reach into RANK (the §4.2 invariant — the one line that would re-break hubs). The
running per-layer workflow (`wx2eywcer`) is auditing which decision reads are still shallow
so the remaining manual enrichments land rank-safe.


---

## Appendix I -- LIPI audit (wx2eywcer) findings + fixes

The whole-architecture per-layer LIPI ran (58 agents). Confirmed findings + status:

| severity | finding | status |
|---|---|---|
| **HIGH** | **Depth LEAKED INTO RANK** -- untyped edge JOINs in `v1r_brief.py` (`_top_functions:267`, `_top_function_names:333/346`, `_hub_degree_fn:1943`) counted promoted READS/WRITES/PRECEDES/DATA_FLOW into function ranking + the hub p80 (the section 4.2 "reach over-promotes hubs" violation, activated by the depth landing) | **FIXED** -- added `type='CALLS'`; depth feeds SCOPE only, never RANK |
| **MEDIUM** | `edit_risk` counted a 2-candidate `name_match` (conf 0.6 >= floor) as a dependent -- contradicting its own docstring | **FIXED** -- excluded by `resolution_method`; regression added |
| HIGH | `post_edit.py:157-163` docstring claims it admits `name_match cc<=1` (the old os.walk launder); the SQL correctly excludes name_match -- a "fix code to match docstring" edit would re-introduce the launder | **FIXED** -- docstring now matches SQL; SQL hardened `!= 'name_match'` -> `NOT LIKE 'name_match%'` (variants too); locked by `test_categorical_filter_no_namematch_launder` |
| MEDIUM | `_issue_relevant_neighbors` / 1-hop expansion add candidates to the "Calls:" line via DATA_FLOW/CO_SERIALIZES (untyped UNION) | **FIXED** -- all 4 JOINs now `type='CALLS'` (matches `_static_callees` sibling); locked by `test_neighbors_calls_only` |
| MEDIUM | `contract_map._read_props` reads properties without a confidence gate | **FIXED** -- `AND COALESCE(confidence,1.0) >= 0.5` (legacy no-column -> permissive); locked by `test_contract_props_confidence_gate` |
| **MEDIUM** | **Layer 4b auditability gap** -- no per-hook FIRE counter; the ledger records only DELIVERED/SUPPRESSED, and a fired-but-quiet hook is skipped before any record. "How many times did each hook fire?" is NOT answerable from disk -- only how many DELIVERED | **FIXED** -- `_record_hook_fire` counts every Lane-A fire (incl. fired-but-quiet) to `GT_HOOK_FIRE_COUNTS` JSON; locked by `test_hook_fire_counter` |
| LOW | scope-chain SELECT omits `trust_tier`; `STDLIB_MODULES` duplicated in two modules (drift risk); L4b reads no promoted edges (missed enrichment) | **FIXED (2/3)** -- STDLIB drift LOCKED (`test_stdlib_modules_single_source`); scope-chain now pulls `trust_tier` + hard-excludes SUPPRESSED (legacy-schema fallback; `test_suppressed_edge_is_never_a_scope_member`). Only L4b promoted-edge enrichment (a FEATURE) remains |

**The headline:** the LIPI caught the precise thing depth threatens -- promoted edges silently
entering RANK through latent untyped JOINs. Fixed. The remaining OWED items are tracked above;
none is a live laundering (all correct-or-quiet or latent).

### Hardening-loop close-out (zero-regression, proven)
The full OWED bug backlog (Units 1-5b) is FIXED + regression-locked, each spec'd + LIPI'd
+ committed (`8a882986 → 87629b7b → d3eb6944 → f4249535 → 3205afbc → 7de4004c`). Only the
L4b promoted-edge *enrichment* (a FEATURE) and F4 cap (INFRA/CI) remain non-bugs.

**Comprehensive regression: 1979 passed, +30 new regression tests, 16 failed.** The 16
failures are **PROVEN PRE-EXISTING** — identical 16-failed/35-passed at the pre-session
baseline `32e4e313`. **The session's entire body of work introduced ZERO regressions.**
Triaged by actual error mode:
- **4 real (FIXED, `74b3711b`):** `test_verify_labeling` asserted the OLD test-name LEAK
  (`Run: pytest <exact test>`); `_get_targeted_verification_suggestion` was deliberately
  disabled (`return ""`, "run12 leaked test_plot_hdi") for legitimacy. Fixed the stale
  docstring + converted the 4 tests to assert `""` — so re-enabling the leak fails CI.
- **~7 environmental (Windows test-harness):** `presubmit_verify` (4) `PermissionError
  [WinError 32]` — temp `.db` not unlinkable (sqlite conn held open); `clip_balanced` (4)
  `NotADirectoryError [WinError 267]` on temp paths. Pass on Linux/CI; not product bugs.
- **2 real (FIXED):** `graph_localizer_l1` `matmul 384 != 768` in `anchor_select.py` was a
  CRASH where it should abstain — a cache/query embedding-dim mismatch (e5/384 vs gte/768)
  blew up the WHOLE localization. Added a dim guard: ABSTAIN (score 0.0, fall to lexical)
  on mismatch instead of crashing. The 2 crash tests now pass (`tests/unit/test_graph_localizer_l1.py`).
- **~1 environmental (embedder):** the remaining `graph_localizer_l1` failure is the
  embedder-config-dependent ranking assertion (importer.py-top — needs semantic ON via the
  container ONNX path per BRIEFING; the local cache/model are dim-mismatched).

**Net (after the full resolution drive): 15 of 16 RESOLVED.**
- 4 legitimacy (`verify_labeling`) + 2 robustness (`anchor_select` matmul dim-guard)
- 4 `presubmit_verify`: a REAL sqlite-connection leak (`conn.close()` lived after the query
  in the `try`, so a query error returned via `except` without closing -> Windows file lock)
  + rewrote the stale tests (asserted the removed test-name leak) to lock test-blindness
- 5 `clip_balanced`: 4 Windows `TemporaryDirectory` teardown errors (`ignore_cleanup_errors`)
  + 1 STALE MOCK (`_FakeEgo` lacked the `.nodes`/`.edges` interface `post_view` now filters)
- **1 remaining (genuinely env-gated):** `test_localize_surfaces_importer_as_top_via_witness`
  — importer.py carries the witness but ranks #2; with semantic ON (container ONNX per
  BRIEFING) it boosts to #1, but the local e5/384-vs-gte/768 mismatch makes semantic abstain.
  NOT fixable as local code without overfitting the localizer ranking (BRIEFING-protected).
  Needs the container embedder env. **None caused by depth/rank/the hardening loop.**

---

## §9 — WHOLE-GT LIPI AGAINST THE ARCHITECTURE (2026-06-14)

A 4-surface LIPI (4-avenue: Logic / Implementation / Integration / Plumbing) of the **whole
GT** read against the architecture invariants (I1–I8, §349 legitimacy, the D5 degree-scoping
rule, correct-or-quiet), chronological reads (never grep-counts), citing file:line. Each
finding names the invariant it serves. Result: **1 real architecture violation found + fixed,
3 legitimacy/I3 hardening fixes, 5 LOW/doc items.** No HIGH defect left open.

### Surface 1 — Indexer / resolver (naming + depth production)
- **CLEAN** I3 stdlib-shadow demote (`resolver.go`: `qualifiedUnresolved` gates `verified_unique`;
  `os.walk`→`account.walk` routes to SPECULATIVE `name_match_qualified_unresolved`, never CERTIFIED),
  CHA/XTA receiver-type matcher (`ec20d603`, language-agnostic, 5 correct-or-quiet guards),
  builtin-method drop (join/get/append/…), I7 substrate (additive/idempotent/non-inventing/
  trust-tiered — idempotency is TESTED in `promote_test.go`, not just asserted).
- **MED → INVARIANT WORDING (no code change):** I5 says depth "never mutates existing edges,"
  but USES/DATA_FLOW annotations `UPDATE edges SET metadata` on existing CALLS rows (by design,
  gt_gt §2.6). The structural map (source/target/type/confidence/trust_tier) IS immutable; only
  `metadata` is appended. **I5 restated:** *depth never mutates edge IDENTITY/confidence/tier —
  metadata-append is the one permitted, idempotent mutation.*
- **LOW:** incremental `-file` re-promote is whole-graph, not changed-subgraph (perf only;
  converges via idempotent delete-rebuild; acknowledged in `main.go` comments).

### Surface 2 — Brief / localizer (the ranking surface, I2-critical)  → **REAL BUG, FIXED**
- **I2-A (FIXED, commit `93cff789`):** the inline hub-demotion block (`v1r_brief.py`) counted
  **untyped** in-degree (`JOIN edges e ON e.target_id=n.id`, no `e.type` filter) and used
  it to **reorder `top_records`** (RANK). Its sibling `_hub_degree_fn` was CALLS-scoped in Unit 2,
  but this parallel path was missed (classic "two paths, one fixed" Integration defect). Promoted
  depth edges would inflate the hub p80 and shift the delivered file order — the exact I2 leak
  ("as depth increases, treat this very seriously"; "reach over-promotes hubs, the architecture
  subordinates it on purpose"). The fix IS present: the hub-demote COUNT queries now carry
  `AND e.type='CALLS'` — the 1-hop neighbor UNION at `:2994`/`:2999` and the hub p80 / per-path
  fan-in COUNT queries at `:3063`/`:3073`. Degrade-safe (byte-identical today; diverges only once
  promote ships).
- **CLEAN (with evidence):** every OTHER edges-JOIN feeding rank/degree/reach is CALLS- or
  CALLS/IMPORTS-typed or `_degree_edge_filter`-scoped (witness BFS `:2275`, path-decay `:504`,
  `_file_degrees`, `_hub_degree_fn`, `_symbol_fanin_fn`, 1-hop expansion, `_top_functions`).
  Promoted edges are quarantined to scope-chains only (`_SCOPE_EDGE_TYPES`/`_build_scope_chains`),
  never `_rrf3`. I1 correct-or-quiet, `_read_props` ≥0.5 gate, trust_tier SUPPRESSED exclusion,
  anchor_select dim-guard, dynamic+hybrid p80 tiers — all clean.

### Surface 3 — Hooks / delivery / legitimacy  → **3 FIXED (commit `74ac3256`)**
- **CONFIRMED:** no agent-facing path can emit a test name or `pytest <test>` (verified at 4
  independent layers). `_categorical_edge_filter_clause` uses `NOT LIKE 'name_match%'` (I3, locked).
- **F4 (FIXED) — §349:** `graph/ego.py` still LOADED the `assertions` table (test names + grader
  expected) into a field the render path no longer consumes → latent re-leak. Removed the read;
  the grader table is now genuinely untouched.
- **I3 consistency (FIXED):** `_hierarchy_edge_filter_clause` used exact `!= 'name_match'` (missed
  `name_match_qualified_unresolved`) → aligned to `NOT LIKE 'name_match%'`.
- **F3 (FIXED):** L6 docstring still described the removed assertions-table behavior → rewrote to
  match the test-blind `properties`-based body.
- **LOW (documented, not fixed):** `ego.py`/`post_view.py` sqlite conns not in `try/finally` (same
  class L6 fixed; only bites on Windows + a query error — the eval container is Linux/GC-finalizes;
  wrapping the 160-line `ego_graph` body is a large non-reversible diff for a LOW Windows-only item).

### Surface 4 — Oracle / mini-swe / verification controller
- **CLEAN on every load-bearing invariant:** `edit_risk` excludes name_match (`NOT LIKE
  'name_match%'` in BOTH the per-symbol blast-radius and the repo fan-in reference — a 2-candidate
  name_match at conf 0.6 ≥ floor is NOT counted); the oracle is correct-or-quiet (every steer
  producer returns `None`/`""` without a real graph/behavioral fact — the gate only selects among
  real producer output, cannot manufacture a steer); LEGITIMACY (internal `_test_run_command`
  computes `pytest …` but NEITHER agent-visible renderer interpolates it — only `bool(covering)`);
  I4 (zero LLM/http in the decision path); I8 (repo-relative saturating risk, median+MAD floors).
  Two-lane bulkhead correct (a Lane-B crash loses only the steer, never Lane-A's data plane).
- **LOW:** (a) `_record_hook_fire` counts Lane A only (Lane B winners record via the ledger, not
  the fire-count file) — telemetry asymmetry, not a correctness bug. (b) `_STRUCTURAL_RISK_ON` is
  default-OFF (needs `GT_VERIFY_STRUCTURAL_RISK`) → **the edit_risk steer is INERT unless the
  witness harness exports that env var. Verify it does, or the structural verification axis is dark
  at the witness.**

### Net
Whole-GT LIPI against the architecture: **the only depth-into-rank leak (I2-A) is closed**, the
legitimacy surface is hardened (assertions table truly off-limits; L6 doc no longer invites a
re-leak), the I3 name_match-never-a-fact gate is consistent across caller + hierarchy paths. The
substrate/oracle/verification surfaces were clean on every load-bearing invariant. Remaining items
are LOW (Windows-only conn hygiene, telemetry asymmetry) or witness-env (`GT_VERIFY_STRUCTURAL_RISK`
must be exported) — none blocks correctness; all named against the invariant they touch.

---

## §10 — DESIRED-vs-CODE conformance audit + parallel fix wave (2026-06-15)

**Method (the user's mandate):** desired = gt_new/gt_gt + invariants; current = the hbali_stack
code; every gap = a bug; found by reading desired-vs-current ONLY (no task-testing = no benchmaxxing).
A 6-agent parallel LIPI (run `wm1frwrk8`) audited all 5 surfaces, weighted to the **Plumbing avenue**
the prior code-logic LIPIs (§9, Appendix I) were blind to. Full catalog: `docs/GT_GAP_CATALOG_LIPI.md`.

**The dominant finding (measured live, then root-caused):** gt_new App E's *"context always reaches
the agent"* was **FALSE in runtime**. Across a 391-turn run with 34 file-ops, the per-turn
`gt-evidence`/`gt-contract`/`gt-scope`/`gt-cochange` producers delivered **0×** — because **graph.db
never reached the pier container** (`gt_agent._BUILD_GRAPH_DB` 404s on a release with no asset + no Go
to build). Only the graph-FREE governors fired. The code was correct; the data didn't flow. **FIXED
`88e97978`** — inject the host graph.db into the container (gzip+b64, under BuildKit's 16MB cap).

**18 gaps catalogued** (HIGH=6, MED=6, LOW=6; **11 plumbing**, 5 REQUIRES-I2-LIPI). Worst-first:
G01 depth→RANK leak in `graph_reach._build_file_graph` (untyped reach SELECT — the I2 violation §9
claimed closed, on a parallel code site); G02 incremental phantom-`name_match` duplicate of promoted
edges; G03/G04 host GT env never reaches the container (pier strips `export`; trial path has no
`--ae`) → edit-risk + every env-gated producer dark; G05 my own graph.db fix killed L6 reindex (no
`/tmp/gt-index`); G06 edit-risk scores the static obligation set not edited-but-untested.

**Resolution: parallel worktree-isolated fix wave** (run `w30fe8rby`), 6 file-disjoint clusters,
each: surgical fix + regression unit test + 4-avenue LIPI, rank-safety enforced for the 5 I2 gaps
(depth must stay byte-identical out of reach/RANK). **No task trial anywhere — every fix is a
structural code change verified by unit test + LIPI, not a flip.**

**RESULT: 18/18 gaps addressed — 17 fully resolved + verified, G05 fail-loud-mitigated.** The wave's worktrees had a mixed base
(4 on gt-trial, 2 on stale origin/master); I salvaged the 4 correct-base clusters by file, redid
the env cluster on gt-trial, and the unreliable gt_agent cluster's fixes are owed.
- **`f687ec09` — 12 core gaps (C1/C2/C5/C6):** G01 depth→RANK leak closed (`graph_reach`
  reuses the D5 predicate, single-source with `graph_localizer`); G16/G18 closure+schema guards;
  G02/G09 Go incremental (phantom-name_match snapshot filter + `-file` inheritanceMap); G06/G07/
  G08/G10/G15 oracle/verify (edited-but-untested scoring, owning-class READS/WRITES in the RISK
  substrate, reset re-delivery, import isolation, Lane-B fire counter); G13/G17 brief (CALLS-only
  test surface + contract READS/WRITES blast facts). **39 py tests + Go store/closure green;
  the 4 I2 fixes LIPI-confirmed + test-pinned rank-safe.**
- **`48238da4` — G03/G04:** the `--ae` single-source block — pier drops host `export`, so GT
  runtime env (verify-risk, oracle route, telemetry sinks) now forwards into the container.
- **`88e97978` (earlier) — the dominant Plumbing gap:** host graph.db injected into the container.
- **`118b672b` — the last 4 (G05/G11/G12/G14), config/infra/diagnostic:** G05 L6 now FAILS LOUD
  (one-time `[GT_META] L6_NO_REINDEX_BINARY`) instead of silently freezing per-turn freshness —
  the ~49MB binary still exceeds the 16MB bake cap, so the *full* reindex capability via a runtime
  binary-mount is the **one genuine deferral**, but the gap is now visible not silent; G11 durable
  `/tmp/gt_out -> /gt_out` bind-mount + copy-out (telemetry survives the container); G12 the trial
  caches the configured gte-modernbert-base/768 (was e5/384); G14 self-test probes the real graph
  path. Verified by py_compile / bash -n / yaml.safe_load; none touches core code or the I2 rank.

**FINAL TALLY: 18/18 gaps addressed.** 17 fully resolved + verified; G05 mitigated fail-loud (the
runtime binary-mount for live per-turn reindex is the single remaining infra task, now diagnosable).
The only thing this round deliberately did NOT do is the live agent witness (gt-evidence 0→>0
consumption proof) — by the no-task-testing constraint. Every fix is a structural code change
verified by unit test + LIPI + compile/syntax gate, never a benchmark flip.

> **CORRECTION (§10.1, 2026-06-15) — the "17 fully resolved + verified" claim above was wrong on
> two counts; an adversarial MAX-LIPI re-review caught them.** The fix-wave's own self-verification
> was not adversarial enough. A second pass (`docs/GT_GAPFIX_MAX_LIPI.md`, 4-avenue per surface,
> every claim re-read against the working tree) found **2 HIGH defects** that the §10 tally had
> counted as resolved. Both are now genuinely closed and test-pinned; the lesson is recorded as a
> trap (self-verified ≠ adversarially-verified). The corrections:

### §10.1 — adversarial re-review corrections (this session)

**HIGH #1 — I2 rank leak via `W_PROX` (the G01 fix stopped one term short).** G01 typed the reach
term (`graph_reach._build_file_graph`) but its sibling `anchor_proximity.compute_anchor_proximity`
— which feeds `anchor_prox` → the `W_PROX` rank term in `v7_4_brief._total_score` (0.05, boosted to
0.12 in the function-level regime) — gated only on `confidence >= 0.7` with **no** type/provenance
exclusion. Once the promotion pass ships, the five cross-file promoted DEPTH classes (READS/WRITES/
RAISES/CO_SERIALIZES/DATA_FLOW, minted at conf 1.0) would pass the gate, inflate the anchor neighbor
set, and **shift rank** — the exact I2 violation G01 claimed to close, on a parallel code site the
catalog never enumerated. **FIXED:** `anchor_proximity` now applies the SAME D5 single-source
predicate (`graph_localizer._degree_edge_filter`). **PINNED:** `tests/test_anchor_proximity_i2.py`
asserts byte-identical proximity with vs without promoted edges (mutation-checked: 2 pass on the fix,
2 FAIL when the predicate is removed). The I2 suite had covered reach/expand but **not** proximity —
that blind spot is why it slipped; it is now covered.

**HIGH #2 — G09 (`-file` inheritanceMap) was DISABLED in committed HEAD, AND a second independent
incremental-path bug sat behind it.** `main.go` carried a `// TEMP-RED-CHECK` that left
`_ = allFiles; _ = allLangs` in place of the `buildInheritanceMap` + `SetInheritanceMap` wiring — so
the CHA rungs (gt_gt §2.3: 1.75/1.94/1.94a/2b) were dead on EVERY `-file` reindex, and the §10
"resolved+verified" claim was **false against the binary**. **FIXED:** reconstructed
`[]walker.SourceFile` from the parallel `allFiles`/`allLangs` rows, built the whole-graph inheritance
map, and `SetInheritanceMap` before `Resolve`. **But the fable-mode mutation check then refused to go
green even with the wiring restored** — exposing a SECOND, independent bug the map alone could not
fix: the incremental path zeroes `pr.Nodes[i].ParentID` for the node insert (main.go:921), fixes up
the DB row, but never restored `ParentID` on the **in-memory copy** appended to `filteredNodes` →
`BuildNodeMeta` → `callerMeta.ParentID == 0` → the self.method()/inherited rungs cannot identify
self's class → inherited calls demote to `name_match` on every reindex regardless of the map.
**FIXED:** restore the parent DB id on the in-memory copy. **PINNED:** the e2e test
(`inheritance_incremental_test.go`) was tautological (single-`save` fixture + byte-identical child →
SHA-256 short-circuit → the resolution path never ran; it passed even on disabled code). Rebuilt to
BITE: a competing `Other.save` makes the name ambiguous (only the hierarchy disambiguates) AND the
child is modified before reindex (so the short-circuit doesn't fire). It now goes RED on either bug
alone and GREEN only with both fixes (probe-confirmed: disabled→`name_match`, fixed→`inherited`).

**MEDIUM/LOW/NIT (the same review, 11 more findings): 10 resolved, 1 deferred-optional.** chmod 777 +
loud copy-out on the codespace telemetry mount (§2.3); honest downgrade of the `gt_ae_block.sh`
"single source of truth" claim — it is wired on the codespace path only, trial/full.yml sourcing is
OWED (§2.4); graph.db injection decode made fail-closed via temp + non-empty assert + atomic mv so
`/tmp/graph.db` is never torn (§2.5); corrected the false `.pyi`-matches-gt-index comment (gt-index
indexes only `.py`; `.pyi` is edit-detection parity only, deliberately graph-quiet) (§2.6); added the
PRECEDES cc>1 SPECULATIVE-demotion test (§2.7); softened the G05 "reindex ENABLED" echo (binary mount
≠ enabled without an in-container graph) (§2.8); made the G17 verified-reader count field-EXACT
(`AND e.metadata = ?`) + a two-field-class test (§2.9); dropped the unused `_PROMOTED_EDGE_TYPES`
import (§2.10); removed a redundant `global` (§2.12); fixed the self-contradicting "Default-OFF"
comment (§2.13). **DEFERRED:** §2.11 (resolveByName returns the global candidate count for a same-file
match) — explicitly optional in the review, and it changes cc semantics shared with DATA_FLOW; not
worth the shared-behavior risk for a display nuance. **Gates:** full `gt-index` build exit 0; resolver
+ cmd/gt-index Go suites green (the lone `TestRoutePatternMatching/comment` failure is PRE-EXISTING,
fails on base, orthogonal to this diff); 31 py tests across contract/brief/I2 green.

---

## §11 — 2026-06-15 functional-fix campaign (commit `d5b1e59a`) + rust-LSP fix

> **Premise (the review that drove it, `.claude/reports/GT_FUNCTIONAL_CODE_REVIEW_20260615T1900Z.md`):**
> architecture parity (docs = code) is necessary, not sufficient. The review hunted the archetype the
> rust-LSP bugs proved real — **code that exists and matches the docs but produces wrong / missing /
> silent output.** 10 reviewers, LIPI 4-avenue, worst-first, ~45 findings. The campaign fixed the
> worst-first set, each pinned by a mutation-checked test. Go build+vet exit 0; 1045 integration tests
> pass, 0 regressions (4 pre-existing contract-pillar failures unrelated, stash-proven).

### (a) The DOMINANT class — name_match-as-fact gate, closed at 4 LIVE surfaces

`name_match` with ≤2 candidates scores **confidence 0.6** (`resolver.go:computeConfidence`), so it
cleared every gate that filtered on `confidence >= 0.5` WITHOUT a `resolution_method` check —
laundering a NAME GUESS as a deterministic FACT. The fix is ONE canonical fact-set
**`DETERMINISTIC_RESOLUTION_METHODS`** (`curation_map.py:83`:
same_file/import/import_type/type_flow/verified_unique/impl_method/inherited/unique_method/return_type/
lsp/lsp_verified), gated categorically at each surface (fail-closed: name_match is NEVER a fact).

| surface | site | what changed | live? |
|---|---|---|---|
| **brief** | `v1r_brief._edge_conf_clause` (`:121`) + medium-scope `_distinct_files` (`:3545-3581`) | on a no-`confidence`-column DB the clause was `""` (NO gate) → the `Calls:` line + neighbor-expansion + "Related files to inspect" rendered every name_match target as a fact; now emits a `resolution_method`-categorical clause when confidence is absent | **LIVE** (the v1r eval brief) |
| **consensus / L5 scope** | `gt_mini_patch._query_scope` (`:2060`) | the delivered `<gt-scope>` 1-hop neighbour set gated only `confidence>=0.5`; name_match (0.6) cleared and shipped as "graph-connected / in scope." Now `resolution_method ∈ DETERMINISTIC` (the inline "drops SPECULATIVE" comment is now TRUE) | **LIVE** (DeepSWE) |
| **localizer** | `hub_penalty` (`:23,47`), `anchor_proximity` (`:30,67`), `graph_reach` (`:77,84`) | structural-degree floors dropped `confidence >= 0.7` → **0.5 name_match floor** so a 0.6 name_match hub is PENALIZED (was escaping penalty) AND name_match-heavy graphs (70-80%) don't blank out, while sub-floor guesses are still excluded | **LIVE** (ranking) |
| **L4 MCP tools** | `index/graph.find_callers` `is_fact` tag (`graph.py:347-357`) ← `mcp/tools.py` | `find_callers`/`handle_trace`/`handle_impact` gated only `confidence>=0.5`, no method gate; now tri-state `is_fact` (method ∈ DETERMINISTIC → FACT; name_match → unverified; None/Python-refs-schema → FACT) | **secondary** (DEAD on DeepSWE; LIVE for Cursor/Claude-Code/Codex MCP clients) |

### (b) Per-element fixes (one line each, by component)

**graph.db (gt-index):**
- **PRECEDES receiver-type gate** — `extractCallOrdering` discarded the receiver type, so `promotePrecedes` resolved each bare method name via a type-blind global `resolveByName` first-match → `PRECEDES open→write` between unrelated funcs. Now requires a resolved receiver type (`promote.go`).
- **Incremental restore qualified_name + name-only demote** (`incremental.go:173-220`) — a single-candidate re-match preserved the original `verified_unique`/`type_flow` method+confidence VERBATIM though re-matched by bare NAME, so a surviving CERTIFIED edge rebound to an unrelated same-named node after rename-replace. Now re-proves TARGET IDENTITY against the snapshotted `TargetQualifiedName`; a bare-name re-match caps at CANDIDATE.
- **Closure best-confidence dedup** (`closure.go:130-165`) — `bestEdgeConf` was computed then never read by the BFS (which used first-scanned `adj.conf`), under-stating `min_confidence` → strong reachability paths dropped below the 0.5 reader floor. The adjacency is now built FROM `bestEdgeConf`.
- **Go factory-return IMPLEMENTS deleted** (`relationships.go:39,437`, #P1-4) — a func returning an interface does NOT implement it; `goReturnInterfaceRe` fabricated meaningless IMPLEMENTS + duplicated CHA wrongly. Regex path removed.
- **JSX COMPOSES line-range owner** (`relationships.go:251-259`, `findEnclosingFunc`) — owner was the first map-iterated func (no line-range) → random component owner across runs; now the func whose `[Start,End]` ENCLOSES the JSX line (ambiguous → return 0, correct-or-quiet).
- **Rust generic impl regex** (`relationships.go:48`, `rustImplForRe`) — `strings.Fields` positional parse mangled `impl<T> Trait for S` / `impl fmt::Display for Foo`; a regex skipping the optional `<...>` impl-generic block now captures trait + implementing type.
- **`super.` strip + cc==2 → 0.5** — rung-1.96 stripped `self.`/`this.` but not `super.`, mis-scoping `super.field.method()`; now stripped. name_match cc==2 dropped 0.6→**0.5** (#P2-9) so the ambiguity gradient is monotone (1→0.6, 2→0.5, 3-5→0.4, >5→0.2). (gt_gt:116 re-synced.)

**embedder (`memory/enrich/embed.py`):** issue-query token window **128→1024** (gte; e5=512) — the hard 128-cap truncated the issue QUERY's signal-bearing tail (named file/symbols/frames); decoupled into `_PASSAGE_TOKEN_WINDOW=128` (bounds activation memory) vs `_query_token_window` (model-appropriate). e5 query/passage role made explicit + folded into the cache key so a mis-prefixed vector can't poison the shared LRU.

**LSP (`resolve.py` + `lsp/`):** per-server readiness budget table now has **`jdtls:180` and `gopls:60`** (was rust-analyzer-only at 180; jdtls/gopls fell to the 20s default and quit before workspace import → 0 conversions = a green PASS); **`degraded` flag + `LSP_DEGRADED_FAIL`** fail-closed (warm + residual>0 + 0 conversions under `GT_REQUIRE_LSP`); `lsp_warm` no longer gated on `probe_latency_ms>0` (a coarse-clock instant warm rounded to 0ms → false NOT-warm) — uses `perf_counter`; **call-site column finder is whole-word call-shaped, not `str.find(name)`** (the substring finder queried the WRONG call site for the method-call majority `get`/`join`/`append`).

**localization:** path-key normalization at every candidate-set ingress (`_norm_path`) — a file appearing as `a/b.py` and `a\b.py`/`./a/b.py` split its signals so a competitor with all signals on one key outranked both halves; RRF tie-break now uses a relevance-bearing secondary key before `file_path` (was alphabetical when the embedder is off); generated-demote uses anchored markers (was bare substring `"/generated/"` → a handwritten file under `generated/` ate −0.5); dispersion MAD computed over the nonzero/covered set, not zero-padded.

**brief:** HIGH-localization `_edit_target_guard` pins the EXACT named file (was `... OR file_path LIKE '%/'+rel ... ORDER BY start_line LIMIT 1` → rendered a guard/return line from a DIFFERENT file's same-named func at the primacy position); `_top_functions` unions anchor symbols into the pool BEFORE the ref-count cap (a freshly-added 0-caller gold function fell past the cap → brief shipped the WRONG function's contract).

**consensus / L5:** "You edited X of N in-scope files" — N is now the issue-anchored connected component of the edits, not the global union of every viewed file's neighborhood (which inflated with exploration → false "you missed file X"); completeness anchor filter case-fixed (focus tokens lowercased to match `_norm_rel` members → CamelCase anchors now match); L5 emits a **`TEST_RESULT` event + passes `test_count` + re-arms on phase-drop** so verify/failure/no-test steers reach the agent on a test turn (they were phase-filtered out as wrong_phase, then the fire-once latch was burned → permanently silenced).

**wiring:** `gt_ae_block.sh` single-sourced into `deepswe_full.yml` so `GT_VERIFY_STRUCTURAL_RISK` (the edit-risk lever) + the deep-telemetry sinks are no longer DARK on the 113-task leaderboard (were forwarded only on the codespace witness path); `gate_lsp` fail-closed on an absent cert (was synthesizing `LSP_ACTIVE_VALID` from `lsp_warm=1`+`residual>0` without the closure/timing proof the cert path enforces).

**Liveness:** **7/8 surfaces are LIVE on DeepSWE.** L4 MCP tools + OH `post_view` are **secondary** (live for the MCP/OpenHands clients — Cursor/Claude-Code/Codex — DEAD on the DeepSWE pier path).

### (c) The rust-LSP fix (commits `fa728e46` + `fa6b4343`) → gt_gt §3 (LSP enrichment) / §17.3

rust-analyzer was dark on the eval path through three plumbing defects, all fixed this session:
1. **`fa728e46` — toolchain on PATH.** rust-analyzer spawns `cargo metadata` to build its crate graph; the dep-extract pulled `CARGO_HOME`/`RUSTUP_HOME` but never put the toolchain `bin/` on PATH → cargo/rustc unfindable → `cargo metadata` exit 127 → no project model → all go-to-def probes empty → `project_ready=False` → 0 lsp edges (det_pct stuck 65.7%). Fix: prepend the extracted toolchain `bin/` before the resolve pass (both `codespace_deepswe_run.sh` + `deepswe_full.yml`).
2. **`fa6b4343` FIX-1 — standalone RA over the rustup shim.** `CARGO_HOME/bin` is a dir of rustup SHIMS (`rust-analyzer -> rustup`); the 1.92.0 toolchain has no RA component so the shim exits 1 ("Unknown binary 'rust-analyzer'") and SHADOWED the working standalone `/usr/local/bin/rust-analyzer`. Fix: drop the shim dir from PATH (real cargo/rustc still on the toolchain bin); RA falls through to the standalone.
3. **`fa6b4343` FIX-2 — advertise `window.workDoneProgress`.** The warm probe gave up before a cold RA finished indexing; advertising the capability lets the probe wait out the index.

**Witnessed live on `fd-deterministic-multi-key-sorting`** (the decisive proof, not inferred): rust
**lsp 0→186** converted edges, **det_pct 65.7%→90.21%**, verdict **`LSP_ACTIVE_VALID`**. This is the
IDENTICAL `resolve.py` LSP path that already enriches Go CALL edges — proving the GT LSP code was
correct and the gap was purely the un-wired rust toolchain (rust isn't system-installed; only the
extracted toolchain). Maps to **gt_gt §3** (LSP enrichment) + **§17.3** (Go/Rust LSP status), both
re-synced. Generalized — PATH/transport plumbing, no per-task logic, no benchmaxxing.

**Status:** all of §11 is **TESTED** (mutation-checked unit/integration red→green) except the rust-LSP
fix, which is **PROVEN** (live `fd` witness: lsp 0→186, det_pct +24.5pp, `LSP_ACTIVE_VALID`). The
broader functional-fix campaign's live agent witness (consumption proof) remains the open Task #6 gate.
