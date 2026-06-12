# Checkpoint - dep-store rust-src truth (2026-06-12)

Branch: `gt-trial`  
Code head while writing: pending local commit after `90a5010d`

## Why this slice existed

During the Go/Rust substrate LIPI pass, one remaining ambiguity was still visible:

- `dep_store_manifest.json` proved that `cargo` and `rustup` were copied
- it did **not** prove that usable `rust-src` existed for the active toolchain

That meant a Rust proof failure could still collapse multiple cases:

1. `rustup` missing entirely
2. `rustup` present but no active toolchain
3. active toolchain present but no `rust-src`
4. real `rust-analyzer` incompatibility after all of the above are valid

Stage 1 needs a sharper boundary than that.

## LIPI

```text
ID: CP018
Desired state:
Rust dep-store proof should distinguish "rustup copied" from "usable rust-src for the
active toolchain exists".

Current state:
Manifest validation only required non-empty cargo/rustup stores. A copied rustup tree
without source components could still look acceptable.

Logic:
`rust-analyzer` product readiness depends on more than "some rustup files exist". The
proof boundary needs to say whether the source tree for the active toolchain is present.

Implementation:
`scripts/swebench/dep_store_manifest.py` now derives and records `stores.rust_src`,
including the active toolchain and source-path mapping when found.

Integration:
`.github/workflows/deepswe_full.yml` now resolves `ACTIVE_RUST_TOOLCHAIN` once and passes
it both to the dep-store manifest and the substrate container environment.

Plumbing:
`tests/test_dep_store_manifest.py` now proves:
  - rustup missing -> fail
  - rust-src missing for active toolchain -> fail
  - rust-src present for active toolchain -> pass

Verdict:
Closed for the evidence boundary. Live proof still needed for P0-01.

Remaining bug, if any:
This does not replace substrate rebuild/re-proof. It makes the next Rust failure more
diagnostic if one still exists.

Fix boundary:
dep_store manifest + workflow evidence handoff
```

## Files changed

- `scripts/swebench/dep_store_manifest.py`
- `.github/workflows/deepswe_full.yml`
- `tests/test_dep_store_manifest.py`

## Receipts

```bash
python -m pytest tests/test_dep_store_manifest.py tests/fail_closed/test_portable_substrate.py -q
python -m py_compile scripts/swebench/dep_store_manifest.py
```

Observed locally:

- `39 passed`
- `py_compile` clean

## Product impact

This improves Stage 1 substrate truth in a general way:

- no task-specific logic
- no benchmark-shaped branches
- no workflow repair of the task image
- better separation between copied toolchain state and usable language-source state

## Remaining blocker

Still blocked on:

- rebuilt substrate image
- pinned new digest
- real Go/Rust re-proof artifacts
