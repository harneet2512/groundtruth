#!/bin/bash
# Benchmark gt-index against real-world repos.
# Usage: ./benchmark.sh [path-to-gt-index-binary]
#
# Expects repos at D:/test-repos/{click,terraform,cpython,sentry,grafana,kubernetes}

set -e

GTINDEX="${1:-./gt-index.exe}"

if [ ! -f "$GTINDEX" ]; then
    echo "Error: gt-index binary not found at $GTINDEX"
    echo "Build it first: cd gt-index && go build -o gt-index.exe ./cmd/gt-index/"
    exit 1
fi

REPOS=(
    "click|D:/test-repos/click|small Python"
    "terraform|D:/test-repos/terraform|Go backend"
    "cpython|D:/test-repos/cpython|Python stdlib"
    "sentry|D:/test-repos/sentry|Python+TS"
    "grafana|D:/test-repos/grafana|TS+Go"
    "kubernetes|D:/test-repos/kubernetes|massive Go"
)

echo "======================================"
echo "  gt-index Benchmark Suite"
echo "======================================"
echo ""

for entry in "${REPOS[@]}"; do
    IFS='|' read -r name root desc <<< "$entry"

    if [ ! -d "$root" ]; then
        echo "SKIP: $name ($root not found)"
        echo ""
        continue
    fi

    output="/tmp/gt_${name}.db"
    rm -f "$output"

    echo "--- $name ($desc) ---"
    echo "Root: $root"

    # Count files
    file_count=$(find "$root" -type f \( -name "*.py" -o -name "*.go" -o -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" -o -name "*.rs" -o -name "*.java" -o -name "*.rb" -o -name "*.php" -o -name "*.cs" -o -name "*.kt" -o -name "*.swift" -o -name "*.scala" -o -name "*.c" -o -name "*.cpp" -o -name "*.h" -o -name "*.lua" -o -name "*.sh" -o -name "*.ex" -o -name "*.exs" \) 2>/dev/null | wc -l)
    echo "Source files: ~$file_count"

    # Run indexer
    start_time=$(date +%s%N)
    "$GTINDEX" --root="$root" --output="$output" --max-files=50000 2>&1 | tail -6
    end_time=$(date +%s%N)

    elapsed_ms=$(( (end_time - start_time) / 1000000 ))
    echo "Wall time: ${elapsed_ms}ms"

    # DB size
    if [ -f "$output" ]; then
        db_size=$(du -h "$output" | cut -f1)
        echo "DB size: $db_size"
    fi

    echo ""
done

echo "======================================"
echo "  Benchmark Complete"
echo "======================================"
