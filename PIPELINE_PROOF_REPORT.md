# PIPELINE_PROOF_REPORT — Stage 4 (run LSP + gates inside the eval container)

> Stage 4 of the full-runtime proof. Branch `gt-trial`. Scope: kill the host/image split — in
> proof/final mode the host runner only orchestrates; GT (graph build, FTS5, LSP, closure,
> embedder/scoring, foundational gates, certificate emission, agent hook graph reads) executes
> INSIDE the eval container. No image-cache / GHCR-manifest / task-manifest / run_v74-semantic /
> ranking-weight / task-specific changes.

## The load-bearing guarantee (runtime, locally proven)

`context.assert_container_boundary` (new) + `foundational_gates.main` (wired) **fail-closed
`FINAL_PIPELINE_HOST_SPLIT_FAIL`** whenever GT runs on the host in proof mode (`GT_CONTAINERIZED`
unset, or cgroup/`.dockerenv` say host). This is enforced at the runtime level, so **the pipeline
can never *silently* host-split even if in-container provisioning is imperfect** — a host run
aborts; an in-container run (cgroup=docker + `GT_CONTAINERIZED=1`) passes. Verified locally:
inert outside proof; raises on host+proof; raises even with the flag set on a host runner (the
cgroup check). `tests/fail_closed/test_container_lockdown.py` covers it.

## Workflow restructure (host/image split killed) — GHA-proven (Stage B)

Host-mode Point A (`swebench_300task.yml`):
- `gtsrc` (the task eval container) is now **kept alive** (was `docker rm -f` right after copy-out)
  and the GT runtime is provisioned into it (`docker cp` the package + scripts + e5 model;
  `pip install onnxruntime tokenizers numpy pyright`).
- **LSP** runs `docker exec gtsrc … python -m groundtruth.resolve …` on the in-container graph
  (`/tmp/graph.db`) + the real in-container source (`$ROOT`), with all 8 proof flags +
  `GT_CONTAINERIZED=1` + `GT_LSP_CERT`. Closure rebuilt in-container.
- **Foundational gates** run `docker exec gtsrc … python3 /opt/gt/scripts/metrics/foundational_gates.py …`
  (same flags). The certs (`lsp_/graph_/embedder_certificate.json`) + gate deep-metrics are
  `docker cp`'d OUT; `gtsrc` is removed after.
- The resolved graph is copied out to `/tmp/gt/graph.db` and handed to the agent (the wrapper
  uploads it into the agent container — the Stage-2 handoff).
- **Substrate proof path FORBIDDEN**: the `gt_use_substrate_image=true` step now fails-closed
  `SUBSTRATE_PROOF_PATH_FORBIDDEN` (it runs the divergent `gt-substrate-run.sh` gate). One runtime,
  one gate path (`foundational_gates.py`), in-container.
- The **agent step** receives `GT_PROOF_MODE=1` + `GT_CONTAINERIZED=1` + the cert/graph env
  (inherited via `$GITHUB_ENV`) for hook validation.
- The certificates upload `if: always()`.

## Tests — `tests/fail_closed/test_container_lockdown.py` (12, all pass)

Runtime: boundary inert-outside-proof · raises on host+proof · raises with flag-but-host-cgroup ·
`foundational_gates.main` returns 1 on host+proof. Workflow structure: YAML parses · LSP runs via
`docker exec` (old host invocation gone) · gates run via `docker exec /opt/gt/...` (old host
invocation gone) · all 8 proof flags present on the execs · substrate forbidden · certs collected
+ uploaded · agent has proof env · `gtsrc` kept-alive + provisioned. Full fail-closed 121/121
(incl. `workflow_lint` 22/22).

## ESCALATION — structural tension surfaced (read before the Stage-B run)

**Running GT's LSP+gates inside an arbitrary task image requires provisioning the GT runtime
(package + node/pyright + onnxruntime + the e5 model) into it at runtime — which is exactly what
the substrate image was built to BAKE.** "Host-mode-only + forbid substrate" and "GT executes
in-container" are in genuine tension, resolved here by provisioning the host path's `gtsrc`:
- `pip install pyright` (PyPI; bundles/downloads node) + `onnxruntime`/`tokenizers`/`numpy`, and
  `docker cp` of the package + ~130MB e5 model, **per task**. Feasibility (network + pip on each
  task image; cost ~1–2 min/task) is **GHA-proven in Stage B**, not locally verifiable.
- **If provisioning fails, the run fails-closed** (the runtime boundary assertion + the 8 proof
  flags) — it never silently reverts to host execution. That is the design guarantee.
- **Secondary risk:** the agent step now sets `GT_PROOF_MODE=1`, so the host-primary brief
  (`run_v74`) runs in proof mode on the host runner. If the host lacks the embedder stack this
  could surface; the `reject_host_aliases` softening keeps the `GT_HOST_*` handoff legitimate.
  Surfaced for the Stage-B run.

Recommendation: Stage B (1-task official run) is the first proof of the in-container provisioning +
the agent proof-env; if provisioning proves too costly/fragile per-task, the clean alternative is
to make the SUBSTRATE image the single non-divergent path (it bakes GT) rather than provisioning
per task — a decision to revisit with Stage-B evidence.

## Live data status

The in-container execution, certificate collection, and the agent proof-env are proven by the
official workflow in Phase 6 **Stage B (1-task)** then **Stage C (held-out-10)**. This stage
proves the runtime boundary locally and restructures the workflow; it does not yet prove the
in-container provisioning end-to-end (that is Stage B).
