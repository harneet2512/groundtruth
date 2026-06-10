#!/usr/bin/env bash
# scripts/vm/gt_proof_sweep.sh — GENERIC VM proof-sweep runner.
#
# Manifest-driven port of .github/workflows/deepswe_proof_sweep.yml's per-task logic
# (byte-mirrored semantics): per task — GHCR-first task-image pull (3-retry backoff,
# classify TASK_IMAGE_PULL_FAIL) -> source extraction (docker run + .git probe over
# /home/user /testbed /workspace /app /repo) -> the IDENTICAL pinned gt-run-proof
# command (8 proof flags, repo mounted /work:ro) -> the SAME row.json shape the
# workflow writes -> docker rmi the task image (disk-bounded for 731-task sweeps).
#
# NO LLM, NO agent, NO gold/task-specific logic. Failures are CLASSIFIED, never
# silent. The substrate is referenced by IMMUTABLE @sha256 digest (fail-closed
# GT_SUBSTRATE_DIGEST_MISSING).
#
# HOST-GENERIC: any Linux box with docker + python3 + bash. ZERO cloud-provider
# specifics — no project IDs, no account names, no credentials, no provider CLI
# commands. Optional GHCR auth ONLY via pre-set env (GHCR_USER/GHCR_TOKEN), never
# stored, never required for public images.
#
# Inputs (env, overridable by flags):
#   MANIFEST             manifest json: {"tasks":[{"instance_id","language","docker_image"},...]}
#                        (artifact_deepswe/repo_manifest.json or pro_manifest.json)
#   GT_SUBSTRATE_DIGEST  ghcr.io/<org>/gt-substrate@sha256:<digest>  REQUIRED — immutable,
#                        fail-closed GT_SUBSTRATE_DIGEST_MISSING (mutable tags rejected)
#   PARALLEL             default: nproc/2, capped at 24
#   OUT_DIR              default: ./sweep_out  (per-task artifacts: $OUT_DIR/<instance_id>/)
#   GHCR_OWNER           default: hbali-stack  (GHCR cache owner for task-image pulls)
#   MAX_TASKS            optional truncation ('' = all)
#   GT_GIT_COMMIT        optional provenance sha (default: git rev-parse HEAD if available)
#   RETRY_FAILED         1 = re-run tasks whose existing row.json is a classified failure
#   DISK_MIN_GB          default 25  — free-space floor checked before each pull
#   DISK_WAIT_MAX_S      default 1800 — max warn+wait for disk before classifying DISK_LOW
#   WORK_DIR             scratch for extracted sources (default: $OUT_DIR/.work)
#
# Flags: --manifest P --digest D --parallel N --out DIR --ghcr-owner O --max-tasks N
#
# RESUMABLE: tasks whose $OUT_DIR/<instance_id>/row.json already exists are skipped
# (idempotent re-runs; set RETRY_FAILED=1 to retry classified failures).
#
# Aggregate: scripts/vm/aggregate_sweep.py -> $OUT_DIR/SWEEP_REPORT.md
# (per-language table + optimization verdict; runner exit code mirrors the verdict —
# nonzero on any failing/missing task, never silent).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── defaults (env-overridable) ───────────────────────────────────────────────
MANIFEST="${MANIFEST:-}"
GT_SUBSTRATE_DIGEST="${GT_SUBSTRATE_DIGEST:-}"
OUT_DIR="${OUT_DIR:-$PWD/sweep_out}"
GHCR_OWNER="${GHCR_OWNER:-hbali-stack}"
MAX_TASKS="${MAX_TASKS:-}"
RETRY_FAILED="${RETRY_FAILED:-0}"
DISK_MIN_GB="${DISK_MIN_GB:-25}"
DISK_WAIT_MAX_S="${DISK_WAIT_MAX_S:-1800}"
NPROC=$( (nproc 2>/dev/null) || echo 4 )
DEFAULT_PAR=$(( NPROC / 2 )); [ "$DEFAULT_PAR" -lt 1 ] && DEFAULT_PAR=1; [ "$DEFAULT_PAR" -gt 24 ] && DEFAULT_PAR=24
PARALLEL="${PARALLEL:-$DEFAULT_PAR}"

# ── flag overrides ───────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --manifest)   MANIFEST="$2"; shift 2 ;;
    --digest)     GT_SUBSTRATE_DIGEST="$2"; shift 2 ;;
    --parallel)   PARALLEL="$2"; shift 2 ;;
    --out)        OUT_DIR="$2"; shift 2 ;;
    --ghcr-owner) GHCR_OWNER="$2"; shift 2 ;;
    --max-tasks)  MAX_TASKS="$2"; shift 2 ;;
    -h|--help)    grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "FATAL: unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ "$PARALLEL" -gt 24 ] 2>/dev/null && PARALLEL=24

WORK_DIR="${WORK_DIR:-$OUT_DIR/.work}"
SWEEP_RUN_ID="${SWEEP_RUN_ID:-vm_$(date -u +%Y%m%dT%H%M%SZ)_$$}"
GT_METRICS_DIR="${GT_METRICS_DIR:-$SCRIPT_DIR/../metrics}"

# ── preflight ────────────────────────────────────────────────────────────────
if [ -z "$MANIFEST" ] || [ ! -f "$MANIFEST" ]; then
  echo "FATAL: MANIFEST not set or not a file: '${MANIFEST}'" >&2; exit 2
fi
command -v docker >/dev/null 2>&1 || { echo "FATAL: docker not found on PATH" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "FATAL: python3 not found on PATH" >&2; exit 2; }

# Digest precheck (fail-closed — GT_SUBSTRATE_DIGEST_MISSING). Mirrors prepare job.
if [ -z "${GT_SUBSTRATE_DIGEST:-}" ]; then
  echo "GT_SUBSTRATE_DIGEST_MISSING: no pinned substrate digest. The proof sweep has NO"
  echo "fallback runtime — aborting before any task."
  exit 1
fi
case "$GT_SUBSTRATE_DIGEST" in
  *@sha256:*) echo "pinned substrate OK: $GT_SUBSTRATE_DIGEST" ;;
  *) echo "GT_SUBSTRATE_DIGEST_MISSING: '$GT_SUBSTRATE_DIGEST' is not an immutable @sha256 digest (mutable tags are not a valid proof input)"; exit 1 ;;
esac

if [ -z "${GT_GIT_COMMIT:-}" ]; then
  GT_GIT_COMMIT="$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || true)"
fi

mkdir -p "$OUT_DIR" "$WORK_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"

DOCKER_DF_PATH="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
[ -n "$DOCKER_DF_PATH" ] && [ -d "$DOCKER_DF_PATH" ] || DOCKER_DF_PATH="/"

# ── task list from the manifest (fail-closed field check; mirrors prepare/gen) ──
TASKS_TSV="$OUT_DIR/tasks.tsv"
MANIFEST="$MANIFEST" OUT_DIR="$OUT_DIR" MAX_TASKS="$MAX_TASKS" \
SWEEP_RUN_ID="$SWEEP_RUN_ID" GT_SUBSTRATE_DIGEST="$GT_SUBSTRATE_DIGEST" \
GT_GIT_COMMIT="$GT_GIT_COMMIT" python3 << 'PYEOF' || exit 1
import json, os, sys
mt = (os.environ.get("MAX_TASKS") or "").strip()
with open(os.environ["MANIFEST"], encoding="utf-8") as f:
    man = json.load(f)
tasks = man.get("tasks") or []
if not tasks:
    print("FATAL: manifest has no tasks", file=sys.stderr)
    sys.exit(1)
# Fail-closed: every entry must carry the fields the proof task consumes.
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
               "benchmark": man.get("benchmark", "")}, f, indent=1)
langs = {}
for t in tasks:
    langs[t["language"]] = langs.get(t["language"], 0) + 1
print(f"{len(include)} tasks queued (max_tasks={mt or 'all'}) langs={langs}",
      file=sys.stderr)
PYEOF

TOTAL_TASKS=$(wc -l < "$TASKS_TSV" | tr -d ' ')
echo "sweep: $TOTAL_TASKS tasks | parallel=$PARALLEL | out=$OUT_DIR | run_id=$SWEEP_RUN_ID"
echo "substrate: $GT_SUBSTRATE_DIGEST"
echo "gt commit: ${GT_GIT_COMMIT:-<unknown>}"

# ── optional GHCR login (best-effort; public images need no auth). Env-only,
#    never stored. Mirrors the workflow's best-effort login semantics. ──
if [ -n "${GHCR_TOKEN:-}" ]; then
  echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER:-$GHCR_OWNER}" --password-stdin || true
fi

# ── pull the pinned substrate ONCE (3-retry backoff; the digest never changes,
#    so per-task pulls in the workflow == one cached pull on a single host). ──
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

# ── disk guard: free-space floor before each pull; warn+wait, prune dangling. ──
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

# ── per-task runner (mirrors the workflow's proof job, step for step) ─────────
run_task() {
  local line="$1" id lang img
  IFS=$'\t' read -r id lang img <<< "$line"
  [ -z "$id" ] && return 0

  local out_task="$OUT_DIR/$id"
  local src_dir="$WORK_DIR/$id/src"
  local ctr="gtsrc_$(printf '%s' "$id" | tr -c 'a-zA-Z0-9_.-' '_')"

  # RESUMABLE: skip when a row already exists (RETRY_FAILED=1 re-runs failures).
  if [ -f "$out_task/row.json" ]; then
    if [ "$RETRY_FAILED" = "1" ] && ROW="$out_task/row.json" python3 -c '
import json, os, sys
r = json.load(open(os.environ["ROW"], encoding="utf-8"))
sys.exit(0 if (r.get("failure_class") or r.get("proof_exit_code", -1) != 0) else 1)
'; then
      echo "[retry] $id — prior row was a classified failure; re-running"
      rm -f "$out_task/row.json"
    else
      echo "[skip] $id — row.json exists (resumable re-run)"
      return 0
    fi
  fi

  mkdir -p "$out_task"
  # Per-task state (the workflow's Init step / GITHUB_ENV contract).
  local FAIL_CLASS="" PROOF_RC=-1 T_TASK_PULL_S=-1 T_EXTRACT_S=-1 T_PROOF_S=-1 PULL_SOURCE=""
  local T0 RC

  # ── Pull task image (GHCR-first, source fallback, 3-retry backoff) ──
  if [ -z "$FAIL_CLASS" ]; then
    if ! disk_guard; then
      FAIL_CLASS="DISK_LOW"
      echo "[disk] $id: DISK_LOW after ${DISK_WAIT_MAX_S}s wait — classified" >&2
    fi
  fi
  if [ -z "$FAIL_CLASS" ]; then
    T0=$(date +%s)
    # GHCR tag derivation — same as deepswe_full.yml / the sweep workflow:
    #   IMG = <registry>/<base>:<tag>  ->  GHCR = ghcr.io/<owner>/<base>:<tag>
    # (When IMG is already the GHCR mirror ref — e.g. Pro's sweap-images — the
    #  candidate equals IMG and the pull is direct.)
    local TAG="${img##*:}"
    local BASE="${img##*/}"; BASE="${BASE%%:*}"
    local PULLED="" CAND
    for CAND in "ghcr.io/${GHCR_OWNER}/${BASE}:${TAG}"; do
      if docker pull "$CAND" >>"$out_task/pull.log" 2>&1; then
        [ "$CAND" != "$img" ] && docker tag "$CAND" "$img"
        PULLED="$CAND"
        break
      fi
      echo "GHCR miss: $CAND" >>"$out_task/pull.log"
    done
    if [ -z "$PULLED" ]; then
      echo "GHCR miss — pulling source ref with 3-retry backoff: $img" >>"$out_task/pull.log"
      local i
      for i in 1 2 3; do
        if docker pull "$img" >>"$out_task/pull.log" 2>&1; then PULLED="$img"; break; fi
        echo "pull attempt $i failed; backoff $((i*20))s" >>"$out_task/pull.log"
        sleep $((i*20))
      done
    fi
    T_TASK_PULL_S=$(( $(date +%s) - T0 ))
    if [ -z "$PULLED" ] || ! docker image inspect "$img" >/dev/null 2>&1; then
      FAIL_CLASS="TASK_IMAGE_PULL_FAIL"
      echo "TASK_IMAGE_PULL_FAIL: $img (GHCR candidate + source ref all failed)" >>"$out_task/pull.log"
    else
      PULL_SOURCE="$PULLED"
    fi
  fi

  # ── Extract repo source (docker run + docker cp, as deepswe_full) ──
  if [ -z "$FAIL_CLASS" ]; then
    T0=$(date +%s)
    docker rm -f "$ctr" >/dev/null 2>&1 || true
    if ! docker run -d --name "$ctr" "$img" sleep 1800 >/dev/null 2>>"$out_task/pull.log"; then
      FAIL_CLASS="SRC_EXTRACT_FAIL"
    else
      # Same repo-root probe as deepswe_full.yml: first dir with a .git, default /testbed.
      local ROOT
      ROOT=$(docker exec "$ctr" bash -c 'for d in /home/user /testbed /workspace /app /repo; do [ -d "$d/.git" ] && echo "$d" && break; done' 2>/dev/null || true)
      ROOT=${ROOT:-/testbed}
      rm -rf "$src_dir" && mkdir -p "$src_dir"
      if ! docker cp "$ctr:$ROOT/." "$src_dir" >/dev/null 2>>"$out_task/pull.log"; then
        FAIL_CLASS="SRC_EXTRACT_FAIL"
      fi
      docker rm -f "$ctr" >/dev/null 2>&1 || true
      # Free disk before the proof (the task image is no longer needed) —
      # disk-bounded for 731-image sweeps.
      docker rmi "$img" "${PULL_SOURCE:-$img}" >/dev/null 2>&1 || true
      T_EXTRACT_S=$(( $(date +%s) - T0 ))
      if [ -z "$FAIL_CLASS" ]; then
        local N_FILES
        N_FILES=$(find "$src_dir" -type f 2>/dev/null | wc -l | tr -d ' ')
        if [ "$N_FILES" -eq 0 ]; then
          FAIL_CLASS="SRC_EXTRACT_FAIL"
        fi
      fi
    fi
    [ "$T_EXTRACT_S" -lt 0 ] && T_EXTRACT_S=$(( $(date +%s) - T0 ))
  fi

  # ── gt-run-proof inside the pinned substrate (the ONE identical command per task) ──
  if [ -z "$FAIL_CLASS" ]; then
    T0=$(date +%s)
    # The byte-identical proof command: 8 proof flags + GT_GIT_COMMIT provenance.
    # Repo mounted /work:ro (never mutated); artifacts -> /gt_artifacts. No LLM,
    # no agent, no issue/gold input.
    # Memory cap per proof container: a runaway encode OOMs the CONTAINER (exit 137,
    # classified row) instead of the HOST (global OOM killed the box 2026-06-10: one
    # python3 hit 15.8G RSS at PARALLEL=8 -> sshd/DHCP paralysis). 4 x 7g fits 32G.
    docker run --rm \
      --memory="${GT_PROOF_MEM:-7g}" --memory-swap="${GT_PROOF_MEM:-7g}" \
      -v "$src_dir:/work:ro" \
      -v "$out_task:/gt_artifacts" \
      -e GT_PROOF_MODE=1 \
      -e GT_CONTAINERIZED=1 \
      -e GT_RUNTIME_STRATEGY=unified_substrate \
      -e GT_REQUIRE_FTS5=1 \
      -e GT_REQUIRE_EMBEDDER=1 \
      -e GT_FORCE_ONNX_EMBEDDER=1 \
      -e GT_REQUIRE_LSP=1 \
      -e GT_REQUIRE_FULL_STACK=1 \
      -e GT_GIT_COMMIT="$GT_GIT_COMMIT" \
      -e GT_SUBSTRATE_DIGEST="$GT_SUBSTRATE_DIGEST" \
      "$GT_SUBSTRATE_DIGEST" gt-run-proof --source-root /work --out /gt_artifacts \
      > "$out_task/proof_run.log" 2>&1
    RC=$?
    T_PROOF_S=$(( $(date +%s) - T0 ))
    PROOF_RC=$RC
    if [ "$RC" -ne 0 ]; then
      # rc is THE diagnostic bit: 137 = kill (memcg OOM under --memory / SIGKILL) — the
      # process died before any fail-closed stderr print could run; never conflate with
      # a fail-closed exit 2. (The 2026-06-10 brief-OOM hid behind the generic class.)
      if [ "$RC" -eq 137 ]; then FAIL_CLASS="GT_PROOF_OOM"; else FAIL_CLASS="GT_RUN_PROOF_FAIL"; fi
      echo "[proof] rc=$RC class=$FAIL_CLASS" >> "$out_task/proof_run.log"
    fi
  fi

  # ── Build the per-task JSON row (always — failures are classified, never silent).
  #    Byte-mirrors the workflow's row builder; env mapping: GITHUB_SHA->GT_GIT_COMMIT,
  #    GITHUB_RUN_ID->SWEEP_RUN_ID, /tmp/gt/out -> $out_task. ──
  TASK_ID="$id" TASK_LANG="$lang" IMG="$img" PULL_SOURCE="$PULL_SOURCE" \
  FAIL_CLASS="$FAIL_CLASS" PROOF_RC="$PROOF_RC" \
  T_TASK_PULL_S="$T_TASK_PULL_S" T_EXTRACT_S="$T_EXTRACT_S" \
  T_SUBSTRATE_PULL_S="$T_SUBSTRATE_PULL_S" T_PROOF_S="$T_PROOF_S" \
  OUT_TASK="$out_task" SWEEP_RUN_ID="$SWEEP_RUN_ID" GT_METRICS_DIR="$GT_METRICS_DIR" \
  python3 << 'PYEOF'
import json, os, sys, time
out = os.environ["OUT_TASK"]
os.makedirs(out, exist_ok=True)

def jload(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

REQ = ["graph.db", "runtime_context.json", "lsp_certificate.json",
       "graph_certificate.json", "embedder_certificate.json",
       "foundational_gate_report.json", "run_manifest.json"]
present = {a: os.path.exists(os.path.join(out, a)) for a in REQ}
lsp = jload(os.path.join(out, "lsp_certificate.json")) or {}
emb = jload(os.path.join(out, "embedder_certificate.json")) or {}
gates = jload(os.path.join(out, "foundational_gate_report.json")) or {}
man = jload(os.path.join(out, "run_manifest.json")) or {}

# Embedder verdict via the canonical classifier (scripts/metrics, stdlib-only).
verdict, ok = "EMBEDDER_CERT_ABSENT", False
if emb:
    try:
        sys.path.insert(0, os.environ.get("GT_METRICS_DIR", "scripts/metrics"))
        sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0] or ".")))
        from embedder_certificate import classify_embedder
        verdict, ok = classify_embedder(emb, proof_mode=True, require_embedder=True)
    except Exception as e:
        verdict, ok = f"CLASSIFY_ERROR:{type(e).__name__}", False

def fnum(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d

def inum(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d

gv = gates.get("verdict") or {}
gr = gates.get("gate_resolution") or {}
ge = (gates.get("gate_embedder") or {}).get("consumption") or {}
resolved = (inum(lsp.get("verified_edges")) + inum(lsp.get("corrected_edges"))
            + inum(lsp.get("deleted_edges")))
row = {
    "instance_id": os.environ.get("TASK_ID", ""),
    "language": os.environ.get("TASK_LANG", ""),
    "image": os.environ.get("IMG", ""),
    "pull_source": os.environ.get("PULL_SOURCE", ""),
    "failure_class": os.environ.get("FAIL_CLASS", ""),
    "proof_exit_code": inum(os.environ.get("PROOF_RC", "-1"), -1),
    "artifacts_present": present,
    "artifacts_present_count": sum(1 for v in present.values() if v),
    "lsp": {
        "verdict_hint": lsp.get("verdict_hint", ""),
        "lsp_warm": bool(lsp.get("lsp_warm", False)),
        "server_launched": bool(lsp.get("server_launched", False)),
        "warm_probe_ok": bool(lsp.get("warm_probe_ok", False)),
        "probe_latency_ms": fnum(lsp.get("probe_latency_ms")),
        "resolved": resolved,
        "verified_edges": inum(lsp.get("verified_edges")),
        "corrected_edges": inum(lsp.get("corrected_edges")),
        "deleted_edges": inum(lsp.get("deleted_edges")),
        "residual": inum(lsp.get("residual")),
        "attempted_edges": inum(lsp.get("attempted_edges")),
        "language": lsp.get("language", ""),
        "scoped_source_files": inum(lsp.get("scoped_source_files")),
    },
    "embedder": {
        "verdict": verdict,
        "ok": bool(ok),
        "class": emb.get("embedder_class", ""),
        "dim": emb.get("embedder_dim"),
        "models_root": emb.get("GT_MODELS_ROOT", ""),
        "discrimination_margin": emb.get("discrimination_margin"),
        "model_download_attempted": bool(emb.get("model_download_attempted", False)),
        "semantic_candidate_count": inum(emb.get("semantic_candidate_count")),
        "rendered_semantic_nonzero_count": inum(emb.get("rendered_semantic_nonzero_count")),
        "effective_w_sem": emb.get("effective_w_sem"),
    },
    "gates": {
        "gate1_resolution": gv.get("resolution_jarvis"),
        "gate2_lsp": gv.get("lsp_enrichment"),
        "gate3_embedder": gv.get("embedder"),
        "all_on": gv.get("all_on"),
        "det_pct": gr.get("det_pct"),
        "w_sem": ge.get("effective_w_sem"),
    },
    "timings_s": {
        "task_pull": inum(os.environ.get("T_TASK_PULL_S", "-1"), -1),
        "extract": inum(os.environ.get("T_EXTRACT_S", "-1"), -1),
        "substrate_pull": inum(os.environ.get("T_SUBSTRATE_PULL_S", "-1"), -1),
        "proof": inum(os.environ.get("T_PROOF_S", "-1"), -1),
    },
    "languages_in_graph": man.get("languages"),
    "gt_git_commit": os.environ.get("GT_GIT_COMMIT", ""),
    "substrate_digest": os.environ.get("GT_SUBSTRATE_DIGEST", ""),
    "run_id": os.environ.get("SWEEP_RUN_ID", ""),
    "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
with open(os.path.join(out, "row.json"), "w", encoding="utf-8") as fo:
    json.dump(row, fo, separators=(",", ":"))
PYEOF

  # Scratch cleanup (extracted source no longer needed once the proof ran).
  rm -rf "$WORK_DIR/$id" 2>/dev/null || true

  # Per-task one-liner (tail-able).
  local done_n
  done_n=$(find "$OUT_DIR" -mindepth 2 -maxdepth 2 -name row.json 2>/dev/null | wc -l | tr -d ' ')
  echo "[$done_n/$TOTAL_TASKS] $id lang=$lang class=${FAIL_CLASS:-OK} exit=$PROOF_RC pull=${T_TASK_PULL_S}s extract=${T_EXTRACT_S}s proof=${T_PROOF_S}s src=${PULL_SOURCE:-none}"
  return 0
}

export -f run_task disk_guard
export OUT_DIR WORK_DIR GHCR_OWNER GT_SUBSTRATE_DIGEST GT_GIT_COMMIT SWEEP_RUN_ID \
       GT_METRICS_DIR T_SUBSTRATE_PULL_S TOTAL_TASKS RETRY_FAILED \
       DISK_MIN_GB DISK_WAIT_MAX_S DOCKER_DF_PATH

# ── N-parallel sweep (xargs -P; one line per task, classification never aborts) ──
xargs -a "$TASKS_TSV" -d '\n' -n 1 -P "$PARALLEL" bash -c 'run_task "$0"'

# ── aggregate: per-language table + optimization verdict -> SWEEP_REPORT.md ──
python3 "$SCRIPT_DIR/aggregate_sweep.py" \
  --out-dir "$OUT_DIR" \
  --tasks "$OUT_DIR/sweep_tasks.json" \
  --report "$OUT_DIR/SWEEP_REPORT.md"
AGG_RC=$?
echo "SWEEP_REPORT: $OUT_DIR/SWEEP_REPORT.md (exit=$AGG_RC)"
exit "$AGG_RC"
