# NEXT_SESSION_HANDOFF.md — GT architecture audit + remediation

Session: 2026-05-31, branch `gt-consensus-curation`. Audit root: `.tmp_gt_architecture_audit/`.

## What this session did

**Architecture-to-output audit (Phases 0–1) — done + cited:**
- `ENVIRONMENT_AND_SOURCE.md` — audited SHA `eaa45b9c`, toolchain, **two-axis scope** (Axis-1 logic = this audit; Axis-2 efficacy = live run).
- `ARCHITECTURE_CLAIM_LEDGER.jsonl` — 30 layer claims, real code refs, status + logic/discrepancy flags.
- `HARDCODED_NUMBERS.md` + `HARDCODED_NUMBERS_REMEDIATION.md` — magic-number inventory + research-backed before/after.
- Source-of-truth corpus record: `.tmp_gt_turn_correctness_audit/github_sot/REMOTE_SOURCE_OF_TRUTH.md` (Deep SWE = datacurve-ai/deep-swe, 113 tasks, **5 langs**: py/ts/go/rust/js).

**Remediation APPLIED (clean files, additive, unit-green 99/0):**
- `v1r_brief.py`: `EDGE_CONFIDENCE_FLOOR` flat-0.7 gate → categorical `_edge_conf_clause` helper (×6 sites).
- `post_view.py`: `_test_file_targets` `>=0.5` → categorical `_edge_filter`.
- Committed scoped (code + audit docs only; the 8 pre-existing dirty files NOT touched).

## Deferred to next session (in priority order)

1. **LIVE validation of the applied Table-1 change** (Axis-2). DONE=metrics: run the brief on real graph.dbs and confirm no candidate/caller/test-link regression vs pre-change. Revert path: `git revert <code commit>`.
2. **OTHER bugs (un-resolved, untouched):**
   - P0 `os.walk`→`account.walk` laundering — **CONFIRMED at runtime** (shadowpkg fixture): resolver **Strategy 1.9 `verified_unique`** stamps the stdlib `os.walk` call CERTIFIED/0.95 because project `walk` is globally unique. **Precise fix** = suppress `verified_unique`/`type_flow` for *qualified* `module.name()` calls on imported non-project modules. Fix locus = **Go resolver** (gt-index); CI-only (no Go/GCC locally). Secondary `_is_stdlib_shadow` only guards the L1 render, not the graph/L3/L3b/map.
   - P1 `__GT_STRUCTURED__` stdout leak — verify/repair the L3b dispatch split in `oh_gt_full_wrapper.py` (DIRTY).
   - P2 `v22_brief` dead path still present in committed `eaa45b9c` `generate_task_brief` (the DIRTY working tree already removes it — confirm + land).
3. **Dirty-file hardcoded numbers** (need the 8 dirty files resolved first): post_edit numeric subqueries → categorical; `_classify_agent_state` stuck-governor absolutes; L3b iteration bands/char-caps. Localizer hyperparams (k_anchor/max_depth/tau/top_k/MAX_FILES) = Axis-2 levers → tune via live run, not blind.
4. **Complete the architecture audit (Phases 2–6):** IMPLEMENTATION_MAP.md, OUTPUT_CONTRACTS.md, per-layer PRODUCER audits on a fresh fixture+graph.db (clean `eaa45b9c` checkout), PLUMBING_MATRIX.md, CONTROLLED_E2E_REPORT.md.
5. **Then Phase 7 Deep SWE 5-lang smoke** (datacurve-ai/deep-swe, fresh clone @commit_hash, fresh graph.db) → DEEPSWE_5LANG_SMOKE_REPORT.md → only then a 25-task stratified audit.

## Standing constraints (carry forward)
- **8 pre-existing dirty tracked files** (`incremental.go`, `oh_gt_full_wrapper.py`, `post_edit.py`, `schemas.py`, +4 tests) — NOT mine; flagged session 1 for owner review before they get committed. Do not stack edits on them.
- **Axis distinction:** the audit proves logic correctness; "localization good enough" is a LIVE-run verdict only.
- **nothing local, all github:** audit the pushed `eaa45b9c`, not the dirty tree.
