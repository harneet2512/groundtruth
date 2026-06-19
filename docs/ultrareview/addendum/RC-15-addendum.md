# RC-15 addendum — bugs and coordination items observed while shipping the perf cluster

## ADD-RC-15-1 — `partial_pull` field schema overlaps with RC-10's gt_layers.log writer

**Files**:
- `scripts/swebench/gt_track4_pre_run.py` — RC-15 added a `pull_failures: list[str]`
  and reaffirmed `partial_pull: bool` in the summary dict returned by
  `_pull_gt_artifacts`. RC-10 had already introduced `pull_attempted /
  pull_succeeded / pull_failures / partial_pull` in the same dict; the two
  changes converged cleanly here.
- The downstream consumer is the per-task `gt_layers.log` line written by
  `_wrap_env_close_with_artifact_pull`. RC-10 owns the canonical schema for
  that line.

**What**: RC-15 wires `partial_pull` and per-artifact failure strings into
the summary returned to the close-wrap path, and assumes the existing
gt_layers.log line already includes a `partial_pull=` token (RC-10 work).
Marked inline as `# TODO(RC-15-coord)` so RC-10's owner can verify the
field shape and extend the schema if needed.

**Severity**: LOW — the data is correctly produced by `_pull_gt_artifacts`
and the verifier path that reads `pull_failures` cannot regress silently
because RC-15 unit tests cover the retry classification end-to-end. The
risk is purely that a future schema bump in RC-10 lands without updating
the RC-15 producer, splitting the contract.

**Recommended follow-up**: when RC-10 finalises the line schema, replace
the `TODO(RC-15-coord)` markers with the canonical column names and add a
schema-shape assertion in `tests/unit/test_rc15_performance.py` so a drift
fails a unit test, not a 300-task run.

## ADD-RC-15-2 — `verify_report.py` has additional `read_text().splitlines()` sites outside `_compute_kernel_gates`

**File**: `scripts/swebench/verify_report.py`.

**What**: The fix targeted by the cluster is the kernel-gates compute
path (lines 325, 342, 370 in the pre-fix file). Greppable inspection of
the rest of `verify_report.py` shows the rest of the file reads
human-bounded artifacts (gt_arm_summary.json, gt_report.csv,
killed_tasks.jsonl) whose absolute size is small. No further streaming
work is required for the n=300 gate, but a 1000-task gate would benefit
from a streaming pass on the CSV loader if that loader ever consumes a
JSONL twin.

**Severity**: LOW — out of scope for RC-15.

**Recommended follow-up**: when the harness scales past 300 tasks, audit
`scripts/swebench/verify_report.py` again for `read_text` calls and
convert any that point at run-scale artifacts.

## ADD-RC-15-3 — gt_index `GOMAXPROCS` cap is per-invocation, not pool-wide

**File**: `tools/sweagent/gt_edit/lib/gt_edit_state.py`.

**What**: RC-15 sets `GOMAXPROCS=2` in the subprocess env for each
gt-index call inside the state command. This caps a single invocation
but does not enforce a pool-wide cap when N concurrent state commands
fire simultaneously across a 4-vCPU VM. The cluster's design intent
(item h in the BUG_GRAPH fix sketch) is that 2 × concurrent_workers
should not exceed `nproc`. The full fix lives in the smoke runner's
worker-pool sizing, not here.

**Severity**: LOW. Per-invocation `GOMAXPROCS=2` materially reduces
contention from 8-NumCPU-saturating bursts to 2-NumCPU bursts, which is
enough at the n≤30 / 4-vCPU operating point. The pool-wide invariant is
the smoke runner's responsibility (RC-03 / RC-15-coord-2).

**Recommended follow-up**: the smoke runner enforces
`workers <= nproc // 2` as a preflight assertion when the GT_EDIT layer
is enabled.

## Notes on what was checked but is NOT a bug

- The `time` and `threading` imports in `scripts/swebench/gt_track4_pre_run.py`
  are top-level (lines 56–57), so the new `_run_async_safely` and
  `_read_file_with_retry` helpers can use them without additional imports.
- The legacy `_ensure_graph_db_built` symbol was renamed to
  `_resolve_graph_db_no_build`; nothing in the repo imports the old name
  except the function's own call site at `main()`, which RC-15 updates.
  Greppable confirmation: `grep -R _ensure_graph_db_built` returns zero
  hits after the patch.
- `_files_with_symbol` in `gt_navigate.py` returned a `set`, so the prior
  non-determinism was masked at the API boundary but not at the LIMIT
  boundary (when the result set exceeded 50 items, two consecutive runs
  could return different 50-item subsets). Adding `ORDER BY file_path ASC`
  closes that gap. The `set` return type is preserved.
