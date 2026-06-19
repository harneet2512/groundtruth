# Artifact Capture — FINAL_ARCH_V2 graph-backed replay

Status: 2026-05-17, plumbing only. No task batch run.

## Why

The shadow replay (`scripts/shadow_replay.py`) needs a matched pair per task:

- `output.jsonl` — agent trajectory written by OpenHands
- `graph.db`     — the GT graph used during that run
- `gt_interactions.jsonl` — structured GT layer events (already collected)
- `gt_hooks.log` — hook fire/skip log (already collected)

Without the per-task `graph.db`, every provider call returns empty and the
router classifies the miss as `NO_GRAPH_DB`. That blocks any meaningful
shadow-replay branch-coverage check.

## What changed

`scripts/swebench/oh_gt_full_wrapper.py` now stages `graph.db` next to
`output.jsonl` whenever the wrapper has a host-side copy of the graph
(`config._host_graph_db`, populated at the existing LSP-verify code path):

- New helper `_pull_graph_db_artifact(config)`:
  - Source: `config._host_graph_db` (already populated by the wrapper).
  - Destination: `$GT_ARTIFACT_DIR/graph.db`, falling back to
    `$EVAL_OUTPUT_DIR/graph.db` then `$OUT_ROOT/graph.db`.
  - Logs `[GT_ARTIFACT] graph.db -> <path> (<bytes>)` on success.
- Called from the two task-end paths:
  - max-iteration exit (`config.action_count > config.max_iter`)
  - regular `finish`/`AgentFinishAction` handler

The helper is best-effort: failures only print a warning and do not affect
the agent loop.

## GHA / runner contract

The eval workflow already collects everything under `--eval-output-dir`
(passed to the wrapper as `args.eval_output_dir`). Set ONE of these env
vars on the runner so `_pull_graph_db_artifact` knows where to write:

- `GT_ARTIFACT_DIR` — preferred; explicit
- `EVAL_OUTPUT_DIR`
- `OUT_ROOT`

When the workflow already passes `--eval-output-dir <dir>`, set
`GT_ARTIFACT_DIR=<dir>` alongside it. The wrapper copies graph.db into
that directory; the existing artifact-upload step picks up `graph.db`
alongside `output.jsonl`.

## What is NOT changed

- No new task batch.
- No new model calls.
- No new container builds.
- The wrapper's live `graph_navigation` / `generate_improved_evidence`
  paths are untouched.
- `gt_interactions.jsonl` / `gt_hooks.log` retrieval is unchanged.

## Sanity check (offline)

After a future run completes, the per-task output dir should contain:

```
.../<task>/output.jsonl
.../<task>/graph.db                  # NEW — wired this pass
.../<task>/gt_hooks.log              # existing (already in instance_ref)
.../<task>/gt_interactions.jsonl     # existing
```

Then:

```
python scripts/shadow_replay.py \
  --outputs '<dir>/<task>/output.jsonl' \
  --graph-map <map.json>                            # or --graph-dir <root>
```

should report `graph_resolved=N` matching the task count, and the
suppression distribution should show real branches (DUPLICATE, STALE,
TOO_LATE, NO_EVIDENCE, DEBOUNCE, BUDGET) — not all NO_GRAPH_DB.

## Stop condition

This change is plumbing. No `5/10/15/30`-task run is needed to validate it.
The replay-fixture path (`scripts/build_replay_fixture.py` →
`reports/shadow_replay/v2_fixture_replay.json`) already demonstrates that
graph-backed replay exercises the EMIT / DUPLICATE / NO_EVIDENCE / TOO_LATE
branches end-to-end.
