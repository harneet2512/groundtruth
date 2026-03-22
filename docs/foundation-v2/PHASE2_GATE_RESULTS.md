# Phase 2 — Representation Substrate: Gate Results

**Date:** 2026-03-21
**Branch:** `research/foundation-v2`

## Gate 0: All tests pass

```
1130 passed, 4 skipped (1061 existing + 38 Phase 1 + 31 Phase 2)
```

No regressions.

## Gate 1: Phase complete

| Criterion | Status | Evidence |
|-----------|--------|---------|
| Registry accepts new rep types | PASS | `register_extractor()` + `get_registry()` tested with DummyExtractor |
| Schema creates alongside existing DB | PASS | `test_coexists_with_existing_tables` — existing `symbols` table survives |
| Store/retrieve/delete cycle works | PASS | 10 CRUD tests: round-trip, upsert, multi-type, get_all, delete_stale |
| Index version lifecycle | PASS | 6 tests: create→commit→supersede, abandon, multi-version |
| Metadata stores and retrieves | PASS | 7 tests: CRUD, scoped queries (same_class, same_module), is_test flag |
| No existing files modified | PASS | Only `core/flags.py` (Phase 1) and `pyproject.toml` (Phase 1) |
| Feature flag ready | PASS | Schema created on-demand via `RepresentationStore.__init__()` |

## Files created

- `src/groundtruth/foundation/repr/__init__.py`
- `src/groundtruth/foundation/repr/registry.py` — RepresentationExtractor protocol + registry
- `src/groundtruth/foundation/repr/schema.py` — DDL for 3 new tables
- `src/groundtruth/foundation/repr/store.py` — RepresentationStore (CRUD + versions + metadata)
- `tests/foundation/test_repr_store.py` — 31 tests

## Schema added (3 tables)

- `symbol_representations` — multi-rep storage (symbol_id × rep_type × rep_version)
- `symbol_similarity_metadata` — scoped query metadata (kind, file, class, language, arity, is_test)
- `index_versions` — versioned snapshots (building → current → superseded)

## Phase 2 complete. Phases 3, 4, 5 can begin.
