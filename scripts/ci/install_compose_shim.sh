#!/usr/bin/env bash
# Force `docker compose up --pull never` (+ no-op a standalone `docker compose pull`) so pier's compose
# uses the locally-cached task image (GHCR mirror + ECR-ref tag from substrate_proof.sh:130) instead of
# RE-PULLING from flaky ECR (which stalls on 'Pulling fs layer' -> no container -> env-start timeout).
#
# v3 — shim EVERY docker binary, not just `command -v docker`. The workflow installs several dockers
# (/usr/bin/docker system, /usr/local/bin/docker, /tmp/docker/docker, /tmp/docker-cli-cache/docker) and
# pier may invoke ANY of them by absolute path — a single-binary shim is bypassed (proven: shimmed
# /usr/bin/docker but pier used /usr/local/bin/docker). So move each aside (.gtreal) and replace each
# in place + a PATH shim. The shimmed docker execs the real with `--pull never` injected; the real then
# invokes the compose plugin with that flag, so the plugin can't re-pull regardless of which docker ran.
set +e
mkdir -p /tmp/gt-shim

build_shim() {  # $1 = path to the REAL (moved-aside) docker this shim should exec
  echo '#!/usr/bin/env bash'
  echo "R=$1"
  echo 'if [ "$1" = compose ]; then'
  echo '  u=0; p=0; f=0'
  echo '  for a in "$@"; do [ "$a" = up ]&&u=1; [ "$a" = pull ]&&p=1; [ "$a" = --pull ]&&f=1; done'
  echo '  if [ "$p" = 1 ] && [ "$u" = 0 ]; then echo "[gt-shim] skip compose pull (image pre-cached local)" >&2; exit 0; fi'
  echo '  if [ "$u" = 1 ] && [ "$f" = 0 ]; then o=(); for a in "$@"; do o+=("$a"); [ "$a" = up ]&&o+=(--pull never); done; echo "[gt-shim] compose up --pull never (local image, no ECR re-pull)" >&2; exec "$R" "${o[@]}"; fi'
  echo 'fi'
  echo 'exec "$R" "$@"'
}

# Every docker pier might use: known install targets + anything resolvable on PATH.
CANDS="/usr/bin/docker /usr/local/bin/docker /tmp/docker/docker /tmp/docker-cli-cache/docker $(type -ap docker 2>/dev/null)"
DONE=""
for d in $(printf '%s\n' $CANDS | sort -u); do
  [ -x "$d" ] || continue
  case " $DONE " in *" $d "*) continue ;; esac
  R="${d}.gtreal"
  # already shimmed (R exists and d execs it)? skip re-copy so we never copy a shim onto itself.
  if [ ! -e "$R" ]; then sudo cp "$d" "$R" 2>/dev/null || cp "$d" "$R" 2>/dev/null; fi
  [ -e "$R" ] || continue
  if build_shim "$R" | sudo tee "$d" >/dev/null 2>&1 && sudo chmod +x "$d" 2>/dev/null; then
    DONE="$DONE $d"
  fi
done

# PATH shim (covers `docker` resolved via $PATH after the caller exports /tmp/gt-shim).
RPATH="$(command -v docker)"; [ -e "${RPATH}.gtreal" ] && RPATH="${RPATH}.gtreal"
build_shim "$RPATH" > /tmp/gt-shim/docker
chmod +x /tmp/gt-shim/docker
echo "[gt-shim] installed in-place at:${DONE:- (none)} + PATH(/tmp/gt-shim); real-suffix=.gtreal"
