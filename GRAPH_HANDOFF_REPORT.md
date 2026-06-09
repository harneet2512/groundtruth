# GRAPH_HANDOFF_REPORT — Stage 2

> Stage 2 of the full-runtime proof. Branch `gt-trial`. Scope: prove the graph is DEEP and the
> SAME post-LSP graph is used by build → LSP → gates → OH hooks. Code + tests only (live
> per-task hashes land in Phase 6 Stage B/C). No embedder / container-movement / image-cache work.

## What changed (the invariant)

A new **graph certificate** (`scripts/metrics/graph_certificate.py`, output
`$GT_GRAPH_CERT` / `/tmp/gt/graph_certificate.json`) measures graph depth + handoff and
classifies it; a single **canonical edge hash** pins every stage to one graph; the OH wrapper
emits a **hook witness** so a run/test can prove the agent's hooks read that same graph.

- **Canonical hash:** `proof.graph_edges_hash` (SHA-256 over `source_id,target_id,type,
  resolution_method,confidence` ordered by id) — identical formula to
  `resolve._graph_edges_hash` (Stage 1's cert) and `graph_certificate.graph_edges_hash`. A drift
  test asserts all three agree, so `graph_hash_after_lsp` (LSP cert) and the hook hash are
  directly comparable.
- **Depth fields:** `nodes/edges/calls/contains_edges_count`, `deterministic/name_match_edge_count`,
  `resolution_method_distribution`, `trust_tier_distribution`, `properties_count`,
  `data_flow_count`, `assertions_count`, `closure_count`, `project_meta_present`,
  `fts5_exists/row_count/match_probe_ok`.
- **FTS5 MATCH proof:** `fts5_match_probe` runs a real `… WHERE nodes_fts MATCH 'a*'` query — a
  Go-built FTS5 vtable answers (`match_ok=True`); a *regular* table named `nodes_fts` (no FTS5)
  makes MATCH raise (`match_ok=False` → fail). Existence/row-count alone is not accepted.
- **Handoff fields:** `host_resolved_graph_db` (`GT_HOST_GRAPH_DB`), `graph_hash_after_lsp`
  (from the LSP cert), `closure_rebuilt_after_lsp`, `lsp_warm_from_same_graph`, `hook_graph_hash`,
  `prebuilt_active`, `built_inside_container`.
- **Hook witness:** `oh_gt_full_wrapper.py` emits, after graph install,
  `[GT_META] graph_witness host_resolved_graph_db=… hook_graph_db=… hook_graph_hash=…
  _gt_prebuilt_active=…`. When the resolved graph was uploaded into the container as
  `config.graph_db` (`_gt_prebuilt_active`), its content equals `GT_HOST_GRAPH_DB`, so the
  hook hash must equal `graph_hash_after_lsp`.

## Hard-gate verdicts (`classify_graph`)

| Verdict | Pass? | Condition |
|---|---|---|
| `GRAPH_VALID` | ✅ | deep graph, FTS5 MATCHes, (proof) in-container + handoff present + active, closure-after-lsp, hashes consistent |
| `GRAPH_FAIL_EMPTY` | ❌ | 0 edges / 0 CALLS edges / no cert |
| `GRAPH_FAIL_FTS5` | ❌ | nodes_fts missing, empty, or MATCH-unqueryable |
| `GRAPH_FAIL_BUILT_ON_HOST` | ❌ | (proof) `built_inside_container is False` |
| `GRAPH_FAIL_MISSING_HANDOFF` | ❌ | (proof) `GT_HOST_GRAPH_DB` unset |
| `GRAPH_FAIL_HANDOFF_INACTIVE` | ❌ | (proof) handoff present but `_gt_prebuilt_active is False` |
| `GRAPH_FAIL_STALE_CLOSURE` | ❌ | `closure_rebuilt_after_lsp is False` |
| `GRAPH_FAIL_HASH_MISMATCH` | ❌ | `graph_hash != graph_hash_after_lsp` (gates' graph ≠ resolved) |
| `GRAPH_FAIL_HOOK_MISMATCH` | ❌ | `hook_graph_hash != graph_hash` (hooks' graph ≠ resolved) |

(Handoff/in-container gates are enforced only under `GT_PROOF_MODE`; correct-or-quiet outside it.)

## Tests — `tests/fail_closed/test_graph_handoff.py` (18, all pass)

Classifier matrix (valid + all 8 failure verdicts, + non-proof handoff-optional) · real-DB FTS5
MATCH **positive** (Go-built vtable, skipped if this Python's sqlite3 lacks FTS5) and **negative**
(regular `nodes_fts` table → MATCH raises) · **hash canonicality** across `graph_certificate` /
`proof` / `resolve` on a real DB · `build_graph_certificate` real-DB counts + `lsp_warm_from_same_graph`
· witness-line format.

## Hash chain (build → LSP → gates → hooks)

| Stage | Hash field | Source |
|---|---|---|
| Build / after-LSP | `graph_hash_after_lsp` | LSP cert (`resolve._graph_edges_hash`, Stage 1) |
| Gates | `graph_hash` | `graph_certificate` over the gated DB |
| Hooks | `hook_graph_hash` | wrapper `[GT_META] graph_witness` over the uploaded resolved DB |

`classify_graph` fails on any mismatch among these. Live values are asserted equal in Stage C
(held-out-10): `hook_graph_hash == graph_hash_after_lsp` + `_gt_prebuilt_active=True`.

## Live data status

Per-task graph hashes, the FTS5 MATCH on real `gt-index` DBs, and the hook witness equality are
produced by the official workflow in Phase 6 **Stage B (1-task)** and **Stage C (held-out-10)**.
This stage proves the certificate + gate + hash logic locally and adds the live witness emitter;
it does not yet assert a live cross-stage hash match (that is Stage C). The container-side
collection of `graph_certificate.json` is wired in **Stage 4**.
