# LAST_MILE_AUDIT.md — End-to-End Mechanism Diagnosis

Tag: `pre_flip_1` (5ae3614f) → this audit
Date: 2026-05-18

## Audit Table

| Mechanism | Files | Layer | Trigger | Evidence Source | Graph Dep? | Delivery Surface | Agent-Visible? | Status | Root Cause | Decision |
|-----------|-------|-------|---------|----------------|-----------|------------------|---------------|--------|------------|----------|
| [9] Semantic | wrapper:2965 | L3 post-edit | FileEditAction | git show HEAD vs current | NO (code-only) | append_observation | NO (0/5) | DEAD | try/except:pass swallows all errors. No logging. | FIX: add error logging, test in container |
| [3] Behavioral | post_edit.py:966 | L3 post-edit (hook) | FileEditAction | graph.db nodes + file read | YES (func start/end) | hook stdout | NO (0/5) | DEAD | Silent graph query fail + possible import fail in container | FIX: add logging at each step |
| [8] Adaptive L5 | governor.py:133 | L5 | every action | graph.db node count | YES | threshold decision | NO (wrong threshold) | BROKEN | Governor init at wrapper:3644 BEFORE B-7 download at wrapper:3699 | FIX: move init after download |
| [7] L6 Consumer | wrapper:2886 | L6 | post-reindex | graph.db caller count | YES | print() only | NO (telemetry) | DEAD | Never injects into observation | DISABLE: 15.6s overhead, 0 impact |
| [2] Tools | patches/oh054 | Agent | LLM decides | gt_query/gt_validate | YES | CmdRunAction | 1/5 (unreproducible) | FRAGILE | OH patch markers fragile + GHA cache | DISABLE: not reliable enough to claim |
| [4] Constraint | wrapper:3013 | L3 post-edit | has_evidence=True | hook caller output | YES (callers) | append_observation | 3/5 | WORKS | Graph quality dependent | KEEP as-is |
| [6] Recall | wrapper:2555,3007 | L3 post-edit | same file read→edit | evidence_cache | NO (cache) | prepend to hook_body | 4/5 | WORKS | Needs prior read of same file | KEEP as-is |
| [10] Scope | wrapper:3028 | L3 post-edit | has_evidence=True | graph.db cross-file callers | YES | append to evidence | 3/5 | WORKS | Graph quality dependent | KEEP as-is |
| [5] L1 Keyword | v7_4_brief.py:427 | L1 | task start | issue text + filenames | NO | brief injection | 4/5 | WORKS | — | KEEP as-is |
| [1] L4 Symbol | wrapper:3346 | L4 | task start | issue text + graph nodes | YES | brief injection | 4/5 | WORKS | loguru-1306: no matching tokens | KEEP as-is |
| L1 Brief | v7_4_brief.py | L1 | task start | graph + issue text | YES | prepend to instruction | 5/5 | WORKS | — | KEEP as-is |
| L3 Router | router.py:98-230 | L3 | file read/edit | AgentState + dedup | NO | routing decision | 5-14/task | WORKS | — | KEEP as-is |
| L5 Scaffold | governor.py:148 | L5 | every action | action count + edit count | NO | append_observation | 2/5 | WORKS (sometimes) | Only fires when 0 edits at threshold | KEEP as-is |

---

## End-to-End Diagnosis Per Broken Mechanism

### [9] Semantic Check — DEAD (0/5 always)

**Path:** agent edits file → wrapper detects FileEditAction → router approves emit → wrapper runs `_sem_cmd` Python snippet in container → snippet compares git show HEAD vs current → extracts guards → outputs GUARD_ADDED/GUARD_REMOVED/RETURN_PATH → wrapper parses output → prepends to hook_body → sets has_evidence=True → delivers to agent

**Where it breaks:** Step 4 (snippet execution). The `try: ... except Exception: pass` at wrapper:3001-3002 swallows ALL errors. Possible failures:
1. `git show HEAD:{file}` fails — container may not have git history initialized
2. `open('{workspace_root}/{file}')` fails — path mismatch
3. Regex syntax error in the shell-escaped Python -c command
4. `_run_internal` returns garbage (ANSI codes, prompts mixed in)
5. Output doesn't match `GUARD_ADDED:/GUARD_REMOVED:/RETURN_PATH:` prefix format

**Fix plan:**
```python
except Exception as _sem_exc:
    print(f"[GT_META] semantic_check_error: {type(_sem_exc).__name__}: {_sem_exc}", flush=True)
```
Then: run one task, read the error, fix the actual cause.

---

### [3] Behavioral Contract — DEAD (0/5 always)

**Path:** hook runs in container → post_edit.py Priority 0.5 block → queries graph.db for func start_line/end_line → reads function body from file → imports `_regex_extract_guards` → extracts guards → extracts return paths → appends to func_parts

**Where it breaks:** Unknown — all steps are inside `try: ... except Exception: pass` at post_edit.py:1005. Possible failures:
1. `_sq_bc.connect(db_path)` — db_path is container path, may not exist or be stale
2. `SELECT start_line, end_line FROM nodes WHERE name = ? AND file_path = ?` — func_name might not match (case, qualified name)
3. `func_start` is None → the `if func_start and func_end:` gate blocks everything
4. `from groundtruth.evidence.change import _regex_extract_guards` — this import IS on the host PYTHONPATH (GT source is uploaded to /tmp/groundtruth in container), but the hooks run via `python3 -m groundtruth.hooks.post_edit` which sets PYTHONPATH to include GT source

**Fix plan:** Add logging at each step:
```python
print(f"[GT_META] behavioral: db={db_path} func={func_name} file={file_path}", flush=True)
print(f"[GT_META] behavioral: start={func_start} end={func_end}", flush=True)
```
Then check whether the graph query returns valid start/end lines.

---

### [8] Adaptive L5 — BROKEN (wrong threshold)

**Path:** governor.__init__ reads GT_GRAPH_DB env → connects to graph.db → counts nodes → sets threshold (20/25/35) → caches in `_cached_scaffold_threshold`

**Where it breaks:** Governor init at wrapper:3644 happens BEFORE B-7 download at wrapper:3699. At init time, `os.environ["GT_GRAPH_DB"]` is not set (or still `/tmp/gt_index.db` which is container path, doesn't exist on host). So `os.path.exists(_gdb)` returns False → threshold defaults to 20.

The fix at wrapper:3704 (`os.environ["GT_GRAPH_DB"] = _local_db`) runs AFTER governor already cached the threshold.

**Fix plan:** After B-7 download succeeds, invalidate the governor's cached threshold:
```python
if config._l5_governor and hasattr(config._l5_governor, '_cached_scaffold_threshold'):
    delattr(config._l5_governor, '_cached_scaffold_threshold')
```

---

### [7] L6 Auto-Consumer — DEAD (telemetry only)

**Path:** successful L6 reindex → wrapper queries caller count → stores in evidence_cache → prints [GT_META] log

**Where it breaks:** Never reaches the agent. The caller delta is printed to logs but not injected into any observation. No code path connects L6 output to agent-visible delivery.

**Decision:** DISABLE. Remove the 1.2s per-reindex overhead of the caller count query. L6 reindex itself is useful (refreshes graph.db for L3/L4), but the auto-consumer adds no value.

---

### [2] Native Tools — FRAGILE (unreproducible)

**Path:** patches/oh054/apply_gt_tools.py patches OH source at GHA install time → adds gt_query/gt_validate to CodeActAgent._get_tools() → agent sees tools in action space → agent calls gt_query → dispatches to CmdRunAction

**Where it breaks:** The patch uses exact string markers that may not match OH's source. GHA caches the OH installation, so the patch may not re-apply on subsequent runs. Result: 1/5 on one run, 0/5 on next.

**Decision:** DISABLE from success claims. The patch is left in place but we don't count tool calls as a working mechanism until the patch application is verified with a post-patch import check.

---

## Design: What To Fix (Ordered)

### A. Observability (no code logic changes)

**Files:** `scripts/swebench/oh_gt_full_wrapper.py`, `src/groundtruth/hooks/post_edit.py`

1. Replace ALL `except Exception: pass` in semantic/behavioral blocks with logged exceptions
2. Add `[GT_META] semantic_check:` and `[GT_META] behavioral_contract:` trace lines
3. Add structured suppression reasons to every mechanism path

**Why generalizes:** Observability is infrastructure. Works on any repo, any model.
**Metric change:** No resolve change. But we'll SEE why mechanisms fail.
**Regression risk:** Zero (logging only).

### B. Semantic + Behavioral Contract

**Files:** `scripts/swebench/oh_gt_full_wrapper.py:2965`, `src/groundtruth/hooks/post_edit.py:966`

After observability reveals the actual error:
1. Fix the specific failure (likely path/import/git issue)
2. Test with a known fixture (loguru-1306: FORCE_COLOR guard change)

**Why generalizes:** Guard extraction is regex-based, language-agnostic. Return path extraction scans for `return` keyword.
**Metric change:** Sem 0/5 → 3-5/5. BhvCt 0/5 → 3-5/5.
**Regression risk:** Low (new evidence, not replacing existing).

### C. Adaptive L5 Init Timing

**File:** `scripts/swebench/oh_gt_full_wrapper.py:3644-3710`

Move governor init AFTER B-7 download, or invalidate cached threshold after download.

**Why generalizes:** Timing fix. Works on any repo.
**Metric change:** L5 threshold correct for repo complexity.
**Regression risk:** Low (only changes when threshold is computed, not what it does).

### D. L6/Tools Disable

**Files:** `scripts/swebench/oh_gt_full_wrapper.py:2886`, `patches/oh054/`

1. Remove L6 auto-consumer caller count query (save 15.6s/task)
2. Keep tool registration but add verification step; don't claim as working

**Why generalizes:** Removing dead code reduces overhead.
**Metric change:** -15.6s per task latency. No resolve change.
**Regression risk:** Zero (removing unused code).

### E. Gating/Noise Control

After B fixes semantic/behavioral, verify they don't over-fire:
- Only emit when guards >= 2 OR return paths >= 3 (non-trivial functions)
- Don't emit on test files
- Don't emit after agent calls finish

**Why generalizes:** Quality gates are structural.
**Metric change:** Prevents noise regression.
**Regression risk:** May suppress legitimate evidence on simple functions. Gate threshold is conservative.
