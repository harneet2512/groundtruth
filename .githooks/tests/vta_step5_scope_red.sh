#!/bin/sh
set -eu
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root/gt-index"
output_file=$(mktemp)
set +e
go test ./internal/resolver -run '^TestVTASameFileScopeBoundary$' -count=1 >"$output_file" 2>&1
test_exit=$?
set -e
sed 's#\\#/#g' "$output_file"
rm -f "$output_file"
exit "$test_exit"
