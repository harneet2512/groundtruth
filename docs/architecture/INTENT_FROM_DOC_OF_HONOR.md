# Intent From DOC_OF_HONOR.md

Extracted 2026-05-27 from DOC_OF_HONOR.md on jedi__branch at 279945b5.
This document records what the architecture INTENDS, not what it achieves.

## Layer 0: Source Code → gt-index → graph.db

**Intent:** Go binary parses source with tree-sitter, extracts functions/classes/calls/imports, writes SQLite graph.db with 7 tables (nodes, edges, file_hashes, project_meta, properties, assertions, cochanges).

**Trigger:** CLI invocation `gt-index -root /path -output graph.db`

**Agent sees:** Nothing directly. graph.db is the substrate for all other layers.

**Key claims:**
- 8-pass pipeline: structure → definitions+imports → calls → properties+assertions → API edges → relationships → serde+twins → extras → file hashes → co-changes
- 3-stage resolution: same-file (1.0) → import-verified (1.0) → name-match (0.2-0.9)
- 23 property kinds from parser.go + main.go
- Multi-signal assertion resolution (TCTracer-inspired, threshold 3.5)
- Incremental reindex per file

**Stabilization finding:** L0 claims are code_audit only. No trajectory proof. Properties/assertions existence not verified on fresh graph.db in artifacts (graph.db not uploaded).

## Layer 1: L1 Brief — Task Start

**Intent:** At task initialization, generate a ranked file list with graph connections (callers, callees, contracts, tests) so the agent knows where to start.

**Trigger:** Wrapper startup, before agent's first action.

**Agent sees:**
```
<gt-task-brief>
1. path/to/file.py
   Functions: func_name(sig)
   Called by: caller_file.py:123
   Calls: dep.py
   Contract: func(params) -> ReturnType
   Tests: test_file.py
</gt-task-brief>
```

**Key claims:**
- Module: graph_map.py
- Queries nodes for functions per file, edges for callers (confidence >= 0.6) and callees (>= 0.6)
- Budget: 2000 chars

**Stabilization finding:** Delivered 5/5 tasks on proof run. VERIFIED by trajectory.

## Layer 1+: L1 Enhancement — Edit Plan + Key Contracts

**Intent:** Append edit-target function and key behavioral contracts to the brief.

**Trigger:** Pre-built graph.db exists with properties table.

**Agent sees:**
```
<gt-edit-target>
  Key function: func() in file.py, line N
  Signature: def func(...)
  N callers depend on this function
  Preserve: guard_clause: if X then Y
</gt-edit-target>
[GT KEY CONTRACTS]
  Preserve: guard_clause: ...
```

**Key claims:**
- Edit target selected by keyword overlap with issue text + caller count
- Key contracts from properties table (guard_clause, conditional_return, side_effect, exception_handler)
- Common-part stopwords filtered from matching

**Stabilization findings:**
- Edit target: wrong function selected on beets (Pipeline instead of set_fields). First-match-wins bug. BUG-003 open.
- Key contracts: marker was missing (BUG-002, fixed in stabilization). Now CONDITIONAL — fires when properties exist.

## Layer 3: L3 Post-Edit — After Agent Edits

**Intent:** After agent edits a file, show caller contracts, test assertions, behavioral contracts, signature, and completeness evidence.

**Trigger:** Agent runs file_editor edit operation.

**Agent sees:** Priority-ordered evidence within 2000 char budget:
- [BEHAVIORAL CONTRACT] / PRESERVE:
- [CALLERS] with code context
- Calls into:
- [SIGNATURE]
- [OVERRIDE]
- [TEST]
- [SIMILAR]
- [PATTERN]
- [COMPLETENESS]
- [MISMATCH]

**Key claims:**
- Module: post_edit.py
- U-shaped ordering (signature first for primacy, test last for recency)
- G7 silence gate: isolated functions suppress most evidence
- Confidence filter >= 0.6 on callers
- Budget: 2000 chars

**Stabilization findings:**
- PRIOR-003: [TEST] shows _common.py instead of relevant test. REPRODUCED.
- PRIOR-004: [COMPLETENESS] noisy — class-level, not function-scoped. REPRODUCED.
- PRIOR-008: [PATTERN] shows __init__. REPRODUCED.
- Delivered 5/5 tasks on proof run.

## Layer 3b: L3b Post-View — After Agent Reads

**Intent:** When agent reads a file, show callers/callees/importers with hub-penalized ranking.

**Trigger:** Agent runs file_editor view operation.

**Agent sees:**
```
Called by: file.py:45 `code` [tag], other.py::func (Nx)
Calls into: dep.py::func (Nx)
Imported by: user.py
```

**Key claims:**
- Module: post_view.py
- Confidence >= 0.7 on CALLS queries
- Hub-penalized ranking: score = cnt * (1 - min(1, in_degree/hub_scale))
- Visited-file suppression
- Issue-aware re-ranking

**Stabilization findings:**
- PRIOR-005: jquery.js appeared in callers. Fixed with vendor filter.
- Delivered 5/5 tasks on proof run.

## Layer 4a: L4a Auto-Query — First File Reads

**Intent:** On first 2 source file reads, show key symbols with callers.

**Trigger:** First read of non-test, non-scaffold source file (max 2 per task).

**Agent sees:**
```
[GT_AUTO] Key symbols in file.py:
  func() called by: file1.py:45, file2.py:120
```

**Stabilization finding:** Delivered on applicable tasks.

## Layer 5: L5 Scaffold Governor

**Intent:** Warn agent when it creates scratch files without making source edits.

**Trigger:** Scaffold file creation before any source edit.

**Agent sees:**
```
<gt-advisory layer="L5" trigger="non_source_without_progress">
Edit source files first.
</gt-advisory>
```

**Stabilization finding:** Fired on 1/5 tasks (beancount). Visible in output.

## Layer 5b: L5b Late Reminder

**Intent:** Remind agent of ignored structural witnesses. Suppressed by default (goku_active=1).

**Stabilization finding:** Visible on some tasks (scope check fires in finish handler). BUG-001 fixed telemetry truth.

## Layer 6: L6 Pre-Submit Review

**Intent:** Before agent submits, show blast radius (callers of changed exports) and test suggestions.

**Trigger:** Agent finish action.

**DOC_OF_HONOR status:** BROKEN (OH limitation). Finish handler runs after state=FINISHED.

**Stabilization fix:** Moved to L6 early review (fires after first source edit). Now visible on 2/3 tasks.

## Layer: Consensus / Scope

**Intent:** When agent views a brief candidate file before edits, show connected file scope.

**Trigger:** Agent views candidate file, no source edits yet.

**Agent sees:**
```
[GT] Scope: N files connected to this issue.
```

**Stabilization finding:** Delivered on applicable tasks.

## Layer: Grep Intercept

**Intent:** When agent greps for a symbol, inject callers from graph.

**Trigger:** Agent runs grep/rg command.

**Agent sees:**
```
[GT] Callers of 'symbol':
  file.py:45 `code`
```

**Stabilization finding:** Conditional. 0/5 visible (agent may not have grepped for function names).

## Infrastructure: Delivery Ledger

**Intent:** Every evidence delivery passes through _deliver_or_trace(). Logs DELIVERED/EMPTY/MISMATCH.

**Stabilization finding:** Exists but does not distinguish DEAD_WRITE from DELIVERED.

## Infrastructure: Stuck Detector Compat

**Intent:** Skip GT injection when agent is stuck (4+ identical action-observation pairs).

**Stabilization finding:** Working. 3-5 skips per task.

## Infrastructure: Hidden Prefixes

**Intent:** Filter [GT_META], [GT_STATUS], etc. from agent observations.

**Stabilization finding:** Working.

## Infrastructure: Dedup

**Intent:** MD5-based dedup per file per layer prevents duplicate evidence.

**Stabilization finding:** code_audit only. Not trajectory-verified.
