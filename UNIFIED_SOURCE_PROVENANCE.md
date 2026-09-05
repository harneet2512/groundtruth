# HAR-83 unified source candidate

Status: source integration candidate; NOT re-certified or benchmark-ready.

Producer ancestry: `84e19be7011fd3b94d8e28616402898e73849bc0`.
Runtime reference: `f2863f8781edaeaef8787c515e36381cdbd692d5`.
Runtime import authority: the 317 `groundtruth/` package files in the harness's
shipped `groundtruth_mcp-1.0.0-py3-none-any.whl`, SHA-256
`2d0483c43cd7209d7049439af963d420666bc853854b21e8a82e07236b00ee0e`.

The old producer source lacks 63 wheel files and differs in 128 more. It is
not a complete source for that wheel. All 317 package files were mechanically
imported from the hash-verified wheel into `src/groundtruth`, preserving exact
bytes. The runtime reference has all 317 paths; 314 match after CRLF-to-LF
normalization. Remaining differences are comment/docstring encoding in
`pretask/anchor_select.py`, the omitted `witness_verified` primary ranking key
in `pretask/v1r_brief.py`, and a trailing newline in `runtime/producer_audit.py`.
The shipped ranking behavior is deliberately preserved, not silently changed.

`pyproject.toml` is imported from the runtime reference, including the wheel's
MCP `<2.0` compatibility bound and optional dependency declarations. Importing
the dev extra is not part of this integration; the shipping harness retains
its own pinned Mini-SWE version.

The build backend is pinned to the locally verified Hatchling 1.32.0. The
new wheel's metadata uses the producer README (which describes its newer
resolution contract), rather than restoring stale runtime-branch prose.
License text is unchanged and checked out with LF for platform parity.

Producer parser repairs are imported from the working repair in
`gt-fh-producer`: dialect-specific TypeScript/TSX grammars, the restricted
newline generic-call scanner repair, and parser regression tests. Vendored
grammar origins, licenses, and original parser checksums are documented in
`gt-index/internal/grammars/README.md`.

Required before release: exact rebuilt wheel/source correspondence, installed
harness tests, Linux producer build and identity, clean source commit/tree,
independent review packets, updated bundle pins, and provider-free acceptance.
This document is provenance disclosure, not an independent PASS review.

## Repairs after the reconstruction commit

The initial reconstruction is commit `473ebdb8324d175fc911a727d9d63e5efcbcfc08`.
Subsequent source edits deliberately depart from the historical wheel:

- Restore the producer's existing Ruff rule selection and exceptions, without
  adding new exceptions. Repair unused imports and mechanical lint findings;
  apply formatter changes with an AST-equality check before functional edits.
- Separate raw issue trace hints from repository-verified frames. Hints no
  longer depend on the caller's working directory. Verified relative paths
  must resolve inside the repository, including after symlink resolution.
- Restore verified-witness precedence in final evidence reranking. Candidate
  order is distinct from the score-separation gate: flat scores still do not
  establish a confident primary target.
- CI checks out LFS payloads so the real E5 load test receives an ONNX model,
  not a Git LFS text pointer. The live local model-load check passes.

Tests are reconciled with the recovered runtime contracts: explicit LSP scope
arguments, real files for verified trace fixtures, demand-paged graph maps,
source-supported graph edges, non-destructive LSP tombstones, rejection of
unconfidenceable facts, and preservation of distinct evidence relationships.
The runtime reference's corresponding test changes were inspected to confirm
these contracts; runtime safety checks were not relaxed to satisfy old tests.
Two strict expected-failure markers were removed where the recovered runtime
already satisfies the checks. Route/re-export retention now exercises the real
filter instead of depending on source-code line formatting.
