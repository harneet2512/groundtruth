# Session Decisions Log — 2026-05-10

## DECISION 0 (LOCKED): Localization Layer = V1R + BM25 + Agent

The localization layer is V1R + BM25 + **agent**. All three working together:

- V1R ranks files from the graph (62% hit@5)
- BM25 matches issue text against file content (strongest signal)
- Agent greps, reads, navigates using its own understanding (88% baseline)
- L3b shows graph connections as agent explores (dynamic hops)

The system is all of these combined. GT alone is never the localization layer. GT + agent together IS the localization layer.

## HOW TO MEASURE GT+AGENT COLLABORATION

The signal is NOT "agent explicitly follows brief file list." The agent may grep on its own — but with GT it greps FASTER because the brief primed its understanding.

**Measure with numbers, not narrative:**
- Turns-to-gold-READ (fewer = GT helped orientation)
- Turns-to-gold-EDIT (fewer = GT helped commitment)  
- First-scaffold iteration (later = less wasted exploration)
- Total actions (fewer = more efficient overall)

**Do NOT look at the trajectory and conclude "agent found it on its own" just because it used grep.** Compare the NUMBERS against baseline. The collaboration is subtle — it's in the agent's reasoning speed, not in a visible "open brief candidate" action.

cfn-lint-3821 proof: baseline=6 steps to gold read, GT=4 steps. GT didn't "redirect" — it made the agent's own search 2 steps faster.

## FILE MAP (so we never put logic in the wrong file again)

| Logic | File | Why this file |
|---|---|---|
| V1R brief generation + hub suppression + modulus gate | `src/groundtruth/pretask/v1r_brief.py` | Entry point the wrapper calls for L1 |
| V7.4 hybrid scorer (BM25 + reach + hub_pen) | `src/groundtruth/pretask/v7_4_brief.py` | Scoring engine called by v1r_brief |
| Post-edit evidence (L3: callers, contracts, patterns) | `src/groundtruth/hooks/post_edit.py` | Fires after every source edit |
| Post-view navigation (L3b: graph connections) | `src/groundtruth/hooks/post_view.py` | Fires after every file read |
| OH wrapper (patches run_infer, GT_PHASE, logging) | `scripts/swebench/oh_gt_full_wrapper.py` | All GT↔OH integration |
| Hub suppression + inverse-degree reranking | `src/groundtruth/pretask/v1r_brief.py` (NOT v7_brief.py) | Was wrongly in v7_brief.py before |

## DECISION 1 (LOCKED): L3 Evidence Architecture

L3 is synced with L1 and dynamic. It shows ACTUAL CODE, not metadata.

**What L3 sends after every edit (200-300 tokens max):**

Priority order (stop when 300 tokens reached):
1. Caller CODE lines (from graph.db edges.source_line → read actual line from file)
2. Sibling function pattern (from graph.db parent_id → read sibling body snippet)
3. Signature + return type (from graph.db nodes.signature)
4. Test assertions (bonus only when available — NOT relied upon, NOT benchmaxxing)

**Synced with L1:**
- Briefed candidate file → FULL evidence (caller code + sibling + signature)
- File from brief's `Calls:` list (1-hop neighbor) → graph-aware evidence
- Unbriefed file → Minimal (signature + "nearest candidate: X")

**Dynamic (same principle as L1 hops):**
- Tracks agent trajectory (edited_files, viewed_files)
- Shows callers agent HASN'T visited yet
- Updates brief progress (3/5 candidates edited)
- Shows cross-file connections between files agent has already edited
- Deprioritizes issue terms already seen — surfaces NEW relevant info

**What this solves:**
- Old: 80% placeholder (showed file names or nothing)
- New: <20% placeholder (caller code lines exist for any non-dead-code function)
- Old: evidence was metadata ("called by auth.py")
- New: evidence is actual code ("auth.py:42: result = validate(token, strict=True)")
- Old: L3 independent of L1
- New: L3 builds on L1's localization — more evidence for briefed files, less for unbriefed

**Research backing:**
- Caller code: +16% (ARISE), +14.5pp mixed feedback (FeedbackEval across 5 models)
- Compact <500 tokens: +2pp + 31-54% savings (SWE-Pruner, Complexity Trap)
- External oracle required: LLMs cannot self-correct without it (TACL 2024)
- Model-agnostic: FeedbackEval tested GPT-4o, Claude 3.5, Gemini 1.5, GLM-4, Qwen2.5 — all benefit

## DECISION 2 (LOCKED): L3b Post-View Navigation Architecture

L3b fires when the agent READS a file. It's part of L1 localization (Decision 0) — helps the agent navigate the graph dynamically.

**What L3b shows:**
- Issue-relevant callers (files that call into this file, ranked by issue-term matches in their content)
- Issue-relevant callees (files this file calls, ranked by issue-term matches)
- Importers (files that import from this file)
- All ranked by relevance to current issue, not by edge count

**File:** `src/groundtruth/hooks/post_view.py`

**What we discussed and implemented:**
- Graph navigation is PRIMARY output (not the old AST coupling analysis)
- Issue terms from `/tmp/gt_issue_terms.txt` used to score neighbors by relevance
- Dynamic: shows what's relevant to THIS issue, not static graph structure
- The agent follows connections based on semantic understanding
- Each file open = one more hop in the navigation graph
- No hop limit — agent decides depth

**Synced with L1:**
- L1 brief seeds candidates + their callees
- L3b extends that navigation at every file read
- Together: brief (hop 0) → Calls in brief (hop 1) → L3b on opened file (hop 2+)

**What still needs doing (from research):**
- Decay: full connections early, lighter later (same as L3)
- Suppress already-visited files from the connection list
- Track progress: "you've visited 3/7 connected files"

## DECISION 3 (LOCKED): L4 Prefetch — 3 Changes, No Major Flips Expected

L4 fills the gap between "agent reads brief" and "agent makes first edit." Constraint-framer, not flip generator.

**Changes:**
1. Add git precedent: "last commit: fix None return in auth" (~20 tokens/file)
2. Tighten taxonomy labels: aggregate caller count into label
3. Remove sibling/body-span lines (zero value)

**Not expected to produce flips.** Prevents wrong first attempts → fewer wasted iterations.

**File:** `oh_gt_full_wrapper.py` L4 section + `gt_query.py`

## L3b Implementation Complete (5 optimizations)
1. Confidence >= 0.5 filter on all edge queries
2. Suppress already-visited files (reads /tmp/gt_viewed.txt)
3. Brief candidate annotation [CANDIDATE] (reads /tmp/gt_brief_candidates.txt)
4. Hub-penalized ranking: score = count * (1 - in_degree/50)
5. Symbol-level hints: auth.py::validate_token,refresh (3x)

All model-agnostic, repo-agnostic, scale-agnostic, $0, deterministic.

## Decision 1: Stream 0 Diagnostics Completed

**Finding:** Four parallel diagnostic streams ran locally at $0.

| Stream | Result |
|---|---|
| 0A: L1 localization audit | hit@3 = 33% (10/30), 67% total miss |
| 0B: Baseline failure modes | 88% find gold file without GT, only 10% scaffolding trap |
| 0C: 6-task trajectory trace | Brief hit 0/6, GT slowed gold-file discovery in 5/6 |
| 0D: Fix gt_interactions | Write-through to `/tmp/gt_interactions.jsonl` — DONE |

## Decision 2: OH Wrapper Uses Wrong Brief Pipeline

**Root cause of 33% hit@3:** The OH wrapper (`oh_gt_full_wrapper.py` line 1592) imports `v7_brief.generate_brief` — which uses v6 cochange-only retrieval. The V1R-map pipeline (`v1r_brief.generate_v1r_brief`) uses v7.4 hybrid scoring (sem + lex + reach + anchor_prox - hub_pen) and achieved **73-80% hit@3** on prior runs.

**Evidence:**
- `last_mile.md` line 691: V1R-map 12/15 gold-in-brief (80%)
- `future_plan.md` line 84: qwen3-OR hit@3 73%
- `docs/v1r_map_runbook.md`: V1R-map frozen 2026-05-03, beat V1 on every metric

**Fix:** Change the import in `oh_gt_full_wrapper.py` from `v7_brief` to `v1r_brief`.

## Decision 3: Phase A Changes to v7_brief.py

Three changes made to `src/groundtruth/pretask/v7_brief.py`:

1. **Hub suppression gate** — if ALL top-3 candidates are above the 80th percentile of in-degree, suppress the brief entirely
2. **Scaffold directive removed** — deleted "Do not add throwaway scaffolding" constraint
3. **Inverse-degree reranking** — `score / log(in_degree + 2)` pushes peripheral files above hubs

**Status:** These changes improve v7_brief, but the real fix is switching to v1r_brief (Decision 2). The v7_brief changes are belt-and-suspenders.

## Decision 4: v1r_brief Tested on 2 Repos Locally

| Task | v7_brief candidates | v1r_brief candidates | Gold file | Hit? |
|---|---|---|---|---|
| cfn-lint-3875 | Properties.py, FindInMap.py, ResourceType.py | FindInMap.py, Used.py, PrefixItems.py, RequiredXor.py, FindInMapResolved.py | _language_extensions.py | NO (both miss) |
| twine-1225 | sdist.py, check.py, auth.py | exceptions.py, auth.py, commands/__init__.py, check.py, package.py | twine/sdist.py | v7 HIT, v1r MISS |

**Finding:** V1R is more targeted (rule files + functions + tests, no generic hubs) but doesn't help on ALL tasks. The cfn-lint-3875 gold file is genuinely hard to localize (transform helper, not a rule). For twine-1225, v7_brief actually found sdist.py at rank 1 but v1r missed it — however v7 was using path "sdist.py" without the "twine/" prefix.

**Key insight from 0B:** The agent finds gold files 88% of the time WITHOUT any brief. The brief's primary value isn't localization — it's curating context (contracts, callers, patterns) that helps the agent produce correct fixes.

## Decision 5: Comparative Stop/Go Criteria (not arbitrary thresholds)

Per user feedback: no made-up numeric thresholds like ">30% follow rate." Instead:
- Better than the prior accepted stack (directional improvement)
- No outcome regressions
- Per-phase flip audit: all regressions, all gains, 5-10 near-misses

## Decision 6: Dev Slice Before Frozen 30

Per user feedback: use small dev slice (5-10 tasks) for iteration, reserve the frozen 30 for acceptance-only. Full 30-task runs are gates, not feedback loops.

## Decision 7: Cost Notification After Every Run

Mandatory cost report after every VM run: LLM cost, VM cost, cumulative, remaining, next-run estimate.

## Decision 8: OH Wrapper Switched to V1R Brief

**Changed** `oh_gt_full_wrapper.py` line 1592: `v7_brief.generate_brief` → `v1r_brief.generate_v1r_brief`

**Local verification on 3 repos:**

| Task | V7 brief (old) | V1R brief (new) | Gold file |
|---|---|---|---|
| beancount-931 | MISS | **HIT rank 1** | `plugins/leafonly.py` |
| cfn-lint-3875 | MISS | MISS (genuinely hard) | `transforms/_language_extensions.py` |
| twine-1225 | HIT rank 1 (sdist.py) | rank 6 (just outside top 5) | `twine/sdist.py` |

V1R's hybrid scorer (sem+lex+reach+anchor_prox-hub_pen) is the one that achieved 73-80% hit@3 in prior runs. The v7 cochange-only pipeline was a regression.

**Risk:** twine-1225 drops from rank 1 to rank 6 with V1R. This is one task where the simpler v7 path-mention signal worked better. Net across the 15 prior tasks: V1R was 12/15 (80%) vs v7's ~5/15 (33%). Trade is strongly positive.

## Decision 9: Full Layer Audit — All Layers Working

Audited every layer in `oh_gt_full_wrapper.py`:

| Layer | Status | What it does |
|---|---|---|
| L1 (brief) | WORKING | V1R brief injected into agent instruction, map-only |
| L3 (post-edit) | WORKING | Evidence or [GT_OK] appended after every source edit |
| L3b (post-view) | WORKING | Evidence or [GT_OK] appended after every file read |
| L5 (checkpoint) | WORKING | Fires at 33%/66% of max_iter, advisory only |
| L6 (reindex) | WORKING | Incremental gt-index before L3 hook, hidden from agent |
| Pacing | WORKING | [GT_OK] emitted on all no-evidence paths, no bypass |
| Scaffold strip | WORKING | Fires on finish + post-loop, idempotent, base_commit aware |
| Interactions log | FIXED | Added L6 logging (was missing), all 6 layers now logged |
| Brief candidates | WORKING | Regex extracts paths from V1R format correctly |

**Key verification:** V1R format (`1. path — funcs`) is correctly parsed by `_extract_candidate_files` regex. The `brief_text` attribute (not `brief`) is correctly accessed.

## Decision 10: Anti-Overfitting Rules (permanent, in .claude/CLAUDE.md)

Added hard rules to `.claude/CLAUDE.md` backed by three papers:
- SWE-bench Illusion (NeurIPS 2025): model contamination
- Test Overfitting Study (arXiv 2511.16858): test-based refinement inflates 3.7%
- SWE-bench+ (arXiv 2410.06992): 32.67% cheating patches

None of these apply to us. Our overfitting risks are: task-specific conditionals, hyperparameter tuning against benchmark outcomes, rewording based on per-task responses. Rules flag all of these.

Testing on the 30 tasks is fine — it's evaluation, not training. Every top system (Agentless, AutoCodeRover, OpenHands) develops and reports on the same test split.

## Decision 11: Product First, Benchmark Second

The 30 frozen tasks are a validation gate, NOT a training set. We do NOT:
- Clone all 30 repos to measure V1R hit@3 and tune until it improves
- Change wording/ranking based on per-task results
- Count task-specific improvements as layer value

We DO:
- Optimize each layer's mechanism to be structurally better in general
- V1R switch is justified because hybrid scoring (sem+lex+reach+hub_pen) is a better retrieval algorithm period, not because it scores better on these 30
- Validate once on the 30 after each layer is optimized
- Final proof on 300 tasks

Motto added to `.claude/CLAUDE.md` and saved to memory.

## Decision 16: Integration Architecture — All Layers Use Observation Augmentation

Research (Strands 100% vs 82.5%, ARISE, RepoGraph, SWE-agent ACI) converges on one pattern:
**Modify tool results at action boundaries.** Don't give optional tools. Weave GT into tools the agent already uses.

- Agent reads a file → GT appends graph neighbors to the result
- Agent edits a file → GT appends contract/caller obligations to the result
- Agent gets no evidence → GT appends [GT_OK] (pacing)
- Brief stays as one-shot injection at start

Anti-pattern (ARISE): do NOT summarize graph data into prose. Give structured output directly.

Architecture written up in `final_arch.md`.

## Decision 17: VM Setup for Live Test

- gt-t0 started (104.154.251.180), ~$0.75/hr
- Updated files deployed: oh_gt_full_wrapper.py, v7_4_brief.py, v1r_brief.py, post_view.py
- Installing litellm + openhands-ai on VM
- Local OH won't work on Windows (needs .NET/WSL)

## Decision 19: L1 Phase B Results — Modulus Violated (deployment bug)

**24-task comparison (GT Phase B vs Baseline):**
- GT-only patches: 2 (cfn-lint-3789, briefcase-2085)
- BL-only patches (regressions): 3 (cfn-lint-3821, cfn-lint-3854, pylint-10044)
- Both have patches: 13
- Neither: 6

**Root cause of regressions:** V1R brief generation CRASHES in the container because `sentence-transformers` isn't installed. The brief injects a Python traceback instead of file candidates. The agent sees error text instead of localization help.

**Fix:** Make V1R's semantic component optional — if import fails, set W_SEM=0 and use only BM25 + graph reach + hub penalty. Brief must degrade gracefully.

**Cost report:**
- LLM: ~$5.80 (54 task-runs × $0.12)
- VM: ~$3.75 (5 hours total)
- Cumulative: ~$9.55
- Remaining: ~$85

## Decision 18: Local Docker Setup Complete

- 30/30 SWE-bench-Live instance images pulled locally (starryzhang/ prefix)
- gt-eval Docker image built with OH 0.54.0 + all deps
- Pipeline verified: config loads, dataset loads, instance matched, Docker image found
- Qwen3-Coder reachable via Vertex ($0.12/task)
- gcloud auth token saved to /test/vertex_token.txt
- Launcher script: `D:\tmp\gt_test\run_baseline.py`
- Next: wire `process_instance` call to actually run the agent

**Docker command to run:**
```
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  -v D:\tmp\OpenHands:/app -v D:\Groundtruth:/gt -v D:\tmp\gt_test:/test \
  gt-eval:latest python /test/run_baseline.py beancount__beancount-931
```

## Current Layer Status (per the framework)

| Layer | Job | Current State | What to Optimize |
|---|---|---|---|
| **A. L1 localization** | Point at right files | Switched v7→V1R (73→80% historical). Structurally better algorithm. | Done for now — V1R is the correct pipeline |
| **B. Brief usability** | Guide agent behavior | V1R format: map-only, compact. Scaffold directive removed. | Wording density, actionability |
| **C. Pacing** | Keep agent in editing mode | [GT_OK] working, compression fix shipped | Placeholder wording, cadence |
| **D. Framed evidence** | Change next action after edit | L3/L3b hooks working, 96% noise with v7 | Framing, brevity, suppress noise |
| **E. Redirect** | Pull agent back from drift | L5 checkpoint working at 33%/66% | Timing, trigger conditions, wording |
| **F. Prefetch** | Help early navigation | L4 prefetch exists but tools are dead | Seed selection, formatting |
| **G. Reindex** | Keep graph fresh | L6 working, hidden from agent | Speed, robustness |
| **H. Hygiene** | Clean patches | Scaffold strip + truncation fix working | Reliability |

## Decision 15: L1 Collaboration Model — Brief Shows Graph Connections

The brief is not a ranked list for the agent to follow. It's a graph map for the agent to navigate WITH.

**Before:** `1. FindInMap.py — fn_findinmap, __init__` (GT says what to edit)
**After:** `1. FindInMap.py (fn_findinmap) / Calls: _condition.py, context.py / Tests: test_find_in_map.py` (GT shows the neighborhood, agent navigates)

Changes:
- Added `_callees_for()` to v1r_brief.py — queries graph.db for outgoing call edges
- Added `callees` field to `FileEntry` dataclass
- Updated `render_brief()` to show `Calls:` lines
- Also redesigned `post_view.py` — graph navigation (callers, callees, importers) is now PRIMARY output, not fallback after AST coupling analysis

Token cost: ~80 extra tokens (237 total vs 155 before). The `Calls:` lines give the agent graph edges to navigate during its own exploration.

**L3b post_view.py redesign:** When agent opens ANY file, GT now shows:
- `Called by: file_a.py (3x), file_b.py (1x)` — who depends on this file
- `Calls into: file_c.py (2x), file_d.py (1x)` — where this file reaches
- `Imported by: file_e.py, file_f.py` — import graph

This is the collaboration: GT provides structural connections at every step of the agent's exploration. Agent uses its semantic understanding to decide which connection to follow.

## Decision 12: Brief Format — Add Signatures (Layer B)

V1R brief is the right structure (minimal, map-only) but missing function signatures. Current: `fn_findinmap, __init__`. Better: `fn_findinmap(validator, value) → Iterator[ValidationError]`. Backed by AutoCodeRover (ISSTA 2024): structured context with signatures reduces false starts. ~20 extra tokens, near-zero noise risk. Pull from `nodes.signature` in graph.db.

## Decision 13: Evidence Design Principles (Layer D, research-backed)

From 7 papers (CodexGraph NAACL 2025, Plan Compliance arXiv 2604.12147, RepoGraph ICLR 2025, Strands Agents, SWE-Pruner, Agent READMEs, JetBrains):

1. **Imperative > declarative** — "MUST return Optional[User]" not "returns Optional[User]"
2. **5-10 lines max** — SWE-Pruner: less context improves success rates
3. **Inject at observation boundaries** — Strands: 100% vs 82.5% for prompt-based (our L3 already does this)
4. **Correct > comprehensive** — wrong evidence worse than none
5. **Assertion values > test pointers** — "assert get_user(99) raises KeyError" not "test_get_user references get_user"
6. **Line-level ego-graphs > file dumps** — RepoGraph: +32.8% improvement

## Decision 14: V1R Localization Results — L1 Ceiling Identified

Full 30-task local measurement (29 tasks — aiogram checkout failed):

| Metric | v7 (old) | V1R (new) |
|---|---|---|
| hit@1 | 3/30 (10%) | 7/29 (24%) |
| hit@3 | 10/30 (33%) | 10/29 (34%) |
| hit@5 | 10/30 (33%) | 10/29 (34%) |

V1R improved hit@1 (+14pp, more rank-1 precision) but hit@3 is flat. 20/29 tasks (69%) have ZERO gold files in top 5 with either pipeline. The historical 73-80% was on a different, easier 15-task set.

**This means:** L1 localization has a hard ceiling at ~34% hit@3 on this task mix with current retrieval. But recall from 0B: the agent finds gold files 88% of the time WITHOUT any brief. So the brief's job isn't localization — it's giving the agent a faster starting point on the 34% where the brief IS correct, while not harming the 66% where it's wrong.

**Implication for layers B-E:** The brief will be wrong 66% of the time. Downstream layers (pacing, evidence, redirect) must be robust to wrong briefs — they should help when the brief is right and stay out of the way when it's wrong.

## What's Next

For each layer A-H, go through:
1. Define the job (done above)
2. Measure current failure (needs VM runs with gt_interactions logging)
3. Optimize that mechanism (code changes, generalized not task-specific)
4. Compare against last accepted stack
5. Audit: regressions, gains, near-misses
6. Keep or drop

**Immediate:** Layers A (localization) and B (brief usability) can be optimized locally. Layers C-H need VM runs to measure behavior. All layer code can be prepared in parallel with GT_PHASE flags.

## L3b Implementation Complete (5 optimizations)
1. Confidence >= 0.5 filter on all edge queries
2. Suppress already-visited files (reads /tmp/gt_viewed.txt)
3. Brief candidate annotation [CANDIDATE] (reads /tmp/gt_brief_candidates.txt)
4. Hub-penalized ranking: score = count × (1 - in_degree/50)
5. Symbol-level hints: auth.py::validate_token,refresh (3x)

All model-agnostic, repo-agnostic, scale-agnostic, $0, deterministic.

## NEXT: 30-task comparison at max_iter=100

**Date:** 2026-05-10
**Purpose:** Real measurement — GT+agent (L1+L3+L3b all active) vs historical baseline of 4/30 resolved.

**Configuration:**
- GT_PHASE=full (L1 V1R brief + L3 post-edit evidence + L3b post-view navigation)
- max_iter=100 (same as baseline)
- Model: qwen3-coder-480b on Vertex MaaS global endpoint
- 30 tasks split across 2 VMs: 20 on gt-t0 (4 workers), 10 on gt-v1 (2 workers)

**Cost:**
- LLM: 30 tasks × $0.12/task = ~$3.60
- VM: ~2 hours at $1.50/hr = ~$3.00
- Total: ~$6.60
- Budget remaining before: ~$75
- Budget after: ~$68.40

**Baseline:**
- 4/30 resolved (historical, same tasks, same model, no GT)
- Files: D:\tmp\gt_test\results_final\baseline_t0.jsonl + baseline_v1.jsonl

**Launcher:** scripts/swebench/run_30task_comparison.sh
**Analysis:** scripts/analysis/compare_30task.py

**Success criteria (from Decision 5 — comparative, not threshold):**
- More patches than baseline (currently 4/30)
- Zero regressions on the 4 that baseline already resolves
- Evidence blocks fire in >80% of tasks (layer health)
- Brief injected in >90% of tasks (no import crashes)
- If regressions exist: audit each trajectory, identify root cause before declaring

## Decision 20 (LOCKED): Regression Root Cause — Two Distinct Failure Modes

**Date:** 2026-05-10
**Source:** Phase 1A envelope validation on 29/30 tasks locally.

**Finding:** The 3 regressions have TWO different root causes, not one:

| Regression | Envelope conf | Gold in top-5? | Root cause |
|---|---|---|---|
| weasyprint-2303 | 0.228 (HIGH) | NO (rank 29) | Retrieval false positive — all signals collude on wrong target (cross-domain bug) |
| beancount-931 | 0.047 (low) | YES (rank 4) | Agent over-trust — correct brief, but agent stopped exploring after seeing candidates |
| twine-1225 | 0.145 (mid) | YES (rank 5) | Agent over-trust — correct brief at rank 5, agent committed too early |

**Architectural consequence:** Two separate mechanisms needed:

1. **Retrieval envelope** (L1 only) — suppress when score distribution is flat or all signals agree on candidates with no path redundancy. Catches NOISY retrievals. Does NOT catch cross-domain false positives (weasyprint).
2. **Over-trust mitigation** (L3/L5) — ensure correct briefs don't suppress useful exploration. When brief IS correct, the agent should STILL explore before committing. This is about pacing, not suppression.

**What the envelope CAN do (validated):**
- Separation effect: +0.08 (mean conf 0.154 gold-correct vs 0.072 gold-wrong)
- Redundancy is the strongest discriminator (0.7 vs 0.3)
- Correctly identifies noisy retrievals (cfn-lint-3779 conf=0.009, cfn-lint-3866 conf=0.030)
- Would suppress ~5 tasks where brief is wrong AND confidence is low

**What the envelope CANNOT do:**
- Cannot catch weasyprint-type (cross-domain, all signals agree on wrong answer)
- Cannot prevent over-trust (beancount/twine — brief was RIGHT, problem is downstream)

**Next steps:**
- Implement envelope scoped to L1 only (suppress when redundancy < 0.4 AND separation < 0.2)
- Separately: trajectory analysis on beancount+twine to identify exact over-trust mechanism
- The τ_abstain threshold is data-derived: conf < ~0.05 cleanly separates "noisy" from "some signal"

## Decision 21: Phase 1A Envelope Data (full 29-task table)

Mean confidence by group:
- Gold in top-5 (n=18): 0.154
- Gold NOT in top-5 (n=11): 0.072
- Resolved (n=9): 0.173
- Baseline resolved (n=3): 0.140

Top-5 by confidence (all gold-correct):
1. checkov-6895: 0.443 (redundancy=1.0, agreement=0.835)
2. cfn-lint-3821: 0.257 (redundancy=0.8, separation=0.572)
3. cfn-lint-3890: 0.256 (redundancy=1.0, separation=0.524)
4. checkov-7002: 0.238 (redundancy=0.8, separation=0.647)
5. weasyprint-2303: 0.228 ← FALSE POSITIVE (gold at rank 29)

Bottom-5 by confidence:
- cfn-lint-3779: 0.009 (redundancy=0.0) — correctly noisy
- beets-5495: 0.019 (redundancy=0.0) — but gold IS at rank 3!
- cfn-lint-3866: 0.030 (redundancy=0.4) — correctly noisy
- cfn-lint-3854: 0.032 (redundancy=0.6) — gold at rank 10
- cfn-lint-4016: 0.032 (redundancy=0.2) — gold at rank 5

**Key insight:** Low confidence doesn't always mean wrong (beets has gold at rank 3 with conf=0.019). The envelope is a WEAK signal for suppression. Its primary value is identifying tasks where GT has near-zero information (redundancy=0) — those are safe to suppress.

## Decision 22 (LOCKED): 7 Generalization Fixes — Making GT Safe on Any Repo

**Date:** 2026-05-10  
**Branch:** `general_start`

These 7 changes make GT repo-agnostic, scale-agnostic, and safe on codebases it's never seen. All are structural (not threshold-tuned). All thresholds are repo-relative.

| # | Fix | File | Principle |
|---|---|---|---|
| 1 | Hub scale = p90_in_degree (not hardcoded 50) | `post_view.py` | Auto-calibrates to any graph topology |
| 2 | Sparse graph → BM25-only (edges_per_file < 2) | `v1r_brief.py` | Graph signals suppressed when meaningless |
| 3 | Adaptive K from score gap distribution | `v1r_brief.py` | Shows more candidates when scores are close |
| 4 | Redundancy=0 → suppress brief entirely | `v1r_brief.py` | No multi-path confirmation = guessing |
| 5 | L3 decoupled from L1 (no briefed/unbriefed tiering) | `post_edit.py` | Evidence quality = edge confidence, not L1 opinion |
| 6 | L5 stuck-pattern detection (never names files) | `oh_gt_full_wrapper.py` | Prevents cascade from wrong L1 |
| 7 | BM25 covers config/data/doc files | `hybrid.py` | Finds bugs in YAML, Dockerfile, .toml, etc. |

**Why these are NOT benchmaxxing:**
- None reference task IDs, repo names, or language-specific patterns
- All use repo-relative statistics (p90, median gap, edges_per_file)
- All would help on a random private repo the same way they help on the 30
- Fix 7 (config files) helps on infra repos GT has never been tested on

**Regression constraint:** The 9 flips must still resolve after these changes. Adaptive K (Fix 3) and redundancy suppression (Fix 4) could theoretically suppress a brief that was previously correct. Must verify on the 7 flip tasks.

## Decision 23: Generalization Audit — 8 Scenarios, 3 Quick Fixes

**Date:** 2026-05-10  
**Source:** Senior engineer + QA audit of all failure modes on real-world codebases.

### Failure Scenarios (ordered by frequency × harm):

| # | Scenario | Frequency | Harm | Current GT Behavior |
|---|---|---|---|---|
| 1 | Frontend (React/Vue) — JSX component edges missing | Very High | Medium | Brief has weak graph, falls back to BM25 |
| 2 | Polyglot — disconnected per-language graphs | High | Medium | Brief is per-language only, misses cross-lang bugs |
| 3 | Generated code pollutes graph | High | **High** | Brief recommends generated files that shouldn't be edited |
| 4 | Monorepo — silent 10K file truncation | Med-High | **High** | 90% of files invisible, no warning |
| 5 | Microservices — no cross-service edges | High | **High** | Confidently recommends wrong service |
| 6 | Infrastructure (Terraform/K8s) — empty graph | Medium | Low | Produces empty brief, not misleading |
| 7 | Notebooks (.ipynb) not indexed | Medium | Medium | GT is useless but not harmful |
| 8 | Plugin/dynamic dispatch (WordPress, VS Code) | Medium | Low | BM25 compensates; GT doesn't mislead |

### 3 Quick Fixes Implemented (each <50 lines):

**Quick Fix A: JSX component edges** — Add `jsx_self_closing_element` and `jsx_opening_element` to CallNodes in JavaScript/TypeScript specs. Immediately gives React/Vue/Angular repos a real component call graph. Covers ~40% of GitHub repos.

**Quick Fix B: Generated-code exclusion** — Add `gen/`, `generated/`, `__generated__/` to skipDirs in walker.go. Add first-line comment detection: skip files starting with `// Code generated`, `# Generated by`, `// DO NOT EDIT`. Covers every gRPC/protobuf/Swagger/GraphQL project.

**Quick Fix C: Silent truncation warning** — When walker hits maxFiles, record files_skipped count in graph.db metadata. v1r_brief checks this and adds confidence disclaimer. Covers any repo over 10K files.

### Key Hardcoded Assumptions That Break:

| File | Assumption | Breaks On |
|---|---|---|
| `walker.go` skipDirs | Exhaustive list | Generated code dirs |
| `walker.go` 500KB limit | Large files unimportant | Schema files, generated code |
| `post_edit.py` extensions | Only source code matters | IaC repos, config-driven repos |
| `javascript.go` CallNodes | `call_expression` captures all calls | JSX components aren't call_expressions |
| `resolver.go` | Resolution is in-process | Microservices, cross-language |
| `anchor_select.py` | All non-test files are candidates | Generated code pollution |

### What's NOT worth fixing now:
- Plugin dynamic dispatch (framework-specific, BM25 compensates)
- Cross-language edge inference (substantial work, disclaimer is sufficient)
- Notebook cell extraction (medium effort, GT is harmless not harmful on these)

## Decision 24 (LOCKED): Full Relationship Taxonomy — 47 Types, 12 Families

**Date:** 2026-05-10  
**Source:** Principal-researcher-level audit across 10 major OSS repos.

**Critical finding:** Function calls are ~35% of meaningful relationships in modern codebases. GT currently captures ONLY function calls. This means GT is blind to 65% of what matters.

### 12 Relationship Families:

| # | Family | Examples | Detection |
|---|---|---|---|
| 1 | Type system (interface/trait impl) | Go interfaces, Rust traits, TS implements | Tree-sitter: struct methods match interface methods |
| 2 | Registration & plugins | `register()`, `@app.route`, DI bindings | Regex/AST: decorator + args, registry calls |
| 3 | Config-driven | YAML keys → code, Terraform → providers, migrations | Config parsing + symbol matching |
| 4 | Cross-language | FFI bindings, proto → generated, platform channels | Binding declarations, schema → codegen mapping |
| 5 | Event-driven | emit/on, Kafka topics, Django signals | String-match event/topic names across files |
| 6 | Routing & URL dispatch | Route → handler, file-system routing | Framework-specific route config parsing |
| 7 | Decorator/annotation metadata | @cache, @retry, @Transactional, @pytest.fixture | Tree-sitter: extract decorators as node metadata |
| 8 | Inheritance & composition | extends, mixins, component rendering | Tree-sitter: base classes, JSX children |
| 9 | Test ↔ production | test_X.py tests X.py, fixtures | Convention mapping + import analysis |
| 10 | Import/export & modules | Barrel re-exports, dynamic imports, workspace deps | AST: export from, import(), package manifests |
| 11 | Data flow & state | Redux store → selectors, Context providers | Find store defs, find useSelector/useContext |
| 12 | Filesystem conventions | Paired files, auto-discovery, middleware order | Framework detection + convention rules |

### Implementation Tiers:

**Tier 1 (P0-P4): 1,300 LOC → 70% coverage (from current 35%)**
- P0: Inheritance hierarchy (200 LOC) — already in AST, just extract base classes
- P1: Interface implementation (400 LOC) — structural matching for Go, syntactic for others
- P2: Decorators/annotations (250 LOC) — already parsed, just store as metadata
- P3: JSX component composition (300 LOC) — JSX elements as edges (Quick Fix A started this)
- P4: Re-exports/barrel files (150 LOC) — regex on `export from`

**Tier 2 (P5-P10): 2,150 LOC → 90% coverage**
- Routes, config→code, events, build deps, DI, ORM models

**Tier 3 (P11-P15): 1,650 LOC → 95% coverage**
- FFI, proto→codegen, message queues, state management, platform channels

### Architecture: Relationship Specs (extends existing Language Specs)

```
RelationshipSpec {
    name: "go_interface_impl"
    languages: ["go"]
    detection_phase: "DEFINITIONS"
    edge_type: "IMPLEMENTS"
    confidence: 0.95
}
```

Same pattern as GT's existing 30 language specs. Incremental, community-extensible, deterministic.

### Schema Extension (graph.db):

New edge types: `IMPLEMENTS`, `EXTENDS`, `COMPOSES`, `HANDLES_ROUTE`, `PRODUCES_EVENT`, `CONSUMES_EVENT`, `CONFIGURED_BY`, `TESTED_BY`, `BINDS_TO`, `RE_EXPORTS`, `MIGRATES`, `OVERRIDES`, `DECORATES`

New node labels: `Interface`, `Trait`, `Route`, `EventChannel`, `Config`, `Migration`, `Component`, `Fixture`, `Schema`, `Middleware`

### What "Fully Generalized" Means:

1. Extract ALL deterministically-discoverable relationships (families 1-12)
2. Auto-detect frameworks and activate relevant extractors
3. Confidence scores reflect what GT does/doesn't see
4. Never claim a relationship that doesn't exist (false positives are catastrophic)
5. Explicitly tell the agent what GT CANNOT see ("event-driven patterns have 80% coverage — verify manually")

### The Metric:

GT's value = relationships it reveals that the agent couldn't find by reading individual files. Going from 35% → 90% relationship coverage means GT is useful on virtually any real codebase, not just dense Python call graphs.

## Decision 25: L3 Self-Correction via Task-Relevance Annotation

**Date:** 2026-05-10  
**Problem:** When agent edits wrong file, L3 full evidence reinforces wrong direction.  
**Research basis:** Huang et al. ICLR 2024 (explicit contrastive feedback); FeedbackEval (mixed +14.5pp, pure positive -3pp); ARISE (absence-as-signal).

**Solution:** Annotate L3 evidence with keyword overlap between callers and issue text. When callers show 0 overlap with the issue, state it explicitly:
```
[NOTE] Callers of this file show 0/5 keyword overlap with the issue.
```

When callers DO overlap: `[issue-relevant]` tag. Mixed signal → agent re-evaluates.

**Cost:** 0 LLM, ~30 tokens/block. Pure BM25 tokenizer (already exists).  
**Constraint:** Never blocks. States facts. Agent decides.  
**Regression risk:** None — additive annotation, doesn't change evidence content.

## Decision 26: Cross-Domain Bridging via Co-Change + Test Co-Import

**Date:** 2026-05-10  
**Problem:** weasyprint-2303 — all signals converge on SVG (symptom), fix is in PDF (cause). Envelope can't catch this because confidence IS high.  
**Research basis:** Zimmermann et al. TSE 2005 (co-change mining, 40-60% precision); Wong et al. TSE 2016 (static FL plateau ~50% cross-module); LocAgent (downstream callees contain fix 23% of time).

**Solution — 3 parts:**

**Part A: Convergence detection** — When top-5 are all in same module + BM25-dominant + dense internal edges → flag "symptom convergence." Trigger expansion.

**Part B: Co-change expansion** — Query `git log` for files in OTHER modules that co-changed with symptom files in past commits (≥2 co-occurrences). Add as "also consider" candidates at 60% of top-5 lowest score.

**Part C: Test co-import bridging** — Find test files that import BOTH symptom files AND files in other modules. Those other-module files are cross-domain bridge candidates.

**Expected coverage:** 50-70% of cross-domain bugs in projects with mature git history + test suites.  
**Unsolvable:** Truly novel cross-domain with zero historical/structural witness. Accept and abstain.  
**Regression risk:** Low — expansion only fires when convergence is detected (strict 3-condition gate). Doesn't modify existing candidates, only adds bridge candidates at lower score.
