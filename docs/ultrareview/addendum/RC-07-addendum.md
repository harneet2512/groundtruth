# RC-07 addendum — bugs discovered while applying the fix

## ADD-RC-07-1 — Remote-host branch silently drops both GT_GRAPH_DB AND GT_INDEXES_ROOT

**File**: `scripts/swebench/swe_agent_smoke_runner.py` (the `else:` branch around the env build, ~line 985 after RC-07 fix).

**What**: When `--remote-host` is set, the runner wraps the launch in `gcloud ssh -- bash -lc <inner>`. Local `env=...` does not cross that SSH boundary. The pre-existing code already documented this for `GT_GRAPH_DB`; the RC-07 fix expands the same gap to `GT_INDEXES_ROOT`. Marked inline as `TODO(RC-07-coord)`.

**Severity**: MAJOR for any remote-host multi-task batch. The pre-run hook on the remote VM will fall back to whatever `GT_INDEXES_ROOT` is in that VM's shell env (often empty), then fall back to `GT_GRAPH_DB` (also empty in the inner bash unless set there), then emit "no graph.db resolved" and skip every brief. From the operator's view this looks like the brief layer is "broken" when in fact env propagation never happened.

**Why not fixed in RC-07**: out of scope — remote-host wiring is its own subsystem (`_wrap_for_remote`) and the fix surface there is more invasive than the 5–15 LoC RC-07 budget. Recommend a follow-up cluster (RC-07-coord) that prepends `export GT_INDEXES_ROOT=...; export GT_GRAPH_DB=...;` to the inner bash of `_wrap_for_remote`, gated on a `--remote-export-gt-env` flag so we don't accidentally clobber a remote-side override.

**Repro signal**: any 2026-05-xx run logged with `--remote-host` AND `len(task_ids) > 1` will show `Track 4 pre-run hook: no graph.db resolved` for every task in `gt_layers.log`. Easy grep gate before assuming the fix landed everywhere.

## ADD-RC-07-2 — Per-task graph.db missing warning is non-fatal but produces silently corrupted briefs

**File**: same, the `for tid in task_ids:` loop printing `WARN: graph.db missing for {tid}`.

**What**: If a subset of task ids have no prebuilt index under `GT_INDEXES_ROOT`, the runner prints a warning and proceeds. The pre-run hook then emits "Track 4 pre-run hook: no graph.db resolved … Skipping brief" for those tasks — empty brief, agent runs without GT signal — while the run-level metrics (delivery_rate, engagement_rate) compute over ALL tasks including the brief-less ones, dragging the rate floor below the verify_report.py gates without flagging the cause.

**Severity**: MODERATE. Not strictly a correctness bug (the pre-run hook fails closed), but a metrics-attribution bug: a partially-indexed batch looks like "GT engagement broken" instead of "operator forgot to build N indexes."

**Recommended fix (out of scope for RC-07)**: in the smoke runner, count missing-index tasks; if the missing fraction exceeds a threshold (say 10%) AND `--allow-partial-indexes` is not set, fail preflight rather than warn. Belongs in its own cluster, not RC-07.

## Notes on what was checked but is NOT a bug

- `gt_track4_pre_run.py:1219` — the `os.environ.get("GT_INDEXES_ROOT", "")` read uses the right variable name. Per-task resolution at `os.path.join(indexes_root, instance_id, "graph.db")` is correct. No coordinated edit needed in that file for RC-07.
- The default value of `--gt-indexes-root` is `/home/ubuntu/eval_indexes` (non-empty), so the new preflight assertion only fires if a caller explicitly passes `--gt-indexes-root ""`. This is the right defensive posture: the assertion exists for the operator-error case, not as a routine gate.
