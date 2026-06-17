# GT ARCHITECTURE — LIVE vs DEAD module lineage (canonical, 2026-06-17)

> **Read this FIRST when touching any GT layer.** It exists to stop the recurring confusion about
> *which module is the live DeepSWE path* vs old/superseded code. Grounded in `gt_audit.md` (layer
> inventory) + `gt_gt.md`/`gt_new.md` §12 (flow), verified by import-tracing (4 parallel mapping
> agents, 2026-06-17). "LIVE" = on the DeepSWE substrate proof path (`gt_run_proof.py` → agent).
> "DEAD-on-DeepSWE" = real importer exists but only on the OH/MCP/CLI path, never DeepSWE.

## THE ONE LIVE DEEPSWE CHAIN (memorize this)

```
substrate proof:  gt_run_proof.py
  §2 graph.db  ->  gt-index (Go binary: cmd/main + internal/{parser,resolver,promote,store,
                   incremental,closure,walker,specs,types})            [owns ALL graph writes]
  §3 LSP       ->  resolve.py  (+ lsp/config.py, lsp/client.py, lsp/protocol.py)
  §5 embedder  ->  memory/enrich/embed.py  (ONNX EmbeddingModel, gte-modernbert-base 768)
  §4/§5 brief  ->  v1r_brief.generate_v1r_brief   [DELIVERED brief — the agent sees this]
                     -> v7_4_brief.run_v74          [the RANKER; writes embedder_certificate]
                     -> anchor_select, anchor_proximity, anchors, hybrid, graph_localizer
  §6 per-turn  ->  artifact_deepswe/gt_mini_patch.py  [ALL per-turn producers INLINE here]
                     orchestrated by gt_agent.py (GTMiniSweAgent), gated by gt_oracle.py /
                     gt_oracle_sense.py / phase_policy.py
  §7 gates     ->  foundational_gates.py (gate_resolution/lsp/embedder) + runtime/proof.py
                     (certs) + brief_cache.py; reconciled by reconcile.py / task_truth.py /
                     deepswe_outcome.py
```

The agent consumes the brief read-only from `$GT_CERT_DIR/brief.txt` (`gt_agent.py:832`). Per-turn
evidence is appended to command observations by `gt_mini_patch.py` monkeypatching mini-swe-agent's
LocalEnvironment. **No `groundtruth.hooks.*` and no `groundtruth.mcp.*` run on DeepSWE.**

---

## §2 graph.db / indexer

| module | verdict | live importer (proof) |
|---|---|---|
| `gt-index/` Go binary (cmd/main + internal/parser,resolver,promote.go,store,incremental.go,closure,walker,specs,types) | **LIVE** | `gt_run_proof.py:960` runs the `gt-index` binary; "the Go indexer owns all writes" (gt_gt.md:415) |
| `src/groundtruth/index/store.py` (Python SymbolStore) | **DEAD-on-DeepSWE** | only `mcp/server.py:21`, `cli/commands.py`; superseded by Go gt-index |
| `index/indexer.py`, `index/ast_parser.py` | **DEAD-on-DeepSWE / CLI-only** | `cli/commands.py:71`; gt_gt.md:1160 "retire the ast paths" |
| `index/graph_store.py`, `graph.py`, `freshness.py`, `path_resolver.py`, `schema_version.py` | **off-DeepSWE** | MCP / hooks / OH-wrapper / preflight only — none on `gt_run_proof.py` |

## §3 LSP

| module | verdict | live importer |
|---|---|---|
| `resolve.py` | **LIVE** | `gt_run_proof.py:1016` (`python -m groundtruth.resolve`) |
| `lsp/config.py`, `lsp/client.py`, `lsp/protocol.py` | **LIVE** | `resolve.py:163,703` (+ protocol transitive) |
| `lsp/manager.py`, `background_promotion.py`, `edge_verifier.py` | **off-DeepSWE** | CLI / MCP / OH-wrapper+preflight only |

## §4 localization + §5 brief + embedder

| module | verdict | live importer |
|---|---|---|
| `v1r_brief.py` (`generate_v1r_brief`) | **LIVE — delivered brief** | `gt_agent.py:855`; `runtime/proof.py`/`brief_cache.py` chain |
| `v7_4_brief.py` (`run_v74`) | **LIVE — ranker core** | `v1r_brief.py:31` |
| `graph_localizer.py`, `anchor_select.py`, `anchor_proximity.py`, `anchors.py`, `hybrid.py` | **LIVE** | `v7_4_brief.py:34,42,44`, `v1r_brief.py:43` |
| `memory/enrich/embed.py` | **LIVE — embedder** | `anchor_select.py:29`, `v7_4_brief.py:626`, `runtime/proof.py:374` |
| `v7_brief.py` + `brief_v5.py` + `v7_layers.py` | **DEAD-on-DeepSWE (CLI-legacy)** | only `cli/commands.py:633`, `control/kernel.py:53`, `scripts/run_baseline_v73.py`, `run_v74_holdout.py`, `phase0_audit.py` — never the v1r/v7_4 chain |
| `v22_brief.py`, `v2_ranker.py`, `brief/graph_map.py` | **DEAD (registry)** | `dead_path_registry.py` |
| orphans: `query_augment.py`, `query_preprocessor.py`, `path_segment.py`, `contract.py`, `v2_types.py` | **DEAD-on-DeepSWE** | reachable only from the dead v7_brief/v22/v2_ranker cluster |

## §6 per-turn delivery + harness

| module | verdict | note |
|---|---|---|
| `artifact_deepswe/gt_mini_patch.py` | **LIVE-CORE** | ALL §6 producers INLINE (consensus `_consensus_block`/`_consensus_progressive`, L3b post_view `_evidence`, L3 contract `_graph_contract_block`, cochange `_cochange_block`, L5/L5b `_l5_nudge`/`_structural_risk_note`, L6 `_l6_reindex` — gated OFF in substrate mode) |
| `gt_agent.py` (GTMiniSweAgent), `gt_oracle.py`, `gt_oracle_sense.py`, `phase_policy.py` | **LIVE** | harness adapter + per-emission decision gate + phase shim |
| `src/groundtruth/hooks/post_view.py`, `post_edit.py` | **DEAD-on-DeepSWE (OH-only)** | docstring "Called by OpenHands PostToolUse hook"; NOT in `gt_agent.py:207-233` injection allow-list; `post_view` survives only as a byte-parity ORACLE for `test_producer_parity.py` |
| `src/groundtruth/mcp/server.py`, `mcp/tools.py` | **DEAD-on-DeepSWE (MCP-only)** | FastMCP stdio server for Cursor/Claude-Code/Codex/OpenHands; pier path never starts MCP |

## §7 gates / proof / reconcile

| module | verdict |
|---|---|
| `scripts/swebench/gt_run_proof.py` | **LIVE — the one proof entrypoint** |
| `scripts/metrics/foundational_gates.py` | **LIVE — GATE 1/2/3** (resolution/LSP/embedder) |
| `runtime/proof.py`, `runtime/brief_cache.py` | **LIVE — cert contract + single-gen brief cache** |
| `scripts/swebench/reconcile.py`, `task_truth.py`, `scripts/verify/deepswe_outcome.py` | **LIVE — witness-over-cert reconcile + outcome classifier** |
| `runtime/dead_path_registry.py` | **inert data table** — enforced ONLY by `tests/unit/test_dead_path_registry.py` (no runtime guard imports it) |

---

## RETIREMENT LABELS (what to mark, and how)

The `dead_path_registry.py` `DEAD_PATHS` are **hard-dead** (no importer anywhere live). Confirmed correct:
`v22_brief`, `v2_ranker`, `brief/graph_map`, `runtime/reindex_helper`.

**Do NOT hard-retire `v7_brief`/`brief_v5`/`v7_layers`** — they have a live CLI/kernel importer
(`cli/commands.py`, `control/kernel.py`, `run_kernel_paired_gate.py`, `run_live_lite_paired.py`).
A hard DEAD_PATH would break those runners. They get a **soft `CLI_LEGACY` / `DEAD_ON_DEEPSWE`** label
(advisory metadata) — dead on the DeepSWE substrate path, live only on the OH/kernel CLI path.

**Registry has no teeth:** `DEAD_PATHS` is enforced by a unit test against 4 hard-coded
`LIVE_ENTRYPOINTS`, NOT a runtime import guard. A wrapper importing a quarantined module is caught only
by `pytest`, not at runtime. (Noted for the owner; not changed here.)
