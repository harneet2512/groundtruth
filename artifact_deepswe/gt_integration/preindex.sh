#!/bin/bash
# Pre-build graph.db indexes for all DeepSWE repos.
#
# Runs gt-index inside each DeepSWE Docker container and copies the
# resulting graph.db to the host. This is a one-time setup step that
# must complete before running tasks with GT briefing.
#
# Usage:
#   ./preindex.sh [--workers W] [--language LANG] [--output-dir DIR]
#
# Requirements:
#   - Docker with access to pull DeepSWE images from public.ecr.aws
#   - gt-index-linux binary (set GT_INDEX_BINARY or auto-detected)
#
# Output layout:
#   {output-dir}/{instance_id}/graph.db
#
# Examples:
#   ./preindex.sh --workers 4                    # Index all 91 repos, 4 parallel
#   ./preindex.sh --language python               # Index only Python repos
#   ./preindex.sh --output-dir /data/gt_indexes   # Custom output location

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST="$REPO_ROOT/repo_manifest.json"

# Defaults
WORKERS=1
LANGUAGE=""
OUTPUT_DIR="${GT_PREBUILT_INDEXES_ROOT:-$REPO_ROOT/indexes}"

# Find gt-index-linux
GT_INDEX="${GT_INDEX_BINARY:-}"
if [[ -z "$GT_INDEX" ]]; then
    for candidate in \
        "$SCRIPT_DIR/../../gt-index/gt-index-linux" \
        "$REPO_ROOT/../gt-index/gt-index-linux" \
        "/usr/local/bin/gt-index"; do
        if [[ -f "$candidate" ]]; then
            GT_INDEX="$(realpath "$candidate")"
            break
        fi
    done
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --workers|-w) WORKERS="$2"; shift 2 ;;
        --language) LANGUAGE="$2"; shift 2 ;;
        --output-dir|-o) OUTPUT_DIR="$2"; shift 2 ;;
        --gt-index) GT_INDEX="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--workers W] [--language LANG] [--output-dir DIR]"
            echo "  --workers W      Parallel indexing jobs (default: 1)"
            echo "  --language LANG  Filter by language"
            echo "  --output-dir DIR Where to store graph.db files (default: ./indexes)"
            echo "  --gt-index PATH  Path to gt-index-linux binary"
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ -z "$GT_INDEX" || ! -f "$GT_INDEX" ]]; then
    echo "ERROR: gt-index-linux binary not found"
    echo "Set GT_INDEX_BINARY or pass --gt-index /path/to/gt-index-linux"
    exit 1
fi

if [[ ! -f "$MANIFEST" ]]; then
    echo "ERROR: repo_manifest.json not found at $MANIFEST"
    exit 1
fi

echo "=== DeepSWE Pre-Indexing ==="
echo "gt-index: $GT_INDEX"
echo "Output:   $OUTPUT_DIR"
echo "Workers:  $WORKERS"
echo "Language: ${LANGUAGE:-all}"
echo ""

mkdir -p "$OUTPUT_DIR"

# Get unique (instance_id, docker_image) pairs
TASKS=$(python3 -c "
import json
with open('$MANIFEST') as f:
    m = json.load(f)
tasks = m['tasks']
lang = '$LANGUAGE'
if lang:
    tasks = [t for t in tasks if t['language'] == lang]
seen = set()
for t in tasks:
    key = t['instance_id']
    if key not in seen:
        seen.add(key)
        print(f\"{t['instance_id']}|{t['docker_image']}|{t['language']}\")
")

TOTAL=$(echo "$TASKS" | wc -l | tr -d ' ')
echo "Tasks to index: $TOTAL"
echo ""

index_task() {
    local line="$1"
    local instance_id docker_image language
    IFS='|' read -r instance_id docker_image language <<< "$line"

    local db_dir="$OUTPUT_DIR/$instance_id"
    local db_path="$db_dir/graph.db"

    # Skip if already indexed
    if [[ -f "$db_path" ]]; then
        local db_size
        db_size=$(stat -c%s "$db_path" 2>/dev/null || stat -f%z "$db_path" 2>/dev/null || echo 0)
        if [[ "$db_size" -gt 1000 ]]; then
            echo "[SKIP] $instance_id ($language) — already indexed ($db_size bytes)"
            return 0
        fi
    fi

    echo "[INDEX] $instance_id ($language) — pulling image..."
    local container_name="gt_idx_${instance_id//[^a-zA-Z0-9]/_}"

    # Pull image if needed
    docker pull "$docker_image" > /dev/null 2>&1 || true

    # Start container
    docker run -d --name "$container_name" "$docker_image" sleep 3600 > /dev/null 2>&1

    # Copy gt-index into container
    docker cp "$GT_INDEX" "$container_name:/tmp/gt-index"
    docker exec "$container_name" chmod +x /tmp/gt-index

    # Detect repo root inside container
    local repo_root
    repo_root=$(docker exec "$container_name" bash -c '
        for d in /testbed /home/user /workspace /app /repo; do
            if [ -d "$d/.git" ]; then echo "$d"; exit 0; fi
        done
        # Fallback: find first .git
        find / -maxdepth 3 -name .git -type d 2>/dev/null | head -1 | sed "s|/.git||"
    ')

    if [[ -z "$repo_root" ]]; then
        echo "[WARN] $instance_id — no repo root found, using /home/user"
        repo_root="/home/user"
    fi

    # Run indexing
    local start_time
    start_time=$(date +%s)

    if docker exec "$container_name" /tmp/gt-index -root="$repo_root" -output=/tmp/graph.db 2>/dev/null; then
        local end_time elapsed db_size
        end_time=$(date +%s)
        elapsed=$((end_time - start_time))

        # Copy graph.db back
        mkdir -p "$db_dir"
        docker cp "$container_name:/tmp/graph.db" "$db_path"
        db_size=$(stat -c%s "$db_path" 2>/dev/null || stat -f%z "$db_path" 2>/dev/null || echo 0)

        echo "[DONE] $instance_id ($language) — ${elapsed}s, ${db_size} bytes, root=$repo_root"
    else
        echo "[FAIL] $instance_id ($language) — gt-index failed"
    fi

    # Cleanup container
    docker rm -f "$container_name" > /dev/null 2>&1
}

export -f index_task
export OUTPUT_DIR GT_INDEX

if [[ $WORKERS -gt 1 ]]; then
    echo "Indexing $TOTAL repos with $WORKERS parallel workers..."
    echo "$TASKS" | xargs -P "$WORKERS" -I {} bash -c 'index_task "$@"' _ {}
else
    while IFS= read -r line; do
        index_task "$line"
    done <<< "$TASKS"
fi

echo ""
echo "=== Pre-Indexing Complete ==="

# Summary
INDEXED=$(find "$OUTPUT_DIR" -name "graph.db" | wc -l | tr -d ' ')
TOTAL_SIZE=$(du -sh "$OUTPUT_DIR" 2>/dev/null | cut -f1)
echo "Indexed: $INDEXED/$TOTAL repos"
echo "Total size: $TOTAL_SIZE"
echo "Output: $OUTPUT_DIR"
