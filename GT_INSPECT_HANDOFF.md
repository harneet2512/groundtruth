# GT + Inspect Integration Handoff — 2026-05-19

## What Exists

### Branches
| Branch | What's on it |
|--------|-------------|
| `inspect_urself` | Inspect adapter (task.py, tools.py, gt_solver.py), GHA workflows, passthrough scorer |
| `jedi__branch` | All GT bug fixes (BUG-1 through BUG-9), OH wrapper, post_edit.py, post_view.py, semantic_check.py, governor.py |
| `master` | GHA workflow discovery only |

### Inspect Adapter Files (on `inspect_urself`)
| File | What | Status |
|------|------|--------|
| `adapters/inspect/task.py` | Task definitions: `swebench_gt_baseline` (no GT) + `swebench_gt` (with GT) | Working |
| `adapters/inspect/tools.py` | 4 GT @tool functions (brief, trace, impact, validate) — pull-based | Working |
| `adapters/inspect/hooks.py` | on_sample_init / on_sample_end lifecycle hooks | Written, NOT wired |
| `adapters/inspect/gt_solver.py` | Full GT solver — monkey-patches execute_tools for in-place augmentation | Written, UNTESTED |
| `adapters/inspect/README.md` | Usage docs | Written |
| `.github/workflows/inspect_cascade.yml` | 1→29 cascade with harness eval | Working |
| `.github/workflows/inspect_single_task.yml` | Single task workflow | Working |
| `scripts/swebench/convert_inspect_to_predictions.py` | Extract patches from eval logs to predictions.jsonl | Working |

### GT Hook Files (on `jedi__branch`)
| File | Lines | CLI | What |
|------|-------|-----|------|
| `src/groundtruth/hooks/post_edit.py` | 2112 | `python -m groundtruth.hooks.post_edit --root=/testbed --db=/tmp/graph.db --file=X --quiet` | L3: callers, contracts, behavioral contract, test assertions |
| `src/groundtruth/hooks/post_view.py` | 640 | `python -m groundtruth.hooks.post_view --root=/testbed --db=/tmp/graph.db --file=X` | L3b: callers, callees, importers, navigation |
| `src/groundtruth/hooks/semantic_check.py` | 106 | `python -m groundtruth.hooks.semantic_check --file=X --workspace=/testbed` | Guard clause diff detection |
| `src/groundtruth/trajectory/governor.py` | 783 | (called from wrapper) | L5: scaffold trap, diff collapse |
| `src/groundtruth/router/router.py` | 430 | (called from wrapper) | Budget, dedup, emit/suppress |
| `src/groundtruth/pretask/v1r_brief.py` | 896 | (called from wrapper) | L1: V1R brief with BM25+graph scoring |

---

## Baseline Results

### Inspect Baseline (DeepSeek V4 Flash, thinking disabled, 100 msgs)
- **30 tasks attempted on GHA**
- **22 ran** (8 missing — Docker image pull timeout)
- **15 patches** produced
- **3 resolved** (amoffat__sh-744, beancount__beancount-931, beetbox__beets-5495)
- **3 near-misses** (cfn-lint-3855, cfn-lint-3890, pypsa-1091) — all F2P pass but P2P regressed
- **Resolve rate: 3/30 = 10%**
- Official evaluation via Microsoft SWE-bench-Live harness (GHA run `26068697189`)

### Model Config
```
model: openai/deepseek-v4-flash
base_url: https://api.deepseek.com
temperature: 1.0
top_p: 1.0
max_tokens: 65536
thinking: {"type": "disabled"}  # MANDATORY — thinking on = 4x cost, ignores temp/top_p
message_limit: 100
```

### Cost
- DeepSeek V4 Flash: $0.14/M input, $0.28/M output (thinking off)
- Per task: ~$0.03-0.10
- 30-task run: ~$1-3

---

## What Full GT Integration Means (from DECISIONS.md)

### The Architecture (Decision 16)
> "Modify tool results at action boundaries. Don't give optional tools. Weave GT into tools the agent already uses."
> — Strands SDK: 100% observation augmentation > 82.5% prompt injection

GT evidence must appear INSIDE tool results, not as separate messages or optional tools. When the agent reads a file, callers are appended to the read output. When the agent edits a file, contracts are appended to the edit confirmation.

### Layer Specifications

**L1: Brief (once, at task start)**
- Ranked candidate files from graph.db
- Top functions per file with caller counts
- Injected into system prompt or first user message
- Budget: ~240 tokens

**L3: Post-Edit (after every source file edit)**
- Priority order (stop at 1200 chars):
  1. Caller CODE lines (actual source, not just "called by X.py")
  2. Sibling function patterns
  3. Signature + return type
  4. Test assertions with expected values
  5. Behavioral contract (guard clauses)
- Plus: semantic check (GUARD_ADDED/REMOVED/RETURN_PATH via git diff)
- Plus: constraint framing (`<gt-constraint>`)
- Delivery: appended to tool result (in-place mutation of ChatMessageTool.content)

**L3b: Post-View (after every source file read)**
- Issue-relevant callers/callees ranked by keyword overlap
- Hub-penalized ranking
- Navigation hint: "→ Next: read test_file.py"
- Visited-file suppression
- Budget: 500 chars max, 5 fires max

**L4: Prefetch (once, at task start)**
- 3 queries on issue-text keywords
- Results baked into L1 brief
- Budget: 1000 chars, 30s wall timeout

**L5: Governor (event-driven)**
- Scaffold trap: agent creates scratch files without source edits → advisory
- Diff collapse: agent's changes disappear → advisory

**L6: Reindex (before every L3)**
- Incremental gt-index on edited file
- Updates graph.db so L3 evidence reflects current state

**Router: Budget/Dedup**
- First-per-file dedup (view and edit tracked separately)
- Max 5 L3b fires, max 10 L3 fires
- MD5 hash dedup for repeated evidence

---

## The Integration Problem

### Why `on_continue` Failed
Inspect's `react()` agent has an `on_continue` callback that fires every iteration. But Inspect's Task runner enforces message limits by CANCELLING the solver (raising CancelledError from a context manager). This cancellation happens INSIDE `_agent_generate()` which runs BEFORE `on_continue`. Result: `on_continue` never fires.

### Current Approach: Monkey-Patch `execute_tools`
The latest `gt_solver.py` patches `inspect_ai.model._call_tools.execute_tools` and `inspect_ai.agent._react.execute_tools` so that after tool execution, GT evidence is injected into `ChatMessageTool.content` before the results reach the agent.

**Status: UNTESTED.** The patch is written but hasn't been verified to work.

### What Needs Testing
1. Does the monkey-patch actually intercept tool calls?
2. Does `ChatMessageTool.content` mutation persist through to the model's next input?
3. Does the `_GTRouter` correctly classify edit vs view from tool output?
4. Do the graph.db queries return real data (path normalization)?

### Alternative Approaches (if monkey-patch fails)
1. **Fork react()**: Copy the 130-line react loop into gt_solver.py, add GT injection after `execute_tools()` at line 200. Most reliable but couples to Inspect version.
2. **Custom Agent**: Write a full `Agent` implementation that wraps generate+tools+GT. Clean but significant work.
3. **Pull-based only**: Keep the 4 GT tools as @tool functions. Agent calls them when it wants. No push-based augmentation. This already works (5 GT calls in loguru test) but violates Decision 16.

---

## GCP VM State

| Item | Value |
|------|-------|
| VM | `gt-diag` (e2-standard-4, us-central1-a) |
| Project | `project-3d0018fc-54e4-4a4d-97c` |
| Disk | 120 GB (68 GB free) |
| Python | 3.12 in `/home/ubuntu/inspect-venv` |
| Inspect AI | 0.3.222 |
| OpenHands | 0.54.0 (SWE-bench-Live fork at f4da691c) |
| gt-index | `/usr/local/bin/gt-index` |
| Docker | 29.1.3 + compose v2.32.4 |
| Docker images | beancount-931, beets-5495, loguru-1306 |
| DeepSeek API key | in `~/.bashrc` as DEEPSEEK_API_KEY and OPENAI_API_KEY |
| GT repo | `/home/ubuntu/Groundtruth` |
| OH repo | `/home/ubuntu/OpenHands` |
| Pre-built graph.db | `/tmp/gt_inspect/delgan__loguru-1306/graph.db` (1264 nodes), `/tmp/gt_inspect/beancount__beancount-931/graph.db` (2265 nodes) |

---

## Bug Status (from golazo_today.md)

| Bug | Description | Status | Evidence |
|-----|------------|--------|----------|
| BUG-1 | has_evidence gate missing markers | Code fixed on jedi__branch | NOT verified on Inspect (needs L3 post-edit firing) |
| BUG-2 | semantic_check shell one-liner | Code fixed, verified on OH (loguru: `mech=semantic_check visible=True`) | NOT on Inspect |
| BUG-3 | behavioral contract except:pass | Code fixed | NOT triggered (needs 2+ guard function) |
| BUG-4 | diagnostic logging | Verified (42 GT_META + 3 GT_TRACE + 9 GT_DELIVERY on OH loguru) | N/A for Inspect |
| BUG-5 | graph.db download | Verified (4827 nodes beets, 1264 nodes loguru) | Inspect: pre-built on host works |
| BUG-6 | L4 query filter | Verified (3 symbols, no builtins) | N/A for Inspect L4 |
| BUG-9 | adaptive L5 threshold | Verified fixed (threshold=25 on loguru, cache invalidated) | N/A for Inspect |
| BUG-13 | DeepSeek thinking enabled | Verified (extra_body={"thinking": {"type": "disabled"}}) | Verified on Inspect |

---

## Next Session Priorities

### 1. Test the execute_tools monkey-patch
Run the GT solver on loguru-1306 on GCP. Check `/tmp/gt_solver_debug.log` for evidence of augmentation. If it works, we have full GT on Inspect.

### 2. Fix the 8 missing GHA tasks
The cascade workflow had 8 tasks that never produced artifacts. Check if Docker image pulls timed out. Either pre-cache images or increase timeout.

### 3. Fix the 3 P2P regressions
cfn-lint-3855, cfn-lint-3890, pypsa-1091 all pass F2P but regress P2P. These are exactly the tasks GT L3 caller evidence should help with — showing the agent what callers depend on the changed function.

### 4. Scale GT run
Once GT integration works on 1 task, run the full 30-task GT arm on GHA and compare resolve rates: baseline vs GT.

### 5. OH Docker buildx (SOLVED)
Docker buildx works on gt-diag with 120GB disk. Runtime images are cached after first build. No longer a blocker.

---

## Key Files to Read First
1. `DECISIONS.md` — all locked decisions about layer architecture
2. `golazo_today.md` — bug inventory and mechanism status
3. `LAST_MILE_AUDIT.md` — per-mechanism diagnosis
4. `adapters/inspect/gt_solver.py` — current GT solver (monkey-patch approach)
5. `scripts/swebench/oh_gt_full_wrapper.py` — OH reference implementation (4000+ lines)
