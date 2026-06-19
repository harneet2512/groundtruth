# Deep SWE PRODUCER Smoke — Rust (pest-parser/pest)

Controlled Axis-1 producer check. NO Docker / NO LLM. Producers called only via the snippet.
Date: 2026-05-31. Branch: gt-consensus-curation.

## Task tuple
- language: rust
- instance_id: pest-character-class-coalescing
- repo: https://github.com/pest-parser/pest
- commit: 79dd30d11aab6f0fba3cd79bd48f456209b966b3 (verified HEAD)
- issue_title: "Coalesce qualifying choices into character classes"

## 1. Clone + checkout
- clone OK; checkout OK; `git rev-parse HEAD` == 79dd30d11aab6f0fba3cd79bd48f456209b966b3.
- Cargo **workspace** with ~11 crates: bootstrap, debugger, derive, generator, grammars(+fuzz), meta(+fuzz), pest, vm.
- 75 `.rs` files outside `target/`. Indexer walked **107 files** total (includes examples/tests/build.rs/fuzz).

## 2. Index build (gt-index-t1t2.exe)
```
files: 107 | nodes: 1104 | edges: 875 | imports: 530 | properties: 2671 | assertions: 36
edges_import: 43 | edges_same_file: 307 | edges_name_match: 250 | time_ms: 6399 | workers: 12
```

## 3. Schema + provenance distribution
- **schema_version: v15.1-trust-tier**
- tables: assertions, cochanges, edges, file_hashes, nodes, project_meta, properties, sqlite_sequence
- nodes 1104 / edges 875

resolution_method:
| method | count | % |
|---|---|---|
| same_file | 307 | 35.1 |
| verified_unique | 256 | 29.3 |
| name_match | 250 | **28.6** |
| import | 43 | 4.9 |
| implements | 19 | 2.2 |

trust_tier: CERTIFIED 606 (69%) | SPECULATIVE 193 | CANDIDATE 57 | "" 19
confidence: 1.0→369, 0.95→256, 0.6→57, 0.4→151, 0.2→42
evidence_type: ast_call 307, unique_name 256, name_match 250, ast_import 43, "" 19
verification_status: unverified 856, "" 19 (note: nothing is marked "verified"; verification_status is a stub — trust comes from resolution_method/tier, not this column)

**name_match_pct: 28.6%** — well below the 70-80% large-repo figure in CLAUDE.md; pest is small + heavily same-crate so same_file + verified_unique dominate.

Representative file chosen: `meta/src/validator.rs` (68 funcs, 112 edges). Stored paths are **repo-relative**.

## 4. Producer output (L1 brief + L3b graph_navigation)
- TITLE issue, REL=`meta/src/validator.rs`, GT_REPO_ROOT=clone.

**L1 brief:** `<gt-task-brief>` present; `<gt-graph-map>` ABSENT (graphmap_present=False).
- L1_HYGIENE: **CLEAN** (no `[GT_META]/[GT_STATUS]/__GT_STRUCTURED__/[VERIFIED]/[WARNING]/[INFO]/v22` inside brief_text).
- Brief ranked `meta/src/parser.rs` #1 (witness "defines character"), surfaced real contracts (`consume_rules -> Result<Vec<AstRule>, Vec<Error<Rule>>>`), real callers in validator.rs:772/788/804, EDIT-TARGET CONTRACTS, related files (validator/parens/pairs).
- **Minor hygiene defect (non-blocking):** one mojibake char `�` (em-dash mis-encoded to cp1252) in the "Highlightest-confidence candidate ... meta/src/parser.rs � graph witness" line. Cosmetic, Windows console encoding artifact in the producer's string, not a leak.
- **Diagnostic leak to STDOUT (not into brief):** `[GT_CONFIG] L1_SCOPE=medium context=top=meta/src/parser.rs distinct=8 high=1` was printed to stdout during generation. It is NOT part of brief_text (hygiene of the delivered artifact is clean), but per CLAUDE.md diagnostics belong on stderr — flag as a stdout/stderr discipline nit.

**L3b graph_navigation(`meta/src/validator.rs`):**
- L3B_LINES[0]: ``Called by: meta/src/optimizer/mod.rs:33 `let map = to_hash_map(&rules);` `` — correct, real caller.
- L3B_CONTRACT: False (no `[CONTRACT]` line emitted for this file)
- L3B_LEAK: False (clean)

## 5. Rust-specific provenance check (impl/trait/assoc-fn, cross-crate homonyms)
Checked whether common method names (new/from/parse/next/peek/restore/is_empty/run/…) that have multiple defs across crates get falsely stamped CERTIFIED.

- CERTIFIED edges to common names are overwhelmingly **same_file** (caller+callee co-located, legitimately 1.0) or **import** (verified `use`). Those are sound.
- `verified_unique` edges: 256 total. **10** target a name that has >1 definition repo-wide. Spot-checked all the high-risk ones against source:

| call site | target name | resolved to | correct? |
|---|---|---|---|
| parser_state.rs:1066 `self.position.match_char_by` | match_char_by | position.rs:378 | ✔ correct (receiver = Position) |
| parser_state.rs:1769 `self.stack.restore()` | restore | stack.rs:111 | ✔ correct (receiver = Stack) |
| debugger/main.rs:57 `self.context.run()` | run | debugger/lib.rs | ✔ correct (receiver = context) |
| parens.rs:57 `expr(p.into_inner())` (recursion) | expr | **pratt_parser.rs** | ✘ WRONG — should bind local `fn expr`@parens.rs:54 |
| stack.rs:62 `self.cache.is_empty()` (Vec) | is_empty | **iterators/pairs.rs:378** | ✘ WRONG — `self.cache: Vec<T>`, this is stdlib `Vec::is_empty`, no in-repo edge should exist |

The resolver DOES use receiver/import context to disambiguate most homonyms correctly (good). But it is **receiver-type-blind on the fallback path**: when a method call's true target is stdlib/external (`Vec::is_empty`) or a same-file recursion, and an unrelated in-repo homonym exists, it can bind to that homonym and stamp it `verified_unique` / CERTIFIED / conf 0.95.

## PROVENANCE BUG (real, generalizes)
**False `verified_unique`/CERTIFIED on receiver-type-blind homonym resolution.** Confirmed 2/10 multi-def verified_unique edges are wrong:
1. `Vec::is_empty` (stdlib) → laundered to `Pairs::is_empty` in-repo (stack.rs:62 → iterators/pairs.rs).
2. same-file recursion `expr` → laundered to a cross-file `expr` (parens.rs → pratt_parser.rs).

Impact: low-volume here (2 confirmed, examples/leaf code), but it is a CERTIFIED-tier false edge — the worst kind because the trust tier suppresses skepticism. On larger Rust repos with more stdlib-named methods (push/next/len/get/insert/iter) this class will scale. Root cause: name-only uniqueness check without (a) excluding stdlib/trait receiver types and (b) preferring same-file/same-impl candidates for recursion.

The other 8 multi-def verified_unique edges I spot-checked resolved correctly. name_match (28.6%) and SPECULATIVE/CANDIDATE tiers behaved as designed.

## Verdicts
- clone_ok: yes | index_ok: yes | schema v15.1-trust-tier
- L1 hygiene: CLEAN (artifact); two nits — mojibake `�`, `[GT_CONFIG]` on stdout
- graphmap_present: False | L3b contract: False | L3b leak: False
- provenance_bug: receiver-type-blind homonym → false CERTIFIED verified_unique (2/10 confirmed wrong)
