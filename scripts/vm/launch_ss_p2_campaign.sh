#!/usr/bin/env bash
# Campaign launch helper — run ON the UpCloud box AFTER bake digest is known.
# Pin: ss-unified-ss-live-20260722 @ 1923ccd42
# Usage:
#   export D=<digest-hex-or-sha256:...>
#   export DEEPSEEK_API_KEY=...   # already on box preferred
#   export REPO=/data/groundtruth # or wherever the clone lives
#   bash /path/to/launch_ss_p2_campaign.sh
set -euo pipefail

REPO="${REPO:-/data/groundtruth}"
cd "$REPO"
WANT_SHA="${WANT_SHA:-1923ccd42}"
test "$(git rev-parse --short=9 HEAD)" = "$WANT_SHA" \
  || { echo "FATAL: HEAD=$(git rev-parse --short=9 HEAD) want=$WANT_SHA"; exit 1; }

D="${D:?set D to substrate digest hex}"
case "$D" in
  sha256:*) DIGEST_PIN="ghcr.io/hbali-stack/gt-substrate@$D" ;;
  *)        DIGEST_PIN="ghcr.io/hbali-stack/gt-substrate@sha256:$D" ;;
esac
docker pull "$DIGEST_PIN"
docker image inspect "$DIGEST_PIN" >/dev/null
test -n "${DEEPSEEK_API_KEY:-}" || { echo "FATAL: DEEPSEEK_API_KEY missing"; exit 1; }

OUT_DIR="/root/gt_runs/ss_p2_campaign_${WANT_SHA}_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT_DIR"
MANIFEST="$OUT_DIR/campaign_manifest.json"
python3 - <<'PY' "$REPO/artifact_deepswe/repo_manifest.json" "$MANIFEST"
import json, sys
src, dst = sys.argv[1], sys.argv[2]
want = [
    "actionlint-action-pinning-lint",
    "bandit-structured-nosec-directives",
    "csstree-shorthand-expansion-compression",
    "abs-module-cache-flags",
    "abs-stepped-slices",
    "httpx-streaming-json-iteration",
]
man = json.load(open(src, encoding="utf-8"))
by_id = {t["instance_id"]: t for t in man["tasks"]}
missing = [i for i in want if i not in by_id]
if missing:
    raise SystemExit(f"missing from manifest: {missing}")
out = {
    "benchmark": man.get("benchmark", "DeepSWE"),
    "source": man.get("source", ""),
    "total_tasks": len(want),
    "tasks": [
        {
            "instance_id": by_id[i]["instance_id"],
            "language": by_id[i]["language"],
            "docker_image": by_id[i]["docker_image"],
        }
        for i in want
    ],
}
json.dump(out, open(dst, "w", encoding="utf-8"), indent=2)
print(f"wrote {dst} n={len(want)}")
PY

export GT_RL_PROFILE=2
export GT_GIT_COMMIT="$(git rev-parse HEAD)"
export GT_ORCHESTRATED=1
export STOP_AT_COST="${STOP_AT_COST:-80}"

nohup env \
  GT_RL_PROFILE=2 \
  GT_GIT_COMMIT="$GT_GIT_COMMIT" \
  GT_ORCHESTRATED=1 \
  STOP_AT_COST="$STOP_AT_COST" \
  DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
  bash "$REPO/scripts/vm/gt_agent_run.sh" \
    --manifest "$MANIFEST" \
    --digest "$DIGEST_PIN" \
    --model deepseek/deepseek-v4-flash \
    --pier-config "$REPO/artifact_deepswe/gt_integration/deepswe_gt_pier.yaml" \
    --parallel 1 \
    --out "$OUT_DIR" \
    --ghcr-owner hbali-stack \
  >"$OUT_DIR/campaign_launch.log" 2>&1 &

echo "PID=$!"
echo "OUT_DIR=$OUT_DIR"
echo "DIGEST=$DIGEST_PIN"
echo "tail -f $OUT_DIR/campaign_launch.log"
