# GT Run Verification Checklist (Smoke, Canary, Full)

> Run this after every smoke, canary, or full eval run.
> A run is invalid if any MUST item fails.
> For GT A/B smoke runs, also follow:
> - `docs/GT_AB_5SMOKE_FULL_UTILIZATION_SPEC_2026-04.md`
> - `docs/GT_AB_5SMOKE_FULL_UTILIZATION_PROMPT_2026-04.md`

## Scope and Inputs
- Run root can be any of: `/tmp/smoke_*`, `/tmp/canary_*`, `/tmp/full_*`.
- Checklist applies to all task counts (5-task smoke, 10-task canary, large full runs).
- Use canonical GT command names in traces: `gt_orient`, `gt_lookup`, `gt_impact`, `gt_check`.

## MUST: GT Runtime Install (per container)
- [ ] `[GT]` install log exists and includes package install + index build.
- [ ] Index build reports `N nodes` where `N > 0`.
- [ ] State hook patch confirmation exists (state command patched with GT hook).

## MUST: Tool Utilization (per task)
- [ ] `gt_orient >= 1` (always used at or near start).
- [ ] `gt_check >= 1` on tasks with material source edits.
- [ ] `gt_lookup >= 1` when symbol resolution is needed.
- [ ] `gt_impact >= 1` when editing externally-called functions/interfaces.

## MUST: Hook Lifecycle and Telemetry
- [ ] Hook cycle telemetry is always present (observability always-on).
- [ ] `material_edit` event appears on tasks with source edits.
- [ ] Startup delivery exists (`gt_evidence` or startup checkpoint event).
- [ ] Suppression is logged with reason (`exact_dedup`, `window_dedup`, `compliance`, `no_signal`).
- [ ] Acknowledgment is measured from trace events (`ack_followed`, `ack_ignored`, `ack_not_observed`), not keyword overlap.

### C3 vacuous-pass clause (denominator = 0)

If an arm produces `ack_followed = 0 AND ack_ignored = 0`, C3 (`ack_followed_rate`) is marked **N/A** and does not block gating, provided all structural gates pass (`wrapper_invoked > 0`, `material_edit > 0`, `micro_emitted > 0`, `verify_emitted > 0`; `lsp_promotion > 0` for LSP arms). Per trace-grading framing, graded metrics are computed from emitted events; absent events from both numerator and denominator mean "not applicable," not "failing."

Ack classification rule:
- `ack_followed` fires when the next meaningful action inside the evidence window returns to the target file/symbol or calls a GT tool directly on the evidence target.
- `ack_ignored` fires when the next meaningful action inside the window is a non-targeted edit or GT action on another file/symbol.
- `ack_not_observed` is reserved for windows that expire without any eligible follow/ignore action.

**Guardrail.** In aggregated runs (Gate 2 = 6 × 10-task repeated runs, 60 samples per arm), require `ack_followed + ack_ignored >= 10` per arm. If Gate 2 also goes vacuous, revisit the metric — do not silently accept.

## MUST: GT MICRO Channel (confidence-gated, deterministic)
- [ ] `GT MICRO` appears for tasks with material source edits.
- [ ] Micro trigger is deterministic: first material edit of changed scope/file-version.
- [ ] Confidence tier shown as `verified` or `likely` for actionable micro content.
- [ ] Same micro guidance is not repeated more than 2 times consecutively.

## MUST: GT VERIFY Channel (deterministic trigger, not permission-gated)
- [ ] `GT VERIFY` fires at pre-submit.
- [ ] `GT VERIFY` also fires every 3rd material edit (or configured equivalent).
- [ ] Verify dedup prevents identical repeated verify payloads.
- [ ] Verify output never overrides stronger confidence policy (no ambiguous-as-fact).

## MUST: Confidence and Truthfulness Gates
- [ ] Ambiguous/stale signals never emitted as hard facts.
- [ ] High-confidence blockers are clearly distinguished from soft warnings.
- [ ] Telemetry still logs decision/suppression when content is withheld.

## MUST: Auth and Trajectory Health
- [ ] No `401 ACCESS_TOKEN_EXPIRED` (or equivalent auth expiry) across run logs.
- [ ] No auth-driven 1-step exits.
- [ ] Every scheduled task emits a trajectory artifact.

## MUST: Prediction Coverage
- [ ] `preds.json` exists for each run.
- [ ] Non-empty patch rate meets run target:
- Smoke/Canary: `>=50%` non-empty patches.
- Full: track and compare against baseline target for that experiment.

## Recommended Metrics Table (report each run)
- `tasks_total`
- `tasks_with_material_edit`
- `gt_orient_rate` = tasks with `gt_orient >= 1` / `tasks_total`
- `gt_check_rate` = tasks with `gt_check >= 1` / tasks with material edits
- `micro_emit_rate` = tasks with `GT MICRO` / tasks with material edits
- `verify_emit_rate` = tasks with `GT VERIFY` / tasks with material edits
- `ack_followed_rate` = `ack_followed` / (`ack_followed` + `ack_ignored`)
- `auth_fail_count`
- `one_step_traj_count`
- `non_empty_patch_rate`

## Quick Checks

```bash
# 1) Point to run root
RUN_ROOT=/tmp/canary_20260416_1200   # or /tmp/smoke_... /tmp/full_...

# 2) GT install / patch signals
grep -R "\[GT\]" "$RUN_ROOT" | head -200

# 3) Tool usage + micro marker
grep -R "gt_orient\|gt_lookup\|gt_impact\|gt_check\|GT MICRO\|GT VERIFY" "$RUN_ROOT" | head -400

# 4) Telemetry event distribution
cat "$RUN_ROOT"/**/gt_hook_telemetry.jsonl 2>/dev/null | \
  grep -o '"event":"[^"]*"' | sort | uniq -c | sort -rn

# 4b) Ack event distribution
cat "$RUN_ROOT"/**/gt_hook_telemetry.jsonl 2>/dev/null | \
  grep -o '"event":"ack_[^"]*"' | sort | uniq -c | sort -rn

# 5) Auth failures
grep -R "401\|ACCESS_TOKEN_EXPIRED\|token.*expired\|Unauthorized\|Forbidden" "$RUN_ROOT" | wc -l

# 6) preds coverage
python3 -c "
import json,glob
for f in sorted(glob.glob('$RUN_ROOT/**/preds*.json', recursive=True)):
    p=json.load(open(f))
    patches=sum(1 for v in p.values() if (v.get('model_patch') or '').strip())
    print(f'{f}: {patches}/{len(p)} non-empty patches')
"
```

## Red Flags (immediate investigation)
- `gt_orient = 0` broadly -> GT tools unavailable or prompt/command mismatch.
- `GT MICRO = 0` on all edited tasks -> hook path or material edit detection broken.
- `GT VERIFY` missing at pre-submit -> verify trigger regression.
- Hook telemetry missing while tasks run -> state command not patched/firing.
- High suppress count with low emits -> confidence/dedup thresholds too aggressive.
- Many 1-step exits -> auth or environment startup failure.
