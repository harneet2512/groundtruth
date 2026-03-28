# GT Failure + Success Analysis — What Would Flip Tasks?

## Data: 50-task baseline with Qwen3-Coder + mini-swe-agent

- **29 resolved, 20 failed, 0 no-patch**
- Agent found the right file in **92% of cases** (45/49)

---

## FAILURE BREAKDOWN (20 tasks)

| Category | Count (%) | What happened |
|---|---|---|
| Right file, wrong fix | **16 (80%)** | Agent found correct file, wrote wrong code |
| Wrong file | **4 (20%)** | Agent edited wrong file entirely |
| No patch | 0 (0%) | Agent always produces something |

### What would flip the 16 right-file-wrong-fix tasks?

| Signal | Tasks it could flip | Example | Generalizable? |
|---|---|---|---|
| **Test assertions** | ~8 | django-11179: test expects `instance.pk is None` after delete | Yes — any repo with tests |
| **Sibling/precedent pattern** | ~5 | django-11742: show how other validators work | Yes — any class with multiple similar methods |
| **Caller type info** | ~3 | astropy-6938: output_field is chararray, not str | Yes — static analysis of call sites |
| **Return value contract** | ~3 | astropy-7746: callers check empty arrays | Yes — usage classification of callers |
| **Recent git diff** | ~4 | Show exact pattern of similar 1-line fixes | Yes — git history |

### What would flip the 4 wrong-file tasks?

| Task | Agent edited | Needed | What GT signal would help |
|---|---|---|---|
| django-11564 | storage.py (3 files) | conf/__init__.py | Cross-file caller: show who CONFIGURES storage |
| django-11620 | urls/resolvers.py | views/debug.py | Caller chain: show the 404 handler path |
| django-11797 | models/sql/query.py | models/lookups.py | Caller: show that Query delegates to Lookup |
| django-11910 | migrations/operations/fields.py | migrations/autodetector.py | Co-change: these files change together |

---

## SUCCESS ANALYSIS (29 tasks)

### Fix complexity of resolved tasks:

| Complexity | Count |
|---|---|
| Trivial (1-3 lines) | 8 (28%) |
| Simple (4-10 lines) | 18 (62%) |
| Moderate (11-30 lines) | 3 (10%) |
| Complex (30+ lines) | 0 (0%) |
| Multi-file | 0 (0%) |

**Every resolved task is single-file.** The model doesn't solve multi-file changes.

### Agent speed on resolved tasks:

| Speed | Count | Avg turns |
|---|---|---|
| Fast (<=40 turns) | 2 (7%) | ~34 |
| Moderate (41-80) | 12 (41%) | ~60 |
| **Slow (>80 turns)** | **15 (52%)** | ~124 |

**Average: 92 turns per resolved task.** Half the resolved tasks took >80 turns — the agent explored extensively before finding the fix. These are tasks GT could accelerate.

### Specific slow-but-resolved tasks where GT would help:

| Task | Turns | Fix | What GT would save |
|---|---|---|---|
| django-12589 | 172 | 15 lines in sql/query.py | Show target function + test expectation → save ~100 turns |
| django-12747 | 166 | 6 lines in deletion.py | Show deletion cascade pattern → save ~100 turns |
| astropy-14995 | 150 | 4 lines in ndarithmetic.py | Show mask propagation callers → save ~80 turns |
| django-12125 | 138 | 2 lines in serializer.py | Show the exact serialization function → save ~100 turns |
| django-11964 | 128 | 8 lines in enums.py | Show Choices class structure → save ~60 turns |
| django-12908 | 128 | 1 line in query.py | Show the query method + its callers → save ~90 turns |
| django-13158 | 128 | 3 lines in query.py | Show query filter chain → save ~80 turns |

**Total saveable turns across 15 slow tasks: ~1200 turns (avg 80/task). At ~$0.01/turn on Qwen3-Coder, that's ~$12 saved on 15 tasks.**

---

## GENERALIZABLE GT SIGNALS (for any codebase)

### Signal 1: Test Assertions (highest impact for flipping failed tasks)

**What:** Extract from test files WHAT the test asserts — the concrete correctness target.

**How:** Parse test files that reference the target function. Extract `assert*` statements and their arguments. Show: "test expects X after calling Y."

**Impact on failures:** ~8/16 right-file-wrong-fix could flip.
**Impact on successes:** Speeds up every task by showing the target.
**Generalizable:** Yes — every language has test frameworks with assertion patterns.

### Signal 2: Ego-graph with real code (highest impact for speeding up)

**What:** Show actual source code of connected functions — callees, callers, references.

**How:** 1-hop graph from index. Read actual file lines at each node.

**Impact on failures:** ~4/20 (the wrong-file cases — show caller chain).
**Impact on successes:** Saves 60-100 turns on slow tasks by showing WHERE to edit.
**Generalizable:** Yes — function calls exist in every language.

### Signal 3: Sibling/Precedent Pattern (highest impact for fix quality)

**What:** Show the nearest similar function in the same class/module.

**How:** Compare fingerprints (parameter count, return type, calls) across siblings.

**Impact on failures:** ~5/16 — agent sees how similar code works and follows the pattern.
**Impact on successes:** Anchors the fix in real code, reduces hallucination.
**Generalizable:** Yes — classes with multiple methods exist everywhere.

### Signal 4: Caller Contract (prevents interface breaks)

**What:** How callers use the return value — destructure, iterate, compare, ignore.

**How:** Classify usage at each call site from surrounding AST context.

**Impact on failures:** ~3/16 — prevents changing return type when callers depend on it.
**Impact on successes:** Prevents regressions that tests catch but take iterations to fix.
**Generalizable:** Yes — function calls have usage patterns in every language.

### Signal 5: Co-change / Modification Scope

**What:** Which OTHER files typically change with the target file.

**How:** Parse git log for co-change frequency.

**Impact on failures:** ~2/4 wrong-file cases.
**Impact on successes:** Low on Lite (all single-file fixes). Higher on Pro/real codebases.
**Generalizable:** Yes — git is universal.

---

## DELIVERY MECHANISM ANALYSIS

### What we've tried:

| Delivery | Injection rate | Agent sees it? | Cost | Problem |
|---|---|---|---|---|
| Precompute + prompt inject | 16-100% (depends on file detection) | Yes | 0 turns | Wrong file detection = wrong context |
| Active calls (agent runs understand) | 100% | Yes | 2-45 turns | Over-calling burns budget |
| Passive hooks (OpenHands) | 100% | **No** | 0 turns | HookExecutionEvent not rendered |
| Execute monkey-patch (v10 hooked) | 100% on edits | Yes | 0 turns | Not tested yet |

### The right delivery is the hook (execute monkey-patch):

1. **No file detection needed** — fires on the file the agent actually edits
2. **No prompt contamination** — same template as baseline
3. **Agent sees output naturally** — appears in command stdout like a compiler warning
4. **Zero turn cost** — runs after the agent's own edit command
5. **Each file analyzed once** — dedup per container
6. **Index pre-built** — 25s at start, <200ms per query

### But delivery alone doesn't flip tasks. Content does.

The hook delivers GT output to the agent. But the output must contain information that changes the agent's next action. Based on the failure analysis:

**Priority 1: Test assertions** — "what the test expects" is the #1 missing signal
**Priority 2: Ego-graph with code** — "what connected code looks like" speeds up exploration
**Priority 3: Sibling pattern** — "how similar code works" anchors the fix

---

## THE PRODUCT ARCHITECTURE

For any codebase (not just SWE-bench):

```
1. Agent starts working on a task
2. Agent reads files, greps, explores
3. Agent edits a .py file
4. GT hook fires automatically:
   a. Reads the edited file
   b. Queries the pre-built index for:
      - Callers of modified functions (who breaks if this changes?)
      - Tests that reference this module (what defines correctness?)
      - Sibling methods in same class (what pattern to follow?)
   c. Reads actual source code at each connected node
   d. Formats as compact output (~15-30 lines)
   e. Appends to command stdout
5. Agent sees GT output and adjusts:
   - Checks callers before changing return type
   - Reads test file to understand expected behavior
   - Follows sibling pattern for consistency
6. Agent continues editing with better context
```

This is the architecture. The hook is the delivery. The ego-graph + obligations are the content. Test assertions are the #1 priority to add.
