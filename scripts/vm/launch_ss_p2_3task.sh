#!/bin/bash
set -euo pipefail
DIGEST="sha256:610b67488602551df33d332c32d4865d46d316f752cae9d2cbd088398faa0e78"
OUT=/root/gt_runs/ss_p2_3task_1923ccd42_$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT"
python3 - "$OUT" <<'PY'
import json, sys
out = sys.argv[1]
src = "/root/groundtruth/artifact_deepswe/repo_manifest.json"
want = [
    "true-myth-iterable-collection-combinators",
    "actionlint-action-pinning-lint",
    "valibot-recursive-schema-composition",
]
man = json.load(open(src))
by = {t["instance_id"]: t for t in man["tasks"]}
missing = [i for i in want if i not in by]
assert not missing, missing
doc = {
    "benchmark": man.get("benchmark", "DeepSWE"),
    "source": man.get("source", ""),
    "total_tasks": 3,
    "tasks": [
        {
            "instance_id": by[i]["instance_id"],
            "language": by[i]["language"],
            "docker_image": by[i]["docker_image"],
        }
        for i in want
    ],
}
open(out + "/campaign_manifest.json", "w").write(json.dumps(doc, indent=2) + "\n")
print("wrote", out + "/campaign_manifest.json")
PY
export PATH=/root/gtvenv/bin:$PATH
export GT_PIER_VENV=/root/gtvenv
export GT_RL_PROFILE=2
export GT_SEM_BODY=1
export GT_CONTENT_LEG=1
export GT_PASSAGE_WIDE=1
export GT_POST_SEARCH=1
export GT_OBLIGATIONS_V2=1
export GT_GIT_COMMIT=$(git -C /root/groundtruth rev-parse HEAD)
export GT_ORCHESTRATED=1
export STOP_AT_COST=80
export DEEPSEEK_API_KEY=$(tr -d '\r\n' </root/.deepseek_key)
nohup env PATH="$PATH" GT_PIER_VENV="$GT_PIER_VENV" GT_RL_PROFILE=2 GT_SEM_BODY=1 GT_CONTENT_LEG=1 GT_PASSAGE_WIDE=1 GT_POST_SEARCH=1 GT_OBLIGATIONS_V2=1 GT_GIT_COMMIT="$GT_GIT_COMMIT" GT_ORCHESTRATED=1 STOP_AT_COST=80 DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
  bash /root/groundtruth/scripts/vm/gt_agent_run.sh \
    --manifest "$OUT/campaign_manifest.json" \
    --digest "ghcr.io/hbali-stack/gt-substrate@$DIGEST" \
    --model deepseek/deepseek-v4-flash \
    --pier-config /root/groundtruth/artifact_deepswe/gt_integration/deepswe_gt_pier.yaml \
    --parallel 1 \
    --out "$OUT" \
    --ghcr-owner hbali-stack \
  >"$OUT/campaign_launch.log" 2>&1 &
echo "PID=$! OUT=$OUT" | tee /root/gt_runs/ss_p2_3task_LATEST.txt
sleep 12
wc -l "$OUT/campaign_launch.log"
tail -80 "$OUT/campaign_launch.log"
