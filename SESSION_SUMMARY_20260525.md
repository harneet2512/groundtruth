# Session Summary — 2026-05-25

## Branch: jedi__branch
## Commits: ba0cb84b → a16a0f20 (30+ commits)

---

## Objective
Fix all GT bugs, implement remaining architecture, verify everything works, run Phase 2.

## What Was Built
- 62 bugs fixed (47 original QA + 15 code review)
- 23 property kinds extracted end-to-end (Go → DB → Python → agent)
- 11 new Go extractors + 4 enhanced
- Properties table wired in post_edit.py (was dead code)
- _container_query base64 fix (ALL container SQL was broken)
- _resolve_file_path (replaces 12 LIKE patterns)
- Host graph.db download + container fallbacks
- MCP tools 29 → 7
- L1+/L3+/L3b+/L6/Grep hooks enhanced
- Unified observability logging
- preflight_doc_of_honor.py (26 checks)
- DOC_OF_HONOR topological rewrite (87 claims)
- 1162 tests + 110 E2E + 26 preflight all passing

## Key Discovery: GT Disables OH Stuck Detector
GT prepends different evidence to every observation, making each one "unique." OH's stuck detector compares consecutive action-observation pairs — if 4+ identical, it kills the loop. GT makes them all different, so the detector never fires. Agent reads sh.py 25 times without being stopped. MORE GT = WORSE results because the stuck detector can't save the agent from exploration loops.

## Smoke Results: 0/5 on Phase 2
All plumbing verified working (L4a fires, preflight passes, graph_db=True). But 0 source edits across all 5 tasks. Root cause: stuck detector disabled by GT observation modification.

## Next Action
Fix stuck detector compatibility: when agent repeats an action, don't modify the observation. Let raw observation through so stuck detector sees the repetition. Only inject GT evidence on first occurrence.

## Research Citations
Vercel (passive>tools), ICSE 2022 (confidence 0.6), SWE-Pruner (less is more), Lost-in-middle (prepend position), OpenHands stuck detector issues #7183 #5480
