#!/bin/bash
# Hardcoded OpenRouter key was rotated upstream 2026-04-18; do not
# reintroduce literal credentials. Fail closed if the caller hasn't
# exported OPENROUTER_API_KEY.
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY must be exported before launching}"
source ~/sweagent-env/bin/activate
cd /tmp/SWE-agent
export PATH=$HOME/.local/bin:$PATH
export OPENAI_API_KEY="$OPENROUTER_API_KEY"
mkdir -p /tmp/smoke_final
python3 -m sweagent run-batch \
  --config /tmp/SWE-agent/config/canary_gt_ds.yaml \
  --instances.type swe_bench \
  --instances.subset verified \
  --instances.split test \
  --instances.filter astropy__astropy-12907 \
  --output_dir /tmp/smoke_final
