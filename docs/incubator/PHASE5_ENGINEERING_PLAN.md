# Phase 5 Engineering Plan — Incubator Integration + Foundation Scale

> Produced from 4-round Codex (GPT-5.4) × Opus debate (2026-03-21).
> Round 4 was adversarial plan review — all bugs found are now fixed.

---

## Scope

Phase 5 delivers:
1. **Incubator wiring** — connect Phase 1-4 subsystems to MCP tools and CLI
2. **Accumulated repo intelligence** — logging-first persistent learning
3. **Communication state machine fixes** — threshold, tool name normalization
4. **SubstrateQuery abstraction** — brute-force → HNSW migration path for similarity
5. All behind feature flags. Flags OFF = byte-identical output.

---

## Architecture Decisions (from debate)

| Decision | Chosen | Alternative Rejected | Why |
|----------|--------|---------------------|-----|
| Integration pattern | `IncubatorRuntime` facade | Per-tool enrich methods | Single audit point for flag parity |
| Repo intel schema | 4 summary tables + existing `pattern_log` | Single generic table | O(1) decision reads vs O(N) scans |
| Repo intel gating | Two flags: LOGGING → DECISIONS (hard dep) | Single flag | Logging-first rollout, no premature influence |
| State machine booleans | No — existing TaskPhase enum suffices | Add patch_seen/tests_seen | Prevents dual-state divergence |
| PATCH_EXISTS transition | Handler produces `has_changes` evidence bit | Infer from tool name alone | Empty/no-op diffs are real |
| HNSW dependency | Optional: `groundtruth[hnsw]` | Required in core | Conservative until proven in production |
| Similarity abstraction | `SubstrateQuery` protocol | Direct HNSW calls | Future backends (partitioned, daemon) |
| Scope filtering | Post-filter after HNSW query | Pre-filter with hnswlib labels | Simpler, fast enough at 100K |
| Session state persistence | In-memory (accept reset on restart) | Persist to SQLite | MCP keeps process alive per session |
| Abstention authority | Single shared callable, not dual paths | Inline in handler + runtime | Prevents split authority divergence |
| Flag migration | Old flag as compat alias for one release | Hard cutover | Prevents stranding existing deployments |

---

## Feature Flags

| Flag | Default | Controls |
|------|---------|----------|
| `GT_ENABLE_REPO_INTEL` | OFF | **DEPRECATED** — compat alias, maps to LOGGING when split flags unset |
| `GT_ENABLE_REPO_INTEL_LOGGING` | OFF | Append-only data collection to summary tables |
| `GT_ENABLE_REPO_INTEL_DECISIONS` | OFF | Use summary data in responses (requires LOGGING) |
| `GT_ENABLE_RESPONSE_STATE_MACHINE` | OFF | Communication framing in tool responses |
| `GT_ENABLE_FOUNDATION` | OFF | Foundation pipeline (similarity + graph expansion) |
| `GT_ENABLE_HNSW` | OFF | Use HNSW backend instead of brute-force (requires hnswlib) |

Existing flags remain: `CONTRADICTIONS`, `ABSTENTION`, `COMMUNICATION`, `STATE_FLOW`, `CONVENTION_FINGERPRINT`, `CONTENT_HASH`, `STRUCTURAL_SIMILARITY`, `TREESITTER`.

**Flag precedence rules:**
- New split flags (`REPO_INTEL_LOGGING`, `REPO_INTEL_DECISIONS`) take priority when set
- If only old `GT_ENABLE_REPO_INTEL=1` is set and split flags are unset → maps to `LOGGING=on, DECISIONS=off`
- If both old and new flags are set → new flags win, old flag ignored
- Emit one deprecation warning in structlog when old flag is detected

---

## Byte-Identical Definition

"Byte-identical" means: **all keys EXCEPT `_incubator_*`, `_token_footprint`, and `_framing` have identical values and identical JSON structure** when comparing flags-OFF output to pre-incubator baseline.

Rationale: `_token_footprint` is computed from serialized payload size. When enrichment adds `_incubator_*` keys, the payload is larger, so the footprint value changes. This is expected and acceptable. The test helper strips these three key prefixes before comparison.

**Strict zero-side-effect contract when all Phase 5 flags OFF:**
- `IncubatorRuntime` is NOT constructed (runtime is `None`)
- Zero additional imports executed
- Zero DDL statements run
- Zero dict mutations in `_finalize()`
- Zero new keys added to any result

---

## Step-by-Step Build Order

### Step 1: Flag Migration + New Flag Infrastructure
**Files:** `src/groundtruth/core/flags.py`, `src/groundtruth/core/ablation.py`

**Migration of existing `GT_ENABLE_REPO_INTEL`:**
```python
def repo_intel_logging_enabled() -> bool:
    """New split flag for data collection."""
    if is_enabled("REPO_INTEL_LOGGING"):
        return True
    # Compat: old umbrella flag maps to logging-only
    if is_enabled("REPO_INTEL") and not is_enabled("REPO_INTEL_LOGGING"):
        return True
    return False

def repo_intel_decisions_enabled() -> bool:
    """Use collected data in responses. Hard-depends on LOGGING."""
    return repo_intel_logging_enabled() and is_enabled("REPO_INTEL_DECISIONS")

def response_state_machine_enabled() -> bool:
    return is_enabled("RESPONSE_STATE_MACHINE")

def hnsw_enabled() -> bool:
    return is_enabled("HNSW")
```

**Update `AblationConfig`:**
- Replace `repo_intel: bool` with `repo_intel_logging: bool` and `repo_intel_decisions: bool`
- Update `from_env()` and `describe()` methods
- Update preset configs (`"intel_logging"`, `"full_stack"`, etc.)

**Gate:** Unit tests for:
- DECISIONS → LOGGING hard dependency
- Old flag only → LOGGING on, DECISIONS off
- New flags only → correct behavior
- Both old and new set → new flags win
- Neither set → both off

**Tests:** `tests/unit/test_flags_migration.py`

---

### Step 2: Communication State Machine Fixes
**Modify:** `src/groundtruth/core/communication.py`

> **Moved before runtime wiring** because `_finalize()` already owns CommunicationPolicy. Fix the state machine before adding new code to _finalize().

1. Change `search_spin_threshold` default from 5 to 3
2. Add `normalize_tool_name()`:
   ```python
   def normalize_tool_name(raw: str) -> str:
       """Strip MCP prefixes to get canonical tool name."""
       name = raw.removeprefix("groundtruth_")
       name = name.removeprefix("consolidated_")
       return name
   ```
3. Accept `evidence` dict in `record_tool_call()`:
   ```python
   def record_tool_call(self, state: SessionState, tool_name: str,
                        evidence: dict[str, object] | None = None) -> SessionState:
       # ... existing counting + phase logic
       # PATCH_EXISTS only on evidence, not tool name alone
       if tool_name in _CHECK_TOOLS:
           if evidence and evidence.get("has_changes"):
               new_phase = TaskPhase.PATCH_EXISTS
           # else: stay in current phase (empty diff = no transition)
   ```

**Modify:** `src/groundtruth/mcp/server.py` `_finalize()`
- Call `normalize_tool_name()` before `record_tool_call()`
- Accept and pass evidence dict

**Modify:** `src/groundtruth/mcp/tools/core_tools.py`
- `handle_consolidated_check` adds `_evidence: {"has_changes": bool(changed_files)}` to result dict

**Gate:** State machine unit tests:
- 3 searches without edit → search_spinning redirect
- Empty check-diff → NO false PATCH_EXISTS transition
- Non-empty check-diff → PATCH_EXISTS transition
- Tool name normalization covers all 20+ tool names

**Tests:** `tests/unit/test_communication_fixes.py`

---

### Step 3: IncubatorRuntime No-Op Shell + _finalize() Wiring
**New files:**
- `src/groundtruth/incubator/__init__.py`
- `src/groundtruth/incubator/runtime.py`

```python
class IncubatorRuntime:
    """Facade for all incubator enrichments.

    Byte-parity contract: when no enrichment flags are on,
    enrich() returns the SAME dict object. No copy, no mutation.
    """
    def __init__(self, store: SymbolStore, root_path: str):
        self._store = store
        self._root_path = root_path

    def enrich(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        if not self._any_enrichment_on():
            return result  # same object — test with `assert out is inp`
        # Shallow copy only when adding data
        enriched = dict(result)
        # ... (filled in later steps)
        return enriched

    def log_interaction(self, tool_name: str, result: dict[str, Any]) -> None:
        pass  # filled in Step 4

    def _any_enrichment_on(self) -> bool:
        return False  # no-op for now
```

**Modify:** `src/groundtruth/mcp/server.py` — exact `_finalize()` contract:

```python
# In create_server():
runtime: IncubatorRuntime | None = None
if _any_phase5_flag_on():
    runtime = IncubatorRuntime(store, root_path)

def _finalize(tool_name: str, result: dict, evidence: dict | None = None) -> str:
    """Serialize result, track tokens, add footprint.

    Mutation order (MUST NOT be reordered):
    1. Communication framing (existing)
    2. Incubator enrichment (may return NEW dict — MUST reassign)
    3. Token tracking (on enriched result, accounts for added data)
    4. Repo intel logging (AFTER tracking, so logged shape = agent-visible shape)
    5. Final serialization
    """
    # 1. Communication framing
    if flags.communication_enabled():
        normalized = normalize_tool_name(tool_name)
        comm_state[0] = comm_policy.record_tool_call(comm_state[0], normalized, evidence)
        framing = comm_policy.get_framing(comm_state[0], normalized)
        if framing:
            result["_framing"] = framing

    # 2. Incubator enrichment
    if runtime is not None:
        result = runtime.enrich(tool_name, result)

    # 3. Token tracking
    response_text = json.dumps(result)
    call_tokens = token_tracker.track(tool_name, response_text)
    result["_token_footprint"] = token_tracker.get_footprint(tool_name, call_tokens)

    # 4. Repo intel logging (after tracking)
    if runtime is not None:
        runtime.log_interaction(tool_name, result)

    # 5. Final serialization
    return json.dumps(result)
```

**When all Phase 5 flags OFF:** `runtime` is `None`. Steps 2 and 4 are skipped entirely. Zero dict mutations from incubator code. Zero imports. Zero DDL.

**Gate:** Full test suite passes. Golden-output JSON test: serialize known result through `_finalize()` with flags OFF, assert byte-identical to recorded baseline.

**Tests:** `tests/unit/test_incubator_runtime.py`, `tests/unit/test_finalize_golden_output.py`

---

### Step 4: Repo Intelligence Logging + Inline Migration
**New files:**
- `src/groundtruth/incubator/intel_logger.py`

**Modify:** `src/groundtruth/index/schema.sql` — add 4 summary tables:

```sql
CREATE TABLE IF NOT EXISTS repo_obligation_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    obligation_kind TEXT NOT NULL,
    seen_count INTEGER DEFAULT 1,
    confidence_avg REAL,
    last_seen_at INTEGER NOT NULL,
    UNIQUE(subject, obligation_kind)
);

CREATE TABLE IF NOT EXISTS repo_convention_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key TEXT NOT NULL,
    fingerprint_hash TEXT NOT NULL,
    stable_count INTEGER DEFAULT 1,
    drift_count INTEGER DEFAULT 0,
    last_seen_at INTEGER NOT NULL,
    UNIQUE(scope_key)
);

CREATE TABLE IF NOT EXISTS repo_confusion_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    confusion_kind TEXT NOT NULL,
    seen_count INTEGER DEFAULT 1,
    corrected_count INTEGER DEFAULT 0,
    last_seen_at INTEGER NOT NULL,
    UNIQUE(symbol, confusion_kind)
);

CREATE TABLE IF NOT EXISTS repo_cochange (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_a TEXT NOT NULL,
    file_b TEXT NOT NULL,
    seen_count INTEGER DEFAULT 1,
    last_seen_at INTEGER NOT NULL,
    UNIQUE(file_a, file_b)
);
```

**RepoIntelLogger implementation:**
- Tables created in `RepoIntelLogger.__init__()` via `CREATE TABLE IF NOT EXISTS` — only when constructed (flag is ON)
- Takes tool_name + result dict after token tracking
- Extracts: obligations fired, conventions detected, symbols in findings, changed files
- Upserts into summary tables using `INSERT ... ON CONFLICT(subject, obligation_kind) DO UPDATE SET seen_count = seen_count + 1, confidence_avg = ...`
- Single transaction per log call (<1ms)
- All summary queries use deterministic `ORDER BY subject, obligation_kind` with tiebreakers

**Relationship to `pattern_log`:** `pattern_log` is the legacy raw telemetry table. RepoIntelLogger writes to the NEW summary tables only. `pattern_log` is NOT used by the new logger. Both can coexist.

**Inline migration — remove old `store.log_pattern()` calls:**
- Remove `core_tools.py:458-473` (the inline `store.log_pattern()` calls for obligations and contradictions)
- Replace with: `IncubatorRuntime.log_interaction()` handles this via `RepoIntelLogger`
- The old `store.log_pattern()` method stays in `store.py` (not deleted) but is no longer called from check handlers

**Wire into:** `IncubatorRuntime.log_interaction()` — construct RepoIntelLogger when `repo_intel_logging_enabled()`

**Schema migration for existing DBs:** `CREATE TABLE IF NOT EXISTS` handles it. No ALTER TABLE needed. Empty tables are harmless.

**Gate:** Run check-diff twice. Verify data appears in summary tables. Verify output is UNCHANGED (no influence). Verify old `pattern_log` is no longer written to from check handler.

**Tests:** `tests/unit/test_intel_logger.py`, `tests/unit/test_intel_logger_migration.py`

---

### Step 5: Extract Abstention Into Shared Interface + Wire

> **CRITICAL FIX from Round 4:** Do NOT duplicate abstention. Extract first, then wire.

**New file:** `src/groundtruth/incubator/abstention_bridge.py`

```python
class AbstentionBridge:
    """Single callable for abstention decisions. Used by both core_tools
    and IncubatorRuntime to prevent split authority."""

    def __init__(self, store: SymbolStore, root_path: str):
        self._policy = AbstentionPolicy()
        self._freshness = FreshnessChecker() if flags.abstention_enabled() else None
        self._store = store
        self._root_path = root_path

    def filter_findings(
        self, findings: list[dict], file_path: str
    ) -> tuple[list[dict], list[dict]]:
        """Returns (hard_blockers, soft_info). Suppresses EMIT_NOTHING."""
        if not flags.abstention_enabled() or self._freshness is None:
            return findings, []
        # ... existing logic extracted from core_tools.py:370-396
```

**Modify:** `src/groundtruth/mcp/tools/core_tools.py`
- Extract the inline abstention block (lines 350-396) into `AbstentionBridge.filter_findings()`
- Replace inline code with: `bridge.filter_findings(contras, cf)`
- Bridge is constructed once in `handle_consolidated_check` or passed from server

**Modify:** `src/groundtruth/incubator/runtime.py`
- `enrich()` uses the same `AbstentionBridge` for impact/obligation abstention
- Single authority, one code path, no divergence

**Gate:** All existing abstention tests pass unchanged. Core_tools behavior identical. New impact/obligation paths also go through abstention when flag ON.

**Tests:** `tests/unit/test_abstention_bridge.py`

---

### Step 6: Conventions + State Flow in Obligation Output
**Modify:** `src/groundtruth/incubator/runtime.py`

When `GT_ENABLE_CONVENTION_FINGERPRINT=1`:
- Add `_incubator_conventions` key to impact/obligation results
- Contains convention fingerprint for affected classes
- Import `ConventionFingerprint` from `analysis.conventions`

When `GT_ENABLE_STATE_FLOW=1`:
- Add `_incubator_state_flow` key
- Contains `StateFlowGraph` data for shared_state obligations
- Import `build_state_flow` from `analysis.pattern_roles`

**All imports are inside flag-gated blocks** — no module-level imports of analysis code from runtime.py. This prevents import side effects when flags are OFF.

**Gate:** Impact output enriched when flags ON. Flags OFF = byte-identical (tested via golden output).

---

### Step 7: CLI Integration
**Modify:** `src/groundtruth/cli/commands.py`

> **GAP FIX from Round 4:** CLI `check-diff` is a separate code path. It must have access to IncubatorRuntime.

- Construct `IncubatorRuntime` in CLI `check_diff()` command when any Phase 5 flag is on
- Call `runtime.enrich("check", result)` before printing output
- Call `runtime.log_interaction("check", result)` after printing
- When all flags OFF: runtime is `None`, CLI behavior identical

**Gate:** CLI check-diff produces same enrichment as MCP check handler. CLI parity test.

**Tests:** `tests/unit/test_cli_incubator.py`

---

### Step 8: SubstrateQuery Abstraction + HNSW Migration
**New files:**
- `src/groundtruth/foundation/similarity/substrate.py` — Protocol + Candidate
- `src/groundtruth/foundation/similarity/substrate_bruteforce.py` — BruteForceSubstrateQuery
- `src/groundtruth/foundation/similarity/substrate_hnsw.py` — HnswSubstrateQuery

```python
# substrate.py
from typing import Protocol

@dataclass
class Candidate:
    symbol_id: int
    similarity: float
    rep_type: str

class SubstrateQuery(Protocol):
    def query(self, *, rep_type: str, query_blob: bytes, top_k: int,
              index_version: int | None = None,
              allowed_symbol_ids: set[int] | None = None) -> list[Candidate]: ...
    def insert(self, symbol_id: int, rep_type: str, blob: bytes) -> None: ...
    def delete(self, symbol_id: int, rep_type: str) -> None: ...
    def count(self, rep_type: str) -> int: ...
```

**Modify:** `src/groundtruth/foundation/similarity/composite.py`
- `find_related()` accepts **OPTIONAL** `substrate: SubstrateQuery | None = None` parameter
- Default `None` → constructs `BruteForceSubstrateQuery(store)` internally
- **This preserves the existing call signature** — `run_pipeline()` in `pipeline.py` calls `find_related(store=..., ...)` without substrate param and continues to work unchanged
- When `hnswlib` importable AND `GT_ENABLE_HNSW=1`: caller passes `HnswSubstrateQuery`
- HNSW is candidate generation only — weighted scoring unchanged

**HnswSubstrateQuery:**
```python
class HnswSubstrateQuery:
    def __init__(self, index_dir: str, dim: int = 32, M: int = 16, ef: int = 200):
        import hnswlib  # lazy import — no cost when not used
        self._indexes: dict[str, hnswlib.Index] = {}
        self._index_dir = index_dir
        self._dim = dim
        self._M = M
        self._ef = ef

    def query(self, *, rep_type, query_blob, top_k, index_version=None,
              allowed_symbol_ids=None):
        idx = self._get_or_load_index(rep_type)
        labels, distances = idx.knn_query(decode_blob(query_blob), k=top_k * 3)
        results = [(int(l), float(d)) for l, d in zip(labels[0], distances[0])]
        if allowed_symbol_ids is not None:
            results = [(l, d) for l, d in results if l in allowed_symbol_ids]
        return [Candidate(l, 1.0 - d, rep_type) for l, d in results[:top_k]]
```

**Index stored at:** `.groundtruth/hnsw_{rep_type}.bin`

**pyproject.toml:**
```toml
[project.optional-dependencies]
hnsw = ["chroma-hnswlib>=0.7.6"]
```

**No-DDL guard:** `HnswSubstrateQuery` is only constructed when `GT_ENABLE_HNSW=1` AND `hnswlib` is importable. `RepresentationStore` constructor calls `create_representation_schema()` — this runs `CREATE TABLE IF NOT EXISTS` which is safe but not free. Ensure `RepresentationStore` is only constructed when `GT_ENABLE_FOUNDATION=1`.

**Gate:** Latency <2x with HNSW flag ON. BruteForce and HNSW produce same top-10 results on test fixtures (allow reordering within score ties). No DDL when HNSW/foundation flags OFF.

**Tests:** `tests/unit/test_substrate_query.py`, `tests/unit/test_hnsw_backend.py`, `tests/unit/test_no_ddl_when_disabled.py`

---

### Step 9: Repo Intelligence Reader (Decision-Time)
**New file:** `src/groundtruth/incubator/intel_reader.py`

```python
class RepoIntelReader:
    """Read summary tables for decision-time enrichment.

    All queries use ORDER BY with deterministic tiebreakers
    and LIMIT to prevent unbounded reads.
    """
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get_obligation_history(self, subjects: list[str]) -> list[dict]:
        """Query repo_obligation_stats for historical patterns.
        ORDER BY subject, obligation_kind LIMIT 50."""

    def get_convention_stability(self, scope_key: str) -> dict | None:
        """Is this scope's convention stable or drifting?
        Returns None if no data."""

    def get_confusion_rate(self, symbol: str) -> float:
        """How often is this symbol confused by agents? Returns 0.0 if no data."""
```

- Only constructed when `GT_ENABLE_REPO_INTEL_DECISIONS=1` (which requires LOGGING)
- Enrichment added as `_incubator_obligation_history`, `_incubator_convention_stability`
- All queries: deterministic `ORDER BY ... LIMIT`

**Gate:** Decisions only appear when BOTH flags ON. LOGGING alone = no output change.

**Tests:** `tests/unit/test_intel_reader.py`

---

### Step 10: Foundation Pipeline Wiring
**Modify:** `src/groundtruth/incubator/runtime.py`

When `GT_ENABLE_FOUNDATION=1`:
- Lazy-init `RepresentationStore` and `GraphExpander` on **first call** (not in constructor)
- Call `run_pipeline()` for impact/obligation queries only (not search/references/trace)
- Add `_incubator_foundation_candidates` to result
- Use `SubstrateQuery` (HNSW if available and flag ON, else brute-force)

**Lazy init pattern:**
```python
def _get_foundation(self):
    if self._foundation is None:
        from groundtruth.foundation.repr.store import RepresentationStore
        from groundtruth.foundation.graph.expander import GraphExpander
        self._foundation = (
            RepresentationStore(self._store.connection),
            GraphExpander(self._store),
        )
    return self._foundation
```

**Gate:** Latency <2x with FOUNDATION flag ON. Foundation candidates appear only for impact/obligation tools. No foundation imports when flag OFF.

---

### Step 11: End-to-End Integration Tests
**New file:** `tests/integration/test_incubator_e2e.py`

Test cases:
1. Index fixture repo → query impact → get obligations with roles + conventions
2. Run check-diff on fixture patch → output includes obligations when flags ON
3. **Golden output:** Full flags-OFF run → output identical to pre-incubator baseline (stripped of `_incubator_*`, `_token_footprint`, `_framing`)
4. Each flag independently → only that subsystem activates, nothing else changes
5. Repo intel logging → data logged to summary tables, no output influence
6. Communication state machine → 3 searches without edit → redirect framing appears
7. **CLI parity:** CLI check-diff and MCP check produce same obligation set
8. **Empty DB first-run:** All flags ON, brand-new repo → graceful table creation, no crash
9. **No-DDL-when-disabled:** All flags OFF → no new tables created in SQLite

---

### Step 12: Gate 1 Verification
Write results to `docs/incubator/PHASE5_GATE_RESULTS.md`:

1. End-to-end fixture test (MCP path) ✓/✗
2. End-to-end CLI test ✓/✗
3. Flag parity (golden output comparison) ✓/✗
4. Each flag independently ✓/✗
5. Accumulated intelligence (log only, no output) ✓/✗
6. Communication state machine (3 searches → redirect) ✓/✗
7. Latency (<2x with all flags ON) ✓/✗
8. Flag migration compat (old flag, new flags, both, neither) ✓/✗
9. No DDL when disabled ✓/✗

---

### Step 13: Gate 2 — 10-Task Diagnostic

| # | Scenario | Expected "Better" |
|---|----------|-------------------|
| 1 | check-diff: add method to class with conventions | Convention warning surfaced |
| 2 | check-diff: modify coupled pair | Both methods in obligation list |
| 3 | impact: high-fan-out symbol | More complete obligations via foundation |
| 4 | check-diff: contradiction | Caught with proper emission level |
| 5 | 3 sequential searches | Redirect framing appears |
| 6 | check-diff twice on same diff | Data logged, no output change |
| 7 | check-diff with DECISIONS on | Historical confidence surfaces |
| 8 | check-diff with empty diff | No false PATCH_EXISTS |
| 9 | check-diff on 5+ file patch | <2x latency |
| 10 | Full flags-OFF of scenarios 1-4 | Byte-identical to baseline |

**Pass criteria:** ZERO regressions. ≥2 tasks measurably better (more obligations, warnings, or contradictions).

**"Measurably better" criteria:**
- ≥1 additional correct information item that flags-OFF missed
- Phase accuracy vs labeled ground truth improves by ≥10%
- p95 latency: <5% regression flags OFF, <15% flags ON

---

## Kill Conditions

| Condition | Action |
|-----------|--------|
| >3 Phase 1-4 tests break during wiring | Redesign integration layer |
| >10% latency overhead on check-diff (flags OFF) | Identify bottleneck, disable that subsystem |
| Summary table ordering instability | Add deterministic ORDER BY + LIMIT |
| HNSW startup >5s for fixture repos | Defer HNSW, keep brute-force default |
| Abstention dual authority detected in tests | Revert to single-path via AbstentionBridge |

---

## New Files Summary

```
src/groundtruth/incubator/
├── __init__.py
├── runtime.py               # IncubatorRuntime facade
├── intel_logger.py          # RepoIntelLogger (append-only to summary tables)
├── intel_reader.py          # RepoIntelReader (decision-time summary queries)
├── abstention_bridge.py     # Single callable for abstention decisions
src/groundtruth/foundation/similarity/
├── substrate.py             # SubstrateQuery protocol + Candidate
├── substrate_bruteforce.py  # BruteForceSubstrateQuery
├── substrate_hnsw.py        # HnswSubstrateQuery (optional hnswlib)
tests/unit/
├── test_flags_migration.py
├── test_incubator_runtime.py
├── test_finalize_golden_output.py
├── test_intel_logger.py
├── test_intel_logger_migration.py
├── test_communication_fixes.py
├── test_abstention_bridge.py
├── test_cli_incubator.py
├── test_substrate_query.py
├── test_hnsw_backend.py
├── test_no_ddl_when_disabled.py
├── test_intel_reader.py
tests/integration/
├── test_incubator_e2e.py
docs/incubator/
├── PHASE5_ENGINEERING_PLAN.md   # this file
├── PHASE5_GATE_RESULTS.md       # written at Gate 1
```

## Modified Files Summary

```
src/groundtruth/core/flags.py              # flag migration + 4 new functions
src/groundtruth/core/ablation.py           # update for two-flag split
src/groundtruth/core/communication.py      # threshold 5→3, normalize_tool_name, evidence
src/groundtruth/mcp/server.py              # wire IncubatorRuntime into _finalize()
src/groundtruth/mcp/tools/core_tools.py    # extract abstention, remove inline log_pattern, evidence bits
src/groundtruth/cli/commands.py            # wire IncubatorRuntime into CLI check-diff
src/groundtruth/index/schema.sql           # +4 summary tables
src/groundtruth/foundation/similarity/composite.py  # optional substrate param
pyproject.toml                             # add [hnsw] optional dependency
```

---

## Estimated Effort

| Step | Effort | Risk |
|------|--------|------|
| 1. Flag migration + infrastructure | Small | Compat edge cases |
| 2. Communication fixes | Small | Tool name mapping coverage |
| 3. Runtime shell + _finalize() wiring | Medium | Mutation order, flag parity |
| 4. Repo intel logging + inline migration | Medium | Schema creation, log_pattern removal |
| 5. Abstention extraction + wiring | Medium | Must not change existing behavior |
| 6. Conventions + state flow | Medium | Source reading I/O, lazy imports |
| 7. CLI integration | Small | Parity with MCP path |
| 8. SubstrateQuery + HNSW | Large | Optional dependency, DDL guard |
| 9. Intel reader | Medium | Query ordering determinism |
| 10. Foundation wiring | Medium | Latency budget, lazy init |
| 11-13. Tests + gates | Large | Fixture coverage, golden outputs |

---

## Bugs Fixed From Round 4 Review

| Bug | Found By | Fix Applied |
|-----|----------|-------------|
| Flag migration gap (old `repo_intel_enabled()` not addressed) | Both | Step 1: compat alias + deprecation warning |
| `_finalize()` mutation order undefined | Both | Step 3: exact 5-phase contract documented |
| Abstention dual authority | Codex | Step 5: extract into `AbstentionBridge`, single path |
| Byte-identical definition too strict | Both | Defined: excludes `_incubator_*`, `_token_footprint`, `_framing` |
| CLI not wired to IncubatorRuntime | Opus | Step 7: new step for CLI integration |
| No DDL guard when flags OFF | Codex | Step 3: runtime=None contract; Step 8: no-DDL test |
| Missing golden-output tests | Both | Step 3: `test_finalize_golden_output.py` |
| Missing flag compat matrix tests | Codex | Step 1: `test_flags_migration.py` |
| `find_related()` signature break | Opus | Step 8: substrate param is OPTIONAL with default |
| `pattern_log` relationship unclear | Both | Step 4: explicitly stated as separate write path |
| Step ordering wrong | Codex | Reordered: 1→2→3→4→5→6→7→8→9→10→11→12→13 |

---

*Plan produced from Codex (GPT-5.4) × Claude (Opus 4.6) debate, 4 rounds including adversarial review.*
*All Round 4 bugs are fixed in this version.*
