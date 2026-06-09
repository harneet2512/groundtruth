# GO / NO-GO — DeepSWE GT integration (consolidated from the 5-phase grounded audit)

> Branch `gt-trial`. Gates whether DeepSWE GT runs may launch. Detail: `docs/DEEPSWE_GT_*` + the 5
> per-stage audits (file:line grounded). **DeepSWE integration is NOT complete.**

## Verdict
| Run type | Status | Reason |
|---|---|---|
| DeepSWE local doc audit | **YES** | 5 docs + 5 phased audits complete |
| DeepSWE 1-task dry/proof run | **NO** | substrate digest unpublished; pyright+e5-only baked; adapter draft has dual-graph + fail-open witness + launcher-env holes |
| DeepSWE held-out 10 | **NO** | depends on D1+D2 |
| DeepSWE paid/full run | **NO** | depends on D0–D3 + frozen baseline + provenance + failure-classification |

## What the audit PROVED is correct (REUSE as-is — do NOT touch)
- **GT contract code is clean across all 5 stages:** LSP cert + REAL warm probe (`resolve.py:558-568`) + dispatch + `residual==0` no-fake-pass + stamp preservation (Stage 1); canonical `graph_edges_hash` byte-identical across 3 emitters + **`-rebuild-closure` PRESERVES `'lsp'` stamps** (`main.go:95-121`) → no `LSP_STAMP_DROPPED` on the substrate path (Stage 2); embedder classifier + proof asserts + language-agnostic candidate production, all fail-closed (Stage 3); `assert_container_boundary` + `classify_runtime_strategy` fail-closed + exact 8-flag `@sha256` docker-run shape (Stage 4).
- **The 113-image GHCR cache EXISTS** (Stage 5: run `26994060868` 113/113; 7-sample HTTP-200 all 5 langs).

## Blockers (grounded, prioritized) — the GT *contract* is clean; the gaps are SUBSTRATE + ADAPTER + PIPELINE
**A. Substrate image (rebuild + publish):**
1. **Bakes pyright ONLY** → TS/JS/Go/Rust (79/113 = 70%) `LSP_INSTALL_MISSING`; the unsupported path exits 0 so `GT_REQUIRE_LSP=1` is **falsely green** on non-Python. → bake `gopls`+`rust-analyzer`+`typescript-language-server`.
2. **Bakes e5 but loader defaults gte** → 3 surfaces disagree (`validate_proof_env:68-71` wants e5; `proof.embedder_model_path:408`+`context.model_files_baked:176` want gte) → preflight fail-close OR **silent e5 substitution**. → bake gte int8 OR pin `GT_EMBED_MODEL_NAME=e5` and reconcile `validate_proof_env`.
3. **Pinned digest UNPUBLISHED** (`vars.GT_SUBSTRATE_DIGEST` unverified; workflow fail-closes if unset). → publish via `gt_substrate_image.yml`, set the repo var.

**B. DeepSWE adapter draft (the audit found MY draft is NOT clean — fix before trusting):**
4. **DUAL-GRAPH:** `gt_agent.py:131-162` (`_BUILD_GRAPH_DB`) still injects an in-container LSP-free `/tmp/graph.db` alongside the substrate-consume — wired regardless of substrate mode (`_inject_steps:346`). → remove/guard when `GT_PORTABLE_SUBSTRATE=1`.
5. **Witness FAIL-OPEN:** `gt_agent.py:566-571` only PRINTS on hook≠post-LSP hash mismatch — never raises. → fail-closed under proof.
6. **Consume path conditional on launcher env:** the whole substrate-consume hinges on `GT_HOST_GRAPH_DB`/`GT_CERT_DIR`, but `deepswe_gt_pier.yaml:152-158` does NOT set them; pier→**container** env passthrough unverified (the adapter reads host paths `/tmp/gt/*`). → the launcher must inject them; verify the execution locus (D2).
7. **`graph_certificate` import** needs a PYTHONPATH-independent guard.

**C. DeepSWE pipeline (provenance + triage):**
8. **No provenance:** GT commit SHA, DeepSWE-bench clone SHA, per-task-image digests are recorded NOWHERE on the DeepSWE path; `legitimacy.write_manifest` (exists, `legitimacy.py:443`) is wired only to OH; `deepswe_full.yml:526` copies a manifest no step produces (dead-copy). → wire `legitimacy.write_manifest` into the DeepSWE path.
9. **No failure classification:** `deepswe_outcome.py` can't separate infra/GT/agent. → add a classifier + paired-Wilcoxon before any paid claim.

## Multilingual (from the matrix)
Python `SUPPORTED_AND_CERTIFIED`; TS/JS/Go/Rust `LSP_INSTALL_MISSING` (fix = blocker 1); graph+FTS5+embedder are language-agnostic and work on all 5; semantics fail-closed. Java/C++/Ruby `UNSUPPORTED_EXPLICIT` (0 tasks).

## D0–D4
- **D0 (GHA/code map): DONE** — entrypoint, workflow, mounts, agent start, pre-agent insertion point, language split all mapped (this audit).
- **D1 (local substrate run): NO** — needs the rebuilt+published image (servers+gte); pyright/e5-only today.
- **D2 (1-task dry): NO** — adapter draft unvalidated (dual-graph/fail-open/launcher-env); env-passthrough unverified.
- **D3 (held-out 10): NO.** **D4 (larger): NO.**

## Is Stage-1 implementation safe to begin?
**Yes for the substrate image** (blockers 1–3: bake servers+gte, publish digest) — additive, generalized, no benchmark logic, unblocks 70% + the embedder. **The adapter fixes (4–7) are also safe + necessary** and small. **Do NOT launch any run until the image is rebuilt+pinned, the adapter holes are closed, and D1 passes.**

## Files to touch first (if implementation approved)
1. `docker/Dockerfile.gt-substrate` — bake gopls/rust-analyzer/typescript-language-server + gte (or pin e5); extend `validate_proof_env`. Rebuild via `gt_substrate_image.yml`; set `vars.GT_SUBSTRATE_DIGEST`.
2. `artifact_deepswe/gt_agent.py` — guard `_BUILD_GRAPH_DB` off in substrate mode (kill dual-graph); make the witness fail-closed; guard the `graph_certificate` import.
3. launcher/`deepswe_full.yml` — inject `GT_HOST_GRAPH_DB`/`GT_CERT_DIR` into the agent container; forward `GT_FORBID_PREBUILT_GRAPH`.
4. wire `legitimacy.write_manifest` into `gt_run_proof.py`/the adapter (provenance).
5. `scripts/verify/deepswe_outcome.py` — infra/GT/agent classification.

## Legitimacy (enforced)
No task edits / gold / FAIL_TO_PASS / test-name leakage / per-task or per-repo exceptions / gate-or-ranking
tuning / hidden host GT exec / substrate→host fallback in proof / prebuilt graph from outside the task repo
/ mutable image tag as proof input. All artifacts per task; all failures classified.

## Open UNKNOWNs (verify by probe/perm, not reading)
1. `vars.GT_SUBSTRATE_DIGEST` value (403, no repo-var read) — biggest: workflow fail-closes if unset.
2. Full 113/113 GHCR enumeration (no `read:packages`) — proven for 7 + the run log, not swept.
3. pier→container env passthrough + the agent execution locus (host vs in-container).
4. Whether `deepswe_full` has ever run the full 113 matrix (all observed runs are short subsets).
