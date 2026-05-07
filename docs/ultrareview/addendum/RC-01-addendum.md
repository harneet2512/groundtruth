# RC-01 — Addendum

New bugs surfaced during Phase 3 RC-01 fix work go here.

Format per entry:

```
## RC-01-addendum-NNN — <short title>
- discovered_during: <step>
- location: <path:line>
- observation: <what>
- severity: BLOCK | MAJOR | MINOR
- next: <action / owner / coord cluster>
```

## RC-01-addendum-001 — Go indexer does not populate `meta.high_freq_identifiers` / `meta.blast_radius_p95`

- discovered_during: writing the per-repo helper functions for RC-01 fixes (a) and (b)
- location: `gt-index/internal/store/sqlite.go`, `gt-index/cmd/gt-index/main.go`
- observation: The Python readers (`_high_freq_repo_identifiers` in `gt_navigate.py`/`gt_intel.py`,
  `_blast_radius_threshold` in `gt_pre_finish_gate.py`) prefer a `meta` row written by the indexer
  but currently fall back to a live SQL computation every process. This works correctly but pays
  the cost of the percentile/top-N scan once per process per db. The Go side should compute the
  same statistics at index time and write CSV / numeric values into `meta` so readers hit the
  cached path. RC-01 is in scope for Python only; the Go-side work is owned by RC-17/RC-04
  per the BUG_GRAPH constraint.
- severity: MINOR (graceful fallback present; perf cost is one extra GROUP BY per process)
- next: coordinate with RC-17/RC-04 owner; add `meta` table population in `sqlite.go` schema
  bootstrap and call from `main.go` after the CALLS pass. `# TODO(RC-01-coord)` markers exist
  in the affected Python files.

## RC-01-addendum-002 — Benchmark task IDs in `benchmarks/*.json` carry literal repo names

- discovered_during: post-fix anti-benchmaxxing grep
- location: `benchmarks/live_lite_300_ids.json`, `benchmarks/smoke_30_*.json`, `benchmarks/t0_pull_order.json`,
  `benchmarks/v1_pull_order.json`, `benchmarks/openhands/cal20_live_lite/*.{json,jsonl,txt}`
- observation: These data files contain SWE-bench-Live task identifiers that include the literal
  repository name (`aws-cloudformation__cfn-lint-NNNN`). They are legitimate benchmark task lists,
  not code, and removing them would break the eval harness. Out of RC-01 scope (data, not code).
- severity: MINOR (data, not code)
- next: no action; documented for completeness so the post-fix grep does not flag spurious
  follow-ups.

## RC-01-addendum-003 — `tools/sweagent/gt_edit/lib/gt_intel.py` is untracked in git

- discovered_during: editing the third `gt_intel.py` copy referenced by the cluster brief
- location: `tools/sweagent/gt_edit/lib/gt_intel.py`
- observation: `git status` reports the file as Untracked, even though it is referenced by the
  bundle config and is required for L3+L6 to function. RC-01 changes were applied to it for
  consistency, but the file should be tracked + committed by the L3 owner so future RC-12 work
  has a single source of truth. RC-12 cluster owns the per-bundle copy reduction; flagging here
  for that follow-up.
- severity: MINOR (functional today via on-disk copy)
- next: RC-12 to reconcile / commit the bundle copy.
