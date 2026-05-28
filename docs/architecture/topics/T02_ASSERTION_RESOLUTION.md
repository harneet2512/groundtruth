# Topic Dossier: T02 Assertion Resolution (§0.6)

**Source:** DOC_OF_HONOR §0.6 Assertion Resolution (Multi-Signal Scoring)
**Status before:** REWRITTEN 2026-05-26, needs verification, target >=50%

## 1. DOC_OF_HONOR Intent
TCTracer-inspired 5-signal scoring (import 4.0, LCBA 3.0, naming 2.0,
same-package 2.0, non-test 0.5). Threshold 3.5. 19 assertion frameworks.

## 2. Current Branch (before fix)
Fixed threshold 3.5. No rescue for 0-candidate case. No score stored.
flexget test_ratio_limit → add_entries scores 2.5 < 3.5 → not linked.

## 3. jedi__branch
Identical code. Same gap.

## 4. Research
- TCTracer ICSE 2020: 5+1 signals including co-change (can't use due to pass ordering)
- Edge confidence model §0.5: fewer candidates = higher confidence
- Cursor principle: confident when unambiguous (1 candidate), silent when many

## 5. Gap
- Fixed threshold ignores candidate count ambiguity
- 0-candidate case returns 0 immediately — no fallback
- No score stored — Python side can't tier [TEST] display

## 6. Fix (3 changes, Go indexer)
1. Dynamic threshold: 1 cand→2.0, 2-3→3.0, 4+→3.5. No regression for 4+.
2. File-stem rescue: test_qbittorrent→qbittorrent→all functions in qbittorrent.py.
   Scores: file-stem(1.5)+same-pkg(2.0)+non-test(0.5)+expr-substr(1.0). Threshold 2.0.
   Only fires when main pass found 0 candidates.
3. resolution_score column in assertions table. Schema v15.2-trust-tier.

## 7. Regression safety
- 4+ candidates: threshold unchanged at 3.5
- Rescue pass: only fires on 0 candidates (can't affect existing links)
- Score column: DEFAULT 0.0, old readers unaffected (don't SELECT it)
