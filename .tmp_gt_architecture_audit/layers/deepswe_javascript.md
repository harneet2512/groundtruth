# Deep SWE PRODUCER Smoke — JavaScript (csstree)

Controlled producer check (Axis-1). NO Docker / NO LLM. Producers called via snippet only.

## Task Tuple
- language: javascript
- instance_id: csstree-shorthand-expansion-compression
- repo: https://github.com/csstree/csstree
- commit: 88e3d965c0b1628642a30a841745b410d6835052
- issue_title: "Add shorthand expansion and compression to the lexer"

## 1. Clone + Checkout
- clone: OK
- checkout HEAD: `88e3d965c0b1628642a30a841745b410d6835052` ("Add note about stacked multipliers") — confirmed via `git rev-parse HEAD`

## 2. Index Build (gt-index-t1t2.exe)
- files=204, nodes=630, edges=1089, imports=625, properties=4503, assertions=0
- edges_import=71, edges_same_file=291, edges_name_match=301
- build_time=6393 ms, workers=12
- index_ok: YES

## 3. Schema + Provenance
- schema_version: **v15.1-trust-tier**
- indexer_version: v16-multilang
- min_confidence: 0.95
- tables: assertions, cochanges, edges, file_hashes, nodes, project_meta, properties, sqlite_sequence
- edge cols: id, source_id, target_id, type, source_line, source_file, resolution_method, confidence, metadata, trust_tier, candidate_count, evidence_type, verification_status

### resolution_method distribution (1089 edges)
| method | count | % |
|---|---|---|
| verified_unique | 422 | 38.8 |
| name_match | 301 | 27.6 |
| same_file | 291 | 26.7 |
| import | 71 | 6.5 |
| re_export | 4 | 0.4 |

**name_match %: 27.6%**

### trust_tier distribution
| tier | count | % |
|---|---|---|
| CERTIFIED | 784 | 72.0 |
| CANDIDATE | 180 | 16.5 |
| SPECULATIVE | 121 | 11.1 |
| (blank) | 4 | 0.4 |

### evidence_type x trust_tier
- unique_name / CERTIFIED: 422
- ast_call / CERTIFIED: 291
- name_match / CANDIDATE: 180
- name_match / SPECULATIVE: 121
- ast_import / CERTIFIED: 71
- (blank, re_export): 4

confidence buckets: 1.0=788, 0.6=180, 0.4=69, 0.2=52
edge types: CALLS=1085, RE_EXPORTS=4

Representative source file: **lib/lexer/Lexer.js** (34 nodes, 33 funcs, 62 out-edges, 51 in-edges; on-topic for the lexer issue).

## 4. Producer Outputs

### L1 brief (generate_v1r_brief)
- HAS_BRIEF_TAG `<gt-task-brief>`: True
- HAS_GRAPHMAP `<gt-graph-map>`: True
- L1_HYGIENE: **CLEAN** (no [GT_META]/[GT_STATUS]/__GT_STRUCTURED__/[VERIFIED]/[WARNING]/[INFO]/v22 inside brief_text)
- Note: `[GT_CONFIG]` and `[GT_META] contract_pillar` lines print to stderr/stdout as diagnostics; they are NOT in brief_text (verified absent). Diagnostic-only.

Brief surfaced Lexer.js with contract ("returns collection"), sibling context (dumpMapSyntax, dumpAtruleMapSyntax, valueHasVar...), Calls lines, and a graph-map block. Ranking put `lib/__tests/lexer.js` at #1 over `lib/lexer/Lexer.js` at #2 — minor mislocalization (test file ranked above source), not a hygiene leak.

### L3b graph_navigation(lib/lexer/Lexer.js)
- L3B_CONTRACT: True (`[CONTRACT]` lines present)
- L3B_LEAK: False (no [GT_META]/__GT_STRUCTURED__)
- Rendered [CONTRACT] for dumpMapSyntax/dumpAtruleMapSyntax/valueHasVar + a "Called by" line.

## 5. JS-Specific Provenance Findings

### Module system
csstree is **ESM** (`import` / `export`, incl. `export * from`, `export { default as X }`, named re-exports). A few files (data.js, match.js, data-patch.js) also use `require()` — mixed. Import edges (ast_import, 71) spot-checked correct, e.g. `definition-syntax-match.js -> buildMatchGraph (match-graph.js)` [import/CERTIFIED] — true positive.

### BUG (material): built-in / prototype method-name collisions laundered as CERTIFIED verified_unique
When exactly ONE user-defined node bears a name that is also a JS built-in prototype method, the resolver marks every `x.<name>(...)` call site as `verified_unique` / CERTIFIED / conf=0.95, with no awareness that the call is on a String/Array/Object built-in.

Confirmed false positives (read from source):
- `scripts/docs/ast.js:25` `section.match(/^\w+/)` → String.prototype.match, resolved to `Lexer#match` (Lexer.js:381) CERTIFIED
- `lib/parser/SyntaxError.js:25` `...substr(...).match(/\t/g)` → String.match, resolved to `Lexer#match` CERTIFIED
- `lib/data.js:27` `...replace(...).match(/^@\S+.../)` → String.match, resolved to `Lexer#match` CERTIFIED
- `lib/__tests/parse.js:51` `value.match(/^([+-]?)(\d+)?n/i)` → String.match, resolved to `Lexer#match` CERTIFIED
- `lib/__tests/clone.js:17` `astNodes.push(node)` → Array.push, resolved to `List#push` (List.js:397) CERTIFIED
- `lib/__tests/fixture/tokenize.js:12` `ensureArray(...).map(...)` → Array.map, resolved to `List#map` (List.js:209) CERTIFIED
- `lib/__tests/fixture/ast.js:40` `Object.keys(...).forEach(...)` → Array.forEach, resolved to `List#forEach` (List.js:138) CERTIFIED
- `lib/data.js:57` `...syntax.replace(extendSyntax,'')` → String.replace, resolved to `List#replace` (List.js:460) CERTIFIED
- `lib/lexer/match-graph.js:494` `...substr(...).replace(/\\'/g,...)` → String.replace, resolved to `List#replace` CERTIFIED

Blast radius: **156 of 422 verified_unique edges (37%)** point into single-user-def nodes whose names are built-in prototype methods. Top offenders: push=71, forEach=22, map=18, replace=17, filter=9, pop=8, match=5, reduce=5. These all carry CERTIFIED / conf=0.95.

Note: the GRAPH only ever creates ONE target node per name (it did NOT fabricate extra `match` nodes), so the structural model is single-node-correct; the defect is that the **resolver attaches built-in/prototype call sites to that lone user node and certifies them** instead of leaving them unresolved or low-confidence. The custom `List` class genuinely has `.push/.map/.forEach/.replace`, so a name-only resolver cannot tell `list.push()` (real) from `array.push()` (built-in) — it certifies both.

Impact on producers: L3b "Called by" for Lexer.js mixed genuine callers with these false String.match edges, presenting regex-match call sites as callers of `Lexer#match`. This is the agent-visible symptom of the underlying CERTIFIED-collision bug.

### BUG (minor): re_export edge attributed to a function node, wrong target
The 4 RE_EXPORTS edges have blank trust_tier. Example: `export * from './char-code-definitions.js'` at tokenizer/index.js:510 produced edge `tokenize (index.js:25) -> isDigit (char-code-definitions.js:8)`. The wildcard re-export was scoped to the `tokenize` function node (line 510 falls after it) rather than to a module node, and a single arbitrary target (isDigit) was picked. Node-scoping artifact for `export *`. Low impact (4 edges, blank tier, not in CERTIFIED counts).

## Hygiene Verdict
- L1 brief_text: CLEAN (no leak markers; diagnostics correctly kept out of brief_text)
- L3b: CLEAN (no [GT_META]/__GT_STRUCTURED__ leak); contract lines present
- Producer plumbing: PASS
- Graph provenance: **FAIL on JS built-in/prototype collisions** — 37% of verified_unique edges are over-certified (CERTIFIED/0.95) on prototype-method calls. This poisons "Called by" / caller evidence with confident-but-false callers, exactly the failure mode the constitution warns against ("confident on weak signals").

## Bugs Summary
1. **provenance_bug (material):** JS built-in/prototype method-name collisions resolved as `verified_unique`/CERTIFIED/0.95 — 156/422 (37%) verified_unique edges; resolver has no built-in-method awareness; cannot distinguish custom List.push from Array.push.
2. **provenance_bug (minor):** `export *` re-export edges scoped to a function node with an arbitrary single target (4 edges, blank tier).
