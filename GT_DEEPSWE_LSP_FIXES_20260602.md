# DeepSWE GT — LSP Enrichment Fixes, State & Runbook (2026-06-02)

## TL;DR
The "saiyan" LSP enrichment (promote graph `name_match` edges → verified `lsp`
edges, delete false ones) was a **silent no-op on Linux/GHA — 0 promotions** —
for a long time. Root cause was a stack of URI/launch bugs, **all masked on
Windows** (drive-letter paths happened to be valid). Found only by running on a
**Linux VM with full untruncated visibility** (GHA piped resolve through
`| tail -15`, hiding the real exception). All fixes are **verified by execution**
(the `lsp` edge count actually moved), not by code inspection.

**4 of 5 languages now promote (python, typescript, go, javascript). rust in progress (needs cargo — env, not code).**

## Root causes & fixes (each verified on a Linux VM against a real DeepSWE repo)

| # | Bug | Fix | Commit | Verified metric |
|---|---|---|---|---|
| 1 | Inbound LSP `definition` URI not URL-decoded (`%3A` etc.) → `os.path.relpath` garbage → no node match | `url2pathname(unquote(urlparse(uri).path))` | `e2fdb5a0` | 11/20 edges promotable (py, isolated) |
| 2 | Outbound URI built `f"file:///{abs}"`; on POSIX abs starts with `/` → `file:////tmp/...` (4 slashes) → pyright `[UriError]` → **initialize fails → 0 promotions** | `pathlib.Path(abs).as_uri()` (`_path_to_uri`) | `19a45dc3` | py `lsp 0→18`, ts `0→64` |
| 3 | gopls launched `["gopls","serve","-stdio"]` → `flag provided but not defined: -stdio` → gopls exits → connection closed | `["gopls"]` | `c4e6d6c7` | go `lsp 0→5`, +25 false edges deleted |
| 4 | rust-analyzer returns empty definitions (warmup not awaited and/or `cargo` missing) | TBD | — | in progress |
| 5 | javascript via typescript-language-server (`.js`) | TBD (test) | — | in progress |

Bugs 1 & 2 are the **one LSP surface** (language-agnostic URI handling) — fixing
them fixes all 5 langs' URI path. Bug 3 is a per-server launch string.

## Per-language state (DeepSWE repos, Linux VM)
| lang | server | status | result |
|---|---|---|---|
| python | pyright 1.1.410 | ✓ | `lsp 0→18` (adaptix) |
| typescript | typescript-language-server | ✓ | `lsp 0→64/67` (arktype) |
| go | gopls v0.22 | ✓ | `lsp 0→5`, 25 false deleted (abs) |
| javascript | typescript-language-server | ✓ | `lsp 0→4` (csstree) |
| rust | rust-analyzer 0.3 | ⏳ in progress | needs `cargo` installed (env gap, not GT code); rust-analyzer runs `cargo metadata` to load crates |

## VM runbook (the "correct runtime" — Ubuntu 22.04 apt defaults are TOO OLD)
- **Python 3.12** (deadsnakes PPA) — apt default 3.10 fails `requires-python>=3.11`.
- **Go 1.22** (official tarball to `/usr/local/go`) — apt default 1.18 too old for gt-index.
- **Node 20** (official tarball to `/usr/local`) — apt default 12 too old for pyright.
- **LSP servers:** pyright + typescript-language-server (npm, node20); gopls (`go install`);
  rust-analyzer (gh release binary). **rust also needs cargo/rustup** (rust-analyzer runs `cargo metadata`).
- **Docker Compose v2 plugin** to `/usr/local/lib/docker/cli-plugins/docker-compose` —
  pier REQUIRES it; ubuntu `docker.io` ships none → pier crashes in 0s (`unknown flag: --project-name`).
- **gt-index:** CGO build with go1.22; if gopls was `go install`ed under sudo, `chown -R $USER ~/.cache/go-build` first.
- Pier run command (saiyan): `pier run -p <task> --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent --model deepseek/deepseek-v4-flash --env docker -y --ak config_file=...deepswe_gt_pier_saiyan.yaml --mounts-json '[{bind /tmp/db→/tmp/gt_host_enriched.db}]' --agent-env GT_GRAPH_DB=/tmp/gt_host_enriched.db` with host `GT_GRAPH_DB`/`GT_REPO_ROOT` set.

## Product caveat (honest — do not oversell)
LSP promotion improves graph **precision** (promote correct edges, **delete false
name_match**). On go, 25/30 sampled name_match edges were *false* and got deleted —
the raw graph was ~83% garbage on that sample; LSP made it trustworthy. That is
necessary hygiene: a broken enrichment shipped **false edges that actively mislead
the agent** (violates correct-or-quiet).

It is **NOT proven sufficient for FLIPS.** Per `AUDIT_PRODUCT_READINESS.md`,
localization is largely grep-solved and the dominant bottleneck is *post-localization
reasoning*. A cleaner graph makes the brief trustworthy; whether that converts to
flips is an **open empirical question. No flip observed yet.** The fix unblocks the
real test: run N tasks with enriched briefs and **measure flips vs baseline**.

## Strategy: debug here, validate on GHA
- **VM** = development/visibility (live Linux logs cracked the URI bug GHA hid).
- **GHA** = validation/reproducibility — once all 5 green + flow proven, port the
  validated config + prepulled images to GHA for the scaled flip-measurement run.

## Next
1. rust (cargo + warmup) and js → all 5 green.
2. GHA prepull pipeline ready for the flip-measurement run.
3. Run N across languages, GT-on vs GT-off, **measure flips** (the product question).
