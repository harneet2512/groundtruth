#!/bin/sh
# RED: the producer has no `reason`/`step` projection, no overload narrowing and
# no MRO linearisation. The evidence for a HAS_CALLSITE edge is spread across
# derivation columns with nothing that renders it, candidates that the argument
# count cannot reach are retained, and inherited candidate sets are published in
# whatever order the name index produced.
#
# The two packages are run in sequence, not as one `go test` invocation, so the
# per-package build failures print in a fixed order. Windows path separators are
# normalised: this output is re-executed and compared byte-for-byte by
# .githooks/pre-push.
set -u
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root/gt-index"
output_file=$(mktemp)
set +e
go test -tags sqlite_fts5 ./internal/resolver -count=1 >"$output_file" 2>&1
resolver_exit=$?
go test -tags sqlite_fts5 ./internal/store -count=1 >>"$output_file" 2>&1
store_exit=$?
set -e
sed 's#\\#/#g' "$output_file"
rm -f "$output_file"
if [ "$resolver_exit" -ne 0 ]; then
  exit "$resolver_exit"
fi
exit "$store_exit"
