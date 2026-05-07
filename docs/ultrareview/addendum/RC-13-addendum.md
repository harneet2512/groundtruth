# RC-13 addendum — bugs discovered while applying the fix

## ADD-RC-13-1 — Dropping the `_REPO_ROOT_HOST/src` candidate breaks the synthetic-test path on Windows when the dev forgets `GT_GROUNDTRUTH_SRC`

**File**: `tools/sweagent/gt_edit/lib/gt_edit_state.py:80-94`.

**What**: RC-13 (a) pruned the hardcoded `/home/ubuntu/Groundtruth/src` and `/root/Groundtruth/src` fallbacks from `_SRC_CANDIDATES`. The container path is unaffected (it uses the git-diff detector, never imports `groundtruth.edit_predicates`). On the Windows dev box, the chain still works because `_REPO_ROOT_HOST / "src"` is computed from `__file__.parents[4]`. But if a future contributor moves the bundle out of the repo tree on Windows AND forgets to set `GT_GROUNDTRUTH_SRC`, the synthetic tests fail with a silent `extract_edited_path = None` instead of a clean ImportError. Logging the unresolved-import condition would be a one-line follow-up.

**Severity**: MINOR. Synthetic-test path only; container path is unaffected.

**Why not fixed in RC-13**: the fix surface is the gt_edit_state module init block; adding logging there means choosing a destination (stderr? `/tmp/gt_edit_state_init.log`?) that is opinionated. Out of the 80 LoC budget. Recommend: emit a single-line `gt_edit_state_init.log` entry tagged `event="src_candidates_unresolved"` when ALL candidates miss.

## ADD-RC-13-2 — `_assert_no_duplicate_submit` is a string-path approximation, not a true SWE-agent validator

**File**: `scripts/swebench/swe_agent_smoke_runner.py:_assert_no_duplicate_submit`.

**What**: The assertion checks whether the YAML's bundles list contains BOTH `tools/registry` AND a path with `review_on_submit_m`. That covers the documented crash mode in `config/gt_track4.yaml:154-160`. It does NOT cover the more general case where some FUTURE bundle (or a SWE-agent-internal default registry) declares `submit` in its own `tools.yaml`. A robust version would parse each bundle's `tools.yaml` and accumulate every bundle that declares `submit`, then fail if `>1` non-`gt_pre_finish_gate` bundle does. That is ~60 LoC and a YAML walker per bundle path — outside the RC-13 budget.

**Severity**: MINOR. The string-path check matches every observed crash to date; the deeper walker is a "future SWE-agent rev" hedge. Recommend: keep this check, add a TODO inline pointing at the deeper variant.

**Repro signal**: this assertion will continue passing for any bundle list that follows the current Track 4 convention. It will only miss if a third-party bundle starts declaring `submit` — at which point the SWE-agent validator itself catches the duplicate at config-load time, just less helpfully than this preflight.

## ADD-RC-13-3 — `_probe_binary_loadable` cache is process-local, not cross-call

**File**: `tools/sweagent/gt_edit/lib/gt_edit_state.py:_LOADER_PROBE_CACHE`.

**What**: The probe caches its verdict in a module-level dict so repeated state-command calls in the same process don't re-run `ldd`. SWE-agent's state-command wiring spawns a fresh Python process per call (`gt_edit_state.py` is the entry point of `bin/_state_gt_edit`, not a long-lived daemon), so the cache is hit ZERO times in production. Each state call pays the full `ldd` cost (~5-15ms on a hot binary, longer on first-touch).

**Severity**: MINOR. ldd is ~15ms; SWE-agent state calls are <100/task, so we burn at most 1.5s/task on this. Not worth a disk cache for now; revisit if state-call latency becomes a bottleneck.

**Recommended fix (out of scope for RC-13)**: persist the verdict to `/tmp/gt_loader_probe_<sha256>.json` keyed on the binary's SHA-256, so subsequent state calls in the same container short-circuit. Trivially correct because the binary is identical across calls; risky only if the operator hot-swaps the bundle binary mid-run, which would be a deliberate dev action.

## Notes on what was checked but is NOT a bug

- `scripts/build_gt_index_linux.sh` is intentionally a stub on non-Linux hosts. It exits 2 with a `TODO(RC-13-build)` line directing the user to run it on a Linux VM. RC-13 instructions explicitly call this out — the actual binary rebuild needs Linux + Go toolchain and CANNOT be done from the Windows dev box.
- The integration check `RC-13.sh` does NOT launch SWE-agent; it only exercises local probes. This is by design — the user-facing behavior is "smoke runner refuses to launch on a fresh VM with the wrong defaults", and that's testable locally.
- The `--vm-profile` flag is OPT-IN. Existing scripts that pass `--venv-python /home/ubuntu/sweagent_venv/bin/python --gt-indexes-root /home/ubuntu/eval_indexes` still work; the explicit flag values win over any profile. The only behavior change for existing callers is: they MUST now pass at least one of `--vm-profile` or both explicit flags, because the silent default was removed.
- `_VM_PROFILES` carries `swe_repo` for git-safe-directory check only — the SWE-agent CLI itself receives `venv_python` (the binary that the runner invokes), not the repo path. So a missing `swe_repo` in a profile only suppresses the safe-directory check, never breaks the launch.
