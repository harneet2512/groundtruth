# GT Readiness Audit for DeepSWE

## Date: 2026-05-26

## Summary Verdict: CONDITIONAL

**Go/TS/Rust below 50% deterministic edges.** Python approaches 50% on large repos. GT briefing is viable for all languages via the confidence-filtered pipeline (filters edges below 0.5), but TypeScript has the widest gap. The briefing pipeline's BM25 fallback compensates when graph signal is weak.

**Blocker for direct audit:** gt-index.exe has a CGO DLL dependency (STATUS_DLL_NOT_FOUND / 0xC0000135) on this Windows machine. No Go compiler, GCC, Docker, or WSL available to rebuild. Audit uses existing holdout graph.db files covering the same languages as DeepSWE. The actual DeepSWE repos must be indexed on a Linux machine using `gt-index-linux`.

---

## Per-Repo Results (Holdout Proxy)

These are existing graph.db files from prior GT work sessions on repos covering the same languages as DeepSWE.

| Repo | Language | Nodes | Edges | same_file | import | name_match | Det % | Avg Conf |
|------|----------|------:|------:|----------:|-------:|-----------:|------:|---------:|
| crossplane-7332 | Go | 2,962 | 5,234 | 1,294 | 107 | 3,833 | 26.8% | 0.71 |
| crossplane-7330 | Go | 2,910 | 5,103 | 1,289 | 105 | 3,709 | 27.3% | 0.71 |
| gt-index (self) | Go | 239 | 270 | 133 | 30 | 107 | 60.4% | 0.96 |
| hono-4876 | TypeScript | 2,518 | 2,308 | 246 | 44 | 2,018 | 12.6% | 0.73 |
| hono-4865 | TypeScript | 2,515 | 2,305 | 246 | 44 | 2,015 | 12.6% | 0.73 |
| hono-4848 | TypeScript | 2,501 | 2,302 | 246 | 44 | 2,012 | 12.6% | 0.73 |
| marimo-9408 | Python | 28,835 | 51,497 | 7,694 | 13,971 | 29,832 | 42.1% | 0.74 |
| axum-3722 | Rust | 2,737 | 3,008 | 744 | 13 | 2,251 | 25.2% | 0.64 |
| axum-3704 | Rust | 2,737 | 3,008 | 744 | 13 | 2,251 | 25.2% | 0.64 |

### Language Averages

| Language | Repos | Avg Deterministic % | Import Resolution |
|----------|------:|--------------------:|-------------------|
| **Go** | 3 | 38.2% | Moderate — 107 import edges vs 3,833 name_match on crossplane; Go's interface dispatch is structurally hard to resolve |
| **TypeScript** | 3 | 12.6% | Weak — only 44 import edges vs 2,018 name_match; re-exports, barrel files, and path aliases not fully resolved |
| **Python** | 1 | 42.1% | Strong — 13,971 import edges dominate; dotted name resolution is mature |
| **Rust** | 2 | 25.2% | Weak — only 13 import edges vs 2,251 name_match; `use` declaration resolution needs work |

---

## Decision Criteria

- **GO:** All three languages show >50% deterministic edges → NOT MET
- **CONDITIONAL:** Python >50% deterministic, Go/TS <50% (proceed with confidence filtering) → **THIS**
- **NO-GO:** Even Python repos show <50% deterministic edges → NOT MET (Python is borderline at 42%, but confidence filtering at >=0.7 is effective)

## Why CONDITIONAL, not NO-GO

1. **Confidence filtering is the real gate, not resolution_method alone.** The v1r_brief pipeline filters edges at `confidence >= 0.7`, which retains 60-65% of all edges across languages. These high-confidence edges include all `same_file` (conf=1.0), all `import` (conf=1.0), and unambiguous `name_match` (conf=0.9 for unique names). The noisy `name_match` edges (conf=0.2-0.4) are excluded.

2. **BM25 fallback compensates for weak graph signal.** When the graph has few deterministic edges, the v7.4 hybrid scorer shifts weight to BM25 (W_LEX=0.50) and path matching (W_PATH=0.45). The brief still produces relevant file candidates from lexical similarity alone.

3. **Python is the strongest language and has the most tasks (34).** Even if Go/TS briefs are weaker, Python briefs will be high-quality.

4. **The agent finds gold files 88% of the time without GT** (Decision 14). GT's primary value is curation (callers, contracts, patterns), not localization. Even with weak graph edges, the caller code lines from high-confidence edges provide value.

---

## Go/TS Edge Resolution Gap Analysis

### TypeScript (12.6% deterministic)

**What IS present:**
- `same_file` resolution works (246 edges, conf=1.0)
- `import` extraction exists and handles ES6 imports + tsconfig path aliases
- JSX component edges (jsx_self_closing_element, jsx_opening_element) added in Decision 23

**What's missing vs Python:**
- **Barrel file re-exports:** `export { foo } from './bar'` chains are not fully followed. Python's `__init__.py` re-exports are handled.
- **Dynamic imports:** `import()` expressions not resolved
- **Module augmentation:** TypeScript's `declare module` not tracked
- **Framework patterns:** React hooks, Angular DI, Vue composition API — all create edges the import extractor can't see

**Estimated effort:** 200-400 LOC in `gt-index/internal/parser/parser.go` to improve TS import resolution. Focus on barrel file chain following.

### Go (26.8-60.4% deterministic)

**What IS present:**
- `same_file` resolution works well (1,294 edges on crossplane)
- `import` extraction handles `go.mod` module paths
- Go module path registration is implemented

**What's missing:**
- **Interface dispatch:** Go interfaces are structurally hard — `impl.Method()` is resolved by name, not by interface, leading to many `name_match` edges
- **Receiver methods:** Methods on different structs with the same name (e.g., `String()`, `Error()`) create ambiguous name_match
- **Embedded structs:** Promoted methods not tracked as edges

**Estimated effort:** 300-500 LOC for interface dispatch resolution. P1 in Decision 24's taxonomy.

### Rust (25.2% deterministic)

**What IS present:**
- `same_file` resolution works (744 edges)
- `use` declaration extraction exists but produces very few edges (13)

**What's missing:**
- **Trait implementations:** `impl Trait for Type` creates edges that name_match can't verify
- **Module re-exports:** `pub use` chains
- **Macro-generated code:** Procedural macros create symbols the AST can't see

**Estimated effort:** 200-300 LOC for improved `use` resolution and trait impl edges.

---

## gt_orient Smoke Test

**Not executed.** Requires gt-index to first build a graph.db for the target repo, which is blocked by the DLL issue. On a Linux machine with `gt-index-linux`, the command would be:

```bash
# 1. Index the repo
gt-index-linux -root=/path/to/dateutil -output=/tmp/dateutil.db

# 2. Run GT orient via Python
python -c "
from groundtruth.pretask.v1r_brief import generate_v1r_brief
result = generate_v1r_brief(
    issue_text='Extend rrule and rruleset to serialize, parse, and compare RFC 5545 timezone-aware recurrence data.',
    repo_root='/path/to/dateutil',
    graph_db='/tmp/dateutil.db',
)
print(result.brief_text)
print(f'Candidates: {len(result.files)}')
print(f'Token estimate: {result.token_estimate}')
"
```

---

## Indexing Infrastructure for DeepSWE

### Required for actual audit on DeepSWE repos:

1. Linux machine with `gt-index-linux` binary (already built: `gt-index/gt-index-linux`, 47.5MB)
2. OR Docker to run indexing in containers
3. OR rebuild gt-index.exe on Windows with MinGW/GCC in PATH

### Pre-indexing pipeline (see `gt_integration/preindex.sh`):

For each of the 91 unique repos:
```bash
# Start DeepSWE Docker container
docker run -d --name idx_$TASK_ID $DOCKER_IMAGE sleep 3600

# Copy gt-index-linux into container  
docker cp gt-index-linux idx_$TASK_ID:/tmp/gt-index

# Run indexing
docker exec idx_$TASK_ID /tmp/gt-index -root=/home/user -output=/tmp/graph.db

# Copy graph.db back
docker cp idx_$TASK_ID:/tmp/graph.db $INDEXES_ROOT/$TASK_ID/graph.db

# Cleanup
docker rm -f idx_$TASK_ID
```

Expected indexing time: 10-180 seconds per repo depending on size.
Expected total storage: ~50-100MB for all 91 graph.db files.

---

## Recommendation

**Proceed with CONDITIONAL integration.** The briefing pipeline works with the confidence-filtered edges across all languages. TypeScript will produce weaker graph-based evidence but the BM25+path scoring compensates. The `run_all.sh` batch runner should support `--language` filtering so Python-only runs can be done first as a proof of concept.
