#!/bin/bash
source ~/sweagent-env/bin/activate
cd /tmp/SWE-agent
export PATH=$HOME/.local/bin:$PATH
export OPENAI_API_KEY=$(gcloud auth print-access-token)

TASKS="astropy__astropy-12907 astropy__astropy-13033 astropy__astropy-13236 astropy__astropy-13398 astropy__astropy-13453 astropy__astropy-13579 astropy__astropy-13977 astropy__astropy-14096 astropy__astropy-14182 astropy__astropy-14309"

OUTDIR=/tmp/vertex_10
rm -rf $OUTDIR
mkdir -p $OUTDIR

echo "=== Launching 10 tasks parallel on Vertex AI $(date) ===" | tee $OUTDIR/master.log

for T in $TASKS; do
  mkdir -p $OUTDIR/$T
  python3 -m sweagent run-batch \
    --config /tmp/SWE-agent/config/canary_gt_ds.yaml \
    --instances.type swe_bench --instances.subset verified --instances.split test \
    --instances.filter $T \
    --output_dir $OUTDIR/$T \
    > $OUTDIR/$T/run.log 2>&1 &
  echo "  $T PID=$!" | tee -a $OUTDIR/master.log
done

echo "All 10 launched. Waiting..." | tee -a $OUTDIR/master.log
wait
echo "=== ALL DONE $(date) ===" | tee -a $OUTDIR/master.log

# Summary
for T in $TASKS; do
  STEPS=$(grep -c STEP $OUTDIR/$T/run.log 2>/dev/null || echo 0)
  STATUS=$(grep -E 'submitted|Exiting' $OUTDIR/$T/run.log 2>/dev/null | tail -1 | head -c 60)
  echo "  $T: $STEPS steps | $STATUS" | tee -a $OUTDIR/master.log
done
