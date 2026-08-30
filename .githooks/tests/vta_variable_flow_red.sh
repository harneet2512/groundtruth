#!/bin/sh
set -eu

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root/gt-index"
go test ./internal/resolver -run '^TestVTAVariableFlowRetainsAlternativesAndAbstainsAmbiguity$' -count=1
