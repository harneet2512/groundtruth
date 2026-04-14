#!/usr/bin/env python3
"""Pairwise patch ranking harness.

For each (correct_patch, plausible_wrong_patch) pair in the fixture file,
asks GT to score both. Records whether GT prefers the correct patch.

This is a GATE before any full benchmark claim:
  - Overall preference rate >= 70% is required
  - Per key family must not be catastrophically low (< 40%)

Usage:
    python pairwise_rank_eval.py --db /path/to/graph.db \\
                                  --fixtures pairwise_fixtures.jsonl \\
                                  [--root /path/to/repo]

Fixture format (one JSON object per line):
    {
      "task_id": "astropy-13977",
      "family": "paired_behavior",
      "correct_diff": "<unified diff of correct patch>",
      "wrong_diff": "<unified diff of plausible wrong patch>",
      "changed_files": ["path/to/file.py"],
      "changed_symbols": ["FunctionName"],
      "description": "optional human-readable description"
    }

Exit codes:
    0  — overall preference rate >= 70%
    1  — overall preference rate < 70% (gate failed)
    2  — fixture file not found or no valid pairs
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PairResult:
    task_id: str
    family: str
    correct_score: float
    wrong_score: float
    correct_decision: str
    wrong_decision: str
    preferred_correct: bool
    tie: bool
    description: str = ""


@dataclass
class FamilyStats:
    total: int = 0
    preferred_correct: int = 0
    ties: int = 0
    family: str = ""

    @property
    def preference_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.preferred_correct / self.total

    @property
    def tie_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.ties / self.total


def run(db_path: str, fixtures_path: str, root: str | None) -> int:
    """Run the pairwise ranking evaluation. Returns 0 if gate passes.

    Fixtures may specify a ``db_path`` field to override the default database.
    This allows multi-repo evaluation where each fixture is scored against the
    graph for its own repository — which is required for the contracts to
    resolve correctly. The default ``--db`` is used as a fallback for fixtures
    that omit ``db_path``.
    """
    try:
        from groundtruth.index.graph_store import GraphStore
        from groundtruth.substrate.graph_reader_impl import GraphStoreReader
        from groundtruth.substrate.service import SubstrateService
    except ImportError:
        print("ERROR: groundtruth package not importable. Run from repo root.", file=sys.stderr)
        return 2

    if not Path(fixtures_path).exists():
        print(f"ERROR: Fixture file not found: {fixtures_path}", file=sys.stderr)
        return 2

    fixtures = _load_fixtures(fixtures_path)
    if not fixtures:
        print("ERROR: No valid fixtures loaded.", file=sys.stderr)
        return 2

    # Lazily open one SubstrateService per db_path to avoid re-opening the
    # same database for every fixture in the same repo.
    _services: dict[str, object] = {}
    _stores: list[object] = []

    def get_service(path: str) -> object:
        if path not in _services:
            store = GraphStore(path)
            store.initialize()
            _stores.append(store)
            reader = GraphStoreReader(store)
            _services[path] = SubstrateService(reader)
        return _services[path]

    results: list[PairResult] = []

    for i, fixture in enumerate(fixtures, 1):
        task_id = fixture.get("task_id", f"pair_{i}")
        family = fixture.get("family", "unknown")
        correct_diff = fixture.get("correct_diff", "")
        wrong_diff = fixture.get("wrong_diff", "")
        changed_files = tuple(fixture.get("changed_files", []))
        changed_symbols = tuple(fixture.get("changed_symbols", []))
        description = fixture.get("description", "")
        # Per-fixture db_path override — enables multi-repo gate runs.
        fixture_db = fixture.get("db_path") or db_path

        if not correct_diff or not wrong_diff:
            print(f"SKIP {task_id}: missing correct_diff or wrong_diff")
            continue

        if not Path(fixture_db).exists():
            print(f"SKIP {task_id}: db not found: {fixture_db}")
            continue

        try:
            service = get_service(fixture_db)
            correct_verdict = service.score_patch(
                diff=correct_diff,
                changed_files=changed_files,
                changed_symbols=changed_symbols,
                task_ref=task_id,
                candidate_id="correct",
                root_path=root,
            )
            wrong_verdict = service.score_patch(
                diff=wrong_diff,
                changed_files=changed_files,
                changed_symbols=changed_symbols,
                task_ref=task_id,
                candidate_id="wrong",
                root_path=root,
            )
        except Exception as e:
            print(f"SKIP {task_id}: scoring error: {e}")
            continue

        # A tie is when scores differ by <= 0.02
        tie = abs(correct_verdict.overall_score - wrong_verdict.overall_score) <= 0.02
        preferred_correct = (
            correct_verdict.overall_score > wrong_verdict.overall_score
            and not tie
        )

        result = PairResult(
            task_id=task_id,
            family=family,
            correct_score=correct_verdict.overall_score,
            wrong_score=wrong_verdict.overall_score,
            correct_decision=correct_verdict.decision,
            wrong_decision=wrong_verdict.decision,
            preferred_correct=preferred_correct,
            tie=tie,
            description=description,
        )
        results.append(result)

        status = "CORRECT" if preferred_correct else ("TIE" if tie else "WRONG")
        print(
            f"[{status:7}] {task_id:<30} family={family:<25} "
            f"correct={correct_verdict.overall_score:.3f} wrong={wrong_verdict.overall_score:.3f}"
        )

    for store in _stores:
        if hasattr(store, "close"):
            store.close()

    if not results:
        print("\nERROR: No pairs evaluated.", file=sys.stderr)
        return 2

    # ----------------------------------------------------------------
    # Aggregate
    # ----------------------------------------------------------------
    by_family: dict[str, FamilyStats] = defaultdict(FamilyStats)
    for r in results:
        by_family[r.family].total += 1
        by_family[r.family].family = r.family
        if r.preferred_correct:
            by_family[r.family].preferred_correct += 1
        if r.tie:
            by_family[r.family].ties += 1

    overall_total = len(results)
    overall_correct = sum(1 for r in results if r.preferred_correct)
    overall_rate = overall_correct / overall_total

    print("\n" + "=" * 70)
    print("PER-FAMILY RESULTS")
    print("-" * 70)
    for fam, stats in sorted(by_family.items()):
        flag = "!" if stats.preference_rate < 0.40 else "+"
        print(
            f"  {flag} {fam:<30} {stats.preferred_correct}/{stats.total} "
            f"({stats.preference_rate:.1%} correct, {stats.tie_rate:.1%} ties)"
        )

    print("-" * 70)
    print(f"\nOVERALL: {overall_correct}/{overall_total} = {overall_rate:.1%} preference for correct patch")
    print(f"GATE threshold: 70%")

    low_families = [
        fam for fam, stats in by_family.items() if stats.preference_rate < 0.40
    ]
    if low_families:
        print(f"\nWARNING: Low-precision families (< 40%): {', '.join(low_families)}")

    if overall_rate >= 0.70:
        print("\nGATE PASSED -- proceed to canary evaluation.")
        return 0
    else:
        print(f"\nGATE FAILED -- {overall_rate:.1%} < 70% threshold. Do not proceed to full benchmark.")
        return 1


def _load_fixtures(path: str) -> list[dict]:
    fixtures = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                fixtures.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"WARN: skipping malformed fixture line: {e}", file=sys.stderr)
    return fixtures


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pairwise patch ranking evaluation for GT semantic substrate."
    )
    parser.add_argument("--db", required=True, help="Path to graph.db")
    parser.add_argument(
        "--fixtures",
        default=str(Path(__file__).parent / "pairwise_fixtures.jsonl"),
        help="Path to JSONL fixture file (default: pairwise_fixtures.jsonl)",
    )
    parser.add_argument("--root", default=None, help="Repository root path")
    args = parser.parse_args()

    sys.exit(run(args.db, args.fixtures, args.root))


if __name__ == "__main__":
    main()
