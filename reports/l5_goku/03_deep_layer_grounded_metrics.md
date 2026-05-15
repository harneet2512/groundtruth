# Deep Layer Grounded Metrics Spec — 2026-05-15

## JSONL Streams

| # | Stream | File | Content |
|---|--------|------|---------|
| 1 | Layer events | gt_layer_events_{task}.jsonl | Every GT layer activation (emit/suppress) |
| 2 | Agent reactions | gt_agent_reactions_{task}.jsonl | GT->agent follow-through |
| 3 | Belief ledger | gt_belief_ledger_{task}.jsonl | File belief state transitions |
| 4 | Agent events | gt_agent_events_{task}.jsonl | Agent actions classified by GT taxonomy |
| 5 | Run summary | gt_run_summary_{task}.json | Aggregated proof tables |

## Schema Extensions (Decision 34)

New fields on GTLayerEvent:
- event_bucket (VALID_EVENT_BUCKETS)
- file_kind (VALID_FILE_KINDS)
- check_kind (VALID_CHECK_KINDS)
- verification_strength (STRONG/WEAK/NONE/UNKNOWN)
- confidence_level (HIGH/MEDIUM/LOW/NONE)
- confidence_score (0.0-1.0)
- confidence_basis (machine-parseable string)

New GTAgentEvent schema:
- agent_action_id, iter, event_bucket, agent_event_type
- file_path, file_kind, command, check_kind, verification_strength

## Utilization Score

| Score | Meaning |
|---|---|
| 0.00 | Absent or fired-only |
| 0.25 | Text emitted, no structured values |
| 0.50 | Structured GT-side, no agent-side |
| 0.75 | Structured both sides |
| 1.00 | Both sides + correct suppression + behavior proxy |

## Hard Fail Conditions

| Condition | Severity |
|---|---|
| emitted=true with empty event_id | FATAL |
| suppressed=true with null suppression_reason | FATAL |
| next_action_type without reaction | FATAL |
| L5 event named after test framework | DESIGN_VIOLATION |
| check_kind contains raw command string | DESIGN_VIOLATION |
| Token cap exceeded | CAP_VIOLATION |
| L5b without safety checker | INTEGRITY_VIOLATION |

## Proof Spine

Every run must satisfy:
- every_emitted_event_has_id = true
- every_suppression_has_reason = true
- every_next_action_has_reaction = true
- every_rendered_message_has_id = true
- no_malformed_events = true
