#!/bin/sh
set -eu

ROOT=$(git rev-parse --show-toplevel)
HOOK="$ROOT/.githooks/pre-push"

if [ ! -f "$HOOK" ]; then
  printf '%s\n' 'RED: fixture-first pre-push hook is missing; a protected implementation can be pushed without a preceding failing-fixture receipt.' >&2
  exit 1
fi

ZERO=0000000000000000000000000000000000000000
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT

new_repo() {
  name=$1
  repo="$TMP_ROOT/$name"
  git init -q "$repo"
  git -C "$repo" config user.name fixture-test
  git -C "$repo" config user.email fixture-test@example.invalid
  git -C "$repo" config core.autocrlf false
  mkdir -p "$repo/gt-index/internal/resolver" "$repo/gt-index/internal/parser"
  printf '%s\n' 'package resolver' > "$repo/gt-index/internal/resolver/resolver.go"
  printf '%s\n' 'package parser' > "$repo/gt-index/internal/parser/parser.go"
  printf '%s\n' '# Groundtruth' > "$repo/README.md"
  git -C "$repo" add .
  git -C "$repo" commit -qm 'base'
  git -C "$repo" update-ref refs/remotes/origin/main "$(git -C "$repo" rev-parse HEAD)"
  printf '%s\n' "$repo"
}

run_hook() {
  repo=$1
  remote_sha=$2
  output=$3
  local_sha=$(git -C "$repo" rev-parse HEAD)
  before_tree=$(git -C "$repo" write-tree)
  before_status=$(git -C "$repo" status --porcelain=v1 -uno)
  set +e
  (
    cd "$repo"
    printf 'refs/heads/topic %s refs/heads/topic %s\n' "$local_sha" "$remote_sha" |
      GT_FIXTURE_FIRST_TRACE=1 "$HOOK" origin unused
  ) >"$output" 2>&1
  hook_status=$?
  set -e
  after_head=$(git -C "$repo" rev-parse HEAD)
  after_tree=$(git -C "$repo" write-tree)
  after_status=$(git -C "$repo" status --porcelain=v1 -uno)
  if [ "$after_head" != "$local_sha" ] || [ "$after_tree" != "$before_tree" ] || [ "$after_status" != "$before_status" ]; then
    printf '%s\n' 'fixture-first pre-push: source repository mutated during RED execution' >>"$output"
    return 99
  fi
  return "$hook_status"
}

expect_pass() {
  label=$1
  repo=$2
  remote_sha=$3
  output="$TMP_ROOT/$label.out"
  if ! run_hook "$repo" "$remote_sha" "$output"; then
    printf 'FAIL %s: expected pass\n' "$label" >&2
    cat "$output" >&2
    exit 1
  fi
}

expect_fail() {
  label=$1
  repo=$2
  remote_sha=$3
  pattern=$4
  output="$TMP_ROOT/$label.out"
  if run_hook "$repo" "$remote_sha" "$output"; then
    printf 'FAIL %s: expected rejection\n' "$label" >&2
    cat "$output" >&2
    exit 1
  fi
  if ! grep -F "$pattern" "$output" >/dev/null; then
    printf 'FAIL %s: rejection did not contain %s\n' "$label" "$pattern" >&2
    cat "$output" >&2
    exit 1
  fi
}

repo=$(new_repo protected_without_red)
base=$(git -C "$repo" rev-parse HEAD)
printf '%s\n' 'package resolver // protected implementation' > "$repo/gt-index/internal/resolver/resolver.go"
git -C "$repo" add .
git -C "$repo" commit -qm 'protected implementation without red'
expect_fail protected_without_red "$repo" "$ZERO" 'missing preceding dedicated RED fixture commit'

repo=$(new_repo docs_only)
printf '%s\n' '# Documentation only' > "$repo/README.md"
git -C "$repo" add README.md
git -C "$repo" commit -qm 'docs only'
expect_pass docs_only "$repo" "$ZERO"

repo=$(new_repo tests_only)
printf '%s\n' 'package resolver' > "$repo/gt-index/internal/resolver/resolver_test.go"
git -C "$repo" add .
git -C "$repo" commit -qm 'tests only'
expect_pass tests_only "$repo" "$ZERO"

repo=$(new_repo unrelated_production)
printf '%s\n' 'package parser // unrelated production' > "$repo/gt-index/internal/parser/parser.go"
git -C "$repo" add .
git -C "$repo" commit -qm 'unrelated production'
expect_pass unrelated_production "$repo" "$ZERO"

repo=$(new_repo malformed_receipt)
base=$(git -C "$repo" rev-parse HEAD)
mkdir -p "$repo/.githooks/tests" "$repo/.githooks/red-artifacts"
printf '%s\n' '#!/bin/sh' 'exit 1' > "$repo/.githooks/tests/protected_change_red.sh"
printf '%s\n' 'expected protected failure' > "$repo/.githooks/red-artifacts/protected.out"
cat > "$repo/.githooks/red-artifacts/protected.receipt" <<EOF
format=gt.fixture-red.v1
base_sha=$base
fixture_path=.githooks/tests/protected_change_red.sh
command=.githooks/tests/protected_change_red.sh
exit_code=1
output_path=.githooks/red-artifacts/protected.out
output_sha256=malformed
EOF
git -C "$repo" add .githooks
git -C "$repo" commit -qm 'test(red): malformed receipt'
printf '%s\n' 'package resolver // protected implementation' > "$repo/gt-index/internal/resolver/resolver.go"
git -C "$repo" add .
git -C "$repo" commit -qm 'protected implementation'
expect_fail malformed_receipt "$repo" "$ZERO" 'malformed RED receipt'

repo=$(new_repo legitimate_sequence)
base=$(git -C "$repo" rev-parse HEAD)
mkdir -p "$repo/.githooks/tests" "$repo/.githooks/red-artifacts"
printf '%s\n' '#!/bin/sh' 'printf "%s\\n" "expected protected failure" >&2' 'exit 1' > "$repo/.githooks/tests/protected_change_red.sh"
printf '%s\n' 'expected protected failure' > "$repo/.githooks/red-artifacts/protected.out"
output_hash=$(sha256sum "$repo/.githooks/red-artifacts/protected.out" | awk '{print $1}')
fixture_hash=$(sha256sum "$repo/.githooks/tests/protected_change_red.sh" | awk '{print $1}')
cat > "$repo/.githooks/red-artifacts/protected.receipt" <<EOF
format=gt.fixture-red.v1
base_sha=$base
fixture_path=.githooks/tests/protected_change_red.sh
fixture_sha256=$fixture_hash
command=.githooks/tests/protected_change_red.sh
exit_code=1
output_path=.githooks/red-artifacts/protected.out
output_sha256=$output_hash
EOF
git -C "$repo" add .githooks
git -C "$repo" commit -qm 'test(red): capture protected failure'
fixture_sha=$(git -C "$repo" rev-parse HEAD)
printf '%s\n' 'package resolver // protected implementation' > "$repo/gt-index/internal/resolver/resolver.go"
git -C "$repo" add .
git -C "$repo" commit -qm 'protected implementation after red'
expect_pass new_branch_range "$repo" "$ZERO"
grep -F "range_base=$base range_source=nearest_remote_ancestor" "$TMP_ROOT/new_branch_range.out" >/dev/null
expect_pass updated_branch_range "$repo" "$fixture_sha"
grep -F "range_base=$fixture_sha range_source=remote_sha" "$TMP_ROOT/updated_branch_range.out" >/dev/null

repo=$(new_repo indeterminate_new_branch)
git -C "$repo" update-ref -d refs/remotes/origin/main
printf '%s\n' 'package resolver // protected implementation' > "$repo/gt-index/internal/resolver/resolver.go"
git -C "$repo" add .
git -C "$repo" commit -qm 'protected implementation with no range base'
expect_fail indeterminate_new_branch "$repo" "$ZERO" 'cannot determine protected new-branch range'

"$ROOT/.githooks/tests/test-forged-red-evidence.sh"
"$ROOT/.githooks/tests/test-protected-root-range.sh"
"$ROOT/.githooks/tests/test-red-output-mismatch.sh"
"$ROOT/.githooks/tests/test-red-symlink-escape.sh"
"$ROOT/.githooks/tests/test-red-nonexecutable-fixture.sh"

printf '%s\n' 'PASS: fixture-first pre-push negative, positive, unrelated, malformed, new-branch, and updated-branch cases'
