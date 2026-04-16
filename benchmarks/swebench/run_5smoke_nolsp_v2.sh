#!/bin/bash
source ~/sweagent-env/bin/activate
cd /tmp/SWE-agent
export PATH=$HOME/.local/bin:$PATH

# Background token refresher — writes fresh token every 45 min
TOKEN_FILE=/tmp/vertex_token
gcloud auth print-access-token > $TOKEN_FILE
(while true; do sleep 2700; gcloud auth print-access-token > $TOKEN_FILE 2>/dev/null; done) &
TOKEN_PID=$!

export OPENAI_API_KEY=$(cat $TOKEN_FILE)

TASKS="astropy__astropy-12907 astropy__astropy-13033 astropy__astropy-13236 astropy__astropy-13398 astropy__astropy-13453"
OUTDIR=/tmp/smoke5v3_nolsp
rm -rf $OUTDIR
mkdir -p $OUTDIR

echo "=== 5-task smoke no-LSP v3 $(date) ===" | tee $OUTDIR/master.log
echo "Token refresher PID=$TOKEN_PID" | tee -a $OUTDIR/master.log

for T in $TASKS; do
  mkdir -p $OUTDIR/$T
  export GT_TELEMETRY_DIR=$OUTDIR/$T
  export OPENAI_API_KEY=$(cat $TOKEN_FILE)
  python3 -m sweagent run-batch \
    --config /tmp/SWE-agent/config/canary_gt_ds.yaml \
    --instances.type swe_bench --instances.subset verified --instances.split test \
    --instances.filter $T \
    --output_dir $OUTDIR/$T \
    > $OUTDIR/$T/run.log 2>&1 &
  echo "  $T PID=$!" | tee -a $OUTDIR/master.log
done

echo "All 5 launched. Waiting..." | tee -a $OUTDIR/master.log
wait
kill $TOKEN_PID 2>/dev/null
echo "=== ALL DONE $(date) ===" | tee -a $OUTDIR/master.log

# Auto-eval
echo "Merging predictions..." | tee -a $OUTDIR/master.log
python3 -c "
import json, glob
preds = {}
for tf in sorted(glob.glob('$OUTDIR/astropy*/astropy*/*.traj')):
    d = json.load(open(tf))
    info = d.get('info', {})
    tid = tf.split('/')[-2]
    preds[tid] = {'model_patch': info.get('submission','') or '', 'instance_id': tid, 'model_name_or_path': 'deepseek-v3.2-nolsp'}
json.dump(preds, open('$OUTDIR/preds.json', 'w'))
print(f'Merged {len(preds)} predictions')
"
echo "Running eval..." | tee -a $OUTDIR/master.log
python3 -m swebench.harness.run_evaluation \
  --predictions_path $OUTDIR/preds.json \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --run_id smoke5v3_nolsp \
  --max_workers 3 \
  >> $OUTDIR/eval.log 2>&1
echo "Eval done" | tee -a $OUTDIR/master.log
grep -E 'resolved|Instances' $OUTDIR/eval.log | tee -a $OUTDIR/master.log
