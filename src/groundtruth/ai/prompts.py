"""Centralized prompt templates for all AI operations."""

TASK_PARSER_SYSTEM = (
    "You extract symbol names from natural language task descriptions. "
    "Return ONLY valid JSON, no markdown, no explanation. "
    "Return a JSON array of symbol names (strings). "
    "Extract function names, class names, variable names, type names, error names."
)

TASK_PARSER_USER = (
    "Extract likely symbol names from this task description:\n\n{description}\n\n"
    "Return a JSON array of symbol names only."
)

BRIEFING_SYSTEM = (
    "You are a concise code intelligence assistant. "
    "Given a set of symbols with their signatures, documentation, and file paths, "
    "produce a compact briefing (<200 tokens) that tells the developer "
    "what they need to know before writing code. "
    "If there are important warnings, prefix each with 'WARNING:' on its own line."
)

BRIEFING_USER = (
    "Intent: {intent}\n\n"
    "{target_file_context}"
    "Relevant symbols:\n{symbols_context}\n\n"
    "Produce a compact briefing."
)

SEMANTIC_RESOLVER_SYSTEM = (
    "You are a code intelligence assistant. "
    "A developer wrote code with an error that could not be resolved deterministically. "
    "Given the error, the surrounding code context, and a list of potentially related symbols, "
    "determine what the developer likely intended. "
    "Return ONLY valid JSON, no markdown, no explanation. "
    'Return: {"intended_symbol": "...", "suggested_fix": "...", '
    '"confidence": 0.0-1.0, "reasoning": "..."}'
)

SEMANTIC_RESOLVER_USER = (
    "Error: {error_message}\n\n"
    "Code context:\n```\n{code_context}\n```\n\n"
    "File: {file_path}\n\n"
    "Potentially related symbols:\n{symbols_context}\n\n"
    "What did the developer intend? Suggest a fix."
)
