#!/usr/bin/env python3
"""Build a minimal groundtruth package tar for SWE-bench container injection.

Creates a tar.gz containing only the modules needed by the hook CLI wrappers,
with stripped __init__.py files that avoid importing unavailable modules
(ai/, lsp/, mcp/, etc.).

Output: /tmp/gt_hook_pkg.tar.gz (or specified path)
"""

import io
import os
import sys
import tarfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(REPO_ROOT, "src")

# Files to include (relative to src/)
INCLUDE_FILES = [
    # Package init
    "groundtruth/__init__.py",

    # Hooks (the entry points)
    "groundtruth/hooks/__init__.py",
    "groundtruth/hooks/post_edit.py",
    "groundtruth/hooks/post_view.py",
    "groundtruth/hooks/indexer_cli.py",
    "groundtruth/hooks/logger.py",

    # Validators (obligations + contradictions)
    "groundtruth/validators/__init__.py",
    "groundtruth/validators/obligations.py",
    "groundtruth/validators/contradictions.py",

    # Evidence families (v3)
    "groundtruth/evidence/__init__.py",
    "groundtruth/evidence/change.py",
    "groundtruth/evidence/contract.py",
    "groundtruth/evidence/pattern.py",
    "groundtruth/evidence/structural.py",

    # Analysis (conventions + pattern roles)
    "groundtruth/analysis/__init__.py",
    "groundtruth/analysis/conventions.py",
    "groundtruth/analysis/pattern_roles.py",

    # Policy
    "groundtruth/policy/__init__.py",
    "groundtruth/policy/abstention.py",

    # Index (store, graph, ast_parser, freshness, schema)
    "groundtruth/index/__init__.py",
    "groundtruth/index/store.py",
    "groundtruth/index/graph.py",
    "groundtruth/index/ast_parser.py",
    "groundtruth/index/freshness.py",
    "groundtruth/index/schema.sql",

    # Core (trust, flags)
    "groundtruth/core/__init__.py",
    "groundtruth/core/trust.py",

    # Utils (result, logger, platform)
    "groundtruth/utils/__init__.py",
    "groundtruth/utils/result.py",
    "groundtruth/utils/logger.py",
    "groundtruth/utils/platform.py",

    # Foundation (graph expander)
    "groundtruth/foundation/__init__.py",
    "groundtruth/foundation/graph/__init__.py",
    "groundtruth/foundation/graph/expander.py",
    "groundtruth/foundation/graph/rules.py",

    # Observability (tracer, schema — for structured logging)
    "groundtruth/observability/__init__.py",
    "groundtruth/observability/schema.py",
    "groundtruth/observability/tracer.py",
]

# __init__.py overrides — stripped versions that don't import unavailable modules
INIT_OVERRIDES = {
    "groundtruth/__init__.py": '"""GroundTruth — hook-mode package."""\n__version__ = "0.2.0"\n',
    "groundtruth/evidence/__init__.py": '"""Evidence families."""\n',
    "groundtruth/validators/__init__.py": '"""Validators."""\n',
    "groundtruth/analysis/__init__.py": '"""Analysis modules."""\n',
    "groundtruth/index/__init__.py": '"""Index modules."""\n',
    "groundtruth/core/__init__.py": '"""Core modules."""\n',
    "groundtruth/policy/__init__.py": '"""Policy modules."""\n',
    "groundtruth/foundation/__init__.py": '"""Foundation modules."""\n',
    "groundtruth/foundation/graph/__init__.py": '"""Graph modules."""\n',
    "groundtruth/observability/__init__.py": '"""Observability modules."""\n',
    "groundtruth/utils/__init__.py": '"""Utils."""\nfrom groundtruth.utils.result import Err, GroundTruthError, Ok, Result\n__all__ = ["Ok", "Err", "Result", "GroundTruthError"]\n',
}


def build(output_path: str = "/tmp/gt_hook_pkg.tar.gz") -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for relpath in INCLUDE_FILES:
            arcname = relpath

            # Check for override
            if relpath in INIT_OVERRIDES:
                content = INIT_OVERRIDES[relpath].encode("utf-8")
                info = tarfile.TarInfo(name=arcname)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
                continue

            full_path = os.path.join(SRC_DIR, relpath)
            if os.path.exists(full_path):
                tar.add(full_path, arcname=arcname)
            else:
                print(f"WARNING: {relpath} not found, skipping", file=sys.stderr)

    with open(output_path, "wb") as f:
        f.write(buf.getvalue())

    print(f"Built {output_path}: {len(buf.getvalue()):,} bytes, {len(INCLUDE_FILES)} files")


if __name__ == "__main__":
    output = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gt_hook_pkg.tar.gz"
    build(output)
