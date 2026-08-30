#!/bin/sh
set -eu

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root/gt-index"
echo "VTA RED boundary: AnalyzeVTA is not implemented"
go test ./internal/resolver -run '^TestVTAVariableFlowRetainsAlternativesAndAbstainsAmbiguity$' -count=1
