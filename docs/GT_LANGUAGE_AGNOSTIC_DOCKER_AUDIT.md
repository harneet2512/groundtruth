# GT Language-Agnostic Docker Audit (honest — the run did NOT execute; here's why + the mechanism)

> Branch `gt-trial`. Source of truth: `gt_gt.md` + the committed substrate (`docker/Dockerfile.gt-substrate`
> @ `f807ec4b`) + `gt-run-proof`. **This audit could NOT be executed.** Stated plainly below; no faked results.

## EXECUTION BLOCKER (read first)
The 5-language `docker run … gt-run-proof` audit **did not run**, for two hard reasons:
1. **No local Docker daemon** (`docker version` → "daemon not running"). The run method requires a Docker runtime.
2. **The multilingual substrate image is not built/published.** Stage A committed the Dockerfile (bakes
   gopls/rust-analyzer/typescript-language-server/jdtls + gte ONNX), but the **rebuild is a pending CI
   dispatch** of `gt_substrate_image.yml`; `vars.GT_SUBSTRATE_DIGEST` is unverified (403, no read perm).

**Therefore every per-language Docker result below is `UNKNOWN_NEEDS_FIX` (PENDING CI build+run), not a pass.**
The real audit = (a) CI builds the image, (b) a CI smoke runs the identical `docker run` on 5 tiny fixtures,
(c) collect the 7 artifacts per language. Until then, this doc records the contract, the static-verifiable
parts, and the *expected* per-language picture from prior on-disk evidence — clearly labeled as expected, not run.

## gt_gt core runtime invariants — and whether they're language-specific
| gt_gt invariant | Docker-level proof needed | Language-specific? | Result |
|---|---|---|---|
| One deterministic runtime path (`gt-run-proof` in the pinned image) | identical `docker run` shape, all langs | **No** | static: contract is identical; runtime PENDING |
| Builds/loads graph (gt-index in-container) | `graph.db` emitted per lang | No (tree-sitter, 30 langs) | PENDING run |
| Emits `graph.db` + the 6 certs + `run_manifest.json` | 7 artifacts present per lang | No (same emit code) | PENDING run |
| LSP certificate emitted + real warm probe | `lsp_certificate.json` per lang | **Yes (server must be baked)** | PENDING — only pyright proven on old graphs |
| Embedder certificate (gte ONNX, baked) | `embedder_certificate.json` | No (language-agnostic model) | PENDING run |
| Fails closed on missing required components | nonzero exit / classified | No | static: fail-closed code present |
| No silent degrade | unsupported → explicit class, not pass | **Partly** | **anti-pattern present** — see below |
| Not host-dependent | `assert_container_boundary` | No | static: present (`context.py:47`) |
| No task-repo/image mutation | `/work:ro`, repo copied writable | No | static: present (`gt_run_proof.py:236`) |

## Per-language status (EXPECTED from prior on-disk graphs — NOT a Docker run)
| Language | Repo to use (tiny fixture) | Image digest | gt-run-proof exit | graph.db | LSP cert | graph cert | embedder cert | gate report | run manifest | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| Python | tiny .py (fn+class+import) | PENDING | PENDING | exp✓ | exp✓ (LSP ran on old graphs: 274 edges) | exp✓ | PENDING | PENDING | PENDING | **UNKNOWN_NEEDS_FIX (expected SUPPORTED_AND_CERTIFIED)** |
| JavaScript | tiny .js | PENDING | PENDING | exp✓ | LSP ~0 on old graphs | exp✓ | PENDING | PENDING | PENDING | **UNKNOWN_NEEDS_FIX** |
| TypeScript | tiny .ts | PENDING | PENDING | exp✓ | LSP 0.66% old; server now baked | exp✓ | PENDING | PENDING | PENDING | **UNKNOWN_NEEDS_FIX** |
| Go | tiny .go | PENDING | PENDING | exp✓ | **LSP 0 on old graphs**; gopls now baked | exp✓ | PENDING | PENDING | PENDING | **UNKNOWN_NEEDS_FIX (was LSP_INSTALL_MISSING)** |
| Rust | tiny .rs | PENDING | PENDING | exp✓ | **LSP 0 on old graphs**; rust-analyzer now baked | exp✓ | PENDING | PENDING | PENDING | **UNKNOWN_NEEDS_FIX (was LSP_INSTALL_MISSING)** |

## Anti-patterns — STATIC verification from the committed Dockerfile/workflow (no run needed)
| Anti-pattern | Must be absent | Status (from committed code) |
|---|---|---|
| 130MB embedder downloaded per task | yes | **ABSENT** — gte baked at build time (`Dockerfile:60-62` pattern), `HF_OFFLINE`+`GT_MODELS_ROOT` |
| GT pip-installed per task | yes | **ABSENT** — baked; `validate_proof_env` fails closed if not |
| pyright/node/servers installed per task | yes | **ABSENT** — baked in the image |
| host GT execution | yes | **ABSENT** — `assert_container_boundary` fail-closed in proof |
| mutable image tag | yes | **ABSENT** — `@sha256` pin asserted (`deepswe_full.yml:370`) |
| fallback to old gtsrc path | yes | **ABSENT** — Stage B killed the dual-graph build in substrate mode |
| artifacts only uploaded on success | yes | **ABSENT** — `if: always()` (Stage B) |
| **unsupported language marked as success** | yes | **PRESENT (BUG)** — `resolve.py` emits `LSP_UNSUPPORTED_EXPLICIT` exit 0, gate PASSES → a no-server lang false-greens `GT_REQUIRE_LSP` (Stage-1 finding). Must be fixed before the audit can pass non-Python. |

## The 8 questions — answered honestly
1. **Same image across 5 langs?** Cannot confirm — **not run** (no Docker, image unbuilt). The `docker run` shape is identical by design.
2. **All 5 produced the artifact bundle?** Unknown — not run.
3. **Fully supported langs?** Unproven. On *old* graphs only Python had real LSP; the new servers are baked but unrun.
4. **Explicitly unsupported/weak?** Go/Rust were `LSP_INSTALL_MISSING` on the old image; the new image bakes their servers but it's unverified they launch+resolve in-container (build-env dependency open).
5. **Any silent pass while missing artifacts?** The `LSP_UNSUPPORTED_EXPLICIT`-exit-0 path is a latent false-green on non-Python — must be tightened.
6. **Avoided runtime downloads/provisioning?** **Yes (static-verified)** — model + servers + deps all baked; no per-task pip/download.
7. **Does the image satisfy gt_gt language-agnostically?** **Cannot certify** — the contract invariants are language-agnostic by construction (static), but LSP depth + extractor parity are unproven per language, and the image isn't built/run.
8. **What blocks DeepSWE?** (a) image not built+published+pinned; (b) the 5-language proof unrun; (c) the unsupported-exit-0 false-green; (d) the indexer extractor-parity question (Track-2, `deepswe-parity` branch).

## How to actually run it (the minimal CI mechanism — not built here)
1. Dispatch `gt_substrate_image.yml` → build the Dockerfile (Stage A's servers+gte) → push GHCR → capture `@sha256`.
2. A tiny smoke job: 5 fixture repos (a few files each), the identical `docker run … gt-run-proof --source-root /work --out /gt_artifacts`, assert the 7 artifacts + classify each language's LSP status from `lsp_certificate.json`.
3. Fill this table with the real exits/artifacts/classes. **That is the audit.** Until step 1 succeeds (the build is unvalidated), nothing here is a pass.
