# 10-TASK PROBE REPORT — official pipeline, proof mode

**Run:** `27160806851` (clean) · prior confirming run `27159779278` · workflow `swebench_300task.yml`
**Commit:** `gt-trial` @ `dfab2bae` · **Mode:** `gates_only=true gt_use_substrate_image=true` (8 proof
flags on the in-container `docker exec`) · **Date:** 2026-06-08 · **Evidence:** per-task
`gt-contracts-<task>` artifacts → `/tmp/probe2/<task>/gt_contracts/*.json`; classifications in
`artifacts/classifications.json`.

Probe = classify only. No task was patched or special-cased.

## VERDICT

| Surface | Verdict | Proof |
|---|---|---|
| **GHA pipeline** | **CORRECT** | 10/10 `run_contract.hard_fail=[]`; resolved SHA + substrate/task image digests + dataset sha256 recorded; `task_ids` honored exactly (matrix = the 10). 0 `GHA_PIPELINE_FAIL`. |
| **Containerization** | **CORRECT** | 10/10 step-0 `runtime_preflight.passed=True` (inside container, `import_from_opt_gt`, baked e5 model, real ONNX embedder, LSP present, all 8 flags). Embedder identical in every container (cos_rel `0.86053280`, cos_unrel `0.76078654`). 0 `CONTAINERIZATION_FAIL`. |
| **GT architecture** | **3 real gaps** | conan = `ABSORPTION_FAIL`; gitingest + checkov = `GATE_FALSE_FAIL` (gate-invariant). |
| **Product quality** | **1 gap** | loguru = `PRODUCT_QUALITY_FAIL` (name_match dominance). |

**Final 300 pipeline: NOT YET READY** — structural surfaces (GHA + containerization) are proven
correct, but the GT-architecture absorption gap (conan) must be fixed and the LSP gate-invariant
(gitingest/checkov) resolved before a clean 300 run.

## CLASSIFICATION (6 top-level classes)

| task | gate | preflight | det% | name_match dom | fts5 | LSP res/resid | sem/rendered | final_class | top_level |
|---|---|---|---|---|---|---|---|---|---|
| cfn-lint-3749 | ✓ | ✓ | 73.91 | no | ✓ | 76/66 | 3/5 | GREEN_ROBUST | GREEN_ROBUST |
| beets-5457 | ✓ | ✓ | 70.59 | no | ✓ | 257/182 | 3/5 | GREEN_ROBUST | GREEN_ROBUST |
| briefcase-2085 | ✓ | ✓ | 53.48 | no | ✓ | 89/92 | 3/5 | GREEN_ROBUST | GREEN_ROBUST |
| dynaconf-1225 | ✓ | ✓ | 70.03 | no | ✓ | 23/23 | 4/5 | GREEN_ROBUST | GREEN_ROBUST |
| aiogram-1594 | ✓ | ✓ | 68.83 | no | ✓ | 63/34 | 1/5 | GREEN_THIN | GREEN_THIN |
| haystack-8525 | ✓ | ✓ | 78.09 | no | ✓ | 2/2 | 3/5 | GREEN_THIN | GREEN_THIN |
| **conan-17092** | ✗ | ✓ | 77.79 | no | ✓ | 27/79 | **0/5** | **ABSORPTION_FAIL** | **GT_ARCHITECTURE_FAIL** |
| **gitingest-115** | ✗ | ✓ | 96.33 | no | ✓ | 0/4 | 2/5 | **GATE_FALSE_FAIL** | **GT_ARCHITECTURE_FAIL** |
| **checkov-6893** | ✗ | ✓ | 69.37 | no | ✓ | 0/1 | 3/5 | **GATE_FALSE_FAIL** | **GT_ARCHITECTURE_FAIL** |
| **loguru-1297** | ✗ | ✓ | 36.65 | **yes** | ✓ | 20/13 | 4/5 | **PRODUCT_QUALITY_FAIL** | **VALID_SETUP_BUT_PRODUCT_QUALITY_GAP** |

## THE 4 REDS — per-task evidence

### conan-17092 — `ABSORPTION_FAIL` (the one real GT-architecture wiring bug)
- Graph healthy (det 77.79%, name_match not dominant, FTS5 ok), LSP did real work (27/79), embedder
  discriminates (cos 0.86 vs 0.76). Substrate is sound.
- **`gate_sem_count=0`, `rendered_count=5`, `dropped_by_join=0`.** Snapshot 08-vs-10 inspection proves
  the 5 rendered candidates ARE matched in run_v74's scored set (`live_join=MATCH` for all 5) — so it is
  **NOT a join miss and NOT candidate-set divergence**. But run_v74's **semantic component is `0.0` for
  all 5** (`live_sem=0.0`, `consistent_sem=0.0`) even though the embedder passes GATE 3a (cos 0.86).
  → **run_v74's semantic SCORING produces 0 for conan's candidates**: the embedder loads but its score
  never reaches `_sem_components` for this repo. GATE 3b correctly fails `sem=0/5`.
- **This is the Phase-6 GT-architecture fix — target `v7_4_brief.py` (run_v74) semantic path**, NOT the
  brief join. Investigate why `_sem_components=0` for conan when the embedder works and the other 9
  tasks get sem 1–4/5 — conan is the largest repo here, so the prime suspect is a big-repo size/time
  cap (or a candidate-count threshold) that silently skips embedding. General, not task-specific.
- *Note:* the earlier non-proof-mode audit guessed an exact-path-join "absorption" bug; this proof-mode
  evidence (live_join=MATCH, sem=0) refutes that and localizes the defect to run_v74's scoring.

### gitingest-115 + checkov-6893 — `GATE_FALSE_FAIL` (gate-invariant; surfaced, NOT auto-tuned)
- gitingest det **96.33%**, checkov det 69.37% — both healthy, deterministic graphs, FTS5 ok,
  embedder works. Residual is **tiny** (gitingest 4, checkov 1).
- GATE 2 (LSP) fails because `resolved=0` and `resolved/residual >= 0.10` is required — but with a
  residual of 4 / 1 on an already-96%/69%-deterministic graph there is essentially nothing to convert.
  The gate measures *mechanism fired* ("LSP converted edges"), not *outcome* ("the in-scope graph is
  well-resolved"). The harness's own `lsp_contract` treats tiny-residual-on-deterministic as
  `LSP_NO_OP_VALID`; the product gate does not.
- **Proposed (NOT applied):** GATE 2 should treat `residual <= K (small) AND det >= floor` as a valid
  no-op pass, matching `lsp_contract`. This is correcting a wrong invariant, not lowering a threshold
  to pass a bad substrate. Left for explicit decision — the rules forbid auto-tuning gates to green.

### loguru-1297 — `PRODUCT_QUALITY_FAIL` (reported, NOT fixed this pass)
- det **36.65%**, **name_match DOMINATES** (the only task where `name_match > deterministic`). The
  resolver genuinely under-resolves loguru's call graph — a known resolver-capability limitation
  (gt_gt.md §10), not infra. The gate correctly fail-closes. Setup is valid; the OUTCOME is a quality
  gap. Deferred to a separate product-quality pass per the plan.

## WHAT IS PROVEN *NOT* THE PROBLEM
- **Containerization** — 10/10 preflight pass; GT imports + runs from `/opt/gt` in the eval container;
  graph/LSP/embedder/gates all execute in-container on the same source+graph+model. No host split, no
  container abort.
- **GHA** — 10/10 clean run_contract; intended commit/image/tasks/flags recorded with digests.
- **The embedder** — identical cos (0.86/0.76) across all 10 containers; it loads and discriminates
  everywhere. conan's `sem=0` is the ranking not reaching the rendered set, NOT a dead embedder.
- **FTS5** — 10/10 `fts5_match_probe_ok` (after the harness probe fix); the index queries on every repo.

## HARNESS CORRECTIONS MADE THIS PROBE (read-only audit code, not GT product)
1. `classify.py`: embedder-alive + `sem_count=0` on a non-empty rendered set ⇒ `ABSORPTION_FAIL`
   (consumption gap), not `GATE_FALSE_FAIL`. Fixed conan's attribution.
2. `graph_contract.py`: fts5 probe now uses the full first subtoken with a prefix (`token*`) match over
   several node names — CamelCase/length can't false-fail a populated index. Cleared checkov's false
   `GRAPH_BASE_FAIL` (FTS5 had 36,041 rows).
3. `classify.py`: GREEN_THIN reads `gate_sem_count` from the absorption contract (correct dir). Fixed
   aiogram (sem=1/5 ⇒ thin).

## WHAT TO FIX FIRST (Phase 6 — GENERAL, not task-specific)
1. **conan semantic scoring (GT-architecture):** run_v74 (`v7_4_brief.py`) emits `sem=0` for conan's
   rendered candidates even though they match (`live_join=MATCH`) and the embedder works (3a, cos 0.86).
   Investigate + fix the run_v74 semantic path — prime suspect is a big-repo size/time cap silently
   skipping embedding (conan is the largest repo; the other 9 score sem 1–4/5). General, not the join.
2. **LSP gate-invariant (gitingest/checkov):** surface + propose the no-op-valid correction to GATE 2;
   do not auto-tune. Decision required.
3. **loguru product-quality:** separate report; do not fix in this pass.

Structural correctness (GHA + containerization + proof-mode enforcement) is achieved. Remaining work
is one GT-architecture absorption fix + a gate-invariant decision; everything else is green or a
deferred product-quality gap.
