# DeepSWE + GroundTruth Results

This directory stores run results from `run_deepswe.sh` and `run_all.sh`.

## Directory Layout

```
results/
├── batch_20260526T180000/     # Batch run output
│   ├── run_config.json        # Run parameters
│   ├── logs/                  # Per-task stdout/stderr
│   │   └── {task_id}.log
│   ├── gt_meta/               # Per-task GT metadata
│   │   └── {task_id}.json     # {hook_injected, index_injected, brief_generated, ...}
│   ├── gt_logs/               # Per-task GT hook logs
│   │   └── {task_id}.jsonl    # Hook invocations (understand/verify)
│   └── predictions.jsonl      # Agent patches
└── README.md
```

## Analyzing Results

```bash
# Count resolved tasks
grep -c '"resolved": true' results/batch_*/predictions.jsonl

# Check GT brief coverage
grep -l '"brief_generated": true' results/batch_*/gt_meta/*.json | wc -l

# Check hook usage
for f in results/batch_*/gt_logs/*.jsonl; do
    task=$(basename "$f" .jsonl)
    calls=$(wc -l < "$f")
    echo "$task: $calls hook calls"
done
```
