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

## 7. Fix
Per-file-once gate: `l3b_file:{path}` key in evidence_sent. First delivery wins.
Replaces hash-based dedup (which was defeated) with path-based gate (unfilterable).

## 8. Tests
tests/invariants/test_l3b_dedup_per_file_once.py — 6 tests including weasyprint regression.
