# Topic Dossier: L3b Per-File-Once Dedup

**Source:** DOC_OF_HONOR §5.1 (Dedup), §2.3 (L3b Post-View)
**Risk level:** MEDIUM — weasyprint same callers 5x on file re-reads

## 1. DOC_OF_HONOR Intent
MD5 hash of stripped body, keyed per-file per-layer. Evolution safety valve at >5.

## 2. Current Branch Bug
Post_view.py filters out visited_files from callers/callees. Each re-read
produces DIFFERENT content (fewer callers as visited set grows), changing the
MD5 hash and defeating dedup. Semantically identical data re-injected.

## 3. jedi__branch
Same code, same bug.

## 4. Trajectory Evidence
weasyprint flex.py read 5+ times → same core callers (float.py:67, block.py:82)
injected each time with slight variations from visited_files filtering.

## 5. Research
- Du et al. EMNLP 2025: 13.9-85% degradation from context length
- OCD/SWEzze 2026: only 8.4% of segments needed
- Lost in the Middle NeurIPS 2024: repeated injections push useful evidence into dead zone
- Chroma 2025: every model degrades within claimed context windows

## 6. Gap
DOC says dedup WORKING. Hash-based dedup is defeated by visited_files filtering.

## 7. Fix (hybrid)
Two-layer gate:
1. Per-file-once: `l3b_file:{path}` key blocks pure re-reads (no graph change)
2. L6 reindex reset: successful reindex clears all `l3b_file:*` keys (graph changed)
3. Hash-based dedup: safety net for post-reindex re-reads where content didn't change

This allows re-delivery after edits (when graph data is legitimately different)
while blocking the 5x duplication from pure re-reads.

## 8. Tests
tests/invariants/test_l3b_dedup_per_file_once.py — 11 tests:
- 6 per-file-once gate tests (including weasyprint regression)
- 5 reindex reset tests (allows redelivery, preserves hash dedup, full cycle)
