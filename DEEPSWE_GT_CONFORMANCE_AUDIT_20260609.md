# DeepSWE GT Conformance Audit — does GT function as gt_gt says, standalone AND via mini-swe-agent?

> ONE question, two columns, judged by FUNCTIONING (proven from agent observation / code+artifacts),
> not code-presence. Branch `gt-trial` @ `30be3232`. Grounded in gt_gt §1–13 + the readiness audit
> (file:line). Legend: ✅ proven · ⚠️ partial/degraded · ❌ broken/missing · ❓ unproven/unknown · n/a.

## The bottom line (read first)
**Standalone: GT largely functions as gt_gt §1–10 says** (with gt_gt's OWN honest gaps — §2.5), and
the new levers (granularity, dense-floor fusion) are committed. **Via mini-swe-agent: NOT yet proven
ready** — the **load-bearing unknown** is whether the mini-swe-agent v2 loop does *real work* on
gt-trial at all (the only gt-trial-era DeepSWE capture, 06-03, did **0 working commands / 284 errored**;
the one healthy run was pre-levers, a different branch, single Go task, unresolved). Plus: the
**embedder-CONSUMPTION gate doesn't run on DeepSWE**, **L6 reindex strips LSP in-container**, **no frozen
DeepSWE baseline exists**, and the **113-image GHCR cache is unverified**.

## Framing — the two columns are PRODUCT vs BENCHMARK SURFACE (and what the Docker image IS)
- **Column 1 = the PRODUCT.** gt_gt §1–10 describes GT-the-product: a GENERALIZED pipeline (graph + LSP
  + embedder + brief + hooks) that works on ANY repo / language / agent / MCP client / IDE. "Functions
  as gt_gt says" here = the generalized capability, **independent of any benchmark**.
- **Column 2 = the DeepSWE BENCHMARK SURFACE.** DeepSWE (`pier + mini-swe-agent + 113 tasks`) is a
  VALIDATION surface — it proves the product works; it does NOT define the product (CLAUDE.md). A
  Column-2 failure may be a *packaging/wiring* gap on the surface, not a product gap.
- **The Docker image = GT *for the DeepSWE benchmark*** — the generalized product PACKAGED for this
  surface: the GT package + multilingual LSP servers (pyright/gopls/rust-analyzer/tsserver) + the
  embedder + the mini-swe-agent/Pier wiring + the 113-image pull. It is **NOT the product itself**, it is
  created **differently** from the OH/SWE-Live substrate (different harness = mini-swe-agent, multilingual,
  Pier/Harbor task-env), and it carries **ZERO benchmark logic** — no task IDs/gold/per-task tuning;
  **ONE image, the SAME GT, identical across all 113 tasks** (per-task variation = manipulation). Its
  insides are the generalized product; its purpose is the benchmark surface.

## §2 Layer-0 graph base (gt-index, tree-sitter)
| capability (gt_gt) | standalone | via mini-swe | note |
|---|---|---|---|
| 5-pass + 6-subpass build (STRUCTURE→DEFS+FTS5→CALLS→PROPS+ASSERTIONS→API/REL/SERDE→CLOSURE→EXTRAS→HASHES→COCHANGE) | ✅ | ⚠️ | in-container fresh index per task (`deepswe_full:410-446`, `GT_FORBID_PREBUILT_GRAPH=1`); proven on the 06-05 Go run |
| 11-rung CALLS resolver (0.2–1.0 + trust_tier) | ✅ | ✅ | same Go binary in-container |
| 9 tables (nodes/edges/properties/assertions/cochanges/closure/file_hashes/project_meta/nodes_fts) | ✅ | ❓ | not table-by-table verified in the mini-swe container beyond the run "working" |
| ~23 property kinds | ✅ py · ⚠️ others | ⚠️ | gt_gt §2.5: cross-lang-solid = data_flow/param/docstring/return_shape/caller_usage; `side_effect`/`field_read` are `self.`/`this.`-only (miss Go/Rust receivers) |
| edge types CALLS/CONTAINS/EXTENDS/IMPLEMENTS/COMPOSES/RE_EXPORTS/HANDLES_ROUTE | ⚠️ language-uneven | ⚠️ | §2.5: COMPOSES/RE_EXPORTS JS/TS-only; 23 Tier-2 langs get ZERO EXTENDS/IMPLEMENTS — **matters: DeepSWE is 70% non-Python** |
| incremental `-file` reindex + `-rebuild-closure` | ✅ | ⚠️ | L6 runs `-file` in-container BUT no LSP server there → reindex is name_match-grade (LSP-strip) |
| documented-dead: IMPORTS never emitted · DEFINES/REFERENCES/INHERITS not impl · metadata empty · verification_status never flipped | ✅ (honestly dead) | ✅ | gt_gt §2.5 already states these are dead-by-design |

## §3 LSP enrichment
| capability | standalone | via mini-swe | note |
|---|---|---|---|
| select low-conf CALLS, `_LANG_TO_EXT` dispatch, def-match by file+name+line, verified/corrected/deleted, promote→`lsp`/CERTIFIED, rebuild-closure | ✅ | ⚠️ **host-only** | LSP servers (pyright/gopls/rust-analyzer/tsserver) run on the **RUNNER** pre-run (`deepswe_full:261-272`), NOT in the task image → the **pre-run** graph is LSP-enriched, but the agent's **post-edit** reindex is name_match-grade (the LSP-strip) |

## §4+§11 Localization + brief
| capability | standalone | via mini-swe | note |
|---|---|---|---|
| `generate_v1r_brief` = run_v74 + localize + render_brief; HIGH/MEDIUM/LOW tiers | ✅ | ✅ reaches agent | brief verbatim in the agent's `--task=` (captured 06-03 arktype run + `delivered_instruction.txt`) |
| CHANGE 1 per-symbol MaxSim granularity | ✅ (3/7→7/7 @ e5/384) | ❓ | committed `33970b9f`; UNPROVEN it shapes the brief the DeepSWE agent *consumed* |
| dense-floor fusion (W_SEM_FLOOR=0.25, base 0.40, query-adaptive) | ✅ (15/15) | ❓ | committed; no gt-trial DeepSWE trajectory with it captured |
| brief CONSUMED (agent reasons *through* it) | n/a | ❓ | the one healthy run (06-05) was pre-levers + unresolved; 06-03 did 0 working commands |

## §5 Semantic / ONNX embedder
| capability | standalone | via mini-swe | note |
|---|---|---|---|
| e5-small-v2 ONNX, both halves via `_OnnxEmbedderAdapter`, `GT_FORCE_ONNX/REQUIRE_EMBEDDER/MODELS_ROOT` | ✅ | ✅ (on RUNNER) | embedder runs host-side for the brief; dim=384 confirmed on the 06-05 run |
| CHANGE 2 gte-modernbert (open-source) | 🔄 worktree | ❌ not on gt-trial | gt-trial default is still e5; **latent break: a gte default without baking `models/gte-modernbert-base` fail-closes `GT_REQUIRE_EMBEDDER=1`** |

## §6+§12 Hooked surface (the layers the agent actually gets)
| layer (its gt_gt role) | standalone | via mini-swe | note |
|---|---|---|---|
| L1 task-start brief (`<gt-task-brief>`/graph-map/localization/orientation) | ✅ | ✅ delivered | proven verbatim |
| L3b per-view/per-edit contracts (SIGNATURE/CALLERS/PRESERVE/TWIN/COMPLETENESS) | ✅ | ✅ cross-lang (unify) | the unify replaced `gt_hook` ast → graph SQL pillars (RED→GREEN: empty-on-Go → non-empty); **validated, not yet committed** |
| L4 event hook | ✅ fires on its event | ❓ | not exercised in the captures |
| L5 trajectory governor | ✅ | ⚠️ reduced | mini-swe has no per-turn callback → only scaffold-trap + loop + hypothesis heuristics port; full L5Governor CANNOT port |
| L6 reindexer (preserve LSP, fresh graph) | ✅ standalone | ⚠️ strips LSP | reindexes in-container but no LSP server there → name_match-grade post-edit |
| consensus `<gt-scope>` + co-change completeness | ✅ | ✅ delivered | graph.db SQL, cross-lang (06-05 run) |
| 16+7 MCP tools | ⚠️ registered-but-passive (~0% adoption) | ⚠️ same | gt_gt §6 states this honestly; delivery is the passive brief/hooks, not tool calls |

## §7 Gates (no-silent-fallback)
| gate | standalone (OH path) | via mini-swe (DeepSWE) | note |
|---|---|---|---|
| GT_REQUIRE_FTS5 / EMBEDDER(present) / FORCE_ONNX / REQUIRE_LSP / REQUIRE_FULL_STACK / FORBID_PREBUILT | ✅ wired | ✅ via `preflight_pipeline.py --census` (`deepswe_full:483`) | preflight runs 16 checks incl. semantic_embedder (loads+nonzero) |
| **embedder CONSUMPTION gate (`effective_w_sem>0`, dispersion) — `foundational_gates.py`** | ✅ (OH workflows) | ❌ **NOT run on DeepSWE** | `foundational_gates` is wired only into the OH workflows; the DeepSWE preflight checks brief-gen with a **synthetic issue** → never asserts `effective_w_sem>0` on the real task. **The exact "present-but-unconsumed" failure CLAUDE.md flags is UNGATED on DeepSWE.** |
| agent-launch viability (`n_agent_steps>0`) | n/a | ❌ missing | 06-03: preflight printed "ALL PASS" while the agent crashed at startup |

## §8 Hardcoded params (RRF k=60, β=0.85, MaxDepth=3, MAX_FILES=5, MAX_BRIEF_TOKENS=600, floor=0.7…)
| | standalone | via mini-swe | note |
|---|---|---|---|
| all fixed-with-reason params | ✅ | ✅ same code | language-agnostic; no change needed |

## §13 The DeepSWE/mini-swe integration itself
| | via mini-swe | note |
|---|---|---|
| pier + GTMiniSweAgent harness, 113-task matrix, GHCR-first pull | ⚠️ scaffolded | `deepswe_full.yml` exists; end-to-end runnability UNPROVEN |
| 113-image GHCR cache | ❓ unverified | `cache_deepswe_images.yml` never on the default branch (self-documented dispatch-404) — can't confirm GHCR has all 113 |
| frozen GT-off DeepSWE baseline (for paired flips) | ❌ none | only the OH/SWE-Live-Lite 87/300 baseline exists |
| infra-vs-GT-vs-agent classification + paired Wilcoxon | ❌ missing | `deepswe_outcome.py` surfaces signals, no triage/delta |

## Gating items before ANY paid run (ordered)
1. **PROBE: does the mini-swe-agent v2 loop do real work on gt-trial?** (n=1 trial, read trajectory; 06-03 = 0 working commands). **This is the load-bearing unknown.**
2. **Verify the 113-image GHCR cache exists** (or expect cold ECR rate-limiting at max-parallel 20).
3. **Wire `foundational_gates.gate_embedder_consumption(db,root,REAL issue)` into the DeepSWE preflight** (consumption is currently ungated there).
4. **Add `n_agent_steps>0` agent-launch-viability post-assertion** (the 06-03 false-green).
5. **Build + FREEZE a GT-off DeepSWE baseline** (stock mini-swe-agent, same model/config/113).
6. **Add infra/GT/agent classification + paired Wilcoxon** to the outcome path.
7. **Resolve the gte-modernbert/e5 substrate mismatch** before CHANGE 2 merges (bake the model or it fail-closes).
8. **Bake an LSP-capable DeepSWE substrate image** so L6 reindex preserves LSP across 5 langs (else post-edit is name_match-grade).
