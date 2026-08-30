#!/bin/sh
set -eu

ROOT=$(git rev-parse --show-toplevel)
HOOK="$ROOT/.githooks/pre-push"
ZERO=0000000000000000000000000000000000000000
TMP_ROOT=$(mktemp -d)
resolved=$(cd "$TMP_ROOT" && pwd -P)
case "$resolved" in /tmp/*) ;; *) exit 2 ;; esac
trap 'rm -rf -- "$resolved"' EXIT
export GIT_AUTHOR_DATE='2001-01-01T00:00:00Z'
export GIT_COMMITTER_DATE='2001-01-01T00:00:00Z'

repo="$resolved/repo"
git init -q "$repo"
git -C "$repo" config user.name fixture-test
git -C "$repo" config user.email fixture-test@example.invalid
git -C "$repo" config core.autocrlf false
mkdir -p "$repo/gt-index/internal/resolver"
printf '%s\n' 'package resolver' > "$repo/gt-index/internal/resolver/resolver.go"
git -C "$repo" add .
git -C "$repo" commit -qm base
base=$(git -C "$repo" rev-parse HEAD)
git -C "$repo" update-ref refs/remotes/origin/main "$base"

mkdir -p "$repo/.githooks/tests" "$repo/.githooks/red-artifacts"
printf '%s\n' '#!/bin/sh' 'exit 0' > "$repo/.githooks/tests/forged.sh"
printf '%s\n' 'invented failure' > "$repo/.githooks/red-artifacts/forged.out"
fixture_hash=$(sha256sum "$repo/.githooks/tests/forged.sh" | awk '{print $1}')
output_hash=$(sha256sum "$repo/.githooks/red-artifacts/forged.out" | awk '{print $1}')
cat > "$repo/.githooks/red-artifacts/forged.receipt" <<EOF
format=gt.fixture-red.v1
base_sha=$base
fixture_path=.githooks/tests/forged.sh
fixture_sha256=$fixture_hash
command=.githooks/tests/forged.sh
exit_code=1
output_path=.githooks/red-artifacts/forged.out
output_sha256=$output_hash
EOF
git -C "$repo" add .githooks
git -C "$repo" commit -qm 'test(red): forged failure evidence'
printf '%s\n' 'package resolver // protected change' > \
  "$repo/gt-index/internal/resolver/resolver.go"
git -C "$repo" add gt-index/internal/resolver/resolver.go
git -C "$repo" commit -qm 'protected implementation'
head=$(git -C "$repo" rev-parse HEAD)

set +e
output=$(
  cd "$repo" &&
    printf 'refs/heads/topic %s refs/heads/topic %s\n' "$head" "$ZERO" |
      GT_FIXTURE_FIRST_TRACE=1 "$HOOK" origin unused 2>&1
)
status=$?
set -e
printf '%s\n' "$output"
printf 'HOOK_EXIT=%s\n' "$status"
if [ "$status" -eq 0 ]; then
  printf '%s\n' 'RED: forged receipt accepted although its fixture exits zero.' >&2
  exit 1
fi
printf '%s\n' 'PASS: forged RED evidence rejected'
