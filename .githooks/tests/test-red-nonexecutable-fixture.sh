#!/bin/sh
set -eu

ROOT=$(git rev-parse --show-toplevel)
HOOK="$ROOT/.githooks/pre-push"
ZERO=0000000000000000000000000000000000000000
TMP_ROOT=$(mktemp -d)
resolved=$(cd "$TMP_ROOT" && pwd -P)
case "$resolved" in /tmp/*) ;; *) exit 2 ;; esac
trap 'rm -rf -- "$resolved"' EXIT
export GIT_AUTHOR_DATE='2001-01-01T00:00:00Z' GIT_COMMITTER_DATE='2001-01-01T00:00:00Z'

repo="$resolved/repo"
git init -q "$repo"
git -C "$repo" config user.name fixture-test
git -C "$repo" config user.email fixture-test@example.invalid
git -C "$repo" config core.autocrlf false
mkdir -p "$repo/gt-index/internal/resolver"
printf '%s\n' 'package resolver' >"$repo/gt-index/internal/resolver/resolver.go"
git -C "$repo" add .
git -C "$repo" commit -qm base
base=$(git -C "$repo" rev-parse HEAD)
git -C "$repo" update-ref refs/remotes/origin/main "$base"

mkdir -p "$repo/.githooks/tests" "$repo/.githooks/red-artifacts"
printf '%s\n' '#!/bin/sh' 'printf "%s\n" "real failure" >&2' 'exit 7' >"$repo/.githooks/tests/nonexec.sh"
printf '%s\n' 'real failure' >"$repo/.githooks/red-artifacts/nonexec.out"
fixture_hash=$(sha256sum "$repo/.githooks/tests/nonexec.sh" | awk '{print $1}')
output_hash=$(sha256sum "$repo/.githooks/red-artifacts/nonexec.out" | awk '{print $1}')
cat >"$repo/.githooks/red-artifacts/nonexec.receipt" <<EOF
format=gt.fixture-red.v1
base_sha=$base
fixture_path=.githooks/tests/nonexec.sh
fixture_sha256=$fixture_hash
command=.githooks/tests/nonexec.sh
exit_code=7
output_path=.githooks/red-artifacts/nonexec.out
output_sha256=$output_hash
EOF
git -C "$repo" add .githooks
git -C "$repo" commit -qm 'test(red): non-executable fixture evidence'
mode=$(git -C "$repo" ls-tree HEAD -- .githooks/tests/nonexec.sh | awk '{print $1}')
[ "$mode" = 100644 ]

printf '%s\n' 'package resolver // protected' >"$repo/gt-index/internal/resolver/resolver.go"
git -C "$repo" add .
git -C "$repo" commit -qm protected
head=$(git -C "$repo" rev-parse HEAD)
set +e
(
  cd "$repo"
  printf 'refs/heads/topic %s refs/heads/topic %s\n' "$head" "$ZERO" |
    GT_FIXTURE_FIRST_TRACE=1 "$HOOK" origin unused
) >/dev/null 2>&1
status=$?
set -e
if [ "$status" -eq 0 ]; then
  printf '%s\n' 'RED: non-executable receipt fixture accepted.'
  exit 1
fi
printf '%s\n' 'PASS: non-executable receipt fixture rejected'
