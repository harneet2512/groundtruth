# Running the A/B benchmark on AWS

## Option 1: Docker on EC2/ECS

1. Build the image:
   ```bash
   docker build -t groundtruth-bench .
   ```

2. Run both conditions (default):
   ```bash
   docker run --rm groundtruth-bench
   ```

3. Run with custom args and mount output:
   ```bash
   docker run --rm -v $(pwd)/out:/app/benchmarks/ab/results \
     groundtruth-bench \
     python -m benchmarks.ab.harness --condition both --fixture all --output-dir /app/benchmarks/ab/results
   ```

4. Upload results to S3 (from host after run):
   ```bash
   aws s3 cp out/ s3://YOUR_BUCKET/benchmark-runs/$(date +%Y%m%d-%H%M%S)/ --recursive
   ```

## Option 2: Parameterized config

Use environment variables to drive the run:

| Variable     | Default    | Description                    |
|-------------|------------|--------------------------------|
| CONDITION   | both       | no_mcp, with_groundtruth_mcp, or both |
| FIXTURE     | python     | all, python, typescript, go    |
| OUTPUT_DIR  | benchmarks/ab/results | Local output path   |
| S3_BUCKET   | (none)     | If set, upload JSON results to s3://BUCKET/benchmark-runs/RUN_ID/ |

Example (Linux/macOS):
```bash
S3_BUCKET=my-bench-results CONDITION=both FIXTURE=all ./scripts/run_benchmark_and_upload.sh
```

## Option 3: AWS Batch / Step Functions

- Use the Docker image as the Batch job container.
- Command override: `python -m benchmarks.ab.harness --condition both --fixture python`.
- Store artifacts: copy container output from Batch to S3 using job definition `volumes` and a post-run script, or run the upload script inside the container with `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `S3_BUCKET` set.

## Comparison report

After both condition files exist (locally or downloaded from S3):

```bash
python -m benchmarks.ab.compare --results-dir benchmarks/ab/results
python -m benchmarks.ab.compare --results-dir benchmarks/ab/results --json
```
