#!/usr/bin/env bash
# Precondition 1 for the contract-drift run (codespace only — needs CGO + gcc).
#
# The resolver stdlib-laundering test must be GREEN on the exact binary that builds
# the run's graph.db. It guards drift's caller counts: a qualified stdlib call
# (os.walk) must NOT resolve to a project function via a DETERMINISTIC method, or a
# phantom caller would inflate drift's "N callers depend on this".
#
#   GREEN -> DOC_OF_HONOR.md:355-357 ("laundering not yet killed") is STALE; update it.
#   RED   -> the resolver demote is not in this binary; drift must not count
#            edge-derived callers until fixed. BLOCKS the run.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../../gt-index" || { echo "[precond1] gt-index dir not found"; exit 2; }

echo "[precond1] go test resolver stdlib-laundering ..."
CGO_ENABLED=1 go test -tags sqlite_fts5 ./internal/resolver \
    -run TestResolve_QualifiedStdlibCall_NotDeterministic -v
rc=$?
echo "----------------------------------------------------------------"
if [ "$rc" -eq 0 ]; then
  echo "[precond1] GREEN — name_match/qualified-stdlib is NOT laundered as deterministic."
  echo "[precond1] ACTION: DOC_OF_HONOR.md:355-357 is stale; update it. Run may proceed."
else
  echo "[precond1] RED — laundering present in this binary."
  echo "[precond1] ACTION: fix resolver demote OR disable edge-derived caller counts in drift."
  echo "[precond1] This BLOCKS the run."
fi
exit "$rc"
