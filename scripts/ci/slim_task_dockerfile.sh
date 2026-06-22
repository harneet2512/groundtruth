#!/usr/bin/env bash
# THE ENV-START FIX. A DeepSWE task's environment/Dockerfile rebuilds the WHOLE task env FROM scratch
# at pier env-start:  FROM .../mars-base  ->  git clone <repo>  ->  go mod download all / npm ci /
# cargo fetch  ->  go build ./... + go test ./... (or the language equivalent). For a GIANT repo that
# is minutes of compile that swap-thrashes dockerd to ~9.8GB on a 16GB runner and NEVER produces a
# running container inside pier's start window -> "[ENV-START] NO container in 900s -> hung deploy"
# (the REAL cause of every giant's env-start failure — not the image re-pull, which was a red herring).
#
# But task.toml [environment].docker_image (passed here as $2 = the matrix image) IS the PRE-BUILT
# result of that exact Dockerfile: its tag == the task ext_id == the build hash, and it contains the
# already-cloned repo + already-baked dep cache (it is the very image GT pulls and indexes into
# graph.db). The GHCR mirror + `docker tag` (substrate_proof.sh:130) already loaded it LOCALLY before
# pier runs. So rewriting the Dockerfile to `FROM <that image>` makes pier's `docker compose build` an
# instant no-op layer on a local image instead of a from-scratch rebuild — the container comes up in
# seconds, env-IDENTICAL (same repo, same deps, same base), zero benchmaxxing, every task the same way.
#
# Usage: slim_task_dockerfile.sh <task_id> <prebuilt_image_ref>
set -eu
T="${1:?task_id}"; IMG="${2:-}"
DF="deepswe-bench/tasks/${T}/environment/Dockerfile"
if [ -z "$IMG" ] || [ ! -f "$DF" ]; then
  echo "[DOCKERFILE SLIM] skip — image='${IMG}' df='${DF}' (missing); pier will build from scratch"
  exit 0
fi
printf 'FROM %s\nCMD ["/bin/bash"]\n' "$IMG" > "$DF"
echo "[DOCKERFILE SLIM] ${DF} -> FROM ${IMG} (use pre-built local image; skip the from-scratch env rebuild that OOM-hung env-start 900s)"
