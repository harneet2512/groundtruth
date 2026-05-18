# GroundTruth + Inspect AI Adapter

Run SWE-bench evaluations with GroundTruth codebase intelligence tools using the [Inspect AI](https://inspect.ai-safety-institute.org.uk/) evaluation framework.

## Architecture

```
Inspect AI (eval framework)
    |
    +-- Task: SWE-bench-Live Lite dataset
    |
    +-- Agent: react() with bash + text_editor + GT tools
    |     |
    |     +-- groundtruth_brief    (pre-edit briefing)
    |     +-- groundtruth_trace    (caller/callee tracing)
    |     +-- groundtruth_validate (proposed code checking)
    |     +-- groundtruth_hotspots (most-referenced symbols)
    |     +-- groundtruth_impact   (blast radius analysis)
    |     +-- groundtruth_symbols  (file symbol listing)
    |
    +-- Hooks:
    |     +-- on_sample_init: build graph.db via gt-index
    |     +-- on_sample_end:  collect GT utilization metrics
    |
    +-- Sandbox: Docker (SWE-bench test containers)
```

## Prerequisites

1. **Python 3.11+**
2. **Inspect AI**: `pip install inspect-ai`
3. **GroundTruth**: `pip install -e .` (from repo root)
4. **gt-index binary**: Built from source (requires Go 1.22+ and GCC for CGO)

```bash
cd gt-index
CGO_ENABLED=1 go build -o /tmp/gt-index ./cmd/gt-index/
export GT_INDEX_BINARY=/tmp/gt-index
```

5. **Docker**: Required for SWE-bench sandbox containers
6. **DeepSeek API key**: `export DEEPSEEK_API_KEY=your-key`

## Quick Start

### Run all 30 tasks with GT tools

```bash
inspect eval adapters/inspect/task.py@swebench_gt \
  --model deepseek/deepseek-v4-flash \
  --model-base-url https://api.deepseek.com \
  -T task_ids=null
```

### Run specific tasks

```bash
inspect eval adapters/inspect/task.py@swebench_gt \
  --model deepseek/deepseek-v4-flash \
  --model-base-url https://api.deepseek.com \
  -T 'task_ids=["delgan__loguru-1297","kozea__weasyprint-2300"]'
```

### Run baseline (no GT tools)

```bash
inspect eval adapters/inspect/task.py@swebench_baseline \
  --model deepseek/deepseek-v4-flash \
  --model-base-url https://api.deepseek.com
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `GT_GRAPH_DB` | Path to pre-built graph.db | Auto-built by on_sample_init |
| `GT_INDEX_BINARY` | Path to gt-index binary | `/tmp/gt-index` or on PATH |
| `GT_OUTPUT_DIR` | Directory for GT artifacts | `/tmp/gt_inspect` |
| `DEEPSEEK_API_KEY` | DeepSeek API key | Required |

### Model Configuration

The default configuration targets DeepSeek V4 Flash:
- `temperature=1.0`, `top_p=1.0`
- `max_tokens=65536`
- No thinking/reasoning tokens

Override via Inspect CLI flags:
```bash
inspect eval ... --model-args temperature=0.7,top_p=0.8
```

## Output

### Eval Results

Inspect writes results to `./logs/` by default. View with:

```bash
inspect view
```

### GT Metrics

Per-sample GT metrics are written to `$GT_OUTPUT_DIR/<instance_id>/gt_metrics.json`:

```json
{
  "instance_id": "delgan__loguru-1297",
  "gt_indexed": true,
  "gt_index_time_s": 2.4,
  "total_tool_calls": 47,
  "gt_tool_calls": 8,
  "gt_tool_breakdown": {
    "groundtruth_brief": 3,
    "groundtruth_trace": 2,
    "groundtruth_symbols": 3
  },
  "gt_utilization_rate": 0.170,
  "graph_nodes": 1245,
  "graph_edges": 3891,
  "avg_edge_confidence": 0.812,
  "high_confidence_ratio": 0.645
}
```

## GHA Workflow

The `.github/workflows/inspect_baseline.yml` workflow runs the full 30-task evaluation on push to `inspect_urself` or via manual dispatch. It:

1. Sets up Python, Go, and Docker
2. Builds gt-index from source
3. Runs `inspect eval` with DeepSeek V4 Flash
4. Uploads logs and metrics as artifacts

## Task List

The 30 SWE-bench-Live Lite tasks used for evaluation:

| # | Task ID |
|---|---------|
| 1 | beancount__beancount-931 |
| 2 | beetbox__beets-5495 |
| 3 | delgan__loguru-1297 |
| 4 | delgan__loguru-1306 |
| 5 | flexget__flexget-4306 |
| 6 | flexget__flexget-4244 |
| 7 | kozea__weasyprint-2300 |
| 8 | kozea__weasyprint-2387 |
| 9 | kozea__weasyprint-2405 |
| 10 | kozea__weasyprint-2398 |
| 11 | kozea__weasyprint-2303 |
| 12 | pypsa__pypsa-1172 |
| 13 | pypsa__pypsa-1112 |
| 14 | pypsa__pypsa-1091 |
| 15 | pypsa__pypsa-1195 |
| 16 | aiogram__aiogram-1594 |
| 17 | amoffat__sh-744 |
| 18 | arviz-devs__arviz-2413 |
| 19 | aws-cloudformation__cfn-lint-3875 |
| 20 | aws-cloudformation__cfn-lint-3890 |
| 21 | aws-cloudformation__cfn-lint-3855 |
| 22 | aws-cloudformation__cfn-lint-4023 |
| 23 | dulwich__dulwich-1399 |
| 24 | dulwich__dulwich-1423 |
| 25 | fal-ai__dbt-fal-842 |
| 26 | getmoto__moto-8271 |
| 27 | getmoto__moto-8301 |
| 28 | graphql-python__graphene-1565 |
| 29 | jd__tenacity-482 |
| 30 | pre-commit__pre-commit-3584 |
