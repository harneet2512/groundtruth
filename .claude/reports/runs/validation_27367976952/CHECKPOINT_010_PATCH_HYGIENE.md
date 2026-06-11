# Checkpoint 010 — Patch hygiene (B8)

Date: 2026-06-11

## Boundary

Layer fixed: patch file classification at submission packaging.

Out of scope: verifier grading, agent patch generation.

## Bug addressed

Lockfile-only / generated / noise patches were not classified separately from real source fixes.

## Code changes

- `scripts/swebench/patch_hygiene.py` — `classify_patch()`, `classify_file()`
- `scripts/swebench/package_submission.py` — `patch_hygiene` per instance in `deep_metrics.json`
- `scripts/swebench/convert_to_submission.py` — `patch_hygiene` on each prediction row
- `tests/test_patch_hygiene.py`

## Classes

`source_fix`, `lockfile`, `generated`, `noise`, `missing_model_patch`

## Verification

```text
python -m pytest tests/test_patch_hygiene.py -q
3 passed
```

## Bug ledger delta

| Bug | Status |
|-----|--------|
| B8 | **FIXED** |
