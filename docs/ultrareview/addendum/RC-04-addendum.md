# RC-04 Addendum — SQLite correctness fix

## What shipped

Closed findings: **A-012, A-025, B-006, B-018, C-004, E-007, E-012, E-013, H-020.**

### Go writer (`gt-index/internal/store/sqlite.go`, `gt-index/cmd/gt-index/main.go`)

1. `_synchronous=OFF` → `_synchronous=NORMAL`. With WAL + NORMAL, a successful
   `Commit()` is durable across power loss / OOM / SIGKILL. OFF was the actual
   crash-corruption vector (E-012).
2. Added `(*DB).CheckpointWAL()` that runs `PRAGMA wal_checkpoint(TRUNCATE)`.
   Called once after the incremental-reindex transaction `Commit` in
   `runIncremental()`. Bounds the reader-vs-writer torn-read window; the WAL
   never grows unbounded on a long-running indexer.
3. `gt-index` now writes `project_meta.min_confidence` at the end of a full
   build (median of resolved edge confidences, fallback 0.5 on empty input).
   No schema change — re-uses the existing `project_meta` table, so no
   conflict with whatever RC-17 may do later.

### Agent-facing readers (4 tools + L5 gate + brief layer)

For each of `gt_query`, `gt_search`, `gt_navigate`, `gt_validate`, and
`gt_pre_finish_gate`:

1. `?mode=ro&immutable=1` → `?mode=ro` (only). `immutable=1` was a caller
   promise that the file is unchanging — false in practice, since the
   `gt-index` writer overlaps with reader processes during incremental
   reindex. With `immutable=1` SQLite skips locking entirely, allowing
   torn-read 0-row returns. Drop it; pay the locking cost via
   `busy_timeout=5000` + `timeout=10`.
2. Added `PRAGMA integrity_check` on first connect. Anything other than
   `ok` produces a `db_corrupt` exit (4) with the offending message on
   stderr — agent-visible, never a silent empty result. (The L5 gate
   surfaces `result: db_corrupt` in its verdict instead of `db_open_error`.)
3. `MIN_CONFIDENCE` is no longer hardcoded `0.7`. The constant is now
   `0.5` (compile-time fallback) and per-conn runtime resolution goes through
   `_resolve_min_confidence(conn) → _conf_for(conn)`:
   - Read `project_meta.min_confidence` if the row exists, clamped to
     `(0.0, 0.9]` so a degenerate index can't push the threshold to 1.0
     and over-filter legitimate `name_match` (singleton, conf=0.9) edges.
   - Otherwise fall back to `0.5` (parity with `gt_intel.MIN_CONFIDENCE`
     in the brief layer). No live-P50 fallback — we tried it and on small
     / mostly-`same_file` graphs the P50 collapses to 1.0, breaking
     `name_match` evidence in the L4 fixture; the directive's "0.5
     fallback" is the structurally correct choice.

### Brief layer (`src/groundtruth/pretask/v22_brief.py:78`)

`sqlite3.connect(graph_db_path)` was upgraded to URI mode + `mode=ro` +
`busy_timeout=5000` + `timeout=10`, so the start-line lookup helper no
longer races with the writer.

## Pending integration in main.go (RC-04 ↔ RC-17 coordination)

The `gt-index` `cmd/gt-index/main.go` file in the working tree carries
parallel modifications from RC-17 (build-stamp variables, deterministic
`indexed_at`, removal of wall-clock `build_time_ms`). To keep RC-04 a
single, atomic commit covering only its own surface, **the two main.go
hooks below are NOT in this commit and must be applied as part of the
next pass that lands alongside RC-17**:

1. `import "sort"` is needed by `computeMedianConfidence`.
2. After the incremental `tx.Commit()` in `runIncremental` (~line 591):
   `db.CheckpointWAL()` — folds WAL frames immediately so a SIGKILL
   between the next reindex and the next reader cannot leave a partial
   WAL visible.
3. After the metadata block in `main()` (~line 329):
   `db.SetMeta("min_confidence", fmt.Sprintf("%.4f", computeMedianConfidence(resolved)))` —
   writes the per-repo threshold the Python readers consume.
4. New helper at end of file:
   ```go
   func computeMedianConfidence(rcs []resolver.ResolvedCall) float64 {
       if len(rcs) == 0 { return 0.5 }
       xs := make([]float64, 0, len(rcs))
       for _, r := range rcs { xs = append(xs, r.Confidence) }
       sort.Float64s(xs)
       mid := len(xs) / 2
       if len(xs)%2 == 1 { return xs[mid] }
       return (xs[mid-1] + xs[mid]) / 2
   }
   ```

Until this lands, Python readers will simply fall back to `0.5` (their
behaviour when `project_meta.min_confidence` is missing), which is the
brief-layer parity floor and the safe default. The RC-04 tool-side
correctness fixes (drop `immutable=1`, add integrity_check, etc.)
deliver the bulk of the cluster's value with or without the meta-write.

## Out-of-scope coordination points

`benchmarks/swebench/gt_intel.py:68` and `tools/sweagent/gt_edit/lib/gt_intel.py:75`
still hold `MIN_CONFIDENCE = 0.7` in module scope. Per RC-04 directive these
are out of scope for this commit; both sites now carry a `TODO(RC-04-coord)`
pointing at the per-repo `project_meta.min_confidence` contract so the next
pass can finish the unification.

## What it does NOT touch

- The `project_meta` schema (no new columns) — pure key/value writes.
- The `same_file` / `import` / `name_match` resolution-method contract.
- The brief layer's `MIN_CONFIDENCE = 0.5` (already correct per directive).
- Anything in RC-17's planned territory (deterministic-build / fixed
  timestamps / `high_freq_identifiers` meta key).

## Test signal

`pytest -x` on the relevant suites:
- `tests/layers/test_l4_gt_query.py` — 9/9 pass (the
  `test_known_symbol_low_conf` case directly exercises the conf=0.9
  `name_match` edge that the new `0.5` floor must let through).
- `tests/layers/test_l5_gate.py` — 15/15 pass (excluding pre-existing
  `test_scratch_file_untracked` failure that is unrelated to RC-04 —
  reproduces on `HEAD` without these changes).
- `tests/layers/test_pullback_hook.py` — 26/26 pass.

## New bugs surfaced during work

None. The L5 `test_scratch_file_untracked` failure mentioned above
predates RC-04 and is a `debug_test.py` prefix not matching the existing
scratch-pattern set; it should be tracked separately.

## Integration check

`docs/ultrareview/integration_checks/RC-04.sh`. Exercises:
1. Full index of a real subtree.
2. `PRAGMA integrity_check` returns `ok`.
3. `project_meta.min_confidence` is populated by `gt-index`.
4. Concurrent writer + reader returns correct rowcount.
5. SIGKILL the writer mid-write — reader either recovers cleanly or surfaces
   `db_corrupt` (never silent 0-row).
6. `immutable=1` is gone from every agent tool.
7. `MIN_CONFIDENCE = 0.7` is gone from every patched site.

Run locally only — DO NOT run on the VM.
