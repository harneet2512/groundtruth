# General Graph Creation Design

Date: 2026-05-16
Status: Design (not implemented)
Scope: Repo-agnostic, agent-agnostic, IDE-agnostic, model-agnostic evidence system

---

## 1. Why Current Graph Creation Is Not General Enough

### What exists today

The gt-index Go binary produces a single-table relationship graph:
- **1 edge type:** CALLS (no imports, inheritance, test-to-source, containment, or runtime edges)
- **3 builders:** same_file (exact match in same file), import (import-verified cross-file), name_match (fallback by name)
- **0% qualified_name coverage:** disambiguating nodes is impossible post-hoc
- **0% metadata/evidence coverage:** no proof is stored with edges
- **No verification status:** edges are either stored or not — no lifecycle state
- **No consumer restrictions:** any downstream system can consume any edge regardless of trust

### Measured quality on real repos

| Metric | dagster (Python, 46K nodes) | hono (TS, 2.5K) | axum (Rust, 2.7K) | crossplane (Go, 4.3K) |
|--------|---------------------------|----------------|------------------|---------------------|
| Certified edges (conf >= 0.9) | 64.4% | 61% | — | — |
| Speculative edges (conf < 0.5) | 26.8% | 32% | 14% | 18% |
| Import resolution coverage | 24% | 2% | 0.4% | 2% |
| name_match fallback rate | 60% | 87% | 75% | 82% |
| Same-package rate at conf=0.2 | 49% (random) | — | — | — |
| Same-package rate at conf=0.9 | 89% | — | — | — |

### The core deficiency

An edge in the current graph answers only:
1. "Function A calls function B" (claim)
2. Confidence = 0.2-1.0 (a number)

It does NOT answer:
- What concrete evidence supports this? (no evidence payload)
- Can I inspect the proof? (no code snippet, no LSP verification)
- Is this relationship still valid? (no staleness tracking)
- Who is allowed to trust this? (no consumer safety rules)
- Was this ever verified by something other than heuristic? (no verification lifecycle)

A graph edge without inspectable evidence is an assertion, not a fact.

---

## 2. General Edge-Quality Principles

### Principle 1: Evidence-First

Every edge must carry evidence of why it exists. An edge without evidence is a candidate, never an operational fact.

Evidence types (ordered by strength):
1. **Verified:** LSP textDocument/references confirmed the relationship
2. **Structural:** AST parse proves syntactic relationship (import statement, same-file call)
3. **Inferred:** Heuristic reasoning with supporting data (single candidate for name, directory proximity)
4. **Speculative:** Name match with multiple candidates, no disambiguating signal

### Principle 2: Trust Tiers Gate Consumption

Not all edges are equal. Trust tiers define what operations are safe:

| Tier | Evidence Required | Allowed Operations |
|------|------------------|--------------------|
| **CERTIFIED** | Verified OR structural with unique resolution | Ranking, navigation, contracts, intervention, any |
| **CANDIDATE** | Inferred with disambiguation signal | Navigation suggestions, caller listing (with caveat) |
| **SPECULATIVE** | Name match with ambiguity | Index search, bulk statistics, candidate pool ONLY |
| **SUPPRESSED** | Evidence contradicts claim (LSP rejected, dead code, stale) | None — tombstoned |

### Principle 3: Lifecycle Not Binary

Edges are not "exists or doesn't exist." They move through states:
- **DISCOVERED** → evidence attached → **EVIDENCED** → verified → **CERTIFIED**
- **DISCOVERED** → evidence insufficient → **SPECULATIVE** → consumer-blocked
- **CERTIFIED** → repo changed, evidence stale → **STALE** → reverified or demoted

### Principle 4: Portability

The graph must work for:
- Any language with a tree-sitter grammar (30+ languages)
- Any repository size (10 files to 100K files)
- Any consuming tool (MCP server, CLI, IDE extension, CI check, benchmark harness)
- Without benchmark metadata, task IDs, gold files, or eval labels

### Principle 5: Auditability

Any edge must answer: "Why do you exist? Show me the proof." If it cannot, it is not operational.

---

## 3. Edge Taxonomy

### Structural Edges (produced by AST/static analysis)

| Edge Type | Relationship | Evidence | Builder |
|-----------|-------------|----------|---------|
| FILE_IMPORTS_FILE | File A imports from file B | Import statement in AST | Import builder |
| SYMBOL_IMPORTS_SYMBOL | Symbol X is imported as Y | Specific import name in AST | Import builder |
| CALLS_SAME_FILE | Function A calls function B (same file) | Call expression + definition in same file | Static call builder |
| CALLS_CROSS_FILE | Function A calls function B (different file) | Call expression + import tracing OR LSP | Static call builder + verifier |
| CLASS_INHERITS | Class A extends class B | Inheritance clause in AST | Type builder |
| METHOD_OVERRIDES | Method in subclass overrides base | Name match + inheritance chain | Type builder |
| FILE_DEFINES_SYMBOL | File contains definition of symbol | AST parse of definition | Symbol definition builder |
| SYMBOL_REFERENCES_SYMBOL | Symbol X references symbol Y (type annotation, default value) | AST parse | Symbol definition builder |

### Behavioral Edges (prove what code DOES, not just structure)

| Edge Type | Relationship | Evidence | Builder |
|-----------|-------------|----------|---------|
| TEST_ASSERTS_SYMBOL | Test function tests behavior of symbol | Test calls symbol + has assertion | Test/contract builder |
| CALLER_EXPECTS_RETURN | Caller uses return value in specific way | Caller code at call site | Caller analysis builder |
| CALLER_PASSES_PATTERN | Caller passes arguments matching a pattern | Caller code at call site | Caller analysis builder |
| FIXTURE_INITIALIZES | Fixture creates object consumed by test | Fixture analysis | Test/contract builder |
| PARAMETER_INFLUENCES_BRANCH | Parameter value determines control flow path | Static analysis or coverage | Coverage builder |

### Dynamic Edges (from runtime observation)

| Edge Type | Relationship | Evidence | Builder |
|-----------|-------------|----------|---------|
| RUNTIME_CALL_OBSERVED | A actually called B at runtime | Trace log, profiler output | Runtime trace builder |
| COVERAGE_LINE_EXECUTED | Test T executes line L in file F | Coverage report | Coverage builder |
| FAILING_TEST_REACHES | Failing test executes path through symbol | Coverage + test result | Coverage builder |
| IMPORT_RESOLVED_RUNTIME | Dynamic import resolves to specific module | Runtime trace | Runtime trace builder |

### Candidate Edges (not yet evidenced enough for operational use)

| Edge Type | Relationship | Evidence | Builder |
|-----------|-------------|----------|---------|
| NAME_MATCH_AMBIGUOUS | A might call B (same name, multiple candidates) | Name equality + candidate count | Static call builder (fallback) |
| SEMANTIC_SIMILAR | A and B are textually/semantically related | Embedding distance, BM25 | Semantic candidate builder |
| CO_CHANGED | A and B changed together in git | Git log analysis | Co-change builder |

---

## 4. Evidence Model

Every edge carries an evidence payload. The payload must be sufficient for a human or verifier to inspect and confirm the relationship.

### Evidence Structure

```
evidence {
    type: "ast_import" | "ast_call" | "lsp_verified" | "name_match" | "coverage" | "runtime" | "co_change" | "semantic"
    
    # What was found
    source_code_snippet: str | null       # 1-3 lines at the source location
    target_code_snippet: str | null       # 1-3 lines at the target location
    import_statement: str | null          # The actual import line (for import edges)
    call_expression: str | null           # The actual call expression (for call edges)
    assertion_text: str | null            # The assertion line (for test edges)
    
    # How it was found
    builder: str                          # Which builder produced this
    resolution_path: str[]                # Steps taken to resolve (e.g., ["import_lookup", "file_map_resolve"])
    candidate_count: int                  # How many candidates existed (1 = unambiguous)
    disambiguation_signal: str | null     # What distinguished this target from others
    
    # How trustworthy
    confidence: float                     # 0.0-1.0 (computed, not hand-set)
    confidence_basis: str                 # Why this confidence value (e.g., "single_candidate", "lsp_confirmed")
    false_positive_risk: "none" | "low" | "medium" | "high"
    
    # Verification
    verifier: str | null                  # "pyright" | "gopls" | "rust-analyzer" | null
    verification_status: "unverified" | "verified" | "rejected" | "timeout"
    verification_date: timestamp | null
}
```

### Evidence Sufficiency Rules

An edge is evidence-sufficient when:

| Trust Tier | Minimum Evidence |
|-----------|-----------------|
| CERTIFIED | (verifier=verified) OR (type=ast_import AND candidate_count=1) OR (type=ast_call AND same_file) |
| CANDIDATE | candidate_count <= 2 AND (disambiguation_signal is not null OR same_package=true) |
| SPECULATIVE | candidate_count > 2 OR no disambiguation signal |
| SUPPRESSED | verification_status=rejected OR evidence contradicts claim |

---

## 5. Trust/Tier Model

### Tier Assignment Algorithm

```
function assign_tier(edge, evidence):
    if evidence.verification_status == "rejected":
        return SUPPRESSED
    
    if evidence.verification_status == "verified":
        return CERTIFIED
    
    if evidence.type in ("ast_import", "ast_call") and edge is same_file:
        return CERTIFIED
    
    if evidence.type == "ast_import" and evidence.candidate_count == 1:
        return CERTIFIED
    
    if evidence.type == "name_match" and evidence.candidate_count == 1:
        return CERTIFIED  # unique name in repo
    
    if evidence.candidate_count == 2 and evidence.disambiguation_signal:
        return CANDIDATE
    
    if evidence.candidate_count <= 5 and evidence.same_package:
        return CANDIDATE
    
    return SPECULATIVE
```

### Tier Properties

| Property | CERTIFIED | CANDIDATE | SPECULATIVE | SUPPRESSED |
|----------|-----------|-----------|-------------|------------|
| Confidence range | 0.9-1.0 | 0.5-0.89 | 0.0-0.49 | N/A |
| Can rank files | YES | NO | NO | NO |
| Can suggest navigation | YES | YES (with caveat marker) | NO | NO |
| Can claim "X calls Y" | YES | NO (say "may call") | NO | NO |
| Can trigger intervention | YES | NO | NO | NO |
| Can populate contracts | YES | NO | NO | NO |
| Visible in graph queries | YES | YES (filtered by consumer) | YES (statistics only) | NO |
| Stored in DB | YES | YES | YES | YES (tombstone) |
| Exposed to agents | YES | OPTIONAL (consumer decides) | NEVER | NEVER |

---

## 6. Consumer Safety Model

### Consumer Categories

| Consumer | What It Does | Minimum Trust Required | Rationale |
|----------|-------------|----------------------|-----------|
| **File ranker** | Orders files by relevance | CERTIFIED | Noise edges inflate irrelevant files |
| **Caller extraction** | Shows "who calls this function" | CERTIFIED | Wrong callers mislead agent fixes |
| **Navigation suggestion** | Tells agent "read file X next" | CANDIDATE (marked) | Noise destinations waste agent budget |
| **Witness/intervention** | Claims agent ignored known caller | CERTIFIED | False witnesses erode agent trust |
| **Contract explanation** | Shows behavioral expectations | CERTIFIED + snippet | Without code, it's a pointer not a contract |
| **Test targeting** | Suggests specific test to run | CERTIFIED + test_asserts | Wrong test = wasted verification loop |
| **Hotspot analysis** | Counts references to find hubs | CANDIDATE | Noise inflates documentation/utils |
| **Dead code detection** | Finds unreferenced exports | CERTIFIED only | Noise hides real dead code |
| **Bulk statistics** | Counts edges per type/file | ANY | Noise acceptable for overview metrics |
| **Graph visualization** | Renders graph structure | ANY (but tier-colored) | User can visually filter |

### Consumer Registration

Each consumer declares its minimum trust tier. The graph query layer ENFORCES the minimum:

```python
def query_callers(node_id, *, consumer: Consumer) -> list[Edge]:
    min_tier = consumer.minimum_trust_tier
    return db.execute("""
        SELECT * FROM edges 
        WHERE target_id = ? AND trust_tier >= ?
    """, (node_id, min_tier.value))
```

A consumer cannot bypass this by passing a lower tier. The graph layer is the authority.

---

## 7. Graph Schema

### Proposed Node Schema

```sql
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- Identity
    kind TEXT NOT NULL,              -- 'file' | 'function' | 'method' | 'class' | 'module' | 'test' | 'assertion'
    name TEXT NOT NULL,              -- Short name (e.g., "get_user")
    qualified_name TEXT,             -- Full path (e.g., "myapp.auth.get_user")
    
    -- Location
    file_path TEXT NOT NULL,         -- Relative to repo root
    start_line INTEGER,
    end_line INTEGER,
    
    -- Type info
    signature TEXT,                  -- Function signature string
    return_type TEXT,
    
    -- Classification
    language TEXT NOT NULL,
    is_exported BOOLEAN DEFAULT 0,
    is_test BOOLEAN DEFAULT 0,
    is_generated BOOLEAN DEFAULT 0,
    
    -- Containment
    parent_id INTEGER REFERENCES nodes(id),  -- Class → method, file → function
    module_path TEXT,               -- Python: "myapp.auth", Go: "github.com/org/repo/auth"
    
    -- Provenance
    repo_root_hash TEXT,            -- SHA256 of repo root path (for multi-repo disambiguation)
    indexed_at TEXT,                -- ISO timestamp
    commit_sha TEXT                 -- Git commit at index time
);
```

### Proposed Edge Schema

```sql
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- Relationship
    source_id INTEGER NOT NULL REFERENCES nodes(id),
    target_id INTEGER NOT NULL REFERENCES nodes(id),
    edge_type TEXT NOT NULL,         -- From taxonomy: 'CALLS_SAME_FILE', 'CALLS_CROSS_FILE', 'FILE_IMPORTS_FILE', etc.
    edge_subtype TEXT,               -- Optional refinement: 'direct_call', 'method_call', 'constructor_call'
    
    -- Location evidence
    source_file TEXT,
    source_line INTEGER,
    target_file TEXT,
    target_line INTEGER,
    
    -- Trust
    trust_tier TEXT NOT NULL DEFAULT 'SPECULATIVE',  -- 'CERTIFIED' | 'CANDIDATE' | 'SPECULATIVE' | 'SUPPRESSED'
    confidence REAL NOT NULL DEFAULT 0.0,             -- 0.0-1.0
    false_positive_risk TEXT DEFAULT 'unknown',       -- 'none' | 'low' | 'medium' | 'high'
    
    -- Evidence
    builder TEXT NOT NULL,           -- Which builder produced this: 'static_same_file', 'import_resolver', 'name_match', 'lsp_verifier'
    evidence_type TEXT,              -- 'ast_import', 'ast_call', 'lsp_verified', 'name_match', 'coverage', 'runtime'
    evidence_payload TEXT,           -- JSON: source_snippet, call_expression, import_statement, assertion, etc.
    
    -- Resolution details
    resolution_method TEXT,          -- Legacy compat: 'same_file', 'import', 'name_match'
    candidate_count INTEGER DEFAULT 1,
    disambiguation_signal TEXT,      -- What broke the tie: 'unique_name', 'import_path', 'same_directory', 'lsp'
    
    -- Verification
    verifier TEXT,                   -- 'pyright', 'gopls', 'rust-analyzer', null
    verification_status TEXT DEFAULT 'unverified',  -- 'unverified', 'verified', 'rejected', 'timeout'
    verified_at TEXT,                -- ISO timestamp
    
    -- Lifecycle
    created_at TEXT NOT NULL,        -- ISO timestamp
    stale_after TEXT,                -- When this edge should be re-verified (commit SHA or timestamp)
    
    -- Consumer control
    allowed_consumers TEXT,          -- JSON array of consumer categories, or null = all (tier-gated)
    
    -- Language and repo
    language TEXT,
    repo_root_hash TEXT
);

-- Indexes for efficient tier-gated queries
CREATE INDEX idx_edges_trust_tier ON edges(trust_tier);
CREATE INDEX idx_edges_target_tier ON edges(target_id, trust_tier);
CREATE INDEX idx_edges_source_tier ON edges(source_id, trust_tier);
CREATE INDEX idx_edges_type_tier ON edges(edge_type, trust_tier);
CREATE INDEX idx_edges_verification ON edges(verification_status);
CREATE INDEX idx_edges_builder ON edges(builder);
```

### Migration from Current Schema

The current schema maps directly:
- `resolution_method='same_file'` → `trust_tier='CERTIFIED'`, `builder='static_same_file'`
- `resolution_method='import'` → `trust_tier='CERTIFIED'`, `builder='import_resolver'`
- `resolution_method='name_match', confidence=0.9` → `trust_tier='CERTIFIED'`, `builder='name_match'`, `candidate_count=1`
- `resolution_method='name_match', confidence=0.6` → `trust_tier='CANDIDATE'`, `builder='name_match'`, `candidate_count=2`
- `resolution_method='name_match', confidence<=0.4` → `trust_tier='SPECULATIVE'`, `builder='name_match'`, `candidate_count>=3`

No data loss. The new fields (`evidence_payload`, `verification_status`, `allowed_consumers`) start as null and are populated incrementally by builders and verifiers.

---

## 8. Builder Architecture

### Builder 1: File/Module Builder

**Inputs:** File system walk, language detection
**Outputs:** File nodes, module nodes
**Edge types:** (none — this builder produces nodes only)
**Precision:** 1.0 (files exist or don't)
**Language requirements:** tree-sitter grammar for language detection
**Failure mode:** Symlinks, generated files, vendor directories

### Builder 2: Symbol Definition Builder

**Inputs:** AST parse via tree-sitter
**Outputs:** Function, Method, Class nodes
**Edge types:** FILE_DEFINES_SYMBOL (implicit via file_path)
**Precision:** 1.0 (AST is ground truth for definitions)
**Language requirements:** tree-sitter grammar + language spec (30 specs exist)
**Failure mode:** Dynamic definitions (exec, eval, metaprogramming)

### Builder 3: Import Builder

**Inputs:** AST parse of import statements, file map (module → files)
**Outputs:** FILE_IMPORTS_FILE, SYMBOL_IMPORTS_SYMBOL edges
**Edge types produced:** FILE_IMPORTS_FILE (confidence 1.0), SYMBOL_IMPORTS_SYMBOL (confidence 1.0)
**Precision:** 1.0 for languages with extractors; N/A for others
**Language requirements:** Language-specific import grammar (currently: Python, Go, JS, TS, Java, Rust)
**Failure mode:** Dynamic imports, conditional imports, re-exports, barrel files, import aliases
**Fallback:** None — if import can't be resolved, no edge is emitted (correct behavior)

### Builder 4: Static Call Builder

**Inputs:** AST call expressions, resolved import map, node definitions
**Outputs:** CALLS_SAME_FILE, CALLS_CROSS_FILE edges
**Precision:**
- same_file: 1.0 (name match within file = certain)
- import-verified cross-file: 1.0 (import traces to unique definition)
- name_match (1 candidate): 0.9
- name_match (2 candidates): 0.5-0.6
- name_match (3+ candidates): 0.2-0.4
**Language requirements:** tree-sitter grammar for call expressions
**Failure mode:** Method calls on untyped receivers, dynamic dispatch, overloaded names
**Fallback:** NAME_MATCH_AMBIGUOUS candidate edge (never CERTIFIED)

### Builder 5: Type/Inheritance Builder

**Inputs:** AST class definitions with base class clauses
**Outputs:** CLASS_INHERITS, METHOD_OVERRIDES edges
**Precision:** 1.0 for single-file inheritance; variable for cross-file
**Language requirements:** Tree-sitter grammar with inheritance query
**Failure mode:** Metaclasses, dynamic inheritance, mixins from different files
**Currently implemented:** NO (not in gt-index today)

### Builder 6: Test/Contract Builder

**Inputs:** Nodes where is_test=1, their call edges, assertion patterns
**Outputs:** TEST_ASSERTS_SYMBOL edges
**Precision:** Depends on whether test→source connection is import-verified
**Language requirements:** Test framework patterns (pytest, unittest, go test, jest, etc.)
**Failure mode:** Indirect testing via helpers, fixtures, parameterized tests
**Currently implemented:** NO (is_test flag exists on nodes, but no dedicated test edges)

### Builder 7: LSP Verifier (Promotion Builder)

**Inputs:** Candidate/speculative edges, running LSP server
**Outputs:** Promotes edges from CANDIDATE/SPECULATIVE → CERTIFIED or SUPPRESSED
**Edge types produced:** None (modifies existing edges)
**Precision:** 1.0 (LSP is ground truth for "does reference exist?")
**Language requirements:** LSP server available (pyright, gopls, rust-analyzer, tsserver, etc.)
**Failure mode:** LSP server unavailable, timeout, workspace not configured, missing type stubs
**Fallback:** Edge retains original tier (no change)
**Cost:** 50-500ms per verification; batch-able
**Currently implemented:** YES (LazyEdgeVerifier in src/groundtruth/lsp/edge_verifier.py) — but only called at suggestion time, not at index time

### Builder 8: Directory Proximity Scorer (Disambiguation Builder)

**Inputs:** Ambiguous name_match candidates, source file path
**Outputs:** disambiguation_signal on existing edges, possible tier promotion
**Edge types produced:** None (modifies disambiguation_signal field)
**Precision:** Heuristic — same-directory = 89% likely correct (measured)
**Language requirements:** None (file path only)
**Failure mode:** Cross-package utility calls (common stdlib patterns)
**Currently implemented:** NO (stub exists at resolver.go:160-164 but never filled in)

### Builder 9: Edge Promoter (Lifecycle Manager)

**Inputs:** Edges in CANDIDATE/SPECULATIVE tier, promotion criteria
**Outputs:** Tier changes, staleness checks
**Criteria for promotion:**
- SPECULATIVE → CANDIDATE: directory proximity confirms OR only 2 candidates
- CANDIDATE → CERTIFIED: LSP verifies OR consumer confirms usage
- ANY → SUPPRESSED: LSP rejects OR stale_after exceeded
- ANY → STALE: commit_sha no longer matches HEAD

### Builder 10: Edge Sampler/Auditor

**Inputs:** Complete edge set, sampling parameters
**Outputs:** Precision estimate per edge type, false positive examples
**Method:**
1. Stratified sample by (edge_type, trust_tier, language)
2. For each sampled edge: attempt LSP verification
3. Record: confirmed_real / confirmed_false / unknown
4. Compute precision = confirmed_real / (confirmed_real + confirmed_false)
5. Report false positive examples for human inspection

**This builder is diagnostic, not operational.** It measures graph quality but doesn't change edges. Run it on any repo to answer "how good is this graph?"

---

## 9. Edge Lifecycle

```
                            ┌─────────────────┐
                            │   DISCOVERED    │
                            │  (call ref in   │
                            │   AST parse)    │
                            └────────┬────────┘
                                     │
                          ┌──────────▼──────────┐
                          │  EVIDENCE ATTACHED  │
                          │  (builder assigns   │
                          │   type + evidence)  │
                          └──────────┬──────────┘
                                     │
                    ┌────────────────┬┴────────────────┐
                    │                │                  │
          ┌─────── ▼──────┐  ┌──────▼──────┐  ┌──────▼───────┐
          │  CERTIFIED    │  │  CANDIDATE  │  │ SPECULATIVE  │
          │ (verified OR  │  │ (2 cands +  │  │ (3+ cands,   │
          │  structural   │  │  disambig)  │  │  no signal)  │
          │  unique)      │  │             │  │              │
          └───────┬───────┘  └──────┬──────┘  └──────┬───────┘
                  │                  │                  │
                  │          ┌───────▼────────┐        │
                  │          │ PROMOTION      │        │
                  │          │ (LSP verify,   │        │
                  │          │  usage confirm)│        │
                  │          └───────┬────────┘        │
                  │                  │                  │
                  │                  ├─── YES ──→ CERTIFIED
                  │                  │
                  │                  └─── NO ──→ SUPPRESSED
                  │
          ┌───────▼───────────────────────────────────────────┐
          │                    STALE CHECK                      │
          │  (commit_sha != HEAD OR stale_after exceeded)       │
          └───────┬────────────────────────────────┬───────────┘
                  │                                │
          re-verify → CERTIFIED           demote → CANDIDATE/SPECULATIVE
```

### Lifecycle Rules

1. **No shortcut from SPECULATIVE to operational.** A speculative edge must be promoted to CANDIDATE (via disambiguation) or CERTIFIED (via verification) before any consumer can act on it.

2. **SUPPRESSED is permanent within an index run.** An LSP-rejected edge stays suppressed until the next full reindex (code may have changed).

3. **Staleness is commit-bound.** An edge verified at commit ABC is stale when HEAD moves past ABC and the source or target file has been modified.

4. **Promotion is idempotent.** Verifying an already-CERTIFIED edge is a no-op (but updates verified_at).

---

## 10. Metrics

### Repo-Level Graph Quality Metrics (computable on ANY indexed repo)

#### Edge Inventory

| Metric | Definition | Computation |
|--------|-----------|-------------|
| `total_edges` | Total edge count | `SELECT COUNT(*) FROM edges` |
| `edges_by_type` | Count per edge_type | `GROUP BY edge_type` |
| `edges_by_builder` | Count per builder | `GROUP BY builder` |
| `edges_by_language` | Count per language | `GROUP BY language` |
| `edges_by_trust_tier` | Count per tier | `GROUP BY trust_tier` |
| `certified_edge_ratio` | CERTIFIED / total | `WHERE trust_tier = 'CERTIFIED'` |
| `speculative_edge_ratio` | SPECULATIVE / total | `WHERE trust_tier = 'SPECULATIVE'` |
| `suppressed_edge_count` | Rejected edges | `WHERE trust_tier = 'SUPPRESSED'` |

#### Evidence Quality

| Metric | Definition | Healthy Range |
|--------|-----------|---------------|
| `evidence_backed_edge_ratio` | Edges with non-null evidence_payload / total | > 0.8 |
| `location_backed_edge_ratio` | Edges with source_line AND target_line / total | > 0.9 |
| `snippet_backed_edge_ratio` | Edges with code snippet in evidence / total | > 0.5 |
| `verifier_backed_edge_ratio` | Edges with verification_status != 'unverified' / total | > 0.1 (aspirational) |
| `speculative_edge_ratio` | SPECULATIVE / total | < 0.3 |
| `ambiguous_target_edge_ratio` | Edges with candidate_count > 1 / total | < 0.4 |
| `unresolved_symbol_edge_ratio` | Edges where target qualified_name is null / total | < 0.5 |

#### Precision Sampling

Run edge sampler on N=100 random edges per type:

| Metric | Definition |
|--------|-----------|
| `precision_by_type` | confirmed_real / (confirmed_real + confirmed_false) per edge_type |
| `precision_certified` | Precision among CERTIFIED tier (target: > 0.95) |
| `precision_candidate` | Precision among CANDIDATE tier (target: > 0.7) |
| `precision_speculative` | Precision among SPECULATIVE tier (report only, no target) |
| `false_positive_examples` | List of 5 concrete false positives with explanation |

#### Consumer Safety

| Metric | Definition | Threshold |
|--------|-----------|-----------|
| `speculative_edges_used_operationally` | SPECULATIVE edges consumed by tier-restricted consumers | MUST BE 0 |
| `ranker_noise_ratio` | SPECULATIVE edges accessible to file rankers | MUST BE 0 |
| `navigation_noise_ratio` | SPECULATIVE edges accessible to navigation | MUST BE 0 |
| `witness_noise_ratio` | SPECULATIVE edges used for intervention triggers | MUST BE 0 |

---

## 11. Acceptance Gates

### Primary Gates (must pass for any graph-creation change)

| Gate | Criterion | Rationale |
|------|-----------|-----------|
| G1 | `evidence_backed_edge_ratio` increases or stays constant | No edge should lose evidence |
| G2 | `speculative_edges_used_operationally` = 0 | Consumer safety enforced |
| G3 | `precision_certified` >= 0.95 | CERTIFIED tier must be trustworthy |
| G4 | `precision_candidate` >= 0.70 | CANDIDATE tier must be better than random |
| G5 | Every CERTIFIED edge has source_line OR verifier proof | Location or proof required |
| G6 | CANDIDATE edges are marked as such to consumers | No silent degradation |
| G7 | Edge audit explains every CERTIFIED edge | Auditability |
| G8 | Fresh-repo smoke passes (see below) | Portability |

### Fresh-Repo Smoke (G8 definition)

```
1. Pick any public repo not in the training set
2. Run gt-index on it
3. Compute all metrics from §10
4. Verify:
   a. certified_edge_ratio > 0.50
   b. evidence_backed_edge_ratio > 0.80
   c. speculative_edge_ratio < 0.35
   d. precision_certified >= 0.90 (sample 50 edges, LSP-verify)
   e. No crashes, no empty graph, no timeout > 5 min for repos < 10K files
5. No benchmark metadata, task IDs, or gold files used
```

### Secondary Benchmark Validation (only after G1-G8 pass)

After general quality gates pass, optionally validate on SWE-bench or similar:
- Run existing benchmark smoke
- Check no regressions in resolved count
- Report directional improvement in edge utilization

This is INFORMATIONAL, not gating. The graph must be good for intrinsic reasons, not because it helps a benchmark.

---

## 12. Fresh-Repo Validation Plan

### Target Repos (diverse, not in any benchmark)

| Repo | Language | Size | Why Chosen |
|------|----------|------|-----------|
| fastapi/fastapi | Python | ~10K LOC | Clean Python, good imports, tests |
| gin-gonic/gin | Go | ~15K LOC | Go with interfaces and middleware |
| tokio-rs/axum | Rust | ~20K LOC | Rust with traits and async |
| vercel/next.js | TypeScript | ~100K LOC | Large TS with barrel exports |
| spring-projects/spring-boot | Java | ~200K LOC | Java with inheritance hierarchies |

### Validation Protocol

For each repo:
1. Clone at latest release tag
2. Run gt-index with default settings
3. Compute all §10 metrics
4. Sample 50 CERTIFIED edges → LSP-verify → compute precision
5. Sample 50 SPECULATIVE edges → LSP-verify → compute false-positive rate
6. Record: certified_edge_ratio, import_coverage, precision_certified, speculative_edge_ratio
7. If precision_certified < 0.90: investigate and fix BEFORE any downstream work

### Success Criteria

The graph must achieve on ALL 5 repos:
- `certified_edge_ratio` > 0.50
- `precision_certified` > 0.90
- `import_coverage` > 0.20 for Tier 1 languages (Python, Go, JS, TS, Java, Rust)
- `speculative_edge_ratio` < 0.35
- Indexing completes in < 5 min for repos < 50K LOC

---

## 13. Benchmark Validation Plan (Secondary)

Only after fresh-repo validation passes:

1. Run 5-task smoke on a benchmark (SWE-bench-Live or equivalent)
2. Measure:
   - Resolved count (no regression from current baseline)
   - Edge utilization by tier (how many CERTIFIED edges were surfaced, how many followed)
   - No SPECULATIVE edge was operationally consumed
3. This is a regression check, not a quality target

---

## 14. Recommended 24-Hour Implementation Plan

### Hour 0-4: Schema Migration + Consumer Safety Layer

1. Add `trust_tier`, `evidence_payload`, `candidate_count`, `disambiguation_signal`, `verification_status`, `allowed_consumers` columns to edges table
2. Backfill trust_tier from existing confidence + resolution_method (pure computation, no data loss)
3. Add tier-gated query wrapper: `query_edges(target_id, min_tier=CERTIFIED)` that enforces consumer rules
4. Write migration script that upgrades existing graph.db files

### Hour 4-8: Builder Improvements

5. Implement directory-proximity disambiguation in resolver.go (stub exists at lines 160-164)
6. Populate `candidate_count` during resolution (data already computed, just not stored)
7. Store `evidence_payload` with import statement text for import-resolved edges
8. Store `evidence_payload` with call expression text for same-file call edges (read source at source_line)

### Hour 8-12: Fresh-Repo Smoke

9. Index fastapi/fastapi with updated gt-index
10. Compute all §10 metrics
11. Sample 50 CERTIFIED edges → manual verification (check if call really exists)
12. Fix any failures found

### Hour 12-16: LSP Verification Integration at Index Time

13. After name_match resolution, batch-verify top-100 ambiguous edges (candidate_count=2) via pyright/gopls
14. Promote verified → CERTIFIED, rejected → SUPPRESSED
15. Store verification_status and verified_at
16. Re-compute metrics after verification

### Hour 16-20: Edge Sampler/Auditor Tool

17. Build `gt-audit` CLI command: samples N edges per type, attempts LSP verification, reports precision
18. Run on fastapi, gin, and axum — validate precision targets
19. Fix any precision failures in builders

### Hour 20-24: Integration Test + Secondary Benchmark Smoke

20. Run full test suite (ensure no regressions)
21. Re-index a benchmark repo (dagster) with new schema
22. Run V1R brief generation — verify documentation files no longer rank above real code
23. Run 5-task benchmark smoke as regression check (not primary gate)

---

## 15. Edge Actionability Analysis

### The Question

Graph correctness and agent usefulness are orthogonal axes. A perfectly precise graph can be useless if it doesn't change agent decisions. Before investing in graph precision, we must ask: **for each edge type, does it actually change what the agent does?**

### Actionability Dimensions

For every edge type, we evaluate 7 decision-change dimensions:

| Dimension | Definition |
|-----------|-----------|
| **Reduces search entropy** | Does knowing this edge reduce the set of files the agent must consider? |
| **Changes first-read** | Does this edge cause the agent to READ a different file than it would have? |
| **Changes first-edit** | Does this edge cause the agent to EDIT a different file/function? |
| **Improves test selection** | Does this edge help the agent run the RIGHT test instead of broad/wrong tests? |
| **Improves verification quality** | Does this edge help the agent verify its fix more precisely? |
| **Prevents wrong-semantic fix** | Does this edge prevent the agent from writing code that satisfies syntax but violates contracts? |
| **Changes trajectory measurably** | Is there observed evidence (from 5-task data or prior runs) that this edge type changed agent behavior? |

### Per-Edge-Type Actionability Assessment

| Edge Type | Search↓ | 1st Read | 1st Edit | Test | Verify | Prevent Wrong | Evidence |
|-----------|---------|----------|----------|------|--------|---------------|----------|
| **CALLS_SAME_FILE** | none | none | none | none | none | none | Agent already has full file context |
| **CALLS_CROSS_FILE (import-verified)** | high | medium | low | low | low | none | Proven: xarray resolved because cross-file callers shown |
| **FILE_IMPORTS_FILE** | high | high | medium | none | none | none | Reduces file set from 10K to ~50 |
| **CLASS_INHERITS** | medium | medium | medium | none | none | medium | Override semantics prevent wrong method signature |
| **TEST_ASSERTS_SYMBOL** | none | none | none | **very high** | **very high** | high | loguru/cfn-lint: agent ran wrong tests 11/5 times |
| **CALLER_EXPECTS_RETURN** | none | none | none | none | medium | **very high** | cfn-lint: agent changed behavior without knowing caller expectations |
| **CALLER_PASSES_PATTERN** | none | none | none | none | low | high | Shows what arguments callers actually pass |
| **NAME_MATCH_AMBIGUOUS** | none | none | none | none | none | none | 49% random — no decision value |
| **COVERAGE_LINE_EXECUTED** | none | none | none | **very high** | high | medium | Which test covers which line = perfect test selection |
| **FAILING_TEST_REACHES** | none | none | low | **very high** | **very high** | high | THE failing test + its path = the strongest signal |

### Key Findings

**Structural precision (better call edges) has HIGH value for search reduction but ZERO value for semantic correctness.**

The 5-task data proves this:
- beancount-931: Agent found file WITHOUT any GT help (grep for "leafonly" sufficed). Better graph wouldn't have helped.
- beets-5495: Agent found file via grep. GT's one contribution was a SIGNATURE suggestion (behavioral, not structural).
- xarray-9760: GT helped because it showed CALLER CODE LINES (behavioral content, not just existence of edge).
- cfn-lint-3821: Agent found files, edited them, verified — still wrong. The missing information was WHAT CALLERS EXPECT.
- loguru-1306: Agent ran 11 tests — all passed. The missing information was WHICH SPECIFIC TEST FAILS.

**Pattern: In 4/5 tasks, the agent found the correct file without GT structural help. In 2/5 failed tasks, the failure was behavioral (wrong semantics), not navigational (wrong file).**

### Actionability Ranking

Based on decision-change evidence:

1. **TEST_ASSERTS_SYMBOL** + **FAILING_TEST_REACHES** — Directly answers "what test should I run?" (addresses loguru failure)
2. **CALLER_EXPECTS_RETURN** + **CALLER_PASSES_PATTERN** — Directly answers "what do callers expect?" (addresses cfn-lint failure)
3. **FILE_IMPORTS_FILE** — Reduces search space for unfamiliar repos (addresses beancount where agent greps)
4. **CALLS_CROSS_FILE (verified)** — Shows real relationships (proven on xarray)
5. **NAME_MATCH_AMBIGUOUS** — Zero decision value. Never actionable.

### Implication for Architecture

The graph must be designed with **actionability as the primary axis**, not correctness.

Concretely:
- A TEST_ASSERTS_SYMBOL edge with lower confidence but actual assertion text is MORE VALUABLE than a CALLS_CROSS_FILE edge with perfect precision but no behavioral content.
- An edge that tells you "test_importer.py:264 asserts `task.set_fields(lib)` produces no leftover files" changes agent behavior more than 100 perfectly-verified CALLS edges.

---

## 16. Consumer Utility Matrix

### Why Trust Tier Alone Is Insufficient

An edge can be CERTIFIED (the relationship provably exists) but operationally weak for certain consumers. Example: `FILE_IMPORTS_FILE` is 1.0 confidence (import statement in AST) but tells you NOTHING about behavioral contracts. It's excellent for ranking/navigation but useless for intervention/contracts.

### Full Utility Matrix

Utility scale: **none** (0) / **weak** (1) / **medium** (2) / **strong** (3) / **very strong** (4)

| Edge Type | Ranking | Navigation | Caller Extract | Contract | Intervention | Test Target | Verification |
|-----------|---------|-----------|---------------|----------|--------------|-------------|-------------|
| CALLS_SAME_FILE | none (0) | none (0) | weak (1) | none (0) | none (0) | none (0) | none (0) |
| CALLS_CROSS_FILE (verified) | strong (3) | strong (3) | strong (3) | weak (1) | medium (2) | none (0) | weak (1) |
| FILE_IMPORTS_FILE | strong (3) | strong (3) | none (0) | none (0) | none (0) | none (0) | none (0) |
| SYMBOL_IMPORTS_SYMBOL | strong (3) | very strong (4) | medium (2) | none (0) | none (0) | none (0) | none (0) |
| CLASS_INHERITS | medium (2) | medium (2) | none (0) | medium (2) | medium (2) | none (0) | none (0) |
| METHOD_OVERRIDES | medium (2) | strong (3) | strong (3) | strong (3) | strong (3) | none (0) | medium (2) |
| TEST_ASSERTS_SYMBOL | weak (1) | medium (2) | none (0) | very strong (4) | strong (3) | very strong (4) | very strong (4) |
| CALLER_EXPECTS_RETURN | none (0) | none (0) | very strong (4) | very strong (4) | very strong (4) | none (0) | strong (3) |
| CALLER_PASSES_PATTERN | none (0) | none (0) | strong (3) | strong (3) | strong (3) | none (0) | medium (2) |
| COVERAGE_LINE_EXECUTED | none (0) | none (0) | none (0) | medium (2) | medium (2) | very strong (4) | very strong (4) |
| FAILING_TEST_REACHES | none (0) | weak (1) | none (0) | strong (3) | very strong (4) | very strong (4) | very strong (4) |
| NAME_MATCH_AMBIGUOUS | none (0) | none (0) | none (0) | none (0) | none (0) | none (0) | none (0) |
| CO_CHANGED | medium (2) | medium (2) | none (0) | none (0) | weak (1) | none (0) | none (0) |

### Column Totals (Sum of Utility Across All Edge Types)

| Consumer Operation | Max Possible | Practical Max (top 3 edges) |
|-------------------|--------------|-----------------------------|
| **Ranking** | 17 | 9 (import + cross_file + symbol_import) |
| **Navigation** | 20 | 10 (symbol_import + cross_file + override) |
| **Caller Extraction** | 13 | 10 (expect_return + pass_pattern + cross_file) |
| **Contract Explanation** | 19 | 11 (test_asserts + expect_return + pass_pattern) |
| **Intervention** | 19 | 11 (failing_test + expect_return + test_asserts) |
| **Test Targeting** | 12 | 12 (test_asserts + coverage + failing_test) |
| **Verification Guidance** | 14 | 11 (test_asserts + coverage + failing_test) |

### Implications

1. **Structural edges (imports, calls) dominate RANKING and NAVIGATION** — these are the graph's traditional strength
2. **Behavioral edges (test_asserts, caller_expects, failing_test) dominate CONTRACTS, INTERVENTION, TEST TARGETING, and VERIFICATION** — these are where the graph currently has ZERO coverage
3. **NAME_MATCH_AMBIGUOUS has zero utility across ALL operations** — it should never reach any consumer
4. **The highest-utility edges for preventing wrong fixes (the dominant failure mode) are behavioral, not structural**

### Design Rule Derived

Each edge type should have a **utility profile** stored alongside it:

```python
EDGE_UTILITY = {
    "TEST_ASSERTS_SYMBOL": {
        "ranking": 1, "navigation": 2, "caller_extract": 0,
        "contract": 4, "intervention": 3, "test_target": 4, "verification": 4
    },
    "CALLS_CROSS_FILE": {
        "ranking": 3, "navigation": 3, "caller_extract": 3,
        "contract": 1, "intervention": 2, "test_target": 0, "verification": 1
    },
    # ...
}
```

Consumers query by operation type, and the system returns edges ranked by UTILITY for that operation — not just by trust tier.

A consumer asking "show me contracts for this function" gets TEST_ASSERTS_SYMBOL first (utility=4), not CALLS_CROSS_FILE (utility=1) — even though the CALLS edge might have higher raw confidence.

---

## 17. Contract-Priority Investigation

### The Hypothesis

**Behavioral contract extraction may be more important than structural graph precision for preventing agent failures.**

### Evidence From 5-Task Data

| Task | Agent Found File? | Agent Found Function? | Agent Wrote Correct Fix? | What Was Missing? |
|------|------|------|------|------|
| beancount-931 | YES (grep, iter 1) | YES | YES | Nothing — task is simple |
| beets-5495 | YES (grep) | YES | YES | Nothing — signature was enough |
| xarray-9760 | YES (86 actions, heavy searching) | YES | YES | Caller code helped (3 lines) |
| cfn-lint-3821 | YES (4 edits) | YES | **NO** | What callers EXPECT from the rule function |
| loguru-1306 | YES (2 edits) | YES | **NO** | Which test actually FAILS (agent ran 11 passing tests) |

**Pattern:** The agent's file-finding ability is strong (5/5 correct file found). The failure point is SEMANTIC — the agent doesn't know what behavior is expected.

### What Contract Information Would Have Changed Outcomes

**cfn-lint-3821 (FAILED — wrong fix semantics):**
```
MISSING: "validate_open_close(entries) is called by 4 callers who expect
          it to return empty list for valid entries and [Error(...)] for invalid.
          Caller test_rule.py:50 asserts validate_open_close([valid_entry]) == []"
```
With this, the agent would know its fix must preserve the empty-list contract for valid input.

**loguru-1306 (FAILED — wrong test verification):**
```
MISSING: "The FAILING test is test_loguru.py::test_format_exception which asserts
          that exception output includes the traceback header. Run THIS test
          specifically, not pytest test_loguru/ (which runs 200 passing tests)."
```
With this, the agent would have run the specific failing test and seen its fix was insufficient.

### Structural vs Behavioral: Decision-Change Analysis

| Information Type | Search Value | Semantic Value | Decision-Change Frequency |
|-----------------|-------------|---------------|---------------------------|
| "File X calls function Y" | HIGH | NONE | Changes agent path in 40% of tasks where agent is lost |
| "Test T asserts behavior B" | LOW | VERY HIGH | Would have changed outcome in 2/2 failed tasks |
| "Caller expects return type R" | LOW | VERY HIGH | Would have changed outcome in 1/2 failed tasks |
| "Specific failing test is T" | NONE | VERY HIGH | Would have changed outcome in 2/2 failed tasks |

### The Inversion

For the 2 FAILED tasks:
- Structural precision contributed NOTHING (agent already had the file)
- Behavioral contracts would have changed the outcome

For the 3 RESOLVED tasks:
- Structural precision helped in 1/3 (xarray — large repo, heavy searching)
- Behavioral contracts helped in 0/3 (agent solved without needing them)

**Structural graph helps FIND. Behavioral contracts help FIX.**

If the dominant failure mode is "found file, wrong fix" (2/5 tasks, 100% of failures), then **contract extraction is the higher-leverage problem**.

### What This Means for Graph Architecture

The graph should NOT be designed as:
```
"correct structural edges first, behavioral edges later"
```

It should be designed as:
```
"What information changes agent decisions? Build that. Trust tier applies to ALL edge types."
```

Concrete architectural implications:

1. **TEST_ASSERTS_SYMBOL is an equal-priority edge type to CALLS_CROSS_FILE** — not a "future addition"
2. **Evidence payload for caller edges MUST include source code at call site** — the edge without code is just a pointer
3. **The graph's primary value proposition is exposing behavioral expectations**, not mapping call trees
4. **A graph that maps all calls perfectly but stores no contracts is solving the WRONG problem for modern coding agents** that already grep effectively

### Unresolved Questions (require more data)

1. **Is contract extraction deterministic?** Can we extract "test T asserts behavior B" from AST alone, or does it require understanding test semantics?
   - Partial answer: assertion expressions ARE parseable by tree-sitter (assert_statement, assertEqual call). The assertion TARGET is derivable from call graph.

2. **How expensive is contract extraction at index time?** Reading source code at every call site means O(edges × file_reads).
   - Partial answer: source lines are already known (source_line field is 100% populated). Reading one line per edge = O(edges) file seeks.

3. **Does richer evidence help or hurt agent context?** More tokens per edge means fewer edges fit in context.
   - Partial answer: Strands research shows one-line directives (25 tokens) beat verbose blocks. Contract evidence must be COMPRESSED: "test_rule.py:50 asserts [] for valid input" (12 tokens), not a full code block.

4. **Does this generalize beyond the 5-task sample?** The "found file, wrong fix" pattern needs validation on N>30.
   - Partial answer: This matches JetBrains NeurIPS 2025 findings (84% of agent tokens are observation/navigation, actual edits are 3% — agents are good at finding, bad at semantic understanding).

### Recommendation

The graph architecture must treat BEHAVIORAL and STRUCTURAL edges as co-equal priorities from day one:

```
PHASE 1 (parallel):
  A. Structural precision: confidence floors, consumer safety, tier-gating
  B. Behavioral extraction: test→source edges with assertion text, caller→function edges with call-site code

NOT:
  PHASE 1: Better structural graph
  PHASE 2: Add behavioral edges (someday)
```

The final system must answer: **"What does this function's contract look like, according to its tests and callers?"** — not just **"Who calls this function?"**

---

## 18. Final Architectural Question

**"What repository information causally changes coding-agent behavior?"**

Based on all evidence gathered:

| Information | Changes Agent Behavior When... | Doesn't Help When... |
|-------------|-------------------------------|---------------------|
| File-to-file connections | Agent is in unfamiliar large repo (>1000 files) | Agent already knows the codebase or greps effectively |
| Cross-file caller list | Agent needs to understand impact of change | Agent is making a contained change |
| Assertion text from tests | Agent needs to verify fix semantics | Agent's fix is trivially correct |
| Specific failing test ID | Agent is running broad tests that pass | Agent already runs the right test |
| Caller argument patterns | Agent is changing a function interface | Agent is fixing internal logic |
| Source code at call site | Agent needs behavioral contracts | Agent just needs file navigation |

**The answer is not one thing.** It's a utility-weighted combination that depends on the agent's current state:
- **Lost agent (doesn't know where to go):** structural edges dominate
- **Found agent (right file, wrong fix):** behavioral contracts dominate
- **Testing agent (verifying fix):** test targeting edges dominate

The graph must serve all three states. But the current graph serves ONLY the first, and the dominant failure mode (measured) is the second.

---

## 19. What Not To Build

1. **No new downstream layers.** Graph creation is the ONLY concern. L1/L3/L3b/L5/prompts/runners untouched.
2. **No vector embeddings.** Semantic similarity is a candidate signal, not a structural evidence source.
3. **No LLM-based edge creation.** Every edge must be deterministically reproducible.
4. **No benchmark-specific tuning.** No gold files, task IDs, or resolved/failed labels in the graph.
5. **No runtime instrumentation framework at scale.** Coverage/trace edges are future infrastructure.
6. **No web dashboard for metrics.** CLI output and markdown reports are sufficient.
7. **No edge caching across repos.** Each repo's graph is independent.
8. **No incremental indexing (yet).** Full reindex is acceptable at current scale.
9. **No agent-facing API changes.** MCP tools continue to work — they just get richer data.
10. **No new languages.** Fix quality on existing 6 Tier 1 languages before expanding.
11. **No assumption that structural precision is sufficient.** Behavioral contracts are co-equal priority.
12. **No graph without actionability validation.** Every edge type must demonstrate decision-change, not just correctness.
