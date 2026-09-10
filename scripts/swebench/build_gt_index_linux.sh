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
# 2026-09-03: the previous digest (sha256:30bd2d5c...) no longer resolves --
# "manifest unknown" from Docker Hub, which rotates the digests behind a patch
# tag. Re-pinned to the digest 1.22.5-bookworm resolves to today. The distro is
# deliberate, not incidental: the produced binary is glibc-linked and has to run
# inside the task containers, so building it on a newer base would raise its
# glibc floor.
DEFAULT_GO_IMAGE="golang:1.22.5-bookworm@sha256:af9b40f2b1851be993763b85288f8434af87b5678af04355b1e33ff530b5765f"
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
BUILD_TAGS="netgo,osusergo,sqlite_fts5"
# Hash every checked-in compiler input, including C/C++ headers. Relative paths
# are part of the digest so renames are identity changes while checkout location
# is not. The toolchain and build tags are bound separately below.
# The fingerprint reads git OBJECT content, not worktree bytes: a checkout may
# carry CRLF where the blob is LF (autocrlf), and hashing worktree files makes
# the stamp depend on the build host's eol policy. `git ls-tree -r` emits
# `mode type sha\tpath` straight from the commit tree — canonical on every
# platform. When HEAD is unavailable (tarball build) the fingerprint falls back
# to the worktree bytes with CR stripped, matching blob content for sources.
if git -C "$REPO_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
  # The fingerprint names the COMMIT's tree; a dirty worktree would build bytes
  # the stamp does not describe (the original binding failure's exact shape).
  if ! git -C "$REPO_DIR" diff --quiet HEAD -- gt-index || \
     ! git -C "$REPO_DIR" diff --cached --quiet HEAD -- gt-index; then
    echo "FATAL: gt-index worktree differs from HEAD — build from a clean tree" >&2
    exit 1
  fi
  SOURCE_FINGERPRINT="$(cd "$REPO_DIR" && git ls-tree -r HEAD -- gt-index | sed 's|\tgt-index/|\t|' | LC_ALL=C sort | grep -E '\.(go|c|cc|cpp|h|hpp|s)$|go\.(mod|sum)[[:space:]]*$' | sha256sum | awk '{print $1}')"
else
  SOURCE_FINGERPRINT="$(cd "$SRC_DIR" && find . -type f \( -name '*.go' -o -name '*.c' -o -name '*.cc' -o -name '*.cpp' -o -name '*.h' -o -name '*.hpp' -o -name '*.s' -o -name 'go.mod' -o -name 'go.sum' \) -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sed 's/^\([0-9a-f]\{64\}\) \*/\1  /' | sha256sum | awk '{print $1}')"
fi

LDFLAGS="-X main.commitSHA=${COMMIT_SHA} -X main.buildTimeUTC=${BUILD_TIME_UTC} -X main.sourceFingerprint=${SOURCE_FINGERPRINT} -X main.compiledBuildTags=${BUILD_TAGS}"

echo "=== build_gt_index_linux: mode=$mode out=$OUT_BIN ==="
echo "    commit=${COMMIT_SHA}"
echo "    built_at=${BUILD_TIME_UTC}"
echo "    go_image=${GO_IMAGE}"
echo "    source_fingerprint=${SOURCE_FINGERPRINT}"

case "$mode" in
  native)
    cd "$SRC_DIR"
    GO_TOOLCHAIN_NATIVE="${GO_TOOLCHAIN_ENV:-$(go version | awk '{print $3}')}"
    CC="${CC:-musl-gcc}" GOOS=linux GOARCH=amd64 CGO_ENABLED=1 go build \
      -tags "${BUILD_TAGS}" \
      -trimpath \
      -mod=readonly \
      -ldflags "${LDFLAGS} -X main.goToolchain=${GO_TOOLCHAIN_NATIVE} -linkmode external -extldflags -static" \
      -o "$OUT_BIN" ./cmd/gt-index/
    ;;
  docker)
    # The bind-mounted checkout is owned by the invoking user while the
    # container runs as root, so git refuses it as dubious ownership and
    # `go build` fails its VCS stamping with "error obtaining VCS status".
    # Trust the mount rather than passing -buildvcs=false: that stamping is
    # part of the binary identity this script exists to produce.
    # MSYS_NO_PATHCONV/MSYS2_ARG_CONV_EXCL must scope to THIS call only: blanket
    # env vars would also disable conversion for git -C "$REPO_DIR" above, and a
    # POSIX path git cannot read fails --verify HEAD and silently falls back to
    # the worktree fingerprint (measured: stamped 377487af instead of fb3af740).
    MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run --rm \
      -e GIT_CONFIG_COUNT=1 \
      -e GIT_CONFIG_KEY_0=safe.directory \
      -e GIT_CONFIG_VALUE_0=/workspace \
      -e HOST_UID="$(id -u)" \
      -e HOST_GID="$(id -g)" \
      -v "$(cygpath -w "$REPO_DIR" 2>/dev/null || printf '%s' "$REPO_DIR")":/workspace \
      -w /workspace/gt-index \
      "$GO_IMAGE" \
      bash -c "set -euo pipefail; \
               apt-get update -qq && apt-get install -qq -y musl-tools >/dev/null && \
               GO_TC=\$(go version | awk '{print \$3}') && \
               CC=musl-gcc GOOS=linux GOARCH=amd64 CGO_ENABLED=1 go build \
                 -tags ${BUILD_TAGS} \
                 -trimpath \
                 -mod=readonly \
                 -ldflags \"${LDFLAGS} -X main.goToolchain=\${GO_TC} -linkmode external -extldflags -static\" \
                 -o /workspace/bin/gt-index-linux ./cmd/gt-index/ && \
               chown \"\${HOST_UID}:\${HOST_GID}\" /workspace/bin/gt-index-linux"
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
