# STAGE 4.1 — GT Container-Runtime Strategy Decision

> Resolves the architecture contradiction Stage 4 surfaced. Branch `gt-trial`. No GHA runs.

## The goal (restated — not "host mode" for its own sake)

- The host runner **only orchestrates**: resolve SHA · pull/cache images · start containers ·
  `docker exec` · copy artifacts · upload artifacts.
- GT runtime **NEVER executes on the host** in proof/final mode: graph build, FTS5, LSP, closure,
  `run_v74`/brief/scoring, embedder, foundational gates, hook graph reads — **all in-container**.
- **ONE proof path** — same `foundational_gates.py`, same `resolve.py`, same graph/LSP/embedder
  **certificates**, same **artifact layout**, consumed by OH hooks from the same place.
- `gates_only` and live share the same prefix; OH hooks read the **same post-LSP graph hash**.

## Decision

**Option B — Unified GT substrate/sidecar runtime — is the chosen final design.** Option A
(provision GT into each task image) is retained only as a *fallback*, valid **only if Stage B
empirically proves** it is deterministic and cheap (no network failures, acceptable per-task
overhead, no host GT execution, no host-primary brief, all certs emitted in-container).

| Decision | Option A: provision task image | **Option B: unified substrate (CHOSEN)** |
|---|---|---|
| GT deps baked? | no (pip/copy per task) | **yes (baked in the GT runtime image)** |
| per-task network? | yes (pip pyright/onnxruntime) | **no** |
| same task container? | yes | no — **shared source/artifacts volume** |
| host GT execution possible? | **must fail-closed** | **must fail-closed** |
| proof-path divergence risk | medium/high | **low (if it runs the same gates/certs)** |
| 300-run risk | high unless Stage-B-proven | **lower** |
| recommendation | only if Stage B proves stable | **preferred** |

### Option B definition (what "unified" means — and its validity conditions)

The official SWE task image stays the task/OH execution environment. A **baked GT runtime
container** (the substrate image: GT package + node/pyright + onnxruntime + e5 model) mounts/shares
the task repo source and runs graph build · FTS5 · LSP · closure · embedder · **the same
`foundational_gates.py`** · certificates, writing graph + certs to a **shared volume**. OH/task
hooks consume the resolved graph + certs from that volume. The host only orchestrates.

**Option B is valid ONLY if it uses the EXACT same:** `foundational_gates.py`, `resolve.py`, graph
certificate, embedder certificate, LSP certificate, artifact layout — `gates_only` and live share
the prefix — OH hooks read the same post-LSP graph hash — **no separate substrate-only gate logic**
(the legacy `gt-substrate-run.sh` shell gate) — **no host GT execution**.

## The rule change (replaces "substrate forbidden globally")

- `LEGACY_DIVERGENT_SUBSTRATE_FORBIDDEN` — the old `gt-substrate-run.sh` shell gate (divergent
  code) is forbidden.
- `UNIFIED_GT_SUBSTRATE_OK` / `UNIFIED_CONTAINER_RUNTIME_REQUIRED` — a substrate runtime that runs
  the SAME `foundational_gates.py` + `resolve.py` + certs IN a container is allowed/required.
- `HOST_GT_EXEC_FORBIDDEN` — any GT execution on the host in proof mode fails-closed.

Implemented as `context.classify_runtime_strategy(gate_module, in_container, proof)` (verdicts
above) + the workflow guard wording updated from `SUBSTRATE_PROOF_PATH_FORBIDDEN` to
`LEGACY_DIVERGENT_SUBSTRATE_FORBIDDEN`.

## The host-primary-brief proof leak — CLOSED

Stage 4 set `GT_PROOF_MODE=1` on the agent step, so the wrapper's host-primary brief ran `run_v74`
on the host runner = host GT execution in proof = a boundary violation. **Fixed:** `run_v74` now
calls `context.assert_container_boundary("run_v74/brief/scoring")` at entry → host `run_v74` in
proof fails-closed `FINAL_PIPELINE_HOST_SPLIT_FAIL` (inert outside proof). The brief must be
generated in-container (where the gates already invoke `run_v74`). Tested:
`test_run_v74_forbids_host_in_proof`, `test_run_v74_guard_inert_outside_proof`.

## Does Stage 4 need rework?

**Yes — partial.** The runtime *guarantees* (boundary asserts, in-container LSP+gates moves,
forbid-divergent, certs) are correct and kept. But Stage 4's **W1 per-task provisioning of `gtsrc`
is Option A** — deprecated in favor of Option B. The follow-up (Stage 4.2 / revised Stage 4) wires
the **unified substrate runtime** (baked GT image + shared source/volume + the same
`foundational_gates.py`), and the per-task `pip install`/model-copy block is removed once B runs.
The Stage 4 commit (`b88beeec`) is preserved as escalation evidence; it is **not the final path**.

## External benchmark-team run contract (Stage 4.2 — portable)

The GT proof runtime is a **published, pinned image** with ONE entrypoint (`gt-run-proof`). An
external benchmark team needs Docker and nothing else — no checkout, no pip, no model download, no
private state.

**Required image digest:** `ghcr.io/<org>/groundtruth-substrate@sha256:<DIGEST>` (pin by digest in
proof/final mode — a tag is NOT acceptable). The digest is published with the cache build.

**Required inputs:**
- `-v "$TASK_REPO:/work:ro"` — the task repo, **read-only** (never mutated).
- `-v "$GT_ARTIFACTS:/gt_artifacts"` — a writable output directory.
- env: `GT_PROOF_MODE=1 GT_CONTAINERIZED=1 GT_RUNTIME_STRATEGY=unified_substrate GT_REQUIRE_FTS5=1
  GT_REQUIRE_EMBEDDER=1 GT_FORCE_ONNX_EMBEDDER=1 GT_REQUIRE_LSP=1 GT_REQUIRE_FULL_STACK=1`.

**Exact command:**
```bash
docker pull ghcr.io/<org>/groundtruth-substrate@sha256:<DIGEST>
docker run --rm \
  -v "$TASK_REPO:/work:ro" -v "$GT_ARTIFACTS:/gt_artifacts" \
  -e GT_PROOF_MODE=1 -e GT_CONTAINERIZED=1 -e GT_RUNTIME_STRATEGY=unified_substrate \
  -e GT_REQUIRE_FTS5=1 -e GT_REQUIRE_EMBEDDER=1 -e GT_FORCE_ONNX_EMBEDDER=1 \
  -e GT_REQUIRE_LSP=1 -e GT_REQUIRE_FULL_STACK=1 \
  ghcr.io/<org>/groundtruth-substrate@sha256:<DIGEST> \
  gt-run-proof --source-root /work --out /gt_artifacts
# discover the contract without running: `gt-run-proof --print-contract`
```

**Expected outputs** (under `$GT_ARTIFACTS`): `graph.db`, `runtime_context.json`,
`lsp_certificate.json`, `graph_certificate.json`, `embedder_certificate.json`,
`foundational_gate_report.json`, `run_manifest.json` (+ `brief/` render artifacts if applicable).

**Agent integration** (the OH/task container consumes artifacts only — it does NOT recreate GT):
```bash
-v "$GT_ARTIFACTS:/gt_artifacts:ro"
-e GT_HOST_GRAPH_DB=/gt_artifacts/graph.db -e GT_CERT_DIR=/gt_artifacts
-e GT_PROOF_MODE=1 -e GT_CONTAINERIZED=1
# wrapper logs: [GT_META] host_resolved_graph_db=/gt_artifacts/graph.db
#               hook_graph_hash=<same post-LSP hash>  _gt_prebuilt_active=True
```

**Failure classifications:** `FINAL_PIPELINE_HOST_SPLIT_FAIL` (GT on host / not containerized) ·
`SUBSTRATE_NOT_PORTABLE` (a baked dep missing — never pip/download per task) ·
`SUBSTRATE_MISSING_CERTS` (a required artifact absent) ·
`LEGACY_DIVERGENT_SUBSTRATE_FORBIDDEN` (the old shell gate) · gate verdicts (LSP/graph/embedder).

**What the benchmark team must trust / need NOT trust:** they trust the pinned digest (the baked GT
runtime) and the artifact contract. They do **not** need: our GHA plumbing, a checkout, network at
run time, a model server, or any gold/FAIL_TO_PASS/test-name data (none is used — the substrate sees
only the repo source + the issue text).

## Go/No-Go

- Stage 4 = **escalation-revealing checkpoint** (accepted as such, not as final).
- Stage 5 is **NOT safe to begin** until the Option-B unified-substrate runtime is wired (or
  Stage B proves Option A stable) — image-cache work presupposes the final runtime path.
