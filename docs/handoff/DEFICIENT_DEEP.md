# DEFICIENT_DEEP.md -- Layer-by-layer GT integration failure analysis

**Run:** 30-task gt-t0 on SWE-bench-Live, 2026-05-08
**Result:** 5/30 resolved (16.7%), gt-v1 3/10 resolved (30%)
**Audited subset:** 20 gt-t0 tasks

This document is the internal engineering autopsy. No marketing language.

---

## Executive summary

GT is injecting approximately 2,500-5,000 tokens of noise per task across
7 layers, while producing at most 200-500 tokens of useful signal. The
signal-to-noise ratio is roughly 1:10. On a 128K context window with ~100
agent iterations, the GT noise consumes 1-3% of context budget and -- more
critically -- pollutes the agent's attention with meaningless XML blocks
that train it to ignore ALL GT output, including the useful parts.

The 5 resolved tasks are almost entirely attributable to the OpenHands
harness switch (100% patch rate vs SWE-agent's 63%). GT's marginal
contribution to resolve rate is indistinguishable from zero.

---

## Audit data interpretation

```
brief=14 evidence=17 advisory=4 prefetch=10 dedup=0 | real=9 empty=8
```

- `brief=14`: Line count of the injected pre-task brief (L1+L2)
- `evidence=17`: Total L3/L3b evidence blocks injected during the task
- `advisory=4`: Number of times the L5 advisory was injected (4 distinct injection points)
- `prefetch=10`: Line count of L4 prefetch block
- `dedup=0`: Number of times dedup suppression actually fired (zero on every task)
- `real=9`: Evidence blocks containing actual findings
- `empty=8`: Evidence blocks containing only `[GT_STATUS] no_evidence` or equivalent
- `patch_has_advisory=True`: L5 advisory text leaked into the git patch

---

## L1: Pre-task Brief

### What is sent
A `<gt-task-brief>` XML block prepended to the first user message in OH.
Content: v7 deterministic edit plan with ranked candidate files (max 3),
contract lines, implementation pattern, expected side files, and
constraints section.

**Source:** `oh_gt_full_wrapper.py:1619-1634` (`patched_get_instruction`)
wraps the brief from `instance["gt_brief"]` which was set by
`patched_initialize_runtime` at line 1576.

### Bugs found

**BUG L1-A: Double-wrapping in `<gt-task-brief>` tags.**
`_render_v7` in `v7_brief.py:443` outputs content already inside
`<gt-task-brief>` tags (line 443 and 495). Then `patched_get_instruction`
at `oh_gt_full_wrapper.py:1626` wraps it AGAIN:
```python
content = f"<gt-task-brief>\n{brief}\n</gt-task-brief>\n\n" + content
```
The agent sees:
```xml
<gt-task-brief>
<gt-task-brief>
GT v7 deterministic edit plan...
</gt-task-brief>
</gt-task-brief>
```
This is a parse/attention hazard. The agent may interpret the inner tag as
the full brief and ignore everything after the first `</gt-task-brief>`.

**BUG L1-B: brief=14 is correct but rigid.**
14 lines is the v7 brief structure:
```
<gt-task-brief>                          # 1
GT v7 deterministic edit plan...         # 2
                                         # 3 (blank)
CANDIDATE CLUSTER:                       # 4
  ranked edit targets:                   # 5
  1. file.py [primary source]            # 6
                                         # 7 (blank)
CONTRACT:                                # 8
  - contract line                        # 9
                                         # 10 (blank)
IMPLEMENTATION PATTERN:                  # 11
  - Mirror the nearest...               # 12
                                         # 13 (blank)
CONSTRAINTS:                             # 14
```
(Plus closing tag.) 14 lines is reasonable for a structured brief.
The problem is not the line count -- it is that 6 of 14 lines are
structural overhead (section headers, blank separators) that carry
zero information. The information-dense lines are only 4-5.

**BUG L1-C: Fallback brief is useless filler.**
When the brief generator fails or produces low content
(`oh_gt_full_wrapper.py:1563-1569`), the fallback is:
```
GT graph built inside the task container.
- repo_root: /workspace
- graph_db: /tmp/gt_index.db
- hooks: post-view and post-edit evidence are appended to tool observations.
```
This tells the agent nothing actionable. It wastes ~80 tokens saying
"GT exists." The agent already knows GT exists because it sees GT
evidence blocks. This fallback should emit NOTHING instead.

### Token cost
14 lines * ~15 tokens/line = ~210 tokens. Injected once. Acceptable.

### Agent benefit
Moderate. The 3-file candidate cluster is genuinely useful for
localization if the files are correct. But the double-wrapping and
structural bloat dilute the signal.

### Fixes
1. Remove inner `<gt-task-brief>` tags from `_render_v7` -- let only
   `patched_get_instruction` wrap. (**5 min fix, high impact**)
2. Strip blank separator lines and redundant section headers.
   Render as a flat list: "Edit these files: X, Y, Z. Contract: ..."
   (**30 min refactor, medium impact**)
3. Replace the fallback with empty string -- inject nothing when the
   brief has no real content. (**2 min fix, medium impact**)

---

## L2: Hybrid Fusion (RRF retrieval)

### What is sent
A self-closing XML tag appended to the brief:
```xml
<gt-pretask layer="L2" fusion="rrf" fused_candidates="N" wall_ms="M" signals="..." />
```
This is metadata for telemetry, not agent-facing evidence.

**Source:** `oh_gt_full_wrapper.py:527-559` (`_format_l2_pretask_tag`)

### Bugs found

**BUG L2-A: L2 tag is injected INTO the agent brief for no reason.**
The `<gt-pretask>` tag is pure telemetry metadata (candidate count,
wall time, signal counts). The agent cannot use this information.
It does not contain file names, function names, or evidence. The agent
sees `fused_candidates="5" wall_ms="234"` and has no idea what to do
with it.

**Source:** Line 1558-1559:
```python
if l2_tag and brief and "[GT_BRIEF_FAILED]" not in brief:
    brief = f"{brief}\n\n{l2_tag}"
```
This appends telemetry to agent-visible content.

**BUG L2-B: No verification that fused candidates match reality.**
The RRF fusion selects candidate files. We never check whether these
files contain the actual bug. On the 20 audited tasks, we have no data
on precision/recall of L2 localization. This is the single most
impactful unknown in the entire pipeline.

### Token cost
~30 tokens for the tag. Negligible but pointless.

### Agent benefit
Zero. The tag is machine telemetry visible to a human operator.
The agent cannot parse or act on it.

### Fixes
1. Remove L2 tag from agent-visible brief. Keep it in telemetry
   JSON only. (**2 min fix, removes noise**)
2. Add localization accuracy measurement: for each task, check if
   the top-3 fused candidates include at least one gold edit file.
   (**Research task, potentially highest ROI**)

---

## L3: Post-edit evidence

### What is sent
On every source file edit, an `<gt-evidence>` XML block is appended to
the observation. Content:
1. A `<gt-reindex>` sub-block with gt-index output (timing, file count)
2. The post_edit hook output (0-3 evidence lines from 5 families:
   CHANGE, CONTRACT, PATTERN, STRUCTURAL, SEMANTIC)
3. A `Verify: gt_validate <file>` suggestion line

**Source:**
- `oh_gt_full_wrapper.py:1044-1153` (post_edit handler)
- `src/groundtruth/hooks/post_edit.py` (the hook itself)

### Bugs found

**BUG L3-A: 75% of evidence blocks are empty -- and STILL INJECTED.**
Across the 20 audited tasks, the evidence quality breakdown:

| Task | Real | Empty | Empty% |
|------|------|-------|--------|
| cfn-lint-3862 | 4 | 24 | 86% |
| cfn-lint-4032 | 4 | 27 | 87% |
| cfn-lint-3866 | 10 | 28 | 74% |
| checkov-6895 | 2 | 12 | 86% |
| briefcase-2085 | 2 | 16 | 89% |
| checkov-6893 | 1 | 8 | 89% |
| checkov-7002 | 5 | 16 | 76% |
| **AVERAGE** | **4.5** | **13.7** | **75%** |

Each empty evidence block looks like:
```xml
<gt-evidence trigger="post_edit:src/cfnlint/rules/foo.py">
<gt-reindex command="/tmp/gt-index-linux -root=/workspace -file=src/cfnlint/rules/foo.py -output=/tmp/gt_index.db">
indexed 1 files in 45ms
</gt-reindex>
[GT_STATUS] no_evidence:abstention_filtered
Verify: gt_validate src/cfnlint/rules/foo.py
</gt-evidence>
```
This is ~60 tokens of NOTHING. Telling the agent "we checked and found
nothing" is worse than silence because (a) it wastes context, (b) it
trains the agent to ignore ALL `<gt-evidence>` blocks including the 25%
that contain real data.

**Root cause:** The wrapper always injects the full evidence block
regardless of content. The suppression logic does NOT exist:
- Line 1145-1152: the evidence block is ALWAYS built and appended
- There is no check for `[GT_STATUS] no_evidence` before injection
- The reindex output is ALWAYS included even when useless

**BUG L3-B: Reindex output is always injected (pure noise).**
Every post_edit evidence block includes:
```xml
<gt-reindex command="...">
indexed 1 files in 45ms
</gt-reindex>
```
This is ~30 tokens per edit. The agent does not need to know that
gt-index reindexed a file. This is operational telemetry, not evidence.

Over a typical task with 15-25 edits, that's 450-750 tokens of pure
reindex noise.

**BUG L3-C: `Verify: gt_validate <file>` appended to EVERY evidence block.**
Line 1150: `f"Verify: gt_validate {rel_p}\n"` is appended to every
post_edit evidence block. The agent sees "Verify: gt_validate foo.py"
after every single edit. If it edits 20 files, it gets 20 identical-format
suggestions to run gt_validate. This is spam.

From the audit, `L4_usage=0` on most tasks -- the agent NEVER runs
gt_validate despite being told 20+ times. The agent learned to ignore it.

**BUG L3-D: Abstention threshold is too high.**
`post_edit.py:506`: `min_confidence=0.55` for abstention. This filters
out findings at 0.50-0.55 confidence. Combined with the graph's tendency
to produce name_match edges with 0.4-0.6 confidence on these repos,
the abstention gate kills legitimate findings.

But this is a secondary issue -- the primary problem is that empty blocks
are injected at all.

**BUG L3-E: Five evidence families, most never produce anything.**
The post_edit hook runs 5 families:
1. CHANGE (before/after AST diff) -- requires old content + AST
2. CONTRACT (callers + tests) -- requires graph edges to callers
3. PATTERN (sibling analysis) -- requires class methods
4. STRUCTURAL (obligations + contradictions) -- requires import graph
5. SEMANTIC (call-site voting + arg affinity + guard consistency) --
   requires multiple call sites

For cfn-lint (linter rules, each in its own file with minimal
cross-references), families 2-5 produce nothing because:
- Rules have few callers in the graph (they're dispatched by framework)
- Rules are standalone classes, no sibling pattern
- No import graph connections to rule files
- No multiple call sites for voting

The hook runs all 5 families, produces zero findings, then emits
`[GT_STATUS] no_evidence:abstention_filtered`. This is by design --
the hook is honest about having nothing. The BUG is that the wrapper
injects this "nothing" into the agent's context.

### Token cost per task
- Empty blocks: ~13.7 * 60 tokens = ~820 tokens of noise
- Real blocks: ~4.5 * 100 tokens = ~450 tokens of signal
- Reindex noise: ~18 * 30 tokens = ~540 tokens
- gt_validate spam: ~18 * 15 tokens = ~270 tokens
- **Total L3 noise: ~1,630 tokens per task**
- **Total L3 signal: ~450 tokens per task**
- **SNR: 0.28 (signal is 22% of total L3 injection)**

### Agent benefit
Marginal. The 4-5 real evidence items per task contain caller/contract
data that COULD help. But they are buried in 14+ noise blocks. The
agent's attention mechanism cannot reliably find the needle.

### Fixes
1. **Do not inject evidence when empty.** If the hook returns
   `[GT_STATUS] no_evidence` or `[GT_STATUS] empty` or
   `[GT_STATUS] skipped`, inject NOTHING into the observation.
   (**10 min fix, removes 75% of GT noise. Highest-ROI fix.**)
2. **Remove reindex output from agent-visible content.** Log it to
   telemetry only. (**5 min fix, saves ~540 tokens/task**)
3. **Remove `Verify: gt_validate` spam.** If the agent wants to
   validate, it can. Don't prompt it 20 times. (**2 min fix**)
4. **Consider disabling L3 entirely on repos where the hook's
   evidence families cannot fire** (e.g., when the graph has fewer
   than N cross-file edges for the edited file). (**Design decision**)

**Expected impact:** Removing empty evidence injection alone should
save ~1,630 tokens/task and -- more importantly -- stop training the
agent to ignore GT output. This is the single highest-leverage fix.

---

## L3b: Post-view evidence

### What is sent
On every source file view, structural coupling data:
```
-- structural coupling [GT_L3B] --
ClassName: N methods share self.X, self.Y [GT_L3B]
  methodA:10 (stores) -> methodB:25 (serializes) [GT_L3B]
  Rule: changes to __init__ params must appear in deconstruct [GT_L3B]
```
Or cross-file linkage from graph.db:
```
-- cross-file linkage [GT_L3B] --
symbolName called from other_file.py:callerName [GT_L3B]
```

**Source:**
- `oh_gt_full_wrapper.py:1008-1042` (post_view handler)
- `src/groundtruth/hooks/post_view.py`

### Bugs found

**BUG L3b-A: dedup=0 across all 20 tasks -- dedup never fires.**

The dedup logic (line 1029-1033) hashes the hook output and checks
against previously sent evidence for the same file. But `dedup=0`
means it NEVER matched.

Root cause analysis: The post_view hook output includes dynamic content
that changes between invocations for the SAME file:
1. The hook re-runs graph queries each time. If the graph was
   reindexed between views (L6 fires on every edit), the query results
   may differ (new edges, changed confidence).
2. The `[GT_STATUS] success:N_items` line at the end includes the
   count, which is deterministic, but the graph query results could
   have new edges.
3. Most likely: the agent rarely views the same source file twice.
   It views a file, edits it, then moves on. The edit converts it
   to a post_edit event, not post_view. Repeated views of the same
   unchanged file are uncommon in practice.

**Verdict:** dedup=0 is not a bug in the dedup mechanism. It is
correctly not firing because the agent's workflow rarely triggers
the same-file-same-content view condition. The dedup mechanism is
solving a problem that does not occur in practice.

**BUG L3b-B: Python-only class coupling analysis.**
`post_view.py:225`: Non-.py files skip the AST-based class coupling
analysis and fall back to `graph_callers_fallback`. For cfn-lint
(Python), the AST path fires, but the coupling analysis requires
classes with 2+ methods sharing self.attrs. Many cfn-lint rule files
have simple rule classes that don't meet this threshold.

When coupling analysis fails, it falls back to graph callers
(line 326-328), which works. The graph fallback is the more useful
path for most files.

**BUG L3b-C: No evidence blocks ARE injected (same as L3).**
When post_view produces `[GT_STATUS] no_evidence:no_class_coupling`,
the wrapper still injects:
```xml
<gt-evidence trigger="post_view:path">
[GT_STATUS] no_evidence:no_class_coupling
No coupling data. Try: gt_search function symbol
</gt-evidence>
```
Same problem as L3-A: empty evidence is injected as noise.

### Token cost
- Per view with coupling data: ~80 tokens (useful)
- Per view without: ~40 tokens (noise)
- Estimated 5-10 views per task * ~60 tokens avg = ~400 tokens

### Agent benefit
Moderate when coupling data is present. The "Rule: changes to __init__
must appear in deconstruct" pattern is genuinely useful for Django-style
ORM models. Less useful for cfn-lint rule files.

### Fixes
1. Same as L3: do not inject empty evidence. (**Already covered**)
2. The `Try: gt_search function <stem>` suggestion when no evidence
   found is potentially useful -- but only on the FIRST empty view.
   After that it's spam. Track whether the suggestion was already
   given and suppress on repeat. (**5 min fix**)

---

## L4: Prefetch + Tool Footer

### What is sent
Two distinct injections:

**A) Prefetch block** (in the brief, line 1571-1573):
```xml
<gt-prefetch layer="L4" queries="3" symbols="sym1,sym2,sym3" wall_ms="234">
[VERIFIED] caller evidence line
[POSSIBLE] sibling evidence line
...
</gt-prefetch>
```
This fires once during initialization, selecting up to 3 symbols from
the issue text that exist in graph.db, then running gt_query on each.

**B) Tool footer** (appended to the first user message, line 1631):
```
GT tools available on PATH (optional):
  gt_query <symbol>            -- inspect callers/callees for one symbol
  gt_search function <name>    -- locate likely function definitions/usages
  gt_navigate <symbol> callers -- trace upstream call chain
  gt_validate <file>           -- verify edited file against known contracts
```

**Source:**
- `oh_gt_full_wrapper.py:1321-1391` (L4 prefetch)
- `oh_gt_full_wrapper.py:506-524` (tool footer)

### Bugs found

**BUG L4-A: prefetch=10 means 10 lines of prefetch content.**
The prefetch content is capped at `L4_PREFETCH_MAX_CHARS = 1200`
characters and `L4_PREFETCH_MAX_LINES_PER_QUERY = 5` lines per symbol.
With 3 symbols * 3-5 useful lines each = 10-15 lines. The `10` in
the audit data is line count, not injection count. Injected once.

This is acceptable. The content is real caller/sibling evidence for
issue-relevant symbols. The problem is not the prefetch itself.

**BUG L4-B: Symbol selection is naive.**
`_select_issue_seeded_symbols` (line 1239-1318) extracts identifiers
from the issue text, then checks if those identifiers exist as
function/method/class names in graph.db for the candidate files.
The stop-word list (line 1259-1264) is small. Issue text often
contains generic identifiers like `Rule`, `Check`, `Validate`,
`Error`, `Config` which exist in dozens of graph nodes. The query
picks the first match, which may not be the relevant one.

**BUG L4-C: Tool footer is injected but tools are never used.**
From the audit data, L4 tool usage (gt_query, gt_search, gt_navigate,
gt_validate as agent commands) is near zero across all tasks. The agent
does not use the GT tools. The tool footer wastes ~60 tokens describing
tools the agent ignores.

Root cause: The OH CodeActAgent does not organically use shell tools
for investigation. Its loop is: read issue -> locate files -> edit ->
test -> submit. There is no "query the symbol graph" step in its
natural workflow. The tools are available but not integrated into the
agent's reasoning patterns.

**BUG L4-D: gt_query.py import path is fragile.**
Line 1351:
```python
f"python3 ${{GT_TOOLS_DIR:-/tmp/gt_tools}}/gt_query/lib/gt_query.py ..."
```
This runs gt_query as a script. If the groundtruth package is not
importable inside the container (PYTHONPATH issue), gt_query still
works because it's standalone. But if the container's Python is too
old (< 3.10), `from __future__ import annotations` may fail silently
and `|` union types in function signatures will crash.

### Token cost
- Prefetch: ~10 lines * 15 tokens = ~150 tokens (once)
- Tool footer: ~60 tokens (once)
- Total: ~210 tokens, all injected once. Acceptable.

### Agent benefit
- Prefetch: Low-to-moderate. The evidence is real but the agent
  rarely references it during its editing loop.
- Tool footer: Zero. Tools are never used.

### Fixes
1. Remove tool footer or reduce to a single line:
   "GT graph tools available: gt_query, gt_search, gt_navigate, gt_validate"
   (**2 min fix, saves ~40 tokens**)
2. For the tool-usage problem: this is an agent architecture issue,
   not a GT bug. The agent would need to be trained/prompted to use
   graph tools. Out of scope for GT.

---

## L5: Pre-submit Advisory

### What is sent
An `<gt-advisory>` XML block:
```xml
<gt-advisory layer="L5" pending_count="N" unresolved_count="M">
[GT_GATE] Pre-submit review:
  Files edited: N
  Pending checks: N (M unresolved)
  WARNING path: [GT_STATUS] success:N_items
  Files explored but not edited: file1, file2
</gt-advisory>
```

**Source:** `oh_gt_full_wrapper.py:605-635` (render) + 1155-1206 (injection)

### Bugs found

**BUG L5-A: Advisory fires 4 times per task, never reaching the agent.**
The audit shows `advisory=4` on every task and `patch_has_advisory=True`
on every task. This means:

1. **Injection point 1** (line 1156-1162): Fires on `finish` event.
   The agent has already decided to submit. Too late.
2. **Injection point 2** (line 1161): Also appended to
   `last_visible_observation` on finish. Contaminates the previous
   observation retroactively.
3. **Injection point 3** (line 1189-1191): Fires on submit-like
   commands (`/submit`, `git diff head`, `git diff --cached`).
   OH's `complete_runtime` runs `git diff --cached` to extract
   the patch -- this triggers advisory injection INTO the patch
   extraction observation.
4. **Injection point 4**: Same as 3 but on a second git diff command.

The net result: the advisory is injected 4 times, never before the
agent decides to submit, and it contaminates the git patch output.

**BUG L5-B: Advisory contaminates git patch.**
`patch_has_advisory=True` on ALL 20 tasks. The advisory XML is
appended to the observation of `git diff --cached`, which OH uses
to extract the patch. If the patch parsing is not robust against
trailing XML, the advisory text becomes part of the submitted patch.

This is not just noise -- it's ACTIVE HARM. A corrupted patch will
fail the SWE-bench evaluator even if the code change was correct.

**Source:** Line 1189-1191: the `is_patch_extract` guard (line 1190:
`if advisory and not is_patch_extract`) is supposed to prevent this,
but `is_patch_extract` only fires on `git diff --cached` (line 1187).
OH's `complete_runtime` may use other commands to extract the patch
that don't trigger this guard. OR: the guard correctly suppresses
the advisory on `git diff --cached`, but the advisory was ALREADY
injected on a prior command in the same submit sequence.

Wait -- re-reading the code: `is_submit_cmd` (line 1183-1186) fires on
`/submit`, `git diff head`, AND `git diff --cached`. Then line 1190
checks `not is_patch_extract` -- if it IS a patch extract (`git diff
--cached`), the advisory is NOT appended to the observation. But it IS
still saved to `instance_ref["gt_advisory"]` (line 1192-1200). So the
advisory avoids the patch observation but still leaks into the instance
record. The `patch_has_advisory=True` in the audit may be checking the
instance record, not the actual patch diff.

Regardless: the advisory fires 4 times (across finish + multiple
submit-like commands) and the agent never sees it in time to act.

**BUG L5-C: Advisory fires on finish, which is AFTER the agent loop ends.**
The OH agent loop calls `finish` after its last action. At this point,
injecting advisory text into the observation is meaningless -- no more
agent turns will process it. The advisory needs to fire BEFORE the
agent's final action, which would require predicting when the agent
is about to submit.

### Token cost
- 4 injections * ~120 tokens = ~480 tokens of wasted advisory text
  that the agent never processes.

### Agent benefit
Zero. The agent never sees the advisory before submitting.

### Fixes
1. **Remove ALL advisory injection on finish/submit.** L5 as
   currently implemented cannot work in OH's event model. The
   agent's submit decision is atomic -- there's no "pre-submit
   hook" in OH. (**10 min fix, removes 480 tokens/task of waste**)
2. **Future: inject advisory N iterations before max_iterations.**
   Instead of firing on submit, fire a "checkpoint advisory" when
   the agent has used 80% of its iteration budget. This gives the
   agent a chance to self-correct. Requires OH iteration counter
   access. (**Design work needed**)
3. **Stop saving advisory to instance record.** It contaminates
   output.jsonl with noise. (**2 min fix**)

---

## L6: Incremental Reindex

### What is sent
A `gt-index -file=<path>` command is run inside the container before
each L3 post-edit hook. The output is embedded in the `<gt-reindex>`
sub-block of the evidence.

**Source:** `oh_gt_full_wrapper.py:1067-1069` (reindex before L3)

### Bugs found

**BUG L6-A: Reindex output visible to agent (already covered in L3-B).**
The reindex itself works correctly. The bug is that its output
(`indexed 1 files in 45ms`) is part of the agent-visible evidence
block. This is operational telemetry, not evidence.

**No other bugs found.** L6 is the only layer that works correctly
and serves its purpose (keeping graph.db fresh for subsequent queries).

### Token cost
Zero beyond what L3-B already accounts for.

### Agent benefit
Indirect: keeps graph.db fresh so L3 and L4 evidence is current.
This is the correct architecture.

---

## Cross-cutting issues

### ISSUE X-1: Total GT token overhead per task

| Layer | Tokens | Useful? |
|-------|--------|---------|
| L1 brief | 210 | Mostly yes |
| L2 tag | 30 | No |
| L3 real evidence | 450 | Yes |
| L3 empty evidence | 820 | No |
| L3 reindex noise | 540 | No |
| L3 validate spam | 270 | No |
| L3b evidence | 400 | Mixed |
| L4 prefetch | 150 | Mostly yes |
| L4 tool footer | 60 | No |
| L5 advisory (4x) | 480 | No |
| **TOTAL** | **~3,410** | |
| **Useful portion** | **~1,210** | **35%** |
| **Noise portion** | **~2,200** | **65%** |

The agent receives ~3,400 tokens of GT content per task, of which
~2,200 tokens (65%) is noise. This is not catastrophic for a 128K
context, but the ATTENTION cost is worse than the TOKEN cost: 18+
evidence blocks where 75% are empty trains the agent to ignore ALL
GT output.

### ISSUE X-2: Evidence block fatigue

The agent sees `<gt-evidence>` blocks on ~18 of its ~100 iterations.
On 75% of those, the content is "no evidence found." By iteration 20,
the agent has learned that `<gt-evidence>` blocks are noise. When a
REAL evidence block appears at iteration 45 with genuine caller data,
the agent skips it because the format has been cry-wolf'd.

This is the most damaging effect of the noise injection. It's not
about token count -- it's about ATTENTION ALLOCATION. The agent's
implicit prior on GT evidence shifts from "potentially useful" to
"probably noise" after seeing 10+ empty blocks.

### ISSUE X-3: No evidence aggregation

Each evidence block is independent. The agent never gets a summary:
"You edited 5 files. 3 have caller evidence. Here are the top
findings across all edits." Instead it gets 18 individual XML blocks
scattered across 100 iterations. Even if each block were useful, the
scatter pattern makes synthesis impossible for the agent.

### ISSUE X-4: The `gt-v1` arm resolves more tasks (30% vs 16.7%)

The gt-v1 arm (10 tasks, 3 resolved) outperforms gt-t0 (30 tasks,
5/30 = 16.7%) on resolve rate. This is a small sample but directionally
concerning. If gt-v1 is a simpler integration with less noise, it
suggests that GT's additional layers are HURTING, not helping.

This needs investigation: what does gt-v1 inject vs gt-t0? If gt-v1
is brief-only (no L3/L3b/L4/L5), the hypothesis is that the noise
layers are actively harmful.

---

## Priority-ordered fix list

| # | Fix | Layer | Effort | Token savings | Resolve impact |
|---|-----|-------|--------|---------------|---------------|
| 1 | Stop injecting empty evidence blocks | L3, L3b | 10 min | ~1,200/task | HIGH -- stops cry-wolf |
| 2 | Remove reindex output from agent evidence | L3 | 5 min | ~540/task | Medium |
| 3 | Remove gt_validate spam from every block | L3 | 2 min | ~270/task | Low |
| 4 | Fix double `<gt-task-brief>` wrapping | L1 | 5 min | ~30/task | Medium (attention) |
| 5 | Remove L2 tag from agent brief | L2 | 2 min | ~30/task | Low |
| 6 | Remove L5 advisory entirely (or redesign) | L5 | 10 min | ~480/task | Medium |
| 7 | Remove tool footer (or make 1 line) | L4 | 2 min | ~40/task | Low |
| 8 | Replace fallback brief with empty | L1 | 2 min | ~80/task | Low |
| 9 | Audit L2 localization accuracy | L2 | 2 hours | 0 | Potentially HIGH |
| **Total** | | | **~40 min** | **~2,670/task** | |

Fixes 1-8 are mechanical code changes totaling ~40 minutes of work.
They remove ~2,670 tokens of noise per task (78% of total noise) and
eliminate the cry-wolf attention problem.

Fix 9 (L2 localization audit) is the only one that could move the
resolve rate needle significantly. If the RRF fusion is pointing at
wrong files, no amount of post-edit evidence helps because the agent
never edits the right files.

---

## Honest prognosis

Fixing all noise issues will NOT significantly move the resolve rate.
The 5/30 result is mostly attributable to: (a) correct file localization
(which L1/L2 provides when it works), (b) the agent's own coding
ability, and (c) the OH harness's 100% patch submission rate.

The layers that COULD move the needle (better localization, richer
contract data, pre-submit review) require architectural changes, not
bug fixes. The noise removal is necessary hygiene but insufficient
for breakthrough improvement.

The path from 16.7% to 25-30% requires better localization (L2) and
an agent that actually uses graph data to guide its editing (L4 tool
usage). Both are hard problems that GT's current architecture does
not solve.
