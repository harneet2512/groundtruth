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
printf '%s\n' '#!/bin/sh' 'printf "%s\n" "actual failure" >&2' 'exit 1' > "$repo/.githooks/tests/mismatch.sh"
chmod +x "$repo/.githooks/tests/mismatch.sh"
printf '%s\n' 'invented failure' > "$repo/.githooks/red-artifacts/mismatch.out"
fh=$(sha256sum "$repo/.githooks/tests/mismatch.sh"|awk '{print $1}'); oh=$(sha256sum "$repo/.githooks/red-artifacts/mismatch.out"|awk '{print $1}')
cat > "$repo/.githooks/red-artifacts/mismatch.receipt" <<EOF
format=gt.fixture-red.v1
base_sha=$base
fixture_path=.githooks/tests/mismatch.sh
fixture_sha256=$fh
command=.githooks/tests/mismatch.sh
exit_code=1
output_path=.githooks/red-artifacts/mismatch.out
output_sha256=$oh
EOF
git -C "$repo" add .githooks
git -C "$repo" update-index --chmod=+x .githooks/tests/mismatch.sh
git -C "$repo" commit -qm 'test(red): mismatched output evidence'
printf '%s\n' 'package resolver // protected' > "$repo/gt-index/internal/resolver/resolver.go"; git -C "$repo" add .; git -C "$repo" commit -qm protected
head=$(git -C "$repo" rev-parse HEAD); set +e
output=$(cd "$repo" && printf 'refs/heads/topic %s refs/heads/topic %s\n' "$head" "$ZERO" | GT_FIXTURE_FIRST_TRACE=1 "$HOOK" origin unused 2>&1); status=$?; set -e
printf '%s\nHOOK_EXIT=%s\n' "$output" "$status"
[ "$status" -ne 0 ] || { printf '%s\n' 'RED: mismatched runtime output accepted.' >&2; exit 1; }
printf '%s\n' 'PASS: mismatched runtime output rejected'
