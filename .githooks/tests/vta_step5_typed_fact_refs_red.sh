#!/bin/sh
set -eu
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root/gt-index"
echo "VTA RED boundary: typed flow fact references are persisted and bound"
output_file=$(mktemp)
set +e
go test ./internal/store -run '^(TestVTACandidateProvenancePersistsReferentialTypedFacts|TestVTACandidateProvenanceRejectsUnknownPrefixedFact)$' -count=1 >"$output_file" 2>&1
test_exit=$?
set -e
sed -E -e 's#\\#/#g' -e 's#[0-9]+\.[0-9]+s#DURATIONs#g' -e 's#20[0-9]{2}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}#TIMESTAMP#g' "$output_file"
rm -f "$output_file"
exit "$test_exit"
