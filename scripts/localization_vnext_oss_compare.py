#!/usr/bin/env python3
"""GitHub-sharded, gold-sealed OSS comparison for localization vNext.

The ``prepare`` phase strips every gold field before a case manifest can enter
the sealing container.  ``seal`` indexes pinned repositories and runs legacy
and vNext localization without a gold-bearing input.  Only the separate
``score`` phase reads the original manifest and joins gold to already-sealed
artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


def _repository_root_for_script(
    script_path: Path,
    *,
    cwd: Path | None = None,
) -> Path:
    parent = script_path.parent
    if parent.name == "scripts":
        return parent.parent
    return cwd or Path.cwd()


REPO_ROOT = _repository_root_for_script(Path(__file__))
GT_SRC = Path(os.environ.get("GT_SRC", str(REPO_ROOT / "src")))
sys.path.insert(0, str(GT_SRC))

from groundtruth.pretask.localization_vnext.comparison import (  # noqa: E402
    evaluate_winner,
    run_sealed_case,
    score_sealed_case,
)

_INPUT_KEYS = (
    "id",
    "issue_text",
    "repo",
    "revision_identity",
    "language",
    "split",
)
_GOLD_PROVENANCE_KEYS = (
    "fix_commit",
    "fix_commit_sha256",
    "patch_sha256",
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _json_ready(value: Any) -> Any:
    if isinstance(value, float):
        return f"{value:.8f}"
    if isinstance(value, dict):
        return {
            str(key): _json_ready(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            _json_ready(payload),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _infer_split(case_id: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    if case_id.startswith("ext2_"):
        return "ext2"
    if case_id.startswith("held_"):
        return "held"
    if case_id.startswith(("rnd_", "random_")):
        return "random"
    return "unknown"


def prepare_shard(
    cases: Sequence[Mapping[str, Any]],
    repositories: Mapping[str, Mapping[str, Any]],
    *,
    language: str,
    shard_index: int,
    shard_count: int,
) -> list[dict[str, str]]:
    """Return a deterministic, gold-free shard manifest."""
    if shard_count <= 0:
        raise ValueError("shard_count must be positive")
    if not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be within shard_count")
    selected = sorted(
        (
            case
            for case in cases
            if str(case.get("language") or "").lower() == language.lower()
        ),
        key=lambda case: str(case.get("id") or ""),
    )
    output: list[dict[str, str]] = []
    for index, case in enumerate(selected):
        if index % shard_count != shard_index:
            continue
        case_id = str(case["id"])
        repo_name = str(case["repo"])
        repo = repositories.get(repo_name)
        if repo is None:
            raise ValueError(f"repository metadata missing for {repo_name}")
        output.append(
            {
                "id": case_id,
                "issue_text": str(case.get("issue_text") or ""),
                "repo": repo_name,
                "revision_identity": str(repo.get("commit") or ""),
                "language": str(case.get("language") or "unknown").lower(),
                "split": _infer_split(
                    case_id,
                    str(case.get("split") or ""),
                ),
            }
        )
    return output


def validate_sealed_case_ids(
    expected: set[str],
    actual: Sequence[str],
) -> dict[str, Any]:
    counts = Counter(actual)
    actual_set = set(actual)
    missing = sorted(expected - actual_set)
    duplicates = sorted(
        case_id for case_id, count in counts.items() if count > 1
    )
    extra = sorted(actual_set - expected)
    return {
        "complete": not missing and not duplicates and not extra,
        "missing": missing,
        "duplicates": duplicates,
        "extra": extra,
    }


def _safe_repo_path(root: Path, repo_name: str) -> Path:
    if not repo_name or Path(repo_name).name != repo_name:
        raise ValueError(f"unsafe repository name: {repo_name!r}")
    root = root.resolve()
    target = (root / repo_name).resolve()
    target.relative_to(root)
    return target


def _remove_ephemeral_repo(root: Path, target: Path) -> None:
    target.resolve().relative_to(root.resolve())
    if target.exists():
        shutil.rmtree(target)


def clone_repositories(
    prepared_cases: Sequence[Mapping[str, Any]],
    repositories: Mapping[str, Mapping[str, Any]],
    destination: Path,
) -> None:
    """Clone only repositories required by one shard at exact pinned commits."""
    destination.mkdir(parents=True, exist_ok=True)
    for repo_name in sorted({str(case["repo"]) for case in prepared_cases}):
        metadata = repositories.get(repo_name)
        if metadata is None:
            raise RuntimeError(f"repository metadata missing for {repo_name}")
        url = str(metadata.get("url") or "")
        commit = str(metadata.get("commit") or "")
        if not url or not commit:
            raise RuntimeError(f"incomplete repository metadata for {repo_name}")
        target = _safe_repo_path(destination, repo_name)
        _remove_ephemeral_repo(destination, target)
        subprocess.run(
            ["git", "init", "--quiet", str(target)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "remote", "add", "origin", url],
            check=True,
        )
        fetched = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "fetch",
                "--quiet",
                "--depth",
                "1",
                "origin",
                commit,
            ],
            check=False,
        )
        if fetched.returncode != 0:
            _remove_ephemeral_repo(destination, target)
            subprocess.run(
                ["git", "clone", "--quiet", "--no-checkout", url, str(target)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(target), "fetch", "--quiet", "origin", commit],
                check=True,
            )
        subprocess.run(
            ["git", "-C", str(target), "checkout", "--quiet", commit],
            check=True,
        )
        head = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if head != commit:
            raise RuntimeError(
                f"{repo_name} HEAD {head[:12]} != pinned {commit[:12]}"
            )


def _index_repository(repo_root: Path, graph_db: Path) -> dict[str, Any]:
    graph_db.parent.mkdir(parents=True, exist_ok=True)
    command = [
        os.environ.get("GT_INDEX_BIN", "gt-index"),
        "-root",
        str(repo_root),
        "-output",
        str(graph_db),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("GT_LOC_INDEX_TIMEOUT", "900")),
    )
    if completed.returncode != 0 or not graph_db.is_file():
        raise RuntimeError(
            "index_failed "
            f"exit={completed.returncode} "
            f"stderr={completed.stderr[-1000:]}"
        )
    final_line = (
        completed.stdout.strip().splitlines()[-1]
        if completed.stdout.strip()
        else "{}"
    )
    try:
        return json.loads(final_line)
    except json.JSONDecodeError:
        return {"stdout_tail": completed.stdout[-1000:]}


class _CaseTimeout(RuntimeError):
    pass


def _alarm_handler(_signum: int, _frame: Any) -> None:
    raise _CaseTimeout("case_timeout")


def seal_shard(
    prepared_cases: Sequence[Mapping[str, Any]],
    *,
    repositories_root: Path,
    graphs_root: Path,
    output_root: Path,
    repeats: int,
) -> dict[str, Any]:
    """Index and seal a gold-free shard inside the substrate container."""
    from groundtruth.pretask.graph_localizer import _get_embedder

    require_embedder = os.environ.get("GT_REQUIRE_EMBEDDER") == "1"
    embedder_loaded = _get_embedder() is not None
    if require_embedder and not embedder_loaded:
        raise RuntimeError("required frozen semantic embedder did not load")

    graph_by_repo: dict[str, Path] = {}
    index_metrics: dict[str, Any] = {}
    for repo_name in sorted({str(case["repo"]) for case in prepared_cases}):
        repo_root = _safe_repo_path(repositories_root, repo_name)
        graph_db = graphs_root / repo_name / "graph.db"
        index_metrics[repo_name] = _index_repository(repo_root, graph_db)
        graph_by_repo[repo_name] = graph_db

    timeout_seconds = int(os.environ.get("GT_LOC_CASE_TIMEOUT", "1200"))
    alarm_signal = getattr(signal, "SIGALRM", None)
    alarm = getattr(signal, "alarm", None)
    if alarm_signal is not None and callable(alarm):
        signal.signal(alarm_signal, _alarm_handler)
    else:
        alarm = None
    sealed_ids: list[str] = []
    failures: list[dict[str, str]] = []
    for case in prepared_cases:
        case_id = str(case["id"])
        repo_name = str(case["repo"])
        engine_input = {
            key: value
            for key, value in {
                "id": case_id,
                "issue_text": str(case.get("issue_text") or ""),
                "repository_root": str(
                    _safe_repo_path(repositories_root, repo_name)
                ),
                "graph_db": str(graph_by_repo[repo_name]),
                "revision_identity": str(
                    case.get("revision_identity") or "unknown"
                ),
                "language": str(case.get("language") or "unknown"),
                "split": str(case.get("split") or "unknown"),
            }.items()
        }
        try:
            if timeout_seconds > 0 and alarm is not None:
                alarm(timeout_seconds)
            sealed = run_sealed_case(
                engine_input,
                repeats=max(3, repeats),
            )
            _write_json(
                output_root / "sealed" / f"{case_id}.json",
                sealed,
            )
            sealed_ids.append(case_id)
        except Exception as exc:
            failures.append(
                {
                    "id": case_id,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            if alarm is not None:
                alarm(0)

    summary = {
        "schema": "gt.localization.vnext.github.seal.v1",
        "expected_case_ids": sorted(str(case["id"]) for case in prepared_cases),
        "sealed_case_ids": sorted(sealed_ids),
        "failures": failures,
        "index_metrics": index_metrics,
        "embedder_loaded": embedder_loaded,
        "thread_settings": {
            key: os.environ.get(key, "")
            for key in (
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
                "TF_ENABLE_ONEDNN_OPTS",
            )
        },
    }
    _write_json(output_root / "SEAL_SUMMARY.json", summary)
    return summary


def score_sealed_artifacts(
    cases: Sequence[Mapping[str, Any]],
    *,
    sealed_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Join gold only after all shard artifacts have already been sealed."""
    expected = {str(case["id"]) for case in cases}
    sealed_paths = sorted(sealed_root.glob("**/sealed/*.json"))
    sealed_rows = [_read_json(path) for path in sealed_paths]
    actual_ids = [str(row["case"]["id"]) for row in sealed_rows]
    completeness = validate_sealed_case_ids(expected, actual_ids)
    gold_by_id = {str(case["id"]): dict(case) for case in cases}

    paired: list[dict[str, Any]] = []
    for sealed in sealed_rows:
        case_id = str(sealed["case"]["id"])
        gold = gold_by_id.get(case_id, {})
        scored = score_sealed_case(sealed, gold)
        scored["gold_provenance"] = {
            key: gold.get(key) for key in _GOLD_PROVENANCE_KEYS
        }
        paired.append(scored)
        _write_json(output_root / "paired" / f"{case_id}.json", scored)

    winner = evaluate_winner(paired)
    if not completeness["complete"]:
        winner = {
            "verdict": "INCONCLUSIVE",
            "reason": "incomplete_or_duplicate_sealed_artifacts",
            "completeness": completeness,
            "provisional_gate_result": winner,
        }
    report = {
        "schema": "gt.localization.vnext.comparison.v1",
        "sealed_count": len(sealed_rows),
        "paired_count": len(paired),
        "completeness": completeness,
        "paired_results": paired,
        "winner": winner,
        "sealed_input_sha256": hashlib.sha256(
            "\n".join(
                str(row.get("vnext", {}).get("deterministic_hash") or "")
                for row in sorted(
                    sealed_rows,
                    key=lambda item: str(item["case"]["id"]),
                )
            ).encode("utf-8")
        ).hexdigest(),
        "unmeasured": [
            "agent_file_reads",
            "repair_accuracy",
            "live_behavioral_causality",
        ],
    }
    _write_json(output_root / "COMPARISON.json", report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--cases", required=True)
    prepare.add_argument("--repos", required=True)
    prepare.add_argument("--language", required=True)
    prepare.add_argument("--shard-index", required=True, type=int)
    prepare.add_argument("--shard-count", required=True, type=int)
    prepare.add_argument("--out", required=True)

    clone = subparsers.add_parser("clone")
    clone.add_argument("--input", required=True)
    clone.add_argument("--repos", required=True)
    clone.add_argument("--destination", required=True)

    seal = subparsers.add_parser("seal")
    seal.add_argument("--input", required=True)
    seal.add_argument("--repos-dir", required=True)
    seal.add_argument("--graphs-dir", required=True)
    seal.add_argument("--out", required=True)
    seal.add_argument("--repeats", type=int, default=3)

    score = subparsers.add_parser("score")
    score.add_argument("--cases", required=True)
    score.add_argument("--sealed-root", required=True)
    score.add_argument("--out", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "prepare":
        prepared = prepare_shard(
            _read_json(Path(args.cases)),
            _read_json(Path(args.repos)),
            language=args.language,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
        )
        _write_json(Path(args.out), prepared)
        print(f"prepared={len(prepared)}")
        return 0
    if args.command == "clone":
        clone_repositories(
            _read_json(Path(args.input)),
            _read_json(Path(args.repos)),
            Path(args.destination),
        )
        return 0
    if args.command == "seal":
        summary = seal_shard(
            _read_json(Path(args.input)),
            repositories_root=Path(args.repos_dir),
            graphs_root=Path(args.graphs_dir),
            output_root=Path(args.out),
            repeats=args.repeats,
        )
        print(json.dumps(_json_ready(summary), sort_keys=True))
        return 0 if not summary["failures"] else 1
    if args.command == "score":
        report = score_sealed_artifacts(
            _read_json(Path(args.cases)),
            sealed_root=Path(args.sealed_root),
            output_root=Path(args.out),
        )
        print(json.dumps(_json_ready(report["winner"]), sort_keys=True))
        return 0 if report["completeness"]["complete"] else 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
