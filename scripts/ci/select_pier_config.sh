#!/usr/bin/env bash
set -euo pipefail

# Select the Pier mini-swe-agent config for DeepSWE.
# SWE-bench Pro does not use Pier; keep this helper out of that path.

benchmark="${1:?benchmark required: deepswe}"
model="${2:-}"

case "$benchmark" in
  deepswe)
    if echo "$model" | grep -qiE "minimax|tokenrouter"; then
      echo "artifact_deepswe/gt_integration/deepswe_gt_pier_minimax.yaml"
    elif echo "$model" | grep -qiE "gemini|vertex_ai"; then
      echo "artifact_deepswe/gt_integration/deepswe_gt_pier_gemini.yaml"
    else
      echo "artifact_deepswe/gt_integration/deepswe_gt_pier.yaml"
    fi
    ;;
  *)
    echo "unknown or non-Pier benchmark: $benchmark" >&2
    exit 2
    ;;
esac
