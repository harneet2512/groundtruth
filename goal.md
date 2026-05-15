# Goal: Fill Every Deep Layer Grounded Metric

Target: `reports/l5_goku/GOAL_DEEP_LAYER_GROUNDED_METRICS.md` — 300+ metrics across 8 layers + meta.
Every cell filled after 1-task run. No blanks. No fired-only. Utilization >= 0.75 or documented reason.

## Current State (2026-05-15)

### Done
- Decision 34 in decisions.md (research-backed, 12 citations)
- Schema extensions: GTLayerEvent + GTAgentEvent + constants
- event_classifier.py: file_kind, check_kind, event_bucket, verification_strength
- state.py: diff tracking, witness tracking, debounce, task-scoped path
- P0 hooks: 5 new generalized hooks
- governor.py goku_check(): self-populating state, confidence gating
- metrics.py: proof spine + utilization from JSONL
- 199 tests pass (146 existing + 53 preflight)
- Replay: 4 fires on loguru-1297 (all correctly suppressed as MEDIUM in mid_commitment)

### Gap
- oh_gt_full_wrapper.py NOT wired (2900 lines, untouched)
- GTAgentEvent NOT emitted at action boundaries
- L1/L3/L3b/L4/L6/Hygiene metrics NOT computed in structured form
- Per-event L5 log fields (27 fields) NOT populated
- Reaction joiner doesn't produce all agent-side metrics
- No run summary aggregator that fills ALL 300+ cells

### Critical Path
1. Wire goku_check() into wrapper at CmdRunAction + post_edit + finish
2. Emit GTAgentEvent at every action boundary
3. Pass L3/L3b next_action into state.record_gt_next_action()
4. Pass diff_size from git diff into goku_check()
5. Extend metrics.py to compute ALL metrics from streams
6. Run 1-task smoke, produce filled table

## Research Citations (all decisions backed by)

| Decision | Citation | Finding |
|---|---|---|
| L5 event-driven, not test-dependent | Agentless (ICLR 2025), Hashimoto (2026) | Structural verification without test failures |
| Confidence gating | FeedbackEval (2025) | Mixed feedback > pure; suppress weak signal |
| Append-only, no reset | SWE-agent ACI (NeurIPS 2024) | Concise ACI elements, never destabilize |
| Fire at tool boundaries | Strands Agents (AWS 2025) | 100% vs 82.5% for prompt-based |
| Token-light emissions | JetBrains Complexity Trap (NeurIPS 2025) | Survive observation masking |
| L5 = external oracle | Huang et al. (TACL 2024) | LLMs cannot self-correct without external feedback |
| Generalized classifiers | SWE-Pruner (2025), ARISE (ASE 2025) | file_kind/check_kind, not framework names |
| Structural witnesses from L3/L3b | RepoGraph (ICLR 2025) | k-hop ego-graphs, +32.8% |

## Progress Log

- [2026-05-15 T1] Decision 34 written, schemas extended, P0 hooks built
- [2026-05-15 T2] goku_check() self-populates state, replay shows 4 fires
- [2026-05-15 T3] GOAL doc written with exact metric list from user prompt
- [2026-05-15 T4] Wrapper wired: goku_check at CmdRunAction + finish + L3/L3b next_action feed
- [2026-05-15 T5] GTAgentEvent emitted at every action boundary
- [2026-05-15 T6] Run summary computed at task close via metrics.py
- [2026-05-15 T7] Replay loguru-1297: 4 fires (WEAK_VERIFICATION_AFTER_EDIT), all correctly suppressed as MEDIUM in mid_commitment. State correct: 3 edited files, 9 verifications, targeted iter 47 > edit iter 41 → finish hook correctly silent.
- [2026-05-15 T8] 199 tests pass after wrapper + state file cleanup fixture

## What the replay proves

The Goku governor:
1. Self-populates state from raw actions (edits, verifications, classifications)
2. Detects WEAK_VERIFICATION_AFTER_EDIT correctly (broad pass, no targeted)
3. Confidence-gates MEDIUM correctly (suppressed in mid band)
4. Would emit in late/final band with same detection
5. Finish hook correctly silent when targeted verification exists (iter 47 > edit iter 41)

## Next: Build metrics aggregator that fills ALL 300+ cells
The JSONL streams are now wired. The metrics.py needs extension to compute every metric in the GOAL doc.
Current metrics.py computes: per-layer utilization, proof spine, hard fails.
Missing: L1 GT-side/agent-side/tandem, L3 GT-side/agent-side/utilization, L3b, L4, L5 per-event fields, L5b, L6, Hygiene, Meta/Reaction — the full 300+ list.
