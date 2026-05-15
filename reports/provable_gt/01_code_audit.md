# 01 Structured-vs-Rendered Audit: Every GT Layer

Audit date: 2026-05-15
Auditor: Claude Opus 4.6 (automated code audit)

## Methodology

For each GT layer, the source code was read in full and evaluated against five audit questions:

1. **Does the layer produce structured data internally?** (typed dataclass / dict with defined keys)
2. **Is the structured data persisted to JSONL?** (written to file or emitted via GTTelemetryWriter)
3. **Is a GTLayerEvent emitted?** (using the canonical `telemetry/schemas.py:GTLayerEvent` schema + `writer.py:emit_layer_event()`)
4. **Can the structured data be joined to the agent reaction?** (via event_id or iter match to `_log_gt_interaction` entries)
5. **Is the rendered text the sole artifact?** (the ONLY thing persisted is a string, not evidence items)

Status definitions:
- **IMPLEMENTED_STRUCTURED**: Both structured evidence items and rendered text are persisted to JSONL with joinable IDs.
- **IMPLEMENTED_RENDERED_ONLY**: Evidence is produced and rendered, logged to JSONL, but only as `gt_sent` (rendered text blob). No `evidence_items[]` array persisted.
- **WIRED_BUT_NOT_MEASURED**: Layer fires and produces output, but telemetry only records ok/fail/skipped counters. No evidence content preserved.
- **NOT_IMPLEMENTED**: The schema/code exists but is never instantiated or called from the runtime path.
- **BROKEN_OR_NOT_JOINABLE**: Structured data exists somewhere but cannot be correlated to agent actions due to missing IDs or disconnected persistence.

---

## Executive Summary

| Layer | Source File(s) | Status | Gap Summary |
|-------|---------------|--------|-------------|
| L1 | `src/groundtruth/pretask/v1r_brief.py` | IMPLEMENTED_RENDERED_ONLY | `V1RBriefResult.files` (structured `FileEntry` list) exists in memory. Wrapper extracts `out.brief_text` only. No evidence items persisted to JSONL. |
| L3 | `src/groundtruth/hooks/post_edit.py` | IMPLEMENTED_RENDERED_ONLY | `_evidence_accumulator` captures structured dicts. `__GT_STRUCTURED__` sentinel prints JSON. But wrapper never passes `--structured-output`. Only rendered text reaches `_log_gt_interaction`. |
| L3b | `src/groundtruth/hooks/post_view.py` | IMPLEMENTED_RENDERED_ONLY | Same pattern as L3: `_evidence_accumulator` + `__GT_STRUCTURED__` sentinel exist, wrapper never enables them. Only rendered navigation lines logged. |
| L4 | `scripts/swebench/oh_gt_full_wrapper.py` | WIRED_BUT_NOT_MEASURED | `_run_l4_prefetch()` fires, produces `<gt-prefetch>` block. Telemetry records query count + line count. No evidence items. Rendered block appended to brief, logged via L1 `_log_gt_interaction`. |
| L5 | `src/groundtruth/trajectory/governor.py` | IMPLEMENTED_RENDERED_ONLY | `_log()` writes to `/tmp/gt_l5_telemetry.jsonl` with `message_text` and `next_action`. Wrapper logs via `_log_gt_interaction`. No `GTLayerEvent` emission. No `evidence_items[]`. |
| L5b | `src/groundtruth/trajectory/hooks.py` | NOT_IMPLEMENTED | `L5bSafetyChecker` exists as a validator class. Never instantiated in governor or wrapper. No emission path. |
| L6 | `scripts/swebench/oh_gt_full_wrapper.py` | WIRED_BUT_NOT_MEASURED | `make_reindex_command()` fires, mtime delta checked. Telemetry records ok/fail counter. Wrapper logs via `_log_gt_interaction` with "reindex_ok"/"reindex_fail". No structured data about what changed in the index. |
| Hygiene | `scripts/swebench/oh_gt_full_wrapper.py` | WIRED_BUT_NOT_MEASURED | `_strip_scaffold_files()` fires. Prints count to stdout. No structured event. No `_log_gt_interaction` call. No telemetry record. |
| Meta | `scripts/swebench/oh_gt_full_wrapper.py` | BROKEN_OR_NOT_JOINABLE | `_log_gt_interaction()` writes per-interaction JSONL with `gt_sent`, `agent_action_before`, `agent_action_after` (backfilled). No `event_id`. No `evidence_items[]`. Cannot be joined to `GTLayerEvent` (which is never emitted). |

**Critical finding:** `GTLayerEvent`, `GTAgentReactionEvent`, and `GTBeliefEvent` (defined in `src/groundtruth/telemetry/schemas.py`) and `GTTelemetryWriter` (defined in `src/groundtruth/telemetry/writer.py`) are **never instantiated anywhere** in the runtime path. Zero calls to `emit_layer_event()`, `emit_agent_reaction()`, or `emit_belief_event()` exist in `oh_gt_full_wrapper.py`, `post_edit.py`, `post_view.py`, `governor.py`, or `hooks.py`. The entire telemetry schema is dead code from the runtime's perspective.

---

## Layer-by-Layer Audit

### L1: Pre-Task Brief (`src/groundtruth/pretask/v1r_brief.py`)

**Internal structured data:** Yes. `V1RBriefResult` is a frozen dataclass containing:
- `files: list[FileEntry]` where each `FileEntry` has `path`, `score`, `functions`, `test_mappings`, `callees`
- `brief_text: str` (the rendered output)
- `token_estimate: int`
- `v74_result: V74BriefResult | None` (upstream retrieval result with `ranked_full`, `focus_set`)

**Persistence to JSONL:** No. The wrapper (line 2195-2201) calls `generate_v1r_brief()`, extracts `out.brief_text` as a string, and discards the `V1RBriefResult` object. The wrapper logs `brief_full_for_log` (rendered text) via `_log_gt_interaction(config, "L1", "brief", "brief_injection", brief_full_for_log)` (line 2385). The structured `FileEntry` list, per-file scores, and v74 retrieval components are never serialized.

**GTLayerEvent emission:** No. No call to `GTTelemetryWriter.emit_layer_event()` anywhere in the brief generation or injection path.

**Joinability:** The `_log_gt_interaction` entry has no `event_id`. It records `iter=0` (since the brief fires before iteration starts). The `agent_action_before` field is empty string for L1. There is no mechanism to correlate the brief's structured candidates with later agent file-open actions.

**What is lost:**
- Per-file retrieval scores (which retrieval signal -- semantic, lexical, reach, anchor_prox -- contributed to each candidate)
- Whether cross-domain expansion fired (cochange/test_coimport bridges)
- Whether hub-suppression or modulus gate filtered candidates
- The `v74.ranked_full` list (all candidates with component scores)
- `v74.focus_set` (only printed to diagnostic stdout, never persisted)

**Status: IMPLEMENTED_RENDERED_ONLY**

---

### L3: Post-Edit Hook (`src/groundtruth/hooks/post_edit.py`)

**Internal structured data:** Yes. The `_evidence_accumulator` parameter (line 541) is an optional `list[dict]`. When non-None, the function appends structured evidence items:
- `l3_caller_code`: file_path, symbol, line_start, text, source, reason (line 703-711)
- `l3_test_assertion`: file_path, symbol, text, source (line 731-738)
- `l3_signature`: file_path, symbol, text, source (line 744-748)
- `l3_sibling_pattern`: file_path, symbol, text, source (line 772-779)
- `l3_targeted_verification`: text, source, reason (line 804-809)

The `__GT_STRUCTURED__` sentinel (line 1493) prints the accumulator as JSON to stdout when `--structured-output` is passed on the CLI.

**Persistence to JSONL:** No at the structured level. The wrapper invokes post_edit.py via subprocess inside the container. It reads stdout and extracts the rendered text (the `<gt-evidence>` block). The wrapper **never passes `--structured-output`** (confirmed by grep: zero matches for `structured.output` or `structured_output` in `oh_gt_full_wrapper.py`). Therefore `_evidence_accumulator` is always None in production, and the `__GT_STRUCTURED__` sentinel never fires.

The rendered text is logged via `_log_gt_interaction(config, "L3", ...)` with `gt_sent=framing + hook_body_edit` (line 1811). This is a text blob.

**GTLayerEvent emission:** No. Zero calls.

**Joinability:** The `_log_gt_interaction` entry contains `iter`, `layer="L3"`, `trigger="post_edit:{file}"`, `type="evidence"|"GT_OK"|"dedup"`. No `event_id`. Cannot be joined to `GTLayerEvent` schema. Can be correlated to agent actions only by iteration number match (fragile -- multiple GT injections can occur in the same iteration).

**What is lost in production:**
- Which evidence families actually produced items (caller, sibling, signature, test) vs were empty
- Per-caller file paths and code snippets as separate queryable records
- Confidence scores per evidence item
- The distinction between connected/minimal file classification
- Blast radius count (logged in rendered text but not as a structured field)

**Status: IMPLEMENTED_RENDERED_ONLY**

---

### L3b: Post-View Hook (`src/groundtruth/hooks/post_view.py`)

**Internal structured data:** Yes. Same `_evidence_accumulator` pattern as L3. When non-None, appends:
- `l3b_caller_edge`: file_path, text, source, reason (line 310-314)
- `l3b_callee_edge`: file_path, text, source, reason (line 316-320)
- `l3b_importer_edge`: file_path, source, reason (line 360-363)

**Persistence to JSONL:** No at the structured level. Same problem as L3: wrapper does not pass `--structured-output`. The `_accum` variable in `main()` (line 416) is only populated when `args.structured_output` is true, which requires the CLI flag. The wrapper invokes post_view.py via subprocess and only reads the rendered navigation lines.

Rendered output logged via `_log_gt_interaction(config, "L3b", ...)` with `gt_sent=hook_body` (line 1613).

**GTLayerEvent emission:** No. Zero calls.

**Joinability:** Same weakness as L3. Iter-based correlation only. No event_id.

**What is lost:**
- Which neighbor files were suppressed by visited-file dedup
- Hub-penalized scores for each neighbor
- Issue-relevance scores for each neighbor
- Whether [CANDIDATE] annotation was applied

**Status: IMPLEMENTED_RENDERED_ONLY**

---

### L4: Prefetch (`scripts/swebench/oh_gt_full_wrapper.py`, `_run_l4_prefetch`)

**Internal structured data:** Minimal. The function builds a list of `blocks` (strings) from gt_query output. It tracks `queries_run`, `total_lines`, and `symbols`. These are logged to stdout (line 2060) but not as structured records.

**Persistence to JSONL:** No structured evidence. The telemetry object `tel.record_l4_prefetch(queries_run, total_lines)` (line 2066) increments the `L4` ok/skipped counter in `GTTelemetry`. The rendered prefetch block is appended to the brief string and logged via the L1 `_log_gt_interaction` call (since it is concatenated before injection). There is no separate `_log_gt_interaction` call for L4 specifically.

**GTLayerEvent emission:** No. Zero calls.

**Joinability:** L4 output is embedded in the L1 brief text blob. It cannot be separated programmatically except by parsing the `<gt-prefetch>` XML tag from the `gt_sent` string.

**What is lost:**
- Per-symbol query results (which symbols were queried, which returned evidence, which were empty)
- Git precedent commit hashes and messages
- Wall time per individual query
- Noise-pattern filtering decisions

**Status: WIRED_BUT_NOT_MEASURED**

---

### L5: Trajectory Governor (`src/groundtruth/trajectory/governor.py`)

**Internal structured data:** Yes. The `_log()` method (line 318-343) constructs a dict with:
- `timestamp`, `layer`, `hook` (name), `iter`, `max_iter`, `band`, `phase`
- `fired` (bool), `suppressed_reason`
- `l5_messages_total`, `message_len`, `message_text` (truncated to 500 chars), `next_action`

This is written to `/tmp/gt_l5_telemetry.jsonl` (line 338-342).

**Persistence to JSONL:** Partially. The telemetry JSONL captures the decision metadata (hook name, band, fired/suppressed) and the rendered message. But:
- No `event_id` field
- No `evidence_items[]` array
- No link to the upstream L3/L3b evidence that informed the decision
- `message_text` is truncated to 500 chars

The wrapper also logs L5 events via `_log_gt_interaction(config, "L5", ...)` (line 1547, 1831). This records `gt_sent` (rendered text) + `agent_action_before`.

**GTLayerEvent emission:** No. Despite `L5` being a valid layer in `telemetry/constants.py:VALID_LAYERS`, zero calls to `emit_layer_event()` exist.

**Joinability:** The `/tmp/gt_l5_telemetry.jsonl` file and the `_log_gt_interaction` entries both lack event IDs. They can be approximately joined by timestamp and iteration number, but this is fragile.

**What is lost:**
- The `FailureSnapshot` struct (exception_type, expected/actual, top_project_frame) -- this exists in `L5TrajectoryState` but is not serialized to any telemetry file
- The `L5TrajectoryState` (edited_source_files, verification history, failure chain) -- only the current-hook decision is logged, not the cumulative state
- Whether `_injection_disabled` was true and why

**Status: IMPLEMENTED_RENDERED_ONLY**

---

### L5b: Safety Checker (`src/groundtruth/trajectory/hooks.py`)

**Internal structured data:** `L5bSafetyChecker` (line 234-260) is a static validator class with a `validate()` method that returns `(bool, str | None)` -- pass/fail and optional rejection reason. It checks for restart language, broad exploration language (in late phase), and token cap violations.

**Persistence to JSONL:** None. The class has no logging, no file output, no telemetry calls.

**Usage in production:** Not used. Grep of `oh_gt_full_wrapper.py` shows zero imports of `L5bSafetyChecker` and zero calls to `validate()`. Grep of `governor.py` shows it is imported in `hooks.py` but `governor.py` never calls it. The governor dispatches to `hook_*` functions directly; `L5bSafetyChecker.validate()` is never invoked on the messages those hooks produce.

**GTLayerEvent emission:** No. `L5b` is listed as a valid layer in `telemetry/constants.py` and `EvidenceKind.L5B_INTERVENTION` exists, but neither is ever used.

**Status: NOT_IMPLEMENTED**

---

### L6: Reindex (`scripts/swebench/oh_gt_full_wrapper.py`)

**Internal structured data:** The wrapper (line 1672-1715) performs reindex and collects:
- `exit_code` from the gt-index binary
- `mtime_before`, `mtime_after` (timestamps of graph.db)
- `r_ok` computed as `exit_code == 0 and mtime_after > mtime_before`

**Persistence to JSONL:** Counter-only. `tel_obj.record_reindex(r_ok)` (line 1713) increments `L6` ok/fail. The `_log_gt_interaction(config, "L6", ...)` call (line 1715) records `reindex_ok|reindex_fail` with `gt_sent=reindex_out[:200]` (first 200 chars of binary output).

**GTLayerEvent emission:** No. Zero calls.

**Joinability:** The `_log_gt_interaction` entry has `iter`, `layer="L6"`, `trigger="reindex:{file}"`. No event_id. The reindex output is truncated to 200 chars. Cannot determine what changed in the graph (new nodes/edges) without parsing the gt-index output.

**What is lost:**
- Number of new/updated nodes and edges after reindex
- Whether the edited file gained new callers/callees
- Whether the reindex affected L3's subsequent evidence (no before/after comparison)

**Status: WIRED_BUT_NOT_MEASURED**

---

### Hygiene: Scaffold Stripping (`scripts/swebench/oh_gt_full_wrapper.py`)

**Internal structured data:** `_strip_scaffold_files()` (line 1074-1108) produces:
- `to_strip`: list of scaffold files to remove
- `kept`: list of new non-scaffold files preserved

**Persistence to JSONL:** None. The function prints to stdout (`GT_ENFORCE: Stripping N scaffold files`), but:
- No `_log_gt_interaction()` call
- No `GTTelemetry` counter
- No JSONL output
- The list of stripped files is not recorded anywhere retrievable

**GTLayerEvent emission:** No. `HYGIENE` is listed as a valid layer in `telemetry/constants.py` and `EvidenceKind.HYGIENE_STRIP` exists, but neither is used.

**Status: WIRED_BUT_NOT_MEASURED**

---

### Meta: Interaction Logging (`scripts/swebench/oh_gt_full_wrapper.py`)

**Internal structured data:** `_log_gt_interaction()` (line 967-1040) constructs a dict per GT->agent interaction:
- `timestamp`, `iter`, `layer`, `trigger`, `type` (ev_type)
- `gt_sent` (full rendered text), `gt_sent_bytes`, `gt_sent_tokens`
- `has_real_evidence` (computed boolean)
- `agent_action_before` (what agent was doing when GT fired)
- `agent_action_after` (backfilled on next call)

Written to both:
- `/tmp/gt_meta_{instance_id}.jsonl` (per-task file)
- `/tmp/gt_interactions_{instance_id}.jsonl` (per-task via `_metrics_path`)
- In-memory `config.interaction_log` list (flushed to `instance_ref`)

**What makes this BROKEN_OR_NOT_JOINABLE:**
1. **No `event_id`:** Entries have `timestamp` and `iter` but no unique event identifier. Two GT injections in the same iteration (e.g., L6 reindex + L3 post_edit) share the same `iter` value and cannot be disambiguated in joins.
2. **No `evidence_items[]`:** The `gt_sent` field is a rendered text blob. To determine what evidence was provided, you must parse the string (looking for `CALLERS:`, `SIGNATURE:`, `Called by:`, etc.). This is fragile and lossy.
3. **No link to GTLayerEvent schema:** The `GTLayerEvent` dataclass defines `event_id`, `evidence_items`, `confidence`, `rendered_text` as separate fields. `_log_gt_interaction` flattens all of these into a single `gt_sent` string. The schemas are incompatible.
4. **`agent_action_after` is best-effort:** It is backfilled on the NEXT `_log_gt_interaction` call, not on the next agent action. If the next agent action does not trigger any GT layer, `agent_action_after` stays empty.

**Status: BROKEN_OR_NOT_JOINABLE**

---

## The GTTelemetryWriter Gap

The telemetry infrastructure (`src/groundtruth/telemetry/`) defines three complete schemas:
- `GTLayerEvent` (85 fields covering trigger, evidence, rendered output, next action)
- `GTAgentReactionEvent` (35 fields covering follow/ignore/contradict analysis)
- `GTBeliefEvent` (15 fields covering candidate status transitions)

And a thread-safe writer (`GTTelemetryWriter`) that writes three separate JSONL streams.

**None of this is used.** Zero imports of `GTTelemetryWriter` exist outside `telemetry/` itself. Zero calls to `emit_layer_event()`, `emit_agent_reaction()`, or `emit_belief_event()` exist in any runtime code. The entire subsystem is dead code from the perspective of production runs.

The actual telemetry path is:
1. `GTTelemetry` (wrapper-level dataclass) -- ok/fail/skipped counters per layer
2. `_log_gt_interaction()` -- rendered text + basic metadata per injection
3. `/tmp/gt_l5_telemetry.jsonl` -- L5 governor decision log

None of these use the `telemetry/schemas.py` types.

---

## File References

| File | Layer(s) | Key Lines |
|------|----------|-----------|
| `src/groundtruth/pretask/v1r_brief.py` | L1 | 23-37 (FileEntry, V1RBriefResult), 308-321 (render_brief), 324-495 (generate) |
| `src/groundtruth/hooks/post_edit.py` | L3 | 533-824 (generate_improved_evidence with _evidence_accumulator), 1335-1498 (main with --structured-output) |
| `src/groundtruth/hooks/post_view.py` | L3b | 188-378 (graph_navigation with _evidence_accumulator), 381-448 (main with --structured-output) |
| `src/groundtruth/trajectory/governor.py` | L5 | 79-343 (L5Governor with _log telemetry) |
| `src/groundtruth/trajectory/hooks.py` | L5b | 234-260 (L5bSafetyChecker -- dead code) |
| `scripts/swebench/oh_gt_full_wrapper.py` | L4, L6, Hygiene, Meta | 1994-2080 (_run_l4_prefetch), 1672-1715 (L6 reindex), 1074-1108 (_strip_scaffold_files), 967-1040 (_log_gt_interaction) |
| `src/groundtruth/telemetry/schemas.py` | (unused) | 85-178 (GTLayerEvent), 182-257 (GTAgentReactionEvent), 260-305 (GTBeliefEvent) |
| `src/groundtruth/telemetry/writer.py` | (unused) | 13-100 (GTTelemetryWriter -- never instantiated) |
| `src/groundtruth/telemetry/constants.py` | (unused) | 6-79 (SCHEMA_VERSION, VALID_LAYERS, token caps) |
