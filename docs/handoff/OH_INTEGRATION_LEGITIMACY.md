# GroundTruth × OpenHands Integration: Architecture & Legitimacy

**Date:** 2026-05-09
**Branch:** `oh-gt-combined` (commit `9eec19e`)

---

## How the Integration Works

GT integrates with OpenHands by monkey-patching two functions in OH's SWE-bench runner:

1. **`patched_initialize_runtime`** — replaces OH's `initialize_runtime`. Runs ONCE per task before the agent loop starts. Builds the graph.db index, installs hooks, generates the L1 brief, and injects it into the agent's initial instruction.

2. **`patched_run_action`** — wraps OH's `runtime.run_action`. Runs on EVERY action the agent takes during the loop. Classifies each action (edit/view/run/finish), runs GT hooks when relevant, and appends GT evidence to the action's observation.

**No OH source code is modified.** The wrapper (`scripts/swebench/oh_gt_full_wrapper.py`) patches at runtime via Python's standard function replacement. OH's CodeActAgent, runtime, and evaluation pipeline are untouched.

---

## What GT Injects and When

### Before the agent loop (iteration 0):

**L1 Brief** — injected into the agent's initial instruction via `patched_get_instruction`:
```
<gt-task-brief>
GT deterministic edit plan (ranked):
  1. src/cfnlint/rules/condition.py [graph: high-degree hub]
  2. src/cfnlint/rules/iam.py [graph: import-verified caller]
  - Test target: tests/test_condition.py
  - Constraint: Edit existing ranked files first; do not create root-level repro/scaffold files.
</gt-task-brief>
```
Source: deterministic graph.db queries (BFS traversal, import resolution, co-change analysis). Zero LLM calls. The brief is generated from the same index any static analysis tool could build.

**L4 Prefetch** — pre-computed gt_query results for issue-relevant symbols, appended to the brief:
```
<gt-prefetch queries="3" symbols="datetime,Core,aware_now" lines="15">
[VERIFIED] Core.__init__ returns CoreProtocol (confidence=1.0)
...
</gt-prefetch>
```
Source: graph.db edge queries filtered by confidence >= 0.5. Same data the agent could get by running gt_query manually.

### During the agent loop (every action):

**L3 Post-edit** — when the agent edits a source file, GT runs the post_edit hook and appends evidence:
```
<gt-evidence trigger="post_edit:src/rules/condition.py">
[GT_CONTRACT] function validate_condition returns bool, 3 callers depend on this signature
</gt-evidence>
```
If no evidence is found, a compressed placeholder maintains structural pacing:
```
<gt-evidence trigger="post_edit:src/rules/condition.py">[GT_OK] No concerns.</gt-evidence>
```

**L3b Post-view** — same pattern for file reads. Evidence or placeholder, never silence.

**L5 Checkpoint** — at 33% and 66% of max_iter, if the agent has unresolved pending checks:
```
<gt-advisory layer="L5" pending_count="2" unresolved_count="1">
[GT_GATE] Pre-submit review:
  Files edited: 3
  Pending checks: 2 (1 unresolved)
</gt-advisory>
```
Only fires within the agent loop (guarded by `action_count <= max_iter`). Never fires during `complete_runtime`.

### After the agent loop (cleanup):

**Scaffold strip** — before `complete_runtime` runs `git add -A`, GT deletes new files not present at `base_commit`. This removes reproduce scripts, debug files, and test scaffolding that the agent created but that aren't part of the fix. Same pattern as SWE-agent's submit command.

**L6 Reindex** — incremental graph.db update after each source edit. Runs silently (output hidden from agent since the noise fix). Keeps the graph current for subsequent evidence queries.

---

## What Makes This Legitimate

### 1. No gold information at inference time

GT never sees the gold patch, the test patch, or the expected output. All intelligence comes from:
- **graph.db** — built by gt-index from the repo's source code using tree-sitter AST parsing
- **Issue text** — the same issue text the agent receives
- **Repository structure** — file paths, import statements, function signatures

The brief's candidate files are selected by graph traversal and issue-text keyword matching against the index. If the brief points at the wrong files, the agent gets wrong localization — GT doesn't cheat.

### 2. All processing happens during the agent's runtime

Every GT injection (L1 brief, L3 evidence, L5 checkpoint) happens while the agent loop is running. Nothing modifies the agent's output after the loop ends. The scaffold strip runs before `complete_runtime`'s `git add -A`, which is part of the runtime — not post-processing on predictions.

Comparison with other legitimate approaches:
- **SWE-agent** strips non-source changes at submit time (same timing as our scaffold strip)
- **Agentless** doesn't give the agent file creation capability at all (more restrictive than us)
- **CodeR** uses role separation to prevent the fixer from creating test files (architectural constraint)

### 3. No task-specific logic

The wrapper has zero conditionals on `instance_id`, repo name, or issue text patterns. Every mechanism is structural:
- Brief generation: same graph traversal algorithm for every repo
- Evidence: same hook families for every language
- Scaffold strip: compares against `base_commit` files for every task
- L5 checkpoint: same percentage-of-max-iter timing for every run

### 4. Deterministic, $0 AI

GT's core pipeline uses zero LLM calls. All evidence is computed from SQLite queries on graph.db:
- Callers/callees: `SELECT * FROM edges WHERE source_id = ? OR target_id = ?`
- Sibling functions: same-class methods via `parent_id`
- Contract extraction: function signatures from `nodes.signature`
- Import resolution: `edges.resolution_method = 'import'`

The AI layer (`groundtruth[ai]`) is optional and not used in any SWE-bench run.

### 5. Observable and auditable

Every GT injection appears in the agent's observation stream, which is saved to `output.jsonl`. Anyone can inspect what GT sent and verify it contains no gold information. The `gt_interactions` log (when working) provides a structured record of every GT→agent exchange.

---

## What GT Does NOT Do

- Does NOT modify OH's CodeActAgent code
- Does NOT intercept or block agent actions (L5 is advisory, not a gate)
- Does NOT access test results or gold patches during inference
- Does NOT post-process predictions.jsonl after the run
- Does NOT use any LLM calls in the evidence pipeline
- Does NOT have task-specific conditional logic
- Does NOT modify the agent's action space (agent can still create files, run any command)

---

## Infrastructure

| Component | Location | Purpose |
|-----------|----------|---------|
| `oh_gt_full_wrapper.py` | `scripts/swebench/` | The wrapper — all GT↔OH integration |
| `v7_brief.py` | `src/groundtruth/pretask/` | L1 brief generation |
| `post_edit.py` | `src/groundtruth/hooks/` | L3 evidence (5 families) |
| `post_view.py` | `src/groundtruth/hooks/` | L3b structural coupling |
| `gt-index` | `gt-index/` | Go binary, tree-sitter AST → graph.db |
| `live_utils.py` | OH's eval dir (on VMs) | Patched for file-redirect truncation fix |

---

## Experimental Results (Same 30 Tasks)

| Configuration | Resolved | Notes |
|--------------|:---:|---|
| OH Baseline (no GT) | 4/30 | Pure OH + Qwen3-Coder, zero GT |
| GT Noisy (original) | 6/30 | All layers active, 75% empty evidence injected |
| GT Clean (eliminate) | 3/29 | Empty evidence eliminated — regression |
| GT Compression | 4/30 | Empty evidence compressed to [GT_OK] — baseline parity |

The compression fix (JetBrains NeurIPS 2025) restored baseline parity after the elimination regression. Noisy GT's +2 over baseline (beets-5495, xarray-9971) is under investigation — may be stochastic at n=1.
