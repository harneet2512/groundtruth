"""Post-edit obligation check — find methods sharing state with edited code.

Runs inside the evaluation container after every Python file edit.
Uses AST parsing only — no graph.db, no external dependencies.

Research: check_v2 endpoint logic (check.py:159-201) adapted for
passive hook delivery. CLAUDE.md items 2+4: Consistency + Completeness
must fire on EVERY edit regardless of graph quality.

Bug 6 fix: only report methods that share state with the EDITED function,
not arbitrary method pairs in the same class.  When --edited-functions is
not supplied, falls back to reading the diff hunk headers from stdin or
reporting all pairs (backward-compatible).

Output format (one line per finding, max 3):
  OBLIGATION: ClassName.method shares attr1, attr2 with edited ClassName.other_method
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys


def _extract_edited_functions_from_diff(diff_text: str) -> set[str]:
    """Extract function names from diff hunk headers (@@ ... @@ def func_name).

    This is a best-effort heuristic: unified diff hunk headers contain the
    enclosing function name for context.  We also look for +def lines.
    """
    names: set[str] = set()
    for line in diff_text.splitlines():
        # Hunk header: @@ -10,5 +10,7 @@ def some_function(self, ...
        m = re.match(r"^@@.*@@\s+(?:async\s+)?def\s+(\w+)", line)
        if m:
            names.add(m.group(1))
        # Added def line
        if line.startswith("+") and not line.startswith("+++"):
            m2 = re.match(r"\+\s*(?:async\s+)?def\s+(\w+)", line)
            if m2:
                names.add(m2.group(1))
    return names


def find_obligations(
    file_path: str,
    workspace: str,
    edited_functions: set[str] | None = None,
) -> list[str]:
    """Find methods that share self.attrs with the edited function(s).

    Args:
        file_path: Relative path inside workspace.
        workspace: Repo root.
        edited_functions: If supplied, only report obligations for methods
            that share attributes with one of these functions.  When None,
            all method pairs are compared (legacy behavior).
    """
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
    seen_pairs: set[tuple[str, str]] = set()

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

        # Bug 6 fix: only iterate over edited methods as method_a.
        # If edited_functions is supplied, restrict to those.  Otherwise
        # compare all pairs (backward-compatible).
        if edited_functions:
            candidate_methods = {
                name: attrs
                for name, attrs in methods.items()
                if name in edited_functions
            }
        else:
            candidate_methods = methods

        for method_a, attrs_a in candidate_methods.items():
            if not attrs_a or method_a == "__init__":
                continue
            for method_b, attrs_b in methods.items():
                if method_b == method_a or method_b == "__init__":
                    continue
                if method_b.startswith("_"):
                    continue
                pair = (min(method_a, method_b), max(method_a, method_b))
                if pair in seen_pairs:
                    continue
                shared = attrs_a & attrs_b
                if len(shared) >= 2:
                    seen_pairs.add(pair)
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
    parser.add_argument(
        "--edited-functions",
        help="Comma-separated list of edited function names",
        default="",
    )
    args = parser.parse_args()

    edited_fns: set[str] | None = None
    if args.edited_functions:
        edited_fns = {f.strip() for f in args.edited_functions.split(",") if f.strip()}

    # Fallback: try to read diff from stdin to auto-detect edited functions
    if not edited_fns and not sys.stdin.isatty():
        try:
            diff_text = sys.stdin.read()
            if diff_text:
                edited_fns = _extract_edited_functions_from_diff(diff_text)
        except Exception:
            pass

    for line in find_obligations(args.file, args.workspace, edited_fns):
        print(line)


if __name__ == "__main__":
    main()
