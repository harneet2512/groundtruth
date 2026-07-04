#!/bin/bash
# Pull SWE-bench base images and build agent-server images for first 50 instances
# MUST run with: export PATH=/root/.local/bin:$PATH
# Run: sudo bash -c 'export PATH=/root/.local/bin:$PATH && bash /path/to/pull_and_build_images.sh'

set -e
export PATH="/root/.local/bin:$PATH"
OH_DIR="/root/oh-benchmarks"
TAG_PREFIX="62c2e7c"
BUILT=0
SKIPPED=0
FAILED=0

# Get first 50 instance IDs
cd "$OH_DIR"
.venv/bin/python -c "
from datasets import load_dataset
ds = load_dataset('princeton-nlp/SWE-bench_Lite', split='test')
for i in range(min(50, len(ds))):
    print(ds[i]['instance_id'])
" 2>/dev/null > /tmp/first_50_ids.txt

TOTAL=$(wc -l < /tmp/first_50_ids.txt)
echo "=== Building images for $TOTAL instances ==="
echo "PATH=$PATH"
echo "uv=$(which uv)"

while read -r instance_id; do
    org=$(echo "$instance_id" | cut -d'_' -f1)
    repo_issue=$(echo "$instance_id" | sed 's/^[^_]*__//')
    org_underscore=$(echo "$org" | tr '-' '_')

    # Check if any matching agent-server image exists
    if docker images --format '{{.Tag}}' | grep -q "${TAG_PREFIX}.*${org_underscore}_1776_${repo_issue}"; then
        echo "SKIP: $instance_id"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Try GHCR pull first
    agent_tag="ghcr.io/openhands/eval-agent-server:${TAG_PREFIX}-sweb.eval.x86_64.${org_underscore}_1776_${repo_issue}-source-minimal"
    if docker pull "$agent_tag" 2>/dev/null; then
        echo "PULLED: $instance_id"
        BUILT=$((BUILT + 1))
        continue
    fi

    # Pull base and build
    base_tag="docker.io/swebench/sweb.eval.x86_64.${org_underscore}_1776_${repo_issue}:latest"
    echo "BUILD: $instance_id"

    if ! docker pull "$base_tag" 2>/dev/null; then
        echo "  NO BASE: $instance_id"
        FAILED=$((FAILED + 1))
        continue
    fi

    # Build via Python with PATH set
    cd "$OH_DIR"
    .venv/bin/python -c "
import sys, os
os.environ['PATH'] = '/root/.local/bin:' + os.environ.get('PATH', '')
sys.path.insert(0, '.')
from openhands.agent_server.docker.build import BuildOptions, build_with_telemetry
opts = BuildOptions(
    base_image='$base_tag',
    custom_tags='${org_underscore}_1776_${repo_issue}',
    image='ghcr.io/openhands/eval-agent-server',
    target='source-minimal',
    platforms=['linux/amd64'],
    push=False,
)
try:
    result = build_with_telemetry(opts)
    print(f'  OK: {result.tags[0] if result.tags else \"no tag\"}')
except Exception as e:
    print(f'  FAIL: {e}')
" 2>/dev/null
    BUILT=$((BUILT + 1))
done < /tmp/first_50_ids.txt

echo ""
echo "=== DONE ==="
echo "Skipped: $SKIPPED, Built: $BUILT, Failed: $FAILED"
echo "Total images:"
docker images --format '{{.Tag}}' | grep "${TAG_PREFIX}" | grep "sweb\|1776" | wc -l

# Write runnable instances
docker images --format '{{.Tag}}' | grep "${TAG_PREFIX}" | while read tag; do
    echo "$tag" | sed "s/.*${TAG_PREFIX}-sweb.eval.x86_64.//;s/-source-minimal//" | sed 's/_1776_/__/' | sed 's/^\([^_]*\)_/\1-/' | sed 's/__/__/'
done | sort -u > /tmp/all_runnable_instances.txt
echo "Runnable instances: $(wc -l < /tmp/all_runnable_instances.txt)"
