# Checkpoint - runtime ledger and action translation parity (2026-06-12)

Code head while writing: pending local commit after `df69bb20`

## Why this slice existed

LIPI uncovered three still-live product gaps on the mini-swe integration path:

1. `artifact_deepswe/gt_mini_patch.py` delegated phase policy to product code, but
   wrong-phase suppressions disappeared before they reached any durable truth surface.
2. Several delegated helper functions still carried unreachable legacy logic under
   an early `return`, which made the visible file state diverge from the live path.
3. `src/groundtruth/runtime/action_translation.py` was still too literal for
   caller-risk facts. It translated markers, but not enough of the "why this matters"
   behavior from `gt_gt.md`.

## LIPI

```text
ID: CP016
Desired state:
Product runtime decides speak/silence, every suppression has a reason, and the
adapter only wires mini-swe-specific transport.

Current state:
Wrong-phase suppressions were filtered out inside the adapter without a durable
runtime record; delegated helpers still carried dead legacy code; caller facts
translated weakly.

Logic:
The policy decision itself is product behavior. If a candidate is denied by
phase policy, that denial must survive as product truth, not vanish as an
implementation detail.

Implementation:
Added a product runtime ledger write path in `artifact_deepswe/gt_mini_patch.py`
for wrong-phase suppressions and delivered winners. Removed unreachable fallback
branches from delegated helper functions. Tightened action translation for
verified-caller contract facts.

Integration:
`scripts/swebench/artifact_resolver.py` now resolves `runtime_ledger` when
present, and `scripts/swebench/task_truth.py` surfaces a
`runtime_control.runtime_ledger_summary`.

Plumbing:
The runtime ledger writes to `GT_RUNTIME_LEDGER` when configured, else to
`/tmp/gt_runtime_ledger.jsonl`. `task_truth.json` now exposes the summary when
that file is available in collected artifacts.

Verdict:
PARTIAL -> stronger than before. Silent wrong-phase drops are closed on the live
adapter surface. Full no-silent-drop coverage for every suppression class is not
done yet; current slice closes the phase-policy gap first.

Remaining bug, if any:
`action_translation.py` is improved but still not a full graph-to-risk/action
translator for every evidence shape. Substrate proof (`P0-01`) is still the
launch blocker.

Fix boundary:
Adapter live path + truth surface. No benchmark-shaped logic. No GHA duplication.
```

## Files changed in this slice

- `artifact_deepswe/gt_mini_patch.py`
- `scripts/swebench/artifact_resolver.py`
- `scripts/swebench/task_truth.py`
- `src/groundtruth/runtime/action_translation.py`
- `tests/test_ledger_suppression.py`
- `tests/test_task_truth.py`
- `tests/test_action_templates.py`

## Receipts

```bash
python -m pytest tests/test_ledger_suppression.py tests/test_task_truth.py tests/test_action_templates.py tests/test_runtime_product_controller.py -q
python -m py_compile artifact_deepswe/gt_mini_patch.py scripts/swebench/task_truth.py scripts/swebench/artifact_resolver.py src/groundtruth/runtime/action_translation.py
```

Observed locally:

- `21 passed`
- `py_compile` clean

## Benchmark readiness after this slice

Still **not ready** for the 10-task GT-on run.

Reason:

- `P0-01` remains live: rebuilt substrate + pinned digest + real Go/Rust proof
  must be green first.
- This slice improved product truth and adapter honesty, but it did not prove the
  Go/Rust LSP substrate boundary.
