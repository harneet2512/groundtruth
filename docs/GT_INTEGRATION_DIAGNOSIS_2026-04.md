# GT Integration Diagnosis — April 2026

## Problem
GT deliveries = 0 during LSP-hybrid canary. GT indexing runs, telemetry events exist, tasks produce patches, but no GT evidence appears in trajectory observations.

## Root Causes Found (from code inspection)

### RC1: Score gating suppressed ALL micro-updates (CRITICAL)
**File:** `swe_agent_state_gt.py:56`

`TIER_SILENT = 0.4` requires `caller_count * 0.4 + asserts * 0.3 + return_shape * 0.3 >= 0.4`.

The Python regex indexer (`gt_tool_install.sh:94-116`) only built cross-file edges by scanning 50 lines per function body for bare name matches. Most repos got **zero or near-zero cross-file edges**, so `caller_count = 0` → `score = 0.0` → all micro-updates suppressed.

Additionally, the query at `swe_agent_state_gt.py:321-326` excluded same-file callers (`source_file != ?`), throwing away the only edges the indexer reliably produced.

**Fix applied:**
- Lowered `TIER_SILENT` from 0.4 to 0.1
- Changed caller query to count ALL callers (including same-file)
- Improved indexer to build import-verified + same-file edges (not just cross-file name-match)

### RC2: Python indexer built weak/zero edges (CRITICAL)
**File:** `gt_tool_install.sh:94-116`

Old indexer:
- Only scanned 50 lines per function body
- Only matched cross-file calls (same-file excluded from edges)
- No import resolution
- No assertion extraction

**Fix applied:**
- Added import map parsing (`from X import Y` → verified edges at conf=1.0)
- Added same-file edges (conf=1.0)
- Extended body scan from 50 to 200 lines
- Added assertion extraction from test functions
- Edge deduplication
- Diagnostic summary with edge type breakdown

### RC3: Startup briefing silently returned empty (HIGH)
**File:** `swe_agent_state_gt.py:495-542`

If `generate_pre_edit_briefing()` failed (no issue text, no candidates, import error), it returned empty string. The checkpoint file was touched (line 498), so no retry. First delivery was empty, and post-edit delivery depended on material edits.

**Fix applied:**
- Added `_fallback_orient()` that queries top-5 most-called symbols
- All failure paths now fall back to orient instead of returning empty
- Ensures first delivery is never empty

### RC4: Verification required 3 edits before first trigger (MEDIUM)
**File:** `swe_agent_state_gt.py:54`

`VERIFY_EVERY_N_EDITS = 3` meant tasks with <3 material edits got zero verification. Combined with score gating on Channel A, many tasks got zero evidence from both channels.

**Fix applied:**
- Changed `VERIFY_EVERY_N_EDITS` from 3 to 1

### RC5: Silent failure logging (MEDIUM)
**File:** `gt_tool_install.sh`

All failures were silently caught:
- `_state_anthropic` patching skipped with no log if path didn't exist
- Python indexer exceptions caught with `pass`
- No diagnostic output about edge/node counts

**Fix applied:**
- All steps now log to `/tmp/gt_install.log`
- Patched `_state_anthropic` logs errors to `/tmp/gt_state_cmd.log`
- Indexer prints edge type breakdown
- Warnings for zero nodes/edges

### RC6: `_state_anthropic` path may not exist (NEEDS VM VERIFICATION)
**File:** `gt_tool_install.sh:15-16`

The hardcoded path `/root/tools/edit_anthropic/bin/_state_anthropic` may not exist in newer SWE-agent containers. If not found, the entire GT hook is silently disabled.

**Fix applied:**
- Added fallback path discovery (`/root/tools/*/bin/_state_*`)
- Logs all found state commands for debugging

### RC0 (NEW — PRIMARY ROOT CAUSE): Bundle `config.yaml` missing `install:` directive
**File:** `/tmp/SWE-agent/tools/groundtruth/config.yaml`

The config was just `tools: {}` — no `install:` directive. SWE-agent never ran `gt_tool_install.sh`, so:
- `_state_anthropic` was never patched (the GT hook never fired after startup)
- The Python indexer ran via some other path but `_state_anthropic` was the original SWE-agent version
- Post-edit evidence was structurally impossible

**Fix applied:**
- Created `config.yaml` with `install: "bash install.sh"` on both VMs

## VM Verification Results (2026-04-15)

| Check | Status | Result |
|-------|--------|--------|
| Runner script | CONFIRMED | Path A: `python3 -m sweagent run-batch` with `canary_gt_ds.yaml` |
| graph.db exists | CONFIRMED | 63MB, 17645 nodes |
| graph.db edges | CONFIRMED | 1,903,974 (188K same_file + 69K import + 1.6M name_match) |
| _state_anthropic patched | CONFIRMED | Contains GT subprocess call after fix |
| Install runs | CONFIRMED | `/tmp/gt_install.log` shows all steps successful |
| Telemetry fires | CONFIRMED | 15 events for 13 steps (hook fires every step) |
| gt_evidence in model input | CONFIRMED | `'gt_evidence': '[GT] Low confidence...'` in run.log |
| Post-edit micro-update | PENDING | Agent hasn't edited yet (DeepSeek V3.2 slow to edit) |
| MCP tools callable | NOT TESTED | Tools are bash commands, not MCP — requires agent to invoke |
| A/B parity | CONFIRMED | Both VMs have identical config (gt-nolsp has no GT_LSP_ENABLED) |

## Validation Gates

1. `nodes > 100 AND edges > 10` in test container
2. Telemetry shows `micro_emitted` or `verify_emitted`
3. At least one traj observation contains GT evidence
4. `python3 /tmp/gt_intel.py orient` returns output
5. A/B config checksums match (except LSP)

## Files Modified

| File | Changes |
|------|---------|
| `benchmarks/swebench/gt_tool_install.sh` | Improved indexer (import+same-file edges, assertions), diagnostic logging |
| `benchmarks/swebench/swe_agent_state_gt.py` | Lowered TIER_SILENT (0.4→0.1), VERIFY_EVERY_N_EDITS (3→1), fallback orient, count all callers, cycle_end logging |
