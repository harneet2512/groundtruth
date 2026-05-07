#!/usr/bin/env bash
# Build the gt-index linux binary into bin/gt-index-linux.
#
# The V1R-map pretask path needs a Linux gt-index binary to build /tmp/graph.db
# inside the OH runtime container. This script produces it from gt-index/.
#
# Two modes:
#   1. Native Linux/WSL with Go 1.22+ and a C toolchain (CGO is required for
#      go-sqlite3): runs `go build` directly.
#   2. Any host with Docker: uses a digest-pinned golang base image and
#      cross-compiles inside.
#
# The output is bin/gt-index-linux, which is gitignored. Re-running is safe.
#
# Env overrides:
#   GT_INDEX_BUILD_MODE=native|docker  (default: auto-detect)
#   GT_INDEX_GO_IMAGE                  (default below — RC-17 pinned digest)
#
# RC-17 (F-003): the build invocation injects (commitSHA, buildTimeUTC,
# goToolchain) via -ldflags='-X main.commitSHA=...' so the resulting
# binary stamps these into project_meta on every run. Adds -trimpath
# (strips local paths from the binary) and -mod=readonly (refuses to
# rewrite go.mod silently). The Docker base image is digest-pinned so
# rebuilding from the same commit produces the same binary.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC_DIR="$REPO_DIR/gt-index"
OUT_DIR="$REPO_DIR/bin"
OUT_BIN="$OUT_DIR/gt-index-linux"

# RC-17 (F-003): digest-pinned go base image. The :latest / :1.22-bookworm
# floating tags can advance; pinning the sha256 freezes the toolchain.
# Override via GT_INDEX_GO_IMAGE if a specific patch release is needed.
# TODO(RC-17-build): bump this digest as part of the toolchain-update SOP;
# verify the new digest matches the upstream go release notes before
# committing.
DEFAULT_GO_IMAGE="golang:1.22.5-bookworm@sha256:30bd2d5cc0ab74b88de9067884ec6bf87c0b88f0d5fcb7bbb35a3a7fdda71cdc"
GO_IMAGE="${GT_INDEX_GO_IMAGE:-$DEFAULT_GO_IMAGE}"

mkdir -p "$OUT_DIR"

mode="${GT_INDEX_BUILD_MODE:-}"
if [ -z "$mode" ]; then
  if command -v go >/dev/null && [ "$(uname -s)" = "Linux" ]; then
    mode=native
  elif command -v docker >/dev/null; then
    mode=docker
  else
    echo "FATAL: no Go toolchain on Linux and no Docker available" >&2
    echo "  install one or set GT_INDEX_BUILD_MODE explicitly" >&2
    exit 1
  fi
fi

# RC-17 (F-003): collect the build stamps. git rev-parse falls back to
# "unknown" outside a git work-tree (e.g., on a release tarball build);
# the in-binary defaults are also "unknown" so the meaning is consistent.
COMMIT_SHA="$(cd "$REPO_DIR" && git rev-parse HEAD 2>/dev/null || echo unknown)"
BUILD_TIME_UTC="$(date -u +%FT%TZ)"
GO_TOOLCHAIN_ENV="${GT_INDEX_GO_TOOLCHAIN:-}"

LDFLAGS="-X main.commitSHA=${COMMIT_SHA} -X main.buildTimeUTC=${BUILD_TIME_UTC}"

echo "=== build_gt_index_linux: mode=$mode out=$OUT_BIN ==="
echo "    commit=${COMMIT_SHA}"
echo "    built_at=${BUILD_TIME_UTC}"
echo "    go_image=${GO_IMAGE}"

case "$mode" in
  native)
    cd "$SRC_DIR"
    GO_TOOLCHAIN_NATIVE="${GO_TOOLCHAIN_ENV:-$(go version | awk '{print $3}')}"
    GOOS=linux GOARCH=amd64 CGO_ENABLED=1 go build \
      -trimpath \
      -mod=readonly \
      -ldflags "${LDFLAGS} -X main.goToolchain=${GO_TOOLCHAIN_NATIVE}" \
      -o "$OUT_BIN" ./cmd/gt-index/
    ;;
  docker)
    docker run --rm \
      -v "$REPO_DIR":/workspace \
      -w /workspace/gt-index \
      "$GO_IMAGE" \
      bash -c "set -euo pipefail; \
               apt-get update -qq && apt-get install -qq -y gcc libc6-dev >/dev/null && \
               GO_TC=\$(go version | awk '{print \$3}') && \
               GOOS=linux GOARCH=amd64 CGO_ENABLED=1 go build \
                 -trimpath \
                 -mod=readonly \
                 -ldflags \"${LDFLAGS} -X main.goToolchain=\${GO_TC}\" \
                 -o /workspace/bin/gt-index-linux ./cmd/gt-index/"
    ;;
  *)
    echo "FATAL: unknown mode $mode" >&2
    exit 1
    ;;
esac

chmod +x "$OUT_BIN"
ls -la "$OUT_BIN"
file "$OUT_BIN" 2>/dev/null || true
echo "OK: built $OUT_BIN"
