# GT Full-Potential Two-Harness Benchmark Setup — Build Plan

Goal: GT runs at FULL potential, deeply integrated, info delivered at the correct
time, with per-shard preflight proof, on BOTH benchmark pipelines — so a benchmark
number reflects GT's true capability, not a half-installed/half-wired GT.

## The two pipelines
- **SWE-Live-Lite 300** → OpenHands. `.github/workflows/swebench_300task.yml`. Path:
  `oh_gt_full_wrapper.py` → v1r brief. Python-only split. (canary-proven on beets-5495)
- **DeepSWE 113** → mini-swe-agent via Pier. `deepswe_preindex.yml` + `deepswe_trial.yml`.
  Path: `gt_hook.py`. 5 langs (35 TS / 34 Go / 34 Py / 5 Rust / 5 JS).

## Audit (verified, 3 parallel agents, evidence-cited)
- **OH**: full stack reaches the agent (L1 brief, L3/L3b, consensus `<gt-scope> importer.py
  primary target`) — verified in real observations. Gaps: L5b fires at finish (dead write).
- **DeepSWE = crippled**: v1r brief WIRED-BUT-DEAD (`GT_GRAPH_DB`/`GT_REPO_ROOT` set in 0
  workflows → `_generate_brief` returns ""); graph.db built but NEVER read (`analyze` never
  called; `understand` has no --db; `verify` uses Python SymbolStore not Go graph.db); no
  localization/consensus/orient/validate/impact/MCP; remaining AST/regex hook is **Python-only**
  → blind on 79/113 non-Python tasks.
- **Env (both)**: semantic OFF everywhere (no torch/st, no committed onnx model); gopls/
  rust-analyzer/ts-language-server installed NOWHERE; DeepSWE has no LSP at all; F5: the
  300-task OH workflow never runs the offline LSP pass.

## Design constraints (baked per step)
- **Speed**: preindex graph.db as cached artifacts + LSP-enrich offline (never per-task
  runtime); bake LSP servers + ONNX embedder+model into the Docker image / GHCR (pulled
  once, not pip-installed per run); embedder/LSP loaded once per process; parallel shards.
  ONNX over torch (90MB vs 2GB).
- **Timing (info at correct moment)**: L1 brief→iter-0 instruction; post-view→file-read obs;
  post-edit→immediately after edit obs; consensus→with localization; L5b→BEFORE finish
  (fix dead-write); rate-limit/dedup (no spam — stuck-detector lesson).

## 4-part plan + step status

### Part 2 — per-shard preflight gate  [extend scripts/verify/preflight_pipeline.py]
- [x] `check_semantic_embedder` + `check_lsp_edges` degradation detectors (F1/F2),
      gated by `GT_REQUIRE_FULL_POTENTIAL=1`. **DONE cb9430d3**, verified on VM
      (python full-potential PASS; rust server-installed-0-edges FAIL).
- [ ] Run preflight per shard in both workflows AFTER preindex, BEFORE agent — a FAIL
      shard is skipped/fixed, never run-and-mis-attributed. (do AFTER installs so it passes)

### Part 3 — install for full potential
- [x] ONNX embedder fallback in `_get_embedder` (container-viable, no torch). **DONE 4da6b42f**,
      ST path verified intact. ONNX activates when model files present.
- [ ] Bake `onnxruntime` + the model (`models/e5-small-v2/model.onnx` + tokenizer) into the
      OH + DeepSWE images / GHCR cache. DECISION: git-LFS vs image-bake (image-bake = speed).
- [ ] Install gopls / rust-analyzer / typescript-language-server (+ pyright on DeepSWE) in the
      setup actions (`.github/actions/setup-eval`, deepswe setup). rust-analyzer/gopls need a
      project build to resolve — preindex must `cargo metadata`/`go build` or accept Py+TS only.

### Part 1 — deep DeepSWE integration (biggest gap)
- [ ] Set `GT_GRAPH_DB`/`GT_REPO_ROOT` in deepswe_trial.yml → the v1r brief fires.
- [ ] Mount the preindexed graph.db artifact into the trial container; wire a runtime reader
      (`analyze`/v1r) so the hook actually USES graph.db (not just builds it).
- [ ] De-Python-ify `gt_hook.py` — tree-sitter, not `ast.parse` — so contracts/siblings/
      co-change/change work on the 79/113 non-Python tasks.

### Part 4 — pipeline optimization
- [ ] F5: 300-task OH workflow runs the offline LSP pass (preindex-promote).
- [ ] Fix DeepSWE graph.db build (internet off / no Go source → use preindexed artifact).
- [ ] L5b timing: move scope-warning before the finish boundary.

## Verified facts this session (don't re-litigate)
- Semantic HELPS (Acc@1 0.55 vs 0.36) — strong-bge ablation, corrected count. Keep it; make
  it work in-container (ONNX). [[project_lsp_uri_bug_and_semantic_lever_20260603]]
- brief-decouple + URI fixes are canary-proven (beets-5495 RESOLVED, gold edited). On
  gt-consensus-curation. Run branch HEAD has them.
- Closure table (deep reachability) STILL unwired — the biggest unused graph-depth lever.
