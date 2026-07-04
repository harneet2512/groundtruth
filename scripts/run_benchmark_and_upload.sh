#!/usr/bin/env bash
# Run A/B benchmark and optionally upload results to S3.
#
# Usage:
#   ./scripts/run_benchmark_and_upload.sh
#   S3_BUCKET=my-bucket ./scripts/run_benchmark_and_upload.sh
#   CONDITION=both FIXTURE=python S3_BUCKET=my-bucket ./scripts/run_benchmark_and_upload.sh
#
# Requires: Python env with pip install -e ".[dev,benchmark]"
# Optional: AWS CLI configured; set S3_BUCKET to upload results.

set -e

cd "$(dirname "$0")/.."
CONDITION="${CONDITION:-both}"
FIXTURE="${FIXTURE:-python}"
OUTPUT_DIR="${OUTPUT_DIR:-benchmarks/ab/results}"
S3_BUCKET="${S3_BUCKET:-}"
RUN_ID=$(python -c "import uuid; print(uuid.uuid4())")

echo "Running A/B benchmark: condition=$CONDITION fixture=$FIXTURE run_id=$RUN_ID"
python -m benchmarks.ab.harness --condition "$CONDITION" --fixture "$FIXTURE" --output-dir "$OUTPUT_DIR"

if [ -n "$S3_BUCKET" ]; then
  PREFIX="benchmark-runs/${RUN_ID}"
  echo "Uploading results to s3://${S3_BUCKET}/${PREFIX}/"
  aws s3 cp "$OUTPUT_DIR/no_mcp.json" "s3://${S3_BUCKET}/${PREFIX}/no_mcp.json" 2>/dev/null || true
  aws s3 cp "$OUTPUT_DIR/with_groundtruth_mcp.json" "s3://${S3_BUCKET}/${PREFIX}/with_groundtruth_mcp.json" 2>/dev/null || true
  echo "Done. Compare: python -m benchmarks.ab.compare --results-dir $OUTPUT_DIR"
else
  echo "S3_BUCKET not set; skipping upload. Compare: python -m benchmarks.ab.compare --results-dir $OUTPUT_DIR"
fi
