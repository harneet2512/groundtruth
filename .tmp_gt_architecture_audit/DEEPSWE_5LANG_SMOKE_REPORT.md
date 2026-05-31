# DEEPSWE_5LANG_SMOKE_REPORT.md — Phase 7 producer smoke (5 languages, fresh GitHub corpus)

Date 2026-05-31. 5 parallel agents, each: clone the real Deep SWE source repo @commit_hash, build a fresh
`graph.db` with `gt-index-t1t2.exe`, run the GT producers (`generate_v1r_brief` + `post_view.graph_navigation`),
verify Axis-1 (logic). **NOT** an autonomous agent run — producer-level controlled trace. Issue text = task `display_title`.

## Task matrix

| lang | repo @commit | files/nodes/edges | name_match% | schema | clone/index |
|---|---|---|---|---|---|
| python | Fatal1ty/mashumaro @de139fd | 148 / 1765 / 2915 | 12.7 | v15.1-trust-tier | OK |
| go | mattn/anko @9d2d84b | 94 / 400 / 651 | 18.9 | v15.1 | OK |
| typescript | vadimdemedes/ink @0cea591 | / 1167 / 2036 | 34.0 | v15.1 | OK |
| rust | pest-parser/pest @79dd30d | 75 / 1104 / 875 | 28.6 | v15.1 | OK |
| javascript | csstree/csstree @88e3d96 | / 630 / 1089 | 27.6 | v15.1 | OK |

## Delivery hygiene (Axis-1, agent-facing) — PASS across 5/5

| lang | L1 `<gt-task-brief>` | `<gt-graph-map>` | no tier-label/leak | L3b `[CONTRACT]` | L3b leak |
|---|---|---|---|---|---|
| python | ✓ | ✓ | CLEAN | (not on REL fn — suppression) | none |
| go | ✓ | ✓ | CLEAN | ✓ | none |
| typescript | ✓ | ✓ | CLEAN | ✓ | none |
| rust | ✓ | ✗ (none this brief) | CLEAN | ✗ (none on REL) | none |
| javascript | ✓ | ✓ | CLEAN | ✓ | none |

`[GT_META]`/`[GT_STATUS]`/`__GT_STRUCTURED__`/`[VERIFIED]`/`v22` never appeared in `brief_text` or returned L3b lines.
Diagnostics stayed on stderr (a couple of agents noted `[GT_CONFIG]` reaching **stdout** during brief gen — to confirm).

## GRAPH PROVENANCE (Axis-1, the headline) — FAIL across 5/5

**Root cause (confirmed in 3 real repos + the local shadow fixture): the resolver's `verified_unique`/`type_flow`
strategy is RECEIVER-TYPE-BLIND.** It promotes a globally-/locally-unique *name* to `CERTIFIED` (conf 0.95–1.0) without
checking the call's receiver type — so builtin / stdlib / external method calls get laundered as deterministic
PROJECT-method facts.

| lang | over-certified false edges | example |
|---|---|---|
| python | **38** verified_unique/CERTIFIED | `list.append()` (type_params/result/required.append) → project `CodeLines.append` |
| go | **≥29** CERTIFIED | `reflect.Value.String/.Set/.Len`, `bufio.Scanner.Scan`, `flag.Parse` → `env.Env.String/Set`, `SortFuncsStruct.Len`, `parser.Scan/Parse` |
| javascript | **156/422 (37%)** verified_unique | `Array.push/forEach/map`, `String.match/replace` → user `List#push/...`, `Lexer#match` |
| typescript | `COMPOSES` jsx **137/515 (27%)** (trust=''), arbitrary wrong file | `<run>` composes App → test/fixtures/console.tsx. (verified_unique CALLS HELD: 0/639) |
| rust | **2/10** multi-def verified_unique | `Vec::is_empty` → `Pairs::is_empty`; same-file recursion `expr()` → cross-file `expr` |

**Agent-facing confirmation (not merely graph-internal):**
- **JS** — false callers surface in the L3b "Called by:" line delivered to the agent.
- **Go** — a false edge (`processCallReturnValues -> env.Env.String`, actually a `reflect`/`fmt` call) surfaces in the L1 **EDIT-TARGET CONTRACTS** block.

The local `_is_stdlib_shadow` guard in `v1r_brief.py` only catches the Python `<module>.<name>` *function* shape on the
brief render — it does NOT cover builtin *methods* (`.append`), Go/Rust/JS receiver methods, the graph itself, L3/L3b, or the map.

## Secondary findings
- **Schema v15.1-trust-tier** in every fresh graph — DOC §0.6 claims **v15.2** (doc-vs-code drift, all 5 confirm v15.1).
- **TS `Box.tsx` (forwardRef arrow component) extracted ZERO nodes** — the agent gets no contract/callers/graph for the symbol it must edit. Real extraction gap (tree-sitter TS arrow/forwardRef).
- **Em-dash → `�` mojibake** in brief on the Windows cp1252 console (cosmetic; UTF-8 container unaffected) — seen in python/rust.
- **L1 mislocalization** (Axis-2, deferred to live run): TS ranked FocusContext.ts over Box.tsx; JS ranked test/lexer.js over Lexer.js; rust surfaced parser.rs not optimizer/.
- TS noise driver: 78% of nodes from `test/` (only 16.5% `src/`) → COMPOSES ambiguity + cand=15 name_match.

## Verdict
- **Producer DELIVERY (Axis-1 hygiene): PASS** on 5/5 — clean, no leak, contract/graph-map present, correct-or-quiet.
- **Graph PROVENANCE (Axis-1 correctness): FAIL** on 5/5 — the `verified_unique` receiver-blind P0 is **generalized and agent-facing**. This is the single architecture defect that most undermines the "name_match is never a fact" contract: it mislabels false callers as the *highest* trust tier, defeating every downstream gate.
- **Architecture readiness: PARTIAL → NOT READY** until P0 (resolver receiver-type gate) is fixed. Fix is **Go/CI-only** here (no Go toolchain locally).
