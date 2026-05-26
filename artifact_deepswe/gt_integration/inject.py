#!/usr/bin/env python3
"""Standalone GT brief injection module for DeepSWE.

Generates a <gt-task-brief> block from a pre-built graph.db and issue text.
Designed to run on the host machine (not inside containers).

Usage:
    from inject import generate_deepswe_brief
    brief = generate_deepswe_brief(
        instance_id="dateutil-rfc5545-timezone-interop",
        problem_statement="Extend rrule...",
        graph_db="/path/to/graph.db",
        repo_root="/path/to/dateutil",
    )
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Add GT source to path
_GT_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _GT_ROOT.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

logger = logging.getLogger("gt_inject")


def generate_deepswe_brief(
    instance_id: str,
    problem_statement: str,
    graph_db: str,
    repo_root: str,
    *,
    max_files: int = 5,
    max_tokens: int = 600,
) -> str:
    """Generate GT brief for a DeepSWE instance.

    Returns <gt-task-brief>...</gt-task-brief> string, or empty string on failure.
    """
    if not problem_statement.strip():
        return ""

    if not os.path.exists(graph_db):
        logger.warning("No graph.db at %s for %s", graph_db, instance_id)
        return ""

    brief_text = ""

    # Try v22 brief first (latest RRF ranker)
    try:
        brief_text = _try_v22_brief(
            problem_statement, graph_db, repo_root, instance_id,
            max_files=max_files,
        )
    except Exception as e:
        logger.debug("v22 brief failed for %s: %s", instance_id, e)

    # Fallback to v1r brief
    if not brief_text:
        try:
            brief_text = _try_v1r_brief(
                problem_statement, graph_db, repo_root, instance_id,
                max_files=max_files, max_tokens=max_tokens,
            )
        except Exception as e:
            logger.debug("v1r brief failed for %s: %s", instance_id, e)

    if not brief_text or len(brief_text) < 50:
        logger.info("No viable brief for %s (too short or empty)", instance_id)
        return ""

    return brief_text


def _try_v22_brief(
    problem_statement: str,
    graph_db: str,
    repo_root: str,
    instance_id: str,
    *,
    max_files: int = 5,
) -> str:
    """Try generating brief via v22 (v8.2.2 RRF ranker)."""
    from groundtruth.pretask.v22_brief import generate_brief

    result = generate_brief(
        issue_text=problem_statement,
        repo_path=repo_root,
        graph_db_path=graph_db,
    )
    return result or ""


def _try_v1r_brief(
    problem_statement: str,
    graph_db: str,
    repo_root: str,
    instance_id: str,
    *,
    max_files: int = 5,
    max_tokens: int = 600,
) -> str:
    """Try generating brief via v1r (hybrid BM25+graph scorer)."""
    from groundtruth.pretask.v1r_brief import generate_v1r_brief

    result = generate_v1r_brief(
        issue_text=problem_statement,
        repo_root=repo_root,
        graph_db=graph_db,
        bug_id=instance_id,
        max_files=max_files,
        max_brief_tokens=max_tokens,
    )
    if result and result.brief_text:
        return result.brief_text
    return ""


def brief_from_indexes_root(
    instance_id: str,
    problem_statement: str,
    indexes_root: str,
    repo_extracts_root: str = "",
) -> str:
    """Convenience wrapper: find graph.db from indexes_root layout.

    Expected layout: {indexes_root}/{instance_id}/graph.db
    """
    graph_db = os.path.join(indexes_root, instance_id, "graph.db")
    repo_root = repo_extracts_root or os.path.join(indexes_root, instance_id, "repo")

    return generate_deepswe_brief(
        instance_id=instance_id,
        problem_statement=problem_statement,
        graph_db=graph_db,
        repo_root=repo_root,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate GT brief for a DeepSWE task")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--problem-statement", required=True)
    parser.add_argument("--graph-db", required=True)
    parser.add_argument("--repo-root", required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    brief = generate_deepswe_brief(
        instance_id=args.instance_id,
        problem_statement=args.problem_statement,
        graph_db=args.graph_db,
        repo_root=args.repo_root,
    )
    if brief:
        print(brief)
    else:
        print("(no brief generated)", file=sys.stderr)
        sys.exit(1)
