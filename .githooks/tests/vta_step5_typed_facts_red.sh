#!/bin/sh
set -eu
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root/gt-index"
echo "VTA RED boundary: typed flow facts (rebound)"
echo "VTA RED boundary: typed flow facts"
output_file=$(mktemp)
set +e
go test ./internal/store -run '^TestVTAPassCarriesTypedFlowIDs$' -count=1 >"$output_file" 2>&1
test_exit=$?
set -e
sed -E -e 's#\\#/#g' -e 's#[0-9]+\.[0-9]+s#DURATIONs#g' "$output_file"
rm -f "$output_file"
exit "$test_exit"
