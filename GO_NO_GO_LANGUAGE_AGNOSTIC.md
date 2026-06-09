# GO / NO-GO — GT language-agnostic Docker proof

> Branch `gt-trial`. Gates DeepSWE on the language-agnostic Docker audit. **The audit did not execute**
> (no local Docker daemon; the multilingual substrate image is not built/published — rebuild is a pending
> CI dispatch). So every proof is **NO (UNVERIFIED — not run)**, not a failure of the architecture.

| Decision | Status | Reason |
|---|---|---|
| Python language-agnostic proof | **NO (unverified)** | not run; *expected* SUPPORTED_AND_CERTIFIED (Python had real LSP on prior graphs) but unproven on the built image |
| JS language-agnostic proof | **NO (unverified)** | not run; LSP ~0 on prior graphs |
| TS language-agnostic proof | **NO (unverified)** | not run; LSP 0.66% on prior graphs; server now baked but unrun |
| Go language-agnostic proof | **NO (unverified)** | not run; LSP=0 on prior graphs (was LSP_INSTALL_MISSING); gopls now baked but unrun |
| Rust language-agnostic proof | **NO (unverified)** | not run; LSP=0 on prior graphs (worst guess-ratio); rust-analyzer baked but unrun + no v15.2 Rust graph ever existed |
| DeepSWE 1-task dry run | **NO** | gated on the language-agnostic proof + image build |
| DeepSWE benchmark run | **NO** | default; gated on all the above |

## What must be TRUE to flip any proof to YES
1. **Build + publish the substrate image** (`gt_substrate_image.yml`) → the hardened self-test gates it (won't ship if a server/gte is missing) → capture `@sha256`, set `vars.GT_SUBSTRATE_DIGEST`. **The build is unvalidated** (`docker buildx --check` couldn't run locally) — it may fail first.
2. **Run the 5-fixture smoke** (identical `docker run`, only the repo changes) → assert the 7 artifacts + classify each language's LSP from `lsp_certificate.json`.
3. **Fix the unsupported-exit-0 false-green** (`resolve.py` `LSP_UNSUPPORTED_EXPLICIT` exit 0 → must not satisfy `GT_REQUIRE_LSP` on a no-server language).
4. **Resolve the extractor-parity question** (Track-2: does `deepswe-parity` already bring Go/Rust/TS to Python's level for IMPLEMENTS/data_flow/receivers, or must it be written?).

## Honest bottom line
The **contract** is language-agnostic by construction (identical `docker run`, identical artifact set,
fail-closed, no host exec, no per-task download — all static-verified from the committed Dockerfile). What
is **unproven** is that the built image actually *produces* that contract per language — because the image
isn't built and the proof hasn't run. **DeepSWE stays NO until the image is built, the 5-language smoke
passes, and the two open bugs (unsupported-exit-0, extractor parity) are closed.**
