"""Post-edit obligation check — find methods sharing state with edited code.

Runs inside the evaluation container after every Python file edit.
Uses AST parsing only — no graph.db, no external dependencies.

Research: check_v2 endpoint logic (check.py:159-201) adapted for
passive hook delivery. CLAUDE.md items 2+4: Consistency + Completeness
must fire on EVERY edit regardless of graph quality.

Output format (one line per finding, max 3):
  OBLIGATION: ClassName.method shares attr1, attr2 with edited ClassName.other_method
"""

from __future__ import annotations

import argparse
import ast
import os


def find_obligations(file_path: str, workspace: str) -> list[str]:
    """Find methods that share self.attrs with other methods in the same class."""
    full_path = os.path.join(workspace, file_path)
    if not os.path.isfile(full_path):
        return []

    try:
        with open(full_path, encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=file_path)
    except (SyntaxError, ValueError, OSError):
        return []

    results: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        methods: dict[str, set[str]] = {}
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            attrs: set[str] = set()
            for sub in ast.walk(item):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self"
                ):
                    attrs.add(sub.attr)
            methods[item.name] = attrs

        for method_a, attrs_a in methods.items():
            if not attrs_a:
                continue
            for method_b, attrs_b in methods.items():
                if method_b == method_a:
                    continue
                if method_b.startswith("_") and method_b != "__init__":
                    continue
                shared = attrs_a & attrs_b
                if len(shared) >= 2:
                    results.append(
                        f"OBLIGATION: {node.name}.{method_b} shares "
                        f"{', '.join(sorted(shared)[:3])} with "
                        f"{node.name}.{method_a}"
                    )

    return results[:3]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()

    for line in find_obligations(args.file, args.workspace):
        print(line)


if __name__ == "__main__":
    main()
