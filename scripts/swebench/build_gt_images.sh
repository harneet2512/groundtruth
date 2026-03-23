#!/usr/bin/env bash
# Bake gt_tool.py into all local SWE-bench eval images.
#
# Usage:
#   bash scripts/swebench/build_gt_images.sh
#   bash scripts/swebench/build_gt_images.sh --filter django  # only django images
#
# What it does:
#   1. Finds all local Docker images matching sweb.eval.*
#   2. For each image, builds a thin layer on top with gt_tool.py at /tmp/gt_tool.py
#   3. Tags the new image with the SAME name (overwrites the original tag)
#
# This means the MCP bridge's _ensure_setup() will detect gt_tool.py already
# exists in the container and skip the docker cp step.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
GT_TOOL="$REPO_DIR/benchmarks/swebench/gt_tool.py"
DOCKERFILE="$SCRIPT_DIR/Dockerfile.gt_layer"

if [ ! -f "$GT_TOOL" ]; then
    echo "ERROR: gt_tool.py not found at $GT_TOOL"
    exit 1
fi

if [ ! -f "$DOCKERFILE" ]; then
    echo "ERROR: Dockerfile.gt_layer not found at $DOCKERFILE"
    exit 1
fi

# Parse optional filter
FILTER="${1:-}"
if [ "$FILTER" = "--filter" ] && [ -n "${2:-}" ]; then
    FILTER="$2"
else
    FILTER=""
fi

# Create temp build context
BUILD_DIR=$(mktemp -d /tmp/gt_build_XXXXXX)
cp "$GT_TOOL" "$BUILD_DIR/gt_tool.py"
cp "$DOCKERFILE" "$BUILD_DIR/Dockerfile"

# Find all sweb.eval images
echo "=== Scanning for SWE-bench eval images ==="
IMAGES=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^sweb\.eval\.' | sort)

if [ -z "$IMAGES" ]; then
    echo "No SWE-bench eval images found. Build them first:"
    echo "  uv run python -m benchmarks.swebench.build_images --dataset princeton-nlp/SWE-bench_Lite --split test"
    rm -rf "$BUILD_DIR"
    exit 1
fi

# Apply filter if specified
if [ -n "$FILTER" ]; then
    IMAGES=$(echo "$IMAGES" | grep "$FILTER" || true)
    if [ -z "$IMAGES" ]; then
        echo "No images matching filter '$FILTER'"
        rm -rf "$BUILD_DIR"
        exit 1
    fi
fi

TOTAL=$(echo "$IMAGES" | wc -l)
echo "Found $TOTAL images to process"
echo ""

# Build each image
COUNT=0
FAILED=0
while IFS= read -r image; do
    COUNT=$((COUNT + 1))
    echo "[$COUNT/$TOTAL] Baking gt_tool.py into $image ..."

    if docker build -q -f "$BUILD_DIR/Dockerfile" --build-arg "BASE_IMAGE=$image" -t "$image" "$BUILD_DIR" > /dev/null 2>&1; then
        echo "  OK"
    else
        echo "  FAILED"
        FAILED=$((FAILED + 1))
    fi
done <<< "$IMAGES"

# Cleanup
rm -rf "$BUILD_DIR"

echo ""
echo "=== Done ==="
echo "Processed: $COUNT"
echo "Failed: $FAILED"

if [ "$FAILED" -gt 0 ]; then
    echo "WARNING: $FAILED images failed to build. Check Docker logs."
    exit 1
fi
