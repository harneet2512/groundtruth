# Checkpoint 004 — Layer 4 evidence delivery (B11)

Date: 2026-06-11

## Boundary

Layer fixed: L4 runtime evidence delivery on the DeepSWE oracle route only.

**In scope:** `artifact_deepswe/gt_mini_patch.py`, `tests/test_verified_adapter.py`.

**Out of scope:** metrics, certs, LSP, outcome classification, path_policy, patch hygiene, gt_oracle replay sensor.

## Bug addressed

**Symptom:** `python -m pytest tests/test_verified_adapter.py -q` → 2 failed (evidence not appended on `cat a.py` / `cat /testbed/a.py`).

**Root cause:** Oracle relevance gate (`_oracle_gate_blocks`) suppressed `l3b.evidence` as `irrelevant` when `_oracle_focus()` was empty. The candidate was appended with `edit_bound=False` even though the trigger was a resolved `post_view` / `post_edit` event. `_evidence()` built valid `[WITNESS]` body from graph.db; the gate dropped it before append.

**Locations:** `gt_mini_patch.py` `_augment_output` (~3074), `_oracle_gate_blocks` (~2873).

## Code changes

- `artifact_deepswe/gt_mini_patch.py`
  - Set event-bound flag on `l3b.evidence` when `_classify(cmd)` is `post_view` or `post_edit` with a resolved file and non-empty evidence (§15.3 VIEW policy waiver).
- `tests/test_verified_adapter.py`
  - Clear `_oracle_delivered_hashes` in abs-path test (oracle content-hash dedup vs `_seen` dedup).
  - Add `test_oracle_view_evidence_not_irrelevant_without_anchors`.

## Verification

```text
python -m pytest tests/test_verified_adapter.py -q
23 passed, 1 warning in ~14s
```

## LIPI (§12 L4)

- L4 fires on **event** (view/edit), not ambient — preserved.
- Correct-or-quiet preserved (no graph → no fabricated evidence).
- Oracle still ≤1 emission/turn; only relevance waiver for event-bound evidence changed.

## Product impact

Agents viewing or editing a source file now receive deterministic `[WITNESS]` evidence on the oracle route even when issue anchors are absent (fake-env tests, early trajectory turns). Metrics/certs unchanged.

## Bug ledger delta

| Bug | Status |
|-----|--------|
| B11 | **FIXED** (CP004) |
