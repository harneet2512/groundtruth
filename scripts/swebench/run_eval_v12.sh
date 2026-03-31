#!/bin/bash
source ~/gt-venv/bin/activate
export PATH=$HOME/.local/bin:$PATH
RES=~/results/v12_pro_60_20260329_053019
echo "Starting eval at $(date -u) UTC"
python3 ~/eval_v12_pro.py $RES/baseline/preds.json $RES/eval_bl 4 > ~/eval_bl.log 2>&1 &
python3 ~/eval_v12_pro.py $RES/gt_v12/preds.json $RES/eval_gt 4 > ~/eval_gt.log 2>&1 &
wait
echo "BOTH_EVAL_DONE at $(date -u) UTC"
