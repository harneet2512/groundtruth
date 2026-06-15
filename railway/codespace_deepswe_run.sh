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
LANG=$(python3 -c "import tomllib;print(tomllib.load(open('$TASK_DIR/task.toml','rb')).get('metadata',{}).get('language','python'))")
# #4 (Go/Rust LSP dep-env): the HOST precision pass needs the task's module/crate cache —
# gopls needs GOMODCACHE, rust-analyzer needs cargo+rustup, else 0 conversions on go/rust
# (the §17.3 dark-LSP gap). Extract from the live task container BEFORE removing it (mirror
# deepswe_full.yml:815-879), then point the host LSP at them. No-op for py/ts/js.
if [ "$LANG" = "go" ] || [ "$LANG" = "rust" ]; then
  mkdir -p /tmp/gt/deps/gomodcache /tmp/gt/deps/cargo /tmp/gt/deps/rustup
  GOMOD=$(docker exec gtsrc sh -lc 'go env GOMODCACHE 2>/dev/null' | tr -d '\r')
  for CAND in "$GOMOD" "$ROOT/go/pkg/mod" /root/go/pkg/mod /home/user/go/pkg/mod; do
    [ -n "$CAND" ] && docker exec gtsrc test -d "$CAND" 2>/dev/null \
      && docker cp "gtsrc:${CAND}/." /tmp/gt/deps/gomodcache 2>/dev/null && break || true
  done
  for CAND in /root/.cargo /home/user/.cargo; do
    docker exec gtsrc test -d "$CAND" 2>/dev/null \
      && docker cp "gtsrc:${CAND}/." /tmp/gt/deps/cargo 2>/dev/null && break || true
  done
  for CAND in /root/.rustup /home/user/.rustup; do
    docker exec gtsrc test -d "$CAND" 2>/dev/null \
      && docker cp "gtsrc:${CAND}/." /tmp/gt/deps/rustup 2>/dev/null && break || true
  done
  export GOMODCACHE=/tmp/gt/deps/gomodcache GOFLAGS=-mod=mod \
    CARGO_HOME=/tmp/gt/deps/cargo RUSTUP_HOME=/tmp/gt/deps/rustup
  echo "  deps: gomodcache=$(du -sh /tmp/gt/deps/gomodcache 2>/dev/null|cut -f1) cargo=$(du -sh /tmp/gt/deps/cargo 2>/dev/null|cut -f1)"
fi
docker rm -f gtsrc 2>/dev/null || true
echo "  LSP precision pass (lang=$LANG)…"
python -m groundtruth.resolve --db /tmp/gt/graph.db --root /tmp/gt/src --resolve --lang "$LANG" 2>&1 | tail -3 || echo "WARN: lsp"
echo "  lsp edges: $(sqlite3 /tmp/gt/graph.db "SELECT COUNT(*) FROM edges WHERE resolution_method='lsp'" 2>/dev/null || echo '?')"
export GT_GRAPH_DB=/tmp/gt/graph.db GT_REPO_ROOT=/tmp/gt/src
# ── SUBSTRATE-CONSUME (gt_gt §17.2 / §6 substrate note): the graph is MOUNTED, never baked.
# Produce brief.txt host-side (mirror gt_run_proof.emit_brief), then hand off the whole /tmp/gt
# as the authoritative substrate via GT_HOST_GRAPH_DB + GT_CERT_DIR. gt_agent sees _substrate_active
# -> SKIPS the b64 bake (the 16MB BuildKit cap that killed the agent at 0 steps) and reads the
# mounted graph; gt_mini_patch._db_path resolves GT_HOST_GRAPH_DB. /tmp/gt is IDENTITY-mounted
# (host path == container path) so the same path is valid for the host-side brief read AND the
# in-container per-turn graph read.
echo "── brief.txt (host-side v1r brief -> the mounted substrate) ──"
ISSUE_FILE="${TASK_DIR}/instruction.md"
GT_GRAPH_DB=/tmp/gt/graph.db GT_REPO_ROOT=/tmp/gt/src PYTHONPATH=src python - "$ISSUE_FILE" <<'PYBRIEF' || echo "WARN: brief.txt gen failed (agent degrades to no-brief)"
import sys
from groundtruth.runtime.brief_cache import get_or_generate
issue = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else ""
r = get_or_generate("/tmp/gt", issue, "/tmp/gt/src", "/tmp/gt/graph.db")
bt = (r.get("brief_text") or "").strip()
open("/tmp/gt/brief.txt", "w", encoding="utf-8").write(bt)
print("  brief.txt:", len(bt), "chars  sha=%s" % (r.get("brief_sha256", "")[:12]))
PYBRIEF
export GT_HOST_GRAPH_DB=/tmp/gt/graph.db GT_CERT_DIR=/tmp/gt

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
# chmod 777: the DeepSWE task-image user is arbitrary (varied/arbitrary uid) and the
# in-container 8-dp telemetry producers (GT_ORACLE_EVENTS/GT_RUNTIME_LEDGER/
# GT_HOOK_FIRE_COUNTS -> /gt_out/*) must be able to append. A host dir at the default
# umask 0755 is owned by the codespace user and is effectively read-only to a different
# in-container uid -> the writes fail, G11 silently does nothing, and the copy-out below
# swallows the absence. Mirror deepswe_full.yml's contract exactly.
mkdir -p "$HOST_GT_OUT" && chmod 777 "$HOST_GT_OUT"
export GT_C_OUT=/gt_out
MOUNTS_JSON="[{\"type\":\"bind\",\"source\":\"${HOST_GT_OUT}\",\"target\":\"/gt_out\",\"read_only\":false}"
# SUBSTRATE MOUNT (the graph-delivery fix): bind the whole /tmp/gt (graph.db + brief.txt + src)
# into the container at the SAME path (identity) read-only. With GT_HOST_GRAPH_DB/GT_CERT_DIR set
# (above + forwarded via --ae below) gt_agent substrate-CONSUMES this instead of baking the graph
# as b64 RUN layers — no 16MB BuildKit cap (the thing that killed the agent at 0 steps), and the
# per-turn producers read the authoritative mounted graph.
MOUNTS_JSON="${MOUNTS_JSON},{\"type\":\"bind\",\"source\":\"/tmp/gt\",\"target\":\"/tmp/gt\",\"read_only\":true}"
# G05: MOUNT the gt-index binary for parity. NOTE: in substrate-consume mode L6 reindex is gated
# OFF (the mounted graph is authoritative/immutable, §6 substrate note) — the per-turn pillars
# read the ONE mounted graph unchanged; do not report L6 as ENABLED here.
GT_INDEX_BIN_HOST="${REPO}/gt-index/gt-index-linux"
if [ -x "$GT_INDEX_BIN_HOST" ]; then
  MOUNTS_JSON="${MOUNTS_JSON},{\"type\":\"bind\",\"source\":\"${GT_INDEX_BIN_HOST}\",\"target\":\"/tmp/gt-index\",\"read_only\":true}"
  echo "  L6: mounted gt-index binary -> /tmp/gt-index (reindex runs only when an in-container /tmp/graph.db is present)"
else
  echo "  L6: no gt-index binary at ${GT_INDEX_BIN_HOST} — per-turn reindex DISABLED (fail-loud telemetry in-container)"
fi
MOUNTS_JSON="${MOUNTS_JSON}]"
# shellcheck source=artifact_deepswe/gt_integration/gt_ae_block.sh
source artifact_deepswe/gt_integration/gt_ae_block.sh
# Hand the substrate off to the in-container agent: GT_HOST_GRAPH_DB + GT_CERT_DIR make
# gt_agent (build-time, skip bake) + gt_mini_patch (per-turn _db_path) consume the mounted
# /tmp/gt. Identity-mounted, so /tmp/gt/{graph.db,brief.txt} resolve at the same path in-container.
GT_AE_ARGS+=(--ae "GT_HOST_GRAPH_DB=/tmp/gt/graph.db" --ae "GT_CERT_DIR=/tmp/gt")
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
# Loud: count what was recovered and WARN on zero — a silent empty copy means the
# in-container writes never landed (e.g. the mount was not writable to the task uid),
# which the bare `|| true` would otherwise hide and leave the run falsely green.
mkdir -p /tmp/gt_debug
_gt_recovered=0
for _f in gt_oracle_events.jsonl gt_runtime_ledger.jsonl gt_hook_fire_counts.json; do
  if [ -s "$HOST_GT_OUT/$_f" ]; then
    cp "$HOST_GT_OUT/$_f" /tmp/gt_debug/ && _gt_recovered=$((_gt_recovered + 1))
  fi
done
if [ "$_gt_recovered" -eq 0 ]; then
  echo "  WARN: 0 deep-telemetry files recovered from $HOST_GT_OUT — in-container 8-dp" \
       "sinks wrote nothing (check the mount is writable to the task uid; G11)." >&2
else
  echo "  G11: recovered $_gt_recovered/3 deep-telemetry sink(s) from the mount."
fi

echo ""
echo "── outcome + deep metrics ──"
python3 scripts/verify/deepswe_outcome.py jobs 2>&1 | tail -20 || echo "WARN: outcome extract"
PYTHONPATH="${REPO}/src:${PYTHONPATH:-}" python scripts/swebench/gt_deep_metrics.py "$TASK" jobs --log "$LOG" 2>&1 | tail -3 || echo "WARN: deep metrics"
cp /tmp/gt/delivered_instruction.txt /tmp/gt_debug/ 2>/dev/null || true
echo "── DONE. logs in /tmp/gt_debug, trajectory in jobs/ ──"
