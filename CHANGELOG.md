# Changelog

## v1.0.0 (2026-04-01)

First public release.

### Architecture
- Hybrid indexing: tree-sitter (30 languages) + edge confidence scoring
- Go indexer (`gt-index`) with 3-stage resolution pipeline
- Python MCP server with 16 deterministic tools
- Evidence engine with 7 families (IMPORT, CALLER, SIBLING, TEST, IMPACT, TYPE, PRECEDENT)

### Go Indexer
- Edge confidence scoring: same_file/import=1.0, unique_name=0.9, ambiguous=0.2-0.6
- Edge deduplication (25% fewer edges on click repo)
- New indexes for confidence and resolution_method columns
- 6 languages with import extractors, 24 with name-match fallback

### MCP Server
- 16 tools all working with both Go (graph.db) and Python (index.db) schemas
- GraphStore bridge with 8 newly implemented methods
- Path normalization for cross-platform compatibility
- All tools deterministic -- anthropic moved to optional dependency

### Evidence Engine
- Admissibility gate re-enabled with confidence filtering
- Confidence-aware tier labels: [VERIFIED] >= 0.9, [WARNING] >= 0.5
- Critical path classification excludes test files
- Backward compatible with pre-v14 graph.db files

### New Commands
- `groundtruth resolve` -- diagnostic tool showing ambiguous edges and LSP server availability

### Bug Fixes
- Fixed GraphStore missing 8 interface methods (crashed MCP tools with graph.db)
- Fixed hotspots returning empty (usage_count=0 in Python indexer)
- Fixed symbols returning empty (absolute vs relative path mismatch)
- Fixed status crashing with graph.db (direct SQL on symbols table)
- Fixed LastInsertId error ignored in Go indexer
- Fixed resolver taking first-match instead of best-match for name_match
- Fixed CRITICAL_PATHS matching test files as critical infrastructure
- Fixed fetchone()[0] crash on empty query results
- Removed 63 temporary debug scripts from repo root
