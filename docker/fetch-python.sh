#!/usr/bin/env bash
# Fetch a self-contained, relocatable Python (python-build-standalone) into /opt/gt.
# Uses the GitHub API to resolve the latest 3.11 install_only build, AUTHENTICATED when a
# token is mounted (avoids the 60/hr unauthenticated rate-limit that broke the build), with
# retries + a couple of pinned fallbacks so a transient API hiccup can't fail the image.
set -euo pipefail

TOKEN="$(cat /run/secrets/github_token 2>/dev/null || true)"
HDR=(-H "Accept: application/vnd.github+json" -H "User-Agent: gt-substrate-build")
[ -n "$TOKEN" ] && HDR+=(-H "Authorization: Bearer $TOKEN")

PBS=""
for a in 1 2 3 4 5; do
  PBS="$(curl -fsSL "${HDR[@]}" \
          https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest 2>/tmp/api.err \
        | grep -oE 'https://[^"]*cpython-3\.11\.[0-9.]+\+[0-9]+-x86_64-unknown-linux-gnu-install_only\.tar\.gz' \
        | head -1 || true)"
  [ -n "$PBS" ] && break
  echo "API attempt $a: no URL (head: $(head -c 160 /tmp/api.err 2>/dev/null))" >&2
  sleep 8
done

# Pinned fallbacks (only used if the API never returned a URL).
if [ -z "$PBS" ]; then
  for U in \
    "https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.11.10+20241016-x86_64-unknown-linux-gnu-install_only.tar.gz" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/20240814/cpython-3.11.9+20240814-x86_64-unknown-linux-gnu-install_only.tar.gz" ; do
    if curl -fsSLI "$U" >/dev/null 2>&1; then PBS="$U"; break; fi
  done
fi

[ -n "$PBS" ] || { echo "FATAL: could not resolve python-build-standalone URL" >&2; exit 1; }
echo "python-build-standalone: $PBS"
curl -fsSL "$PBS" -o /tmp/py.tgz
tar -xzf /tmp/py.tgz -C /opt/gt            # -> /opt/gt/python
/opt/gt/python/bin/python3 --version
