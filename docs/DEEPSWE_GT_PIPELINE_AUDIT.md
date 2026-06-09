# DeepSWE ↔ GT Pipeline Audit (mapping + gaps — NOT a completion claim)

> Branch `gt-trial`. 3 surfaces × the GT proof contract. DeepSWE is the benchmark SURFACE; GT is the
> generalized PRODUCT. The DeepSWE adapter + the `deepswe_full.yml` substrate step exist only as an
> **uncommitted DRAFT** this session — D0–D4 unrun, so nothing here is "done."

## Surface 1 — GT code surface (does GT provide the contract DeepSWE needs?)
| GT file/function | Contract provided | Used by SWE-Live-Lite? | Reusable for DeepSWE? | DeepSWE risk |
|---|---|---|---|---|
| `scripts/swebench/gt_run_proof.py` | the ONE portable entrypoint; emits 7 artifacts; fail-closed proof; `--print-contract` | yes (`swebench_300task.yml`) | **REUSE as-is** | none (harness-agnostic) |
| `runtime/context.py` (`assert_container_boundary`, `from_env`, `classify_runtime_strategy`) | host-boundary guard + host-handoff consumption branch | yes | **REUSE** | adapter must use the host-handoff branch (read-only), not rebuild |
| `runtime/proof.py` (`graph_edges_hash:225`, embedder identity/consume asserts, `require`) | canonical cross-stage graph fingerprint + fail-closed | yes | **REUSE** | witness must use `graph_edges_hash` to match the post-LSP cert hash |
| `scripts/metrics/foundational_gates.py` (3 gates, `lsp_stamp_check`) | resolution/LSP/embedder-consumption gates | yes (OH workflows only) | **REUSE inside substrate** | **NOT wired into DeepSWE preflight** — embedder-CONSUMPTION ungated on DeepSWE |
| `scripts/metrics/graph_certificate.py` (`format_graph_witness:202`, `classify_graph`) | graph cert + canonical witness line | yes | **REUSE** | adapter emits an equivalent witness (must import it on PYTHONPATH) |
| `scripts/metrics/embedder_certificate.py` (`classify_embedder`) | embedder verdicts (zero/ST-forced/divergence/no-discrim) | yes | **REUSE** | gte vs e5 identity must match the baked model |
| `resolve.py` (LSP cert, `_LANG_TO_EXT`, warm probe) | LSP liveness cert; per-lang dispatch | yes | **REUSE** | **only pyright baked** → non-Python = `LSP_INSTALL_MISSING` |
| failure classes (`FINAL_PIPELINE_HOST_SPLIT_FAIL`, `SUBSTRATE_*`, `LSP_STAMP_DROPPED`, …) | classified fail-closed | yes | **REUSE** + add `DEEPSWE_ADAPTER_FAIL` | new class needed |
| `oh_gt_full_wrapper.py` | **reference ONLY** — consumes `/gt_artifacts` read-only (798-829), emits `[GT_META]` (6906-6922) | yes | **DO NOT COPY internals** | OH AgentState/router_v2/output.jsonl are OH-specific |

**Surface-1 verdict: GT code provides the full contract; REUSE as-is.** The only GT-code gap on DeepSWE is wiring (foundational_gates consumption gate into the DeepSWE preflight), not missing capability.

## Surface 2 — Docker/substrate surface (portable + reproducible, no task-image mutation?)
| Component | Current setup | Reusable for DeepSWE? | Inefficiency avoided? | DeepSWE risk |
|---|---|---|---|---|
| `docker/Dockerfile.gt-substrate` | bakes gt-index(musl+FTS5), python+onnxruntime, node+**pyright**, **e5-small-v2**, gt-run-proof entrypoint, build-time self-test (87-92) | **REUSE the pattern** | yes (no per-task pip/download) | **bakes pyright ONLY** (non-Python LSP missing); **bakes e5 not gte** (CHANGE-2 mismatch) |
| `gt_substrate_image.yml` | builds Dockerfile → GHCR, emits immutable digest | REUSE | yes | digest must be published + pinned (`GT_SUBSTRATE_DIGEST`) — **not yet published** |
| pinned digest | `@sha256:` immutable, no mutable tag | REUSE | yes (no mutable-tag proof input) | DeepSWE must pin the same way |
| model bake | e5 baked (`Dockerfile:60-62`) | REUSE pattern | yes (no runtime download) | **must add gte int8 + the 3 LSP servers** |
| image pull/retry | GHCR-first + retry (OH) | REUSE | yes | the 113 task images: GHCR cache **unverified** |

**Surface-2 verdict: substrate is reusable but INCOMPLETE for DeepSWE** — bake gopls/rust-analyzer/typescript-language-server + gte-modernbert; publish + pin the digest.

## Surface 3 — DeepSWE pipeline/harness (calls GT at the right point + agent consumes?)
| DeepSWE step | File/workflow | Current behavior | GT integration point | Risk |
|---|---|---|---|---|
| workflow | `.github/workflows/deepswe_full.yml` | **DRAFT (uncommitted):** host gt-index/LSP split REPLACED with the gt-run-proof substrate step + cert-env + guards + witness-verify + `if:always` upload | the pre-agent substrate step | digest unpublished → fails-closed; pier→container env passthrough unverified |
| harness | `pier run` + `artifact_deepswe/gt_agent.py` (GTMiniSweAgent) | brief from `/gt_artifacts/brief.txt` (draft); `[GT_META]` witness (draft) | `_generate_brief` + `_emit_gt_meta_witness` | the `graph_certificate` import needs PYTHONPATH guard |
| per-turn | `artifact_deepswe/gt_mini_patch.py` | reads `GT_HOST_GRAPH_DB` (substrate graph); **L6 reindex gated OFF in substrate mode** (draft) | `_db_path`/`_invalidate_on_edit` | env must reach the task container (D2) |
| task set | `repo_manifest.json` (113, 5 langs) | matrix builder | language distribution in run_manifest | 70% non-Python (LSP gap) |
| grading | `scripts/verify/deepswe_outcome.py` | reward/steps/hooks; **NO infra/GT/agent classification** | add classification | manual triage today |

**Surface-3 verdict: the adapter+workflow exist as an uncommitted draft per the handoff doc; not validated end-to-end (D0–D4 unrun).**

## Side-by-side: SWE-Live-Lite (done) vs DeepSWE (current)
| Requirement | SWE-Live-Lite/OH | DeepSWE current | DeepSWE required action |
|---|---|---|---|
| pinned GT substrate digest | ✅ `GT_SUBSTRATE_DIGEST` | draft env added | publish + pin the digest |
| pre-agent GT proof step | ✅ `swebench_300task.yml:919-944` | draft step added | verify end-to-end |
| `/work:ro` mount | ✅ | draft | confirm |
| `/gt_artifacts` output | ✅ `/tmp/gt` | draft `/tmp/gt` | confirm |
| graph cert | ✅ | from substrate | consume only |
| LSP cert | ✅ (Python) | from substrate | **bake non-Python servers** |
| embedder cert | ✅ (e5) | from substrate | **bake gte** |
| foundational gate report | ✅ | from substrate | wire consumption gate into preflight |
| agent wrapper consumes graph | ✅ OH wrapper | draft gt_agent/gt_mini_patch | validate (D2) |
| GT_META witness | ✅ `oh:6906` | draft `_emit_gt_meta_witness` | guard import; validate |
| artifact upload `if: always()` | ✅ | draft | confirm |
| gates_only/dry mode | ✅ | none for DeepSWE | add a dry/proof-only mode |
| full run mode | ✅ 300 | `deepswe_full` (113) | gate on D0-D3 |
| image pull retry | ✅ | kept (GHCR-first) | verify 113 cache exists |
| failure classes | ✅ | + `DEEPSWE_ADAPTER_FAIL` (draft) | confirm |
| multilingual LSP support | n/a (Python) | **pyright-only baked** | bake gopls/rust-analyzer/tsserver |
| unsupported-language classification | n/a | per `resolve.py` | artifact per task |

## Intended target flow
```
DeepSWE workflow → task/repo prepared → pinned GT substrate pulled
→ gt-run-proof (repo @ /work:ro, out /gt_artifacts) → 7 artifacts → existence checked
→ agent starts with GT_CERT_DIR=/gt_artifacts + GT_HOST_GRAPH_DB=/gt_artifacts/graph.db
→ DeepSWE adapter emits [GT_META] witness (graph_hash == post-LSP hash; gt_prebuilt_active=true)
→ prediction → eval → GT artifacts uploaded if:always (+ language + lsp_status)
```
Witness fields: `gt_artifacts, graph_db, graph_hash, lsp/graph/embedder/foundational cert paths,
gt_prebuilt_active=true, runtime_strategy=unified_substrate, substrate_digest, language(s), lsp_status`.
