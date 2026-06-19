# GT vNext VM Dry-Run — 13453 Event Brief Proof

**Date:** 2026-04-25
**Task:** astropy__astropy-13453
**Status:** PARTIAL_GATE_PASS — event_brief only

---

## Proven

| Check | Result |
|---|---|
| GT_VNEXT=1 in container | YES (trace.log) |
| gt_vnext_enabled in state | TRUE |
| event_brief_called | TRUE |
| event_brief_findings_count | 8 |
| event_brief_suppressed (novelty) | 1 |
| vnext_event_brief fires | 4 times across 47 steps |
| Surface-tagged deliveries | 3 (steps 16, 17, 18) |
| Agent could respond | YES (29 steps after first delivery) |

## Sample delivery (step 16)

```
<gt-evidence surface="event_brief">
[VERIFIED] [caller_expectation] called as: table = read(DATA, Reader=Ipac) @ astropy/io/ascii/tests/test_ipac_definitions.py:22 (0.95) — VERIFY
[VERIFIED] [caller_expectation] called as: table = read(DATA, Reader=Ipac, definition='ignore') @ astropy/io/ascii/tests/test_ipac_definitions.py:28 (0.95) — VERIFY
[VERIFIED] [caller_expectation] called as: table = read(DATA, Reader=Ipac, definition='left') @ astropy/io/ascii/tests/test_ipac_definitions.py:34 (0.95) — VERIFY
[WARNING] [import_path] signature: _check_multidim_table @ astropy/io/ascii/core.py:43 (0.70) — VERIFY
[WARNING] [import_path] signature: isinstance @ astropy/table/table.py:308 (0.70) — VERIFY
[WARNING] [caller_contract] 13 callers in 4 files @ astropy/io/ascii/html.py (0.70) — VERIFY
[INFO] [test_assertion] test function references read @ astropy/io/ascii/tests/test_compressed.py:14 (0.55) — VERIFY
[INFO] [test_assertion] test function references read @ astropy/io/ascii/tests/test_compressed.py:23 (0.55) — VERIFY
</gt-evidence>
```

## Not proven

| Surface | Status | Root cause | Fix needed |
|---|---|---|---|
| task_map | Subprocess called, empty stdout | `vnext_briefing_error=1`: gt_intel_real.py crashes inside container. First run: `sqlite3.OperationalError: database is locked` (fixed with WAL). Subsequent runs: different Python exception (caught, no stderr). | Debug gt_intel_real.py `--enhanced-briefing --findings-json` path inside container — likely import error or missing function |
| review_patch | Submit tool patched but findings not emitted | The submit tool runs review_patch code but output doesn't appear in trajectory. Either the code silently fails (try/except), or `gt_intel_real.py` path doesn't exist in the submit tool's context. | Add error output to submit tool's review_patch block. The state hook CAN'T detect submit (timing: hook runs before submit tool). |

## Timing issue (review_patch)

The SWE-agent state hook runs BEFORE tool execution in each cycle:
1. State hook reads state.json → runs swe_agent_state_gt.py → writes state.json
2. Agent tool runs (submit, bash, etc.)
3. Tool output becomes observation

So `_is_presubmit(state)` in the state hook can NEVER see the submit because it hasn't happened yet. The submit tool injection (`patch_submit_tool.py`) is the correct approach, but the injected code fails silently inside the container.

## Root cause of prior failures

`install.sh` copies files from `$BUNDLE_DIR/bin/`, not the bundle root.
Updated files were at `tools/groundtruth/swe_agent_state_gt.py` but
install.sh reads from `tools/groundtruth/bin/swe_agent_state_gt.py`.
Fixed by copying to both locations.

## Run details

- Commit: d4ccdcb
- VM: gt-runner-gcp (n2-standard-16)
- Config: canary_gt_vnext_qwen.yaml (GT_VNEXT=1 in env_variables)
- Model: qwen3-coder-480b-a35b-instruct-maas via LiteLLM proxy
- Steps: 47
- Patch: YES (5 +/- lines)
- Exit: submitted
