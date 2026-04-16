#!/bin/bash
source ~/sweagent-env/bin/activate
cd /tmp/SWE-agent
export PATH=$HOME/.local/bin:$PATH
export OPENAI_API_KEY=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
export OPENROUTER_API_KEY=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
mkdir -p /tmp/smoke_final
python3 -m sweagent run-batch \
  --config /tmp/SWE-agent/config/canary_gt_ds.yaml \
  --instances.type swe_bench \
  --instances.subset verified \
  --instances.split test \
  --instances.filter astropy__astropy-12907 \
  --output_dir /tmp/smoke_final
