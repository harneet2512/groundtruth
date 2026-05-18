# golazo_today.md — Session Learnings (2026-05-17 to 2026-05-18, updated 2026-05-18)

## Session 2 Update (2026-05-18)

### 4 Bugs Fixed
1. **BUG-1 (CRITICAL)**: has_evidence gate missing 8 markers — BEHAVIORAL CONTRACT, TEST EXPECTS silently dropped even when correctly produced. Fixed: added all markers to gate.
2. **BUG-2 (HIGH)**: Semantic check shell one-liner fragile — replaced with `src/groundtruth/hooks/semantic_check.py` proper module.
3. **BUG-3 (MEDIUM)**: Behavioral contract outer `except: pass` — changed to logging.
4. **BUG-4 (LOW)**: No diagnostic logging for empty/unrecognized hook output — 5 new log points.
5. **Legacy gate at line 3125** also patched with same markers.

### 42 Tests Pass
- `tests/unit/test_semantic_check.py` — 16 tests (guard extraction, return paths, git-based diff)
- `tests/unit/test_evidence_gate.py` — 6 tests (gate recognizes behavioral contract, guards fire)
- `tests/unit/test_post_edit_improved.py` — 20 tests (existing + updated for contract priority)

### Key Finding: Behavioral Contract WORKS, Gate Was Broken
The behavioral contract code at `post_edit.py:966-1018` correctly extracts guards and return paths. Test output:
```
BEHAVIORAL CONTRACT:
  GUARD: if os.environ.get('FORCE_COLOR') -> return
  GUARD: if not sys.stderr.isatty() -> return
```
But the wrapper's has_evidence gate didn't include "BEHAVIORAL CONTRACT:" so the output was silently discarded.

### DeepSeek V4 Flash Analysis
- Our 30-task baseline (4/30=13.3%) uses DeepSeek V4 Flash via direct API
- V4 Flash claims 79% on SWE-bench Verified but used Inspect ReAct, not OpenHands
- Our config (temp=0.7, top_p=0.8) is suboptimal — V4 Flash model card says temp=1.0, top_p=1.0
- 4/30 is 6x more likely under V3-class performance than V4-Flash-class (binomial math)
- Qwen3-Coder (66.5% Verified on OpenHands) is a stronger bet for OH evaluation

### Inspect AI Integration Path
- Inspect natively speaks MCP — zero-code integration via `mcp_server_stdio()`
- For deeper hooks: `@tool` wrappers (~100 LOC) or lifecycle hooks
- MCP covers 60% of scaffold surface; observation augmentation needs per-scaffold adapters

---

## What We Built

### fliperachu Analysis
- Deep causal analysis of all GT layers across 5 tasks (fliperachu.md)
- Evidence hierarchy: File → Caller identity → Caller CODE → Test assertions → Behavioral contract
- Key finding: GT works through CONTEXT PRIMING, not explicit causal chains
- Paired delta is the ONLY valid metric (agent+GT vs agent alone)

### 10 Mechanisms Implemented (3 phases, research-backed)
1. L4 symbol fix — issue keywords not hub centrality (Agentless, arXiv 2407.01489)
2. Tool registration — native OH SDK ChatCompletionToolParam (patches/oh054/)
3. Behavioral contract — guard clauses + return paths (Shape or Distort, arXiv 2604.11088)
4. Constraint framing — `<gt-constraint> MUST NOT break` (Shape or Distort)
5. L1 keyword weight — exact basename match = 1.0
6. Recall injection — cache L3b evidence, re-inject at edit (Plan Compliance, arXiv 2604.12147)
7. L6 auto-consumer — disabled (15.6s overhead, 0 impact)
8. Adaptive L5 — threshold scales by repo complexity (SWE-Skills, arXiv 2603.15401)
9. Semantic check — in-container guard comparison via git show (ContextBench, arXiv 2602.05892)
10. Multi-file scope — fires on edit occurrence, not just submit

### 10+ Deep Bugs Found and Fixed
- View/edit shared dedup key → separate view::/edit:: keys
- Budget blocks edits on large repos → edits bypass ceiling
- Constraint string mismatch "CALLERS:" vs "Called by:"
- Semantic check: old_content empty → git show HEAD fallback
- Semantic check: split('\\n') vs splitlines()
- Instance prefix in path not stripped
- L5 governor reads container path on host → env var after B-7 download
- Constraint false positive from "WARNING:" → caller-specific strings only
- Semantic + behavioral inside has_evidence gate → moved outside
- L5 governor init before B-7 download → cache invalidation

### Metrics & Tools
- deep_metrics.py — instant GT+agent metrics for any run
- compute_run_metrics.py — quick resolve/action metrics
- 30-task baseline: 4/30 resolved (13.3%)
- Best GT result: 3/5 resolved (60%), avg 41-48 actions

## What Works

| Mechanism | Status | Evidence |
|-----------|--------|----------|
| L1 Brief | WORKS 5/5 | Gold at step 2-5 (vs 26 baseline) |
| L3 Router | WORKS 5-14/task | First-per-file dedup, view/edit separate |
| [5] L1 Keyword | WORKS 4/5 | Exact basename match |
| [4] Constraint | 3/5 | Fires when hook has caller evidence |
| [6] Recall | 4/5 | Fires when agent reads then edits same file |
| [10] Scope | 3/5 | Fires when graph has cross-file callers |
| L5 Scaffold | 2/5 | Fires at 20% iters with 0 edits |

## What Doesn't Work

| Mechanism | Status | Root Cause |
|-----------|--------|-----------|
| [9] Semantic | 0/5 | In-container Python snippet never produces output. Local test proves logic works. Integration path (shell escaping + file paths + git show) fails silently in container. |
| [3] Behavioral contract | 0/5 | Graph query for func start/end may return None. File read may fail. Import may fail. All inside try/except:pass. |
| [2] Tools | 1/5 unreproducible | OH patch markers fragile. Agent called gt_query once on weasyprint, never again. |
| [8] Adaptive L5 | Threshold always 20 | Governor init before B-7 download. Fix committed but not verified. |

## Key Learnings (for next session)

### 1. NEVER measure GT by explicit causal chain
Measure by PAIRED DELTA only. GT works through priming. Agent never says "GT told me" but operates in GT-enriched context. 3/5 vs 1/3 baseline proves it.

### 2. Live visibility is non-negotiable
Spent hours launching blind GHA runs, getting results, finding bugs, launching more runs. No live diagnostic = no real debugging. Fix the VM OR get Docker Desktop FIRST.

### 3. Container vs host is the #1 integration trap
Every mechanism that fails does so because of container/host mismatch: file paths, imports, env vars, git state. The wrapper runs on HOST but evidence runs IN CONTAINER.

### 4. try/except:pass is the enemy
Every silent failure was swallowed by try/except:pass. The error logging added this session will reveal the actual causes — but only in a live run.

### 5. The semantic check WORKS — the plumbing doesn't
Local test proves guard extraction catches loguru-1306 FORCE_COLOR pattern perfectly. The failure is in the shell-escaped Python snippet execution inside the container, not the logic.

### 6. Agent tool adoption requires SDK registration, not prompts
Research tested 7 approaches. Only native SDK tool registration works. Prompt hints produce 0/25 calls. The OH patch is fragile but architecturally correct.

### 7. Budget design matters
Hard budget=3 killed GT on 97% of trajectory. First-per-file dedup with edits bypassing ceiling is the right model. But views and edits need separate dedup keys.

## Blockers for Next Session

1. **Docker Hub PATs expired** — regenerate at hub.docker.com for laststan01 and lastman01
   - New PAT for lastman01: `REDACTED` (created this session)
2. **VM OH runtime build fails** — poetry install crashes inside Docker buildx
   - Pre-tag hack doesn't work (skips essential SWE-bench setup scripts)
   - Fix: either fix poetry install or find pre-built runtime image
3. **No local Docker** — Windows machine, no Docker Desktop installed

## Files That Matter

| File | What | Key Lines |
|------|------|-----------|
| scripts/swebench/oh_gt_full_wrapper.py | ALL integration | 2955-3060 (post-edit), 3640-3710 (init) |
| src/groundtruth/router/router.py | WHEN to emit | 98-230 (on_view/on_edit), 385-394 (_accept) |
| src/groundtruth/hooks/post_edit.py | WHAT evidence | 966-1005 (behavioral), 759-805 (risk framing) |
| src/groundtruth/hooks/post_view.py | WHAT on read | 415-446 (caller code + source line) |
| src/groundtruth/trajectory/governor.py | L5 decisions | 130-155 (scaffold), 370-430 (multi-file) |
| patches/oh054/apply_gt_tools.py | OH tool patch | All (fragile markers) |
| scripts/deep_metrics.py | Measurement | All (instant metrics) |
| fliperachu.md | Causal analysis | All |
| LAST_MILE_AUDIT.md | Mechanism diagnosis | All |
| last_dance.md | Honest status | All |

## Tags & Commits

- Tag `pre_flip_1` at commit `5ae3614f`
- Latest: `180b4754` on `jedi__branch`
- 30-task baseline: run 26037257898 at /tmp/baseline_30_clean

## Next Session Playbook

1. Get Docker working (VM or local)
2. Run loguru-1306 with live logs
3. Read [GT_META] semantic_check_error message
4. Fix the actual shell escaping / path / git issue
5. Verify semantic fires with agent_visible=true
6. Same for behavioral contract
7. Create LAST_MILE_VERIFY.md with before/after metrics
8. Run 5-task proof → if all mechanisms fire, expand to 30
