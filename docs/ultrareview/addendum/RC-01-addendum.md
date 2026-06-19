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

## RC-01-addendum-004 — RC-01 deltas absorbed into upstream `RC-04`/`RC-06` commits during parallel Phase-3 fix runs

- discovered_during: post-fix `git status` after concurrent agents landed RC-04 (SQLite
  correctness), RC-06 (language-agnostic L5 + identifier extraction), RC-09 (submission
  pipeline), and RC-15 (performance) on the same branch
- location: HEAD of `opensource-experimentation`
- observation: Six of the seven files this fix sketch enumerated (`benchmarks/swebench/gt_intel.py`,
  `tools/sweagent/gt_navigate/lib/gt_navigate.py`, `tools/sweagent/gt_pre_finish_gate/lib/gt_pre_finish_gate.py`,
  `scripts/swebench/gt_track4_pre_run.py`, `tools/sweagent/gt_query/config.yaml`,
  `tools/sweagent/gt_pre_finish_gate/config.yaml`) carry the RC-01 deltas in HEAD already, but
  the commits authoring them are titled `RC-04` / `RC-06` because the index was concurrently
  shared. Verifications:
    * `_NOISE_WORDS` no longer contains literal repo names anywhere
    * `_high_freq_repo_identifiers(conn)` exists in both `gt_navigate.py` and `gt_intel.py`
      with meta-table-then-live-top-1% lookup order
    * `_blast_radius_threshold(conn)` derives the gate threshold from per-repo P95 with the
      legacy 20 floor only as a fallback
    * `SCRATCH_PATTERNS_DEFAULT` keeps language-level prefixes; the agent-fingerprint set
      (`test_`, `debug_`, `comprehensive_test`, `test_case`, `_debug`) is OPT-IN via env
    * `check_scratch_files` scans `.`, `tests/`, `test/`, `src/` and whitelists files that
      already existed at HEAD
    * `_pick_bootstrap_symbol(issue_text, graph_db_path)` validates against graph.db and
      returns `""` on miss; `config/gt_track4.yaml` wraps the directive in
      `{% if gt_bootstrap_symbol %}…{% endif %}`
    * `tools/sweagent/gt_query/config.yaml` example is `parse_date`
    * Grep for `cfn-lint`, `CloudFormation`, `snapstartsupported`, `SnapStart` returns zero
      matches in `tools/sweagent/`, `benchmarks/swebench/gt_intel.py`,
      `scripts/swebench/gt_track4_pre_run.py`, and `config/`
- severity: MINOR (work is in HEAD; only the commit title is split across siblings)
- next: this RC-01 commit records the audit trail and the integration-check script.
