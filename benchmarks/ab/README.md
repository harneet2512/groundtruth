# A/B Benchmark Harness

Single entrypoint for a controlled A/B benchmark where the **only** independent variable is GroundTruth MCP availability and actual use.

## Conditions

- **no_mcp** — Same task set and evaluation, run in-process with no MCP server. No GroundTruth tools available.
- **with_groundtruth_mcp** — Same task set; harness spawns the real GroundTruth MCP server, connects as an MCP client, runs the same tasks via tool calls, and **proves** tool usage (connection, tools discovered, substantive tool calls).

## Reproducible commands

From the **project root**:

```bash
# No MCP (in-process)
python -m benchmarks.ab.harness --condition no_mcp [--fixture all|python|typescript|go]

# With GroundTruth MCP (spawn server, connect client, prove tool use)
python -m benchmarks.ab.harness --condition with_groundtruth_mcp [--fixture all|python|typescript|go]

# Both conditions (for comparison)
python -m benchmarks.ab.harness --condition both [--fixture all]

# Custom output directory
python -m benchmarks.ab.harness --condition no_mcp --output-dir /path/to/results
```

Default output directory: `benchmarks/ab/results/`. Each run writes `<condition>.json`.

## Verifying MCP proof

For **with_groundtruth_mcp**, the run is valid only if the report includes proof that the MCP server was actually used:

1. **connection_ok** — Client connected and initialized with the server.
2. **tools_discovered** — Non-empty list of tool names (e.g. `groundtruth_validate`, `groundtruth_find_relevant`, …).
3. **substantive_tool_count** — At least one call to a substantive tool (not just `groundtruth_status`).
4. **valid** — `true` when `connection_ok` and `substantive_tool_count >= 1`.

In the JSON report, see `metadata.mcp_proof`:

```json
"mcp_proof": {
  "mcp_enabled": true,
  "connection_ok": true,
  "tools_discovered": ["groundtruth_validate", "groundtruth_find_relevant", ...],
  "tool_calls": [{"name": "groundtruth_validate", "success": true}, ...],
  "substantive_tool_count": 15,
  "valid": true
}
```

If **valid** is `false`, the harness exits with code 1 and prints a warning. A run with MCP “configured” but never used would have `connection_ok=false` or `substantive_tool_count=0`, and would not be considered a valid MCP-enabled run.

## Fair comparison

- Same task set (hallucination + file relevance cases from `benchmarks/`).
- Same evaluation logic (detection, fix rate, precision/recall).
- Same fixture filter and output schema.
- Only variable: MCP server availability and actual tool use (proven in the MCP arm).

## Requirements

- Project installed (e.g. `pip install -e ".[dev]"`).
- For **with_groundtruth_mcp**: MCP SDK (anyio) is a dependency of `mcp`; no extra install.
