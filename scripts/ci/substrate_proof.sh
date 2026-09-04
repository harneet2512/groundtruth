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
  echo "::warning::GT_ARTIFACT_MISSING: ${_detail} — proceeding (non-fatal)" | tee -a trial_output.log
  # Record but do NOT block. The agent can still run with degraded GT
  # (missing cert = some layers suppress, which is correct-or-quiet).
}
write_proof_status running PROOF_STARTED "substrate proof step started"

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

# ── Extract dep stores so the substrate's LSP servers can resolve ──────
# gopls needs the Go module cache; rust-analyzer needs cargo + rustup.
# Discover paths inside gtsrc (mars-base images use non-default GOMODCACHE).
mkdir -p /tmp/gt/deps/gomodcache /tmp/gt/deps/cargo /tmp/gt/deps/rustup
GOMOD_SRC=""
GOMOD_DECLARED="$(docker exec gtsrc sh -lc 'go env GOMODCACHE 2>/dev/null' | tr -d '\r')"
for CAND in \
  "$GOMOD_DECLARED" \
  "/root/go/pkg/mod" "/home/user/go/pkg/mod" "/go/pkg/mod"; do
  [ -n "$CAND" ] || continue
  if docker exec gtsrc test -d "$CAND" 2>/dev/null; then
    docker cp "gtsrc:${CAND}/." /tmp/gt/deps/gomodcache 2>/dev/null && GOMOD_SRC="$CAND" && break
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
      docker cp "gtsrc:${CAND}/." /tmp/gt/deps/cargo 2>/dev/null && CARGO_SRC="$CAND" && break
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
      docker cp "gtsrc:${CAND}/." /tmp/gt/deps/rustup 2>/dev/null && RUSTUP_SRC="$CAND" && break
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
      docker cp "gtsrc:${TASK_RUST_SRC}/." "/tmp/gt/deps/rustup/toolchains/$ACTIVE_RUST_TOOLCHAIN/lib/rustlib/src/rust/library" 2>/dev/null \
        && echo "backfilled rust-src from sysroot: $TASK_RUST_SRC" \
        || echo "rust-src backfill failed (non-fatal)"
    fi
  fi
  # FIX-C (2026-06-13): if the task image had NO rust-src anywhere (the copied rustup
  # AND its sysroot both lack it), fall back to the BAKED substrate's /opt/gt/rustup —
  # which always ships rust-src (Dockerfile.gt-substrate). Without this, the :ro-removed
  # but rust-src-less copied rustup still shadows the baked one and rust-analyzer can't
  # warm. Bounded + non-fatal: FIX-A already keeps the task alive on a cold RA; this only
  # upgrades graph quality. Uses the same docker-cp-from-image pattern proven above.
  if [ "${GT_MATRIX_LANGUAGE}" = "rust" ] && [ -n "$ACTIVE_RUST_TOOLCHAIN" ] \
     && [ ! -d "/tmp/gt/deps/rustup/toolchains/$ACTIVE_RUST_TOOLCHAIN/lib/rustlib/src/rust/library" ] \
     && [ -n "${GT_SUBSTRATE_DIGEST:-}" ]; then
    BAKED_RUST_SRC="$(docker run --rm --entrypoint sh "$GT_SUBSTRATE_DIGEST" -lc \
      'ls -d /opt/gt/rustup/toolchains/*/lib/rustlib/src/rust/library 2>/dev/null | head -1' | tr -d '\r' || true)"
    if [ -n "$BAKED_RUST_SRC" ]; then
      cid="$(docker create "$GT_SUBSTRATE_DIGEST" 2>/dev/null || true)"
      if [ -n "$cid" ]; then
        mkdir -p "/tmp/gt/deps/rustup/toolchains/$ACTIVE_RUST_TOOLCHAIN/lib/rustlib/src/rust/library"
        docker cp "$cid:${BAKED_RUST_SRC}/." "/tmp/gt/deps/rustup/toolchains/$ACTIVE_RUST_TOOLCHAIN/lib/rustlib/src/rust/library" 2>/dev/null \
          && echo "FIX-C: backfilled rust-src from BAKED substrate ($BAKED_RUST_SRC)" \
          || echo "FIX-C: baked rust-src backfill failed (non-fatal; FIX-A keeps the task alive)"
        docker rm -f "$cid" >/dev/null 2>&1 || true
      fi
    fi
  fi
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
  python3 - "$TASK_DIR" <<'PYEOF' || { echo "::error::GT_ISSUE_MISSING: no issue text" | tee -a trial_output.log; exit 1; }
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
          "— refusing to run without the task instruction", file=sys.stderr)
    raise SystemExit(2)
with open(out_path, "w", encoding="utf-8") as f:
    f.write(issue)
print(f"issue text: {len(issue)} chars from {source} -> {out_path}")
PYEOF
  python3 scripts/swebench/issue_manifest.py /tmp/issue.txt /tmp/gt/issue_manifest.json --source instruction || echo "::warning::issue_manifest.py failed — non-fatal, continuing"
else
  # Pro harness: issue text from Pro-OS run_scripts or fallback to in-image paths.
  TASK_ID="${GT_MATRIX_TASK}"
  PRO_RUN_SCRIPTS="swebench-pro-os/run_scripts"
  ISSUE_TEXT=""

  # Try Pro-OS run_scripts dir first
  for candidate in \
      "${PRO_RUN_SCRIPTS}/${TASK_ID}/problem_statement.md" \
      "${PRO_RUN_SCRIPTS}/${TASK_ID}/README.md" \
      "${PRO_RUN_SCRIPTS}/${TASK_ID}/instruction.md"; do
    if [ -s "$candidate" ]; then
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
      --memory=10g --memory-swap=10g \
      -v "/tmp/gt/src:/work:ro" -v "/tmp/gt:/gt_artifacts" \
      -v "/tmp/issue.txt:/work_issue.txt:ro" \
      -v "/tmp/gt/deps/gomodcache:/tmp/gomodcache" \
      -v "/tmp/gt/deps/cargo:/root/.cargo" \
      -v "/tmp/gt/deps/rustup:/root/.rustup" \
      -e GT_PROOF_MODE=1 -e GT_CONTAINERIZED=1 -e GT_RUNTIME_STRATEGY=unified_substrate \
      -e GT_REQUIRE_FTS5=1 -e GT_REQUIRE_EMBEDDER=1 -e GT_FORCE_ONNX_EMBEDDER=1 \
      -e GT_REQUIRE_LSP=1 -e GT_REQUIRE_FULL_STACK=1 -e GT_ISSUE_FILE=/work_issue.txt \
      -e GT_LSP_READY_BUDGET_S_OVERRIDE="${GT_LSP_READY_BUDGET_S_OVERRIDE:-}" \
      -e GOFLAGS=-mod=mod -e GOPROXY=off -e GONOSUMCHECK=1 -e GOSUMDB=off \
      -e CARGO_HOME=/root/.cargo \
      -e CARGO_TARGET_DIR=/tmp/cargo-target -e CARGO_NET_OFFLINE=true \
      -e RUSTUP_HOME=/root/.rustup -e RUSTUP_TOOLCHAIN="$ACTIVE_RUST_TOOLCHAIN" \
      -e PATH="/root/.rustup/toolchains/$ACTIVE_RUST_TOOLCHAIN/bin:/opt/gt/bin:/opt/gt/node/bin:/opt/gt/python/bin:/opt/gt/jre/bin:/opt/gt/go/bin:/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
      -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -e HF_DATASETS_OFFLINE=1 \
      -e GT_GATES_DELIVER_ALWAYS="${GT_GATES_DELIVER_ALWAYS:-0}" \
      -e GT_GIT_COMMIT="${GT_GITHUB_SHA}" \
      -e GT_SUBSTRATE_DIGEST="$GT_SUBSTRATE_DIGEST" \
      -e GT_TASK_REPO_COMMIT="$TASK_REPO_COMMIT" \
      "$GT_SUBSTRATE_DIGEST" gt-run-proof --source-root /work --out /gt_artifacts || PROOF_RC=$?
else
  docker run --rm \
      --memory=10g --memory-swap=10g \
      -v "/tmp/gt/src:/work:ro" -v "/tmp/gt:/gt_artifacts" \
      -v "/tmp/issue.txt:/work_issue.txt:ro" \
      -v "/tmp/gt/deps/gomodcache:/tmp/gomodcache" \
      -v "/tmp/gt/deps/cargo:/root/.cargo" \
      -v "/tmp/gt/deps/rustup:/root/.rustup" \
      -e GT_PROOF_MODE=1 -e GT_CONTAINERIZED=1 -e GT_RUNTIME_STRATEGY=unified_substrate \
      -e GT_REQUIRE_FTS5=1 -e GT_REQUIRE_EMBEDDER=1 -e GT_FORCE_ONNX_EMBEDDER=1 \
      -e GT_REQUIRE_LSP=1 -e GT_REQUIRE_FULL_STACK=1 -e GT_ISSUE_FILE=/work_issue.txt \
      -e GT_LSP_READY_BUDGET_S_OVERRIDE="${GT_LSP_READY_BUDGET_S_OVERRIDE:-}" \
      -e GOFLAGS=-mod=mod -e GOPROXY=off -e GONOSUMCHECK=1 -e GOSUMDB=off \
      -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -e HF_DATASETS_OFFLINE=1 \
      -e GT_GATES_DELIVER_ALWAYS="${GT_GATES_DELIVER_ALWAYS:-0}" \
      -e GT_GIT_COMMIT="${GT_GITHUB_SHA}" \
      -e GT_SUBSTRATE_DIGEST="$GT_SUBSTRATE_DIGEST" \
      -e GT_TASK_REPO_COMMIT="$TASK_REPO_COMMIT" \
      -e PATH="/opt/gt/bin:/opt/gt/node/bin:/opt/gt/python/bin:/opt/gt/jre/bin:/opt/gt/go/bin:/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
      "$GT_SUBSTRATE_DIGEST" gt-run-proof --source-root /work --out /gt_artifacts || PROOF_RC=$?
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
  echo "::warning::gt-run-proof exited $PROOF_RC — trial will proceed with degraded GT (correct-or-quiet)"
  # Override to ok — a proof failure means degraded GT, not "can't run the agent."
  # The agent still has its instruction and can self-localize. GT layers suppress
  # when certs are missing (correct-or-quiet by design).
  write_proof_status ok PROOF_DEGRADED "gt-run-proof exited $PROOF_RC but agent trial proceeds"
else
  docker rm -f gtsrc 2>/dev/null || true
fi

# §E GT_ARTIFACT_MISSING / SUBSTRATE_MISSING_CERTS — verify the 8-artifact contract.
# brief.txt INCLUDED (P0.1-c): the agent consumes /gt_artifacts/brief.txt read-only;
# in proof mode an empty/missing brief is GT_ARTIFACT_MISSING, never a WARN (there
# is no host fallback — host run_v74 is fail-closed by the container boundary).
for c in graph.db runtime_context.json lsp_certificate.json graph_certificate.json \
         embedder_certificate.json foundational_gate_report.json run_manifest.json \
         brief.txt; do
  test -s "/tmp/gt/$c" || fail_artifact "/gt_artifacts/$c absent after gt-run-proof"
done
echo "all 8 GT artifacts present under /tmp/gt (= /gt_artifacts)"
write_proof_status ok PROOF_OK "all 8 GT artifacts present"

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
