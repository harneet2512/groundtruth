#!/bin/bash
# Pull SWE-bench base images and build agent-server images for first 50 instances
# Run this in background: sudo nohup bash pull_and_build_images.sh > /tmp/pull_build.log 2>&1 &

OH_DIR="/root/oh-benchmarks"
TAG_PREFIX="62c2e7c"

# Get first 50 instance IDs
cd "$OH_DIR"
.venv/bin/python -c "
from datasets import load_dataset
ds = load_dataset('princeton-nlp/SWE-bench_Lite', split='test')
for i in range(min(50, len(ds))):
    print(ds[i]['instance_id'])
" 2>/dev/null > /tmp/first_50_ids.txt

# For each instance, check if agent-server image exists, if not try to build
while read -r instance_id; do
    # Convert instance_id to tag components
    # e.g., django__django-11019 -> org=django, repo_issue=django-11019
    org=$(echo "$instance_id" | cut -d'_' -f1)
    repo_issue=$(echo "$instance_id" | sed 's/^[^_]*__//')
    org_underscore=$(echo "$org" | tr '-' '_')

    agent_tag="ghcr.io/openhands/eval-agent-server:${TAG_PREFIX}-sweb.eval.x86_64.${org_underscore}_1776_${repo_issue}-source-minimal"
    base_tag="docker.io/swebench/sweb.eval.x86_64.${org_underscore}_1776_${repo_issue}:latest"

    # Skip if agent-server image already exists
    if docker image inspect "$agent_tag" > /dev/null 2>&1; then
        echo "SKIP (exists): $instance_id"
        continue
    fi

    # Try pulling agent-server from GHCR first
    if docker pull "$agent_tag" 2>/dev/null; then
        echo "PULLED (ghcr): $instance_id"
        continue
    fi

    # Pull base image and build
    echo "BUILDING: $instance_id (pulling base: $base_tag)"
    if ! docker pull "$base_tag" 2>/dev/null; then
        echo "  FAILED (no base): $instance_id"
        continue
    fi

    # Try building via OpenHands
    cd "$OH_DIR"
    .venv/bin/python -c "
import sys
sys.path.insert(0, '.')
from benchmarks.utils.build_utils import build_image
result = build_image(
    base_image='$base_tag',
    target_image='ghcr.io/openhands/eval-agent-server',
    custom_tag='${org_underscore}_1776_${repo_issue}',
    target='source-minimal',
    push=False,
)
if result.error:
    print(f'  BUILD FAILED: {result.error}')
else:
    print(f'  BUILT: {result.tags}')
" 2>/dev/null
done < /tmp/first_50_ids.txt

echo ""
echo "=== Final count ==="
docker images --format '{{.Tag}}' | grep "${TAG_PREFIX}-sweb" | wc -l
echo "agent-server images available"
