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
