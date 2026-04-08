"""Post-view hook — structural coupling enrichment for file reads.

Called by OpenHands PostToolUse hook on file_editor view operations.
Composes: PatternRoleClassifier + shared-state coupling detection.
Outputs 0-5 compact structural notes to stdout.

Usage:
    python -m groundtruth.hooks.post_view --root=/testbed --db=/tmp/gt_index.db --file=<path>
"""

from __future__ import annotations

import argparse
import ast
import os
import time
from collections import defaultdict

from groundtruth.hooks.logger import log_hook


def _read_file(root: str, relpath: str) -> str:
    try:
        path = relpath if os.path.isabs(relpath) else os.path.join(root, relpath)
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _is_test_file(filepath: str) -> bool:
    fp = "/" + filepath.lower().replace("\\", "/")
    return any(p in fp for p in ["/tests/", "/test/", "/testing/", "/fixtures/"])


def _classify_role(method_name: str, method_node: ast.FunctionDef) -> str:
    """Classify a method's role based on AST patterns."""
    if method_name == "__init__":
        return "stores"
    # Check for Store context on self.attrs
    written = set()
    for child in ast.walk(method_node):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == "self"
            and isinstance(child.ctx, ast.Store)
        ):
            written.add(child.attr)
    if len(written) >= 2:
        return "stores"

    serialize_names = ("deconstruct", "serialize", "to_dict", "as_dict", "get_params")
    if any(s in method_name.lower() for s in serialize_names):
        return "serializes"

    if method_name in ("__eq__", "__ne__", "__hash__", "__lt__", "__le__", "__gt__", "__ge__"):
        return "compares"

    validate_names = ("validate", "check", "clean", "verify")
    if any(s in method_name.lower() for s in validate_names):
        return "validates"

    for child in ast.walk(method_node):
        if isinstance(child, ast.Raise):
            return "validates"

    return "reads"


def _get_role_label(role: str) -> str:
    return {
        "stores": "stores",
        "serializes": "serializes to kwargs",
        "compares": "compares",
        "validates": "checks",
        "reads": "reads",
    }.get(role, role)


def main() -> None:
    parser = argparse.ArgumentParser(description="GT post-view enrichment hook")
    parser.add_argument("--root", default="/testbed")
    parser.add_argument("--db", default="/tmp/gt_index.db")
    parser.add_argument("--file", required=True, help="File path to enrich")
    args = parser.parse_args()

    start = time.time()
    log_entry = {
        "hook": "post_view",
        "endpoint": "understand",
        "file": args.file,
        "classes_found": 0,
        "coupled_classes": 0,
    }

    filepath = args.file
    if _is_test_file(filepath):
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_hook(log_entry)
        return

    source = _read_file(args.root, filepath)
    if not source:
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_hook(log_entry)
        return

    # AST analysis is Python-only; skip non-Python files gracefully
    if not filepath.endswith(".py"):  # Python fallback
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_hook(log_entry)
        return

    try:
        tree = ast.parse(source)
    except SyntaxError:
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_hook(log_entry)
        return

    output_lines = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        log_entry["classes_found"] += 1

        # Collect method info
        method_infos = {}
        method_nodes = {}
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            attrs = set()
            for child in ast.walk(item):
                if (
                    isinstance(child, ast.Attribute)
                    and isinstance(child.value, ast.Name)
                    and child.value.id == "self"
                ):
                    attrs.add(child.attr)
            method_infos[item.name] = attrs
            method_nodes[item.name] = item

        if len(method_infos) < 3:
            continue

        # Find attrs shared across >=3 methods
        attr_counts = defaultdict(int)
        for attrs in method_infos.values():
            for attr in attrs:
                attr_counts[attr] += 1
        shared_attrs = sorted(a for a, c in attr_counts.items() if c >= 3)

        if len(shared_attrs) < 2:
            continue

        log_entry["coupled_classes"] += 1

        # Classify roles and build chain
        chain = []
        for mname, mnode in sorted(method_nodes.items(), key=lambda x: x[1].lineno):
            if len(method_infos[mname] & set(shared_attrs)) < 2:
                continue
            role = _classify_role(mname, mnode)
            chain.append((mname, mnode.lineno, role))

        if len(chain) < 2:
            continue

        # Check space before adding
        if len(output_lines) + 2 > 5:
            break

        shared_str = ", ".join(f"self.{a}" for a in shared_attrs[:4])
        if len(shared_attrs) > 4:
            shared_str += f", +{len(shared_attrs) - 4} more"

        output_lines.append("-- structural coupling --")
        output_lines.append(f"{node.name}: {len(chain)} methods share {shared_str}")
        chain_parts = [f"{m}:{ln} ({_get_role_label(r)})" for m, ln, r in chain[:6]]
        output_lines.append("  " + " -> ".join(chain_parts))

        # Actionable rule
        stores = [m for m, _, r in chain if r == "stores"]
        targets = [m for m, _, r in chain if r in ("serializes", "compares", "validates")]
        if stores and targets:
            output_lines.append(
                f"  Rule: changes to {stores[0]} params must appear in {' and '.join(targets[:3])}"
            )

        if len(output_lines) >= 5:
            break

    final = output_lines[:5]
    if final:
        print("\n".join(final))

    log_entry["output_lines"] = len(final)
    log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
    log_hook(log_entry)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
