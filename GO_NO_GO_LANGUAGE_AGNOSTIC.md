# GO / NO-GO — GT language-agnostic Docker proof

> Branch `gt-trial`. Gates DeepSWE on the language-agnostic Docker audit. **EXECUTED 2026-06-09 on the fixed-stack image** (superseding the earlier not-run state)
> (no local Docker daemon; the multilingual substrate image is not built/published — rebuild is a pending
> CI dispatch). So every proof is **NO (UNVERIFIED — not run)**, not a failure of the architecture.

| Decision | Status | Reason |
|---|---|---|
| Python language-agnostic proof | **YES** | smoke 27249519490 (fixed stack `3c9e4a79`): exit 0, 7 artifacts, warm pyright |
| JS language-agnostic proof | **YES** | exit 0, 7 artifacts, warm tsserver |
| TS language-agnostic proof | **YES** | exit 0, 7 artifacts, warm tsserver |
| Go language-agnostic proof | **YES** | exit 0, 7 artifacts, **warm gopls** (the `-stdio` launch bug was the blocker — fixed `8ae5584d`) |
| Rust language-agnostic proof | **YES** | exit 0, 7 artifacts, warm rust-analyzer |
| DeepSWE 1-task dry run | **GATED on the 113 sweep + integration audit** | smoke proves the contract; the sweep (27249519544, running) proves it at scale on real repos |
| DeepSWE benchmark run | **NO** | gated on D2 trajectories |

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
