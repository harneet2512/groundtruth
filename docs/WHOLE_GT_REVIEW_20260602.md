# Whole-GT Review — 2026-06-02 (5 parallel adversarial agents, empirical)

Scope: the entire product, not a diff — indexer (parser+specs), resolver+store+passes,
post_edit, post_view+brief+localizer, evidence modules + integration + architecture-vs-
reality. Every finding is empirical (built fixtures, indexed, queried graph.db) with
file:line. Verdict up front:

> **GT is a real $0/no-LLM graph-intelligence pipeline with genuine 5-language
> Contract+Callers — but it is NOT the "16-tool, LSP-enriched, 7-evidence-family"
> product the docs claim, Rust indexing is half-blind, the incremental (post-edit)
> reindex produces WRONG edges, the localizer inverts caller↔callee on its own gold
> example, and the Consistency pillar (the actual flip lever) is Python-only.**

## P0 — correctness / flip-blocking

| ID | Where | Defect | Evidence |
|---|---|---|---|
| P0-1 | resolver/incremental | **Incremental reindex produces FACTUALLY WRONG edges.** `GetAllNodes` (incremental.go:256) omits `parent_id` (Defect A) AND fresh nodes' parent is zeroed and never restored (Defect B, main.go:797/832) → `methodsByClass` empty → `self.process()` in `Helper.run` wrongly resolves to `Worker.process` cross-class. The nodeMeta commit this session is **inert**. | full-index correct (same_file 1.0) vs `-file` reindex `name_match 0.6` to WRONG class |
| P0-2 | localizer | **Caller↔callee inversion.** On the module's own canonical gold case, `graph_localizer` ranks the **callee `db.py` #1 (0.8372)** over the gold caller `importer.py (0.8156)`; `W_SUBJECT=0.15` is too weak to overturn degree+witness. The **brief headlines this wrong target to the agent**. The flip lever. | `/tmp/diag.py` score dump |
| P0-3 | post_edit | **`Impact:` launders name_match callers.** The categorical FACT filter drops name_match callers from `[CONTRACT]`, but `change_impact` (numeric `min_confidence=0.9`) re-admits the SAME edges and `insert(0)` puts them ABOVE verified Contract. Reintroduces the exact laundering the filter prevents. | `after_filter=0` yet Impact shows the dropped callers |
| P0-4 | post_view | **`[CONTRACT BODY]` stale-line bug** (this session's feature). Reads current source with graph.db line numbers → after an edit-then-review, delivers `progress_write`'s body under the `set_fields` header — wrong body as fact. | `/tmp/run_cp.py` reproduced |

## P1 — language parity / pervasive correctness

| ID | Where | Defect |
|---|---|---|
| P1-1 | parser (Rust) | **Rust structurally blind to body evidence:** field_read=0, guard_clause=0, exception_type=0, conditional_return=0, return_shape=1. Rust AST node types differ (`field_expression`, `if_expression`-in-`expression_statement`, `Err(...)`/`?`, trailing-expr returns) and the extractors match py/js/go names. ~5 evidence families dead on a Tier-1 language. |
| P1-2 | resolver/incremental | **Incremental parity holes:** `SetInheritanceMap`, `SetAssignmentIndex`, Rust crate/module tree, Go module/vendor aliases are **never run in `runIncremental`** → ~8 of 13 strategies + relationship/serde/twin enrichment weaker/dead on every edited file. |
| P1-3 | parser (TS) | **TS typed params malformed:** `name:: type` (double colon) on 100% of typed params — pollutes Contract on the language where types matter most. |
| P1-4 | parser (Go) | **Go guard consequence always `{`** (consequence field is the `block`; `Child(0)` is the brace). Go guards carry no payload. |
| P1-5 | post_edit | **[PATTERN] sibling un-gated → noise** (this session's regression): renders top-1 same-file function at relevance 0. Needs a relevance floor. |
| P1-6 | post_edit | **Consistency pillar Python-only + shadowed.** `pattern.py` + `semantic/*` are `ast.parse` (Python-only) AND on the post-:3983 legacy path the L3 early-return skips. The wrong-logic-catching signal is silent off-Python — the single highest-leverage gap for flips. |

## P2 — architecture-vs-reality (docs overstate)

| Claim (CLAUDE.md) | Reality |
|---|---|
| "16 MCP tools (trace/hotspots/brief/explain/impact…)" | **7 active; the 16 documented are all deprecated/commented.** DEAD as documented. |
| "7 evidence families fire" | Fire **only in `gt_intel.py`**, which is **NOT imported by the live OH/DeepSWE path**. Parallel benchmark engine. |
| "LSP-enriched contracts (ONE LSP surface)" | The `resolve` CLI is **never invoked by the live pipeline** — an unwired diagnostic. LSP leg of "ONE pipeline" is absent in delivery. |
| `buildInheritanceMap` (all langs) | **skips go+rust** → cross-file `[OVERRIDE]`/`[PEER]` dead on 2 Tier-1 langs. |
| evidence modules all wired | `error_chain.py`, `return_usage.py`, `sibling_v2.py` = **DEAD** (only their own unit tests). `class_decorator` property = extracted, zero consumers. |

## P3 — lower
Node-resolution: anonymous JS/TS units (`module.exports = function`, anon callbacks) → all ~20 pillars collapse to one **misleading** `[INFO] isolated` line. · Localizer gate cutoffs (`_dynamic_max_hop` 3.0/2/3, `_dynamic_conf_floor` 0.8/0.6, grep 0.5) are **hardcoded magic numbers** (Pillar-1 tension). · Python lambda / Go func-var / JS `module.exports.X=function` naming shapes uncaptured. · Rust `Result<T,E>` truncated by clip_balanced.

## What IS solid (verified)
ONE graph.db pipeline; **$0/no-LLM, no gold/task-ID logic** in product code (gt_env, post_edit, evidence/*); genuine 5-lang property extraction for the kinds that match (param/boundary/docstring + Go receiver MUTATES/READS just fixed); `change.py`/`contract.py` Path-1 are graph-backed 5-lang; **gt_env delivery is clean** (no leaks, no monkey-patch, dedup, GT_BASELINE honored); anchors.py generalizes; the localizer's *suppression* (abstain→grep) is correct-or-quiet.

## The honest bottom line
Localization is the lever (P0-2 inverts it), and post-localization correctness needs the
Consistency pillar — which is Python-only and shadowed (P1-6). The post-edit path the agent
relies on after every edit is silently degraded (P0-1 wrong edges, P1-2 parity holes). And
the docs describe a product (16 tools, LSP, 7 families) that the live path doesn't run. None
of this is "done" (CLAUDE.md): zero baseline-paired flips exist. The remediation register
above is prioritized; P0-1/P0-2/P0-3/P1-6 are the flip-relevant ones.
