# Deep Analysis: oh_gt_full_wrapper.py Evidence Delivery Pipeline

**File:** `D:\Groundtruth\scripts\swebench\oh_gt_full_wrapper.py` (5122 lines)
**Date:** 2026-05-22

---

## 1. GTRuntimeConfig (lines 270-340)

The `GTRuntimeConfig` dataclass is the per-task state object that tracks everything about a single agent session.

### Core State Fields
| Field | Type | Purpose |
|---|---|---|
| `workspace_root` | str | Container workspace path (default `/workspace`) |
| `graph_db` | str | Path to SQLite graph DB in container (`/tmp/gt_index.db`) |
| `gt_index_bin` | str | Path to gt-index binary in container |
| `tools_dir` | str | Path to GT tools directory in container |
| `max_items` | int | Max items per evidence response (3) |
| `source_exts` | tuple | File extensions treated as source code |
| `action_count` | int | Running count of CmdRunAction events (iteration counter) |
| `max_iter` | int | Max iterations for the agent (default 100) |

### Evidence Tracking Fields
| Field | Type | Purpose |
|---|---|---|
| `pending_checks` | set[str] | Files that GT flagged as needing validation |
| `verified_checks` | set[str] | Files where gt_validate was run |
| `edited_files` | set[str] | All files the agent has edited |
| `viewed_files` | set[str] | All files the agent has viewed |
| `evidence_sent` | dict[str,str] | file -> MD5 hash for dedup (keyed as `edit:path` or `view:path`) |
| `evidence_cache` | dict[str,str] | file -> first line of evidence (for recall injection on re-edit) |
| `brief_candidates` | set[str] | Files ranked by L1 brief as likely fix targets |
| `interaction_log` | list[dict] | Full GT interaction log (written to disk per-entry) |

### Layer-Specific Fields
| Field | Type | Purpose |
|---|---|---|
| `_l3_fire_count` | int | L3 post-edit fires (budget: max 5) |
| `_l3b_fire_count` | int | L3b post-view fires (budget: max 3) |
| `_l5_scaffold_fired` | bool | Whether L5 scaffold redirect has fired |
| `_l5_governor` | Any | L5Governor object (trajectory-level interventions) |
| `_l5_metrics` | dict | Tracks L5 effectiveness (source edit after, candidate touched) |
| `_l5_edit_counts_per_file` | dict[str,int] | Per-file edit count (suppresses L3 after 3 edits to same file) |
| `_consensus_fired` | bool | Whether L2 consensus has fired (fires once) |
| `_consensus_scope` | list[str] | Scope files detected at consensus time |
| `_agent_state` | Any | FINAL_ARCH_V2 Layer 2 AgentState |
| `_edge_verifier` | Any | LSP edge verifier (optional, off by default) |
| `_host_graph_db` | str | Path to graph.db on host side (for proxy vs always transfer) |
| `_router_v2` | (dynamic) | CollaborationRouter for FINAL_ARCH_V2 |

### Behavioral Tracking (Rescue Governor)
| Field | Purpose |
|---|---|
| `_last_gt_action` | Action count when GT last injected evidence |
| `_source_edit_actions` | List of action counts where source files were edited |
| `_test_actions` | List of action counts where tests were run |
| `_read_history` | List of files read (ordered) |
| `_search_count_since_edit` | Grep/find/rg count since last source edit |
| `_rescue_fired_count` | How many rescue interventions have fired (max 3) |
| `_diff_ever_nonzero` | Whether git diff ever showed changes |
| `_diff_collapsed_count` | How many times diff collapsed to 0 after being nonzero |

---

## 2. patched_run_action (line 2770) - Core Interception Point

### Entry Point
`wrap_runtime_run_action(runtime, config)` (line 2753) captures `runtime.run_action` as `orig_run_action`, creates the closure `patched_run_action`, and replaces `runtime.run_action` with it.

### Full Flow

```
patched_run_action(action)
  |
  +--> Phase B check: if GT_PHASE=="b", pass through to orig_run_action directly
  |
  +--> Get action text, action class
  +--> Backfill agent_action_after on previous interaction_log entry
  +--> Record L4 if action is a gt_ tool command
  |
  +--> obs = orig_run_action(action)   <-- THE REAL EXECUTION
  |
  +--> _check_pending_next_actions(config, file, obs)   <-- may append L5b
  +--> classify_tool_event(action)     <-- returns HookEvent(kind, path)
  |
  +--> if CmdRunAction:
  |      action_count++
  |      Track searches, tests, max_iter, gt_validate
  |      L5 governor: after_interaction (advisory/suppressed)
  |      L5 goku_check (event-driven)
  |
  +--> _emit_agent_event(config, action, event, file)
  +--> Rescue governor check (every 5 actions, max 3 rescues, 10-action cooldown)
  |
  +--> BRANCH on event.kind:
       |
       +--> "post_view"  --> L3b path (line 2964)
       +--> "post_edit"  --> L3 path (line 3402)
       +--> "finish"     --> L5 final check + cleanup
       +--> "skip"       --> return obs unchanged
```

### How post_edit vs post_view Detection Works (classify_tool_event, line 667)

The detection is based on OpenHands action classification:

1. **FileReadAction / FileViewAction** -> `post_view` (if source file)
2. **FileEditAction / FileWriteAction** -> `post_edit` (if source file, not test)
3. **CmdRunAction** -> parsed for editor commands:
   - `str_replace_editor view` -> `post_view`
   - `str_replace_editor create/str_replace/insert/write` -> `post_edit`
   - `cat`, `head`, `tail`, `less` of source file -> `post_view`
4. Test files are skipped for edits (not views)
5. Non-source extensions are skipped

---

## 3. _deliver_or_trace (line 1131) - The Delivery Invariant

This is the SINGLE POINT through which evidence enters the agent observation (for L3/L3b). It enforces an invariant: evidence either reaches the agent OR gets an explicit trace explaining why it didn't.

### Contract
```
_deliver_or_trace(obs, payload, config, layer, file_path, prepend=False)
```

### Three outcomes:
1. **Empty payload** -> logs `ROUTER_EMIT_HOOK_EMPTY`, returns obs unchanged
2. **Payload lacks markers** (fails `has_gt_evidence(payload, layer)`) -> logs `ROUTER_EMIT_MARKER_MISMATCH` with first 300 chars, returns obs unchanged
3. **Payload has markers** -> calls `append_observation` or `prepend_observation`, logs `DELIVERED`, updates `_last_gt_action`

### What the agent sees vs host-only:
- **Agent sees:** The `obs.content` field is mutated with the payload text
- **Host-only:** All `[GT_TRACE]` and `[GT_META]` lines go to stdout (which is host-side stderr in the OpenHands architecture)
- **CRITICAL DETAIL:** `has_gt_evidence()` checks for specific marker strings like `[CONTRACT]`, `Called by:`, `[GT] `, etc. If a hook produces output that doesn't contain ANY of these markers, the evidence is SILENTLY DROPPED from the agent's perspective (only a trace line appears on host).

### Potential Bug: Marker Mismatch as Silent Drop
The `_deliver_or_trace` function treats marker-missing content as "not evidence" and drops it. But the markers are defined in `evidence_markers.py` and the hooks produce their own markers. If a hook is updated to use new marker text that isn't in the markers list, evidence will be assembled, the hook will run, output will be generated -- and then `_deliver_or_trace` will drop it silently. The trace log says MARKER_MISMATCH but the agent never sees it.

---

## 4. append_observation / prepend_observation (lines 2135-2167)

### append_observation
Simply concatenates `text` to `obs.content`. No truncation, no filtering. Logs `+N chars`.

### prepend_observation
Concatenates `text` BEFORE `obs.content` but **hard-caps at 600 chars** (~150 tokens). This is significant: L3b evidence and L4 auto-query use prepend, meaning they are capped at 600 chars regardless of evidence quality.

### Both functions:
- Handle None content gracefully
- Log via `[GT_DELIVERY]` prefix
- Return the modified obs
- Never raise (catch + log exceptions)

---

## 5. L3b Post-View Path (lines 2964-3400)

### Full chain:

```
event.kind == "post_view"
  |
  +--> Record viewed file in config.viewed_files
  |
  +--> L4 Auto-Query (first 2 source files only):
  |     Query graph.db for top symbols + callers
  |     Inject via _deliver_or_trace with prepend (600 char cap)
  |
  +--> L2 Consensus (fires once):
  |     If file is a brief candidate AND no source edit yet
  |     First time: detect scope, prepend scope message
  |     Subsequent: "also in scope" append
  |
  +--> Router V2 check (_router_v2_on_view)
  |
  +--> BASELINE GUARD: if _GT_BASELINE, return obs (no L3b)
  |
  +--> If router_v2 == "live":
  |     Budget gate: _l3b_fire_count >= 3 -> suppress
  |     Late iteration: action_count > 0.75 * max_iter -> suppress
  |     If router says don't emit -> return obs
  |     If router says emit:
  |       Write viewed files to container
  |       Run post_view hook in container
  |       Strip [GT_META] lines, __GT_STRUCTURED__ JSON
  |       Cap at 500 chars
  |       Format with [GT] prefix + next-file suggestion
  |       Deliver via _deliver_or_trace (prepend)
  |     Increment _l3b_fire_count
  |     Return obs
  |
  +--> Legacy path (router_v2 != "live"):
       Budget gate: _l3b_fire_count >= 3 -> suppress
       Late iteration: > 75% max_iter -> suppress
       Write viewed files to container
       Run post_view hook (make_view_hook_command)
       Check for hook fatal errors
       Check has_gt_evidence(hook_out, "l3b")
         No evidence -> return obs with [GT_OK] log
       |
       Dedup check: normalize body, MD5 hash, compare to evidence_sent["view:path"]
         Duplicate -> return obs with [dedup] log
       |
       CURATION GATE: Only inject if:
         (a) Agent has NOT made a durable source edit, OR
         (b) This file IS a brief candidate
         Otherwise -> suppress (structured event only, no agent injection)
       |
       Extract primary edge + next_action (if GT_L3B_PRIMARY_EDGE=1)
       Compact to 2 lines max, 130 chars each, within 300 chars
       Suppress stale next-file suggestions (already viewed)
       Cap at 500 chars
       |
       _deliver_or_trace(obs, evidence, config, "l3b", path)
```

### Suppression conditions (9 total):
1. `_GT_BASELINE` = True
2. Budget exhausted (`_l3b_fire_count >= 3`)
3. Late iteration (`action_count > 0.75 * max_iter`)
4. Router v2 live mode + router says don't emit
5. No evidence markers in hook output
6. Duplicate evidence (MD5 hash match)
7. Curation gate: agent has source edit AND file is not a brief candidate
8. Empty evidence after compaction
9. `_deliver_or_trace` marker mismatch (failsafe)

---

## 6. L3 Post-Edit Path (lines 3402-4099)

### Full chain:

```
event.kind == "post_edit"
  |
  +--> Phase 1: Record edit state
  |     Add to edited_files
  |     Track source edit actions
  |     _emit_belief_event
  |
  +--> Router V2 check (_router_v2_on_edit)
  +--> _record_edit_iter, _record_diff_snapshot
  +--> Track per-file edit counts
  +--> L5 governor: record source edit
  |
  +--> Phase 3: Scaffolding early-exit
  |     If _is_scaffolding_path: reindex only, skip L3, return obs
  |
  +--> Phase 4: L6 reindex (sequential, before L3)
  |     Run gt-index -file on the edited file
  |     Verify via mtime comparison
  |     Download/refresh graph.db to host (proxy or always mode)
  |
  +--> BASELINE GUARD: if _GT_BASELINE, return obs (no L3)
  |
  +--> If router_v2 == "live":
  |     If router says don't emit -> return obs
  |     If router says emit:
  |       Extract diff + old content from observation
  |       Write artifacts to container
  |       Run post_edit hook (make_edit_hook_command_with_artifacts)
  |       Strip [GT_META] lines
  |       Check evidence_markers tuple (inline, NOT from evidence_markers.py!)
  |       GATE_MISMATCH check (see section 7)
  |       Run semantic_check (guard added/removed detection)
  |       If has_evidence:
  |         Strip __GT_STRUCTURED__
  |         Cap at 2000 chars
  |         Recall injection (prepend cached evidence from prior L3b)
  |         Format with [GT] Post-edit prefix
  |         Multi-file scope check (callers in unedited files)
  |         Scope-aware progress tracking
  |         append_observation(obs, formatted)
  |       Return obs
  |
  +--> Legacy path (router_v2 != "live"):
       Budget gate: _l3_fire_count >= 5 -> suppress
       Same-file suppression: >= 3 edits to same file -> suppress
       Extract diff + old content
       Compute L3 mode (post_edit or post_failure from governor)
       Run post_edit hook (make_edit_hook_command_with_artifacts)
       |
       Track needs_check (pending_checks for L5 advisory)
       Semantic check: run semantic_check regardless
       |
       Check has_gt_evidence(hook_body_edit, "l3")
         No evidence -> return obs with [GT_OK] log
       |
       Dedup check: normalize, MD5, compare to evidence_sent["edit:path"]
         Duplicate -> return obs with [dedup] log
       |
       Structural next_action extraction (if GT_STRUCTURAL_NEXT_ACTION=1):
         Priority: caller_code > consumer/importer > signature > targeted_test > static_sanity
         With optional LSP verification (GT_LSP_VERIFY=1)
       |
       Compact: strip XML wrappers, status lines, meta lines
       Cap at 2000 chars
       Format with [GT] prefix + next-file suggestion
       |
       _deliver_or_trace(obs, evidence, config, "l3", path)
```

### Suppression conditions (8 total):
1. `_GT_BASELINE` = True
2. Scaffolding file (reindex only)
3. Budget exhausted (`_l3_fire_count >= 5`)
4. Same-file suppression (`>= 3` edits to same file)
5. Router v2 live mode + router says don't emit
6. No evidence markers in hook output
7. Duplicate evidence (MD5 hash match)
8. Empty evidence after compaction + `_deliver_or_trace` marker mismatch

---

## 7. GATE_MISMATCH

### What It Is
GATE_MISMATCH is a trace-level status that fires when a hook produces output but that output contains NONE of the expected evidence markers. It means: "the hook ran and returned text, but we don't recognize it as evidence."

### Where It Fires
Two locations in the live router-v2 L3 path (lines 3629-3665):

1. **Line 3654:** After the post_edit hook runs and the output is checked against the inline `_evidence_markers` tuple. If the hook body is non-empty but contains none of the markers -> `reason=GATE_MISMATCH`.
2. **Line 3704:** After the semantic_check runs and produces output that doesn't match the semantic check markers (GUARD_ADDED, GUARD_REMOVED) -> `reason=GATE_MISMATCH`.

### Key Detail: Dual Marker Lists (POTENTIAL BUG)
In the live router-v2 path (line 3629), evidence markers are defined as an **inline tuple**:
```python
_evidence_markers = (
    "[CONTRACT]", "[CONTRACT ~]", "[SIGNATURE]", "[PATTERN]", "[PEER]", "[TWINS]",
    "[PROPAGATE]", "[CO-CHANGE]", "[SCOPE]",
    "[BEHAVIORAL CONTRACT]", "[TEST]",
    "[GT_VERIFY]", "[GT L3:",
    "SIGNATURE:", "CALLERS:", "SIBLING:", "WARNING:",
    "TOP CALLER:", "MUST PRESERVE:", "TEST EXPECTS:", "TEST:",
)
```
But the legacy path uses `has_gt_evidence(hook_body_edit, "l3")` which reads from `evidence_markers.py` (L3_MARKERS).

**The two lists are NOT identical.** L3_MARKERS includes additional markers:
- `Called by:`, `Calls into:`, `Imported by:`, `Next:`, `[GT] `
- `[GT_AUTO]`, `[MISMATCH]`, `[FORMAT]`, `[GT_CONTRACT`
- `[GT_CHANGE]`, `[GT_PATTERN]`, `[GT_STRUCTURAL]`, `[GT_SEMANTIC]`, `[GT_COUPLING]`
- `[RECALL]`, `GUARD_ADDED:`, `GUARD_REMOVED:`

This means: **evidence that passes the shared `has_gt_evidence` check in the legacy path could fail the inline marker check in the live router path**, causing GATE_MISMATCH and silent suppression. The `GUARD_ADDED:` / `GUARD_REMOVED:` markers from semantic_check are NOT in the live path's inline tuple -- but those are prepended to `hook_body` before the check, so they'd need to also be in `_evidence_markers`. They are NOT. However, the semantic check output is prepended to hook_body and then has_evidence is re-evaluated, so if the semantic check adds `GUARD_ADDED:` to hook_body, the `has_evidence` boolean is set True via the `SEMANTIC WARNING:` rewrite. Actually reading more carefully: the semantic check rewrite produces `SEMANTIC WARNING:` prefix, not `GUARD_ADDED:` -- so the inline markers don't match those either. But `has_evidence` is set to True directly by the code, bypassing the marker check. So this specific path is not a bug, but the dual-list divergence is still a latent risk.

### What Causes GATE_MISMATCH
- Hook runs successfully but produces status/diagnostic output without actual evidence
- Hook produces evidence with markers that aren't in the expected set
- Hook produces freeform text analysis without structured markers

### Impact
The agent never sees the content. A `[GT_TRACE]` line with `visible=False surface=none` is printed to host stdout only.

---

## 8. Router V2 (_router_v2_on_view, _router_v2_on_edit)

### Architecture
The router is a `CollaborationRouter` object (from `groundtruth.router`) that wraps an `AgentState` and decides WHEN to emit evidence. It does NOT produce evidence itself -- it controls the gate.

### Three Modes (from GT_ROUTER_V2 env):
1. **off** (default): Router never instantiated. Legacy paths unchanged.
2. **shadow**: Router runs in parallel, decisions logged to telemetry, but NO effect on agent observations. Legacy paths run as normal. Used for A/B comparison.
3. **live**: Router is the SOLE L3/L3b evidence source. Legacy evidence path is skipped entirely. Router decides emit/suppress; if emit, the legacy hook runs in-container to produce the actual evidence text.

### What Makes Evidence "Eligible" vs "Emitted"

The router's `on_view` / `on_edit` methods return an `EmissionDecision` with:
- `emit: bool` -- whether to inject into agent context
- `suppression_reason` -- why it was suppressed (e.g., budget, debounce, band)
- `evidence_items` -- count of evidence items found
- `evidence_text` -- the text (may be empty even when emit=True in delegated mode)
- `band` -- iteration band (early/mid/late)
- `kind` -- the emission kind

Evidence is "eligible" when the router's internal providers find relevant graph data. Evidence is "emitted" only when it passes the router's gates:
- **Budget:** Shared with legacy (3 for L3b, 5 for L3)
- **Debounce:** Action count synced from config; router may suppress rapid-fire events
- **Band:** Iteration band affects emission threshold
- **delegate_evidence:** In live mode, router delegates evidence production to the in-container hook

### What the Router Returns
A dict with `emit`, `suppression_reason`, `evidence_items` count, `next_action_type`, `next_action_file`, `band`, etc. This dict is persisted to:
1. In-memory `config.interaction_log`
2. `/tmp/gt_interactions_<task>.jsonl`
3. Structured event via `_emit_structured_event`

### Shadow vs Live Behavioral Difference
In shadow mode, the router's dict is logged but the legacy path still runs and injects evidence. In live mode, the wrapper checks `_v2_event.get("emit")` and EITHER runs the legacy hook (if emit=True) OR returns obs unchanged (if emit=False). The legacy `generate_improved_evidence` path is completely skipped.

---

## 9. L1 Brief Injection Path (lines 4458-4976)

### Two-Phase Architecture

**Phase 1: `patched_initialize_runtime` (line 4458)**
Called during task setup. Does:
1. Probe workspace root in container
2. Create GTRuntimeConfig + GTTelemetry
3. Initialize L5 governor, edge verifier, telemetry writer
4. Upload gt-index binary + hooks to container
5. Build graph.db index in container
6. Generate brief:
   - Upload brief runner script to container
   - Upload issue text + metadata
   - Extract issue anchors
   - Run `generate_v1r_brief` in container
   - Parse output (brief text + L2 JSON separated by `---GT_L2_JSON---`)
   - Fallback: if 0 candidates + low signal, produce gt_search suggestion
   - Truncate to 500 tokens via `_brief_max_tokens`
   - Store as `instance["gt_brief"]`
7. Run L4 prefetch (graph-based constraints appended to brief)
8. Call `wrap_runtime_run_action(runtime, config)` to install the patched_run_action

**Phase 2: `patched_get_instruction` (line 4875)**
Called when OpenHands generates the first user message. Does:
1. If `_GT_BASELINE`, return original message unchanged
2. Generate tools_hint (always, if GT_NATIVE_TOOLS=1)
3. Retrieve brief from `instance.gt_brief`
4. Wrap brief in `<gt-task-brief>` tags
5. Optionally add `<gt-demo>` with gt_query example
6. PREPEND to the message content: `<gt-task-brief>...\n\n{tools_hint}\n{demo}\n{original_content}`
7. Log L1 structured event + belief events for each candidate

### What the Agent Actually Sees
The first message to the agent has this structure:
```
<gt-task-brief>
[brief text, max 500 tokens / ~2000 chars]
</gt-task-brief>

## Codebase Intelligence
[tool usage instructions]

<gt-demo>
[optional gt_query example]
</gt-demo>

[original OpenHands instruction message]
```

### Brief Truncation
`_brief_max_tokens` (line 432) caps at 500 tokens (~2000 chars). It prioritizes lines containing file paths over other lines.

---

## 10. Full Chain: Agent Edits File -> Agent Sees Evidence

Ordered function call chain for a post_edit event:

```
1.  Agent submits FileEditAction or str_replace_editor command
2.  patched_run_action(action)                          [line 2770]
3.    obs = orig_run_action(action)                     [line 2803] -- action executes
4.    _check_pending_next_actions(config, file, obs)    [line 2813] -- may append L5b
5.    classify_tool_event(action)                       [line 2814] -- returns HookEvent("post_edit", path)
6.    config.action_count += 1                          [line 2818]
7.    _emit_agent_event(config, action, event, file)    [line 2916]
8.    config.edited_files.add(rel_p)                    [line 3406]
9.    _router_v2_on_edit(config, path, [])              [line 3418]
10.   _record_edit_iter, _record_diff_snapshot           [line 3419-3420]
11.   _is_scaffolding_path check                         [line 3450] -> early exit if scaffold
12.   make_reindex_command + _run_internal (L6)          [line 3478-3494] -- gt-index -file
13.   mtime verification                                 [line 3505-3512]
14.   graph.db refresh to host                           [line 3546-3584]
15.   _GT_BASELINE check                                 [line 3588] -> return if baseline
16.   Budget gate: _l3_fire_count >= 5                   [line 3803] -> return if exhausted
17.   Same-file gate: edits >= 3                         [line 3813] -> return if suppressed
18.   _extract_diff_and_old_content(obs)                 [line 3822]
19.   _write_text_to_container (diff + old content)      [line 3827-3830]
20.   make_edit_hook_command_with_artifacts               [line 3839-3850]
21.   _run_internal(orig_run_action, hook_cmd, 45)       [line 3839] -- hook executes in container
22.   semantic_check run                                  [line 3908-3920]
23.   has_gt_evidence(hook_body_edit, "l3")               [line 3922] -> return if no evidence
24.   Dedup: MD5 hash compare                            [line 3927-3936] -> return if duplicate
25.   Structural next_action extraction                   [line 3942-3998]
26.   Compact: strip XML/status/meta lines               [line 4019-4030]
27.   Cap at 2000 chars                                  [line 4030]
28.   _deliver_or_trace(obs, evidence, config, "l3")     [line 4099]
29.     has_gt_evidence(evidence, "l3")                   [line 1156] -- marker check
30.     append_observation(obs, evidence)                  [line 1169] -- obs.content mutated
31.   return obs                                         [line 4099] -- agent sees modified obs
```

**Total function call depth: 31 steps from action to observation mutation.**

---

## 11. Evidence That Gets Assembled But Silently Dropped

Yes, there are multiple points where evidence is assembled but never reaches the agent:

### 1. GATE_MISMATCH in Live Router Path (line 3650-3665)
Hook runs, produces text, but text lacks recognized markers. Logged as `visible=False surface=none`. Agent never sees it.

### 2. Dedup Hash Match (lines 3268, 3930)
Evidence is generated but its normalized MD5 matches the previous evidence sent for that file. Common when the agent re-edits the same file without substantive changes.

### 3. Curation Gate for L3b (line 3289-3302)
L3b hook runs and produces evidence, but:
- Agent has already made a source edit, AND
- The viewed file is NOT a brief candidate
Result: evidence logged to structured events but NOT injected.

### 4. Empty Evidence After Compaction (L3, line 4038)
The hook produces output that passes `has_gt_evidence`, but after stripping XML wrappers, status lines, meta lines, and `__GT_STRUCTURED__` JSON, the remaining `directive_lines` may be empty. The evidence is assembled, formatted, but `evidence.strip()` is empty so `_deliver_or_trace` gets empty payload -> ROUTER_EMIT_HOOK_EMPTY.

### 5. prepend_observation 600-char Cap (line 2157)
For L3b and L4 auto-query, evidence is prepended with a hard 600-char cap. If the hook produces 800 chars of evidence, only the first 600 reach the agent. The truncation is silent -- no log that truncation occurred.

### 6. L3b 500-char Cap (line 3394)
L3b evidence is capped at 500 chars before delivery. Combined with the prepend 600-char cap, the effective limit is 500 chars.

### 7. L3 2000-char Cap (line 4030 legacy, line 3731 live)
L3 evidence is capped at 2000 chars. Truncation appends "..." but is otherwise silent.

### 8. _brief_max_tokens 500-token Cap (line 432)
The L1 brief is truncated to ~2000 chars, prioritizing lines with file paths. Lower-priority lines (explanatory text, caveats) are dropped.

### 9. Stale Next-File Suppression (line 3367-3369)
If the L3b evidence includes a "Next: read X" suggestion but the agent already viewed X, the suggestion is stripped. The evidence text may become substantially less useful without the navigation cue.

### 10. __GT_STRUCTURED__ Stripping
Throughout both L3 and L3b paths, the `__GT_STRUCTURED__` JSON blob is stripped from agent-visible text. This JSON contains structured evidence items (caller edges, confidence scores, verification status) that the JSONL telemetry sees but the agent never does. This is by design (agent sees compact natural language, telemetry sees structured data).

---

## 12. Summary of Potential Bugs and Gaps

### BUG-1: Dual Marker Lists (HIGH SEVERITY)
The live router-v2 L3 path (line 3629) uses an inline `_evidence_markers` tuple that is a SUBSET of `L3_MARKERS` from `evidence_markers.py`. The legacy path uses the shared module. This means evidence that would be delivered in the legacy path may be GATE_MISMATCH-suppressed in the live path. Specifically missing from the inline tuple: `Called by:`, `Calls into:`, `Imported by:`, `[GT] `, `[GT_AUTO]`, `[RECALL]`, `[GT_CONTRACT` (note: `[GT_CONTRACT]` IS present but `[GT_CONTRACT` without closing bracket is not).

**Fix:** Replace the inline tuple with `from groundtruth.config.evidence_markers import L3_MARKERS`.

### BUG-2: prepend_observation Truncation is Silent (MEDIUM)
When L3b or L4 auto-query evidence exceeds 600 chars, it is silently truncated mid-sentence. No truncation marker like `[GT_BRIEF_TRUNCATED]` is appended. The agent may see a partial sentence with no indication that more context was available.

### BUG-3: L3 Live Path and Legacy Path Have Different Delivery Mechanisms (MEDIUM)
In the live router path, evidence is delivered via `append_observation(obs, formatted)` directly (line 3801). In the legacy path, evidence goes through `_deliver_or_trace` (line 4099). The live path bypasses the `_deliver_or_trace` contract (marker check, empty check, delivery trace logging). If `_formatted_pe` somehow lacks markers, the live path will still inject it, while the legacy path would catch and suppress it.

### GAP-1: No Holistic Budget Across Layers
L3 has a budget of 5, L3b has 3, L5 has 3 rescues, L4 auto-query has 2. These are independent. An agent could receive 5+3+3+2 = 13 GT injections, potentially flooding the context. There is no cross-layer token budget.

### GAP-2: Rescue Governor Has No Knowledge of L3/L3b Evidence Quality
The rescue governor fires based on behavioral signals (action patterns, search count, edit count) but doesn't know whether L3/L3b actually delivered useful evidence. It may fire a rescue advisory when L3 already gave the agent exactly what it needed, or stay silent when L3 was suppressed and the agent actually needs help.

### GAP-3: evidence_cache for Recall Injection is Per-File, Single-Line
`evidence_cache[file]` stores only the first line of the last L3b evidence for that file (line 3209). On re-edit of the same file, this single line is prepended as `[RECALL]`. But if the original evidence had multiple important constraints, only the first is recalled. The recall is also not dedup-aware -- the same recall line may appear alongside identical new evidence.
