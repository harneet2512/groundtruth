#!/bin/bash
# 5-task smoke no-LSP (parallel — all 5 concurrent, litellm retries on 429)
# Venv path: absolute so `sudo systemd-run` (HOME=/root) still finds it.
SWEAGENT_VENV="${SWEAGENT_VENV:-/home/Lenovo/sweagent-env}"
source "$SWEAGENT_VENV/bin/activate"
cd /tmp/SWE-agent
export PATH=/home/Lenovo/.local/bin:$PATH
export OPENAI_API_KEY=dummy
# litellm built-in retry on 429/5xx
export LITELLM_NUM_RETRIES=6
export LITELLM_REQUEST_TIMEOUT=120

TASKS="astropy__astropy-12907 astropy__astropy-13033 astropy__astropy-13236 astropy__astropy-13398 astropy__astropy-13453"
OUTDIR=/tmp/smoke5par_nolsp
rm -rf $OUTDIR
mkdir -p $OUTDIR
export GT_RUN_ID="${GT_RUN_ID:-smoke5par_nolsp_$(date -u +%Y%m%dT%H%M%SZ)}"

echo "=== 5-task smoke no-LSP PARALLEL $(date) ===" | tee $OUTDIR/master.log

if ! curl -s http://localhost:4000/health | grep -qi 'healthy'; then
    echo "FATAL: LiteLLM proxy not healthy at localhost:4000" | tee -a $OUTDIR/master.log
    exit 1
fi
echo "Proxy health: OK" | tee -a $OUTDIR/master.log
echo "Launching 5 tasks concurrently..." | tee -a $OUTDIR/master.log

for T in $TASKS; do
  mkdir -p $OUTDIR/$T
  export GT_TELEMETRY_DIR=$OUTDIR/$T
  export GT_INSTANCE_ID=$T
  python3 -m sweagent run-batch \
    --config /tmp/SWE-agent/config/canary_gt_ds.yaml \
    --instances.type swe_bench --instances.subset verified --instances.split test \
    --instances.filter $T \
    --output_dir $OUTDIR/$T \
    > $OUTDIR/$T/run.log 2>&1 &
  echo "  $T PID=$!" | tee -a $OUTDIR/master.log
  sleep 2  # tiny stagger so proxy doesn't see 5 simultaneous auth handshakes
done
wait
echo "=== ALL DONE $(date) ===" | tee -a $OUTDIR/master.log

echo "Merging predictions..." | tee -a $OUTDIR/master.log
python3 -c "
import json, glob
preds = {}
for tf in sorted(glob.glob('$OUTDIR/astropy*/astropy*/*.traj')):
    d = json.load(open(tf))
    info = d.get('info', {})
    tid = tf.split('/')[-2]
    preds[tid] = {'model_patch': info.get('submission','') or '', 'instance_id': tid, 'model_name_or_path': 'deepseek-chat-v3-0324-nolsp'}
json.dump(preds, open('$OUTDIR/preds.json', 'w'))
print(f'Merged {len(preds)} predictions')
"
echo "Running eval..." | tee -a $OUTDIR/master.log
python3 -m swebench.harness.run_evaluation \
  --predictions_path $OUTDIR/preds.json \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --run_id smoke5par_nolsp \
  --max_workers 5 \
  >> $OUTDIR/eval.log 2>&1
echo "Eval done" | tee -a $OUTDIR/master.log
grep -E 'resolved|Instances' $OUTDIR/eval.log | tee -a $OUTDIR/master.log

echo "=== GT 4-METRIC SUMMARY ===" | tee -a $OUTDIR/master.log
echo "L1 delivery events:" | tee -a $OUTDIR/master.log
grep -c 'checkpoint_startup\|micro_emitted\|verify_emitted' $OUTDIR/*/gt_hook_telemetry.jsonl 2>/dev/null | tee -a $OUTDIR/master.log
echo "L2 material edits detected:" | tee -a $OUTDIR/master.log
grep -c 'material_edit' $OUTDIR/*/gt_hook_telemetry.jsonl 2>/dev/null | tee -a $OUTDIR/master.log
echo "L3 GT tool calls (action-field only) from trajectories:" | tee -a $OUTDIR/master.log
for traj in $OUTDIR/*/astropy*/*.traj; do
  [ -f "$traj" ] || continue
  tname=$(basename "$(dirname "$traj")")
  TRAJ_PATH="$traj" TNAME="$tname" python3 - <<'PYEOF' | tee -a $OUTDIR/master.log
import json, os, collections
p = os.environ['TRAJ_PATH']; name = os.environ['TNAME']
try:
    d = json.load(open(p))
except Exception as e:
    print(f"{name}: parse_error {e}"); raise SystemExit
hist = d.get('history') or d.get('trajectory') or []
gt = collections.Counter()
for e in hist:
    act = (e.get('action') or '').strip()
    if not act: continue
    tok = act.split()[0]
    if tok.startswith('gt_'):
        gt[tok] += 1
print(f"{name}: {dict(gt)}")
PYEOF
done
echo "L4 ACK followed/ignored/not_observed:" | tee -a $OUTDIR/master.log
grep -c 'ack_followed\|ack_ignored\|ack_not_observed' $OUTDIR/*/gt_hook_telemetry.jsonl 2>/dev/null | tee -a $OUTDIR/master.log
echo "Event histogram:" | tee -a $OUTDIR/master.log
cat $OUTDIR/*/gt_hook_telemetry.jsonl 2>/dev/null | grep -oE '"event":"[^"]*"' | sort | uniq -c | sort -rn | tee -a $OUTDIR/master.log
