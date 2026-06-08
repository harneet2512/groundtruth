#!/usr/bin/env bash
###############################################################################
# codespace_run.sh
#
# Reproduce the PROVEN GitHub Actions GT eval pipeline (canary_3arm.yml) on a
# GitHub Codespaces HOST for ONE task, ONE arm.
#
#   Task : beetbox__beets-5495
#   Arm  : v2_live   (baseline_flag=false, router_v2_mode=live)
#
# This uses the PRODUCTION path:
#   scripts/swebench/oh_gt_full_wrapper.py
#     + OpenHands 0.54.0 Docker runtime (ghcr.io/all-hands-ai/runtime:0.54-nikolaik)
#     + the SWE-bench-Live task image (docker.io/starryzhang/...:latest)
#
# It is NOT the railway/run_one_task.py local-runtime driver (buggy; abandoned).
#
# Faithfully mirrors these GHA steps (canary_3arm.yml + composite actions):
#   - .github/actions/setup-eval/action.yml      (OH clone+install, gt-index build)
#   - .github/actions/preindex-promote/action.yml (offline /testbed pre-index -> graph.db)
#   - canary_3arm.yml "Write OH config"  (config.toml)
#   - canary_3arm.yml "Pull Docker image"/"Compute task Docker image" (image name calc)
#   - canary_3arm.yml "Run agent"        (the exact wrapper invocation + GT_* env)
#
# RUN AS (operator passes the key, never hardcoded):
#   DEEPSEEK_API_KEY=sk-xxxxxxxx bash railway/codespace_run.sh
#
# Optional: CSOUT_LOG=/path/to/run.log to override the tee log location.
###############################################################################

# (1) strict-ish bash. We intentionally do NOT use `set -u` because the GHA
#     scripts reference optional vars; and we keep pipefail so a failed wrapper
#     in a pipe is caught. Known non-fatal WARNs (docker pull fallback, pyright
#     miss, closure-table absence) are individually guarded with `|| echo WARN`
#     so they never trip `set -e`.
set -eo pipefail

# ---------------------------------------------------------------------------
# Paths (codespace host: repo lives at /workspaces/groundtruth)
# ---------------------------------------------------------------------------
REPO="${REPO_ROOT:-/workspaces/groundtruth}"
OH_DIR="/tmp/OpenHands"
VENV="/tmp/ohvenv"
GT_INDEX_BIN="/tmp/gt-index"
TASK="${GT_TASK:-beetbox__beets-5495}"
ARM="v2_live"
# arm=v2_live mapping (canary_3arm.yml prepare.split, lines 62):
#   baseline_flag=false  router_v2_mode=live
# GT_BASELINE=1 flips to the pure-OpenHands arm (GT disabled) for A/B.
BASELINE_FLAG="$([ "${GT_BASELINE:-0}" = "1" ] && echo true || echo false)"
ROUTER_V2_MODE="live"
# Multilingual support: override dataset/split/image-namespace via env to run
# SWE-bench Multilingual (go/rust/ts/js/java/c++/php/ruby) instead of the
# python-only SWE-bench-Live. The OH harness maps any non-(live/gym/rebench)
# dataset name to DATASET_TYPE=SWE-bench -> docker.io/swebench/ namespace, which
# is where the multilingual instance images live. Example:
#   GT_TASK=gin-gonic__gin-1805 GT_DATASET=swe-bench/SWE-bench_Multilingual \
#   GT_SPLIT=test GT_IMG_NS=swebench bash railway/codespace_run.sh
DATASET="${GT_DATASET:-SWE-bench-Live/SWE-bench-Live}"
SPLIT="${GT_SPLIT:-lite}"
IMG_NS="${GT_IMG_NS:-starryzhang}"

LOGFILE="${CSOUT_LOG:-/tmp/gt_debug/full_run.log}"
CSOUT_DIR="${REPO}/.csout"

# Clear per-run state FIRST: /tmp/gt_debug holds the belief ledger + layer events,
# and the router recalls prior deliveries from there — without clearing, a stale
# delta from an EARLIER run of the same task gets re-shown ("[RECALL]") in this run
# (observed run4 flood recalled into run5). Each run must start clean.
rm -rf /tmp/gt_debug /tmp/results 2>/dev/null
mkdir -p /tmp/gt_debug /tmp/results "${CSOUT_DIR}"

echo "==============================================================="
echo " GT codespace reproduction"
echo "   repo       = ${REPO}"
echo "   task       = ${TASK}"
echo "   arm        = ${ARM}  (baseline_flag=${BASELINE_FLAG}, router_v2=${ROUTER_V2_MODE})"
echo "   venv       = ${VENV}"
echo "   logfile    = ${LOGFILE}"
echo "==============================================================="

# ---------------------------------------------------------------------------
# (0) Sanity: the key must be present (passed by the operator, not hardcoded)
# ---------------------------------------------------------------------------
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "FATAL: DEEPSEEK_API_KEY is not set. Run as:"
  echo "  DEEPSEEK_API_KEY=sk-xxxx bash railway/codespace_run.sh"
  exit 1
fi

# ---------------------------------------------------------------------------
# (2) Python venv + OpenHands 0.54.0 + GroundTruth + datasets + swebench
#     Mirrors .github/actions/setup-eval (Install Python packages step).
#
#     DECISION on PEP 668 (externally-managed codespace python): use a DEDICATED
#     venv at /tmp/ohvenv. This sidesteps PEP668 cleanly and guarantees the
#     wrapper + OpenHands run in the SAME interpreter (mandatory: GT patches OH
#     in-place and imports openhands). We deliberately do NOT use
#     --break-system-packages, which would pollute the system interpreter and
#     risk a different python being picked up by `python` vs `python3`.
# ---------------------------------------------------------------------------
echo "=== [2] Python venv + OpenHands + GroundTruth install ==="
if [ ! -d "${VENV}" ]; then
  python3 -m venv "${VENV}"
fi
# shellcheck disable=SC1091
source "${VENV}/bin/activate"
python -m pip install --upgrade pip >/dev/null 2>&1 || true

# Idempotent: skip the whole install if everything is already importable
# (same import probe the GHA "skip if cached" step uses).
if python -c "import openhands; import groundtruth; import datasets; import swebench" 2>/dev/null; then
  echo "All Python packages importable — skipping install (idempotent re-run)"
else
  echo "Installing Python packages into ${VENV} ..."
  # OpenHands 0.54.0 — clone only if not already present (idempotent-ish)
  if [ ! -d "${OH_DIR}/.git" ]; then
    git clone --depth 1 --branch 0.54.0 https://github.com/All-Hands-AI/OpenHands.git "${OH_DIR}"
  else
    echo "OpenHands clone already present at ${OH_DIR} — reusing"
  fi
  ( cd "${OH_DIR}" && pip install -e . )
  pip install datasets toml
  ( cd "${REPO}" && pip install -e . )
  # Patch OH to register GT tools as native function-calling tools (setup-eval line 47)
  python "${REPO}/patches/oh054/apply_gt_tools.py"
  # SWE-bench-Live harness (eval side; python-only branch, --recursive)
  if ! python -c "import swebench" 2>/dev/null; then
    if [ ! -d /tmp/SWE-bench-Live/.git ]; then
      git clone --depth 1 --branch python-only --recursive \
        https://github.com/microsoft/SWE-bench-Live.git /tmp/SWE-bench-Live
    fi
    ( cd /tmp/SWE-bench-Live && pip install -e . )
  fi
fi

# Semantic embedder (gt_trial §1): bake the e5-small-v2 ONNX model so semantic localization is
# ON (no torch). Idempotent — setup_models.py skips if present. Required for GT_REQUIRE_EMBEDDER.
if [ ! -f "${REPO}/models/e5-small-v2/model.onnx" ]; then
  pip install -q onnxruntime tokenizers huggingface_hub 2>/dev/null || echo "WARN: onnxruntime install failed (semantic will fail-closed)"
  python "${REPO}/scripts/setup_models.py" 2>&1 | tail -3 || echo "WARN: embedder bake failed"
else
  echo "embedder model present at ${REPO}/models/e5-small-v2"
fi

# pyright (deterministic, warn-don't-fail) — setup-eval lines 61-68.
# Needed for the C6 offline LSP promotion pass in preindex-promote.
if python -c "import pyright" 2>/dev/null || command -v pyright >/dev/null 2>&1; then
  echo "pyright already available"
else
  pip install pyright 2>/dev/null || echo "WARN: pyright install failed, LSP verification will use fallback"
fi

# ---------------------------------------------------------------------------
# (3) Build gt-index (host has Go 1.26 + CGO). setup-eval lines 74-91.
# ---------------------------------------------------------------------------
echo "=== [3] Build gt-index ==="
(
  cd "${REPO}/gt-index"
  # -ldflags version stamp mirrors GHA; harmless if git rev-parse fails.
  GTVER="$(git -C "${REPO}" rev-parse --short HEAD 2>/dev/null || echo dev)"
  CGO_ENABLED=1 go build -tags sqlite_fts5 -ldflags "-X main.version=${GTVER}" -o "${GT_INDEX_BIN}" ./cmd/gt-index/
  chmod +x "${GT_INDEX_BIN}"
)
"${GT_INDEX_BIN}" --version 2>/dev/null || echo "gt-index built (no --version flag)"
test -x "${GT_INDEX_BIN}" && echo "gt-index binary OK at ${GT_INDEX_BIN}" || { echo "FATAL: gt-index missing"; exit 1; }

# ---------------------------------------------------------------------------
# (4) Write config.toml — BYTE-FAITHFUL to canary_3arm.yml "Write OH config"
#     (lines 95-117). max_iterations=100, temperature=0.7, top_p=0.8.
#     api_key interpolated from ${DEEPSEEK_API_KEY} at runtime (heredoc is
#     unquoted so ${DEEPSEEK_API_KEY} expands; nothing else needs expansion).
# ---------------------------------------------------------------------------
echo "=== [4] Write config.toml ==="
cat > "${REPO}/config.toml" << TOML
[core]
max_iterations = 100
default_agent = "CodeActAgent"
# condenser disabled — GT evidence must survive in context

[llm.eval]
model = "deepseek/deepseek-v4-flash"
api_key = "${DEEPSEEK_API_KEY}"
base_url = "https://api.deepseek.com"
temperature = 0.7
top_p = 0.8
max_output_tokens = 65536
drop_params = true
caching_prompt = false
num_retries = 10
timeout = 300

[sandbox]
runtime_container_image = "ghcr.io/all-hands-ai/runtime:0.54-nikolaik"
TOML
echo "config.toml written to ${REPO}/config.toml"
# caching_prompt safety check (canary lines 167-180): false is mandatory for DeepSeek.
grep -q 'caching_prompt = false' "${REPO}/config.toml" \
  && echo "caching_prompt=false OK" \
  || { echo "FATAL: caching_prompt must be false for DeepSeek"; exit 1; }

# ---------------------------------------------------------------------------
# (5) Compute task image name + pull it.
#     canary "Compute task Docker image" / "Pull Docker image" (lines 122-131, 182-190):
#       TASK=beetbox__beets-5495
#       REPO_PART = cut -d'_' -f1            -> beetbox
#       REST      = sed s/^beetbox__//       -> beets-5495
#       BASENAME  = sweb.eval.x86_64.beetbox_1776_beets-5495
#       IMAGE     = docker.io/starryzhang/${BASENAME}:latest
#     (GHCR path skipped — no GH token on a bare codespace host; go straight to dockerhub.)
# ---------------------------------------------------------------------------
echo "=== [5] Pull task Docker image ==="
REPO_PART="$(echo "${TASK}" | cut -d'_' -f1)"
REST="$(echo "${TASK}" | sed "s/^${REPO_PART}__//")"
BASENAME="sweb.eval.x86_64.${REPO_PART}_1776_${REST}"
TASK_IMAGE="docker.io/${IMG_NS}/${BASENAME}:latest"
echo "task_image=${TASK_IMAGE}"
docker pull "${TASK_IMAGE}" || echo "WARN: pull failed for ${TASK} (${TASK_IMAGE}) — wrapper/eval may re-pull"

# Also pre-pull the OH runtime image so the wrapper does not stall on first turn.
docker pull ghcr.io/all-hands-ai/runtime:0.54-nikolaik || echo "WARN: OH runtime image pull failed — wrapper will pull on demand"

# ---------------------------------------------------------------------------
# (5b) Offline pre-index + LSP promotion (C6) — preindex-promote/action.yml.
#      Extract /testbed from the task image, gt-index it -> /tmp/gt_prebuilt.db,
#      run the Pyright precision pass, export GT_PREBUILT_GRAPH_DB.
#      WARN-don't-fail throughout: if the testbed extract or index fails, the
#      wrapper still rebuilds graph.db at runtime via GT_INDEX_BINARY +
#      GT_REBUILD_L1=1 (the GHA "Run agent" step works the same way). So this
#      block is best-effort: it improves edge precision but is not required.
# ---------------------------------------------------------------------------
echo "=== [5b] Offline pre-index target /testbed (C6, best-effort) ==="
GT_PREINDEX_DB="/tmp/gt_prebuilt.db"
PREINDEX_OK=0
CONTAINER_ID="$(docker create "${TASK_IMAGE}" 2>/dev/null || true)"
if [ -n "${CONTAINER_ID}" ]; then
  rm -rf /tmp/testbed_src; mkdir -p /tmp/testbed_src
  docker cp "${CONTAINER_ID}:/testbed/." /tmp/testbed_src/ 2>/dev/null || echo "WARN: could not copy /testbed"
  docker rm "${CONTAINER_ID}" >/dev/null 2>&1 || true
  # SANDBOX (legitimacy: "GT touches ZERO tests"): strip the testbed's TEST files/dirs from GT's
  # source view BEFORE indexing -> zero test nodes + empty assertions table -> no layer can surface
  # a test name. Proven: assertions 6709->0, is_test 4553->0. Agent's container keeps the full repo.
  find /tmp/testbed_src -type d \( -name tests -o -name test -o -name __tests__ -o -name testing -o -name spec \) -prune -exec rm -rf {} + 2>/dev/null || true
  find /tmp/testbed_src -type f \( -name 'test_*.py' -o -name '*_test.py' -o -name 'conftest.py' -o -name '*_test.go' -o -name '*.test.js' -o -name '*.test.ts' -o -name '*.spec.js' -o -name '*.spec.ts' -o -name '*_spec.rb' -o -name '*Test.java' \) -delete 2>/dev/null || true
  if [ -d /tmp/testbed_src ] && [ -n "$(find /tmp/testbed_src -name '*.py' -o -name '*.go' -o -name '*.js' -o -name '*.ts' -o -name '*.java' -o -name '*.rs' 2>/dev/null | head -1)" ]; then
    echo "Indexing /tmp/testbed_src (tests stripped — source-only sandbox) -> ${GT_PREINDEX_DB} ..."
    "${GT_INDEX_BIN}" -root /tmp/testbed_src -output "${GT_PREINDEX_DB}" 2>&1 | tail -5 || echo "WARN: gt-index pre-index failed (non-fatal)"
    if [ -f "${GT_PREINDEX_DB}" ]; then
      PREINDEX_OK=1
      # C6 LSP precision pass — ONE SURFACE, ALL 5 LANGUAGES.
      # Each language needs (a) its LSP server installed, (b) dependencies
      # resolved so the LSP can do cross-module definition lookups, (c) the
      # resolve.py enrichment pass. All best-effort / non-fatal.
      AMBIG="$(sqlite3 "${GT_PREINDEX_DB}" "SELECT COUNT(*) FROM edges WHERE resolution_method='name_match' AND type='CALLS'" 2>/dev/null || echo 0)"
      echo "Ambiguous name_match CALLS before LSP: ${AMBIG}"
      if [ "${AMBIG}" -gt 0 ]; then
        # Detect which languages are present in the graph
        LANGS="$(sqlite3 "${GT_PREINDEX_DB}" "SELECT DISTINCT language FROM nodes WHERE language IS NOT NULL" 2>/dev/null || echo "")"
        echo "Languages in graph: ${LANGS}"

        # Per-language: install deps (so LSP can resolve cross-module), then enrich
        for LANG in ${LANGS}; do
          case "${LANG}" in
            python)
              if command -v pyright >/dev/null 2>&1; then
                # Python: pip install -e . (so pyright sees the package)
                (cd /tmp/testbed_src && pip install -e . 2>/dev/null | tail -1) || true
                echo "  [${LANG}] enriching with pyright..."
                timeout 180 python -m groundtruth.resolve --db "${GT_PREINDEX_DB}" --root /tmp/testbed_src --resolve --lang python 2>&1 | tail -5 \
                  || echo "  WARN: ${LANG} LSP failed (non-fatal)"
              else
                echo "  [${LANG}] pyright not installed — skipping"
              fi
              ;;
            go)
              if command -v gopls >/dev/null 2>&1; then
                (cd /tmp/testbed_src && go mod download 2>/dev/null) || true
                echo "  [${LANG}] enriching with gopls..."
                timeout 180 python -m groundtruth.resolve --db "${GT_PREINDEX_DB}" --root /tmp/testbed_src --resolve --lang go 2>&1 | tail -5 \
                  || echo "  WARN: ${LANG} LSP failed (non-fatal)"
              else
                echo "  [${LANG}] gopls not installed — skipping"
              fi
              ;;
            rust)
              if command -v rust-analyzer >/dev/null 2>&1; then
                (cd /tmp/testbed_src && cargo fetch 2>/dev/null) || true
                echo "  [${LANG}] enriching with rust-analyzer..."
                timeout 180 python -m groundtruth.resolve --db "${GT_PREINDEX_DB}" --root /tmp/testbed_src --resolve --lang rust 2>&1 | tail -5 \
                  || echo "  WARN: ${LANG} LSP failed (non-fatal)"
              else
                echo "  [${LANG}] rust-analyzer not installed — skipping"
              fi
              ;;
            typescript|javascript)
              if command -v typescript-language-server >/dev/null 2>&1; then
                (cd /tmp/testbed_src && npm install --ignore-scripts 2>/dev/null | tail -1) || true
                echo "  [${LANG}] enriching with typescript-language-server..."
                timeout 180 python -m groundtruth.resolve --db "${GT_PREINDEX_DB}" --root /tmp/testbed_src --resolve --lang "${LANG}" 2>&1 | tail -5 \
                  || echo "  WARN: ${LANG} LSP failed (non-fatal)"
              else
                echo "  [${LANG}] typescript-language-server not installed — skipping"
              fi
              ;;
            *)
              echo "  [${LANG}] no LSP server configured — skipping"
              ;;
          esac
        done

        REMAIN="$(sqlite3 "${GT_PREINDEX_DB}" "SELECT COUNT(*) FROM edges WHERE resolution_method='name_match' AND type='CALLS'" 2>/dev/null || echo 0)"
        echo "Ambiguous name_match CALLS after LSP: ${REMAIN} (was ${AMBIG})"
      else
        echo "Skipping LSP pass (no ambiguous edges)"
      fi
    else
      echo "WARN: gt-index produced no pre-index db — wrapper will rebuild at runtime"
    fi
  else
    echo "WARN: no source files found in /testbed — wrapper will rebuild at runtime"
  fi
  # KEEP /tmp/testbed_src — the brief generator + L3b post-view need source
  # files on disk for BM25 content matching + contract extraction. Deleting
  # them killed the ranker ("0 candidates") on non-Python tasks where the
  # source isn't re-available inside the container.
  echo "Keeping /tmp/testbed_src for brief + L3b ($(du -sh /tmp/testbed_src 2>/dev/null | cut -f1))"
else
  echo "WARN: could not create container from ${TASK_IMAGE} — wrapper will rebuild graph.db at runtime"
fi

# ---------------------------------------------------------------------------
# (6) Export the EXACT env block from canary_3arm.yml "Run agent" (lines 205-230).
#     PYTHONPATH order matters: src : scripts/swebench : OpenHands.
#     GT_ROUTER_V2=live for arm=v2_live. DEEPSEEK_API_KEY passed through.
#     GT_PREBUILT_GRAPH_DB only exported if the pre-index produced a db.
# ---------------------------------------------------------------------------
echo "=== [6] Export GT_* env ==="
export PYTHONPATH="${REPO}/src:${REPO}/scripts/swebench:${OH_DIR}"
export GT_INDEX_BINARY="${GT_INDEX_BIN}"
export EVAL_CONDENSER=""
export GT_DEBUG_DIR="/tmp/gt_debug"
export GT_MAX_LLM_CALLS="300"
export GT_REBUILD_L1="1"
export GT_REBUILD_L3="1"
export GT_REBUILD_L3B="1"
export GT_REBUILD_L5="1"
export GT_LAYER_EVENTS="1"
export GT_STRUCTURED_EVENTS="1"
export GT_STRUCTURAL_NEXT_ACTION="1"
export GT_L3B_PRIMARY_EDGE="1"
export GT_L5_STRUCTURAL_UNVERIFIED="1"
export GT_L5_GOKU_EVENTS="0"
export GT_DEEP_LAYER_GROUNDED_METRICS="1"
export GT_L5B_SAFETY_REQUIRED="1"
# Runtime LSP promotion: when L6 reindexes a file after an agent edit, new
# name_match edges get LSP-promoted to lsp_verified immediately — so L3
# post-edit evidence uses high-quality edges, not stale name_match. The C6
# offline pass enriches the pre-built graph; this covers the RUNTIME delta.
# One surface: background_promotion.py dispatches to the same LSP servers
# (pyright/gopls/rust-analyzer/ts-language-server) via _LANG_TO_EXT.
export GT_LSP_VERIFY="1"
export GT_ROUTER_V2="${ROUTER_V2_MODE}"   # = live for v2_live
# DEEPSEEK_API_KEY already in env (passed by operator) — re-export for child procs.
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY}"
if [ "${PREINDEX_OK}" = "1" ] && [ -f "${GT_PREINDEX_DB}" ]; then
  export GT_PREBUILT_GRAPH_DB="${GT_PREINDEX_DB}"
  echo "GT_PREBUILT_GRAPH_DB=${GT_PREINDEX_DB}"
  # HOST-PRIMARY brief (oh_gt_full_wrapper.py:7125): the IN-CONTAINER brief runner has NO
  # onnxruntime/e5 model -> semantic SILENTLY 0 (run14: "No semantic embedder ... scores will be 0"),
  # which kills run_v74 anchoring and the localization fixes. Point the wrapper at the HOST source +
  # HOST graph.db so the brief is generated on the HOST (where GT_MODELS_ROOT + onnxruntime exist),
  # making semantic REAL and GT_REQUIRE_EMBEDDER actually fail-closed.
  export GT_HOST_SRC_ROOT="/tmp/testbed_src"
  export GT_HOST_GRAPH_DB="${GT_PREINDEX_DB}"
  echo "GT_HOST_SRC_ROOT=/tmp/testbed_src GT_HOST_GRAPH_DB=${GT_PREINDEX_DB} (host-primary brief -> semantic ON)"
else
  echo "GT_PREBUILT_GRAPH_DB not set — wrapper rebuilds graph.db at runtime (GT_REBUILD_L1=1)"
fi

# --- gt_trial §1: FULL-STACK environment (semantic ON + FTS5) or ABORT on degrade ---
# Semantic is the BRIEFING-critical invariant: a half-on pipeline (W_SEM=0) gives worthless
# localization numbers. The e5-small-v2 ONNX model is baked at ${REPO}/models (no torch).
export GT_MODELS_ROOT="${GT_MODELS_ROOT:-${REPO}/models}"
export GT_FORCE_ONNX_EMBEDDER="${GT_FORCE_ONNX_EMBEDDER:-1}"   # both halves on the identical ONNX surface
export GT_REQUIRE_EMBEDDER="${GT_REQUIRE_EMBEDDER:-1}"         # RAISE if semantic would zero (no silent W_SEM=0)
export GT_REQUIRE_FTS5="${GT_REQUIRE_FTS5:-1}"                 # nodes_fts built+populated+MATCH or gt-index aborts
# Strictest gates (LSP launch/resolve + per-dimension full-stack incl lsp_edges/assertions) are
# opt-in via GT_STRICT=1 — arm once the in-container LSP surface + those dimensions are confirmed.
if [ "${GT_STRICT:-0}" = "1" ]; then
  export GT_REQUIRE_LSP=1
  export GT_REQUIRE_FULL_STACK=1
fi
echo "ENV gates: EMBEDDER=on(ONNX) FTS5=on STRICT=${GT_STRICT:-0} MODELS_ROOT=${GT_MODELS_ROOT}"

# arm gating (canary "Run agent" lines 233-238): v2_live => GT arm, no GT_BASELINE.
if [ "${BASELINE_FLAG}" = "true" ]; then
  echo "=== BASELINE ARM ==="
  export GT_BASELINE=1
else
  echo "=== GT ARM (router_v2=${ROUTER_V2_MODE}) ==="
fi

# ---------------------------------------------------------------------------
# (7) Run the agent — EXACT wrapper invocation from canary "Run agent" (lines 239-247).
# ---------------------------------------------------------------------------
echo "=== [7] Run agent (oh_gt_full_wrapper.py) ==="
(
  cd "${REPO}"
  python scripts/swebench/oh_gt_full_wrapper.py \
      --instance-ids "${TASK}" \
      -l eval \
      -i 100 \
      --eval-num-workers 1 \
      --eval-output-dir /tmp/results \
      --dataset "${DATASET}" \
      --split "${SPLIT}" \
      2>&1 | tee "${LOGFILE}"
)

# Post-run LLM-call sanity (canary lines 249-251).
LLM_CALLS="$(grep -c 'GT_COST.*call=' "${LOGFILE}" 2>/dev/null || echo 0)"
echo "POST-RUN: llm_calls=${LLM_CALLS}"
if [ "${LLM_CALLS}" -lt 2 ]; then
  echo "WARN: ${LLM_CALLS} LLM calls — run may be broken (GHA would FATAL here)"
fi

# ---------------------------------------------------------------------------
# (8) Copy artifacts to ${REPO}/.csout/ for retrieval.
#     output.jsonl (per-task) + gt_interactions_*.jsonl + supporting logs.
# ---------------------------------------------------------------------------
echo "=== [8] Stage artifacts to ${CSOUT_DIR} ==="
# output.jsonl (there may be one per task subdir under /tmp/results)
find /tmp/results -name 'output.jsonl' -print0 2>/dev/null | while IFS= read -r -d '' f; do
  d="$(dirname "${f}")"
  base="$(basename "${d}")"
  mkdir -p "${CSOUT_DIR}/${base}"
  cp "${f}" "${CSOUT_DIR}/${base}/output.jsonl" 2>/dev/null || true
  # Stage per-task GT interaction + layer logs beside it (canary lines 278-284).
  cp /tmp/gt_interactions_*.jsonl "${CSOUT_DIR}/${base}/" 2>/dev/null || true
  cp /tmp/gt_layer_events_*.jsonl "${CSOUT_DIR}/${base}/" 2>/dev/null || true
done
# Top-level convenience copies (flat) so the caller finds them without descending.
cp /tmp/gt_interactions_*.jsonl "${CSOUT_DIR}/" 2>/dev/null || echo "WARN: no /tmp/gt_interactions_*.jsonl to copy"
cp "${LOGFILE}" "${CSOUT_DIR}/full_run.log" 2>/dev/null || true
cp /tmp/gt_debug/gt_hooks.log "${CSOUT_DIR}/" 2>/dev/null || true
[ -f "${GT_PREINDEX_DB}" ] && cp "${GT_PREINDEX_DB}" "${CSOUT_DIR}/gt_prebuilt.db" 2>/dev/null || true

echo "interaction_files=$(ls /tmp/gt_interactions_*.jsonl 2>/dev/null | wc -l)"

# ---------------------------------------------------------------------------
# (9) OFFICIAL EVAL — Microsoft SWE-bench-Live harness -> RESOLVED verdict.
#     gt_trial §2: a run with no verdict instrument is unfalsifiable -> not allowed.
#     CARDINAL: never a custom eval. Set GT_EVAL=0 to skip (e.g. patchless smoke).
#     Set GT_EVAL_GOLD=1 to grade the GOLD patch instead (instrument self-test).
# ---------------------------------------------------------------------------
if [ "${GT_EVAL:-1}" = "1" ]; then
  echo "=== [9] Official eval (SWE-bench-Live harness) ==="
  EVAL_RUN_ID="gt_$(echo "${TASK}" | tr -cd 'a-zA-Z0-9')_$(date +%s)"
  PREDS="/tmp/gt_predictions.jsonl"
  if [ "${GT_EVAL_GOLD:-0}" = "1" ]; then
    PREDS_ARG="gold"; echo "[9] grading GOLD patch (instrument self-test)"
  else
    PREDS_ARG="${PREDS}"
    python - "${TASK}" "${PREDS}" <<'PYEOF'
import json, sys, glob, re
task, preds = sys.argv[1], sys.argv[2]

# PATCH HYGIENE — strip GENERATED/scaffold artifacts the OH whole-workdir `git diff`
# sweeps into the model_patch (the agent runs pytest -> .coverage/coverage.xml/pytest.xml;
# OpenHands writes .openhands/). These pollute the diff, and a binary `.coverage` or a
# tail-truncated coverage.xml hunk makes `git apply` fail -> the eval ERRORS on a
# possibly-correct SOURCE fix (observed: mesa-2394 errored, xarray-9586 diff buried).
# Keep ONLY real source-file sections: drop everything before the first `diff --git`
# (orphaned/truncated hunks), drop junk paths, drop binary sections. Generalized
# (pattern-based, any repo/lang); touches no gold/test/FAIL_TO_PASS — pure hygiene.
_JUNK = re.compile(r'(^|/)(\.coverage[^/]*|coverage\.xml|pytest\.xml|\.pytest_cache/|'
                   r'__pycache__/|node_modules/|\.openhands/)|\.pyc$')
def clean_patch(p):
    if not p:
        return p
    idxs = [m.start() for m in re.finditer(r'(?m)^diff --git a/\S+ b/\S+', p)]
    if not idxs:
        return ""  # no recoverable source section
    out = []
    for i, s in enumerate(idxs):
        e = idxs[i + 1] if i + 1 < len(idxs) else len(p)
        sec = p[s:e]
        path = re.match(r'diff --git a/(\S+) b/(\S+)', sec).group(2)
        is_bin = ('GIT binary patch' in sec) or bool(re.search(r'(?m)^Binary files ', sec))
        if _JUNK.search(path) or is_bin:
            continue
        out.append(sec)
    cleaned = "".join(out)
    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"
    return cleaned

patch = ""
for f in glob.glob("/tmp/results/**/output.jsonl", recursive=True):
    for line in open(f, encoding="utf-8"):
        try: d = json.loads(line)
        except Exception: continue
        if d.get("instance_id") == task or ("test_result" in d):
            patch = (d.get("test_result") or {}).get("git_patch", "") or d.get("git_patch", "") or patch
raw_len = len(patch)
patch = clean_patch(patch)
with open(preds, "w", encoding="utf-8") as w:
    w.write(json.dumps({"instance_id": task, "model_name_or_path": "gt", "model_patch": patch}) + "\n")
print(f"[9] predictions: instance={task} raw_patch_len={raw_len} cleaned_patch_len={len(patch)} (source-only)")
PYEOF
  fi
  ( cd "${REPO}" && python -m swebench.harness.run_evaluation \
      --dataset_name "${DATASET}" \
      --split "${SPLIT}" \
      --instance_ids "${TASK}" \
      --namespace "${IMG_NS}" \
      --predictions_path "${PREDS_ARG}" \
      --max_workers 1 \
      --run_id "${EVAL_RUN_ID}" 2>&1 | tee -a "${LOGFILE}" ) || echo "WARN: eval harness returned nonzero"
  REPORT="$(find "${REPO}" /tmp -maxdepth 3 -name "*${EVAL_RUN_ID}*.json" 2>/dev/null | head -1)"
  if [ -n "${REPORT}" ] && [ -f "${REPORT}" ]; then
    cp "${REPORT}" "${CSOUT_DIR}/report.json" 2>/dev/null || true
    python - "${REPORT}" "${TASK}" <<'PYEOF'
import json, sys
r = json.load(open(sys.argv[1])); task = sys.argv[2]
resolved = (task in (r.get("resolved_ids") or [])) or (r.get("resolved_instances", 0) >= 1)
print(f"=== EVAL VERDICT === task={task} RESOLVED={bool(resolved)}")
keys = ('resolved_instances','unresolved_instances','error_instances','completed_instances',
        'total_instances','resolved_ids','unresolved_ids','error_ids')
print("report:", json.dumps({k: r[k] for k in keys if k in r}))
PYEOF
  else
    echo "=== EVAL VERDICT === report NOT FOUND for run_id=${EVAL_RUN_ID} (eval failed — check log above) ==="
  fi
fi

echo "=== DONE. Artifacts in ${CSOUT_DIR} ==="
ls -la "${CSOUT_DIR}" 2>/dev/null || true
