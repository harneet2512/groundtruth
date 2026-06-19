# GT Integration Deficiency Report & Build Prompt

**Date:** 2026-05-08
**Branch:** `oh-gt-combined` (pushed to origin)
**Run:** 30 exact Phase 4 tasks, OH + GT + Qwen3-Coder, SWE-bench-Live Lite
**Result:** 6/30 resolved (20%) — essentially OH baseline, GT contribution near zero
**Comparison:** SWE-agent + GT = 3/30 (10%) on same tasks
**Deep audit:** `docs/handoff/DEFICIENT_DEEP.md` (17 bugs, 9 critical, full code locations)

---

## The honest picture

GT injects ~3,400 tokens per task. 65% is noise. The agent learns to ignore
GT output by iteration 20 because 75% of evidence blocks are empty. L5 never
reaches the agent. The 6/30 resolve rate is OH doing the work, not GT.

---

## Layer-by-layer deficiencies (build list)

### L1 Brief — 3 bugs

**BUG L1-A: Double `<gt-task-brief>` wrapping**
- `v7_brief.py` outputs content already inside `<gt-task-brief>` tags
- `oh_gt_full_wrapper.py:1626` wraps it AGAIN
- Agent sees nested `<gt-task-brief><gt-task-brief>...</gt-task-brief></gt-task-brief>`
- **Fix:** Remove inner tags from `v7_brief.py` output OR remove outer wrapping in wrapper. Not both.
- **File:** `src/groundtruth/pretask/v7_brief.py` (search for `<gt-task-brief>` in render function)
- **File:** `scripts/swebench/oh_gt_full_wrapper.py:1626`

**BUG L1-B: 6 of 14 brief lines are structural overhead**
- Section headers (`CANDIDATE CLUSTER:`, `CONTRACT:`, `IMPLEMENTATION PATTERN:`, `CONSTRAINTS:`) and blank separators carry zero information
- **Fix:** Flatten to a compact format: "Edit targets: X, Y, Z. Contract: ... Pattern: ..."
- **File:** `src/groundtruth/pretask/v7_brief.py` (render function)

**BUG L1-C: Fallback brief is useless filler**
- When brief generation fails, wrapper emits "GT graph built inside the task container..." with repo_root/graph_db paths
- Agent can't use this. It's ~80 tokens saying "GT exists"
- **Fix:** Replace with empty string — inject nothing when there's no real content
- **File:** `scripts/swebench/oh_gt_full_wrapper.py:1563-1569`

---

### L2 Hybrid Fusion — 2 bugs

**BUG L2-A: Telemetry tag injected into agent-visible brief**
- `<gt-pretask layer="L2" fusion="rrf" fused_candidates="5" wall_ms="234" signals="..." />` is appended to the brief
- This is machine telemetry. The agent sees `wall_ms="234"` and can't use it
- **Fix:** Remove from agent-visible brief. Keep in telemetry JSON only.
- **File:** `scripts/swebench/oh_gt_full_wrapper.py:1558-1559` (the line `brief = f"{brief}\n\n{l2_tag}"`)

**BUG L2-B: Localization accuracy never verified**
- The RRF fusion selects candidate files. We never checked if they match the actual gold edit files
- This is the single highest-ROI unknown. If candidates are wrong, the agent starts from bad localization
- **Fix:** For the 6 resolved tasks, check if brief's candidate files include the files the agent actually edited. For unresolved, check if the gold file was in the candidates.
- **Not a code fix — research task**

---

### L3 Post-edit — 5 bugs (MOST CRITICAL LAYER)

**BUG L3-A: 75% of evidence blocks are empty — still injected (HIGHEST PRIORITY)**
- Across 20 tasks: average 4.5 real / 13.7 empty evidence blocks per task
- Empty blocks look like:
  ```xml
  <gt-evidence trigger="post_edit:src/foo.py">
  <gt-reindex command="...">indexed 1 files in 45ms</gt-reindex>
  [GT_STATUS] no_evidence:abstention_filtered
  Verify: gt_validate src/foo.py
  </gt-evidence>
  ```
- ~60 tokens of NOTHING per block × 13.7 blocks = ~820 tokens/task of noise
- **Cry-wolf effect:** Agent learns to ignore ALL `<gt-evidence>` blocks including the 25% with real data
- **Fix:** Before injecting, check if hook output contains real evidence tags (`[GT_CHANGE]`, `[GT_CONTRACT]`, `[GT_PATTERN]`, `[GT_STRUCTURAL]`, `[GT_SEMANTIC]`, `[VERIFIED]`, `[POSSIBLE]`). If none found, DO NOT INJECT. Emit nothing.
- **File:** `scripts/swebench/oh_gt_full_wrapper.py:1133-1153` (the post_edit evidence block builder)
- **Exact check to add before line 1133:**
  ```python
  has_real_evidence = any(tag in hook_out for tag in (
      "[GT_CHANGE]", "[GT_CONTRACT]", "[GT_PATTERN]",
      "[GT_STRUCTURAL]", "[GT_SEMANTIC]", "[VERIFIED]", "[POSSIBLE]"
  ))
  if not has_real_evidence:
      # Still record telemetry but don't inject into agent context
      return append_observation(obs, "")  # or just: return obs
  ```

**BUG L3-B: Reindex output always visible to agent**
- Every evidence block includes `<gt-reindex>indexed 1 files in 45ms</gt-reindex>`
- ~30 tokens × 18 edits = ~540 tokens/task of operational telemetry
- **Fix:** Remove `<gt-reindex>` from agent-visible evidence. Keep the reindex command running but don't include its output in the XML block.
- **File:** `scripts/swebench/oh_gt_full_wrapper.py:1135-1137` (remove the reindex sub-block from the evidence string)

**BUG L3-C: `Verify: gt_validate <file>` spam on every edit**
- Appended to EVERY post-edit evidence block
- Agent sees it 20+ times per task, never uses it (L4 tool usage = 0)
- **Fix:** Remove entirely. If we want to suggest gt_validate, do it once in the brief, not on every edit.
- **File:** `scripts/swebench/oh_gt_full_wrapper.py:1150` (the line `f"Verify: gt_validate {rel_p}\n"`)

**BUG L3-D: Abstention threshold too high (0.55)**
- Filters out findings at 0.50-0.55 confidence that might be useful
- **Fix:** Lower to 0.40 or make configurable
- **File:** `src/groundtruth/hooks/post_edit.py:506` (min_confidence)

**BUG L3-E: Five evidence families, most never produce on framework-heavy repos**
- CHANGE, CONTRACT, PATTERN, STRUCTURAL, SEMANTIC all run on every edit
- For cfn-lint (linter rules dispatched by framework), families 2-5 produce nothing because rules have few graph callers
- This is by design (honest abstention) but the noise comes from BUG L3-A injecting the "nothing"
- **Fix:** Covered by L3-A fix (don't inject when nothing found)

---

### L3b Post-view — 2 bugs

**BUG L3b-A: Empty evidence blocks still injected (same as L3-A)**
- When post_view finds no coupling, it injects `[GT_STATUS] no_evidence:no_class_coupling` + `Try: gt_search function <stem>`
- Same cry-wolf problem as L3
- **Fix:** Same as L3-A — don't inject empty blocks
- **File:** `scripts/swebench/oh_gt_full_wrapper.py:1029-1038`

**BUG L3b-B: `Try: gt_search` suggestion repeated on every empty view**
- Useful the first time, spam after that
- **Fix:** Track if suggestion was already given, suppress on repeat
- **File:** `scripts/swebench/oh_gt_full_wrapper.py:1033-1035`

---

### L4 Prefetch + Tools — 3 bugs

**BUG L4-A: Tool footer useless (agent never calls tools)**
- 4-line tool description injected but L4 tool usage = 0 across all tasks
- **Fix:** Remove tool footer entirely OR reduce to single line
- **File:** `scripts/swebench/oh_gt_full_wrapper.py:506-524` (`render_l4_tool_footer`)

**BUG L4-B: Symbol selection stop-word list too small**
- Generic identifiers like `Rule`, `Check`, `Error`, `Config` pass through and match wrong graph nodes
- **Fix:** Expand stop-word list for issue text extraction
- **File:** `scripts/swebench/oh_gt_full_wrapper.py:1259-1264`

**BUG L4-C: Prefetch evidence not verified for relevance**
- We select issue-seeded symbols and run gt_query, but never check if the evidence actually relates to the bug
- **Not a code fix — needs L2 localization audit first**

---

### L5 Gate — 3 bugs (COMPLETELY BROKEN)

**BUG L5-A: Advisory fires 4x per task, none reach the agent**
- Fires on: finish event (too late), last_visible_observation (retroactive), submit-like commands (patch extraction), second git diff
- Agent NEVER sees it before submitting — `Advisory in agent observations (not patch): 0` across ALL tasks
- **Fix:** Remove all current L5 injection points. They cannot work in OH's event model.
- **File:** `scripts/swebench/oh_gt_full_wrapper.py:1155-1206` (all 4 injection points)

**BUG L5-B: Advisory corrupted patches (FIXED in latest commit)**
- Advisory text was appended to `git diff --cached` output, corrupting 5/30 patches
- **Status:** Fixed. `is_patch_extract` guard added at line 1190.
- One of those 5 (beets-5495) resolved after fix, confirming the corruption was losing points.

**BUG L5-C: No architectural path to reach the agent before submit**
- OH's submit is atomic — no pre-submit hook
- **Future fix:** Inject advisory at 80% iteration budget (iteration 80 of 100) as a "checkpoint" — gives agent 20 iterations to self-correct
- **Requires:** Access to OH's iteration counter from the wrapper
- **File:** `scripts/swebench/oh_gt_full_wrapper.py` — needs new logic in `patched_run_action` to track iteration count

---

### L6 Reindex — CLEAN (0 bugs)

Works correctly. Only layer with no issues.

---

## Cross-cutting issues

### ISSUE X-1: Evidence Block Fatigue (most damaging)
Agent sees 18+ `<gt-evidence>` blocks per task. 75% are empty. By iteration 20,
the agent has learned `<gt-evidence>` = noise. When real evidence appears at
iteration 45, agent skips it. **This is worse than sending nothing.**

### ISSUE X-2: No evidence aggregation
Each evidence block is independent. Agent never gets a summary across all edits.
18 scattered XML blocks across 100 iterations — impossible to synthesize.

### ISSUE X-3: Token budget
~3,410 tokens/task from GT. ~2,200 (65%) is noise. Not catastrophic for 128K
context, but the attention cost is worse than the token cost.

---

## Priority-ordered fix list (40 min total)

| # | Fix | Effort | Token saved | Impact |
|---|-----|--------|-------------|--------|
| 1 | Don't inject empty L3/L3b evidence | 10 min | ~1,200/task | **CRITICAL** — stops cry-wolf |
| 2 | Remove reindex from agent evidence | 5 min | ~540/task | High |
| 3 | Remove gt_validate spam | 2 min | ~270/task | Medium |
| 4 | Fix double gt-task-brief wrapping | 5 min | ~30/task | Medium (attention) |
| 5 | Remove L2 telemetry tag from brief | 2 min | ~30/task | Low |
| 6 | Remove L5 entirely (or redesign) | 10 min | ~480/task | Medium |
| 7 | Remove/shrink tool footer | 2 min | ~40/task | Low |
| 8 | Replace fallback brief with empty | 2 min | ~80/task | Low |
| **Total** | | **~40 min** | **~2,670/task** | |

### Research task (highest ROI, not a code fix):
**Audit L2 localization accuracy.** For each of the 30 tasks, check if the
brief's top-3 candidate files include the actual gold edit file. If L2 is
pointing at wrong files, that's the root cause of most unresolved tasks.

---

## Files to modify

All changes are in 3 files:
1. `scripts/swebench/oh_gt_full_wrapper.py` — fixes 1, 2, 3, 5, 6, 7, 8
2. `src/groundtruth/pretask/v7_brief.py` — fix 4 (double wrapping)
3. `src/groundtruth/hooks/post_edit.py` — fix for L3-D (abstention threshold)

---

## Verification protocol

After fixing, run 1-task smoke (loguru-1297) and check:
1. `grep -c 'gt-evidence' output.jsonl` — should be < 5 (was 17)
2. `grep -c 'no_evidence' output.jsonl` — should be 0 (was ~13)
3. `grep -c 'gt-advisory' output.jsonl` — should be 0
4. `grep -c 'gt-reindex' output.jsonl` — should be 0 in agent-visible content
5. `grep -c 'gt_validate' output.jsonl` — should be ≤ 1 (in brief only)
6. `grep -c 'gt-task-brief' output.jsonl` — should be exactly 2 (open + close tag, once)
7. No `<gt-task-brief>` nesting

Then run the same 30-task Phase 4 set and compare resolve rate.

---

## Current state of the code

**Branch:** `oh-gt-combined` on `https://github.com/harneet2512/groundtruth.git`
**Latest commit:** `4c83006` — includes the L5 advisory patch fix + both deficiency docs
**Deployed on:**
- gt-t0: `/home/ubuntu/Groundtruth/` (git clone of oh-gt-combined)
- gt-v1: `/home/ubuntu/Groundtruth/` (git clone of oh-gt-combined)

**OH setup:**
- gt-t0: `/home/ubuntu/OpenHands-0.54.0/` with `.venv`, config.toml has `[llm.qwen3]`
- gt-v1: `/home/ubuntu/OpenHands-0.54.0/` with `.venv`, config.toml copied from OpenHands/
- LiteLLM proxy on both VMs at port 4000, master_key `sk-gt-local`

**Phase 4 task IDs (for re-verification):**
gt-t0 (20): aiogram__aiogram-1594, aws-cloudformation__cfn-lint-3789, 3798, 3821, 3854, 3856, 3862, 3866, 3875, 3890, 4002, 4023, 4032, beancount__beancount-931, beetbox__beets-5495, beeware__briefcase-2075, beeware__briefcase-2085, bridgecrewio__checkov-6893, 6895, 7002

gt-v1 (10): arviz-devs__arviz-2413, aws-cloudformation__cfn-lint-3779, 3805, 4016, delgan__loguru-1306, kozea__weasyprint-2303, pydata__xarray-9760, 9971, pylint-dev__pylint-10044, pypa__twine-1225

**Phase 4 SWE-agent baseline on these tasks:** 3/30 resolved (10%), 19/30 patched (63%)
**Current OH+GT on these tasks:** 6/30 resolved (20%), 29/30 patched (97%)
**After fixes target:** 8-13/30 (27-43%)
