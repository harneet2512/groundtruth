#!/usr/bin/env bash
# gt_vertex_token_refresher.sh — HOST-side Vertex token minter for the in-container
# litellm shim (gt_vertex_token_shim.py). Mints an OAuth access token from the GCE
# metadata server (host has cloud-platform scope) every $REFRESH_INTERVAL_S and writes
# it atomically to $GT_AUTH_DIR/vertex_token. The agent container bind-mounts
# $GT_AUTH_DIR (read-only, as a DIRECTORY) so refreshes are visible live. Also stages
# the shim + a sitecustomize.py that imports it, so the agent's python auto-installs
# the shim from PYTHONPATH=$GT_AUTH_DIR.
#
# NO secret is ever written into the repo: $GT_AUTH_DIR defaults to /gt_auth (VM-only,
# 0755 dir so the in-container agent uid can read; token 0644 — a ~60-min VM-only
# credential). NEVER point GT_AUTH_DIR inside /data/groundtruth.
#
# Runs until killed (the runner starts it in the background and kills it on exit).
set -u

GT_AUTH_DIR="${GT_AUTH_DIR:-/gt_auth}"
SHIM_SRC="${SHIM_SRC:-}"
REFRESH_INTERVAL_S="${REFRESH_INTERVAL_S:-2400}"   # 40 min (token TTL ~60 min)
MD_URL="http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"

mkdir -p "$GT_AUTH_DIR"
chmod 755 "$GT_AUTH_DIR"

# Stage the shim + sitecustomize so the agent python auto-loads it from PYTHONPATH.
if [ -n "$SHIM_SRC" ] && [ -f "$SHIM_SRC" ]; then
  cp -f "$SHIM_SRC" "$GT_AUTH_DIR/gt_vertex_token_shim.py"
  chmod 644 "$GT_AUTH_DIR/gt_vertex_token_shim.py"
fi
cat > "$GT_AUTH_DIR/sitecustomize.py" <<'PYEOF'
try:
    import gt_vertex_token_shim
except Exception:
    pass
PYEOF
chmod 644 "$GT_AUTH_DIR/sitecustomize.py"

mint() {
  local tok
  tok="$(curl -s -m 10 -H 'Metadata-Flavor: Google' "$MD_URL" \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])' 2>/dev/null)"
  if [ -z "$tok" ]; then
    echo "[token-refresher] WARN: metadata mint returned empty token" >&2
    return 1
  fi
  printf '%s' "$tok" > "$GT_AUTH_DIR/vertex_token.tmp"
  chmod 644 "$GT_AUTH_DIR/vertex_token.tmp"
  mv -f "$GT_AUTH_DIR/vertex_token.tmp" "$GT_AUTH_DIR/vertex_token"
  echo "[token-refresher] minted token ($(date -u +%H:%M:%SZ), len=${#tok})"
}

# First mint MUST succeed (fail-closed for the caller to detect).
mint || { echo "[token-refresher] FATAL: initial mint failed"; exit 1; }

while true; do
  sleep "$REFRESH_INTERVAL_S"
  mint || true
done
