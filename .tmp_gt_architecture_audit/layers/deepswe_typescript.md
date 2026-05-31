# Deep SWE PRODUCER Smoke — TypeScript (ink)

Controlled producer check (Axis-1). NO Docker / NO LLM. Producers called only via snippet.

## Task tuple
- language: typescript
- instance_id: `ink-grid-box-layout`
- repo: https://github.com/vadimdemedes/ink
- commit: `0cea59169ef0f3f83e4aa7fbedbff9d165646472` (verified HEAD)
- issue: "Add CSS Grid layout to the Box component"

## 1. Clone + build
- clone_ok: TRUE. Checked out commit, `git rev-parse HEAD` == target.
- Source layout: 50 `.ts` + 10 `.tsx` under `src/` (incl. target `src/components/Box.tsx`). Whole repo (incl. `test/`, `examples/`, `benchmark/`) = 216 files indexed.
- index_ok: TRUE. Binary `gt-index/gt-index-t1t2.exe`. BuildTime 5568 ms.
  - Nodes 1167, Edges 2036 (CALLS 1521 + COMPOSES 515), Imports 1059, Properties 3113, Assertions 33.

## 2. Schema
- schema_version: **v15.1-trust-tier** (indexer_version v16-multilang)
- Tables: assertions, cochanges, edges, file_hashes, nodes, project_meta, properties, sqlite_sequence
- edge cols include trust_tier, candidate_count, evidence_type, verification_status
- min_confidence (meta) = 0.95

## 3. Provenance distribution
**Edge type**: CALLS 1521, COMPOSES 515.
**resolution_method**: verified_unique 639, name_match 517, jsx_component 515, same_file 365.
**trust_tier**: CERTIFIED 1004, '' (empty) 515, CANDIDATE 301, SPECULATIVE 216.
**verification_status**: unverified 1521, '' 515.
**name_match %**: 25.4% of all edges, **34.0% of CALLS** (517/1521).
**name_match candidate_count**: cand=2 →301, cand=15 →96, cand=6 →90, cand=5 →9 ...

**Node provenance (ROOT-CAUSE OF NOISE)**: src 193 (16.5%), **test 916 (78%)**, examples 52, benchmark 1, other 5. Indexer ingests the entire repo tree; test fixtures dominate the graph.

**Nodes by ext**: .tsx 886, .ts 276, .js 5. **`.tsx` IS indexed** (tsx_indexed = TRUE).

## 4. Producer excerpts
REL = `src/styles.ts` (chosen: holds `applyFlexStyles`/`applyDisplayStyles`/`applyGapStyles` — the exact functions a Grid feature touches; Box.tsx extracts 0 nodes, see bugs).

**L1 brief (`generate_v1r_brief`)** — text:
```
<gt-task-brief>
1. src/components/FocusContext.ts (add() {}, activate() {}, deactivate() {})
   Callers: useFocus() ... | addLayoutListener() ... | useFocus() ...
   Context: remove, enableFocus, ... | Last: 4ed6267 Update dependencies (#643)
   Calls: src/components/App.tsx, src/dom.ts, src/hooks/use-focus.ts
</gt-task-brief>
<gt-graph-map>
src/components/FocusContext.ts :: add
  called by: addLayoutListener (src/dom.ts), useFocus (src/hooks/use-focus.ts)
</gt-graph-map>
```
- L1_HYGIENE: **CLEAN** (no [GT_META]/[VERIFIED]/[WARNING]/[INFO]/v22/__GT_STRUCTURED__ in brief_text).
- HAS_BRIEF_TAG: TRUE, HAS_GRAPHMAP: TRUE.

**L3b graph_navigation(`src/styles.ts`)**:
```
[CONTRACT] (node: YogaNode, style: Styles): void => { -> : void
[CONTRACT] ( -> : void
```
- L3b_contract: TRUE, L3b_leak: FALSE, COMPOSES NOT surfaced to agent (checked App.tsx + ink.tsx too).

**Diagnostics routing**: `[GT_META]`/`[GT_CONFIG]` go to **stderr** (0 on stdout). Correct.

## 5. TS-specific provenance verdict
- **`.js`→`.ts` ESM specifier resolution WORKS.** ink imports `'../use-app.js'` etc.; resolver correctly maps `useApp -> use-app.ts`, `useInput -> use-input.ts` as verified_unique cross-file edges.
- **verified_unique integrity HOLDS**: all 639 verified_unique/CERTIFIED edges target a name present in exactly ONE file (0 collisions). No false certifications from homonyms in the CALLS layer.
- Barrel: `src/ink.tsx` is the package barrel; indexed. No separate `src/index.ts`.

## BUGS / DEFECTS

### BUG-1 (HIGH) — COMPOSES (jsx_component) edges are ungated and frequently FALSE
515 COMPOSES edges resolve a JSX tag `<Foo>` to a `Foo` node by **bare name match with NO disambiguation and NO trust tier** (trust_tier='', verification_status=''). They sit OUTSIDE the SPECULATIVE/CANDIDATE/CERTIFIED model.
- **137/515 (27%)** target a component name defined in 2+ files; resolver picks an arbitrary wrong file.
  - `static.tsx <run> composes App -> picked test/fixtures/console.tsx` (App has 9 defs).
  - `index.tsx <App> composes App -> picked use-input-multiple.tsx`.
  - `<constructor> composes Test -> picked ci.tsx` out of 21 `Test` candidates.
- Parser emits **test-block description strings as component nodes**, e.g. `<test: Mixed text with and without background inheritance> composes Test`.
- **MITIGATION**: L3b/L1 producers do NOT surface COMPOSES to the agent (verified). So this is a graph-integrity defect, not (currently) an agent-delivery leak. Risk: any future producer that reads COMPOSES would ship wrong facts untagged.

### BUG-2 (HIGH) — Target file `src/components/Box.tsx` extracts ZERO nodes
Box (the literal subject of the issue) is a `forwardRef((props, ref) => {...})` arrow component. The indexer extracts 0 function/class nodes for the whole file. The agent gets no contract, no callers, no graph map for the exact symbol it must modify. (App.tsx `function App` extracts; styles.ts/dom.ts extract fine — the gap is specifically the `forwardRef(arrow)` and bare exported-arrow-component pattern.)

### BUG-3 (MED) — Contract truncation on multi-line TS signatures (C1 defect, still live)
L3b emits clipped contracts when a signature spans lines:
- styles.ts: `[CONTRACT] ( -> : void` (should be `applyBorderStyles(...)`).
- App.tsx: `[CONTRACT] function App({ -> : React.ReactNode` (destructure clipped at `({`).
Multi-line params are common in TS; the clip yields a meaningless `(`.

### BUG-4 (MED) — Empty/anonymous-arrow contracts as noise
ink.tsx L3b emits `[CONTRACT] () => {}` and `[CONTRACT] async () =>` — zero-signal contracts for anonymous arrows. Should be suppressed.

### BUG-5 (MED) — Whole-tree indexing pollutes graph with test/example nodes
78% of nodes are from `test/`, only 16.5% from `src/`. Test fixtures define many same-named `App`/`Test`/`Example` components → this is the ROOT CAUSE of BUG-1 ambiguity and the cand=15 name_match noise (96 edges resolving `render -> examples/.../demo.js`). No src/test partitioning in the graph.

### BUG-6 (LOW) — L1 mislocalization
For "Add CSS Grid to Box", L1 ranked `FocusContext.ts` #1, not `styles.ts`/`Box.tsx`. Known L1 ranker mislocalization; agent-visible but a localization-quality issue, not a hygiene/leak bug.

## Hygiene verdict
Agent-delivered surfaces (L1 brief_text, L3b lines) are CLEAN: no GT_META/structured/tier-marker leakage; diagnostics on stderr. The serious defects are in the GRAPH (BUG-1 false ungated COMPOSES, BUG-2 missing Box, BUG-5 test pollution) and contract formatting (BUG-3/4), not in delivery hygiene.
