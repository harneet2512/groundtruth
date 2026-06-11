# Checkpoint 009 — Surface path policy (B7)

Date: 2026-06-11

## Boundary

Layer fixed: centralized vendored/generated/minified path filtering.

Out of scope: `gt_mini_patch.py` inline copy (container constraint — parity via shared policy module).

## Bug addressed

Vendor/static/generated paths (e.g. aiomonitor Tailwind assets) surfaced inconsistently across brief, localizer, and post_view.

## Code changes

- `src/groundtruth/delivery/path_policy.py` — `is_vendored_path`, `is_generated`, `is_delivery_excluded`
- `src/groundtruth/pretask/graph_localizer.py` — imports `is_generated`
- `src/groundtruth/pretask/v1r_brief.py` — imports `is_vendored_path`
- `src/groundtruth/hooks/post_view.py` — imports `is_vendored_path` via `_is_vendor_path`
- `tests/test_path_policy.py`

## Excluded patterns (appendix)

- Vendor dirs: `/vendor/`, `/node_modules/`, `/extern/`, `/dist/`, `/static/`, `/assets/`, …
- Minified suffixes: `.min.js`, `.min.css`, …
- Generated markers: `.pb.go`, `_pb2.py`, `.g.dart`, `zz_generated`, …

## Verification

```text
python -m pytest tests/test_path_policy.py -q
3 passed
```

## Bug ledger delta

| Bug | Status |
|-----|--------|
| B7 | **FIXED** |
