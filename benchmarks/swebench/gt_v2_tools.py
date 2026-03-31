"""V2 Pull Architecture — 3 focused tool definitions for OpenAI function calling."""

from __future__ import annotations

GT_V2_PULL_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "gt_locate",
            "description": (
                "Find which files most likely need changes for this issue. "
                "Returns a ranked list of 3-5 files with confidence levels. "
                "Use when you don't know where to start."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_description": {
                        "type": "string",
                        "description": "The issue/bug description to localize.",
                    },
                },
                "required": ["issue_description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gt_context",
            "description": (
                "Get structural context for a file you're about to edit: "
                "callers, interface contracts, sibling patterns, related tests. "
                "Use after you've found the right file but before editing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file (relative to repo root).",
                    },
                    "function_name": {
                        "type": "string",
                        "description": "Specific function name to get context for. Optional.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gt_impact",
            "description": (
                "Check what could break if you change a file/function. "
                "Shows downstream callers, must-pass tests, and related functions. "
                "Use before submitting a patch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file being changed.",
                    },
                    "function_name": {
                        "type": "string",
                        "description": "Specific function name being changed. Optional.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
]
