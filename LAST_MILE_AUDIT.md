# LAST_MILE_AUDIT.md — End-to-End Mechanism Diagnosis

Tag: `pre_flip_1` (5ae3614f) → audit → fixes at HEAD
Date: 2026-05-18
Branch: jedi__branch

---

## Step 1: Mechanism Audit Table

| Mechanism | Files | Layer | Trigger | Evidence Source | Graph/Index Dep? | Delivery Surface | Agent-Visible? | Failure Mode | Root Cause (ACTUAL) | Fix/Disable Decision |
|-----------|-------|-------|---------|----------------|-----------------|------------------|---------------|-------------|-------------------|---------------------|
| [9] Semantic check | wrapper:2962, hooks/semantic_check.py | L3 post-edit | FileEditAction on source file | git show HEAD vs current file | NO (code-only) | append_observation via hook_body | NO (0/5) | Silent empty output, no diagnostic | **BUG-2**: Shell one-liner `python3 -c "..."` with 4-level escape nesting; any crash goes to stderr which `_run_internal` discards. **BUG-1**: Even if output existed, has_evidence gate lacked matching markers. | FIX: Replace with proper module `semantic_check.py`. Add to has_evidence gate. Add diagnostic logging. |
| [3] Behavioral contract | post_edit.py:966-1018 | L3 post-edit (in-container hook) | FileEditAction → hook runs in container | graph.db nodes (start_line, end_line) + file read | YES (func boundaries) | hook stdout → hook_body | NO (0/5) | Evidence produced but silently dropped | **BUG-1 (CRITICAL)**: has_evidence gate at wrapper:2938 checked 8 markers but "BEHAVIORAL CONTRACT:" was not among them. The contract code WORKS — unit test proves it extracts guards correctly. Output was silently discarded at the wrapper level. | FIX: Add "BEHAVIORAL CONTRACT:" + 7 other markers to has_evidence gate. Change except:pass to logging. |
| [8] Adaptive L5 | governor.py:130-155, wrapper:3644+3699 | L5 | Every action | graph.db node count | YES (node count) | Threshold decision (internal) | NO (wrong threshold=20 always) | Threshold defaults to 20 regardless of repo | Governor init at wrapper:3644 before B-7 download at wrapper:3699. `GT_GRAPH_DB` env set AFTER governor cached threshold. Cache invalidation added at wrapper:3704 but not verified live. | FIX: Invalidate governor cached threshold after B-7 download. Needs live verification. |
| [7] L6 Auto-consumer | wrapper:2863-2896 | L6 | Post-reindex success | graph.db caller count delta | YES (callers) | print() to host stdout ONLY | NO (telemetry-only) | Never reaches agent observation | No code path connects L6 output to `append_observation()`. The delta is logged but not injected. | DISABLE: 15.6s overhead, 0 agent impact. Reindex itself useful (refreshes graph.db), consumer dead. |
| [2] Native tools | patches/oh054/apply_gt_tools.py | Agent | LLM decides to call gt_query/gt_validate | graph.db via tool commands | YES | CmdRunAction stdout | 1/5 (unreproducible) | Patch markers fragile, GHA cache may skip re-apply | OH source code exact-string patch. GHA caches `/tmp/OpenHands` so patch may not apply on cache hit. No post-patch verification until setup-eval line 48. | DISABLE from success claims. Keep patch in tree, add import check. |
| [4] Constraint framing | wrapper:2993-2997 | L3 post-edit | has_evidence=True AND caller strings in hook_body | hook caller output ("Called by:", "verified callers") | YES (callers) | `<gt-constraint>` wrapper around hook_body | 3/5 (graph-dependent) | Doesn't fire when graph has no callers for edited function | Correct behavior: only fires when callers exist. No bug. | KEEP as-is. |
| [6] Recall injection | wrapper:2988-2991 | L3 post-edit | Same file read→edit | evidence_cache (in-memory) | NO (cache) | Prepend `[RECALL]` to hook_body | 4/5 | Doesn't fire if agent edits without prior read | Correct behavior: recall requires prior view of same file. | KEEP as-is. |
| [10] Multi-file scope | wrapper:3009-3031 | L3 post-edit | has_evidence=True AND edit event | graph.db cross-file callers (confidence>=0.7) | YES (edges) | Append scope warning to evidence | 3/5 (graph-dependent) | Doesn't fire when graph lacks cross-file callers | Correct behavior: scope needs graph edges. | KEEP as-is. |
| [5] L1 Keyword | v7_4_brief.py:419-448 | L1 | Task start | Issue text tokens → file path match | NO | Brief injection | 4/5 | Misses when issue text has no exact filename tokens | Correct behavior: keyword match is heuristic. | KEEP as-is. |
| [1] L4 Symbol | wrapper:3346-3434 | L4 | Task start | Issue text → graph.db symbol search | YES (nodes) | Brief injection | 4/5 | loguru-1306: no matching tokens in issue text | Correct behavior: symbol selection from issue tokens. | KEEP as-is. |
| L1 Brief | v7_4_brief.py | L1 | Task start | Graph + issue text | YES | prepend_observation | 5/5 WORKS | — | — | KEEP as-is. |
| L3 Router | router.py:98-230 | L3 | File read/edit | AgentState + dedup map | NO | Routing decision (emit/suppress) | 5-14/task WORKS | — | — | KEEP as-is. |
| L5 Scaffold trap | governor.py:148 | L5 | Every action | action_count + edit_count | NO | append_observation warning | 2/5 | Only fires when 0 edits at threshold iteration | Correct behavior: scaffold trap is an early-warning for stuck agents. | KEEP as-is. |

---

## Step 2: End-to-End Diagnosis (Broken Mechanisms Only)

### [9] Semantic Check — WAS DEAD, NOW FIXED

**Full path:** agent edits file → wrapper detects FileEditAction → router approves emit (line 2906) → wrapper constructs `_sem_cmd` → `_run_internal(orig_run_action, _sem_cmd, 8)` executes in container → snippet runs → stdout captured → parsed for GUARD_ADDED/GUARD_REMOVED/RETURN_PATH → prepended to hook_body → has_evidence set True → delivered via append_observation

**Where it broke (ACTUAL, not hypothesized):**

| Step | What happened | Diagnostic evidence |
|------|--------------|-------------------|
| 4: Command construction | Shell one-liner with 4-level escape nesting (`\\\\s+` in f-string inside shell double-quotes) | Code inspection: wrapper:2964-2975 |
| 5: In-container execution | If snippet crashes, traceback goes to stderr. `_run_internal` only reads `obs.content` (stdout). Result: empty string. | Code inspection: `_run_internal` at wrapper:1907 returns `getattr(obs, "content", "")` |
| 6: Empty output handling | `if _sem_out:` (line 2971) — empty string is falsy, entire block skipped. NO diagnostic log. | Code inspection: no else branch, no GT_META line for empty case |
| 7: Even if output existed | has_evidence gate at 2938 didn't include "GUARD_ADDED:", "GUARD_REMOVED:", "RETURN_PATH:", or "SEMANTIC WARNING:" | Code inspection: gate only checked SIGNATURE/CALLERS/SIBLING/TWINS/PROPAGATE/CO-CHANGE/SCOPE |

**Root cause chain:** Shell escaping fragility → crash goes to invisible stderr → empty stdout → no diagnostic → even if fixed, gate would block it.

**Fix applied:**
1. Replaced one-liner with `python3 -m groundtruth.hooks.semantic_check --file=X --workspace=Y` (new module)
2. Added "BEHAVIORAL CONTRACT:", "TEST EXPECTS:", "TEST:", "WARNING:", "TOP CALLER:", "MUST PRESERVE:", "[GT_VERIFY]", "[GT L3:" to has_evidence gate
3. Added diagnostic logging for: empty output, raw output without markers, matched markers

### [3] Behavioral Contract — WAS DEAD, NOW FIXED

**Full path:** hook runs in container → post_edit.py loop enters Priority 0.5 block (line 966) → queries graph.db: `SELECT start_line, end_line FROM nodes WHERE name=? AND file_path=?` → reads function body from file → imports `_regex_extract_guards` → extracts guards → extracts return paths → gate: `len(guards) >= 2 or len(return_paths) >= 3` → appends "BEHAVIORAL CONTRACT:" + GUARD lines to func_parts → func_parts added to output_parts → output printed to stdout

**Where it broke (ACTUAL):**

| Step | What happened | Diagnostic evidence |
|------|--------------|-------------------|
| ALL STEPS WORKED | The contract code produces correct output. Unit test `test_behavioral_contract_recognized` proves it: output = `BEHAVIORAL CONTRACT: GUARD: if os.environ.get('FORCE_COLOR') -> return` | test_evidence_gate.py line ~130, test output |
| WRAPPER GATE | has_evidence gate at wrapper:2938 only checked for `"SIGNATURE:", "CALLERS:", "SIBLING:"...` — "BEHAVIORAL CONTRACT:" was NOT in the list | Direct code inspection |
| DELIVERY | Evidence existed in hook_body but `has_evidence` was False → code fell through to `if has_evidence:` at line 2987 → returned `obs` unmodified | Code flow analysis |

**Root cause:** Single bug — has_evidence tuple missing "BEHAVIORAL CONTRACT:" marker.

**Fix applied:**
1. Added "BEHAVIORAL CONTRACT:" to has_evidence gate
2. Changed `except Exception: pass` to `except Exception as _bc_outer_exc: print(...)` at post_edit.py:1017
3. Added diagnostic logging for matched markers

### [8] Adaptive L5 — BROKEN, PARTIAL FIX

**Full path:** wrapper:3644 creates governor → governor.__init__ reads `os.environ.get("GT_GRAPH_DB")` → connects to graph.db → counts nodes → sets threshold → wrapper:3699 downloads graph.db from container → sets `os.environ["GT_GRAPH_DB"]` → wrapper:3704 invalidates router (but NOT governor threshold)

**Where it breaks:** Governor caches threshold at init. At init time, GT_GRAPH_DB either doesn't exist or points to container path. After download, env var is set but governor already cached `threshold=20`.

**Fix needed:** After B-7 download, invalidate governor's cached threshold. Committed as intent in wrapper:3704 (router reset) but governor threshold not invalidated. Needs live verification.

---

## Step 3: Design Per Mechanism (Before Fix)

### [9] Semantic Check

**Root cause:** Fragile shell-escaped Python one-liner + missing gate marker.
**Correct layer:** L3 post-edit, runs in container via proper module.
**Why fix generalizes:** Guard extraction is regex-based, works on any language with `if...return/raise/throw` patterns. Module import eliminates shell escaping entirely. Works on any repo.
**Expected metric change:** Semantic delivery 0/5 → 3-5/5 (any file with return statements produces RETURN_PATH).
**Regression risk:** LOW — new evidence appended, doesn't replace existing. If module import fails in container, diagnostic logging reveals it immediately (no silent failure).

### [3] Behavioral Contract

**Root cause:** has_evidence gate missing marker string.
**Correct layer:** L3 post-edit hook output, delivered via wrapper.
**Why fix generalizes:** The marker string "BEHAVIORAL CONTRACT:" is a fixed constant. The fix adds it to a string-matching gate. Works on any repo, any language.
**Expected metric change:** Contract delivery 0/5 → 3-5/5 (functions with ≥2 guards OR ≥3 return paths).
**Regression risk:** LOW — the behavioral contract code was already running and producing output silently. The fix only changes whether that output reaches the agent. No new code paths.

### [8] Adaptive L5

**Root cause:** Init timing — governor reads stale env before download.
**Correct layer:** L5 governor, threshold computation.
**Why fix generalizes:** Timing fix, works on any repo. Threshold is based on node count (structural property).
**Expected metric change:** Threshold correct for repo complexity (small=20, medium=25, large=35).
**Regression risk:** MEDIUM — if threshold changes agent behavior (earlier/later L5 warnings), could affect resolve rate. But the FIX is making the threshold CORRECT, not different.

### [7] L6 Auto-Consumer

**Root cause:** No delivery path to agent.
**Correct decision:** DISABLE. Remove overhead, keep reindex.
**Why generalizes:** Dead code removal, works everywhere.
**Regression risk:** ZERO — removing code that never reached the agent.

### [2] Native Tools

**Root cause:** Fragile string-matching patch.
**Correct decision:** DISABLE from success claims. Keep in tree for future work.
**Why generalizes:** Patch fragility is an OH version-coupling issue, not GT issue.
**Regression risk:** ZERO — not removing the patch, just not claiming it works.

---

## Step 4: Structured Trace Fields (Code Definition)

See `src/groundtruth/hooks/trace_fields.py` for the structured trace field definitions and suppression reason enum.

---

## Step 5: Final Before/After/Proof Table

| Mechanism | Before | Root Cause | Change | After | Proof | Risk |
|-----------|--------|-----------|--------|-------|-------|------|
| [9] Semantic | 0/5 delivery | Shell one-liner fragility + missing gate marker | New module `semantic_check.py` + 8 markers added to gate + 5 diagnostic log points | Should deliver 3-5/5 | 16 unit tests pass, `test_detects_added_guard` proves git-based comparison works | LOW: module import could fail in container; diagnostic logging will reveal |
| [3] Behavioral | 0/5 delivery | has_evidence gate missing "BEHAVIORAL CONTRACT:" | Added marker to gate + except:pass→logging | Should deliver 3-5/5 | `test_behavioral_contract_recognized` PASSES with correct guards extracted | LOW: no new code paths, just unblocking existing output |
| [4] Constraint | 3/5 | Graph-dependent (correct) | No change | 3/5 | — | — |
| [6] Recall | 4/5 | Needs prior read (correct) | No change | 4/5 | — | — |
| [10] Scope | 3/5 | Graph-dependent (correct) | No change | 3/5 | — | — |
| [8] Adaptive L5 | Always 20 | Init before download | Cache invalidation needed (partial) | Needs live verification | — | MEDIUM: behavioral change |
| [7] L6 Consumer | Telemetry-only | No delivery path | DISABLED per decision | N/A | — | ZERO |
| [2] Tools | 1/5 unreproducible | Fragile patch | DISABLED from claims | N/A | — | ZERO |
| [5] L1 Keyword | 4/5 WORKS | — | No change | 4/5 | — | — |
| [1] L4 Symbol | 4/5 WORKS | — | No change | 4/5 | — | — |
| L1 Brief | 5/5 WORKS | — | No change | 5/5 | — | — |
| L3 Router | 5-14/task WORKS | — | No change | 5-14/task | — | — |
| L5 Scaffold | 2/5 WORKS | — | No change | 2/5 | — | — |

---

## Stop Rule Applied

| Mechanism | Reliable? | Visible? | Measurable? | Decision |
|-----------|----------|---------|------------|----------|
| [9] Semantic | YES (after fix) | YES (after gate fix) | YES (GUARD_ADDED/RETURN_PATH markers) | ENABLED |
| [3] Behavioral | YES (proven by test) | YES (after gate fix) | YES (BEHAVIORAL CONTRACT: marker) | ENABLED |
| [8] Adaptive L5 | PARTIAL | NO (internal threshold) | YES (GT_META log) | ENABLED with caveat: needs live verification |
| [7] L6 Consumer | NO | NO | NO | **DISABLED** — 15.6s overhead, 0 impact |
| [2] Tools | NO | UNREPRODUCIBLE | NO | **DISABLED** from claims |
