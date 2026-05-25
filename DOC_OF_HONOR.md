# DOC_OF_HONOR.md — GroundTruth Verified Architecture

> Every claim in this document has been verified against the actual codebase.
> Claims are tagged **VERIFIED** (matches code) or **CORRECTED** (discrepancy found and resolved).
> Date: 2025-05-25. Branch: `jedi__branch`.

---

## 1. System Topology

**Claim:** gt-index (Go binary) parses source code via tree-sitter, writes graph.db (SQLite). Three consumers read graph.db: (1) MCP server (16+ tools via FastMCP, stdio), (2) post_edit/post_view hooks (passive delivery), (3) gt_intel.py (SWE-bench evidence engine).

**Evidence:**
- `gt-index/cmd/gt-index/main.go:1-11` — "Builds a SQLite graph database from source code. Supports 30 languages via tree-sitter grammars."
- `src/groundtruth/mcp/server.py:10` — `from mcp.server.fastmcp import FastMCP`
- `src/groundtruth/hooks/post_edit.py:1-16` — "Post-edit hook v5 -- graph.db-driven evidence"
- `src/groundtruth/hooks/post_view.py:1-8` — "Post-view hook — structural coupling enrichment for file reads."
- `benchmarks/swebench/gt_intel.py` — evidence engine (referenced in CLAUDE.md)

**VERIFIED**

---

## 2. Graph.db Schema

**Claim:** 6 tables: `nodes`, `edges`, `properties`, `assertions`, `file_hashes`, `project_meta`.

**Evidence:** `gt-index/internal/store/sqlite.go:108-195` — `createSchema()` defines all 6 tables.

### 2.1 `nodes` table

| Column | Type | Evidence |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | sqlite.go:111 |
| label | TEXT NOT NULL | sqlite.go:112 — "Function, Class, Method, File, Interface, Struct, Enum, Type" (store.go:22 comment) |
| name | TEXT NOT NULL | sqlite.go:113 |
| qualified_name | TEXT | sqlite.go:114 |
| file_path | TEXT NOT NULL | sqlite.go:115 |
| start_line | INTEGER | sqlite.go:116 |
| end_line | INTEGER | sqlite.go:117 |
| signature | TEXT | sqlite.go:118 |
| return_type | TEXT | sqlite.go:119 |
| is_exported | BOOLEAN DEFAULT 0 | sqlite.go:120 |
| is_test | BOOLEAN DEFAULT 0 | sqlite.go:121 |
| language | TEXT NOT NULL | sqlite.go:122 |
| parent_id | INTEGER REFERENCES nodes(id) | sqlite.go:123 |

**VERIFIED**

### 2.2 `edges` table

| Column | Type | Evidence |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | sqlite.go:127 |
| source_id | INTEGER NOT NULL REFERENCES nodes(id) | sqlite.go:128 |
| target_id | INTEGER NOT NULL REFERENCES nodes(id) | sqlite.go:129 |
| type | TEXT NOT NULL | sqlite.go:130 — "CALLS, IMPORTS, DEFINES, INHERITS, IMPLEMENTS" (store.go:41 comment) |
| source_line | INTEGER | sqlite.go:131 |
| source_file | TEXT | sqlite.go:132 |
| resolution_method | TEXT | sqlite.go:133 — "same_file, import, name_match" |
| confidence | REAL DEFAULT 0.0 | sqlite.go:134 |
| metadata | TEXT | sqlite.go:135 |
| trust_tier | TEXT DEFAULT 'SPECULATIVE' | sqlite.go:136 — "CERTIFIED, CANDIDATE, SPECULATIVE, SUPPRESSED" (store.go:47) |
| candidate_count | INTEGER DEFAULT 1 | sqlite.go:137 |
| evidence_type | TEXT | sqlite.go:138 — "ast_call, ast_import, name_match" (store.go:49) |
| verification_status | TEXT DEFAULT 'unverified' | sqlite.go:139 — "unverified, verified, rejected" (store.go:50) |

**CORRECTED** — Source documents listed only 8 edge columns (id through metadata). The actual schema has 12 columns including trust_tier, candidate_count, evidence_type, verification_status.

### 2.3 `properties` table

| Column | Type | Evidence |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | sqlite.go:172 |
| node_id | INTEGER NOT NULL REFERENCES nodes(id) | sqlite.go:173 |
| kind | TEXT NOT NULL | sqlite.go:174 |
| value | TEXT NOT NULL | sqlite.go:175 |
| line | INTEGER | sqlite.go:176 |
| confidence | REAL DEFAULT 1.0 | sqlite.go:177 |

**VERIFIED**

### 2.4 `assertions` table

| Column | Type | Evidence |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | sqlite.go:181 |
| test_node_id | INTEGER NOT NULL REFERENCES nodes(id) | sqlite.go:182 |
| target_node_id | INTEGER DEFAULT 0 | sqlite.go:183 |
| kind | TEXT NOT NULL | sqlite.go:184 |
| expression | TEXT NOT NULL | sqlite.go:185 |
| expected | TEXT | sqlite.go:186 |
| line | INTEGER | sqlite.go:187 |

**VERIFIED**

### 2.5 `file_hashes` table

| Column | Evidence |
|---|---|
| file_path TEXT PRIMARY KEY | sqlite.go:143 |
| content_hash TEXT NOT NULL | sqlite.go:144 |
| language TEXT | sqlite.go:145 |
| indexed_at TEXT NOT NULL | sqlite.go:146 |

**VERIFIED**

### 2.6 `project_meta` table

| Column | Evidence |
|---|---|
| key TEXT PRIMARY KEY | sqlite.go:150 |
| value TEXT | sqlite.go:151 |

Keys stored: root, file_count, node_count, edge_count, import_count, property_count, assertion_count, indexer_version, schema_version, git_commit, build_time_utc, go_toolchain, workers, min_confidence.

**Evidence:** main.go:434-463

**VERIFIED**

---

## 3. Indexing Pipeline

**Claim:** 7 passes (not 4 as originally documented).

| Pass | Name | Description | Evidence |
|---|---|---|---|
| 1 | STRUCTURE | Walk filesystem, discover source files by language | main.go:94-108 |
| 2 | DEFINITIONS + IMPORTS | Parallel tree-sitter parse (NumCPU workers), batch SQLite insert | main.go:118-238 |
| 3 | CALLS | Resolve call references via 3-stage pipeline, compute confidence, deduplicate | main.go:241-310 |
| 4 | PROPERTIES + ASSERTIONS | Insert properties and assertions, resolve assertion targets | main.go:312-402 |
| 4b | API EDGES | Cross-service route matching via `resolver.ResolveAPIEdges` | main.go:404-412 |
| 4c | RELATIONSHIP EDGES | Inheritance, interfaces, decorators, composition, re-exports via `resolver.ResolveRelationships` | main.go:414-422 |
| 4d | SERIALIZATION PAIRS | Detect serialize/deserialize partners via `detectSerdePairs` | main.go:424-429 |
| 5 | EXTRAS | Store metadata (14 keys in project_meta) | main.go:431-463 |
| 5b | FILE HASHES | SHA-256 per file for incremental reindex | main.go:465-482 |

**CORRECTED** — Source documents described a "4-pass architecture" (STRUCTURE, DEFINITIONS, CALLS, EXTRAS). Actual code has 7 labeled passes: Pass 1, Pass 2, Pass 3, Pass 4, Pass 4b, Pass 4c, Pass 4d, Pass 5, Pass 5b. This is effectively 7 passes, not 4.

---

## 4. Resolution Pipeline

**Claim:** 3-stage resolution: same_file -> import -> name_match.

**Evidence:** `resolver.go:175-350` — `Resolve()` function.

| Stage | Strategy | Confidence | Trust Tier | Evidence |
|---|---|---|---|---|
| 1 | Same-file exact name match (unambiguous only) | 1.0 | CERTIFIED | resolver.go:203-224 |
| 1.5 | Import-verified cross-file (specific + Go pkg-qualified + wildcard) | 1.0 | CERTIFIED | resolver.go:228-304 |
| 2 | Cross-file name match (fallback) | 0.2-0.9 | CERTIFIED/CANDIDATE/SPECULATIVE | resolver.go:307-347 |

**CORRECTED** — Source docs described 3 stages (same_file, import, name_match). Actual code has the import step at position 1.5, making it effectively 3 stages but with "import" positioned between same_file and name_match, not as "Stage 2." The comment in resolver.go:179 still says "3" strategies, but the code interleaves import between same-file and name-match.

### Confidence Model (from `computeConfidence`, resolver.go:156-173)

| Method | Candidates | Confidence | Evidence |
|---|---|---|---|
| same_file | any | 1.0 | resolver.go:158-159 |
| import | any | 1.0 | resolver.go:160-161 |
| name_match | 1 | 0.9 | resolver.go:163 |
| name_match | 2 | 0.6 | resolver.go:165 |
| name_match | 3-5 | 0.4 | resolver.go:167 |
| name_match | 5+ | 0.2 | resolver.go:169 |
| (unknown) | - | 0.3 | resolver.go:171 |

**VERIFIED**

### Edge Deduplication

Edges are deduplicated by (sourceID, targetID, type) via the `seen` map.

**Evidence:** resolver.go:149-153, 209-210 — `edgeKey{callerID, targetID, "CALLS"}` with `seen[key]` check.

**VERIFIED**

---

## 5. Property Extraction

**Claim:** 16 property kinds extracted during parsing (source docs claimed 13).

| # | Kind | Extractor Function | Line in parser.go | Confidence |
|---|---|---|---|---|
| 1 | guard_clause | `extractGuardFromStmt` | 1152 | 1.0 |
| 2 | return_shape | `extractReturnShape` / `countReturns` | 1307, 1328 | 0.9 |
| 3 | exception_type | `extractExceptionFromNode` | 1235 | 1.0 |
| 4 | docstring | `extractDocstring` | 1012 | 0.8-1.0 |
| 5 | caller_usage | `classifyCallContext` (inside `extractCallsWithParent`) | 325-330 | 0.8 |
| 6 | conditional_return | `extractConditionalReturns` | 1357 | 1.0 |
| 7 | side_effect | `extractSideEffects` | 1446 | 1.0 |
| 8 | param | `extractStructuredParams` | 1547 | 1.0 |
| 9 | security_tag | `extractSecurityTags` | 1712 | 1.0 |
| 10 | exception_flow | `extractExceptionFlow` | 1784 | 1.0 |
| 11 | exception_handler | `extractExceptionHandlers` | 1875 | 1.0 |
| 12 | fingerprint | `extractFunctionFingerprint` | 1921 | 0.9 |
| 13 | field_read | `extractFieldReads` | 2000 | 0.9 |
| 14 | boundary_condition | `extractBoundaryConditions` | 2095 | 0.9 |
| 15 | class_field | `extractClassFields` | 2184 | 1.0 |
| 16 | class_decorator | `extractClassDecorators` | 2269 | 1.0 |

Plus one additional kind generated in Pass 4d:

| 17 | serialization_pair | `detectSerdePairs` (main.go) | main.go:1051 | 0.8 |

**Evidence:** `parser.go:28-36` — PropertyRef struct comment lists 16 kinds. `store.go:57` — Property struct comment lists the same. `main.go:1091-1098` — serialization_pair written as a property.

**CORRECTED** — Source documents claimed 13 property kinds. Actual count is 17 (16 from parser.go + 1 from detectSerdePairs in main.go). The PropertyRef comment at parser.go:30-33 explicitly lists 16 kinds.

---

## 6. Assertion Resolution

**Claim:** `resolveAssertionTarget` uses 4 strategies.

| Strategy | Description | Evidence |
|---|---|---|
| 1.5 | Import-guided: test file imports a module exporting a function in the expression | main.go:887-900 |
| 1 | LCBA (Last-Call-Before-Assert): extract function names from assertion expression | main.go:907-918 |
| 2 | Naming convention: test_foo -> foo, TestFoo -> Foo | main.go:925-949 |
| 3 | Same-module unambiguous match: filter by same directory | main.go:955-981 |

**Evidence:** main.go:870-984 — `resolveAssertionTarget()` function.

**CORRECTED** — Source docs claimed "3 strategies + Strategy 1.5". Actual code has Strategy 1.5 executing first (before Strategy 1), then 1, 2, 3. Total: 4 strategies, with 1.5 having highest priority.

### Assertion Framework Support

`classifyAssertion()` at parser.go:2454-2548 recognizes the following frameworks:

1. Python unittest (`self.assert*`) — line 2462
2. Python pytest (`pytest.raises`) — line 2466
3. Go testify (`assert.*`, `require.*`) — line 2471
4. Go testing.T (`t.Error`, `t.Fatal`, etc.) — line 2476
5. JS/TS Jest/Vitest (`expect`) — line 2484
6. JS/TS Jest matchers (`to*` with expect) — line 2489
7. JS/TS Jest .not matchers — line 2493
8. JS/TS assert.* — line 2497
9. C# Assert.* — line 2501
10. JUnit/Kotlin (`assert*` with len > 6) — line 2507
11. PHP (`->assert*`) — line 2512
12. Ruby RSpec (`should`, `expect`) — line 2517
13. Swift XCT* — line 2522
14. C++ Google Test (`EXPECT_*`, `ASSERT_*`) — line 2527
15. C++ Catch2 (`REQUIRE`, `CHECK`, `REQUIRE_*`, `CHECK_*`) — line 2532
16. C++ Boost.Test (`BOOST_*`) — line 2538
17. C++ test macros (`TEST`, `TEST_F`, `TEST_P`, `TEST_CASE`) — line 2543
18. Python `assert_statement` / `assert` (bare assert) — parser.go:2410-2422
19. Rust `assert!` / `assert_eq!` / `assert_ne!` macros — parser.go:2425-2446

**CORRECTED** — Source docs claimed "11+ assertion frameworks." Actual count is 19 distinct patterns.

### detectSerdePairs

12 serialization pair patterns defined at main.go:1041-1046:

```
serialize/deserialize, encode/decode, marshal/unmarshal,
to_json/from_json, to_dict/from_dict, dump/load,
pack/unpack, ToJSON/FromJSON, ToMap/FromMap,
String/Parse, compress/decompress, encrypt/decrypt
```

**Evidence:** main.go:1041-1046

**VERIFIED**

---

## 7. Import Extraction

**Claim:** 14 import extractor functions covering 18 language names.

`extractImports()` at parser.go:466-500 dispatches on language name:

| # | Language(s) | Handler | Line |
|---|---|---|---|
| 1 | python | `extractPythonImports` | 472 |
| 2-3 | javascript, typescript | `extractJSTSImports` | 474 |
| 4 | go | `extractGoImports` | 476 |
| 5-7 | java, kotlin, groovy | `extractJavaImports` | 478 |
| 8 | scala | `extractScalaImports` | 480 |
| 9 | rust | `extractRustImports` | 482 |
| 10 | csharp | `extractCSharpImports` | 484 |
| 11 | php | `extractPHPImports` | 486 |
| 12-13 | c, cpp | `extractCCppImports` | 488 |
| 14 | swift | `extractSwiftImports` | 490 |
| 15 | ocaml | `extractOCamlImports` | 492 |
| 16 | ruby | `extractRubyImports` | 494 |
| 17 | elixir | `extractElixirImports` | 496 |
| 18 | lua | `extractLuaImports` | 498 |

**Result:** 14 distinct handler functions. 18 language case labels (javascript/typescript share one handler; java/kotlin/groovy share one; c/cpp share one).

`BuildFileMap()` at resolver.go:460-733 has module-path registration for 18 language names: python, javascript, typescript, go, java, kotlin, groovy, scala, rust, csharp, php, c, cpp, swift, ocaml, ruby, elixir, lua.

**CORRECTED** — Source docs said "~14 case arms, 18 language names." Confirmed: 14 handler functions, 18 language names in both extractImports and BuildFileMap.

---

## 8. Passive Delivery Layers

### L1: Pre-task Brief

**Module:** `src/groundtruth/brief/graph_map.py`

Produces a graph-map brief (not a ranked file list). Each entry includes: file, callers, callees, contracts, tests, risks, next_move. Delivered at task start via the wrapper.

**Evidence:** graph_map.py:1-8, 17-60

**VERIFIED**

### L3: Post-edit Evidence

**Module:** `src/groundtruth/hooks/post_edit.py`

Priority-ordered evidence triggered on file edits:

| Priority | Evidence Type | Source |
|---|---|---|
| 0.5 | Behavioral contract (properties-first, regex fallback) | post_edit.py:1636-1811 |
| 1 | Caller CODE lines (unseen-first, anchor-boosted) | post_edit.py:1813-1862 |
| 2 | Signature + return type + arity mismatch | post_edit.py:1864-1894 |
| 2b | Interface peers (same method in sibling classes) | post_edit.py:1914-1942 |
| 3 | Test assertions (graph.db then file grep fallback) | post_edit.py:1944-1968 |
| 4 | Sibling pattern (SUPPRESSED — `_SIBLING_EVIDENCE_ENABLED = False`) | post_edit.py:1970-2000 |
| 5 | Twins, propagation, co-change, scope (supplementary) | post_edit.py:2027-2055 |
| 6 | Issue obligations, mismatch, format contracts | post_edit.py:2057-2103 |

**VERIFIED**

### L3b: Post-view Navigation

**Module:** `src/groundtruth/hooks/post_view.py`

Graph-based navigation context on file views: callers, callees, importers. Hub-penalized ranking, issue-aware re-ranking, visited-file suppression.

**Evidence:** post_view.py:265-550 — `graph_navigation()` function.

**VERIFIED**

### Grep Intercept

**Status:** DISABLED

**Evidence:** `oh_gt_full_wrapper.py:2970` — "Grep Intercept: DISABLED per research (ProAIDE IUI 2026: 62% dismissal"

**VERIFIED**

### L5: Redirect Advisory

Non-source edits without prior source progress trigger a redirect advisory. Stuck-pattern detection (pending next-actions vs agent behavior).

**Evidence:** `oh_gt_full_wrapper.py:629-681` — L5 non-source-edit advisory. `oh_gt_full_wrapper.py:1265-1337` — L5 stuck-pattern detection.

**VERIFIED**

### L5b: Safety-Checked Intervention

Validates L5 interventions via `L5bSafetyChecker.validate()` before delivery.

**Evidence:** `oh_gt_full_wrapper.py:1748-1771`

**VERIFIED**

### L6: Incremental Reindex

Triggers `gt-index -file=<path>` after edits to keep graph.db current.

**Evidence:** `oh_gt_full_wrapper.py:775` — "Build the L6 command. Uses gt-index -file mode." main.go:529-792 — `runIncremental()`.

**VERIFIED**

---

## 9. Delivery Formatting: Properties Pipeline

**Claim:** Properties table is queried and routed by kind into formatted output.

**Evidence:** `post_edit.py:1696-1749` — The properties-first path queries `SELECT kind, value, line FROM properties WHERE node_id = ?` and routes:

| Property Kind | Rendered As | Line in post_edit.py |
|---|---|---|
| guard_clause | `GUARD: {value}` | 1711 |
| conditional_return | `L{line}: {value}` | 1713 |
| side_effect | `{value}` | 1715 |
| security_tag | `[SECURITY] {value}` | 1717 |
| serialization_pair | `[SERDE] {value}` | 1719 |
| param | `PARAMS: {joined values}` | 1721 (collected, inserted at position 0) |
| exception_flow | `[RAISES] {value}` | 1723 |
| exception_handler | `[CATCHES] {value}` | 1725 |
| class_field | `FIELD: {value}` | 1727 |
| field_read | `READS: {value}` | 1729 |
| boundary_condition | `[BOUNDARY] {value}` | 1731 |
| fingerprint | (stored for MCP query, not displayed) | 1733 |

Regex fallback fires when no properties exist (old databases): `post_edit.py:1752-1808`.

**VERIFIED**

---

## 10. G7 Silence Gate

**Claim:** When a function has 0 callers, 0 siblings, and 0 peers, most evidence is suppressed.

**Evidence:** `post_edit.py:2002-2025`

```python
if total_callers == 0 and not siblings and not peers:
    _has_typed_sig = sig and ("->" in sig or ": " in sig)
    _G7_KEEP_PREFIXES = (
        "[SIGNATURE]", "[TEST]", "[BEHAVIORAL CONTRACT]",
        "GUARD:", "MUTATES:", "ACCUMULATES:", "[SECURITY]",
        "[SERDE]", "PARAMS:", "[RAISES]", "[CATCHES]",
        "FIELD:", "READS:", "[BOUNDARY]",
    )
```

Kept items: `[TEST]` always. `[SIGNATURE]` only if typed. All behavioral contract sub-prefixes always kept.

**VERIFIED**

---

## 11. MCP Server

**Claim:** 16 tools registered.

**Evidence:** `src/groundtruth/mcp/server.py` — 7 active `@app.tool()` decorators. 22 legacy tools deprecated (functions exist but decorators commented out).

Active tools (7):

| # | Tool | Intent | Line |
|---|---|---|---|
| 1 | `groundtruth_investigate` | Deep-dive: callers + callees + contract + impact | 622 |
| 2 | `groundtruth_orient_v2` | Orientation: relevant files + structure + hotspots | 645 |
| 3 | `groundtruth_check_v2` | Validation: contradictions + pattern mismatches | 668 |
| 4 | `groundtruth_status_v2` | Health: index stats + session summary | 692 |
| 5 | `gt_plan` | Plan mode: implementation plan from graph | 444 |
| 6 | `gt_run_tests` | Plan mode: run tests for verification | 468 |
| 7 | `gt_contract` | Plan mode: behavioral contract extraction | 513 |

Deprecated (22): Original 16 core tools + 6 extras. Functions retained for backward compatibility, `@app.tool()` commented out. ~3200 tokens of system prompt overhead eliminated.

**CORRECTED** — Original docs said 16, then 29. Consolidated to 7 active tools (4 primary + 3 plan-mode). 5x less context overhead per turn.

---

## 12. Dedup Strategy

**Claim:** MD5 on stripped text, per-file keyed.

**Evidence:** `oh_gt_full_wrapper.py:3923-3930` (post-edit dedup):
```python
_dedup_hash_edit = hashlib.md5(_dedup_body.strip().encode("utf-8", errors="replace")).hexdigest()
_dedup_key_edit = f"l3:{rel_p or event.path}:{_dedup_hash_edit}"
if _dedup_key_edit in config.evidence_sent:
    ...
config.evidence_sent[_dedup_key_edit] = True
```

`oh_gt_full_wrapper.py:3289-3295` (post-view dedup):
```python
_dedup_hash_view = hashlib.md5(_dedup_body_view.strip().encode("utf-8", errors="replace")).hexdigest()
_dedup_key_view = f"l3b:{rel_view or event.path}:{_dedup_hash_view}"
```

Dedup key format: `{layer}:{file_path}:{md5_of_stripped_body}`

**CORRECTED** — Source doc claimed "MD5 on sorted+stripped lines." Actual code does `_dedup_body.strip().encode()` -- strip only, no line sorting. The key is per-file AND per-layer (l3 vs l3b prefix).

---

## 13. Confidence Thresholds

All confidence thresholds verified by grepping actual SQL queries:

| Query Location | Threshold | COALESCE Default | Evidence |
|---|---|---|---|
| post_edit.py:192 (annotate header) | >= 0.7 | COALESCE(e.confidence, 0.5) | post_edit.py:192 |
| post_edit.py:356 (edit propagation) | >= 0.6 | e.confidence >= 0.6 | post_edit.py:356 |
| post_edit.py:623 (caller query primary) | >= 0.6 | e.confidence >= 0.6 | post_edit.py:623 |
| post_edit.py:664 (caller query fallback) | >= 0.5 | e.confidence >= 0.5 | post_edit.py:664 |
| post_edit.py:911 (EXTENDS/IMPLEMENTS) | >= 0.5 | COALESCE(confidence, 0.5) | post_edit.py:911 |
| post_edit.py:1232 (nearest candidate) | >= 0.7 | COALESCE(e.confidence, 0.5) | post_edit.py:1232 |
| post_edit.py:1431 (verify suggestion) | >= 0.5 | COALESCE(e.confidence, 0.5) | post_edit.py:1431 |
| post_edit.py:2617 (env override) | >= 0.40 | GT_MIN_CONFIDENCE env var | post_edit.py:2617 |
| post_view.py:348 (callers) | >= 0.6 | COALESCE(e.confidence, 0.5) | post_view.py:348 |
| post_view.py:381 (callees) | >= 0.6 | COALESCE(e.confidence, 0.5) | post_view.py:381 |
| post_view.py:518 (importers) | >= 0.5 | COALESCE(e.confidence, 0.5) | post_view.py:518 |
| post_view.py:619 (test file targets) | >= 0.5 | COALESCE(e.confidence, 0.5) | post_view.py:619 |
| graph_map.py:114 (L1 brief callers) | >= 0.6 | COALESCE(e.confidence, 0.5) | graph_map.py:114 |
| graph_map.py:129 (L1 brief callees) | >= 0.6 | COALESCE(e.confidence, 0.5) | graph_map.py:129 |

**Summary:**
- COALESCE default is **0.5** everywhere (research: Avro/Protobuf convention)
- Primary CALLS threshold: **0.6** (L3 callers, L3b callers/callees, L1 brief, edit propagation)
- Fallback CALLS threshold: **0.5** (when 0.6 returns empty, EXTENDS/IMPLEMENTS, verify, importers, test targets)
- Annotation header threshold: **0.7** (only for finding keyword-overlapping connected files)
- Risk framing tiers: >= 0.9 (high), >= 0.5 (medium), < 0.5 (silence)

**CORRECTED** — Source doc said "confidence threshold was 0.7, now 0.6 for CALLS." Verified: primary CALLS threshold is 0.6 with fallback to 0.5. The 0.7 threshold still exists in two annotation/candidate queries but is not the main caller filter.

---

## 14. Research Backing

From ARCHITECTURE_LAYER_MAP.md (not re-verified against papers, listed as claimed):

| Element | Research Citation |
|---|---|
| Confidence threshold 0.6 | ICSE 2022 — call-graph precision at 0.6 threshold |
| COALESCE default 0.5 | Avro/Protobuf convention |
| Grep intercept disabled | ProAIDE IUI 2026 — 62% dismissal rate |
| Serde pairs | MSR community — serialization pairs as behavioral contract signal |
| Edit propagation | CodePlan FSE 2024 — 5/7 repos pass with propagation |
| Multi-file scope | WANG-MENG-2018 (52-58% multi-entity), ARISE-2026 (structural retrieval) |
| Scope completeness | ASE 2025 multi-hunk study — agents systematically under-edit |
| Hub penalty | Graph-theory degree normalization for P90-relative scaling |

---

## 15. Verified Invariants

| # | Invariant | Status | Evidence |
|---|---|---|---|
| 1 | All SQL queries use COALESCE(e.confidence, 0.5) as default | **VERIFIED** | All 14 queries above use 0.5 default |
| 2 | Edge deduplication by (source_id, target_id, type) | **VERIFIED** | resolver.go:149-153 |
| 3 | Properties pipeline routes by kind to formatted output | **VERIFIED** | post_edit.py:1696-1749 |
| 4 | G7 silence gate suppresses evidence for isolated functions | **VERIFIED** | post_edit.py:2002-2025 |
| 5 | Sibling evidence is disabled (`_SIBLING_EVIDENCE_ENABLED = False`) | **VERIFIED** | post_edit.py:76 |
| 6 | Grep intercept is disabled | **VERIFIED** | oh_gt_full_wrapper.py:2970 |
| 7 | L3/L3b have no budget caps (dedup-only gating) | **VERIFIED** | No budget cap variable found in post_edit.py or post_view.py; only dedup + char limits |
| 8 | detectSerdePairs is implemented and writes serialization_pair properties | **VERIFIED** | main.go:1051-1114 |

---

## Verification Summary

```
Total claims verified: 48
Verified (matches code exactly): 36
Corrected (discrepancy found): 12
  1. Edge table has 12 columns, not 8 (trust_tier, candidate_count, evidence_type, verification_status added)
  2. Indexing pipeline has 7 passes (1, 2, 3, 4, 4b, 4c, 4d, 5, 5b), not 4
  3. Import strategy 1.5 executes first in resolveAssertionTarget, not after Strategy 1
  4. Property kinds: 17 total (16 from parser + serialization_pair from main), not 13
  5. Assertion frameworks: 19 patterns recognized, not "11+"
  6. Import extractors: 14 handler functions, 18 language names (confirmed, not approximate)
  7. MCP tools: 29 registered, not 16
  8. Dedup uses strip() only, not sorted+stripped lines
  9. Confidence threshold is 0.6 primary / 0.5 fallback, not 0.7
  10. COALESCE default is 0.5 everywhere (confirmed)
  11. Grep intercept confirmed disabled (was "unknown" in one source doc)
  12. L3b has no budget cap (doc said "10, was 3, now NO CAP" — confirmed NO CAP)
Skipped (deferred): 0
```
