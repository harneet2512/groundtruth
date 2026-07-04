"""Tool definitions for the SWE-bench agent (OpenAI function calling format)."""

from __future__ import annotations

# Base tools available to ALL agents (baseline and GT)
BASE_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command in the repository's Docker container. Use for running tests, installing dependencies, checking file structure, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_file",
            "description": "Read the contents of a file. Returns numbered lines. Use start_line/end_line to read specific sections of large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to show (1-indexed). Optional.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to show (1-indexed). Optional.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a specific string in a file with a new string. The old_str must appear exactly once in the file. Use for making targeted code changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to edit.",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "The exact string to find and replace. Must be unique in the file.",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "The replacement string.",
                    },
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search for a pattern in files using grep. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The search pattern (supports regex).",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in. Defaults to repo root.",
                    },
                    "include": {
                        "type": "string",
                        "description": "File glob pattern to include, e.g. '*.py'.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_patch",
            "description": "Submit your changes as the final patch. Call this when you believe the issue is resolved and tests pass. After calling this, no more actions are possible.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


# GroundTruth tools — dynamically generated from bridge
def make_gt_tool_def(name: str, description: str, parameters: dict) -> dict:
    """Create an OpenAI function-calling tool definition for a GroundTruth tool."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


# The 15 GroundTruth tools in OpenAI function-calling format.
# These mirror the MCP tool schemas from server.py.
GROUNDTRUTH_TOOLS: list[dict] = [
    make_gt_tool_def(
        "groundtruth_orient",
        "Get a complete orientation of the codebase: project structure, entry points, build commands, risk areas. ALWAYS call this first.",
        {"type": "object", "properties": {}, "required": []},
    ),
    make_gt_tool_def(
        "groundtruth_find_relevant",
        "Find files relevant to a task description. Returns ranked list of files with relevance scores.",
        {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Natural language description of the task."},
                "max_files": {"type": "integer", "description": "Maximum files to return. Default 10."},
            },
            "required": ["description"],
        },
    ),
    make_gt_tool_def(
        "groundtruth_brief",
        "Get a proactive briefing before writing code. Provides symbols, imports, patterns relevant to your intent.",
        {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "description": "What you plan to do."},
                "target_file": {"type": "string", "description": "The file you plan to modify."},
            },
            "required": ["intent"],
        },
    ),
    make_gt_tool_def(
        "groundtruth_validate",
        "Validate proposed code against the codebase index. Checks imports, symbols, signatures, packages.",
        {
            "type": "object",
            "properties": {
                "proposed_code": {"type": "string", "description": "The code to validate."},
                "file_path": {"type": "string", "description": "Path where this code will be placed."},
                "language": {"type": "string", "description": "Programming language. Default: auto-detect."},
            },
            "required": ["proposed_code", "file_path"],
        },
    ),
    make_gt_tool_def(
        "groundtruth_explain",
        "Deep dive into a symbol: source code, dependency chain, callers, callees, side effects, complexity.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name to explain."},
                "file_path": {"type": "string", "description": "Narrow search to this file. Optional."},
            },
            "required": ["symbol"],
        },
    ),
    make_gt_tool_def(
        "groundtruth_impact",
        "Assess the blast radius of modifying a symbol. Shows all files and functions affected.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name to check impact for."},
                "max_depth": {"type": "integer", "description": "Max graph traversal depth. Default 3."},
            },
            "required": ["symbol"],
        },
    ),
    make_gt_tool_def(
        "groundtruth_patterns",
        "Detect coding conventions in sibling files: naming, imports, error handling, docstrings.",
        {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "File whose sibling patterns to analyze."},
            },
            "required": ["file_path"],
        },
    ),
    make_gt_tool_def(
        "groundtruth_trace",
        "Trace a symbol through the codebase — follow its references and call chain.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name to trace."},
                "direction": {"type": "string", "description": "'callers', 'callees', or 'both'. Default 'both'."},
                "max_depth": {"type": "integer", "description": "Max traversal depth. Default 3."},
            },
            "required": ["symbol"],
        },
    ),
    make_gt_tool_def(
        "groundtruth_symbols",
        "List all symbols defined in a file with their types, line numbers, and signatures.",
        {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file."},
            },
            "required": ["file_path"],
        },
    ),
    make_gt_tool_def(
        "groundtruth_context",
        "Show usage context for a symbol: where it's imported, how it's called, related symbols.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name."},
                "limit": {"type": "integer", "description": "Max references to return. Default 10."},
            },
            "required": ["symbol"],
        },
    ),
    make_gt_tool_def(
        "groundtruth_status",
        "Health check: index stats, symbols/files/refs counts, last indexing time.",
        {"type": "object", "properties": {}, "required": []},
    ),
    make_gt_tool_def(
        "groundtruth_checkpoint",
        "Session progress summary: tools called, validations run, errors found, risk level.",
        {"type": "object", "properties": {}, "required": []},
    ),
    make_gt_tool_def(
        "groundtruth_dead_code",
        "Find exported symbols with zero references anywhere in the codebase.",
        {"type": "object", "properties": {}, "required": []},
    ),
    make_gt_tool_def(
        "groundtruth_unused_packages",
        "Find installed packages that no file imports.",
        {"type": "object", "properties": {}, "required": []},
    ),
    make_gt_tool_def(
        "groundtruth_hotspots",
        "Most-referenced symbols in the codebase — the critical nodes.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of hotspots to return. Default 20."},
            },
            "required": [],
        },
    ),
]
