# Target Architecture — Per-Layer Behavior Specification

**Date:** 2026-05-15
**Status:** Target state. Not yet implemented.
**Dependencies:** 03_decision32_summary.md (design rules), 04_easy_wins_matrix.md (implementation items)

---

## Iteration Band Definitions

All layers reference the same iteration bands:

| Band | Iteration Ratio | Agent Phase |
|------|----------------|-------------|
| EARLY | 0% - 30% | Exploration: reading files, understanding the codebase |
| MID | 30% - 60% | Commitment: making edits, testing hypotheses |
| LATE | 60% - 85% | Repair: fixing failures, refining edits |
| FINAL | 85% - 100% | Finalization: cleanup, final tests, finish |

---

## L1 — Pre-Task Localization Brief

### Target Behavior

L1 fires once before iteration 1. It injects a `<gt-task-brief>` block containing:

1. **Candidate files** (max 5) ranked by hybrid retrieval (BM25 + graph reach + anchor proximity + hub penalty).
2. **Per-candidate structural witness** (NEW): for the top function in each candidate file, render the single highest-reference-count caller's call-site code line. Format: `Witness: src/runner.py:145 → rule.validate(template)`. This is the primary contract evidence — the caller code line tells the agent what interface contract the function must satisfy.
3. **Test mapping**: `Tests: tests/test_foo.py` when test edges exist. Omitted (not "empty") when no test edges.
4. **Graph calls**: `Calls: src/other.py` — existing behavior, unchanged.
5. **Graph coverage tag** (NEW): when graph.db has <2 edges/file for any top-3 candidate, append `[SPARSE GRAPH — structural evidence limited]` after the candidate list.

### Render Cap

**400 tokens hard cap** (existing `MAX_BRIEF_TOKENS = 400` in v1r_brief.py).

Truncation strategy (existing, unchanged): drop last candidate file until under budget. The structural witness line for each candidate adds ~15-25 tokens; budget is tight but feasible for 3-4 candidates with witnesses.

Remove the wrapper's secondary 500-token cap (`_brief_max_tokens()`) — it is unreachable and dead code.

### Decay Rules

None. L1 fires once. No iteration-aware decay.

### Measurement Requirements

| Metric | Source | Gate |
|--------|--------|------|
| `brief_generated` | GTTelemetry `record_brief(ok, l2)` | Must be True for every task (False = abstention) |
| `token_estimate` | V1RBriefResult | Must be > 0 and <= 400 |
| `candidate_count` | Structured JSON `/tmp/gt_l1_structured.json` | Must be >= 1 |
| `witness_present` | NEW: count of candidates with a `Witness:` line | Report-only, no gate (sparse graphs will have 0) |
| `graph_coverage_warning` | NEW: boolean, True when `[SPARSE GRAPH]` tag emitted | Report-only |
| `agent_opened_candidate_in_first_3` | Post-hoc trajectory join: did agent open a brief candidate file within first 3 actions | Report-only (causal attribution not possible) |

### What L1 Does NOT Do

- Does not set `next_action_type`. L1 is inject-once, passive.
- Does not constrain agent edits. No "editing elsewhere requires justification" prose (V1R-map design: map-only).
- Does not fire again after iteration 1.

---

## L3 — Post-Edit Contract Evidence

### Target Behavior

L3 fires after every agent source file edit. After L6 reindex completes, L3 queries graph.db for the edited function(s) and injects:

1. **Callers** (priority 1): cross-file callers with confidence >= 0.5, showing the actual call-site code line. Max 3 callers (decays to 2 after 3 edits, to 1 after 6 edits).
2. **Signature**: function signature + return type when available.
3. **Siblings**: same-class method patterns when caller evidence is thin (<2 callers).
4. **Test assertions**: specific assertion values from the `assertions` table when available.

**NEW: Always populate `next_action_type`** using the Decision 32 hierarchy:

```
if callers exist and agent has not viewed any caller file:
    next_action_type = "READ_CALLER_CONTRACT"
    next_action_file = <highest-reference-count caller file>
elif edited function has downstream consumers (callees that depend on return type):
    next_action_type = "READ_CONSUMER"
    next_action_file = <first downstream consumer file>
elif signature changed (detected by comparing pre-edit vs post-edit signature in graph.db):
    next_action_type = "CHECK_SIGNATURE"
    next_action_file = <edited file>
elif static analysis tool available (linter, type checker in PATH):
    next_action_type = "RUN_STATIC_SANITY"
    next_action_test = <linter command>
elif test file exists for edited module (from _get_test_assertions_from_graph):
    next_action_type = "RUN_TARGETED_TEST"
    next_action_test = <test file path>
else:
    next_action_type = "NONE"
```

### Render Cap

**<=300 tokens** (existing `_MAX_EVIDENCE_CHARS = 1200` ~ 300 tokens).

Late-repair cap: 150 tokens (existing `_LATE_REPAIR_MAX_CHARS = 600`) when iteration_ratio >= 0.60.

### Decay Rules

| Edits So Far | Max Callers Rendered | Max Total Chars |
|-------------|---------------------|-----------------|
| 1-3 | 3 | 1200 |
| 4-6 | 2 | 1000 |
| 7+ | 1 | 600 |
| iteration >= 0.60 | 1 | 600 (late-repair) |

Dedup: existing MD5-hash dedup continues. If evidence body matches a previous injection for the same file, emit `<gt-evidence dedup="true" />` (zero tokens).

### Measurement Requirements

| Metric | Source | Gate |
|--------|--------|------|
| `l3_fired` | GTTelemetry `record_hook("L3", ok, empty)` | Count per task |
| `next_action_type` | GTLayerEvent field | Must be populated on every L3 fire (NEW gate) |
| `next_action_hierarchy_level` | NEW: which level of the hierarchy was selected | Report distribution: READ_CALLER_CONTRACT / READ_CONSUMER / CHECK_SIGNATURE / RUN_STATIC_SANITY / RUN_TARGETED_TEST / NONE |
| `reaction_follow_type` | GTAgentReactionEvent from reaction joiner | FOLLOWED_EXACT / FOLLOWED_PARTIAL / IGNORED / CONTRADICTED / NOT_MEASURABLE |
| `evidence_tokens` | `output_lines` * ~4 | Must be <= 300 (hard gate) |
| `has_real_evidence` | `_compute_has_real_evidence()` | True when L3 emits callers/signature/siblings (not GT_OK) |

---

## L3b — Post-View Navigation Context

### Target Behavior

L3b fires after every agent source file view (excluding test files). After querying graph.db for the viewed file's callers, callees, and importers, L3b:

1. **Selects one primary edge per type** (callers, callees, importers) by task-relevance score (issue keyword overlap in file content, weighted by graph confidence). All other edges go to structured telemetry only.
2. **Suppresses hub edges**: edges from/to files with in-degree > p90 of the graph are demoted to structured-only, unless they are the only edge of their type.
3. **Renders the primary edges** with file path, function names, and call count.
4. **Populates `next_action_type`** (NEW): when the primary caller or callee is a file the agent has not yet viewed, set `next_action_type = "READ_CALLER_CONTRACT"` or `"READ_CONSUMER"`. When no actionable navigation exists, set `next_action_type = "NONE"`.

### Render Cap

Hard token caps with iteration decay:

| Band | Max Rendered Tokens | Max Rendered Chars | Edges Per Type |
|------|--------------------|--------------------|----------------|
| EARLY (0-30%) | 250 | 1000 | 1 primary + 0 alternatives rendered |
| MID (30-60%) | 160 | 640 | 1 primary only |
| LATE (60-85%) | 80 | 320 | 1 primary only, importers suppressed |
| FINAL (85-100%) | 0 (silent) | 0 | None rendered, structured-only |

Enforcement: after rendering, count tokens via `len(text) // 4 + 1`. If over cap, truncate from the bottom (drop importer line first, then callee line, then truncate caller line).

### Decay Rules

1. **Iteration-band decay**: token cap drops per band (table above).
2. **Visited-file suppression**: edges pointing to files the agent has already viewed are omitted from rendering (existing behavior via `/tmp/gt_viewed.txt`).
3. **Brief candidate annotation**: edges pointing to L1 brief candidates get `[CANDIDATE]` tag (existing behavior, unchanged).
4. **FINAL band silence**: at 85%+ iteration ratio, L3b emits structured telemetry only, zero rendered tokens. The agent is in finalization; navigation context is noise.

### Measurement Requirements

| Metric | Source | Gate |
|--------|--------|------|
| `l3b_fired` | GTTelemetry `record_hook("L3b", ok, empty)` | Count per task |
| `rendered_tokens` | `len(rendered_text) // 4 + 1` | Must be <= band cap (hard gate, NEW) |
| `primary_edges_rendered` | Count of edges in rendered output | Max 3 (one per type) |
| `hub_edges_suppressed` | Count of edges demoted to structured-only due to hub penalty | Report-only |
| `next_action_type` | GTLayerEvent field | Must be populated on every L3b fire (NEW gate) |
| `reaction_follow_type` | GTAgentReactionEvent | Report distribution |
| `total_l3b_tokens_per_task` | Sum of rendered_tokens across all L3b fires | Report-only. Target: <800 total (vs current ~3600) |

---

## L4 — Pre-Task Prefetch

### Target Behavior

L4 fires once after L1 brief generation. It pre-fetches `gt_query` evidence for issue-seeded symbols and appends to the brief injection. Target output:

```
<gt-prefetch symbols="validate,match" queries="2">
validate: returns list[Match], 3 callers, highest-risk: runner.py:145
match: 1 caller, returns Optional[str]
Last commit: abc1234 Fix SubNotJoin validation (src/.../SubNotJoin.py)
</gt-prefetch>
```

**NEW: Compact risk frame** — Each symbol gets one line: name, return type, caller count, highest-risk caller (file:line). No verbose `[VERIFIED]`/`[POSSIBLE]` prose. Git precedent stays as a single line.

### Render Cap

**80-120 tokens** (wire the existing `L4_TOKEN_CAP = 120` in constants.py to actual rendering).

Implementation: replace `L4_PREFETCH_MAX_CHARS = 1200` with `L4_PREFETCH_MAX_CHARS = 480` (120 tokens * 4 chars/token). Remove noise patterns filter — the compact format eliminates the need for post-hoc noise filtering.

Max queries: 3 (unchanged).
Wall timeout: 30 seconds (unchanged).

### Decay Rules

None. L4 fires once. No iteration-aware decay.

### Measurement Requirements

| Metric | Source | Gate |
|--------|--------|------|
| `l4_prefetch_fired` | GTTelemetry `record_l4_prefetch(queries, lines)` | Report-only (empty prefetch is valid) |
| `prefetch_tokens` | Character count / 4 | Must be <= 120 (hard gate, NEW) |
| `symbols_queried` | `queries_run` from stdout | Report count |
| `l4_tool_usage_count` | `tel.record_l4()` from CmdRunAction | Report-only (currently 0, not gated) |

---

## L5 — Trajectory Governor (Detection)

### Target Behavior

L5 monitors the agent's action stream and fires detection events for anti-patterns. Existing hooks remain. Four new detection hooks added:

#### New Hook: `ignored_next_action`

- **Trigger:** GT emitted a GTLayerEvent with `next_action_type != "NONE"` and the agent's subsequent 3 actions do not match the suggestion (classification by reaction joiner's `compute_follow_type()`).
- **Dependency:** Requires L3-1 and L3b-4 to populate `next_action_type` first.
- **Debounce:** Once per unique `next_action_type + next_action_file` combination. If GT suggests the same file twice and the agent ignores both, only one detection fires.
- **L5b message:** Repeat the original suggestion with specific file and symbol. "GT previously suggested reading callers of `validate` in src/runner.py:145. This has not been done."

#### New Hook: `structural_unverified_patch`

- **Trigger:** Iteration ratio >= 0.60, agent has >= 1 source edit, agent has never opened a file that is a caller of any edited function (no `post_view` event on a caller file, no `gt_query` targeting a caller).
- **Guard:** Only fires if the edited file has >= 1 caller edge in graph.db (sparse-graph safe).
- **Debounce:** Fires once per task.
- **L5b message:** "You have edited `validate` but never inspected its callers. Top caller: src/runner.py:145 → `rule.validate(template)`. Next action: read src/runner.py to verify your change preserves the call contract."

#### New Hook: `collapsed_diff`

- **Trigger:** Iteration ratio >= 0.50, agent's cumulative diff has fewer than 5 meaningful lines.
- **Meaningful line:** A diff line that is not: whitespace-only, comment-only (language-agnostic: lines starting with `#`, `//`, `/*`, `*`, `--`, `%`, `;;` after leading whitespace), import-only (heuristic: line starts with `import `, `from ... import`, `require(`, `use `, `#include`).
- **Debounce:** Fires once per task.
- **L5b message:** "At iteration N/M, your diff has K meaningful lines. The patch may be empty or trivially small. Review your edits."

#### New Hook: `broad_only_verification`

- **Trigger:** Iteration ratio >= 0.70, agent has run >= 1 verification command classified as "broad" (full test suite), zero commands classified as "targeted" (single file/function test).
- **Debounce:** Fires once per task.
- **L5b message:** "All verification has been broad (full test suite). No targeted test has been run for the edited files. Next action: run targeted test for [specific edited module]."

### Render Cap

L5 itself produces 0 tokens. All rendering is in L5b.

### Decay Rules

- Hooks fire more aggressively in later bands (lower thresholds for structural_unverified_patch at 0.60 vs earlier hooks at 0.30+).
- All hooks respect debounce to prevent intervention fatigue.

### Measurement Requirements

| Metric | Source | Gate |
|--------|--------|------|
| `l5_hooks_fired` | L5 telemetry JSONL | Count per task, by hook type |
| `l5_hooks_suppressed` | L5 telemetry `suppressed_reason` | Count per task |
| `collapsed_diff_detected` | NEW: boolean per task | Report-only. In smoke-1 this SHOULD have been True. |
| `structural_unverified_detected` | NEW: boolean per task | Report-only |
| `ignored_next_action_count` | NEW: count per task | Report-only. Depends on reaction joiner. |

---

## L5b — Trajectory Interventions

### Target Behavior

L5b renders intervention messages when L5 fires a detection. All messages must:

1. **Name one specific file or symbol** the agent should act on next.
2. **Specify the action type** from the Decision 32 hierarchy (READ_CALLER_CONTRACT, READ_CONSUMER, CHECK_SIGNATURE, RUN_STATIC_SANITY, RUN_TARGETED_TEST).
3. **Not contain restart/start-over language** (existing L5bSafetyChecker).
4. **Not suggest broad exploration** after iteration 50%.
5. **Always populate `next_action_type` and `next_action_file`/`next_action_test`** in the GTLayerEvent.

### Render Cap

**180 tokens** (existing `_MAX_L5_TOKENS = 180`, enforced by L5bSafetyChecker).

Typical message should be 40-65 tokens (150-250 chars). The 180-token cap is a safety ceiling, not a target.

### Decay Rules

- L5b messages include `_iteration_prefix()` at ratio >= 0.60 (~25 chars).
- L5b messages include `_late_repair_suffix()` in LATE/FINAL bands (~55 chars).
- After 3 L5b messages in a single task, subsequent messages are shortened to 100-token max (NEW: intervention fatigue prevention). The agent has received enough guidance; additional messages must be ultra-concise.

### Measurement Requirements

| Metric | Source | Gate |
|--------|--------|------|
| `l5b_interventions` | GTLayerEvent with `layer="L5b"` | Count per task |
| `l5b_blocked` | Events with `event_type="blocked_by_safety"` | Count per task (target: 0 in normal operation) |
| `next_action_type` | GTLayerEvent field | Must be populated on every L5b fire (existing gate) |
| `reaction_follow_type` | GTAgentReactionEvent | FOLLOWED_EXACT / IGNORED / CONTRADICTED / NOT_MEASURABLE |
| `intervention_tokens` | `len(text) // 4` | Must be <= 180 (hard gate, existing) |
| `intervention_fatigue_cap_applied` | NEW: boolean per event | Report-only. True when 3+ prior L5b messages triggered 100-token cap. |

---

## L6 — Incremental Reindex

### Target Behavior

L6 fires after every agent source file edit, before L3. It runs `gt-index -file=<path>` to update graph.db.

**NEW telemetry after reindex:**
1. Query `SELECT COUNT(*) FROM edges WHERE source_id IN (SELECT id FROM nodes WHERE file_path LIKE '%<edited_file>') OR target_id IN (SELECT id FROM nodes WHERE file_path LIKE '%<edited_file>')` BEFORE and AFTER reindex.
2. Emit `edges_before`, `edges_after`, `edges_changed = edges_after - edges_before` as structured telemetry.
3. Query `SELECT n.name, COUNT(e.id) AS caller_count FROM nodes n LEFT JOIN edges e ON e.target_id = n.id WHERE n.file_path LIKE '%<edited_file>' AND n.label IN ('Function','Method') GROUP BY n.id` AFTER reindex. Emit as `caller_counts_after`.
4. Call `GraphStore._build_usage_cache()` after reindex to refresh stale cache.

### Render Cap

0 tokens. L6 is invisible to the agent.

### Decay Rules

None. L6 fires on every edit regardless of iteration band. Graph freshness matters throughout.

### Measurement Requirements

| Metric | Source | Gate |
|--------|--------|------|
| `reindex_ok` | `r_ok` (exit_code == 0 AND mtime delta > 0) | Must be True (existing) |
| `reindex_latency_ms` | Wall time | Report-only. Alarm if > 10s. |
| `edges_changed` | NEW: delta from pre/post edge count | Report-only |
| `caller_counts_after` | NEW: per-function caller counts | Report-only |
| `usage_cache_rebuilt` | NEW: boolean | Must be True after every successful reindex |

---

## Hygiene — Scaffold File Strip

### Target Behavior

Hygiene fires at task finish and max_iter timeout.

**NEW pre-strip checks:**
1. **Patch collapse detection:** Before stripping, run `git diff --stat` in the container. If diff has 0 files changed or fewer than 5 non-whitespace lines, log `behavior_class=collapsed` and `patch_collapsed=True` in structured telemetry. Still proceed with strip (harmless on empty diff).
2. **Source edit preservation check:** After stripping, run `git diff --stat` again. Compare file list before and after strip. If any file that was in the pre-strip diff is missing from the post-strip diff, log `source_edit_lost=True` with the specific file path.

### Render Cap

0 tokens to agent. Hygiene output goes to container stdout and structured telemetry only.

### Decay Rules

None. Hygiene fires once at finish.

### Measurement Requirements

| Metric | Source | Gate |
|--------|--------|------|
| `scaffold_files_stripped` | Evidence items count | Report count |
| `non_scaffold_files_kept` | Evidence items with `reason="kept"` | Report count |
| `patch_collapsed` | NEW: boolean | Report-only. True = agent produced empty/trivial diff. |
| `source_edit_lost` | NEW: boolean | ALARM if True. Means scaffold strip deleted a real edit. |

---

## Meta/Reaction — Interaction Log + Reaction Joiner

### Target Behavior

Meta records every GT-to-agent interaction. Post-run, the reaction joiner classifies agent responses.

**NEW: Structural action follow classification.** Extend `compute_follow_type()` to return a richer enum:

```
FOLLOWED_READ_CALLER     — agent opened or read a caller file within N iterations
FOLLOWED_READ_CONSUMER   — agent opened or read a downstream consumer file
FOLLOWED_CHECK_SIGNATURE — agent read the edited file's function header/signature
FOLLOWED_RUN_STATIC      — agent ran a linter/type-checker
FOLLOWED_RUN_TEST        — agent ran a targeted test
IGNORED                  — agent took >= 3 actions, none matched any hierarchy level
CONTRADICTED             — agent did the opposite (e.g., GT said "read callers," agent deleted the function)
NOT_MEASURABLE           — agent finished immediately, or classification is ambiguous
```

The old binary `followed_within_1/3/5` fields remain for backward compatibility. The new `follow_hierarchy_type` field provides the structural classification.

**NEW: NOT_MEASURABLE emission.** When the agent's next action after a GT event is `FinishAction` or the task times out, emit `follow_type=NOT_MEASURABLE` instead of silently dropping.

### Render Cap

0 tokens to agent. Pure observability.

### Decay Rules

None. Meta records everything regardless of iteration band.

### Measurement Requirements

| Metric | Source | Gate |
|--------|--------|------|
| `total_gt_events` | GTLayerEvent count | Report per task |
| `events_with_next_action` | Count where `next_action_type` is populated | NEW gate: must be > 0 for tasks where L3 or L3b fired |
| `reactions_produced` | GTAgentReactionEvent count | NEW gate: must be > 0 when `events_with_next_action` > 0 |
| `follow_hierarchy_distribution` | NEW: count by follow_hierarchy_type | Report distribution |
| `ignored_rate` | IGNORED / total reactions | Report-only. High ignored rate = GT suggestions are wrong or poorly formatted. |
| `not_measurable_rate` | NOT_MEASURABLE / total reactions | Report-only. High rate = tasks ending too fast for measurement. |

---

## Relationship Indexer (GraphStore)

### Target Behavior

GraphStore remains a read-only bridge to graph.db. Changes are in what data is available and how it is maintained:

1. **Usage cache rebuild after L6 reindex** (NEW): `_build_usage_cache()` is called after every successful L6 reindex. This keeps `_top_functions()` and `get_hotspots()` ordering correct.
2. **Inheritance edge verification** (NEW): confirm that `INHERITS` edges are actually extracted by the Go indexer for the test repos (Python, Go, JS, TS, Java, Rust). If missing, file a Go indexer issue. GraphStore already has `_EDGE_TYPE_TO_REF` mapping for INHERITS.
3. **Decorator/route/config/event edges** (DEFERRED): these require Go indexer tree-sitter spec additions. Not blocking current layer fixes. Tracked in 04_easy_wins_matrix.md items REL-1 through REL-4.

### Render Cap

N/A — infrastructure layer.

### Decay Rules

N/A — infrastructure layer.

### Measurement Requirements

| Metric | Source | Gate |
|--------|--------|------|
| `high_confidence_edge_ratio` | `get_high_confidence_edge_ratio()` | Report-only. Baseline for graph quality. |
| `inherits_edge_count` | NEW: `SELECT COUNT(*) FROM edges WHERE type='INHERITS'` | Report-only. 0 = Go indexer not extracting inheritance. |
| `usage_cache_age` | NEW: timestamp of last `_build_usage_cache()` call | Must be <= timestamp of last L6 reindex |

---

## Cross-Layer Invariants

These invariants must hold across all layers simultaneously:

### Invariant 1: Total GT tokens per task

Sum of all rendered GT tokens across L1 + L3 (all fires) + L3b (all fires) + L4 + L5b (all fires) must not exceed **2,000 tokens** in any single task.

Budget allocation at maximum:
- L1: 400 tokens (one-shot)
- L4: 120 tokens (one-shot)
- L3: 300 tokens * ~3 fires average = 900 tokens (but decay reduces this)
- L3b: 250 tokens * ~8 fires average = 2000 tokens (but decay and final-silence reduce this to ~800)
- L5b: 180 tokens * ~2 fires average = 360 tokens

Realistic total with decay: ~1,200-1,800 tokens. The 2,000-token ceiling is a safety net, not a target.

### Invariant 2: Every rendered message has a reaction

For every GTLayerEvent with `next_action_type` populated and `emitted=True`, the reaction joiner must produce exactly one GTAgentReactionEvent. Zero reactions for populated-next-action events is a pipeline bug.

### Invariant 3: No layer fires after task finish

Once the agent emits `FinishAction`, no GT layer may inject tokens into the agent's observation. L5 may fire a detection (for `unsafe_finish`), and L5b may emit a warning, but this is BEFORE the finish is committed. Post-finish, only Hygiene (scaffold strip) and Meta (telemetry flush) run.

### Invariant 4: Token caps are enforced, not advisory

Every render cap listed in this document is a hard ceiling enforced in code. If rendering exceeds the cap, the output is truncated (not rejected). Truncation is logged in telemetry with `truncated=True` and `tokens_before_truncation`.

### Invariant 5: Anti-overfitting

No task ID, repo name, or benchmark-specific string appears in any layer's code path. All heuristics operate on structural properties: edge counts, confidence scores, iteration ratios, action types. Verification: grep the codebase for task IDs from the benchmark set; any match is a bug.
