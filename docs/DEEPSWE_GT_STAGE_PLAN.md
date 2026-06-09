# DeepSWE GT 5-Stage Plan (audit + plan — NOT implementation sign-off)

> Each stage × {GT code · Docker/substrate · DeepSWE pipeline · multi-language}. Branch `gt-trial`.
> Same 5 stages that hardened SWE-Live-Lite. **Implementation gated on GO_NO_GO_DEEPSWE.md.**

## Stage 1 — LSP liveness
| Surface | Current state | Required DeepSWE change | Failure class | Blocker? |
|---|---|---|---|---|
| GT code | `resolve.py` emits LSP cert + warm probe + `lsp_stamp_check`; non-`residual==0`-fake-pass; `LSP_UNSUPPORTED_EXPLICIT` for no-server langs | none — REUSE | `GT_LSP_CERT_FAIL` | no |
| Docker | **only pyright baked** (`Dockerfile.gt-substrate:55`) | **bake gopls + rust-analyzer + typescript-language-server** | `LSP_INSTALL_MISSING` | **YES (70% of tasks)** |
| DeepSWE pipeline | draft substrate step runs gt-run-proof before agent; uploads `lsp_certificate.json`; no host LSP | confirm end-to-end; classify per task | `GT_LSP_CERT_FAIL` | yes (digest unpublished) |
| multi-language | Python certified; TS/JS/Go/Rust `LSP_INSTALL_MISSING`; Java/C++/Ruby `UNSUPPORTED_EXPLICIT` | bake the 3 servers → certify TS/JS/Go/Rust; per-task `lsp_status` artifact | `LSP_INSTALL_MISSING` | **YES** |

## Stage 2 — graph handoff / graph hash
| Surface | Current state | Required DeepSWE change | Failure class | Blocker? |
|---|---|---|---|---|
| GT code | `graph.db` + `graph_certificate.json` emitted; `graph_edges_hash` canonical; closure rebuilt after LSP preserving stamps | none — REUSE | `LSP_STAMP_DROPPED` | no |
| Docker | `/gt_artifacts/graph.db` written out of the container (mount) | none | `GT_ARTIFACT_MISSING` | no |
| DeepSWE pipeline | draft: `GT_HOST_GRAPH_DB`/`GT_CERT_DIR` exported; adapter witness asserts `hook_graph_hash==post-LSP`; L6 gated OFF in substrate (no divergent graph) | validate witness on a real task (D2) | `GT_ARTIFACT_NOT_CONSUMED` | yes (unvalidated) |
| multi-language | tree-sitter graph is 30-lang; edges language-uneven (gt_gt §2.5: COMPOSES/RE_EXPORTS JS/TS-only) | document per-lang edge coverage; non-supported lang files must not corrupt the proof | — | no (graph is language-agnostic base) |

## Stage 3 — embedder usage
| Surface | Current state | Required DeepSWE change | Failure class | Blocker? |
|---|---|---|---|---|
| GT code | embedder cert; ONNX-forced; ZeroModel rejected; ST-under-forced rejected; all-zero-on-nonempty fails; **CHANGE 2 default = gte-modernbert** | none — REUSE | `EMBEDDER_*` | no |
| Docker | **bakes e5, NOT gte** (`Dockerfile:60-62`) → `GT_REQUIRE_EMBEDDER=1` mismatch when loader defaults gte | **bake gte int8** (or pin `GT_EMBED_MODEL_NAME=e5` until baked) | `EMBEDDER_MODEL_ROOT_DIVERGENCE` / FileNotFoundError | **YES (latent fail-close)** |
| DeepSWE pipeline | `embedder_certificate.json` uploaded (draft); no host embedder | confirm; consumption gate on real issue | (embedder cert verdicts) | yes |
| multi-language | gte multilingual; CHANGE-2 MAD proven Py 3.3× / TS 6.3× vs e5 | confirm semantic candidates produced for Go/Rust/JS or classify; never silently accept all-zero | — | no (embedder is language-agnostic) |

## Stage 4 — container/runtime boundary
| Surface | Current state | Required DeepSWE change | Failure class | Blocker? |
|---|---|---|---|---|
| GT code | host exec fails in proof; `classify_runtime_strategy`; fallback/provisioning forbidden | none — REUSE | `FINAL_PIPELINE_HOST_SPLIT_FAIL`, `PROOF_RUNTIME_FALLBACK_FORBIDDEN` | no |
| Docker | pinned digest, no mutable tag, `/work:ro` + `/gt_artifacts`, no per-task pip/download | publish + pin the digest | `GT_SUBSTRATE_DIGEST_MISSING`, `GT_SUBSTRATE_PULL_FAIL` | yes (unpublished) |
| DeepSWE pipeline | draft: the exact `docker run … gt-run-proof` shape; agent starts only after artifacts ready | validate; pier→container env passthrough | `GT_RUN_PROOF_FAIL`, `GT_ARTIFACT_MISSING`, `DEEPSWE_ADAPTER_FAIL` | yes |
| multi-language | substrate runs LSP without host deps — **only for pyright today** | bake the 3 servers (Stage 1); no LSP install in task container | `LSP_INSTALL_MISSING` | **YES** |

## Stage 5 — image cache / manifest / determinism
| Surface | Current state | Required DeepSWE change | Failure class | Blocker? |
|---|---|---|---|---|
| GT code | `run_manifest.json` (GT commit, cert versions, flags) | record language distribution + lsp_status counts | — | no |
| Docker | substrate digest pinned; 113 task images GHCR-first | **verify the 113 GHCR cache exists** (cache workflow never on default branch) | `IMAGE_CACHE_*`, `GT_SUBSTRATE_PULL_FAIL` | yes (unverified) |
| DeepSWE pipeline | draft: GT commit + substrate digest recorded; `if:always` upload; no hidden filtering | record DeepSWE commit + task-image digests; classification + paired-Wilcoxon | `GHA_PIPELINE_FAIL` | yes |
| multi-language | none | run manifest records lang distribution + unsupported/no-op counts; no language-specific silent exclusion | — | no |

## Stage-readiness summary
- **Stage 1 (LSP): BLOCKED** — bake gopls/rust-analyzer/tsserver (70% of tasks).
- **Stage 3 (embedder): BLOCKED** — bake gte (or pin e5) to match the CHANGE-2 default.
- **Stages 2/4/5: ready in GT code; blocked on the substrate digest publish + D2 validation + the 113-cache verify.**
- No stage is safe to *implement-and-run* until the substrate image is rebuilt (LSP servers + gte) and pinned, and the adapter draft passes D0-D2.
