#!/usr/bin/env bash
# Install a `docker` shim that forces `docker compose up --pull never` (+ no-ops a standalone
# `docker compose pull`) so pier's compose uses the locally-cached task image (GHCR mirror + the
# ECR-ref tag from substrate_proof.sh:130) instead of RE-PULLING it from the flaky ECR registry
# (which stalls on 'Pulling fs layer' -> no container -> env-start timeout). The image is GUARANTEED
# local (proof asserts `docker image inspect`), so this is correct-or-fail-fast.
#
# The caller MUST run `export PATH="/tmp/gt-shim:$PATH"` AFTER this script so pier picks up the shim.
set -e
mkdir -p /tmp/gt-shim
# Capture the REAL docker BEFORE the caller modifies PATH (workflow installs it at
# /usr/local/bin/docker, not /usr/bin/docker — the shim must exec the SAME binary pier would use).
R=$(command -v docker || echo /usr/bin/docker)
{
  echo '#!/usr/bin/env bash'
  echo "R=$R"
  echo 'if [ "$1" = compose ]; then'
  echo '  u=0; p=0; f=0'
  echo '  for a in "$@"; do [ "$a" = up ]&&u=1; [ "$a" = pull ]&&p=1; [ "$a" = --pull ]&&f=1; done'
  echo '  if [ "$p" = 1 ] && [ "$u" = 0 ]; then echo "[gt-shim] skip compose pull (image pre-cached local)" >&2; exit 0; fi'
  echo '  if [ "$u" = 1 ] && [ "$f" = 0 ]; then o=(); for a in "$@"; do o+=("$a"); [ "$a" = up ]&&o+=(--pull never); done; echo "[gt-shim] compose up --pull never (local image, no ECR re-pull)" >&2; exec "$R" "${o[@]}"; fi'
  echo 'fi'
  echo 'exec "$R" "$@"'
} > /tmp/gt-shim/docker
chmod +x /tmp/gt-shim/docker
echo "[gt-shim] installed at /tmp/gt-shim/docker; real docker=$R (caller must export PATH=/tmp/gt-shim:\$PATH)"
