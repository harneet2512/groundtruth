#!/usr/bin/env bash
# Install a `docker` shim that forces `docker compose up --pull never` (+ no-ops a standalone
# `docker compose pull`) so pier's compose uses the locally-cached task image (GHCR mirror + the
# ECR-ref tag from substrate_proof.sh:130) instead of RE-PULLING it from the flaky ECR registry
# (which stalls on 'Pulling fs layer' -> no container -> env-start timeout). The image is GUARANTEED
# local (proof asserts `docker image inspect`), so this is correct-or-fail-fast.
#
# BULLETPROOF install — intercepts compose no matter how pier resolves docker:
#   (1) PATH shim at /tmp/gt-shim/docker (caller must `export PATH="/tmp/gt-shim:$PATH"` after), AND
#   (2) IN-PLACE replacement at the real docker binary path (catches absolute-path callers too).
# Both exec the moved-aside real docker for everything except `compose up`/`compose pull`.
set -e
mkdir -p /tmp/gt-shim
R=$(command -v docker || echo /usr/bin/docker)   # real docker, e.g. /usr/local/bin/docker
RREAL="${R}.gtreal"
# Copy the real docker aside (idempotent) so both shim copies can exec it.
if [ ! -x "$RREAL" ]; then sudo cp "$R" "$RREAL" 2>/dev/null || cp "$R" "$RREAL"; fi

build_shim() {
  echo '#!/usr/bin/env bash'
  echo "R=$RREAL"
  echo 'if [ "$1" = compose ]; then'
  echo '  u=0; p=0; f=0'
  echo '  for a in "$@"; do [ "$a" = up ]&&u=1; [ "$a" = pull ]&&p=1; [ "$a" = --pull ]&&f=1; done'
  echo '  if [ "$p" = 1 ] && [ "$u" = 0 ]; then echo "[gt-shim] skip compose pull (image pre-cached local)" >&2; exit 0; fi'
  echo '  if [ "$u" = 1 ] && [ "$f" = 0 ]; then o=(); for a in "$@"; do o+=("$a"); [ "$a" = up ]&&o+=(--pull never); done; echo "[gt-shim] compose up --pull never (local image, no ECR re-pull)" >&2; exec "$R" "${o[@]}"; fi'
  echo 'fi'
  echo 'exec "$R" "$@"'
}

# (1) PATH shim
build_shim > /tmp/gt-shim/docker
chmod +x /tmp/gt-shim/docker
# (2) in-place replacement at the real binary (catches absolute-path callers; best-effort under sudo)
build_shim | sudo tee "$R" >/dev/null 2>&1 && sudo chmod +x "$R" 2>/dev/null \
  && echo "[gt-shim] installed PATH(/tmp/gt-shim) + IN-PLACE($R); real=$RREAL" \
  || echo "[gt-shim] installed PATH(/tmp/gt-shim) only (in-place replace failed); real=$RREAL"
