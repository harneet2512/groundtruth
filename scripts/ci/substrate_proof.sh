#!/usr/bin/env bash
# GT substrate proof (handoff §B-AFTER/§D/§G) — pre-agent, pinned, read-only repo
#
# Extracted from deepswe_full.yml / swebench_pro_full.yml to stay under GHA's
# 21,000-character expression limit. Behaviour is IDENTICAL to the inline block.
#
# Required env vars (exported by the workflow before calling this script):
#   GT_MATRIX_TASK                  — ${{ matrix.task }}
#   GT_MATRIX_IMAGE                 — ${{ matrix.image }}         (deepswe only; empty for pro)
#   GT_MATRIX_LANGUAGE              — ${{ matrix.language }}
#   GT_INPUTS_ASSETS_REPO           — ${{ inputs.assets_repo }}
#   GT_GITHUB_REPOSITORY_OWNER      — ${{ github.repository_owner }}
#   GT_GITHUB_SHA                   — ${{ github.sha }}
#
# Plus all top-level env: vars already in the runner environment:
#   GT_SUBSTRATE_DIGEST, GT_REQUIRE_PINNED_SUBSTRATE, GT_LSP_READY_BUDGET_S_OVERRIDE,
#   GT_GATES_DELIVER_ALWAYS, GT_REQUIRE_LSP, GITHUB_ENV, etc.
#
# HARNESS VARIANT (deepswe vs pro) is controlled by GT_PROOF_HARNESS:
#   deepswe  (default) — uses GT_MATRIX_IMAGE + instruction.md issue extraction
#   pro                — uses GT_MATRIX_DOCKERHUB_TAG + Pro-OS problem_statement extraction
#
# The script is sourced from the repo checkout root so relative paths (scripts/…)
# resolve correctly.
set -e

HARNESS="${GT_PROOF_HARNESS:-deepswe}"

# ── Deterministic float-reduction pins (FIX 1b) ──────────────────────────────────────────
# The brief-determinism proof reduces the same embedding cosines twice (gate subprocess vs
# in-process witness). numpy/BLAS pooling+normalisation read these thread counts at process
# start; unset (the live env sets none), >1 thread reduces in different orders -> a near-tie
# cosine flips top-k membership -> DETERMINISM_MISMATCH. Export them here so the gt-run-proof
# CONTAINER process (below, `docker run ... -e OMP_NUM_THREADS ...`) starts pinned. This reaches
# the runtime WITHOUT a substrate rebake (the container inherits the value at launch); the
# Dockerfile ENV is the belt for the next rebake. Never lowers a gate; determinism only.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"

# ── SUBSTRATE-CONSUME pattern (mirrors swebench_300task.yml:919-944) ──────────
# Replaces the OLD host/image split (§B BEFORE/invalid): NO host gt-index build,
# NO host `groundtruth.resolve` LSP pass, NO host gates. The pinned substrate
# image runs gt-run-proof INSIDE the container with the task repo mounted /work:ro
# and emits the 8 artifacts to /gt_artifacts; the agent CONSUMES them read-only.
if [ "$HARNESS" = "deepswe" ]; then
  TASK_DIR="deepswe-bench/tasks/${GT_MATRIX_TASK}"
fi

mkdir -p /tmp/gt
PROOF_STATUS=/tmp/gt/proof_status.json
write_proof_status() {
  python3 - "$PROOF_STATUS" "$1" "$2" "${3:-}" <<'PY'
import json, os, sys, time
path, state, code, detail = sys.argv[1:5]
os.makedirs(os.path.dirname(path), exist_ok=True)
# Fail-closed by construction: never downgrade a 'failed' verdict to 'ok' (a legitimacy/
# anti-cheat/OOM/no-build failure must stay failed so the paid-run gate refuses to spend).
# Only an explicit GT_PROOF_FORCE_OK=1 may override (none of the launder paths set it).
try:
    with open(path, encoding="utf-8") as _pf:
        if json.load(_pf).get("state") == "failed" and state == "ok" \
           and os.environ.get("GT_PROOF_FORCE_OK") != "1":
            sys.exit(0)
except Exception:
    pass
with open(path, "w", encoding="utf-8") as f:
    json.dump({
        "schema": "gt.proof_status.v1",
        "state": state,
        "code": code,
        "detail": detail,
        "ts": time.time(),
    }, f, indent=2, sort_keys=True)
    f.write("\n")
PY
}
fail_proof() {
  _code="$1"; shift || true
  _detail="$*"
  echo "GT_RUN_PROOF_FAIL: ${_code}: ${_detail}" | tee -a trial_output.log
  write_proof_status failed "$_code" "$_detail"
  exit 1
}
fail_artifact() {
  _detail="$*"
  echo "GT_ARTIFACT_MISSING: ${_detail} — proceeding (non-fatal)" | tee -a trial_output.log
  # Record but do NOT block. The agent can still run with degraded GT
  # (missing cert = some layers suppress, which is correct-or-quiet).
}
write_proof_status running PROOF_STARTED "substrate proof step started"

# Memory logger: runs in background, writes to trial_output.log every 10s.
# Survives partial writes on SIGTERM — each line is flushed independently.
# This is the ONLY way to get memory evidence when the runner OOM-kills.
(
  while true; do
    echo "[MEMLOG $(date +%H:%M:%S)] $(free -m | grep Mem | awk '{printf "used=%sMB total=%sMB swap_used=", $3, $2}')$(free -m | grep Swap | awk '{print $3"MB"}')" >> trial_output.log 2>/dev/null
    sleep 10
  done
) &
_MEMLOG_PID=$!
trap 'kill "$_MEMLOG_PID" 2>/dev/null' EXIT

# §E GT_SUBSTRATE_DIGEST_MISSING / PROOF_RUNTIME_FALLBACK_FORBIDDEN (fail-closed).
# G1: every §E marker echo ALSO appends to trial_output.log (tee -a creates it) —
# this step exits BEFORE the agent step creates the log, and deepswe_outcome.py's
# INFRA classification scans trial_output.log line-anchored. Without the tee the
# failure classified UNKNOWN instead of INFRA.
if [ -z "$GT_SUBSTRATE_DIGEST" ]; then
  if [ "${GT_REQUIRE_PINNED_SUBSTRATE:-1}" = "1" ]; then
    echo "GT_SUBSTRATE_DIGEST_MISSING / PROOF_RUNTIME_FALLBACK_FORBIDDEN: proof/substrate-consume" | tee -a trial_output.log
    write_proof_status failed GT_SUBSTRATE_DIGEST_MISSING "mode requires GT_SUBSTRATE_DIGEST"
    echo "mode requires GT_SUBSTRATE_DIGEST (the pinned portable substrate, by @sha256 digest)."
    echo "Host GT execution + per-task provisioning are forbidden. Set the gt_substrate_digest"
    echo "input or the GT_SUBSTRATE_DIGEST repo variable to the published image digest."
    exit 1
  fi
  echo "::warning::GT_SUBSTRATE_DIGEST unset and GT_REQUIRE_PINNED_SUBSTRATE=0 (transitional)"
  echo "::warning::— skipping the substrate proof; the agent runs with the brief OFF (no host GT)."
  exit 0
fi
# §F.1 immutable-digest assertion: a mutable tag is NOT a valid proof input.
case "$GT_SUBSTRATE_DIGEST" in
  *@sha256:*) : ;;
  *) fail_proof GT_SUBSTRATE_DIGEST_MISSING "GT_SUBSTRATE_DIGEST='$GT_SUBSTRATE_DIGEST' is not a pinned @sha256 digest" ;;
esac
echo "pinned substrate: $GT_SUBSTRATE_DIGEST"

# ── Task image pull ────────────────────────────────────────────────────────────
if [ "$HARNESS" = "deepswe" ]; then
  # Task image comes from the MATRIX (manifest-driven; prepare already
  # cross-checked it byte-equal against the clone's task.toml docker_image).
  IMG="${GT_MATRIX_IMAGE}"
  [ -z "$IMG" ] && fail_proof TASK_IMAGE_PULL_FAIL "matrix carried no task image"
  echo "task image: $IMG"
  # GHCR-FIRST pull (cache_deepswe_images.yml mirrors the source images to GHCR
  # from the SAME manifest, so the cache-set == this run-set). MULTI-CANDIDATE
  # (proof-sweep pattern): assets_repo owner, then hbali-stack (the run-infra
  # account that ran the mirror), then this repo's owner — kills the
  # owner-mismatch cache miss. ECR-public source is the retried fallback.
  A_OWNER=$(echo "${GT_INPUTS_ASSETS_REPO}" | cut -d/ -f1 | tr '[:upper:]' '[:lower:]')
  R_OWNER=$(echo "${GT_GITHUB_REPOSITORY_OWNER}" | tr '[:upper:]' '[:lower:]')
  TAG="${IMG##*:}"
  BASE="${IMG##*/}"; BASE="${BASE%%:*}"
  PULLED=""
  for CAND_OWNER in "$A_OWNER" "hbali-stack" "$R_OWNER"; do
    GHCR="ghcr.io/${CAND_OWNER}/${BASE}:${TAG}"
    case " $TRIED " in *" $GHCR "*) continue ;; esac
    TRIED="${TRIED:-} $GHCR"
    if docker pull "$GHCR" 2>/dev/null; then
      docker tag "$GHCR" "$IMG"; PULLED="$GHCR"; echo "pulled from GHCR cache: $GHCR"; break
    fi
    echo "GHCR miss: $GHCR"
  done
  if [ -z "$PULLED" ]; then
    echo "all GHCR candidates missed — pulling ECR-public source with retries"
    for i in 1 2 3 4; do docker pull "$IMG" && { PULLED="$IMG"; break; } || { echo "pull attempt $i failed, retrying..."; sleep 15; }; done
    # G1: canonical token (deepswe_outcome.INFRA_LOG_MARKERS) — the old
    # "FATAL: task image pull failed" echo could never classify INFRA.
    [ -n "$PULLED" ] || { echo "TASK_IMAGE_PULL_FAIL: task image pull failed after 4 attempts ($IMG)" | tee -a trial_output.log; write_proof_status failed TASK_IMAGE_PULL_FAIL "task image pull failed after 4 attempts ($IMG)"; exit 1; }
  fi
  docker image inspect "$IMG" >/dev/null 2>&1 || { echo "TASK_IMAGE_PULL_FAIL: task image not present after pull ($IMG)" | tee -a trial_output.log; write_proof_status failed TASK_IMAGE_PULL_FAIL "task image not present after pull ($IMG)"; exit 1; }
else
  # Pro harness: image name is built from the dockerhub_tag matrix column.
  # The Pro mirror uses jefzda/sweap-images; GHCR candidates are tried first.
  DOCKERHUB_TAG="${GT_MATRIX_DOCKERHUB_TAG}"
  [ -z "$DOCKERHUB_TAG" ] && fail_proof TASK_IMAGE_PULL_FAIL "matrix carried no dockerhub_tag"
  A_OWNER=$(echo "${GT_INPUTS_ASSETS_REPO}" | cut -d/ -f1 | tr '[:upper:]' '[:lower:]')
  R_OWNER=$(echo "${GT_GITHUB_REPOSITORY_OWNER}" | tr '[:upper:]' '[:lower:]')
  PULLED=""
  TRIED=""
  for CAND_OWNER in "$A_OWNER" "hbali-stack" "$R_OWNER"; do
    GHCR="ghcr.io/${CAND_OWNER}/sweap-images:${DOCKERHUB_TAG}"
    case " $TRIED " in *" $GHCR "*) continue ;; esac
    TRIED="${TRIED:-} $GHCR"
    if docker pull "$GHCR" 2>/dev/null; then
      # Tag as the canonical name for downstream steps.
      docker tag "$GHCR" "jefzda/sweap-images:${DOCKERHUB_TAG}"
      PULLED="$GHCR"
      echo "pulled from GHCR cache: $GHCR"
      break
    fi
    echo "GHCR miss: $GHCR"
  done
  if [ -z "$PULLED" ]; then
    echo "all GHCR candidates missed — pulling Docker Hub source with retries"
    DH_IMG="docker.io/jefzda/sweap-images:${DOCKERHUB_TAG}"
    for i in 1 2 3 4; do
      docker pull "$DH_IMG" && { PULLED="$DH_IMG"; break; } || { echo "pull attempt $i failed, retrying..."; sleep 15; }
    done
    [ -n "$PULLED" ] || { echo "TASK_IMAGE_PULL_FAIL: task image pull failed after 4 attempts (${DH_IMG})" | tee -a trial_output.log; write_proof_status failed TASK_IMAGE_PULL_FAIL "task image pull failed after 4 attempts (${DH_IMG})"; exit 1; }
  fi
  IMG="jefzda/sweap-images:${DOCKERHUB_TAG}"
  docker image inspect "$IMG" >/dev/null 2>&1 || { echo "TASK_IMAGE_PULL_FAIL: task image not present after pull ($IMG)" | tee -a trial_output.log; write_proof_status failed TASK_IMAGE_PULL_FAIL "task image not present after pull ($IMG)"; exit 1; }
fi

# ── SPEED-OPT C: load substrate from GHA cache or pull from GHCR ────
# If prepare saved the substrate tarball and the cache restored it above,
# load from the local file (fast, no network). Fall back to GHCR pull if
# the cache file is absent (first run, cache eviction, or oversized).
SUBSTRATE_LOADED=0
if [ -f /tmp/gt-substrate.tar.gz ]; then
  echo "SPEED-OPT C: loading substrate from GHA cache (docker load)"
  if docker load < /tmp/gt-substrate.tar.gz; then
    # docker load with a digest-tagged image may produce a <none> name; tag it.
    docker tag "$GT_SUBSTRATE_DIGEST" "$GT_SUBSTRATE_DIGEST" 2>/dev/null || true
    # Verify the digest is now present in the local daemon.
    if docker image inspect "$GT_SUBSTRATE_DIGEST" >/dev/null 2>&1; then
      echo "substrate loaded from cache OK"
      SUBSTRATE_LOADED=1
    else
      echo "::warning::docker load succeeded but digest not found locally — falling back to GHCR pull"
    fi
  else
    echo "::warning::docker load failed (corrupt tarball?) — falling back to GHCR pull"
  fi
fi
if [ "$SUBSTRATE_LOADED" -eq 0 ]; then
  echo "substrate cache miss — pulling from GHCR (pinned digest)"
  if ! docker pull "$GT_SUBSTRATE_DIGEST"; then
    echo "GT_SUBSTRATE_PULL_FAIL: docker pull of the pinned substrate failed" | tee -a trial_output.log
    write_proof_status failed GT_SUBSTRATE_PULL_FAIL "docker pull of the pinned substrate failed"
    exit 1
  fi
fi
rm -f /tmp/gt-substrate.tar.gz 2>/dev/null || true

# Materialize the task repo at its base commit and copy it OUT to a host dir we
# then mount READ-ONLY into the substrate (the substrate copies to its own writable
# scratch internally; the task image is never mutated).
docker rm -f gtsrc 2>/dev/null || true
docker run -d --entrypoint sh --name gtsrc "$IMG" -lc 'sleep 1800' >/tmp/gtsrc_create.log 2>&1 \
  || fail_proof TASK_IMAGE_EXTRACT_FAIL "task image container failed to start: $(tail -20 /tmp/gtsrc_create.log | tr '\n' ' ')"

if [ "$HARNESS" = "deepswe" ]; then
  ROOT=$(docker exec gtsrc sh -lc 'for d in /home/user /testbed /workspace /app /repo; do [ -e "$d/.git" ] && echo "$d" && break; done' 2>/tmp/gt_root_probe.err || true)
  if [ -z "$ROOT" ]; then
    fail_proof TASK_IMAGE_EXTRACT_FAIL "no git repo root found in task image (tried /home/user /testbed /workspace /app /repo); $(cat /tmp/gt_root_probe.err 2>/dev/null | tail -5 | tr '\n' ' ')"
  fi
else
  # Pro images use /app as the repo root (confirmed: eval_pro_simple.py `cd /app`).
  # Fall back to scanning common paths if /app/.git absent.
  ROOT=$(docker exec gtsrc sh -lc '
    for d in /app /testbed /workspace /repo /home/user; do
      [ -e "$d/.git" ] && echo "$d" && break
    done' 2>/tmp/gt_root_probe.err || true)
  if [ -z "$ROOT" ]; then
    fail_proof TASK_IMAGE_EXTRACT_FAIL "no git repo root found in Pro task image (tried /app /testbed /workspace /repo /home/user); $(cat /tmp/gt_root_probe.err 2>/dev/null | tail -5 | tr '\n' ' ')"
  fi
  echo "Pro repo root: $ROOT"
fi
echo "repo root: $ROOT"
rm -rf /tmp/gt && mkdir -p /tmp/gt /tmp/gt/src
docker cp "gtsrc:$ROOT/." /tmp/gt/src || fail_proof TASK_IMAGE_EXTRACT_FAIL "source extract failed from $ROOT"
# C1 (parity w/ OH host-src gate): `docker cp` returns rc=0 even when $ROOT was
# misdetected and the copied tree holds NO source — gt-run-proof then finds nothing
# and SILENTLY degrades GT (correct-or-quiet), which corrupts run accounting (looks
# like "GT ran" when it had zero source). Validate CONTENT, not just rc: a no-source
# extract is the same severity as a failed extract -> LOUD, classified hard-fail.
if ! find /tmp/gt/src -type f \( \
      -name '*.py' -o -name '*.go' -o -name '*.rs' -o -name '*.ts' -o -name '*.tsx' \
   -o -name '*.js' -o -name '*.jsx' -o -name '*.java' -o -name '*.c' -o -name '*.cc' \
   -o -name '*.cpp' -o -name '*.h' -o -name '*.hpp' -o -name '*.cs' \
   \) -print -quit 2>/dev/null | grep -q .; then
  fail_proof TASK_IMAGE_EXTRACT_EMPTY \
    "extracted /tmp/gt/src has NO LSP-supported source file (ROOT='$ROOT' misdetected or empty image tree) — refusing a silent zero-source degrade"
fi

# ── Deepen git history so cochange mining has commits to walk (COCHANGE DATA fix) ──
# The cochanges table populates ONLY from `git log --name-only -n 500` (gt-index
# mineCochanges). Task images are built from a SHALLOW checkout (`git clone --depth 1`),
# so the extracted /tmp/gt/src/.git carries ONE commit -> `git log` returns ≤1 commit ->
# 0 co-change pairs on EVERY substrate graph, even though the producer is correct
# (proven: a multi-commit repo stores pairs; a 1-commit/shallow repo stores 0).
#
# Fix: deepen the WRITABLE host copy (/tmp/gt/src) HERE, BEFORE it is mounted /work:ro
# into the substrate where gt-index runs — the read-only mount makes an in-container
# unshallow impossible, so it must happen on the host.
#
# Correct-or-quiet + fail-closed by construction (never aborts the proof):
#   - acts ONLY when the repo is actually shallow (.git/shallow present);
#   - `--unshallow` DEEPENS history reachable from existing refs; it does NOT move HEAD,
#     so the base commit + tree the agent edits are byte-identical afterward (verified:
#     rev-list tip is unchanged, only ancestors are added);
#   - if there is no `origin`, the remote is unreachable (offline/auth), or the fetch
#     fails for any reason, the repo is LEFT AS-IS and cochange simply stays empty —
#     exactly today's behavior. A missing/unavailable remote is never a hard failure.
if [ -d /tmp/gt/src/.git ] && [ -f /tmp/gt/src/.git/shallow ]; then
  _COMMITS_BEFORE=$(git -C /tmp/gt/src rev-list --count HEAD 2>/dev/null || echo 0)
  if git -C /tmp/gt/src remote get-url origin >/dev/null 2>&1; then
    # --depth=500 (not --unshallow): the cochange miner reads only the last 500
    # commits (change.py: `git log -n 500`), so cap the host fetch at 500 commits
    # to avoid pulling unbounded monorepo history. Deepens depth-1 -> 500,
    # HEAD/tree-preserving; a timeout/failure degrades gracefully to shallow below.
    if git -C /tmp/gt/src fetch --quiet --depth=500 origin 2>/tmp/gt_unshallow.err; then
      _COMMITS_AFTER=$(git -C /tmp/gt/src rev-list --count HEAD 2>/dev/null || echo 0)
      echo "cochange: deepened git history ${_COMMITS_BEFORE} -> ${_COMMITS_AFTER} commits (depth=500 OK)"
    else
      echo "::warning::cochange: git fetch --depth=500 failed ($(tail -1 /tmp/gt_unshallow.err 2>/dev/null | tr -d '\n')) — proceeding with shallow history (cochange will be empty, correct-or-quiet)"
    fi
  else
    echo "::warning::cochange: repo is shallow but has no 'origin' remote — cannot deepen history (cochange will be empty, correct-or-quiet)"
  fi
elif [ -d /tmp/gt/src/.git ]; then
  echo "cochange: git history already full (not shallow), $(git -C /tmp/gt/src rev-list --count HEAD 2>/dev/null || echo '?') commits"
else
  echo "cochange: no .git in extracted source — cochange mining will be empty (correct-or-quiet)"
fi

# ── Extract dep stores so the substrate's LSP servers can resolve ──────
# gopls needs the Go module cache; rust-analyzer needs cargo + rustup.
# Discover paths inside gtsrc (mars-base images use non-default GOMODCACHE).
mkdir -p /tmp/gt/deps/gomodcache /tmp/gt/deps/cargo /tmp/gt/deps/rustup
# Copy a dep-cache tree from gtsrc to the host. Caches are IMMUTABLE (Go/cargo stamp
# every dir 0555), and `docker cp` preserves those modes then cannot write children
# into them -> it aborts after the first dir (measured: 1 of 146484 files). tar writes
# a dir's children BEFORE stamping its mode, so it copies the whole tree;
# --no-same-permissions keeps the host copy readable for the language server. ONE
# mechanism for every language's cache (go/cargo/rustup/rust-src). rc 0 only if files landed.
copy_store(){  # $1=src-in-container  $2=host-dest
  docker exec gtsrc tar -C "$1" -cf - . 2>/dev/null | tar -C "$2" -xf - --no-same-permissions 2>/dev/null
  chmod -R u+w "$2" 2>/dev/null   # caches arrive 0555; make host copy writable so rm-cleanup works later
  [ -n "$(ls -A "$2" 2>/dev/null)" ]
}
GOMOD_SRC=""
GOMOD_DECLARED="$(docker exec gtsrc sh -lc 'go env GOMODCACHE 2>/dev/null' | tr -d '\r')"
for CAND in \
  "$GOMOD_DECLARED" \
  "/root/go/pkg/mod" "/home/user/go/pkg/mod" "/go/pkg/mod"; do
  [ -n "$CAND" ] || continue
  if docker exec gtsrc test -d "$CAND" 2>/dev/null; then
    copy_store "$CAND" /tmp/gt/deps/gomodcache && GOMOD_SRC="$CAND" && break
  fi
done
[ -n "$GOMOD_SRC" ] || echo "no Go module cache copied from task image"

if [ "$HARNESS" = "deepswe" ]; then
  CARGO_SRC=""
  for CAND in \
    "$(docker exec gtsrc sh -lc 'echo ${CARGO_HOME:-$HOME/.cargo}' | tr -d '\r')" \
    "/root/.cargo" "/home/user/.cargo"; do
    [ -n "$CAND" ] || continue
    if docker exec gtsrc test -d "$CAND" 2>/dev/null; then
      copy_store "$CAND" /tmp/gt/deps/cargo && CARGO_SRC="$CAND" && break
    fi
  done
  [ -n "$CARGO_SRC" ] || echo "no Cargo home copied from task image"
  # Missing rust-src must fail closed via the manifest/proof path; do not
  # mutate the task image here to provision toolchain components.
  ACTIVE_RUST_TOOLCHAIN="$(docker exec gtsrc sh -lc 'rustup show active-toolchain 2>/dev/null | awk "{print \$1}" | tr -d "\r"' || true)"
  RUSTUP_SRC=""
  for CAND in \
    "$(docker exec gtsrc sh -lc 'echo ${RUSTUP_HOME:-$HOME/.rustup}' | tr -d '\r')" \
    "/root/.rustup" "/home/user/.rustup"; do
    [ -n "$CAND" ] || continue
    if docker exec gtsrc test -d "$CAND" 2>/dev/null; then
      copy_store "$CAND" /tmp/gt/deps/rustup && RUSTUP_SRC="$CAND" && break
    fi
  done
  [ -n "$RUSTUP_SRC" ] || echo "no rustup copied from task image"
  # Backfill rust-src from task image sysroot if not in the copied rustup tree
  ACTIVE_RUST_TOOLCHAIN="$(docker exec gtsrc sh -lc 'rustup show active-toolchain 2>/dev/null | awk "{print \$1}" | tr -d "\r"' || true)"
  RUST_SYSROOT="$(docker exec gtsrc sh -lc 'rustc --print sysroot 2>/dev/null' | tr -d '\r' || true)"
  if [ -n "$ACTIVE_RUST_TOOLCHAIN" ] && [ -n "$RUST_SYSROOT" ] \
     && [ ! -d "/tmp/gt/deps/rustup/toolchains/$ACTIVE_RUST_TOOLCHAIN/lib/rustlib/src/rust/library" ]; then
    TASK_RUST_SRC="${RUST_SYSROOT%/}/lib/rustlib/src/rust/library"
    if docker exec gtsrc test -d "$TASK_RUST_SRC" 2>/dev/null; then
      mkdir -p "/tmp/gt/deps/rustup/toolchains/$ACTIVE_RUST_TOOLCHAIN/lib/rustlib/src/rust/library"
      copy_store "$TASK_RUST_SRC" "/tmp/gt/deps/rustup/toolchains/$ACTIVE_RUST_TOOLCHAIN/lib/rustlib/src/rust/library" \
        && echo "backfilled rust-src from sysroot: $TASK_RUST_SRC" \
        || echo "rust-src backfill failed (non-fatal)"
    fi
  fi
  # Never transplant rust-src from the substrate's arbitrary toolchain into
  # the task toolchain path. Missing task-owned rust-src remains missing and
  # the dependency manifest rejects it instead of recording false provenance.
  python3 scripts/swebench/dep_store_manifest.py \
    --out /tmp/gt/dep_store_manifest.json \
    --language "${GT_MATRIX_LANGUAGE}" \
    --gomodcache-host /tmp/gt/deps/gomodcache --gomodcache-source "$GOMOD_SRC" --gomodcache-declared-source "$GOMOD_DECLARED" \
    --cargo-host /tmp/gt/deps/cargo --cargo-source "$CARGO_SRC" \
    --rustup-host /tmp/gt/deps/rustup --rustup-source "$RUSTUP_SRC" \
    --rust-toolchain "$ACTIVE_RUST_TOOLCHAIN" \
    || { echo "DEP_STORE_MANIFEST_WARN: dep store incomplete for ${GT_MATRIX_LANGUAGE} (Go/Rust LSP will degrade)" | tee -a trial_output.log; }
else
  # Pro harness: only Go dep store is extracted (no Rust tasks in SWE-bench Pro).
  CARGO_SRC=""
  RUSTUP_SRC=""
  ACTIVE_RUST_TOOLCHAIN=""
  python3 scripts/swebench/dep_store_manifest.py \
    --out /tmp/gt/dep_store_manifest.json \
    --language "${GT_MATRIX_LANGUAGE}" \
    --gomodcache-host /tmp/gt/deps/gomodcache --gomodcache-source "$GOMOD_SRC" --gomodcache-declared-source "$GOMOD_DECLARED" \
    --cargo-host /tmp/gt/deps/cargo --cargo-source "" \
    --rustup-host /tmp/gt/deps/rustup --rustup-source "" \
    --rust-toolchain "" \
    || { echo "DEP_STORE_MANIFEST_WARN: dep store incomplete for ${GT_MATRIX_LANGUAGE}" | tee -a trial_output.log; }
fi

# Run provenance: the task base commit = the materialized repo's HEAD (docker cp
# carries .git). Recorded-or-empty — gt-run-proof writes null when absent, never
# a guess. Provenance only; no task IDs in product logic, no gate behavior change.
TASK_REPO_COMMIT=$(git -C /tmp/gt/src rev-parse HEAD 2>/dev/null || true)
echo "task repo commit: ${TASK_REPO_COMMIT:-<unknown>}"

# ── Issue text extraction ──────────────────────────────────────────────────────
if [ "$HARNESS" = "deepswe" ]; then
  # THIS task's issue (problem statement only — NEVER gold/tests) so the substrate's
  # demand-driven LSP scope + embedder cert operate on the issue-relevant subgraph.
  # P0.1-a: the real DeepSWE task instruction lives in the sibling instruction.md
  # (pier reads it as Task.instruction) — task.toml has NO metadata.issue/task.prompt,
  # so the old extraction yielded an EMPTY issue on every task (and `|| :` swallowed
  # it), silently degrading the substrate to a no-issue run (the green-zero-run
  # chain). Read instruction.md first, task.toml fields as fallback, and FAIL-CLOSED
  # (GT_ISSUE_MISSING, classified INFRA) if the issue text ends up empty — never run
  # the substrate with an empty issue.
  python3 - "$TASK_DIR" <<'PYEOF' || { echo "::warning::GT_ISSUE_MISSING: no issue text — proceeding with empty issue (agent has its own instruction)" | tee -a trial_output.log; }
import os, sys, json, hashlib
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
task_dir = sys.argv[1]
out_path = os.environ.get("GT_ISSUE_OUT", "/tmp/issue.txt")
issue, source = "", ""
# 1) DeepSWE-native: the sibling instruction.md (pier reads it as Task.instruction).
inst = os.path.join(task_dir, "instruction.md")
if os.path.exists(inst):
    with open(inst, encoding="utf-8", errors="replace") as f:
        issue = f.read().strip()
    if issue:
        source = "instruction.md"
# 2) DeepSWE task.toml fields.
if not issue:
    try:
        with open(os.path.join(task_dir, "task.toml"), "rb") as f:
            t = tomllib.load(f)
        issue = ((t.get("metadata", {}) or {}).get("issue", "")
                 or (t.get("task", {}) or {}).get("prompt", "")
                 or (t.get("task", {}) or {}).get("instruction", "") or "").strip()
        if issue:
            source = "task.toml"
    except Exception:
        pass
# 3) ROOT-CAUSE FIX (2026-07-08): Live-Lite / dataset problem_statement, keyed by
#    instance_id (== GT_MATRIX_TASK). This is the SAME source the agent step reads,
#    so the substrate builds its pretask artifacts (anchors/localizer/brief) on the
#    IDENTICAL real issue the agent sees — not a placeholder. The deepswe branch
#    never fetched it, so 295/299 Live-Lite tasks ran GT BLIND on the synthesized
#    fallback (empty anchors -> hub-fallback -> identical ranking on every task).
if not issue:
    task_id = (os.environ.get("GT_MATRIX_TASK") or "").strip()
    ds_path = os.environ.get("GT_ISSUE_DATASET", "benchmarks/data/swebench_live_lite.jsonl")
    if task_id and os.path.exists(ds_path):
        try:
            with open(ds_path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    if r.get("instance_id") == task_id:
                        issue = (r.get("problem_statement") or "").strip()
                        if issue:
                            source = "live_lite_dataset"
                        break
        except Exception as e:
            print(f"GT_ISSUE_DATASET read error ({ds_path}): {e}", file=sys.stderr)
# 4) genuinely-missing issue -> synthesized fallback (made FAIL-CLOSED downstream).
if not issue:
    issue = "Fix the issue described in the repository."
    source = "synthesized_fallback"
    print("GT_ISSUE_MISSING: no instruction.md / task.toml / dataset problem_statement "
          f"for task={os.environ.get('GT_MATRIX_TASK','?')} — synthesized fallback", file=sys.stderr)
with open(out_path, "w", encoding="utf-8") as f:
    f.write(issue)
digest = hashlib.sha256(issue.encode("utf-8")).hexdigest()
for path, val in (("/tmp/gt/issue_source.txt", source), ("/tmp/gt/issue_sha256.txt", digest)):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as sf:
            sf.write(val)
    except Exception:
        pass
print(f"issue text: {len(issue)} chars from {source} (sha256={digest[:12]}) -> {out_path}")
PYEOF
  # FAIL-CLOSED (2026-07-08): if the issue collapsed to the synthesized placeholder, REFUSE to build
  # GT pretask artifacts on it — this is the blind-run bug (empty anchors -> hub fallback -> identical
  # ranking on every task). Surface the marker (deepswe_outcome.py classifies it INFRA), drop a
  # sentinel, and ABORT the task at the substrate stage, BEFORE the paid agent runs. The old behavior
  # (warn + run blind) is restorable with GT_ISSUE_FAIL_CLOSED=0.
  ISSUE_SOURCE="$(cat /tmp/gt/issue_source.txt 2>/dev/null || echo unknown)"
  if [ "$ISSUE_SOURCE" = "synthesized_fallback" ]; then
    echo "GT_ISSUE_MISSING: no instruction.md/task.toml/dataset problem_statement for ${GT_MATRIX_TASK:-?} — synthesized fallback; INFRA-diagnostic, excluded from resolved denominator" | tee -a trial_output.log
    if [ "${GT_ISSUE_FAIL_CLOSED:-1}" != "0" ]; then
      mkdir -p /tmp/gt 2>/dev/null || true
      echo "$ISSUE_SOURCE" > /tmp/gt/ISSUE_FAIL_CLOSED
      echo "GT_ISSUE_MISSING_FATAL: refusing to run GT on a placeholder issue (set GT_ISSUE_FAIL_CLOSED=0 to override). Task aborted at substrate stage — no paid agent, excluded from resolved denominator." | tee -a trial_output.log
      exit 3
    fi
    echo "::warning::GT_ISSUE_FAIL_CLOSED=0 — proceeding on synthesized fallback (BLIND GT run)" | tee -a trial_output.log
  fi
  python3 scripts/swebench/issue_manifest.py /tmp/issue.txt /tmp/gt/issue_manifest.json --source instruction || echo "::warning::issue_manifest.py failed — non-fatal, continuing"
  # P0 (2026-06-23): write the verbatim issue into the substrate (/tmp/gt -> mounted
  # read-only at /gt_artifacts) so the runtime re-surface (gt_mini_patch) can read
  # $GT_CERT_DIR/issue.txt as the requirement fallback when the structured
  # obligations[] array is empty (the common case — the extractor is remove-param-only).
  cp /tmp/issue.txt /tmp/gt/issue.txt 2>/dev/null \
    && echo "issue.txt -> substrate ($(wc -c </tmp/gt/issue.txt) chars)" \
    || echo "::warning::issue.txt copy failed — re-surface fallback unavailable"
else
  # Pro harness: issue text from Pro-OS run_scripts or fallback to in-image paths.
  TASK_ID="${GT_MATRIX_TASK}"
  PRO_RUN_SCRIPTS="swebench-pro-os/run_scripts"
  ISSUE_TEXT=""

  # PRIMARY source: problem_statement from the CANONICAL HF dataset (ScaleAI/SWE-bench_Pro).
  # LEAKAGE GUARD (non-negotiable): fetch ONLY problem_statement — NEVER read
  # run_scripts/<instance>/instance_info.txt (it carries FAIL_TO_PASS/PASS_TO_PASS).
  # The Pro-OS repo has no instructions/ or problem_statement.md — the issue lives only on HF.
  HF_PS="$(curl -s --max-time 60 -G "https://datasets-server.huggingface.co/filter" \
    --data-urlencode "dataset=ScaleAI/SWE-bench_Pro" \
    --data-urlencode "config=default" \
    --data-urlencode "split=test" \
    --data-urlencode "where=\"instance_id\"='${TASK_ID}'" \
    --data-urlencode "length=1" 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); r=(d.get('rows') or [{}])[0].get('row',{}); sys.stdout.write((r.get('problem_statement') or '').strip())" 2>/dev/null || true)"
  if [ -n "$HF_PS" ]; then
    ISSUE_TEXT="$HF_PS"
    echo "issue text from HF ScaleAI/SWE-bench_Pro (problem_statement only): ${#ISSUE_TEXT} chars"
  fi

  # Fallback A: Pro-OS run_scripts dir (only if HF failed)
  for candidate in \
      "${PRO_RUN_SCRIPTS}/${TASK_ID}/problem_statement.md" \
      "${PRO_RUN_SCRIPTS}/${TASK_ID}/README.md" \
      "${PRO_RUN_SCRIPTS}/${TASK_ID}/instruction.md"; do
    if [ -z "$ISSUE_TEXT" ] && [ -s "$candidate" ]; then
      ISSUE_TEXT="$(cat "$candidate")"
      echo "issue text from ${candidate}: ${#ISSUE_TEXT} chars"
      break
    fi
  done

  # Fallback: extract from inside the image
  if [ -z "$ISSUE_TEXT" ]; then
    for cand_path in /opt/problem_statement.txt /opt/issue.txt /tmp/problem_statement.txt; do
      TEXT="$(docker exec gtsrc cat "$cand_path" 2>/dev/null | tr -d '\r' || true)"
      if [ -n "$TEXT" ]; then
        ISSUE_TEXT="$TEXT"
        echo "issue text from in-image ${cand_path}: ${#ISSUE_TEXT} chars"
        break
      fi
    done
  fi

  # Final fallback: synthesize a minimal issue from the task ID
  if [ -z "$ISSUE_TEXT" ]; then
    ISSUE_TEXT="Fix issue in ${TASK_ID}. See the repository for context."
    echo "::warning::GT_ISSUE_FALLBACK: no problem_statement found for ${TASK_ID} — using synthesized issue text"
  fi

  # Detect synthesized fallback and classify before the substrate run (INFRA diagnostic,
  # not an abort — the substrate handles empty-issue fail-closed internally).
  if echo "$ISSUE_TEXT" | grep -qE "^Fix issue in "; then
    echo "GT_ISSUE_MISSING: no real issue text — synthesized fallback detected (Pro-OS clone may have failed or run_scripts/${TASK_ID}/ absent)" | tee -a trial_output.log
    echo "::warning::Running with synthesized issue text for ${TASK_ID} (no real Pro-OS problem_statement found)"
  fi

  echo "$ISSUE_TEXT" > /tmp/issue.txt
  [ -s /tmp/issue.txt ] || { echo "::warning::GT_ISSUE_MISSING: issue text empty after all fallbacks — proceeding with synthesized fallback" | tee -a trial_output.log; echo "Fix the issue described in the repository." > /tmp/issue.txt; }
  python3 scripts/swebench/issue_manifest.py /tmp/issue.txt /tmp/gt/issue_manifest.json --source pro || echo "::warning::issue_manifest.py (pro) failed — non-fatal, continuing"
fi

# ── Per-language LSP readiness budget (efficiency, src-free knob) ────────────
# resolve.py runs a ONE-TIME project-readiness barrier per pass bounded by
# GT_LSP_READY_BUDGET_S (default 20s, read via os.environ — resolve.py:523). The
# barrier EARLY-EXITS on the first non-error/non-empty definition (resolve.py:507),
# so a WARM server (pyright/tsserver) pays ~one drain + one request, NOT the full
# budget. The full budget is only ever consumed by a server that NEVER converges —
# the measured go/rust shape (project_ready=false, 0 LSP conversion: the audit's
# gopls/rust-analyzer no-op). On those languages the 20s wait is pure dead time
# (tree-sitter structural depth is index-time, unaffected by this wait), so we
# SHORTEN it; on ts/js (tsserver converts) we KEEP the full budget where it pays.
# Pure process-env (no src change, no per-server branching) — overridable via the
# repo var GT_LSP_READY_BUDGET_S (global override) if a future task profile shifts.
# Workflow passes only a global override; gt-run-proof owns per-language
# readiness budgeting inside the substrate boundary.
python3 -c "
import json, os, time
p = {
    'schema': 'gt.run_provenance.v1',
    'task': os.environ.get('GT_MATRIX_TASK', ''),
    'language': os.environ.get('GT_MATRIX_LANGUAGE', ''),
    'substrate_digest': os.environ.get('GT_SUBSTRATE_DIGEST', ''),
    'git_sha': os.environ.get('GT_GITHUB_SHA', '') or os.environ.get('GITHUB_SHA', ''),
    'ts': time.time(),
}
open('/tmp/gt/run_provenance.json', 'w').write(json.dumps(p, indent=2) + '\n')
" 2>/dev/null || true

# ── gt-run-proof INSIDE the pinned substrate (the ONE portable command, §A) ──
# Repo mounted /work:ro; artifacts -> /gt_artifacts. All 8 proof flags set so the
# substrate fails-closed on any boundary/baked-dep violation. The host only orchestrates.
# --memory: a runaway in-container encode must OOM the CONTAINER (rc=137,
# classified GT_PROOF_OOM) — uncapped, the GHA HOST OOM-killer killed the
# process BEFORE its fail-closed stderr print, surfacing as a silent generic
# failure (the 2026-06-10 5-task brief-OOM). rc is THE diagnostic bit.
# Preserve the whole substrate closure on PATH. Omitting /opt/gt/node/bin hides
# pyright-langserver/typescript-language-server even when the pinned image baked them.
PROOF_RC=0
if [ "$HARNESS" = "deepswe" ]; then
  docker run --rm \
      --memory=14g --memory-swap=34g \
      -v "/tmp/gt/src:/work:ro" -v "/tmp/gt:/gt_artifacts" \
      -v "/tmp/issue.txt:/work_issue.txt:ro" \
      -v "/tmp/gt/deps/gomodcache:/tmp/gomodcache" \
      -v "/tmp/gt/deps/cargo:/root/.cargo" \
      -v "/tmp/gt/deps/rustup:/root/.rustup" \
      -e GT_PROOF_MODE=1 -e GT_CONTAINERIZED=1 -e GT_RUNTIME_STRATEGY=unified_substrate \
      -e OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}" -e MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}" \
      -e OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}" -e NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}" \
      -e GT_HOST_GRAPH_DB=/tmp/gt/graph.db \
      -e GT_REQUIRE_FTS5=1 -e GT_REQUIRE_EMBEDDER=1 -e GT_FORCE_ONNX_EMBEDDER=1 \
      -e GT_REQUIRE_LSP=1 -e GT_REQUIRE_FULL_STACK=1 -e GT_ISSUE_FILE=/work_issue.txt \
      -e GT_RRF_FUSION="${GT_RRF_FUSION:-on}" \
      -e GT_LOC_FUSION_V2="${GT_LOC_FUSION_V2:-1}" -e GT_LOC_BEHAVIOR_LEAD="${GT_LOC_BEHAVIOR_LEAD:-0}" \
      -e GT_PROACTIVE_TOPN="${GT_PROACTIVE_TOPN:-1}" \
      -e GT_NEG_EVIDENCE="${GT_NEG_EVIDENCE:-0}" \
      -e GT_TYPEFLOW_FIXPOINT="${GT_TYPEFLOW_FIXPOINT:-0}" \
      -e GT_FIELD_CANDIDATES="${GT_FIELD_CANDIDATES:-0}" \
      -e GT_SEM_BODY="${GT_SEM_BODY:-1}" \
      -e GT_BRIEF_MINIMAL="${GT_BRIEF_MINIMAL:-1}" \
      -e GT_BRIEF_NATIVE="${GT_BRIEF_NATIVE:-1}" \
      -e GT_SS_ACK_FORM="${GT_SS_ACK_FORM:-0}" \
      -e GT_LOC_RESLOT="${GT_LOC_RESLOT:-0}" \
      -e GT_INSEAM_METRICS="${GT_INSEAM_METRICS:-0}" \
      -e GT_PASSAGE_WIDE="${GT_PASSAGE_WIDE:-0}" \
      -e GT_CONTENT_LEG="${GT_CONTENT_LEG:-1}" \
      -e GT_CONSENSUS_LEDGER="${GT_CONSENSUS_LEDGER:-0}" \
      -e GT_OBLIGATIONS_V2="${GT_OBLIGATIONS_V2:-1}" \
      -e GT_LSP_READY_BUDGET_S_OVERRIDE="${GT_LSP_READY_BUDGET_S_OVERRIDE:-}" \
      -e GOMODCACHE=/tmp/gomodcache -e GOFLAGS=-mod=mod -e GOPROXY=off -e GONOSUMCHECK=1 -e GOSUMDB=off \
      -e CARGO_HOME=/root/.cargo \
      -e CARGO_TARGET_DIR=/tmp/cargo-target -e CARGO_NET_OFFLINE=true \
      -e RUSTUP_HOME=/root/.rustup -e RUSTUP_TOOLCHAIN="$ACTIVE_RUST_TOOLCHAIN" \
      -e PATH="/root/.rustup/toolchains/$ACTIVE_RUST_TOOLCHAIN/bin:/opt/gt/bin:/opt/gt/node/bin:/opt/gt/python/bin:/opt/gt/jre/bin:/opt/gt/go/bin:/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
      -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -e HF_DATASETS_OFFLINE=1 \
      -e GT_GATES_DELIVER_ALWAYS="${GT_GATES_DELIVER_ALWAYS:-0}" \
      -e GT_GIT_COMMIT="${GT_GITHUB_SHA}" \
      -e GT_REQUIRE_COMMIT_PARITY="${GT_REQUIRE_COMMIT_PARITY:-0}" \
      -e GT_REQUIRE_GRAPH_VALID="${GT_REQUIRE_GRAPH_VALID:-0}" \
      -e GT_SUBSTRATE_DIGEST="$GT_SUBSTRATE_DIGEST" \
      -e GT_TASK_REPO_COMMIT="$TASK_REPO_COMMIT" \
      "$GT_SUBSTRATE_DIGEST" gt-run-proof --source-root /work --out /gt_artifacts 2>&1 | tee /tmp/gt/proof_output.log || PROOF_RC=$?
  PROOF_RC=${PIPESTATUS[0]:-$PROOF_RC}
else
  docker run --rm \
      --memory=14g --memory-swap=34g \
      -v "/tmp/gt/src:/work:ro" -v "/tmp/gt:/gt_artifacts" \
      -v "/tmp/issue.txt:/work_issue.txt:ro" \
      -v "/tmp/gt/deps/gomodcache:/tmp/gomodcache" \
      -v "/tmp/gt/deps/cargo:/root/.cargo" \
      -v "/tmp/gt/deps/rustup:/root/.rustup" \
      -e GT_PROOF_MODE=1 -e GT_CONTAINERIZED=1 -e GT_RUNTIME_STRATEGY=unified_substrate \
      -e OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}" -e MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}" \
      -e OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}" -e NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}" \
      -e GT_HOST_GRAPH_DB=/tmp/gt/graph.db \
      -e GT_REQUIRE_FTS5=1 -e GT_REQUIRE_EMBEDDER=1 -e GT_FORCE_ONNX_EMBEDDER=1 \
      -e GT_REQUIRE_LSP=1 -e GT_REQUIRE_FULL_STACK=1 -e GT_ISSUE_FILE=/work_issue.txt \
      -e GT_RRF_FUSION="${GT_RRF_FUSION:-on}" \
      -e GT_LOC_FUSION_V2="${GT_LOC_FUSION_V2:-1}" -e GT_LOC_BEHAVIOR_LEAD="${GT_LOC_BEHAVIOR_LEAD:-0}" \
      -e GT_PROACTIVE_TOPN="${GT_PROACTIVE_TOPN:-1}" \
      -e GT_NEG_EVIDENCE="${GT_NEG_EVIDENCE:-0}" \
      -e GT_TYPEFLOW_FIXPOINT="${GT_TYPEFLOW_FIXPOINT:-0}" \
      -e GT_FIELD_CANDIDATES="${GT_FIELD_CANDIDATES:-0}" \
      -e GT_SEM_BODY="${GT_SEM_BODY:-1}" \
      -e GT_BRIEF_MINIMAL="${GT_BRIEF_MINIMAL:-1}" \
      -e GT_BRIEF_NATIVE="${GT_BRIEF_NATIVE:-1}" \
      -e GT_SS_ACK_FORM="${GT_SS_ACK_FORM:-0}" \
      -e GT_LOC_RESLOT="${GT_LOC_RESLOT:-0}" \
      -e GT_INSEAM_METRICS="${GT_INSEAM_METRICS:-0}" \
      -e GT_PASSAGE_WIDE="${GT_PASSAGE_WIDE:-0}" \
      -e GT_CONTENT_LEG="${GT_CONTENT_LEG:-1}" \
      -e GT_CONSENSUS_LEDGER="${GT_CONSENSUS_LEDGER:-0}" \
      -e GT_OBLIGATIONS_V2="${GT_OBLIGATIONS_V2:-1}" \
      -e GT_LSP_READY_BUDGET_S_OVERRIDE="${GT_LSP_READY_BUDGET_S_OVERRIDE:-}" \
      -e GOMODCACHE=/tmp/gomodcache -e GOFLAGS=-mod=mod -e GOPROXY=off -e GONOSUMCHECK=1 -e GOSUMDB=off \
      -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -e HF_DATASETS_OFFLINE=1 \
      -e GT_GATES_DELIVER_ALWAYS="${GT_GATES_DELIVER_ALWAYS:-0}" \
      -e GT_GIT_COMMIT="${GT_GITHUB_SHA}" \
      -e GT_REQUIRE_COMMIT_PARITY="${GT_REQUIRE_COMMIT_PARITY:-0}" \
      -e GT_REQUIRE_GRAPH_VALID="${GT_REQUIRE_GRAPH_VALID:-0}" \
      -e GT_SUBSTRATE_DIGEST="$GT_SUBSTRATE_DIGEST" \
      -e GT_TASK_REPO_COMMIT="$TASK_REPO_COMMIT" \
      -e PATH="/opt/gt/bin:/opt/gt/node/bin:/opt/gt/python/bin:/opt/gt/jre/bin:/opt/gt/go/bin:/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
      "$GT_SUBSTRATE_DIGEST" gt-run-proof --source-root /work --out /gt_artifacts 2>&1 | tee /tmp/gt/proof_output.log || PROOF_RC=$?
  PROOF_RC=${PIPESTATUS[0]:-$PROOF_RC}
fi

if [ "$PROOF_RC" -ne 0 ]; then
  if [ "$PROOF_RC" -eq 137 ]; then
    echo "GT_PROOF_OOM: gt-run-proof rc=137 — killed by the container memory cap (capacity kill, not logic; the fail-closed print never ran)" | tee -a trial_output.log
    write_proof_status failed GT_PROOF_OOM "gt-run-proof rc=137"
  else
    echo "GT_RUN_PROOF_FAIL: gt-run-proof rc=$PROOF_RC (boundary/leak/missing-baked-dep)" | tee -a trial_output.log
    write_proof_status failed GT_RUN_PROOF_FAIL "gt-run-proof rc=$PROOF_RC"
    if [ -f /tmp/gt/proof_failure.json ]; then
      echo "proof_failure.json:" | tee -a trial_output.log
      cat /tmp/gt/proof_failure.json | tee -a trial_output.log
    fi
    if [ -f /tmp/gt/proof_progress.json ]; then
      echo "proof_progress.json:" | tee -a trial_output.log
      cat /tmp/gt/proof_progress.json | tee -a trial_output.log
    fi
    if [ -f /tmp/gt/dep_store_manifest.json ]; then
      echo "dep_store_manifest.json:" | tee -a trial_output.log
      cat /tmp/gt/dep_store_manifest.json | tee -a trial_output.log
    fi
  fi
  docker rm -f gtsrc 2>/dev/null || true
  # FAIL-CLOSED under the master gate. On a paid full run (GT_REQUIRE_FULL_STACK=1 or
  # GT_REQUIRE_GRAPH_VALID=1) a proof failure means the substrate is NOT real — do NOT
  # launder it to ok. Leaving proof_status=failed makes the agent gate (PROOF_STATE != ok,
  # deepswe_full.yml) refuse the paid trial, so we never spend money on a false/thin map.
  # This closes the line-619 laundering that previously overrode EMBEDDER_USAGE_FAIL /
  # GT_INDEX_FAIL / GRAPH_CERT_INVALID / missing-brief to ok. The degraded-proceed path is
  # kept ONLY for non-fail-closed iteration runs.
  # PRODUCT RULE: never refuse a task for GT-QUALITY (thin graph, degraded depth/LSP/embedder/index)
  # -> agent runs BEST-EFFORT (correct-or-quiet, cert recorded for honest attribution). But HARD-ABORT
  # (flag-independent) on a LEGITIMACY / ANTI-CHEAT / NO-BUILD breach — these make the results INVALID,
  # not merely thin, so spending on them would be worse than refusing:
  #   COMMIT_PARITY               proof ran STALE code (wrong-commit attribution)
  #   EVAL_LEAKAGE_FORBIDDEN      test names/FAIL_TO_PASS leaked into the brief (anti-cheat)
  #   SUBSTRATE_NOT_PORTABLE      substrate can't run in the eval env (handoff invalid)
  #   FINAL_PIPELINE_HOST_SPLIT_FAIL  host/container split broken (invalid handoff)
  #   GT_DEAD_SURFACE_LOADED      the wrong/dead GT code path was loaded
  #   GT_PROOF_OOM (rc=137)       substrate did not build at all
  # Everything else (thin graph, degraded depth/LSP/embedder) proceeds best-effort.
  if [ "$PROOF_RC" -eq 137 ]; then
    echo "::error::gt-run-proof OOM (rc=137) — substrate did NOT build; task FAILS (no laundering)" | tee -a trial_output.log
    exit 1
  fi
  _LEGIT="COMMIT_PARITY|EVAL_LEAKAGE_FORBIDDEN|SUBSTRATE_NOT_PORTABLE|FINAL_PIPELINE_HOST_SPLIT_FAIL|GT_DEAD_SURFACE_LOADED"
  if grep -qE "$_LEGIT" /tmp/gt/proof_failure.json 2>/dev/null; then
    _LC=$(grep -oE "$_LEGIT" /tmp/gt/proof_failure.json 2>/dev/null | head -1)
    echo "::error::gt-run-proof failed on ${_LC} (legitimacy/anti-cheat — results would be INVALID) — task FAILS" | tee -a trial_output.log
    # proof_status stays 'failed' (written above); HARD ABORT so no paid spend on an invalid substrate.
    exit 1
  # TASK != METRICS — FULL DECOUPLE (2026-07-20, user directive). The substrate proof is a
  # MEASUREMENT, not a task gate. It has already RUN and RECORDED its full verdict
  # (proof_failure.json / graph_certificate.json / the gate certs are uploaded as artifacts), so
  # every GT-quality signal — determinism mismatch, empty/thin brief, degraded graph/LSP/embedder,
  # full-potential — is COUNTED reliably in the post-run audit WITHOUT gating the paid task. Per
  # GT's thesis (the agent self-localizes; GT only BOOSTS), the agent ALWAYS runs BEST-EFFORT on a
  # GT-quality metric (run6 ran these same tasks with 33-byte near-empty briefs). Coupling the
  # measurement into the paid task wasted spend and hid the signal, and it degenerated into
  # whack-a-mole per reason (determinism, then brief-empty, then the next). The ONLY hard-aborts
  # are the two RESULT-VALIDITY classes handled above: legitimacy/anti-cheat (wrong-commit,
  # eval-leakage, unportable/mislabeled substrate, dead surface) and OOM/no-build (rc=137) — those
  # make the RESULT invalid, not merely GT-unhelpful. Everything else -> best-effort, verdict
  # recorded, counted afterwards.
  else
    echo "::warning::gt-run-proof exited $PROOF_RC — trial proceeds BEST-EFFORT with degraded GT (correct-or-quiet; never refuse a task for quality)"
    write_proof_status ok PROOF_DEGRADED "gt-run-proof exited $PROOF_RC but agent trial proceeds best-effort"
  fi
else
  docker rm -f gtsrc 2>/dev/null || true
fi

# §E GT_ARTIFACT_MISSING / SUBSTRATE_MISSING_CERTS — verify the 8-artifact contract.
# brief.txt INCLUDED (P0.1-c): the agent consumes /gt_artifacts/brief.txt read-only;
# in proof mode an empty/missing brief is GT_ARTIFACT_MISSING, never a WARN (there
# is no host fallback — host run_v74 is fail-closed by the container boundary).
_MISSING=0
_MISSING_CORE=0
for c in graph.db runtime_context.json lsp_certificate.json graph_certificate.json \
         embedder_certificate.json foundational_gate_report.json run_manifest.json \
         brief.txt; do
  if ! test -s "/tmp/gt/$c"; then
    fail_artifact "/gt_artifacts/$c absent after gt-run-proof"
    _MISSING=$((_MISSING + 1))
    # graph.db + brief.txt are the CORE substrate the agent consumes; absent = no usable substrate.
    case "$c" in graph.db|brief.txt) _MISSING_CORE=$((_MISSING_CORE + 1)) ;; esac
  fi
done
# §E artifacts are ALL GT-quality/measurement outputs (graph.db + brief.txt INCLUDED):
# GT-quality is RECORDED, never fatal. A missing/empty §E artifact — including the
# formerly-CORE graph.db/brief.txt (gt_run_proof degrade-and-continue writes an EMPTY
# brief.txt so downstream has the file) — is PROOF_DEGRADED best-effort, NOT exit 1.
# Only VALIDITY (host-split/commit-parity/dead-surface) + OOM/IO fail closed, and those
# are handled UPSTREAM (~721-786), never in §E. §E must never exit 1.
if [ "$_MISSING" -eq 0 ]; then
  echo "all 8 GT artifacts present under /tmp/gt (= /gt_artifacts)"
  write_proof_status ok PROOF_OK "all 8 GT artifacts present"
else
  echo "::warning::${_MISSING} GT artifact(s) missing/empty (${_MISSING_CORE} formerly-core graph.db/brief.txt) — degraded GT, best-effort (GT-quality never blocks a task)"
  write_proof_status ok PROOF_DEGRADED "${_MISSING} GT artifacts missing/empty (${_MISSING_CORE} formerly-core) but trial proceeds best-effort"
fi

# ── Cert-env handoff into the agent (§D) — the adapter reads these READ-ONLY ──
# GT_HOST_GRAPH_DB + GT_CERT_DIR are the canonical host->agent handoff (proof.py
# HOST_HANDOFF); the adapter resolves them via GTRuntimeContext.from_env (non-proof
# host-handoff branch, context.py:111-112). GT_REPO_ROOT kept for adapter brief compat.
{
  echo "GT_PORTABLE_SUBSTRATE=1"
  echo "GT_CERT_DIR=/tmp/gt"
  echo "GT_HOST_GRAPH_DB=/tmp/gt/graph.db"
  echo "GT_HOST_SRC_ROOT=/tmp/gt/src"
  echo "GT_REPO_ROOT=/tmp/gt/src"
  echo "GT_LSP_CERT=/tmp/gt/lsp_certificate.json"
  echo "GT_GRAPH_CERT=/tmp/gt/graph_certificate.json"
  echo "GT_EMBEDDER_CERT=/tmp/gt/embedder_certificate.json"
} >> "$GITHUB_ENV"
# Deep gate record alongside the per-task metrics (mandate: persist the 8-dp gate report).
cp -f /tmp/gt/foundational_gate_report.json "/tmp/gt_gates_deep_${GT_MATRIX_TASK}.json" 2>/dev/null || true
echo "substrate proof done: consumed /gt_artifacts; host LSP+gates SKIPPED (no divergent graph)"
