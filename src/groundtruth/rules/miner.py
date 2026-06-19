"""Codebase rule miner — extracts conventions from sibling files.

Mines: naming conventions, import style, error handling style,
test style, public API conventions, sibling-file patterns.

Deterministic. No LLM. Uses graph.db + source reading.
Shared between OH adapter and MCP product face.
"""
from __future__ import annotations

import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MinedRule:
    category: str  # naming, import, error, test, api, sibling
    pattern: str
    frequency: int = 0
    examples: list[str] = field(default_factory=list)
    file_paths: list[str] = field(default_factory=list)


def mine_naming_conventions(
    graph_db_path: str,
    file_path: str,
) -> list[MinedRule]:
    """Extract naming conventions from sibling functions in the same file/module."""
    if not os.path.exists(graph_db_path):
        return []
    conn = sqlite3.connect(graph_db_path)
    try:
        funcs = conn.execute(
            "SELECT name FROM nodes WHERE file_path = ? AND label IN ('Function','Method')",
            (file_path,),
        ).fetchall()
    finally:
        conn.close()

    names = [f[0] for f in funcs if f[0]]
    if not names:
        return []

    rules: list[MinedRule] = []

    snake = [n for n in names if re.match(r'^[a-z][a-z0-9_]*$', n)]
    camel = [n for n in names if re.match(r'^[a-z][a-zA-Z0-9]*$', n) and any(c.isupper() for c in n)]
    prefix_counts: Counter[str] = Counter()
    for n in names:
        parts = n.split('_')
        if len(parts) >= 2:
            prefix_counts[parts[0]] += 1

    if len(snake) > len(camel) and len(snake) >= 2:
        rules.append(MinedRule(
            category="naming",
            pattern="snake_case",
            frequency=len(snake),
            examples=snake[:3],
            file_paths=[file_path],
        ))
    elif len(camel) > len(snake) and len(camel) >= 2:
        rules.append(MinedRule(
            category="naming",
            pattern="camelCase",
            frequency=len(camel),
            examples=camel[:3],
            file_paths=[file_path],
        ))

    for prefix, count in prefix_counts.most_common(2):
        if count >= 3 and prefix not in ('_', '__'):
            rules.append(MinedRule(
                category="naming",
                pattern=f"prefix_{prefix}_",
                frequency=count,
                examples=[n for n in names if n.startswith(prefix + '_')][:3],
                file_paths=[file_path],
            ))

    return rules


def mine_import_style(
    repo_root: str,
    file_path: str,
) -> list[MinedRule]:
    """Extract import conventions from a file's siblings in same directory."""
    full_path = os.path.join(repo_root, file_path) if repo_root else file_path
    if not os.path.exists(full_path):
        return []

    dir_path = os.path.dirname(full_path)
    if not os.path.isdir(dir_path):
        return []

    py_files = [f for f in os.listdir(dir_path) if f.endswith('.py') and not f.startswith('_')][:10]

    from_imports = 0
    direct_imports = 0
    for pf in py_files:
        try:
            with open(os.path.join(dir_path, pf), 'r', errors='replace') as fh:
                for line in fh:
                    if line.strip().startswith('from '):
                        from_imports += 1
                    elif line.strip().startswith('import '):
                        direct_imports += 1
                    if from_imports + direct_imports > 50:
                        break
        except OSError:
            continue

    rules: list[MinedRule] = []
    total = from_imports + direct_imports
    if total >= 5:
        if from_imports > direct_imports * 2:
            rules.append(MinedRule(
                category="import",
                pattern="from_import_preferred",
                frequency=from_imports,
                file_paths=[file_path],
            ))
        elif direct_imports > from_imports * 2:
            rules.append(MinedRule(
                category="import",
                pattern="direct_import_preferred",
                frequency=direct_imports,
                file_paths=[file_path],
            ))
    return rules


def mine_rules_for_file(
    graph_db_path: str,
    repo_root: str,
    file_path: str,
) -> list[MinedRule]:
    """Mine all conventions relevant to a file."""
    rules: list[MinedRule] = []
    rules.extend(mine_naming_conventions(graph_db_path, file_path))
    rules.extend(mine_import_style(repo_root, file_path))
    return rules


def render_rules(rules: list[MinedRule], max_chars: int = 500) -> str:
    """Render mined rules as compact text for agent consumption."""
    if not rules:
        return ""
    parts = ["[PATTERN] Conventions in this area:"]
    for r in rules[:5]:
        examples = ", ".join(r.examples[:2]) if r.examples else ""
        line = f"  {r.category}: {r.pattern} ({r.frequency}x)"
        if examples:
            line += f" e.g. {examples}"
        parts.append(line)
    rendered = "\n".join(parts)
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars - 3] + "..."
    return rendered
