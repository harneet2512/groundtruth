#!/bin/sh
set -eu
ROOT=$(git rev-parse --show-toplevel); HOOK="$ROOT/.githooks/pre-push"
ZERO=0000000000000000000000000000000000000000
TMP_ROOT=$(mktemp -d); resolved=$(cd "$TMP_ROOT" && pwd -P)
case "$resolved" in /tmp/*) ;; *) exit 2 ;; esac
trap 'rm -rf -- "$resolved"' EXIT
export GIT_AUTHOR_DATE='2001-01-01T00:00:00Z' GIT_COMMITTER_DATE='2001-01-01T00:00:00Z'
repo="$resolved/repo"; git init -q "$repo"
git -C "$repo" config user.name fixture-test; git -C "$repo" config user.email fixture-test@example.invalid; git -C "$repo" config core.autocrlf false
mkdir -p "$repo/gt-index/internal/resolver"; printf '%s\n' 'package resolver' > "$repo/gt-index/internal/resolver/resolver.go"
git -C "$repo" add .; git -C "$repo" commit -qm base; base=$(git -C "$repo" rev-parse HEAD); git -C "$repo" update-ref refs/remotes/origin/main "$base"
mkdir -p "$repo/.githooks/tests" "$repo/.githooks/red-artifacts"
target='../../../outside.sh'; blob=$(printf '%s' "$target" | git -C "$repo" hash-object -w --stdin)
git -C "$repo" update-index --add --cacheinfo 120000,"$blob",.githooks/tests/escape.sh
printf '%s\n' 'invented failure' > "$repo/.githooks/red-artifacts/escape.out"
fh=$(printf '%s' "$target"|sha256sum|awk '{print $1}'); oh=$(sha256sum "$repo/.githooks/red-artifacts/escape.out"|awk '{print $1}')
cat > "$repo/.githooks/red-artifacts/escape.receipt" <<EOF
format=gt.fixture-red.v1
base_sha=$base
fixture_path=.githooks/tests/escape.sh
fixture_sha256=$fh
command=.githooks/tests/escape.sh
exit_code=1
output_path=.githooks/red-artifacts/escape.out
output_sha256=$oh
EOF
git -C "$repo" add .githooks/red-artifacts; git -C "$repo" commit -qm 'test(red): symlink escape evidence'
printf '%s\n' 'package resolver // protected' > "$repo/gt-index/internal/resolver/resolver.go"; git -C "$repo" add .; git -C "$repo" commit -qm protected
head=$(git -C "$repo" rev-parse HEAD); set +e
output=$(cd "$repo" && printf 'refs/heads/topic %s refs/heads/topic %s\n' "$head" "$ZERO" | GT_FIXTURE_FIRST_TRACE=1 "$HOOK" origin unused 2>&1); status=$?; set -e
printf '%s\nHOOK_EXIT=%s\n' "$output" "$status"
[ "$status" -ne 0 ] || { printf '%s\n' 'RED: symlink fixture path escape accepted.' >&2; exit 1; }
printf '%s\n' 'PASS: symlink fixture path escape rejected'
