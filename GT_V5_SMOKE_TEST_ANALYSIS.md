# GroundTruth v5 Smoke Test Analysis

**Date:** 2026-03-27
**Branch:** startupmode-v4
**Environment:** GCP VM `gt-eval-final` (us-central1-a)
**Agent:** OpenHands SDK v1.14.0 + Qwen3-Coder (Vertex AI, via LiteLLM proxy)
**Tasks:** 10 Django SWE-bench Lite instances (same 10 used across all v1-v5 smoke tests)
**Delivery:** Prompt-template driven (`gt_hook_v5.j2`) + base64 injection of `gt_hook.py`

---

## Executive Summary

**GroundTruth spoke for the first time in production SWE-bench evaluation.**

After 1,133 silent hook invocations across v1-v4, the v5 evidence layer produced 6 evidence lines across 3 out of 10 tasks (30% fire rate). The pattern evidence family (SiblingAnalyzer) is the first family to fire in production, detecting exception type outliers, return shape deviations, and API access pattern differences.

Resolution rate: **5/10 (50%)**, matching the v1 baseline. GT evidence did not measurably help or hurt in this small sample, but the evidence is structurally correct and informative.

| Metric | v1 (baseline) | v1 (GT write) | v4 (GT write) | **v5 (hook+prompt)** |
|--------|---------------|---------------|---------------|----------------------|
| Resolved | 5/10 | 6/10 | 4/10 | **5/10** |
| GT Spoke | 0 | 0 | 0 | **6 lines / 3 tasks** |
| Fire Rate | 0% | 0% | 0% | **30%** |
| Cost | ~$3.02 | ~$2.95 | ~$2.57 | **~$2.64** |

---

## The v1-v5 Journey: From Silence to Speech

### v1 (startupmode) — March 2026
- **What:** Self-contained `gt_tool_v4.py` (50 lines), only checked `self.attr` coupling
- **Hook delivery:** OpenHands PostToolUse HookConfig via `Conversation.__new__`
- **Result:** 389 hook invocations, **0 spoke**. Write-only was +1 over baseline (stochastic)
- **Root cause of silence:** Hook command ran but found no structural issues in Django code

### v2 (startupmode-v2) — March 2026
- **What:** Real GroundTruth package (ObligationEngine, ContradictionDetector, ConventionChecker)
- **Result:** 556 hooks, **0 spoke**
- **Root cause:** All signals check structural correctness; SWE-bench bugs are semantic, not structural

### v3 (startupmode-v3) — March 2026
- **What:** Added 3 evidence families (ChangeAnalyzer, CallerUsageMiner, SiblingAnalyzer)
- **Result:** 188 hooks, **0 spoke**
- **Root cause:** `indexer_cli.py` never populated `refs` table → contract/caller evidence blind

### v4 (startupmode-v4) — March 2026
- **What:** Fixed refs table, path separators, git safe.directory
- **Breakthrough:** Manual container test: GT spoke for first time (3 contract evidence lines)
- **Result in eval:** 174 hooks, **0 spoke**
- **Root cause:** Workspace path mismatch — hook ran at `/testbed` but edits at `/workspace/django/`

### v5 (hook+prompt) — March 2026-27
- **What:** Amalgamated `gt_hook.py` (1981 lines, all 5 families + semantic layer), prompt-template driven
- **Delivery:** Agent told to run `python3 /tmp/gt_hook.py` after each edit in prompt Phase 6.2
- **Result:** **6 GT lines across 3 tasks (30% fire rate)**
- **Why it worked:** Correct workspace path (`--root=/workspace`), agent-driven execution (not SDK hooks)

---

## Detailed Results

### Per-Task Breakdown

| Task | v5 Result | v4 Baseline | v4 GT Write | v1 Baseline | GT Evidence |
|------|-----------|-------------|-------------|-------------|-------------|
| django-11815 | **PASS** | FAIL | PASS | FAIL | (none) |
| django-12308 | FAIL | FAIL | FAIL | PASS | `5/2 siblings access value via isinstance(value) -- edit uses different` |
| django-13321 | FAIL | FAIL | FAIL | FAIL | `6/6 siblings raise NotImplementedError -- edit raises SuspiciousSession` |
| django-13448 | FAIL | FAIL | FAIL | FAIL | (none) |
| django-13551 | **PASS** | PASS | FAIL | PASS | (none) |
| django-13768 | FAIL | FAIL | FAIL | FAIL | (none) |
| django-13933 | **PASS** | PASS | PASS | PASS | (none) |
| django-14238 | **PASS** | PASS | PASS | PASS | (none) |
| django-14672 | **PASS** | PASS | PASS | PASS | `19/20 siblings return scalar -- edit returns tuple(9)` |
| django-15202 | FAIL | FAIL | FAIL | FAIL | (none) |

**Stochastic tasks:** django-11815 and django-13551 flip between PASS/FAIL across runs regardless of GT.
**Always FAIL:** django-13321, django-13448, django-13768, django-15202 — require semantic understanding beyond GT's scope.
**Always PASS:** django-13933, django-14238, django-14672 — easy enough for any approach.

### GT Evidence Analysis — Full Agent Interaction Trace

#### django-12308 (FAIL -- JSONField display in admin)

**The bug:** JSONField values display as Python dict (`{'foo': 'bar'}`) instead of valid JSON (`{"foo": "bar"}`) in readonly admin views.

**What the agent did:** Made an edit to `django/contrib/admin/utils.py`, adding an `isinstance(field, models.JSONField)` branch to `display_for_field()`.

**When GT spoke:** Immediately after the edit, the agent ran `python3 /tmp/gt_hook.py --root=/workspace/django/ --quiet --max-items=3` as instructed in Phase 6.2.

**What GT said:**
```
GT: 5/2 siblings access value via isinstance(value) -- edit uses different pattern [pattern]
```

**What this means:** SiblingAnalyzer compared the agent's edited method against sibling methods in the same function. 5 out of 7 existing branches access `value` via `isinstance(value, ...)` pattern, but the edit uses a different access pattern. This is a **true positive** -- the fix uses `field.get_prep_value(value)` which is structurally different from how other branches handle `value`.

**Did the agent act on it?** The agent ran the check a second time after another edit and got the same warning. It did NOT change its approach based on the warning. The task ultimately FAILED (the patch was close but missed a test case).

**Verdict:** True positive, informative but not actionable. The deviation IS the fix.

---

#### django-13321 (FAIL -- Session decode corruption handling)

**The bug:** Corrupted session data causes a crash instead of returning an empty session with a security warning.

**What the agent did:** Modified `django/contrib/sessions/backends/base.py` to add a `except signing.BadSignature` handler that logs a warning and returns `{}`.

**When GT spoke:** After the edit, the agent ran the structural check.

**What GT said:**
```
GT: 6/6 siblings raise NotImplementedError -- edit raises SuspiciousSession [pattern]
```

**What this means:** The session backend's `decode()` method was edited inside a class where 6 other methods (abstract interface stubs) all raise `NotImplementedError`. GT correctly detected that the edit introduces a different exception pattern (`SuspiciousSession` via `logger.warning`).

**Did the agent act on it?** No. The agent correctly recognized this was an intentional deviation -- session decode SHOULD handle errors differently from abstract stubs.

**Verdict:** True positive structurally, but a false alarm semantically. The deviation is intentional and correct. GT would benefit from recognizing abstract base methods vs concrete implementations.

---

#### django-14672 (PASS -- Migration RenameIndex reduce)

**The bug:** `RenameIndex.reduce()` doesn't handle squashing correctly, causing migration optimization failures.

**What the agent did:** Modified `django/db/migrations/operations/models.py` to add proper `reduce()` logic for `RenameIndex`.

**When GT spoke:** After editing the `reduce()` method.

**What GT said:**
```
GT: 19/20 siblings return scalar -- edit returns tuple(9) [pattern]
```

**What this means:** The `reduce()` method now returns a list of operations (tuple-like), while 19 out of 20 sibling methods in the class return scalar values. GT correctly flagged the return shape deviation.

**Did the agent act on it?** No. The agent correctly understood that `reduce()` methods in Django migrations return lists of operations by design.

**Verdict:** True positive detection, but the deviation is correct by design. The task resolved successfully. This evidence would be HIGH VALUE if an agent accidentally returned a scalar from `reduce()` -- GT would catch it.

---

### Summary: When GT Helped vs Didn't

| Scenario | GT Value |
|----------|----------|
| Agent makes edit that accidentally breaks sibling pattern | **HIGH** -- GT catches it before tests run |
| Agent makes edit that intentionally differs from siblings (the fix) | **LOW** -- True positive but not actionable |
| Agent makes edit in a method with no siblings | **NONE** -- SiblingAnalyzer has nothing to compare against |
| Agent makes edit with wrong exception type (unintentional) | **HIGH** -- GT would flag the deviation |
| Agent changes return shape accidentally | **HIGH** -- GT would catch tuple/scalar mismatch |

**The key insight:** GT's SiblingAnalyzer provides a "this is different from how the rest of the code works" signal. For SWE-bench tasks where the fix IS a deviation, this is noise. For real-world development where most deviations are bugs, this is valuable. The 300-task run will reveal the true positive-to-noise ratio.

### Hook Adoption

| Task | Hook Calls | Total Tool Calls | Hook/Tool Ratio |
|------|-----------|-----------------|----------------|
| django-11815 | 2 | 52 | 3.8% |
| django-12308 | 3 | 63 | 4.8% |
| django-13321 | 3 | 59 | 5.1% |
| django-13448 | 2 | 55 | 3.6% |
| django-13551 | 2 | 47 | 4.3% |
| django-13768 | 2 | 38 | 5.3% |
| django-13933 | 2 | 49 | 4.1% |
| django-14238 | 3 | 59 | 5.1% |
| django-14672 | 3 | 63 | 4.8% |
| django-15202 | 2 | 61 | 3.3% |
| **Average** | **2.4** | **54.6** | **4.4%** |

The agent called gt_hook.py 2-3 times per task (~4.4% of total tool calls). This is appropriate — the prompt instructs "after EACH file edit" and the agent typically makes 2-3 file edits per task.

---

## Architecture: What Finally Worked

### The Delivery Problem (v1-v4)

Every attempt to deliver GT evidence passively failed:

1. **OpenHands HookConfig via `Conversation.__new__`** — Config set correctly in SDK but agent server receives `hook_config: None`. Confirmed via debug logging: `hook_config.model_dump()` IS serialized into the HTTP payload, but `StoredConversation` on the server side doesn't persist it. Likely an OpenHands SDK v1.14.0 bug in the conversation creation API.

2. **OpenHands HookConfig via `Conversation.__init__`** — Conversation uses `__new__` as a factory returning `RemoteConversation`. Python calls `RemoteConversation.__init__`, NOT `Conversation.__init__`. Patch never fires.

3. **`.openhands/hooks.json` file** — HookManager loads from this file, but only at conversation startup. Our file is written AFTER the agent server starts, so it's never loaded.

4. **`RemoteConversation.__init__` patch** — Successfully injected `hook_config=HookConfig(...)` into the init kwargs. Confirmed serialized into payload. Server still shows `None`.

5. **inotifywait watcher** — Not installed in SWE-bench Docker images. Can't `apt-get install` (no root).

6. **Python polling watcher** — Successfully starts (WATCHER_PID confirmed), detects file changes, runs gt_hook.py. But output goes to `/tmp/gt_hook_stdout.log` inside the container — **invisible to the agent**. And log extraction fails because containers are torn down before `finally` block can execute commands.

### What Worked: Prompt-Template Driven Execution (v5)

The prompt template `gt_hook_v5.j2` adds Phase 6.2:

```
After EACH file edit, run this structural check command:
    python3 /tmp/gt_hook.py --root={{ instance.repo_path }} --quiet --max-items=3
If the command outputs any GT: lines, read them carefully — they are structural
warnings about your edit. Consider adjusting your edit if the warning is relevant.
```

The agent:
1. Sees the instruction in its system prompt
2. After making a file edit, voluntarily runs the terminal command
3. Sees GT evidence (or no output) in the terminal result
4. Can act on the evidence before proceeding

This is explicit, not passive — but it actually works. The agent sees the evidence and can reason about it.

### Injection Chain (Confirmed Working)

```
Host: oh_gt_hook_wrapper.py
  ├── patched_evaluate() injects gt_hook.py via base64 chunks → /tmp/gt_hook.py
  ├── patched_evaluate() starts Python file watcher → /tmp/gt_watcher.py (background)
  ├── _original_evaluate() runs with gt_hook_v5.j2 prompt template
  │   └── Agent runs: python3 /tmp/gt_hook.py --root=/workspace --quiet --max-items=3
  │       └── gt_hook.py: _detect_workspace_root("/workspace") → /workspace/django/
  │       └── git diff → finds modified .py files
  │       └── ChangeAnalyzer, SiblingAnalyzer, etc. → evidence
  │       └── _apply_abstention(min_confidence=0.65) → filtered output
  │       └── stdout: "GT: ..." lines → visible to agent in terminal result
  └── finally: _extract_hook_log() (container may already be torn down)
```

---

## Evidence Family Status

| Family | Status | Fires in Production | Notes |
|--------|--------|-------------------|-------|
| **Pattern** (SiblingAnalyzer) | **Working** | **Yes (3/10 tasks)** | Exception types, return shapes, API access patterns |
| Change (ChangeAnalyzer) | Built | No | Needs `git diff HEAD` to detect before/after changes; may fire with larger diffs |
| Contract (CallerUsageMiner) | Built | No | Needs populated `refs` table (not available without full GT indexing in container) |
| Structural | Built | No | Needs full GT package (ObligationEngine, etc.) — gracefully no-ops |
| Semantic (CallSiteVoter, etc.) | Built | No | Needs `git grep` to find call sites; may fire with functions that have many callers |

The **SiblingAnalyzer** fires because it only needs the current file's AST — no git history, no refs table, no external dependencies. It compares the edited function against its siblings (other methods in the same class) across 5 dimensions:
1. Exception types raised
2. Return shape (scalar vs tuple vs None)
3. Guard clauses present
4. Framework API calls used
5. Per-parameter attribute access patterns (Dimension 5, new in v5)

---

## Signal Quality Assessment

| Evidence Line | True Positive? | Actionable? | Notes |
|---------------|---------------|-------------|-------|
| `5/2 siblings access value via isinstance(value) -- edit uses different` | Yes | Partially | Correctly detects deviation; the fix IS different from siblings by design |
| `6/6 siblings raise NotImplementedError -- edit raises SuspiciousSession` | Yes | No | Correct detection, but the edit SHOULD differ from abstract base methods |
| `19/20 siblings return scalar -- edit returns tuple(9)` | Yes | No | Correct detection, but `reduce()` correctly returns operation tuples |

**All 3 findings are true positives** (the detected pattern deviation is real). But none were directly actionable for fixing the bug — they correctly describe structural deviations that are intentional in these cases.

This matches the research prediction: pattern evidence catches unintentional deviations (wrong exception type, forgotten guard clause). For SWE-bench tasks where the deviation IS the fix, the evidence is informative but doesn't change the outcome.

**The high-value scenario** (not yet observed in this sample): An agent makes an edit that accidentally breaks a pattern all siblings follow. GT catches it before the agent moves on. This requires a larger sample (300+ tasks) to measure statistically.

---

## Cost Analysis

| Run | Cost | Notes |
|-----|------|-------|
| v1 baseline | ~$3.02 | No GT |
| v1 GT write | ~$2.95 | GT hooks (silent) |
| v4 baseline | ~$2.65 | No GT |
| v4 GT write | ~$2.57 | GT hooks (silent) |
| **v5 hook+prompt** | **~$2.64** | GT speaking |

GT hook execution adds **negligible cost** — the hook runs in <3s per invocation, and the agent calls it 2-3 times per task. The evidence output is 0-2 lines, adding minimal tokens to the context.

---

## Recommendations

### For the 300-Task Run

1. **Use the prompt-template approach** (`gt_hook_v5.j2`) — it's the only delivery mechanism confirmed working
2. **Keep the Python watcher as backup logging** — even though the agent can't see its output, the watcher creates `/tmp/gt_hook_log.jsonl` for post-hoc analysis
3. **Lower the abstention threshold from 0.65 to 0.55** for the SiblingAnalyzer — more tasks would get evidence, increasing sample size for signal quality measurement
4. **Add a `--change-only` flag** to gt_hook.py that skips families requiring git history — reduces noise from families that can't fire in containers

### For Product Development

1. **File an OpenHands SDK issue** — `hook_config` sent in conversation creation payload is not persisted server-side. This blocks all passive hook delivery.
2. **Investigate `Change` family** — If `git diff HEAD` works inside the container (the agent does git operations), ChangeAnalyzer should fire. May need the workspace path fix propagated to the diff command.
3. **Pre-index the container** — Run `gt_hook.py --build-index` before the agent starts, populating refs table for contract/caller evidence. This was the v4 breakthrough in manual testing.

### Signal Quality Improvement

1. **Add negative examples to SiblingAnalyzer** — "This method's siblings do X, but the base class uses Y — the deviation may be intentional if this overrides behavior"
2. **Weight by method kind** — Abstract methods, `__init__`, `__str__` have expected deviation patterns; regular methods deviating from siblings is more suspicious
3. **Cross-reference with test expectations** — If a test asserts the deviant behavior, suppress the finding

---

## Appendix: Technical Artifacts

### Files Created/Modified in v5

| File | Lines | Purpose |
|------|-------|---------|
| `benchmarks/swebench/gt_hook.py` | 1981 | Amalgamated single-file hook (all 5 evidence families + semantic layer) |
| `scripts/swebench/oh_gt_hook_wrapper.py` | ~240 | OpenHands wrapper: injection + watcher + Conversation patches |
| `scripts/swebench/prompts/gt_hook_v5.j2` | 55 | Prompt template with Phase 6.2 GT check instruction |
| `scripts/swebench/oh_smoke_hook.sh` | 87 | 10-task Django smoke test runner |
| `scripts/swebench/analyze_hook_logs.py` | 436 | v4 log analyzer with per-family breakdown |
| `tests/smoke_gt_hook.py` | 228 | Local verification test suite |
| `src/groundtruth/evidence/semantic/` | ~860 | 3 new signal modules (call_site_voting, argument_affinity, guard_consistency) |
| `src/groundtruth/evidence/pattern.py` | +92 | Dimension 5 (API access pattern) |
| `src/groundtruth/evidence/change.py` | +15 | AsyncFunctionDef type fixes |
| `src/groundtruth/hooks/post_edit.py` | +127 | Workspace detection, view skip, semantic integration |

### Commit History (startupmode-v4 branch)

```
5ba9874 feat: add gt_hook_v5.j2 prompt template with explicit post-edit GT check
b3d6e55 fix: extract hook logs in finally block to survive conversation errors
a5fa8ac feat: pure-Python polling watcher replaces inotifywait
2431297 feat: add inotify watcher as fallback hook delivery mechanism
16f7734 fix: patch RemoteConversation.__init__ directly instead of Conversation.__new__
00c9c70 debug: add stderr logging to __new__ patch
0dfb0aa fix: use --root=/workspace instead of /testbed for hook command
148ea14 debug: add patch verification prints
af37e35 fix: restore Conversation.__new__ patch (factory pattern requires __new__)
193a034 fix: write .openhands/hooks.json inside container for HookManager auto-load
6745d27 fix: patch Conversation.__init__ to inject hook_config parameter
7a71f76 fix: add metadata hook_config injection for remote Docker conversations
53fde64 feat: amalgamated gt_hook.py + smoke test tooling for v4 evidence layer
```

### Key Debugging Findings

1. **`SWEBenchEvaluation.evaluate_instance` CAN be monkey-patched** despite extending Pydantic `BaseModel` — verified via `is` comparison and `__dict__` lookup
2. **`Conversation.__new__` factory pattern**: Returns `RemoteConversation`, NOT `Conversation`. Any `__init__` patch on `Conversation` is never called.
3. **`RemoteConversation.__init__` receives `hook_config`**: Confirmed via marker files. Serialized into payload at line 733. But server-side `StoredConversation` shows `None`.
4. **Container workspace path**: `/testbed` is the original repo copy. `/workspace/django/` is where the agent works. `_detect_workspace_root("/workspace")` scans `/workspace/*/` and finds the `.git` dir.
5. **SWE-bench Docker images**: Run as non-root, no `apt-get`, no `inotifywait`. Python 3.11.5 available at `/opt/miniconda3/bin/python3`.
