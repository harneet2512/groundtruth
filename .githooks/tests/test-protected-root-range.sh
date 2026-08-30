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
printf '%s\n' 'package resolver // protected root' > \
  "$repo/gt-index/internal/resolver/resolver.go"
git -C "$repo" add .
git -C "$repo" commit -qm 'protected root without RED evidence'
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
  printf '%s\n' 'RED: protected root commit accepted without RED evidence.' >&2
  exit 1
fi

unprotected="$resolved/unprotected"
git init -q "$unprotected"
git -C "$unprotected" config user.name fixture-test
git -C "$unprotected" config user.email fixture-test@example.invalid
git -C "$unprotected" config core.autocrlf false
mkdir -p "$unprotected/gt-index/internal/parser"
printf '%s\n' 'package parser // unprotected root' > \
  "$unprotected/gt-index/internal/parser/parser.go"
git -C "$unprotected" add .
git -C "$unprotected" commit -qm 'unprotected root'
unprotected_head=$(git -C "$unprotected" rev-parse HEAD)
(
  cd "$unprotected"
  printf 'refs/heads/topic %s refs/heads/topic %s\n' "$unprotected_head" "$ZERO" |
    GT_FIXTURE_FIRST_TRACE=1 "$HOOK" origin unused
)
printf '%s\n' 'PASS: protected root history rejected'
