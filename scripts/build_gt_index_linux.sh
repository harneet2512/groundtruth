#!/usr/bin/env bash
# RC-13 — Build a portable Linux gt-index binary.
#
# WHY
#   The bundled tools/sweagent/gt_edit/bin/gt-index is a 47MB glibc-3.2.0+
#   ELF (`file gt-index` confirms "dynamically linked, for GNU/Linux 3.2.0").
#   On any container running musl libc (Alpine and derivatives) or older
#   glibc (CentOS 7 base images), the binary fails to load with a cryptic
#   loader error and the L6 (gt-index -file) layer goes silently dark.
#   Audit finding A-008 / B-019 / RC-13 (b).
#
# STRATEGY
#   1. First pass: try `CGO_ENABLED=0 go build` (pure-Go static binary).
#      go-sqlite3 needs CGO and won't build this way; if the build fails
#      because of go-sqlite3, fall through to step 2.
#   2. Second pass: build with `musl-gcc` so the resulting binary's loader
#      is musl-compatible; works on both glibc and musl containers.
#   3. Probe the produced binary with `ldd` and `file`; refuse to ship a
#      dynamic glibc binary out of this script.
#
# This script must be run on a Linux host with a Go toolchain. It is a
# no-op stub on Windows / macOS.
#
# TODO(RC-13-build): the actual binary rebuild has to land on a Linux VM.
# This script gates on local toolchain detection so calling it on a Mac
# / Windows dev box prints a clear "build host required" message instead
# of producing a non-portable binary by accident.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SRC_DIR="${REPO_ROOT}/gt-index"
OUT_DIR="${RC13_OUT_DIR:-${REPO_ROOT}/tools/sweagent/gt_edit/bin}"
OUT_BIN="${OUT_DIR}/gt-index"

if [[ "${OSTYPE:-}" != linux-gnu* ]]; then
    echo "[build_gt_index_linux] OSTYPE=${OSTYPE:-unknown} — this script must run on Linux" >&2
    echo "[build_gt_index_linux] TODO(RC-13-build): rebuild on a Linux host (Ubuntu/musl-gcc)" >&2
    exit 2
fi

if ! command -v go >/dev/null 2>&1; then
    echo "[build_gt_index_linux] go toolchain missing — install go 1.22+ first" >&2
    exit 2
fi

mkdir -p "${OUT_DIR}"

echo "[build_gt_index_linux] src=${SRC_DIR}"
echo "[build_gt_index_linux] out=${OUT_BIN}"

# ---- Pass 1: pure-Go static (CGO_ENABLED=0) ---------------------------------
# go-sqlite3 requires CGO so this attempt is best-effort. If a CGO-free
# alternative (modernc.org/sqlite) is wired up later, this becomes the happy
# path.
echo "[build_gt_index_linux] attempting CGO_ENABLED=0 static build..."
if ( cd "${SRC_DIR}" && CGO_ENABLED=0 go build \
        -ldflags='-s -w -extldflags "-static"' \
        -o "${OUT_BIN}" \
        ./cmd/gt-index/ ); then
    echo "[build_gt_index_linux] CGO=0 build succeeded — static binary written"
else
    echo "[build_gt_index_linux] CGO=0 build failed (likely go-sqlite3 cgo dep) — falling back to musl-gcc"
    if ! command -v musl-gcc >/dev/null 2>&1; then
        echo "[build_gt_index_linux] musl-gcc missing; install musl-tools (Ubuntu) or apk add musl-dev (Alpine)" >&2
        exit 3
    fi
    # ---- Pass 2: musl-gcc CGO build -----------------------------------------
    # The resulting binary's interpreter is /lib/ld-musl-x86_64.so.1, which is
    # present on Alpine and resolvable on glibc systems via musl's
    # ld-linux-musl shim. Note: SQLite still gets statically linked when
    # `-ldflags '-extldflags "-static"'` is set together with CC=musl-gcc.
    ( cd "${SRC_DIR}" && CGO_ENABLED=1 CC=musl-gcc go build \
        -tags 'netgo,osusergo' \
        -ldflags='-s -w -extldflags "-static"' \
        -o "${OUT_BIN}" \
        ./cmd/gt-index/ )
    echo "[build_gt_index_linux] musl-gcc build succeeded"
fi

# ---- Verification ----------------------------------------------------------
echo "[build_gt_index_linux] file ${OUT_BIN}:"
file "${OUT_BIN}"

if ldd "${OUT_BIN}" 2>&1 | grep -q "not a dynamic executable"; then
    echo "[build_gt_index_linux] OK — binary is statically linked, no runtime libc dep"
else
    echo "[build_gt_index_linux] ldd output:"
    ldd "${OUT_BIN}" || true
    # Refuse to publish a dynamically-linked binary out of this script —
    # that defeats the whole point of RC-13 (b).
    echo "[build_gt_index_linux] FAIL — binary is dynamically linked; will NOT be portable across libc versions" >&2
    exit 4
fi

echo "[build_gt_index_linux] done. Verify on a target image:"
echo "    docker run --rm -v ${OUT_BIN}:/gt-index alpine:3.19 /gt-index -version"
