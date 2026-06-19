# Handoff — 2026-05-31 (session 2): GT multi-path discovery + converge on OpenHands

## HEADLINE DECISION
**GT is not one product — there are 4 agent-facing emission paths + 2 MCP servers.**
**Converge on Path 2 (the OpenHands "Live Lite" pipeline) as THE GT. Harden it; ignore the rest.**

## Git state (READ FIRST)
- Branch `gt-consensus-curation`, **HEAD `41cfeb45`** (this commit adds one more on top).
- **UNPUSHED:** `origin/gt-consensus-curation` = `5022e1d5`. Local is 2 commits ahead:
  - `65f00aaf` — fix(relevance): 4 bugs from the beets-5495 trajectory audit  ← **the fixed OH wrapper**
  - `41cfeb45` — docs: morning handoff
  - (+ this handoff commit)
- **The GHA pipeline runs the PRE-FIX wrapper** (origin at 5022e1d5). To use the fixes: `git push origin gt-consensus-curation`.
- **Dirty TRACKED product files = NOT from this session** (analysis-only session). Provenance = prior/parallel work; left untouched, verify before committing:
  `gt-index/internal/store/incremental.go`, `scripts/swebench/oh_gt_full_wrapper.py`,
  `scripts/verify/check_brief_delivery.py`, `src/groundtruth/hooks/post_edit.py`,
  `src/groundtruth/telemetry/schemas.py`, + 3 test files.

## The 4 GT paths (verified on disk, not doc-derived)
| # | Path | Files | Harness | Engine |
|---|---|---|---|---|
| 1 | vNext `Finding` (3-surface) | `src/groundtruth/schema/finding.py`, `mcp/endpoints/{task_map,event_brief,review_patch}.py`, `benchmarks/swebench/gt_intel.py` | MCP clients, Mini-SWE | graph.db + typed `Finding` |
| **2** | **OpenHands Live Lite** ← USE THIS | `scripts/swebench/oh_gt_full_wrapper.py` → `v1r_brief.py`/`post_view.py`/`post_edit.py` | **OpenHands CodeActAgent** | graph.db, free-form |
| 3 | DeepSWE injection | `artifact_deepswe/gt_agent.py` → `benchmarks/swebench/gt_hook.py` | DeepSWE/Pier | **grep name-match** |
| 4 | SWE-agent gt_edit | `tools/sweagent/gt_edit/lib/gt_hook.py` | SWE-agent | grep name-match |

Also two MCP servers: `mcp/server.py` AND `mcp/composite_server.py`. The multiplicity is a half-finished
vNext consolidation (only wired into MCP+Mini-SWE) layered on 4 per-harness integrations.

## THE GT to use: OpenHands "Live Lite" (Path 2)
- **Entry:** `live_lite_full.yml` (coordinator) → `live_lite_inference.yml:334` runs
  `python scripts/swebench/oh_gt_full_wrapper.py …` driving real OpenHands `CodeActAgent` (`/tmp/OpenHands`)
  with GT: L1 brief (`v1r_brief`) + L3b (`post_view`) + L3 (`post_edit`) + grep intercept + L5/L6.
- **Paired baseline:** `GT_BASELINE=1` env disables GT (pure OpenHands) for A/B.
- **Modes:** `mode` input = smoke(5)/pilot20/pilot100/full300; corpus = SWE-bench-Live Lite.
- **Working on GitHub:** last *clean success* = run **`26699266942`** (2026-05-31 00:48Z). Later runs
  (01:32, 02:30, 14:09) cancelled/failed = active iteration.
- **Why this one:** only path that drives real OpenHands; most-developed; **every fix this session +
  prior landed here**. NOT bug-free (open localization/relevance bugs below) but the most mature.

## This session's findings (analysis only — NO product code changed)
1. **Localization audit** (gold-blind, 60 holdout tasks, 4 langs; `generate_v1r_brief`):
   **hit@1 13%, hit@3 22%, hit@5 23%. 46/60 (77%) gold absent from top-5.**
   Per-lang hit@1: **go 0%, ts 8%, rust 17%, python 21%.** Mislocalization mechanism = ranks
   NON-source files #1 (`CHANGELOG.md`, `Cargo.toml`, `zz_generated.conversion.go`,
   `scripts/run-pyright.py`) + repeated hub attractors across unrelated issues.
   Artifacts: `.tmp_gt_correctness_audit_20260531T1731Z.{py,json,log}`.
2. **Scorer weights** (`v7_4_brief.DEFAULT_WEIGHTS`): `W_LEX 0.50, W_PATH 0.45, W_FRAME 0.60,
   W_SEM 0.15, W_REACH 0.05, W_PROX 0.05` → **graph signals are only ~10% of localization score**;
   lexical+path dominate. So IMPORTS edges (5% reach) have a low ceiling; the real lever is a
   **non-source/generated-file filter + dir-proximity** (scoped, NOT implemented — user redirected).
3. **Graph quality per language** (60 holdout graphs): name_match% = **TS 87%, Rust 75%, Go 73%,
   Python 59%**; **ZERO IMPORTS edges in any graph** (only CALLS). verified-edge% tracks hit-rate.
4. **LSP** (`src/groundtruth/lsp/edge_verifier.py`, `GT_LSP_VERIFY`): verifies L3 **callers at
   delivery only** — does NOT rewrite graph.db, does NOT touch localization. **col-0 bug**: queries
   `references` at column 0 → 0 refs → would mark every caller unverified (HARMFUL if flipped on).
   Only `pyright` installed; `gopls`/`rust-analyzer`/`tsserver` missing. LSP→localization = net-new build.
   Probe: `.tmp_lsp_col_probe_20260531.py`.
5. **Render hygiene** (delivered `brief_text`): **0/60 leaks** — the L1 brief path is clean.
6. **DeepSWE (Path 3) setup audit:** `gt_agent.py` injects `gt_hook.py` (grep `understand`/`verify`,
   self-contained git+grep, runs container-style). Output = name-match on generic symbols
   (`app`/`init`/`__init__`). Risk: non-Python task containers may lack `python3` → GT no-ops.
7. **Conformance doc** read (`~/Downloads/GT_Emission_Architecture_Conformance_20260531T1443Z.md`,
   prior session): vNext 3-surface `Finding` is claimed-authoritative; OH bypasses it.
   Source-of-truth conflict `GT_ARCHITECTURE_CONTRACT.md` vs `DOC_OF_HONOR.md`.

## Next session — in order
1. **Push** `65f00aaf` (+ this handoff) so the GHA pipeline runs the FIXED OH wrapper.
2. **Dispatch `live_lite_full` `mode=smoke` (5 tasks) on `gt-consensus-curation`** → confirm the fixed
   OH wrapper is green end-to-end + GT delivers (audit `output.jsonl` agent observations, NOT telemetry).
3. **Localization is the open product bug on Path 2** (77% multi-lang gold-miss). Cheap lever scoped but
   unbuilt: source-file/generated filter + dir-proximity in `v7_4_brief`. Graph (LSP/IMPORTS) is a low
   ceiling at 10% weight — don't start there.
4. **4-path consolidation:** keep OH Live Lite as THE GT for OH; retire `gt_hook.py` (Paths 3/4);
   decide vNext `Finding`'s role (it's the only typed/contract-tested path — candidate future unifier).

## Key artifacts (local; `.tmp_` is gitignored)
- `.claude/PROMPT_gt_output_correctness_audit_20260531T1731Z.md` (+ copy in `~/Downloads/`) — the
  full "bug in everything GT sends" audit prompt (7 claim families A–G).
- `.tmp_gt_correctness_audit_20260531T1731Z.{py,json,log}` — localization + render-hygiene audit.
- `.tmp_loc_recall_vs_rank_20260531.py` — recall-vs-rank + source-filter prototype (unrun; user redirected).
- `.tmp_lsp_col_probe_20260531.py` — LSP col-0 bug probe.
- `.tmp_gt_turn_correctness_audit/deepswe/smoke_5lang.py` — abandoned synthetic per-claim harness.
- `~/Downloads/GT_Emission_Architecture_Conformance_20260531T1443Z.md` — prior-session conformance audit.
