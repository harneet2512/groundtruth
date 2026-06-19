# DeepSWE GT Multi-Language Support Matrix (audit — NOT a support claim)

> Grounded in `docker/Dockerfile.gt-substrate` (what's actually baked), `resolve.py` `_LANG_TO_EXT`
> (what GT *knows*), and `artifact_deepswe/repo_manifest.json` (the 113-task language split).
> Branch `gt-trial`. **Do not fake support — UNKNOWN/missing is stated honestly.**

## Headline (the load-bearing multilingual fact)
**The substrate image bakes ONLY `pyright` (Python).** `Dockerfile.gt-substrate:49-56` installs Node +
**pyright only**; there is **no gopls / rust-analyzer / typescript-language-server / jdtls / clangd /
solargraph**. In the substrate-consume model the LSP precision pass runs *inside* that image — so on
DeepSWE's **70% non-Python tasks (79/113)** GT's LSP enrichment is **`LSP_INSTALL_MISSING`**. The
graph base (tree-sitter, 30 langs), FTS5, and the embedder ARE language-agnostic and still work — only
the **LSP edge promotion** is Python-only. (`resolve.py` *knows* the servers via `_LANG_TO_EXT` for
py/ts/tsx/js/jsx/go/rust/java/c/cpp/ruby/kotlin — they're just not baked.)

## DeepSWE language distribution (113 tasks, `repo_manifest.json`)
TypeScript 35 · Go 34 · Python 34 · Rust 5 · JavaScript 5. **Python = 30%; non-Python = 70%.**

## The matrix
| Language | Extensions | LSP server (`_LANG_TO_EXT`) | Baked in substrate? | tree-sitter graph? | FTS5? | embedder? | LSP edges? | DeepSWE classification | tasks |
|---|---|---|---|---|---|---|---|---|---|
| **Python** | `.py` | pyright | **YES** (`Dockerfile:55`) | yes | yes | yes | yes (warm probe OK) | **SUPPORTED_AND_CERTIFIED** | 34 |
| **TypeScript** | `.ts`,`.tsx` | typescript-language-server / tsserver | **NO** | yes | yes | yes | no | **LSP_INSTALL_MISSING** (graph+FTS5+embedder OK; no LSP promotion) | 35 |
| **JavaScript** | `.js`,`.jsx` | typescript-language-server / tsserver | **NO** | yes | yes | yes | no | **LSP_INSTALL_MISSING** | 5 |
| **Go** | `.go` | gopls | **NO** | yes | yes | yes | no | **LSP_INSTALL_MISSING** | 34 |
| **Rust** | `.rs` | rust-analyzer | **NO** | yes | yes | yes | no | **LSP_INSTALL_MISSING** | 5 |
| Java | `.java` | jdtls | NO | yes (tier-2) | yes | yes | no | **UNSUPPORTED_EXPLICIT** (not in DeepSWE set; not baked) | 0 |
| C/C++ | `.c`,`.cpp`,`.h`,`.hpp` | clangd | NO | yes (tier-2) | yes | yes | no | **UNSUPPORTED_EXPLICIT** | 0 |
| Ruby | `.rb` | solargraph | NO | yes (tier-2) | yes | yes | no | **UNSUPPORTED_EXPLICIT** | 0 |

## What each classification means here
- **SUPPORTED_AND_CERTIFIED (Python):** the full stack — graph + FTS5 + embedder + warm LSP + LSP-promoted
  edges + closure rebuilt — runs in the substrate; `gt-run-proof` emits a valid `lsp_certificate.json`.
- **LSP_INSTALL_MISSING (TS/JS/Go/Rust — 79 tasks, the 70%):** the language server *exists* and GT
  *knows* it, but it is **NOT baked** in the substrate image. Under `GT_REQUIRE_LSP=1` `gt-run-proof`
  will either fail-closed or emit `LSP_UNSUPPORTED_EXPLICIT` per task. **The graph (tree-sitter), FTS5
  retrieval, and the embedder still function** — so localization/brief still work, but edges stay
  `name_match`-grade (no LSP promotion). **Not a fake pass; an explicit, classified gap.**
- **UNSUPPORTED_EXPLICIT (Java/C++/Ruby):** not in DeepSWE's 113 and not baked — documented for generality.

## Per-language verification still required (UNKNOWN_NEEDS_TEST until D1 probe)
For each of TS/JS/Go/Rust, a **D1 substrate probe** must confirm: (1) does gt-index produce a non-empty
graph + FTS5 MATCH on a real repo of that language; (2) does the embedder yield discriminating vectors
(CHANGE 2 measured TS MAD 6.3× e5 on arktype — TS embedder is strong); (3) the `lsp_certificate.json`
verdict (`LSP_UNSUPPORTED_EXPLICIT` vs fail). Do **not** treat universal-zero LSP edges as success.

## The fix (Stage-1 multilingual blocker)
**Bake `gopls` + `rust-analyzer` + `typescript-language-server` into `Dockerfile.gt-substrate`** (Node is
already present for tsserver). That moves TS/JS/Go/Rust from `LSP_INSTALL_MISSING` →
`SUPPORTED_AND_CERTIFIED`, covering the 70%. Until then, GT on DeepSWE is **graph+FTS5+embedder on all 5
langs, LSP-precision on Python only.** Also resolve the **e5→gte-modernbert** bake (the loader defaults
to gte; the image bakes e5 → `GT_REQUIRE_EMBEDDER=1` mismatch).

## Legitimacy
No per-language exceptions, no task IDs. The classification is structural (baked-or-not, server-known-or-not),
identical across all tasks of a language. Universal-zero LSP on a non-baked language is reported as
`LSP_INSTALL_MISSING`, never silently accepted.
