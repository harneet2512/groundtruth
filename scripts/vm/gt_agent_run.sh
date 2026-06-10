#!/usr/bin/env bash
# scripts/vm/gt_agent_run.sh — GENERIC VM **agent-run** sweeper (DeepSWE-113, GT-on).
#
# Manifest-driven port of .github/workflows/deepswe_full.yml's per-task AGENT flow
# (byte-mirrored semantics), built on scripts/vm/gt_proof_sweep.sh's runner discipline
# (same manifest reader, same GHCR-first pull, same row/resume idempotence, same
# disk-bounded prune-as-you-go). Per task:
#   (a) GT substrate proof — pull task image (GHCR-first, retries, classified),
#       extract the repo source, materialize THIS task's issue (instruction.md first,
#       task.toml fallback, GT_ISSUE_MISSING fail-closed), run the IDENTICAL pinned
#       gt-run-proof command (repo /work:ro, 8 proof flags + GT_ISSUE_FILE), verify
#       the 8-artifact contract (incl. brief.txt). OR consume an existing proof dir:
#       REUSE_PROOF_DIR=<dir> skips gt-run-proof when <dir>/<instance_id>/ already
#       holds ALL 8 artifacts (a proof-sweep dir lacking brief.txt falls through to
#       a fresh proof — the agent path REQUIRES the issue-scoped brief).
#   (b) pier install check (once, preflight: datacurve-pier==0.2.0 — the pinned
#       --ae/--mounts-json plumbing contract).
#   (c) `pier run` with the EXACT --agent-import-path / --ak config_file / --ae env
#       list / --mounts-json from deepswe_full.yml (byte-mirrored; config file
#       selectable via PIER_CONFIG so the gemini/deepseek variants swap cleanly).
#   (d) `timeout 5400` per task (the workflow's 90-min bound; TASK_TIMEOUT_S env).
#   (e) the workflow's witness-verify greps (DeepSweAdapterError / PIER rc /
#       error=DEEPSWE_ADAPTER_FAIL / gt_prebuilt_active=true /
#       hook_graph_hash_matches_post_lsp=False) — classified, never silent.
#   (f) scripts/verify/deepswe_outcome.py classification (INFRA/GT/AGENT/RESOLVED)
#       per task -> outcome.json.
#   (g) row.json + artifacts per task; RESUMABLE (row.json exists -> skip;
#       RETRY_FAILED=1 re-runs classified failures).
#   (h) PARALLEL workers (default 8, env) + docker prune-as-you-go.
#
# MODEL AUTH — NO API keys / project IDs / credentials ANYWHERE in this script.
# The OPERATOR exports them at launch; the script only checks presence + forwards:
#   deepseek/* models  : export DEEPSEEK_API_KEY=...   (pier auto-forwards it into
#                        the task container: pier/agents/utils.py PROVIDER_KEYS).
#   vertex_ai/* models : export VERTEXAI_PROJECT=<gcp-project>  (REQUIRED — pier
#                        raises without it; auto-forwarded into the container).
#                        export VERTEXAI_LOCATION=<loc>  (default: us-east1, the
#                        LOCKED region — NOT global, whose shared serving pool
#                        carries a 429 risk; NOT auto-forwarded by pier -> this
#                        script adds an --ae. Fallback us-east4/us-central1 if the
#                        model isn't served in us-east1: one env override, no code change).
#                        export GOOGLE_APPLICATION_CREDENTIALS=<sa.json on the VM>
#                        (STRONGLY recommended: DeepSWE tasks set
#                        allow_internet=false, so pier isolates the task container
#                        on an internal-only docker network behind a squid egress
#                        proxy allowlisting only .googleapis.com — the GCE metadata
#                        server (169.254.169.254) is NOT reachable in-container, so
#                        metadata-ADC fails there. The script bind-mounts the SA
#                        JSON read-only at /gt_auth/adc.json and forwards the env;
#                        its token endpoint oauth2.googleapis.com:443 passes the
#                        proxy. Without it the run proceeds with a LOUD warning.)
#
# Inputs (env, overridable by flags):
#   MANIFEST             manifest json: {"tasks":[{"instance_id","language","docker_image"},...]}
#                        (artifact_deepswe/repo_manifest.json)
#   GT_SUBSTRATE_DIGEST  ghcr.io/<org>/gt-substrate@sha256:<digest>  REQUIRED — immutable,
#                        fail-closed GT_SUBSTRATE_DIGEST_MISSING (mutable tags rejected)
#   MODEL                default: deepseek/deepseek-v4-flash
#                        (gemini: vertex_ai/gemini-3-flash-preview)
#   PIER_CONFIG          default: artifact_deepswe/gt_integration/deepswe_gt_pier.yaml
#                        (gemini: artifact_deepswe/gt_integration/deepswe_gt_pier_gemini.yaml)
#   BENCH_DIR            deepswe-bench clone (default: $REPO_ROOT/deepswe-bench; shallow-
#                        cloned from https://github.com/datacurve-ai/deep-swe.git if absent)
#   PARALLEL             default: 8
#   OUT_DIR              default: ./agent_sweep_out  (per-task: $OUT_DIR/<instance_id>/)
#   REUSE_PROOF_DIR      optional prior proof/agent OUT_DIR to consume (skip re-proving)
#   GHCR_OWNER           default: harneet2512 (deepswe_full.yml's assets_repo owner)
#   MAX_TASKS            optional truncation ('' = all)
#   RETRY_FAILED         1 = re-run tasks whose existing row.json is a classified failure
#   TASK_TIMEOUT_S       default 5400 — per-task pier-run bound (workflow: 90 min job)
#   DISK_MIN_GB          default 25; DISK_WAIT_MAX_S default 1800 (same disk guard)
#   GT_GIT_COMMIT        optional provenance sha (default: git rev-parse HEAD)
#   SKIP_HOST_PREFLIGHT  1 = skip pier/GT-import install checks (DRY-RUN TESTING ONLY)
#
# Flags: --manifest P --digest D --model M --pier-config P --parallel N --out DIR
#        --bench-dir D --reuse-proof-dir D --ghcr-owner O --max-tasks N
#
# Known limitation (documented, not fixable here): gt_agent.py writes the host-side
# delivery proof to the FIXED path /tmp/gt/delivered_instruction.txt — with PARALLEL
# workers that file is last-writer-wins. It is copied per task best-effort; the
# canonical delivery proof remains the agent trajectory (jobs/*/agent/*.trajectory.json).
#
# Aggregate: $OUT_DIR/AGENT_SWEEP_REPORT.md. Exit code: nonzero on missing rows or any
# harness/infra/GT-wire failure class; AGENT-class misses (reward 0) do NOT fail the
# runner (they are the benchmark's signal, not the harness's).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── defaults (env-overridable) ───────────────────────────────────────────────
MANIFEST="${MANIFEST:-}"
GT_SUBSTRATE_DIGEST="${GT_SUBSTRATE_DIGEST:-}"
MODEL="${MODEL:-deepseek/deepseek-v4-flash}"
PIER_CONFIG="${PIER_CONFIG:-$REPO_ROOT/artifact_deepswe/gt_integration/deepswe_gt_pier.yaml}"
BENCH_DIR="${BENCH_DIR:-$REPO_ROOT/deepswe-bench}"
OUT_DIR="${OUT_DIR:-$PWD/agent_sweep_out}"
REUSE_PROOF_DIR="${REUSE_PROOF_DIR:-}"
GHCR_OWNER="${GHCR_OWNER:-harneet2512}"
MAX_TASKS="${MAX_TASKS:-}"
RETRY_FAILED="${RETRY_FAILED:-0}"
TASK_TIMEOUT_S="${TASK_TIMEOUT_S:-5400}"
DISK_MIN_GB="${DISK_MIN_GB:-25}"
DISK_WAIT_MAX_S="${DISK_WAIT_MAX_S:-1800}"
PARALLEL="${PARALLEL:-8}"
SKIP_HOST_PREFLIGHT="${SKIP_HOST_PREFLIGHT:-0}"
# Canonical embedder classifier dir (scripts/metrics) — the row builder imports
# embedder_certificate.classify_embedder for the embedder verdict (mirrors gt_proof_sweep.sh).
GT_METRICS_DIR="${GT_METRICS_DIR:-$REPO_ROOT/scripts/metrics}"
# Per-run cost-budget halt (constitution / LATEST_TASK.md). Default 200 (full); the
# trial passes STOP_AT_COST=25. '' or 0 disables the halt.
STOP_AT_COST="${STOP_AT_COST:-200}"

# ── flag overrides ───────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --manifest)        MANIFEST="$2"; shift 2 ;;
    --digest)          GT_SUBSTRATE_DIGEST="$2"; shift 2 ;;
    --model)           MODEL="$2"; shift 2 ;;
    --pier-config)     PIER_CONFIG="$2"; shift 2 ;;
    --parallel)        PARALLEL="$2"; shift 2 ;;
    --out)             OUT_DIR="$2"; shift 2 ;;
    --bench-dir)       BENCH_DIR="$2"; shift 2 ;;
    --reuse-proof-dir) REUSE_PROOF_DIR="$2"; shift 2 ;;
    --ghcr-owner)      GHCR_OWNER="$2"; shift 2 ;;
    --max-tasks)       MAX_TASKS="$2"; shift 2 ;;
    -h|--help)         grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "FATAL: unknown arg: $1" >&2; exit 2 ;;
  esac
done

SWEEP_RUN_ID="${SWEEP_RUN_ID:-vm_agent_$(date -u +%Y%m%dT%H%M%SZ)_$$}"

# ── fail-closed env block (deepswe_full.yml top-level env, byte-mirrored) ────
# Set THEN asserted (the workflow's "Verify env armed" step) — a missing/overridden
# flag silently re-enables a degraded fallback.
export GT_REQUIRE_FULL_STACK=1 GT_REQUIRE_FULL_POTENTIAL=1 GT_REQUIRE_FTS5=1 \
       GT_FORCE_ONNX_EMBEDDER=1 GT_REQUIRE_EMBEDDER=1 GT_REQUIRE_LSP=1 \
       GT_FORBID_PREBUILT_GRAPH=1 \
       HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1
export GT_GATES_DELIVER_ALWAYS="${GT_GATES_DELIVER_ALWAYS:-0}"
export GT_PROOF_MODE=1 GT_CONTAINERIZED=1 GT_RUNTIME_STRATEGY=unified_substrate
BAD=0
for k in GT_REQUIRE_FTS5 GT_REQUIRE_EMBEDDER GT_FORCE_ONNX_EMBEDDER \
         GT_REQUIRE_LSP GT_REQUIRE_FULL_STACK GT_FORBID_PREBUILT_GRAPH \
         HF_DATASETS_OFFLINE TRANSFORMERS_OFFLINE HF_HUB_OFFLINE; do
  v=$(printenv "$k" || true)
  if [ "$v" = "1" ]; then echo "  $k=1 OK"; else echo "FATAL: env $k='$v' expected '1' — fail-closed gate"; BAD=1; fi
done
[ "$BAD" -eq 0 ] || { echo "FATAL: fail-closed env not armed"; exit 1; }
echo "=== fail-closed env ARMED ==="

# ── preflight ────────────────────────────────────────────────────────────────
if [ -z "$MANIFEST" ] || [ ! -f "$MANIFEST" ]; then
  echo "FATAL: MANIFEST not set or not a file: '${MANIFEST}'" >&2; exit 2
fi
command -v docker  >/dev/null 2>&1 || { echo "FATAL: docker not found on PATH"  >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "FATAL: python3 not found on PATH" >&2; exit 2; }
[ -f "$PIER_CONFIG" ] || { echo "FATAL: PIER_CONFIG not a file: $PIER_CONFIG" >&2; exit 2; }
# pier composes via the docker compose v2 plugin (same requirement the workflow installs).
docker compose version >/dev/null 2>&1 || { echo "FATAL: docker compose v2 plugin missing (pier requires it)"; exit 2; }

# Digest precheck (fail-closed — GT_SUBSTRATE_DIGEST_MISSING). Required even with
# REUSE_PROOF_DIR: any reuse miss falls back to a fresh in-task proof.
if [ -z "${GT_SUBSTRATE_DIGEST:-}" ]; then
  echo "GT_SUBSTRATE_DIGEST_MISSING: no pinned substrate digest. The agent run has NO"
  echo "fallback proof runtime — aborting before any task."
  exit 1
fi
case "$GT_SUBSTRATE_DIGEST" in
  *@sha256:*) echo "pinned substrate OK: $GT_SUBSTRATE_DIGEST" ;;
  *) echo "GT_SUBSTRATE_DIGEST_MISSING: '$GT_SUBSTRATE_DIGEST' is not an immutable @sha256 digest (mutable tags are not a valid proof input)"; exit 1 ;;
esac
export GT_SUBSTRATE_DIGEST

if [ -z "${GT_GIT_COMMIT:-}" ]; then
  GT_GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
fi

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

DOCKER_DF_PATH="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
[ -n "$DOCKER_DF_PATH" ] && [ -d "$DOCKER_DF_PATH" ] || DOCKER_DF_PATH="/"

# ── model-auth presence check (operator-exported env ONLY — nothing stored) ──
case "$MODEL" in
  vertex_ai/*)
    if [ -z "${VERTEXAI_PROJECT:-}" ]; then
      echo "FATAL: MODEL=$MODEL requires VERTEXAI_PROJECT exported by the operator."
      echo "pier hard-requires it (pier/agents/utils.py PROVIDER_KEYS['vertex_ai']) and"
      echo "forwards it into the task container; litellm reads it via get_secret()."
      echo "NEVER hardcode it — export VERTEXAI_PROJECT=<your-gcp-project> and relaunch."
      exit 2
    fi
    # FORCED to 'global' (2026-06-10): gemini-3-flash-preview is Pre-GA and served ONLY
    # at the global endpoint — live-probed 404 on BOTH us-east1 and us-central1, 200 on
    # global. The us-east1 429-avoidance plan is moot when the model isn't served there.
    # 429 risk on global is mitigated by PARALLEL=4 + num_retries=3 + exp backoff.
    export VERTEXAI_LOCATION="${VERTEXAI_LOCATION:-global}"
    echo "vertex auth: VERTEXAI_PROJECT=<set> VERTEXAI_LOCATION=$VERTEXAI_LOCATION"
    if [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
      [ -f "$GOOGLE_APPLICATION_CREDENTIALS" ] || { echo "FATAL: GOOGLE_APPLICATION_CREDENTIALS not a file: $GOOGLE_APPLICATION_CREDENTIALS"; exit 2; }
      echo "vertex auth: SA JSON will be bind-mounted ro at /gt_auth/adc.json (token endpoint oauth2.googleapis.com passes the egress proxy)"
    else
      echo "WARNING: GOOGLE_APPLICATION_CREDENTIALS unset. DeepSWE tasks run with"
      echo "WARNING: allow_internet=false -> pier's internal-only network + squid proxy"
      echo "WARNING: (allowlist .googleapis.com) BLOCK the GCE metadata server in-container,"
      echo "WARNING: so metadata-ADC is expected to FAIL inside the task container."
      echo "WARNING: Export a service-account JSON path to be safe."
    fi
    ;;
  deepseek/*)
    if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
      echo "FATAL: MODEL=$MODEL requires DEEPSEEK_API_KEY exported by the operator"
      echo "(pier auto-forwards it into the task container; never stored here)."
      exit 2
    fi
    ;;
  *) echo "NOTE: MODEL=$MODEL — ensure its provider API key env is exported (pier resolves it via PROVIDER_KEYS and fails per-task if absent)" ;;
esac

# ── host harness preflight (once): pier pin + GT importable for the adapter ──
if [ "$SKIP_HOST_PREFLIGHT" != "1" ]; then
  # (b) pier install check — PINNED 0.2.0: the --ae/--mounts-json env+mount plumbing
  # was source-verified against this version (deepswe_full.yml rationale). Never float.
  PIER_VER="$(python3 -m pip show datacurve-pier 2>/dev/null | sed -n 's/^Version: //p')"
  if [ "$PIER_VER" != "0.2.0" ] || ! command -v pier >/dev/null 2>&1; then
    echo "pier missing/unpinned (found '${PIER_VER:-none}') — installing datacurve-pier==0.2.0"
    python3 -m pip install "datacurve-pier==0.2.0" || { echo "FATAL: pier install failed"; exit 1; }
  fi
  command -v pier >/dev/null 2>&1 || { echo "FATAL: pier not on PATH after install"; exit 1; }
  # Current GT importable host-side (adapter imports ONLY — no host LSP, no gt-index build).
  export PYTHONPATH="$REPO_ROOT:$REPO_ROOT/src:$REPO_ROOT/scripts/swebench:$REPO_ROOT/scripts/metrics${PYTHONPATH:+:$PYTHONPATH}"
  if ! python3 -c "import groundtruth.runtime.proof as p, groundtruth.runtime.context as c; assert p.graph_edges_hash and c.GTRuntimeContext" 2>/dev/null; then
    echo "GT runtime not importable — installing current checkout (pip install -e)"
    python3 -m pip install -e "$REPO_ROOT" || { echo "FATAL: GT host install failed"; exit 1; }
    python3 -c "import groundtruth.runtime.proof as p, groundtruth.runtime.context as c; print('GT runtime importable host-side:', bool(p.graph_edges_hash and c.GTRuntimeContext))" \
      || { echo "FATAL: GT runtime still not importable"; exit 1; }
  fi
  # artifact_deepswe must import from any cwd (pier runs per-task cwd, unlike the
  # workflow's workspace cwd — REPO_ROOT on PYTHONPATH covers it).
  python3 -c "import artifact_deepswe.gt_agent" 2>/dev/null \
    || { echo "FATAL: artifact_deepswe.gt_agent not importable (PYTHONPATH=$PYTHONPATH)"; exit 1; }
  echo "host harness ready: pier==0.2.0, GT adapter importable"
else
  echo "SKIP_HOST_PREFLIGHT=1 — pier/GT import checks skipped (dry-run testing only)"
  export PYTHONPATH="$REPO_ROOT:$REPO_ROOT/src:$REPO_ROOT/scripts/swebench:$REPO_ROOT/scripts/metrics${PYTHONPATH:+:$PYTHONPATH}"
fi

# ── deepswe-bench clone (the task dirs pier consumes) ────────────────────────
if [ ! -d "$BENCH_DIR/tasks" ]; then
  echo "deepswe-bench absent at $BENCH_DIR — shallow-cloning"
  git clone --depth 1 https://github.com/datacurve-ai/deep-swe.git "$BENCH_DIR" \
    || { echo "FATAL: deepswe-bench clone failed"; exit 1; }
fi
GT_DEEPSWE_BENCH_SHA="$(git -C "$BENCH_DIR" rev-parse HEAD 2>/dev/null || true)"
echo "deepswe-bench: $BENCH_DIR (sha ${GT_DEEPSWE_BENCH_SHA:-<unknown>})"

# ── task list from the manifest (fail-closed field check; sweep-identical) ───
TASKS_TSV="$OUT_DIR/tasks.tsv"
MANIFEST="$MANIFEST" OUT_DIR="$OUT_DIR" MAX_TASKS="$MAX_TASKS" \
SWEEP_RUN_ID="$SWEEP_RUN_ID" GT_SUBSTRATE_DIGEST="$GT_SUBSTRATE_DIGEST" \
GT_GIT_COMMIT="$GT_GIT_COMMIT" MODEL="$MODEL" PIER_CONFIG="$PIER_CONFIG" \
python3 << 'PYEOF' || exit 1
import json, os, sys
mt = (os.environ.get("MAX_TASKS") or "").strip()
with open(os.environ["MANIFEST"], encoding="utf-8") as f:
    man = json.load(f)
tasks = man.get("tasks") or []
if not tasks:
    print("FATAL: manifest has no tasks", file=sys.stderr)
    sys.exit(1)
for t in tasks:
    for k in ("instance_id", "language", "docker_image"):
        if not t.get(k):
            print(f"FATAL: manifest entry missing {k!r}: {t}", file=sys.stderr)
            sys.exit(1)
if mt:
    tasks = tasks[: int(mt)]
out = os.environ["OUT_DIR"]
with open(os.path.join(out, "tasks.tsv"), "w", encoding="utf-8", newline="\n") as f:
    for t in tasks:
        f.write(f'{t["instance_id"]}\t{t["language"]}\t{t["docker_image"]}\n')
include = [{"task": t["instance_id"], "language": t["language"],
            "image": t["docker_image"]} for t in tasks]
with open(os.path.join(out, "sweep_tasks.json"), "w", encoding="utf-8") as f:
    json.dump({"include": include,
               "run_id": os.environ.get("SWEEP_RUN_ID", ""),
               "substrate_digest": os.environ.get("GT_SUBSTRATE_DIGEST", ""),
               "gt_git_commit": os.environ.get("GT_GIT_COMMIT", ""),
               "model": os.environ.get("MODEL", ""),
               "pier_config": os.environ.get("PIER_CONFIG", ""),
               "benchmark": man.get("benchmark", "")}, f, indent=1)
langs = {}
for t in tasks:
    langs[t["language"]] = langs.get(t["language"], 0) + 1
print(f"{len(include)} tasks queued (max_tasks={mt or 'all'}) langs={langs}",
      file=sys.stderr)
PYEOF

TOTAL_TASKS=$(wc -l < "$TASKS_TSV" | tr -d ' ')
echo "agent sweep: $TOTAL_TASKS tasks | model=$MODEL | parallel=$PARALLEL | out=$OUT_DIR | run_id=$SWEEP_RUN_ID"
echo "pier config: $PIER_CONFIG"
echo "substrate:   $GT_SUBSTRATE_DIGEST"
echo "gt commit:   ${GT_GIT_COMMIT:-<unknown>}"
case "${STOP_AT_COST:-0}" in
  ''|0|0.0|0.00) echo "budget:      STOP_AT_COST disabled (no cost halt)" ;;
  *)             echo "budget:      STOP_AT_COST=\$${STOP_AT_COST} (per-run halt; tasks past the cap -> BUDGET_HALTED)" ;;
esac
[ -n "$REUSE_PROOF_DIR" ] && echo "reuse proof: $REUSE_PROOF_DIR (8-artifact dirs consumed; misses re-prove)"

# ── optional GHCR login (best-effort; env-only, never stored) ────────────────
if [ -n "${GHCR_TOKEN:-}" ]; then
  echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER:-$GHCR_OWNER}" --password-stdin || true
fi

# ── pull the pinned substrate ONCE (3-retry backoff) ─────────────────────────
T0=$(date +%s)
SUB_PULLED=0
for i in 1 2 3; do
  if docker pull "$GT_SUBSTRATE_DIGEST"; then SUB_PULLED=1; break; fi
  echo "substrate pull attempt $i failed; backoff $((i*20))s"
  sleep $((i*20))
done
T_SUBSTRATE_PULL_S=$(( $(date +%s) - T0 ))
if [ "$SUB_PULLED" -ne 1 ]; then
  echo "GT_SUBSTRATE_PULL_FAIL: docker pull of the pinned substrate failed — aborting (fail-closed, no fallback runtime)"
  exit 1
fi
echo "substrate pulled in ${T_SUBSTRATE_PULL_S}s"

# ── disk guard (sweep-identical): free-space floor; warn+wait, prune dangling ──
disk_guard() {
  local need_kb=$(( DISK_MIN_GB * 1024 * 1024 ))
  local waited=0 free_kb
  while :; do
    free_kb=$(df -Pk "$DOCKER_DF_PATH" 2>/dev/null | awk 'NR==2{print $4}')
    [ -z "$free_kb" ] && return 0           # unknown fs — do not block
    [ "$free_kb" -ge "$need_kb" ] && return 0
    echo "[disk] LOW: $((free_kb/1024/1024))GB free < ${DISK_MIN_GB}GB floor on $DOCKER_DF_PATH — pruning dangling images, waiting 30s (waited ${waited}s)" >&2
    docker image prune -f >/dev/null 2>&1 || true
    sleep 30
    waited=$(( waited + 30 ))
    [ "$waited" -ge "$DISK_WAIT_MAX_S" ] && return 1
  done
}

# ── per-run cost budget (STOP_AT_COST) — race-safe under xargs -P ─────────────
# Sum the per-task recorded cost (gt_deep_metrics_<id>.json efficiency.llm_cost_usd,
# copied into each task dir) across ALL completed tasks, under an flock on a sentinel,
# so concurrent workers can't double-count or race. Returns 0 (halt) when the running
# total >= STOP_AT_COST. STOP_AT_COST='' or '0' disables the halt (always returns 1).
budget_exceeded() {
  local cap="${STOP_AT_COST:-0}"
  case "$cap" in ''|0|0.0|0.00) return 1 ;; esac      # disabled
  local sentinel="$OUT_DIR/.budget.lock"
  : > "$sentinel" 2>/dev/null || true
  # flock the sentinel; recount inside the lock so the read is atomic vs other workers.
  # If flock is unavailable (non-Linux), fall back to an unlocked recount (the recount
  # itself is idempotent — worst case is a one-task overshoot, never a crash).
  local _lock="flock 9"; command -v flock >/dev/null 2>&1 || _lock=":"
  local total
  total="$( exec 9>>"$sentinel"; $_lock
    OUT_DIR="$OUT_DIR" python3 - <<'PYB'
import glob, json, os
out = os.environ["OUT_DIR"]
tot = 0.0
# Prefer the deep-metrics record (model-recorded cost); fall back to the row's lsp/cost.
for p in glob.glob(os.path.join(out, "*", "gt_deep_metrics_*.json")):
    try:
        d = json.load(open(p, encoding="utf-8"))
        c = (d.get("efficiency") or {}).get("llm_cost_usd")
        if isinstance(c, (int, float)):
            tot += float(c)
    except Exception:
        pass
print(f"{tot:.8f}")
PYB
  )"
  total="${total:-0}"
  # numeric compare via awk (floats); >= cap => halt.
  awk -v t="$total" -v c="$cap" 'BEGIN{ exit (t+0 >= c+0) ? 0 : 1 }'
}

# ── timeout teardown: rm the leaked task+squid containers a TERMed pier left behind ──
# `docker container prune` only removes STOPPED containers; pier's detached task/squid
# containers keep RUNNING after timeout(1) SIGTERMs the pier process. Find them by the
# pier compose-project label and by the task-id name pattern, then force-rm (which also
# drops the per-task internal network once its endpoints are gone). Best-effort; the id
# is sanitized to a docker-safe token (the same transform used for $ctr).
pier_timeout_teardown() {
  local tid="$1"
  local safe; safe="$(printf '%s' "$tid" | tr -c 'a-zA-Z0-9_.-' '_')"
  local cids
  # (1) by compose-project label (pier names the project after the task/job).
  cids="$(docker ps -aq --filter "label=com.docker.compose.project" \
            --filter "name=${safe}" 2>/dev/null || true)"
  # (2) by name substring (task id or 'squid'/'pier' in the container name) — covers
  #     containers pier did not label with the project.
  cids="$cids $(docker ps -aq --filter "name=${safe}" 2>/dev/null || true)"
  cids="$cids $(docker ps -aq --filter "name=squid" --filter "name=${safe}" 2>/dev/null || true)"
  # de-dup + force-rm
  local uniq
  uniq="$(printf '%s\n' $cids | sort -u | tr '\n' ' ')"
  if [ -n "${uniq// /}" ]; then
    echo "[timeout-teardown] $tid: force-removing leaked containers: $uniq" >&2
    # shellcheck disable=SC2086
    docker rm -f $uniq >/dev/null 2>&1 || true
  fi
  # Drop any now-dangling internal network pier created for this task.
  docker network ls --filter "name=${safe}" -q 2>/dev/null | while read -r net; do
    [ -n "$net" ] && docker network rm "$net" >/dev/null 2>&1 || true
  done
}

# ── per-task runner (mirrors deepswe_full.yml's trial job, step for step) ─────
run_task() {
  local line="$1" id lang img
  IFS=$'\t' read -r id lang img <<< "$line"
  [ -z "$id" ] && return 0

  local task_dir="$OUT_DIR/$id"
  local art_dir="$task_dir/gt"           # the workflow's /tmp/gt, per-task (parallel-safe)
  local ctr="gtsrc_$(printf '%s' "$id" | tr -c 'a-zA-Z0-9_.-' '_')"
  local trial_log="$task_dir/trial_output.log"

  # RESUMABLE: skip when a row already exists (RETRY_FAILED=1 re-runs failures).
  if [ -f "$task_dir/row.json" ]; then
    if [ "$RETRY_FAILED" = "1" ] && ROW="$task_dir/row.json" python3 -c '
import json, os, sys
r = json.load(open(os.environ["ROW"], encoding="utf-8"))
sys.exit(0 if (r.get("failure_class") or r.get("pier_rc", -1) != 0) else 1)
'; then
      echo "[retry] $id — prior row was a classified failure; re-running"
      rm -f "$task_dir/row.json"
    else
      echo "[skip] $id — row.json exists (resumable re-run)"
      return 0
    fi
  fi

  # ── STOP_AT_COST budget halt: before any spend, if the running total of recorded
  #    per-task cost has reached the cap, do NOT launch this task. Record it as a
  #    classified BUDGET_HALTED row (not silent) so the aggregate counts it. ──
  if budget_exceeded; then
    mkdir -p "$task_dir"
    echo "[budget] $id — STOP_AT_COST=${STOP_AT_COST} reached; not launching (BUDGET_HALTED)" >&2
    TASK_ID="$id" TASK_LANG="$lang" IMG="$img" MODEL="$MODEL" \
    PIER_CONFIG="$PIER_CONFIG" OUT_TASK="$task_dir" SWEEP_RUN_ID="$SWEEP_RUN_ID" \
    STOP_AT_COST="$STOP_AT_COST" python3 - <<'PYH'
import json, os, time
out = os.environ["OUT_TASK"]; os.makedirs(out, exist_ok=True)
row = {
    "instance_id": os.environ.get("TASK_ID", ""),
    "language": os.environ.get("TASK_LANG", ""),
    "image": os.environ.get("IMG", ""),
    "model": os.environ.get("MODEL", ""),
    "pier_config": os.path.basename(os.environ.get("PIER_CONFIG", "")),
    "failure_class": "BUDGET_HALTED",
    "pier_rc": -1,
    "budget_cap_usd": os.environ.get("STOP_AT_COST", ""),
    "run_id": os.environ.get("SWEEP_RUN_ID", ""),
    "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
with open(os.path.join(out, "row.json"), "w", encoding="utf-8") as fo:
    json.dump(row, fo, separators=(",", ":"))
PYH
    return 0
  fi

  mkdir -p "$task_dir" "$art_dir"
  : > "$trial_log"
  local FAIL_CLASS="" PIER_RC=-1 PROOF_REUSED=0 TASK_REPO_COMMIT=""
  local T_TASK_PULL_S=-1 T_PROOF_S=-1 T_AGENT_S=-1 T0 RC

  # Bench task dir must exist (the workflow's "Validate task exists" step).
  if [ ! -d "$BENCH_DIR/tasks/$id" ]; then
    FAIL_CLASS="TASK_DIR_MISSING"
    echo "TASK_DIR_MISSING: $BENCH_DIR/tasks/$id not found in the bench clone" | tee -a "$trial_log"
  fi

  # ── Pull task image (GHCR-first, source fallback, retries) — ALWAYS needed:
  #    pier builds the agent image FROM it even when the proof is reused. ──
  if [ -z "$FAIL_CLASS" ]; then
    if ! disk_guard; then
      FAIL_CLASS="DISK_LOW"
      echo "[disk] $id: DISK_LOW after ${DISK_WAIT_MAX_S}s wait — classified" >&2
    fi
  fi
  if [ -z "$FAIL_CLASS" ]; then
    T0=$(date +%s)
    local TAG="${img##*:}"
    local BASE="${img##*/}"; BASE="${BASE%%:*}"
    local GHCR="ghcr.io/${GHCR_OWNER}/${BASE}:${TAG}"
    local PULLED=""
    if docker pull "$GHCR" >>"$task_dir/pull.log" 2>&1; then
      [ "$GHCR" != "$img" ] && docker tag "$GHCR" "$img"
      PULLED="$GHCR"
    else
      echo "GHCR miss ($GHCR) — pulling source ref with retries" >>"$task_dir/pull.log"
      local i
      for i in 1 2 3 4; do
        if docker pull "$img" >>"$task_dir/pull.log" 2>&1; then PULLED="$img"; break; fi
        echo "pull attempt $i failed; backoff 15s" >>"$task_dir/pull.log"
        sleep 15
      done
    fi
    T_TASK_PULL_S=$(( $(date +%s) - T0 ))
    if [ -z "$PULLED" ] || ! docker image inspect "$img" >/dev/null 2>&1; then
      FAIL_CLASS="TASK_IMAGE_PULL_FAIL"
      # Canonical token (deepswe_outcome.INFRA_LOG_MARKERS), line-anchored in the trial log.
      echo "TASK_IMAGE_PULL_FAIL: task image pull failed after retries ($img)" | tee -a "$trial_log"
    fi
  fi

  # ── (a) GT substrate proof — REUSE_PROOF_DIR consume, else fresh gt-run-proof ──
  local NEED_PROOF=1
  if [ -z "$FAIL_CLASS" ] && [ -n "$REUSE_PROOF_DIR" ] && [ -d "$REUSE_PROOF_DIR/$id" ]; then
    local missing=0 c
    for c in graph.db runtime_context.json lsp_certificate.json graph_certificate.json \
             embedder_certificate.json foundational_gate_report.json run_manifest.json \
             brief.txt; do
      [ -s "$REUSE_PROOF_DIR/$id/$c" ] || { missing=1; echo "[reuse] $id: $c absent in $REUSE_PROOF_DIR/$id — re-proving" >&2; break; }
    done
    # Presence is NOT enough: a reused proof must also be CLEAN and on THIS substrate.
    # Reject (fall through to a fresh proof, never error) when — (1) a source row.json
    # exists with a non-empty failure_class; (2) any cert verdict is FAIL (lsp/graph/
    # embedder + per-language lsp certs); (3) run_manifest.substrate_digest != the pinned
    # GT_SUBSTRATE_DIGEST. A pre-fix-substrate or FAILED-cert proof must never be admitted.
    if [ "$missing" -eq 0 ]; then
      if ! RP="$REUSE_PROOF_DIR/$id" WANT_DIGEST="$GT_SUBSTRATE_DIGEST" python3 - <<'PYV'
import json, glob, os, sys
rp = os.environ["RP"]; want = os.environ.get("WANT_DIGEST", "")
def jload(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
# (1) source row failure_class must be empty (if a row exists at all)
row = jload(os.path.join(rp, "row.json"))
if isinstance(row, dict) and (row.get("failure_class") or "").strip():
    print(f"REUSE_REJECT: source row failure_class={row.get('failure_class')!r}", file=sys.stderr); sys.exit(1)
# (2) no cert verdict may be FAIL (scan lsp/graph/embedder + per-language lsp certs)
def has_fail(d):
    if not isinstance(d, dict):
        return False
    for k in ("verdict", "verdict_hint", "status", "result"):
        if "FAIL" in str(d.get(k, "")).upper():
            return True
    v = d.get("verdict")
    if isinstance(v, dict):
        for vv in v.values():
            if "FAIL" in str(vv).upper():
                return True
    return False
certs = ["lsp_certificate.json", "graph_certificate.json", "embedder_certificate.json"]
certs += [os.path.basename(p) for p in glob.glob(os.path.join(rp, "lsp_certificate_*.json"))]
for c in certs:
    if has_fail(jload(os.path.join(rp, c))):
        print(f"REUSE_REJECT: cert {c} carries a FAIL verdict", file=sys.stderr); sys.exit(1)
# (3) substrate digest must match the pinned one
man = jload(os.path.join(rp, "run_manifest.json")) or {}
got = str(man.get("substrate_digest") or "")
if want and got and got != want:
    print(f"REUSE_REJECT: substrate_digest mismatch (proof={got} != pinned={want})", file=sys.stderr); sys.exit(1)
sys.exit(0)
PYV
      then
        missing=1
        echo "[reuse] $id — proof present but failed admit gate (failed cert / dirty row / substrate mismatch) — re-proving" >&2
      fi
    fi
    if [ "$missing" -eq 0 ]; then
      for c in graph.db runtime_context.json lsp_certificate.json graph_certificate.json \
               embedder_certificate.json foundational_gate_report.json run_manifest.json \
               brief.txt gt_issue_anchors.json gt_scope_files.txt gt_lsp_metrics.txt issue.txt; do
        cp -f "$REUSE_PROOF_DIR/$id/$c" "$art_dir/$c" 2>/dev/null || true
      done
      # Per-language LSP certs (lsp_certificate_<lang>.json) — glob; absent on
      # single-language proofs, so best-effort. These carry the per-lang verdicts
      # the row builder/metrics surface reads.
      for pc in "$REUSE_PROOF_DIR/$id"/lsp_certificate_*.json; do
        [ -e "$pc" ] && cp -f "$pc" "$art_dir/" 2>/dev/null || true
      done
      [ -d "$REUSE_PROOF_DIR/$id/src" ] && cp -a "$REUSE_PROOF_DIR/$id/src" "$art_dir/src" 2>/dev/null
      NEED_PROOF=0; PROOF_REUSED=1
      echo "[reuse] $id — consumed 8-artifact proof from $REUSE_PROOF_DIR/$id (gt-run-proof skipped)"
    fi
  fi

  # Source extraction (needed for a fresh proof AND for GT_HOST_SRC_ROOT when the
  # reused proof dir carried no src/). Same .git probe as deepswe_full.yml.
  if [ -z "$FAIL_CLASS" ] && [ ! -d "$art_dir/src" ]; then
    docker rm -f "$ctr" >/dev/null 2>&1 || true
    if ! docker run -d --name "$ctr" "$img" sleep 1800 >/dev/null 2>>"$task_dir/pull.log"; then
      FAIL_CLASS="SRC_EXTRACT_FAIL"
    else
      local ROOT
      ROOT=$(docker exec "$ctr" bash -c 'for d in /home/user /testbed /workspace /app /repo; do [ -d "$d/.git" ] && echo "$d" && break; done' 2>/dev/null || true)
      ROOT=${ROOT:-/testbed}
      rm -rf "$art_dir/src" && mkdir -p "$art_dir/src"
      if ! docker cp "$ctr:$ROOT/." "$art_dir/src" >/dev/null 2>>"$task_dir/pull.log"; then
        FAIL_CLASS="SRC_EXTRACT_FAIL"
      fi
      docker rm -f "$ctr" >/dev/null 2>&1 || true
      if [ -z "$FAIL_CLASS" ]; then
        # Same empty-extract guard as gt_proof_sweep.sh (a cp that copied nothing).
        local N_FILES
        N_FILES=$(find "$art_dir/src" -type f 2>/dev/null | wc -l | tr -d ' ')
        [ "$N_FILES" -eq 0 ] && FAIL_CLASS="SRC_EXTRACT_FAIL"
      fi
    fi
    [ -n "$FAIL_CLASS" ] && echo "SRC_EXTRACT_FAIL: source extraction failed for $img" | tee -a "$trial_log"
  fi
  TASK_REPO_COMMIT=$(git -C "$art_dir/src" rev-parse HEAD 2>/dev/null || true)

  if [ -z "$FAIL_CLASS" ] && [ "$NEED_PROOF" -eq 1 ]; then
    # THIS task's issue (problem statement only — NEVER gold/tests). instruction.md
    # first, task.toml fields fallback, GT_ISSUE_MISSING fail-closed (P0.1-a).
    # NOTE: the heredoc is a STANDALONE command, never inside an `if !` condition —
    # bash's export -f serialization corrupts heredocs inside compound-command
    # conditions at nesting depth >=2 (the then-branch line gets interleaved before
    # the heredoc body in the xargs child shell). Reproduced + fixed 2026-06-10.
    GT_ISSUE_OUT="$art_dir/issue.txt" python3 - "$BENCH_DIR/tasks/$id" << 'PYEOF'
import os, sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
task_dir = sys.argv[1]
out_path = os.environ.get("GT_ISSUE_OUT", "/tmp/issue.txt")
issue, source = "", ""
inst = os.path.join(task_dir, "instruction.md")
if os.path.exists(inst):
    with open(inst, encoding="utf-8", errors="replace") as f:
        issue = f.read().strip()
    source = "instruction.md"
if not issue:
    try:
        with open(os.path.join(task_dir, "task.toml"), "rb") as f:
            t = tomllib.load(f)
        issue = ((t.get("metadata", {}) or {}).get("issue", "")
                 or (t.get("task", {}) or {}).get("prompt", "")
                 or (t.get("task", {}) or {}).get("instruction", "") or "").strip()
        source = "task.toml"
    except Exception:
        issue = ""
if not issue:
    print("GT_ISSUE_MISSING: no instruction.md and no task.toml issue/prompt/instruction "
          "— the substrate must never run with an EMPTY issue (fail-closed)", file=sys.stderr)
    sys.exit(1)
with open(out_path, "w", encoding="utf-8") as f:
    f.write(issue)
print(f"issue text: {len(issue)} chars from {source} -> {out_path}")
PYEOF
    RC=$?
    if [ "$RC" -ne 0 ] || [ ! -s "$art_dir/issue.txt" ]; then
      FAIL_CLASS="GT_ISSUE_MISSING"
      echo "GT_ISSUE_MISSING: no issue text for this task — refusing to run the substrate with an EMPTY issue (fail-closed)" | tee -a "$trial_log"
    fi
  fi

  if [ -z "$FAIL_CLASS" ] && [ "$NEED_PROOF" -eq 1 ]; then
    # gt-run-proof INSIDE the pinned substrate — the deepswe_full.yml command,
    # byte-mirrored (8 proof flags + issue file + provenance), per-task paths.
    T0=$(date +%s)
    docker run --rm \
        --memory="${GT_PROOF_MEM:-7g}" --memory-swap="${GT_PROOF_MEM:-7g}" \
        -v "$art_dir/src:/work:ro" -v "$art_dir:/gt_artifacts" \
        -v "$art_dir/issue.txt:/work_issue.txt:ro" \
        -e GT_PROOF_MODE=1 -e GT_CONTAINERIZED=1 -e GT_RUNTIME_STRATEGY=unified_substrate \
        -e GT_REQUIRE_FTS5=1 -e GT_REQUIRE_EMBEDDER=1 -e GT_FORCE_ONNX_EMBEDDER=1 \
        -e GT_REQUIRE_LSP=1 -e GT_REQUIRE_FULL_STACK=1 -e GT_ISSUE_FILE=/work_issue.txt \
        -e GT_GATES_DELIVER_ALWAYS="${GT_GATES_DELIVER_ALWAYS:-0}" \
        -e GT_GIT_COMMIT="$GT_GIT_COMMIT" \
        -e GT_SUBSTRATE_DIGEST="$GT_SUBSTRATE_DIGEST" \
        -e GT_TASK_REPO_COMMIT="$TASK_REPO_COMMIT" \
        "$GT_SUBSTRATE_DIGEST" gt-run-proof --source-root /work --out /gt_artifacts \
        > "$task_dir/proof_run.log" 2>&1
    RC=$?
    T_PROOF_S=$(( $(date +%s) - T0 ))
    if [ "$RC" -ne 0 ]; then
      # 137 = killed (memcg OOM / SIGKILL): the in-container fail-closed print never ran —
      # distinct class so a capacity kill can't masquerade as a logic failure.
      if [ "$RC" -eq 137 ]; then FAIL_CLASS="GT_PROOF_OOM"; else FAIL_CLASS="GT_RUN_PROOF_FAIL"; fi
      echo "$FAIL_CLASS: gt-run-proof rc=$RC" | tee -a "$trial_log"
    fi
  fi

  # §E GT_ARTIFACT_MISSING — verify the 8-artifact contract (incl. brief.txt).
  if [ -z "$FAIL_CLASS" ]; then
    local c
    for c in graph.db runtime_context.json lsp_certificate.json graph_certificate.json \
             embedder_certificate.json foundational_gate_report.json run_manifest.json \
             brief.txt; do
      if [ ! -s "$art_dir/$c" ]; then
        FAIL_CLASS="GT_ARTIFACT_MISSING"
        echo "GT_ARTIFACT_MISSING / SUBSTRATE_MISSING_CERTS: $art_dir/$c absent after gt-run-proof" | tee -a "$trial_log"
        break
      fi
    done
  fi

  # ── (c)+(d) pier run — the EXACT deepswe_full.yml invocation, per-task cwd ──
  if [ -z "$FAIL_CLASS" ]; then
    # Cert-env handoff (§D) for the HOST-side adapter (gt_agent.py witness/brief):
    # the workflow's $GITHUB_ENV block, exported per worker process.
    export GT_PORTABLE_SUBSTRATE=1
    export GT_CERT_DIR="$art_dir"
    export GT_HOST_GRAPH_DB="$art_dir/graph.db"
    export GT_HOST_SRC_ROOT="$art_dir/src"
    export GT_REPO_ROOT="$art_dir/src"
    export GT_LSP_CERT="$art_dir/lsp_certificate.json"
    export GT_GRAPH_CERT="$art_dir/graph_certificate.json"
    export GT_EMBEDDER_CERT="$art_dir/embedder_certificate.json"

    # CONTAINER mount + env: the substrate dir -> /gt_artifacts READ-ONLY (the ONE
    # graph; ro => no divergent reindex). JSON is the pier --mounts-json contract.
    local GT_C_ARTIFACTS=/gt_artifacts
    local MOUNTS_JSON="[{\"type\":\"bind\",\"source\":\"${art_dir}\",\"target\":\"${GT_C_ARTIFACTS}\",\"read_only\":true}"
    # Vertex SA JSON: bind-mount ro + forward the env (metadata-ADC is blocked by the
    # egress proxy — see header). NO secret content touches this script or any file.
    local -a AE_EXTRA=()
    case "$MODEL" in
      vertex_ai/*)
        AE_EXTRA+=( --ae VERTEXAI_LOCATION="${VERTEXAI_LOCATION:-us-east1}" )
        if [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
          MOUNTS_JSON+=",{\"type\":\"bind\",\"source\":\"${GOOGLE_APPLICATION_CREDENTIALS}\",\"target\":\"/gt_auth/adc.json\",\"read_only\":true}"
          AE_EXTRA+=( --ae GOOGLE_APPLICATION_CREDENTIALS="/gt_auth/adc.json" )
        fi
        ;;
    esac
    MOUNTS_JSON+="]"

    echo "=== DeepSWE GT agent: $id (model $MODEL, config $(basename "$PIER_CONFIG")) ===" | tee -a "$trial_log"
    echo "HOST  GT_CERT_DIR=$GT_CERT_DIR  GT_HOST_GRAPH_DB=$GT_HOST_GRAPH_DB" | tee -a "$trial_log"
    echo "CONTAINER mount: ${art_dir} -> ${GT_C_ARTIFACTS} (ro)" | tee -a "$trial_log"

    # Per-task cwd so pier's jobs/ tree is task-isolated under PARALLEL workers.
    mkdir -p "$task_dir/pier"
    T0=$(date +%s)
    (
      cd "$task_dir/pier" || exit 97
      # P0.1-b: pipefail + PIPESTATUS — `pier | tee` must not return tee's 0.
      set -o pipefail
      timeout "$TASK_TIMEOUT_S" pier run \
        -p "$BENCH_DIR/tasks/$id" \
        --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent \
        --model "$MODEL" \
        --env docker \
        -y \
        --mounts-json "${MOUNTS_JSON}" \
        --ae GT_HOST_GRAPH_DB="${GT_C_ARTIFACTS}/graph.db" \
        --ae GT_CERT_DIR="${GT_C_ARTIFACTS}" \
        --ae GT_HOST_SRC_ROOT="${GT_C_ARTIFACTS}/src" \
        --ae GT_PORTABLE_SUBSTRATE="1" \
        --ae GT_FORBID_PREBUILT_GRAPH="1" \
        --ae GT_PROOF_MODE="${GT_PROOF_MODE:-1}" \
        --ae GT_CONTAINERIZED="1" \
        --ae GT_RUNTIME_STRATEGY="${GT_RUNTIME_STRATEGY:-unified_substrate}" \
        ${AE_EXTRA[@]+"${AE_EXTRA[@]}"} \
        --ak version=2.2.8 \
        --ak config_file="$PIER_CONFIG" \
        2>&1 | tee -a "$trial_log"
      exit "${PIPESTATUS[0]}"
    )
    PIER_RC=$?
    T_AGENT_S=$(( $(date +%s) - T0 ))
    echo "=== Trial complete (pier rc=$PIER_RC, ${T_AGENT_S}s) ===" | tee -a "$trial_log"

    # Host-side delivery proof (gt_agent.py writes the FIXED /tmp/gt path — racy
    # under parallel workers; best-effort copy, trajectory remains canonical).
    cp -f /tmp/gt/delivered_instruction.txt "$task_dir/" 2>/dev/null || true

    # ── (e) the workflow's witness-verify greps, byte-mirrored ──
    if [ "$PIER_RC" -eq 124 ]; then
      FAIL_CLASS="PIER_TIMEOUT"
      echo "PIER_TIMEOUT: pier run exceeded ${TASK_TIMEOUT_S}s (timeout(1) rc=124)" | tee -a "$trial_log"
      # timeout(1) TERMs pier, but the detached task + squid containers pier launched
      # SURVIVE (docker container prune only removes STOPPED ones) — they leak and
      # starve the next task of CPU/RAM/ports. Tear down THIS task's compose project /
      # leftover containers before moving on. Match by the pier compose-project label
      # (com.docker.compose.project) and by the task-id name pattern; force-rm both.
      pier_timeout_teardown "$id"
    elif grep -RIq "DeepSweAdapterError" "$task_dir/pier/jobs" "$trial_log" 2>/dev/null; then
      FAIL_CLASS="DEEPSWE_ADAPTER_FAIL"
      echo "DEEPSWE_ADAPTER_FAIL: DeepSweAdapterError found in pier jobs/exception_message (swallowed adapter raise)" | tee -a "$trial_log"
      grep -RIn "DeepSweAdapterError" "$task_dir/pier/jobs" "$trial_log" 2>/dev/null | head -5 || true
    elif [ "$PIER_RC" -ne 0 ] && grep -Eiq "429|too many requests|rate.?limit|resource[_ ]exhausted|quota exceeded" "$trial_log" 2>/dev/null; then
      # A non-zero pier run dominated by provider rate-limiting (429 / RESOURCE_EXHAUSTED /
      # quota). Distinct class so the run can be re-driven (different region / lower
      # PARALLEL) rather than charged as a generic harness fault. Line-anchored RATE_LIMIT
      # token so the classifier/aggregate can see it cheaply.
      FAIL_CLASS="RATE_LIMIT"
      echo "RATE_LIMIT: pier run exited rc=$PIER_RC with provider 429/quota signal (re-drive: us-east4/us-central1 or lower PARALLEL)" | tee -a "$trial_log"
      grep -Ein "429|too many requests|rate.?limit|resource[_ ]exhausted|quota exceeded" "$trial_log" 2>/dev/null | head -3 || true
    elif [ "$PIER_RC" -ne 0 ]; then
      FAIL_CLASS="PIER_RUN_FAIL"
      echo "PIER_RUN_FAIL: pier run exited rc=$PIER_RC (pipefail surfaced — not tee's 0)" | tee -a "$trial_log"
    elif grep -q "error=DEEPSWE_ADAPTER_FAIL" "$trial_log"; then
      FAIL_CLASS="DEEPSWE_ADAPTER_FAIL"
      grep "DEEPSWE_ADAPTER_FAIL" "$trial_log" | head -5
    elif ! grep -q "gt_prebuilt_active=true" "$trial_log"; then
      FAIL_CLASS="GT_ARTIFACT_NOT_CONSUMED"
      echo "GT_ARTIFACT_NOT_CONSUMED: no [GT_META] witness with gt_prebuilt_active=true (delivery, not telemetry)" | tee -a "$trial_log"
      grep "\[GT_META\]" "$trial_log" | head -5 || echo "(no [GT_META] line at all)"
    elif grep -q "hook_graph_hash_matches_post_lsp=False" "$trial_log"; then
      FAIL_CLASS="GRAPH_FAIL_HASH_MISMATCH"
      echo "GRAPH_FAIL_HASH_MISMATCH: hook_graph_hash != graph_hash_after_lsp" | tee -a "$trial_log"
      grep "hook_graph_hash_matches_post_lsp" "$trial_log" | head -3
    else
      echo "adapter witness OK: substrate graph consumed read-only; hook hash == post-LSP hash"
      grep "\[GT_META\] graph_witness" "$trial_log" | head -2 || true
    fi
  fi

  # ── (f) deepswe_outcome.py classification (always — failures classified too) ──
  GT_TRIAL_LOG="$trial_log" GT_CERT_DIR="$art_dir" \
  GT_DEEPSWE_OUTCOME_JSON="$task_dir/outcome.json" \
    python3 "$REPO_ROOT/scripts/verify/deepswe_outcome.py" "$task_dir/pier/jobs" \
    > "$task_dir/outcome.txt" 2>&1 || echo "WARN: outcome extract failed for $id" >&2

  # Deep 8-dp metrics (constitution mandate) — best-effort, mirrors the workflow.
  # results_dir = the TASK dir (holds BOTH gt/<certs,graph.db> AND pier/jobs/**/agent/
  # *.trajectory.json) so the trajectory glob resolves THIS task and can't grab a
  # sibling (gt_deep_metrics also id-filters its hits). graph.db pinned via --db.
  python3 "$REPO_ROOT/scripts/swebench/gt_deep_metrics.py" "$id" "$task_dir" \
    --db "$art_dir/graph.db" --log "$trial_log" >/dev/null 2>&1 || echo "DEEP_METRICS_WARN: $id" >&2
  cp -f "/tmp/gt_deep_metrics_${id}.json" "/tmp/gt_deep_metrics_${id}.md" "$task_dir/" 2>/dev/null || true

  # ── (g) per-task JSON row (always — failures are classified, never silent) ──
  TASK_ID="$id" TASK_LANG="$lang" IMG="$img" MODEL="$MODEL" \
  PIER_CONFIG="$PIER_CONFIG" FAIL_CLASS="$FAIL_CLASS" PIER_RC="$PIER_RC" \
  PROOF_REUSED="$PROOF_REUSED" TASK_REPO_COMMIT="$TASK_REPO_COMMIT" \
  T_TASK_PULL_S="$T_TASK_PULL_S" T_PROOF_S="$T_PROOF_S" T_AGENT_S="$T_AGENT_S" \
  T_SUBSTRATE_PULL_S="$T_SUBSTRATE_PULL_S" OUT_TASK="$task_dir" ART_DIR="$art_dir" \
  GT_METRICS_DIR="$GT_METRICS_DIR" \
  SWEEP_RUN_ID="$SWEEP_RUN_ID" BENCH_SHA="$GT_DEEPSWE_BENCH_SHA" \
  python3 << 'PYEOF'
import glob, json, os, sys, time
out = os.environ["OUT_TASK"]
art = os.environ.get("ART_DIR", out)
os.makedirs(out, exist_ok=True)

def jload(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def inum(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d

# ── embedder / lsp / gate verdicts from the proof certs (mirror gt_proof_sweep.sh:405-423)
#    so the agent row carries the metrics surface (effective_w_sem, lsp verdict, gates). ──
emb = jload(os.path.join(art, "embedder_certificate.json")) or {}
lsp_cert = jload(os.path.join(art, "lsp_certificate.json")) or {}
gates = jload(os.path.join(art, "foundational_gate_report.json")) or {}
emb_verdict, emb_ok = ("EMBEDDER_CERT_ABSENT", False)
if emb:
    try:
        sys.path.insert(0, os.environ.get("GT_METRICS_DIR", "scripts/metrics"))
        from embedder_certificate import classify_embedder
        emb_verdict, emb_ok = classify_embedder(emb, proof_mode=True, require_embedder=True)
    except Exception as e:
        emb_verdict, emb_ok = (f"CLASSIFY_ERROR:{type(e).__name__}", False)
gv = gates.get("verdict") or {}
gr = gates.get("gate_resolution") or {}
ge = (gates.get("gate_embedder") or {}).get("consumption") or {}
# per-language LSP certs (lsp_certificate_<lang>.json) — per-lang verdict surface.
per_lang_lsp = {}
for pc in sorted(glob.glob(os.path.join(art, "lsp_certificate_*.json"))):
    name = os.path.basename(pc)[len("lsp_certificate_"):-len(".json")]
    c = jload(pc) or {}
    per_lang_lsp[name] = {
        "verdict_hint": c.get("verdict_hint", ""),
        "server_launched": bool(c.get("server_launched", False)),
        "warm_probe_ok": bool(c.get("warm_probe_ok", False)),
        "verified_edges": inum(c.get("verified_edges")),
        "corrected_edges": inum(c.get("corrected_edges")),
        "deleted_edges": inum(c.get("deleted_edges")),
    }

outcome = jload(os.path.join(out, "outcome.json")) or {}
rec = (outcome.get("tasks") or [{}])[0]
row = {
    "instance_id": os.environ.get("TASK_ID", ""),
    "language": os.environ.get("TASK_LANG", ""),
    "image": os.environ.get("IMG", ""),
    "model": os.environ.get("MODEL", ""),
    "pier_config": os.path.basename(os.environ.get("PIER_CONFIG", "")),
    # Runner-level class (pull/proof/pier/witness); '' when the harness was clean.
    "failure_class": os.environ.get("FAIL_CLASS", ""),
    "pier_rc": inum(os.environ.get("PIER_RC", "-1"), -1),
    "proof_reused": os.environ.get("PROOF_REUSED", "0") == "1",
    # deepswe_outcome.py's classification (INFRA/GT/AGENT/RESOLVED/UNKNOWN).
    "outcome_class": rec.get("failure_class"),
    "in_resolved_denominator": rec.get("in_resolved_denominator"),
    "reward": rec.get("reward"),
    "n_agent_steps": rec.get("n_agent_steps"),
    "exit_status": rec.get("exit_status"),
    "gt_prebuilt_active": rec.get("gt_prebuilt_active"),
    "hook_hash_match": rec.get("hook_hash_match"),
    # ── embedder / LSP / gate verdicts (the metrics surface — effective_w_sem etc.) ──
    "embedder": {
        "verdict": emb_verdict,
        "ok": bool(emb_ok),
        "class": emb.get("embedder_class", ""),
        "dim": emb.get("embedder_dim"),
        "discrimination_margin": emb.get("discrimination_margin"),
        "semantic_candidate_count": inum(emb.get("semantic_candidate_count")),
        "rendered_semantic_nonzero_count": inum(emb.get("rendered_semantic_nonzero_count")),
        "effective_w_sem": emb.get("effective_w_sem"),
    },
    "lsp": {
        "verdict_hint": lsp_cert.get("verdict_hint", ""),
        "server_launched": bool(lsp_cert.get("server_launched", False)),
        "warm_probe_ok": bool(lsp_cert.get("warm_probe_ok", False)),
        "verified_edges": inum(lsp_cert.get("verified_edges")),
        "corrected_edges": inum(lsp_cert.get("corrected_edges")),
        "deleted_edges": inum(lsp_cert.get("deleted_edges")),
        "language": lsp_cert.get("language", ""),
        "per_language": per_lang_lsp,
    },
    "gates": {
        "gate1_resolution": gv.get("resolution_jarvis"),
        "gate2_lsp": gv.get("lsp_enrichment"),
        "gate3_embedder": gv.get("embedder"),
        "all_on": gv.get("all_on"),
        "det_pct": gr.get("det_pct"),
        "effective_w_sem": ge.get("effective_w_sem"),
    },
    "timings_s": {
        "task_pull": inum(os.environ.get("T_TASK_PULL_S", "-1"), -1),
        "proof": inum(os.environ.get("T_PROOF_S", "-1"), -1),
        "agent": inum(os.environ.get("T_AGENT_S", "-1"), -1),
        "substrate_pull": inum(os.environ.get("T_SUBSTRATE_PULL_S", "-1"), -1),
    },
    "task_repo_commit": os.environ.get("TASK_REPO_COMMIT", ""),
    "deepswe_bench_sha": os.environ.get("BENCH_SHA", ""),
    "gt_git_commit": os.environ.get("GT_GIT_COMMIT", ""),
    "substrate_digest": os.environ.get("GT_SUBSTRATE_DIGEST", ""),
    "run_id": os.environ.get("SWEEP_RUN_ID", ""),
    "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
with open(os.path.join(out, "row.json"), "w", encoding="utf-8") as fo:
    json.dump(row, fo, separators=(",", ":"))
PYEOF

  # ── (h) prune-as-you-go: the task base image is per-task; the pier-built agent
  #    image layers on top of it. rmi best-effort (in-use bases survive), then
  #    drop dangling layers. The substrate (pulled by digest) is NOT touched. ──
  docker rmi "$img" >/dev/null 2>&1 || true
  docker container prune -f >/dev/null 2>&1 || true
  docker image prune -f >/dev/null 2>&1 || true

  local done_n
  done_n=$(find "$OUT_DIR" -mindepth 2 -maxdepth 2 -name row.json 2>/dev/null | wc -l | tr -d ' ')
  echo "[$done_n/$TOTAL_TASKS] $id lang=$lang class=${FAIL_CLASS:-OK} pier_rc=$PIER_RC pull=${T_TASK_PULL_S}s proof=${T_PROOF_S}s agent=${T_AGENT_S}s reuse=$PROOF_REUSED"
  return 0
}

export -f run_task disk_guard pier_timeout_teardown budget_exceeded
export OUT_DIR BENCH_DIR REPO_ROOT GHCR_OWNER GT_SUBSTRATE_DIGEST GT_GIT_COMMIT \
       SWEEP_RUN_ID MODEL PIER_CONFIG RETRY_FAILED REUSE_PROOF_DIR TASK_TIMEOUT_S \
       T_SUBSTRATE_PULL_S TOTAL_TASKS DISK_MIN_GB DISK_WAIT_MAX_S DOCKER_DF_PATH \
       GT_DEEPSWE_BENCH_SHA GT_METRICS_DIR STOP_AT_COST

# ── N-parallel sweep (xargs -P; one line per task, classification never aborts) ──
xargs -a "$TASKS_TSV" -d '\n' -n 1 -P "$PARALLEL" bash -c 'run_task "$0"'

# ── aggregate: class tally -> AGENT_SWEEP_REPORT.md; exit mirrors harness health ──
OUT_DIR="$OUT_DIR" TASKS_TSV="$TASKS_TSV" SWEEP_RUN_ID="$SWEEP_RUN_ID" \
MODEL="$MODEL" PIER_CONFIG="$PIER_CONFIG" GT_SUBSTRATE_DIGEST="$GT_SUBSTRATE_DIGEST" \
python3 << 'PYEOF'
import json, os, sys, collections
out = os.environ["OUT_DIR"]
ids = [l.split("\t")[0] for l in open(os.environ["TASKS_TSV"], encoding="utf-8") if l.strip()]
rows, missing = [], []
for tid in ids:
    p = os.path.join(out, tid, "row.json")
    if os.path.isfile(p):
        try:
            rows.append(json.load(open(p, encoding="utf-8")))
        except Exception:
            missing.append(tid)
    else:
        missing.append(tid)
runner_cls = collections.Counter((r.get("failure_class") or "OK") for r in rows)
outcome_cls = collections.Counter((r.get("outcome_class") or "UNKNOWN") for r in rows)
resolved = [r["instance_id"] for r in rows if r.get("outcome_class") == "RESOLVED"]
denom = [r for r in rows if r.get("in_resolved_denominator")]
lang_tab = collections.defaultdict(lambda: collections.Counter())
for r in rows:
    lang_tab[r.get("language") or "?"][r.get("outcome_class") or "UNKNOWN"] += 1
lines = [
    "# DeepSWE GT agent sweep — report",
    "",
    f"- run_id: `{os.environ.get('SWEEP_RUN_ID','')}`",
    f"- model: `{os.environ.get('MODEL','')}` | config: `{os.path.basename(os.environ.get('PIER_CONFIG',''))}`",
    f"- substrate: `{os.environ.get('GT_SUBSTRATE_DIGEST','')}`",
    f"- tasks: {len(ids)} | rows: {len(rows)} | missing rows: {len(missing)}",
    "",
    "## Runner classes (harness health)",
    "",
] + [f"- {k}: {v}" for k, v in sorted(runner_cls.items())] + [
    "",
    "## Outcome classes (deepswe_outcome.py)",
    "",
] + [f"- {k}: {v}" for k, v in sorted(outcome_cls.items())] + [
    "",
    f"## Resolved: {len(resolved)} / denominator {len(denom)}",
    "",
] + [f"- {t}" for t in sorted(resolved)] + [
    "",
    "## Per-language outcome",
    "",
] + [f"- {lang}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items()))
     for lang, c in sorted(lang_tab.items())]
if missing:
    lines += ["", "## MISSING ROWS (harness failure — rerun)", ""] + [f"- {t}" for t in missing]
open(os.path.join(out, "AGENT_SWEEP_REPORT.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("\n".join(lines))
# Exit: nonzero on missing rows or any non-AGENT harness/GT-wire failure class.
# AGENT-class misses (reward 0, clean harness) are benchmark signal, not failure.
bad = bool(missing) or any((r.get("failure_class") or "") for r in rows)
sys.exit(1 if bad else 0)
PYEOF
AGG_RC=$?
echo "AGENT_SWEEP_REPORT: $OUT_DIR/AGENT_SWEEP_REPORT.md (exit=$AGG_RC)"
exit "$AGG_RC"
