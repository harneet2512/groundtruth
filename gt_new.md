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

## Appendix A — DEPTH before/after (gt_gt §2.6 spec vs the current code, read line by line)

**Framing.** gt_gt §2.6 was updated this session to record the landing (it cites
`b5ceaf5d`, "Pass 4f LANDED"), so it is now a spec **+** landing-record, not a clean
"before". The true BEFORE is the pre-session state §2.6 describes; the AFTER is the code
as it actually runs — `promote.go` (957 lines) + `imports.go` (336 lines), both read in
full. **Verdict: the code faithfully implements the §2.6 spec; no drift found.** The
difference is the substrate transformation + precision details the prose under-states.

### Per-edge-class: gt_gt spec (BEFORE) → code reality (AFTER)
| edge class | gt_gt §2.6 says | code does (file:line) | confidence | match |
|---|---|---|---|---|
| **CO_SERIALIZES** | PROMOTE-NOW, 100% resolvable, value carries `@file:line`, undirected | `promoteSerde` parses `partner:<name>@file:<line>`, exact `(file,name,line)` key + any-file fallback, undirected dedup on min/max id (`promote.go:429-464, 230-236`) | **1.0 CERTIFIED** | ✓ |
| **READS** | PROMOTE-PARTIAL, reader → owning Class via parent_id; field-only stays property | `promoteFieldReads` → owning Class via `src.ParentID` (`label=Class`) (`:470-492`) | 0.6, **lifts to 0.9 if a declared `class_field`** (`:486`) | ✓ + precision |
| **WRITES** | side_effect write → owning Class | `promoteWrites`, same owning-Class resolve (`:498-520`) | 0.6 / 0.9 (declared field) | ✓ |
| **RAISES** | PROMOTE-PARTIAL, internal classes only, builtins stay property | `promoteRaises`: **drops dotted tokens** (`errors.New`, D3, `:545`), `cleanExceptionBase`, **105-name builtin denylist** (`:87-109`), polyglot `{Class,Struct,Type,Enum,Interface}` (`:526-576`) | **0.9** | ✓ |
| **PRECEDES** | PROMOTE-CAUTIOUS, distinct internal nodes only | `promotePrecedes` parses `a→b→c`, requires distinct internal func/method nodes (`:768-806`) | **0.5** (lowest — "cautious") | ✓ |
| **DATA_FLOW** | CALLS.metadata annotation; standalone only for no-CALLS hops | `forEachDataFlowTarget` + `promoteDataFlowStandalone` (mint only when no CALLS edge, `:632`) + `promoteDataFlowAnnotations` (append `dataflow=` tag, `:654`) | candidate count 1→0.8, 2→0.6, ≤5→0.4, **>5 suppressed** (`:751`) | ✓ |
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
3. DATA_FLOW **suppresses >5-candidate** hops (`:618`) — correct-or-quiet at the
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
**package-qualified Go field types (`Cache *cache.Store`) still ABSTAIN** (correct-or-quiet
under-resolution). Research: XTA (Tip & Palsberg OOPSLA'00) + CHA (Dean/Grove/Chambers
ECOOP'95).

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
- **R1 leaf-naming bridge** (`_semantic_leaf_names`, `:2070-2118`): fires ONLY when
  `defines_anchor` named nothing; ranks within-file leaves by per-symbol MaxSim + per-symbol
  FTS5, **RRF-fused** (Cormack SIGIR'09), **symbol-hub-demoted**; `[]` when no signal.
- **n_components wired** to the GT_META/8-dp telemetry (`SCOPE_COMPONENTS …`, `:3256` area)
  — the dead nerve, now a consumer. Regression: `test_n_components_signal_is_consumed`.

**THE CHANGE:** the brief OUTPUT volume is unchanged; the depth makes its scope-chain trust
**honest** and its leaf-naming **relevant** (the per-symbol semantic signal that reached gold).

---

## Appendix E — ORACLE / DELIVERY (gt_gt §15) → gt_mini_patch.py

**BEFORE (gt_gt §15).** `HorizonThresholds` stubbed (per-turn gate dead); edit-credit saw
only direct writes (`apply_patch`/`git apply` = 0 coverage); **monolithic** delivery (one
oracle exception lost the entire delivery, including always-on context — the 0/8 stub-crash);
in-container ran a divergent inline fallback; edit detection = closed verb whitelist.

**AFTER (code: `gt_mini_patch.py` + `gt_agent.py`).**
| change | code | commit |
|---|---|---|
| **hybrid bulkhead** — Lane A (context, always-on) delivers BEFORE Lane B (steers, one isolated try/except) → a steer crash can't undo context | `_lane_a_deliver` (`:4062`), Lane B (`:4297-4478`) | `35a3fb17`,`32e4e313` |
| **RC5 hybrid edit-credit** — apply_patch/git-apply/patch + staged diffs, ≥3-signal FACT-tier; dry-run flags excluded | `_classify`, `_is_patch_apply` (`:433`), `edit_coverage_ratio` | `a7a4be87`,`e6ddc06e` |
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
| **HIGH** | **Depth LEAKED INTO RANK** -- untyped edge JOINs in `v1r_brief.py` (`_top_functions:243`, `_top_function_names:300/313`, `_hub_degree_fn:1871`) counted promoted READS/WRITES/PRECEDES/DATA_FLOW into function ranking + the hub p80 (the section 4.2 "reach over-promotes hubs" violation, activated by the depth landing) | **FIXED** -- added `type='CALLS'`; depth feeds SCOPE only, never RANK |
| **MEDIUM** | `edit_risk` counted a 2-candidate `name_match` (conf 0.6 >= floor) as a dependent -- contradicting its own docstring | **FIXED** -- excluded by `resolution_method`; regression added |
| HIGH | `post_edit.py:157-163` docstring claims it admits `name_match cc<=1` (the old os.walk launder); the SQL correctly excludes name_match -- a "fix code to match docstring" edit would re-introduce the launder | **FIXED** -- docstring now matches SQL; SQL hardened `!= 'name_match'` -> `NOT LIKE 'name_match%'` (variants too); locked by `test_categorical_filter_no_namematch_launder` |
| MEDIUM | `_issue_relevant_neighbors` / 1-hop expansion add candidates to the "Calls:" line via DATA_FLOW/CO_SERIALIZES (untyped UNION) | **FIXED** -- all 4 JOINs now `type='CALLS'` (matches `_static_callees` sibling); locked by `test_neighbors_calls_only` |
| MEDIUM | `contract_map._read_props` reads properties without a confidence gate | **FIXED** -- `AND COALESCE(confidence,1.0) >= 0.5` (legacy no-column -> permissive); locked by `test_contract_props_confidence_gate` |
| **MEDIUM** | **Layer 4b auditability gap** -- no per-hook FIRE counter; the ledger records only DELIVERED/SUPPRESSED, and a fired-but-quiet hook is skipped before any record. "How many times did each hook fire?" is NOT answerable from disk -- only how many DELIVERED | **FIXED** -- `_record_hook_fire` counts every Lane-A fire (incl. fired-but-quiet) to `GT_HOOK_FIRE_COUNTS` JSON; locked by `test_hook_fire_counter` |
| LOW | scope-chain SELECT omits `trust_tier`; `STDLIB_MODULES` duplicated in two modules (drift risk); L4b reads no promoted edges (missed enrichment) | **PARTIAL** -- STDLIB drift LOCKED (`test_stdlib_modules_single_source` + comment fixed); scope-chain `trust_tier` (latent — no writer emits SUPPRESSED@conf>=0.5) + L4b promoted-edge enrichment (a feature, not a bug) remain OWED |

**The headline:** the LIPI caught the precise thing depth threatens -- promoted edges silently
entering RANK through latent untyped JOINs. Fixed. The remaining OWED items are tracked above;
none is a live laundering (all correct-or-quiet or latent).
