#!/usr/bin/env bash
###############################################################################
# codespace_deepswe_run.sh — LIVE DeepSWE/pier run on GitHub Codespaces,
# streamed to a PUBLIC ngrok SSE URL so the run is watchable LINE-BY-LINE in the
# Claude chat (anti-drift: Claude curls the URL in 55s windows and narrates what
# GT does). Mirrors .github/workflows/deepswe_trial.yml exactly:
#   * fresh substrate per task (graph.db + LSP precision pass + source extract)
#   * host-side v1r brief wired via GT_GRAPH_DB / GT_REPO_ROOT
#   * preflight HARD gate (abort on a degraded/half-on stack)
#   * pier run with the FULL GT engine: oracle two-lane + edit_risk + brief
#
# This is the DeepSWE-native path (Path 3): Pier + GTMiniSweAgent + gt_mini_patch.
# The embedder is Alibaba gte-modernbert-base/768 (the localization default), NOT
# e5 (e5 is only the memory-store fallback). gt-index is BUILT FROM SOURCE here
# (sqlite_fts5) — never the harneet2512 release URL (hbali-stack is the repo).
#
# GOAL — match BEHAVIOR with ARCHITECTURE across ALL 5 languages (py/go/ts/js/rust),
# NOT "resolve". The live watch verifies, per language, that GT does what gt_gt says:
# brief localizes, <gt-evidence> delivers verified callers/contracts, the oracle steers
# only when warranted, edit_risk fires correctly, the consumption witness holds. RESOLVED
# is incidental — a right trajectory on a non-resolved task is still a GT win. The script
# is LANGUAGE-AGNOSTIC: it reads the task's language from task.toml and dispatches the
# matching LSP server, so the SAME run works for a go/rust/ts/js task as for python.
#
# RUN (in a Codespace terminal):
#   export DEEPSEEK_API_KEY=sk-xxxx
#   export NGROK_AUTHTOKEN=xxxx          # free ngrok acct -> authtoken (for the live URL)
#   GT_TASK=fastapi-implicit-head-options bash railway/codespace_deepswe_run.sh
#
# Then paste the `curl -N '<url>'` line it prints to Claude; Claude streams it live.
###############################################################################
set -o pipefail
REPO="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
cd "$REPO"

TASK="${GT_TASK:-fastapi-implicit-head-options}"   # a python DeepSWE task (34 available)
MODEL="${GT_MODEL:-deepseek/deepseek-v4-flash}"
TASK_DIR="deepswe-bench/tasks/${TASK}"
LOG="/tmp/gt_debug/deepswe_${TASK}.log"
mkdir -p /tmp/gt_debug /tmp/gt

# UTF-8 everywhere — on Windows-local, git/rg subprocess output + pier's braille spinner
# crash under the cp1252 locale; UTF-8 mode fixes both. No-op on Linux/Codespaces.
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
# --- full-stack enforcement (deepswe_trial.yml env) + the recent hardening flags ---
export GT_REQUIRE_FULL_POTENTIAL=1 GT_REQUIRE_FTS5=1
export GT_FORCE_ONNX_EMBEDDER=1 GT_REQUIRE_EMBEDDER=1
export GT_FORBID_PREBUILT_GRAPH=1
export GT_MODELS_ROOT="${GT_MODELS_ROOT:-${REPO}/models}"
# Turn the structural edit-risk steer ON (default OFF) + oracle route ON (default on).
# These are the surfaces the recent LIPI hardened; the OH path can't exercise them.
export GT_VERIFY_STRUCTURAL_RISK=1
export GT_ORACLE_ROUTE=1
# deep 8-dp metrics (CLAUDE.md mandatory)
export GT_DEEP_LAYER_GROUNDED_METRICS=1 GT_STRUCTURED_EVENTS=1 GT_LAYER_EVENTS=1
export GT_DEBUG_DIR=/tmp/gt_debug

echo "── preflight ──"
[ -n "${DEEPSEEK_API_KEY:-}" ] || { echo "FATAL: DEEPSEEK_API_KEY unset"; exit 1; }
docker info >/dev/null 2>&1 || { echo "FATAL: docker daemon down (pier needs --env docker)"; exit 1; }
[ -d "$TASK_DIR" ] || { echo "FATAL: no task $TASK_DIR"; echo "available:"; ls deepswe-bench/tasks 2>/dev/null | head -20; exit 1; }
command -v go >/dev/null 2>&1 || { echo "FATAL: go missing (need it to build gt-index)"; exit 1; }
[ -n "${NGROK_AUTHTOKEN:-}" ] || echo "WARN: NGROK_AUTHTOKEN unset — log_relay degrades to LOCAL passthrough (no public URL; Claude can't watch live)."
echo "  task=$TASK  model=$MODEL  lang=$(python3 -c "import tomllib;print(tomllib.load(open('$TASK_DIR/task.toml','rb')).get('metadata',{}).get('language','?'))" 2>/dev/null)"

echo "── install pier + groundtruth + embedder + pyright + ngrok ──"
pip install -q "datacurve-pier==0.2.0" 2>&1 | tail -1 || echo "WARN: pier install"
pip install -q -e . 2>&1 | tail -1 || true
pip install -q onnxruntime tokenizers huggingface_hub pyright 2>&1 | tail -1 || echo "WARN: py deps"
# gte-modernbert-base PRIMARY (768) + e5 fallback. setup_models.py bakes both.
python scripts/setup_models.py 2>&1 | tail -3 || echo "WARN: embedder bake"
[ -f "${REPO}/models/gte-modernbert-base/model.onnx" ] || echo "WARN: gte primary missing — GT_REQUIRE_EMBEDDER will fail-closed at brief."
command -v rg >/dev/null 2>&1 || sudo apt-get install -y ripgrep >/dev/null 2>&1 || echo "WARN: ripgrep (GT grep-seed falls back to slow py walk)"

# ALL 5-language LSP servers (py/go/ts/js/rust) so the precision pass is real for ANY
# task language — the all-language behavior-parity goal. ONE surface, per-lang dispatch.
echo "── install LSP servers (5-language) ──"
go install golang.org/x/tools/gopls@latest 2>/dev/null && sudo cp "$(go env GOPATH)/bin/gopls" /usr/local/bin/ 2>/dev/null && echo "  gopls OK" || echo "  WARN: gopls"
npm install -g typescript typescript-language-server 2>/dev/null | tail -1 && echo "  ts-language-server OK" || echo "  WARN: ts-language-server"
curl -fsSL https://github.com/rust-lang/rust-analyzer/releases/latest/download/rust-analyzer-x86_64-unknown-linux-gnu.gz 2>/dev/null | gunzip -c 2>/dev/null | sudo tee /usr/local/bin/rust-analyzer >/dev/null 2>&1 && sudo chmod +x /usr/local/bin/rust-analyzer && echo "  rust-analyzer OK" || echo "  WARN: rust-analyzer"
for s in pyright-langserver gopls rust-analyzer typescript-language-server; do command -v "$s" >/dev/null 2>&1 && echo "  $s OK" || echo "  $s MISSING"; done
# ngrok for the public SSE relay (so the run streams to a URL Claude can curl)
if ! command -v ngrok >/dev/null 2>&1; then
  curl -fsSL https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz 2>/dev/null \
    | tar xz -C /tmp 2>/dev/null && sudo mv /tmp/ngrok /usr/local/bin/ 2>/dev/null \
    && echo "  ngrok installed" || echo "WARN: ngrok install failed — relay local-only"
fi
[ -n "${NGROK_AUTHTOKEN:-}" ] && ngrok config add-authtoken "$NGROK_AUTHTOKEN" >/dev/null 2>&1 || true

echo "── build gt-index from SOURCE (sqlite_fts5) ──"
( cd gt-index && CGO_ENABLED=1 go build -tags sqlite_fts5 -o gt-index-linux ./cmd/gt-index/ && chmod +x gt-index-linux ) \
  || { echo "FATAL: gt-index build failed"; exit 1; }

echo "── substrate: graph.db + LSP + source from the task image (Point A) ──"
IMG=$(python3 -c "import tomllib;print(tomllib.load(open('$TASK_DIR/task.toml','rb')).get('environment',{}).get('docker_image',''))")
[ -n "$IMG" ] || { echo "FATAL: no docker_image in task.toml"; exit 1; }
echo "  task image: $IMG"
for i in 1 2 3 4; do docker pull "$IMG" && break || { echo "  pull $i failed, retry…"; sleep 15; }; done
docker image inspect "$IMG" >/dev/null 2>&1 || { echo "FATAL: image pull failed"; exit 1; }
docker rm -f gtsrc 2>/dev/null || true
docker run -d --name gtsrc "$IMG" sleep 2400 >/dev/null
ROOT=$(docker exec gtsrc bash -c 'for d in /home/user /testbed /workspace /app /repo; do [ -d "$d/.git" ] && echo "$d" && break; done'); ROOT=${ROOT:-/testbed}
echo "  repo root: $ROOT"
docker cp gt-index/gt-index-linux gtsrc:/tmp/gt-index && docker exec gtsrc chmod +x /tmp/gt-index
docker exec gtsrc /tmp/gt-index -root="$ROOT" -output=/tmp/graph.db 2>&1 | tail -3 || echo "WARN: gt-index run"
docker cp gtsrc:/tmp/graph.db /tmp/gt/graph.db || { echo "FATAL: no graph.db"; exit 1; }
mkdir -p /tmp/gt/src && docker cp "gtsrc:$ROOT/." /tmp/gt/src 2>/dev/null || echo "WARN: no source"
docker rm -f gtsrc 2>/dev/null || true
LANG=$(python3 -c "import tomllib;print(tomllib.load(open('$TASK_DIR/task.toml','rb')).get('metadata',{}).get('language','python'))")
echo "  LSP precision pass (lang=$LANG)…"
python -m groundtruth.resolve --db /tmp/gt/graph.db --root /tmp/gt/src --resolve --lang "$LANG" 2>&1 | tail -3 || echo "WARN: lsp"
echo "  lsp edges: $(sqlite3 /tmp/gt/graph.db "SELECT COUNT(*) FROM edges WHERE resolution_method='lsp'" 2>/dev/null || echo '?')"
export GT_GRAPH_DB=/tmp/gt/graph.db GT_REPO_ROOT=/tmp/gt/src

echo "── preflight HARD gate (abort on a degraded stack) ──"
python scripts/verify/preflight_pipeline.py --db /tmp/gt/graph.db --root /tmp/gt/src \
  || { echo "FATAL: preflight failed — refusing a half-on run"; exit 1; }

echo ""
echo "==============================================================="
echo " LIVE: the next lines stream to the ngrok SSE URL below."
echo " Paste the  curl -N '<url>'  line to Claude to watch live."
echo "==============================================================="
# G03/G04 (HIGH): pier DROPS host `export` — only `--ae KEY=VAL` reaches the in-container
# agent interpreter. Without it the structural edit-risk axis + oracle two-lane route +
# deep telemetry run with EMPTY GT env in-container. Forward them via the single-source
# `--ae` block (shared with deepswe_full.yml so trial+full can't drift). GT_C_OUT=/tmp keeps
# the telemetry sinks writable in-container (a durable host-mount = gap G11, still owed).
# G11: a WRITABLE host-mount for the in-container deep-telemetry sinks — without it the
# producers default into the container and DIE with it (the 8-dp metrics lost). Mirror
# deepswe_full.yml's contract (ServiceVolumeConfig type/source/target/read_only): bind
# /tmp/gt_out -> /gt_out, point GT_C_OUT (the block's sink dir) at it, copy out after.
HOST_GT_OUT=/tmp/gt_out
mkdir -p "$HOST_GT_OUT"
export GT_C_OUT=/gt_out
MOUNTS_JSON="[{\"type\":\"bind\",\"source\":\"${HOST_GT_OUT}\",\"target\":\"/gt_out\",\"read_only\":false}"
# G05 (full): MOUNT the gt-index binary into the container so L6 per-turn reindex actually
# runs (the ~49MB binary exceeds the 16MB bake cap, so it cannot be base64-injected — a
# read-only bind mount is the durable fix). gt_mini_patch defaults GT_INDEX_BIN=/tmp/gt-index.
GT_INDEX_BIN_HOST="${REPO}/gt-index/gt-index-linux"
if [ -x "$GT_INDEX_BIN_HOST" ]; then
  MOUNTS_JSON="${MOUNTS_JSON},{\"type\":\"bind\",\"source\":\"${GT_INDEX_BIN_HOST}\",\"target\":\"/tmp/gt-index\",\"read_only\":true}"
  echo "  L6: mounting gt-index binary -> /tmp/gt-index (per-turn reindex ENABLED)"
else
  echo "  L6: no gt-index binary at ${GT_INDEX_BIN_HOST} — per-turn reindex DISABLED (fail-loud telemetry in-container)"
fi
MOUNTS_JSON="${MOUNTS_JSON}]"
# shellcheck source=artifact_deepswe/gt_integration/gt_ae_block.sh
source artifact_deepswe/gt_integration/gt_ae_block.sh
# pier output -> terminal log AND the ngrok SSE relay (log_relay prints the URL).
pier run \
  -p "$TASK_DIR" \
  --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent \
  --model "$MODEL" \
  --env docker -y \
  "${GT_AE_ARGS[@]}" \
  --mounts-json "${MOUNTS_JSON}" \
  --ak config_file=artifact_deepswe/gt_integration/deepswe_gt_pier.yaml \
  2>&1 | tee "$LOG" | python -u scripts/log_relay.py
# G11: recover the deep 8-dp telemetry the in-container producers wrote to the mount.
mkdir -p /tmp/gt_debug
for _f in gt_oracle_events.jsonl gt_runtime_ledger.jsonl gt_hook_fire_counts.json; do
  cp "$HOST_GT_OUT/$_f" /tmp/gt_debug/ 2>/dev/null || true
done

echo ""
echo "── outcome + deep metrics ──"
python3 scripts/verify/deepswe_outcome.py jobs 2>&1 | tail -20 || echo "WARN: outcome extract"
PYTHONPATH="${REPO}/src:${PYTHONPATH:-}" python scripts/swebench/gt_deep_metrics.py "$TASK" jobs --log "$LOG" 2>&1 | tail -3 || echo "WARN: deep metrics"
cp /tmp/gt/delivered_instruction.txt /tmp/gt_debug/ 2>/dev/null || true
echo "── DONE. logs in /tmp/gt_debug, trajectory in jobs/ ──"
