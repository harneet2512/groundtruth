#!/usr/bin/env python3
"""GitHub-sharded, gold-sealed OSS comparison for localization vNext.

The ``prepare`` phase emits exactly ``_INPUT_KEYS`` per case and rejects any
other field, so every gold field -- including one added to the corpus after
this file was written -- is stripped before a case manifest can enter the
sealing container.  ``seal`` indexes pinned repositories and runs legacy and
vNext localization without a gold-bearing input.  Only the separate ``score``
phase reads the original manifest and joins gold to already-sealed artifacts.
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

# The engine-visible allowlist. An allowlist, not a denylist: a gold field added
# to the corpus later is excluded by default instead of leaking until someone
# remembers to name it.
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
_PROVENANCE_REQUIRED_KEYS = (
    "substrate_digest",
    "source_sha",
    "github",
    "prepared_input_sha256",
    "cases_manifest_sha256",
    "repositories_manifest_sha256",
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_lower_hex(value: str, length: int) -> bool:
    return len(value) == length and all(
        character in "0123456789abcdef" for character in value
    )


def _validate_seal_provenance(
    provenance: Mapping[str, Any],
    *,
    prepared_input: Path,
) -> dict[str, Any]:
    missing = sorted(
        key for key in _PROVENANCE_REQUIRED_KEYS if not provenance.get(key)
    )
    if missing:
        raise ValueError(f"seal provenance missing required fields: {missing}")
    substrate_digest = str(provenance["substrate_digest"])
    substrate_sha256 = substrate_digest.rpartition("@sha256:")[2]
    if not _is_lower_hex(substrate_sha256, 64):
        raise ValueError("substrate provenance must be pinned by sha256 digest")
    source_sha = str(provenance["source_sha"])
    if not (
        _is_lower_hex(source_sha, 40) or _is_lower_hex(source_sha, 64)
    ):
        raise ValueError("source provenance must be an exact git SHA")
    for key in (
        "prepared_input_sha256",
        "cases_manifest_sha256",
        "repositories_manifest_sha256",
    ):
        if not _is_lower_hex(str(provenance[key]), 64):
            raise ValueError(f"seal provenance {key} must be a sha256 digest")
    github = provenance.get("github")
    if not isinstance(github, Mapping):
        raise ValueError("seal provenance github identity must be an object")
    missing_github = sorted(
        key
        for key in (
            "repository",
            "workflow",
            "run_id",
            "run_attempt",
            "job",
            "matrix_language",
            "matrix_shard",
        )
        if github.get(key) in (None, "")
    )
    if missing_github:
        raise ValueError(
            f"seal provenance github identity missing fields: {missing_github}"
        )
    actual_input_sha256 = _file_sha256(prepared_input)
    expected_input_sha256 = str(provenance["prepared_input_sha256"])
    if actual_input_sha256 != expected_input_sha256:
        raise ValueError(
            "prepared input hash mismatch "
            f"actual={actual_input_sha256} expected={expected_input_sha256}"
        )
    return dict(provenance)


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


def _input_only(row: Mapping[str, Any]) -> dict[str, str]:
    """Fail closed unless a prepared row carries exactly the allowed keys."""
    unexpected = sorted(set(row) - set(_INPUT_KEYS))
    missing = sorted(set(_INPUT_KEYS) - set(row))
    if unexpected or missing:
        raise ValueError(
            "prepared case must carry exactly _INPUT_KEYS "
            f"(unexpected={unexpected} missing={missing})"
        )
    return {key: str(row[key]) for key in _INPUT_KEYS}


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


SHARD_BY_LANGUAGE = "language"
SHARD_BY_CASE = "case"
_SHARD_BY_MODES = (SHARD_BY_LANGUAGE, SHARD_BY_CASE)
# Lanes per language when sharding by language. Frozen: this is the divisor that
# produced the sealed oss-60 partition, so it may never move without reproving the
# assignment digest in tests/pretask/test_localization_vnext_oss_compare.py.
LANGUAGE_LANES_PER_LANGUAGE = 4
MATRIX_LANGUAGES = ("python", "go", "javascript", "typescript", "rust")
# Lanes when sharding by case. Chosen on measured timing, not taste: run
# 30196352388 sealed 60 cases in 29,833 job-seconds (497 s/case) with a per-case
# spread of 7 s..2,341 s (cv 1.04, driven by repository size, not issue length).
# Bootstrapping that empirical per-case distribution over 294 cases puts
# P(some lane exceeds the 180 min job timeout) at 68% for 20 lanes and 0.40% for
# 40 lanes, and 40 is exactly two full waves of the 20-way job concurrency the
# same run measured.
CASE_LANES = 40


def lane_members(
    cases: Sequence[Mapping[str, Any]],
    *,
    lane_index: int,
    lane_count: int,
    language: str = "",
    shard_by: str = SHARD_BY_LANGUAGE,
) -> list[Mapping[str, Any]]:
    """The cases one lane owns. The ONLY definition of the partition.

    ``shard_by=language`` keeps every case in its language's lane group, which is
    the assignment the sealed oss-60 corpus was produced with. ``shard_by=case``
    lifts only the language predicate; the round-robin over id-sorted cases is the
    same statement, so a corpus that is 293 python + 1 typescript spreads over all
    lanes instead of piling into four of them. Round-robin rather than a hash of
    the id because it is balanced by construction (lane sizes differ by at most
    one), and lane balance is exactly what buys the job-timeout margin.
    """
    if lane_count <= 0:
        raise ValueError("lane_count must be positive")
    if not 0 <= lane_index < lane_count:
        raise ValueError("lane_index must be within lane_count")
    if shard_by not in _SHARD_BY_MODES:
        raise ValueError(f"shard_by must be one of {list(_SHARD_BY_MODES)}")
    if shard_by == SHARD_BY_LANGUAGE and not language:
        raise ValueError("shard_by=language requires a language")
    pool = (
        cases
        if shard_by == SHARD_BY_CASE
        else [
            case
            for case in cases
            if str(case.get("language") or "").lower() == language.lower()
        ]
    )
    selected = sorted(pool, key=lambda case: str(case.get("id") or ""))
    return [
        case
        for index, case in enumerate(selected)
        if index % lane_count == lane_index
    ]


def prepare_shard(
    cases: Sequence[Mapping[str, Any]],
    repositories: Mapping[str, Mapping[str, Any]],
    *,
    lane_index: int,
    lane_count: int,
    language: str = "",
    shard_by: str = SHARD_BY_LANGUAGE,
) -> list[dict[str, str]]:
    """Return a deterministic, gold-free shard manifest for one lane."""
    output: list[dict[str, str]] = []
    for case in lane_members(
        cases,
        lane_index=lane_index,
        lane_count=lane_count,
        language=language,
        shard_by=shard_by,
    ):
        case_id = str(case["id"])
        repo_name = str(case["repo"])
        repo = repositories.get(repo_name)
        if repo is None:
            raise ValueError(f"repository metadata missing for {repo_name}")
        output.append(
            _input_only(
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
        )
    return output


def plan_lanes(
    cases: Sequence[Mapping[str, Any]],
    *,
    shard_by: str = SHARD_BY_LANGUAGE,
    lane_count: int | None = None,
) -> dict[str, Any]:
    """Resolve the seal matrix for one corpus.

    The workflow matrix is built from this so the cell list and the partition each
    cell then re-computes come from one function instead of two hand-kept copies.
    Emits job identity only -- language, lane index, lane count -- and never a case
    id, so the gold-bearing manifest cannot reach a job through the matrix.

    Empty lanes are dropped: an empty shard would fail the sealed-artifact upload
    (``if-no-files-found: error``) at the END of a job, and 12 of the 20 language
    cells are empty on a 293-python corpus.
    """
    if shard_by not in _SHARD_BY_MODES:
        raise ValueError(f"shard_by must be one of {list(_SHARD_BY_MODES)}")
    by_language = shard_by == SHARD_BY_LANGUAGE
    resolved = int(
        lane_count
        or (LANGUAGE_LANES_PER_LANGUAGE if by_language else CASE_LANES)
    )
    if resolved <= 0:
        raise ValueError("lane_count must be positive")
    if not by_language:
        # More lanes than cases would only manufacture empty jobs.
        resolved = min(resolved, len(cases))
    if resolved <= 0:
        raise ValueError("cannot plan lanes for an empty corpus")
    cells = (
        [
            {"language": language, "shard": lane}
            for language in MATRIX_LANGUAGES
            for lane in range(resolved)
        ]
        if by_language
        else [{"language": "all", "shard": lane} for lane in range(resolved)]
    )

    include: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for cell in cells:
        size = len(
            lane_members(
                cases,
                lane_index=int(cell["shard"]),
                lane_count=resolved,
                language=str(cell["language"]),
                shard_by=shard_by,
            )
        )
        counts[f"{cell['language']}-{cell['shard']}"] = size
        (include if size else dropped).append(cell)
    return {
        "schema": "gt.localization.vnext.lane_plan.v1",
        "shard_by": shard_by,
        "lane_count": resolved,
        "include": include,
        "lane_sizes": counts,
        "dropped_empty_lanes": dropped,
        "case_count": len(cases),
        "assigned_count": sum(counts.values()),
    }


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
    execution_provenance: Mapping[str, Any],
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
            sealed["execution_provenance"] = dict(execution_provenance)
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
        "execution_provenance": dict(execution_provenance),
    }
    _write_json(output_root / "SEAL_SUMMARY.json", summary)
    return summary


def validate_sealed_joins(
    cases: Sequence[Mapping[str, Any]],
    repositories: Mapping[str, Mapping[str, Any]],
    sealed_rows: Sequence[Mapping[str, Any]],
    *,
    expected_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed when a sealed row cannot be joined to its exact input."""
    expected_provenance = dict(expected_provenance or {})
    case_counts = Counter(str(case.get("id") or "") for case in cases)
    gold_by_id = {str(case["id"]): case for case in cases}
    failures: list[dict[str, Any]] = []
    for case_id, count in sorted(case_counts.items()):
        if not case_id or count != 1:
            failures.append(
                {
                    "case_id": case_id,
                    "code": "gold_case_id_not_unique",
                    "expected": 1,
                    "actual": count,
                }
            )

    for sealed in sealed_rows:
        sealed_case = sealed.get("case") or {}
        case_id = str(sealed_case.get("id") or "")
        gold = gold_by_id.get(case_id)
        if gold is None:
            failures.append(
                {"case_id": case_id, "code": "sealed_case_missing_from_gold"}
            )
            continue

        expected_issue_sha256 = hashlib.sha256(
            str(gold.get("issue_text") or "").encode("utf-8")
        ).hexdigest()
        actual_issue_sha256 = str(sealed_case.get("issue_sha256") or "")
        if actual_issue_sha256 != expected_issue_sha256:
            failures.append(
                {
                    "case_id": case_id,
                    "code": "issue_sha256_mismatch",
                    "expected": expected_issue_sha256,
                    "actual": actual_issue_sha256,
                }
            )

        repo_name = str(gold.get("repo") or "")
        repo = repositories.get(repo_name)
        if repo is None:
            failures.append(
                {
                    "case_id": case_id,
                    "code": "repository_pin_missing",
                    "repository": repo_name,
                }
            )
        else:
            expected_revision = str(repo.get("commit") or "")
            actual_revision = str(sealed_case.get("revision_identity") or "")
            if not expected_revision or actual_revision != expected_revision:
                failures.append(
                    {
                        "case_id": case_id,
                        "code": "revision_identity_mismatch",
                        "expected": expected_revision,
                        "actual": actual_revision,
                    }
                )

        provenance = sealed.get("execution_provenance")
        if not isinstance(provenance, Mapping):
            failures.append(
                {"case_id": case_id, "code": "execution_provenance_missing"}
            )
            continue
        for key in _PROVENANCE_REQUIRED_KEYS:
            if not provenance.get(key):
                failures.append(
                    {
                        "case_id": case_id,
                        "code": "execution_provenance_field_missing",
                        "field": key,
                    }
                )
        prepared_sha256 = str(provenance.get("prepared_input_sha256") or "")
        if not _is_lower_hex(prepared_sha256, 64):
            failures.append(
                {
                    "case_id": case_id,
                    "code": "prepared_input_sha256_invalid",
                    "actual": prepared_sha256,
                }
            )
        substrate_sha256 = str(provenance.get("substrate_digest") or "").rpartition(
            "@sha256:"
        )[2]
        source_sha = str(provenance.get("source_sha") or "")
        invalid_fields = [
            key
            for key, value, length in (
                ("substrate_digest", substrate_sha256, 64),
                ("cases_manifest_sha256", provenance.get("cases_manifest_sha256"), 64),
                (
                    "repositories_manifest_sha256",
                    provenance.get("repositories_manifest_sha256"),
                    64,
                ),
            )
            if not _is_lower_hex(str(value or ""), length)
        ]
        if not (
            _is_lower_hex(source_sha, 40) or _is_lower_hex(source_sha, 64)
        ):
            invalid_fields.append("source_sha")
        for field in invalid_fields:
            failures.append(
                {
                    "case_id": case_id,
                    "code": "execution_provenance_field_invalid",
                    "field": field,
                }
            )

        for key in (
            "substrate_digest",
            "source_sha",
            "cases_manifest_sha256",
            "repositories_manifest_sha256",
        ):
            if key not in expected_provenance:
                continue
            actual = str(provenance.get(key) or "")
            expected = str(expected_provenance[key])
            if actual != expected:
                failures.append(
                    {
                        "case_id": case_id,
                        "code": "execution_provenance_mismatch",
                        "field": key,
                        "expected": expected,
                        "actual": actual,
                    }
                )

        actual_github = provenance.get("github")
        expected_github = expected_provenance.get("github")
        if not isinstance(actual_github, Mapping):
            failures.append(
                {"case_id": case_id, "code": "github_run_identity_missing"}
            )
        elif isinstance(expected_github, Mapping):
            for key, expected in expected_github.items():
                actual = actual_github.get(key)
                if str(actual) != str(expected):
                    failures.append(
                        {
                            "case_id": case_id,
                            "code": "github_run_identity_mismatch",
                            "field": key,
                            "expected": str(expected),
                            "actual": str(actual),
                        }
                    )
    return {
        "valid": not failures,
        "validated_count": len(sealed_rows),
        "failures": failures,
    }


def _case_explanation(sealed: Mapping[str, Any]) -> dict[str, Any]:
    vnext = sealed.get("vnext") or {}
    comparison = sealed.get("comparison") or {}
    decisions = list(vnext.get("decisions") or ())
    decision_counts = Counter(
        str(decision.get("action") or "unknown").upper()
        for decision in decisions
    )
    return {
        "stopping_reason": str(vnext.get("stopping_reason") or "unknown"),
        "counts": {
            "discovered": len(vnext.get("discoveries") or ()),
            "admitted": int(decision_counts.get("ADMIT", 0)),
            "rejected": int(decision_counts.get("REJECT", 0)),
            "deferred": int(decision_counts.get("DEFER", 0)),
            "regions": len(vnext.get("admitted_regions") or ()),
        },
        "coverage": vnext.get("coverage") or {},
        "capabilities": vnext.get("capabilities") or {},
        "first_divergence": comparison.get("first_divergence") or {},
        "decisions": decisions,
        "regions": list(vnext.get("admitted_regions") or ()),
        "ablations": comparison.get("ablations") or {},
        "operational_metrics": vnext.get("metrics") or {},
        "legacy_ranked_files": list(
            (sealed.get("legacy") or {}).get("candidate_order") or ()
        ),
        "vnext_ranked_files": list(
            comparison.get("ranked_discovery_files") or ()
        ),
        # Attribution diagnostic: the same order with the model-visible legacy
        # ranking floor removed, so a shadow ordering change stays readable.
        "vnext_ranked_files_shadow_only": list(
            comparison.get("ranked_discovery_files_shadow_only") or ()
        ),
    }


def score_sealed_artifacts(
    cases: Sequence[Mapping[str, Any]],
    repositories: Mapping[str, Mapping[str, Any]],
    *,
    sealed_root: Path,
    output_root: Path,
    expected_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Join gold only after all shard artifacts have already been sealed."""
    expected = {str(case["id"]) for case in cases}
    sealed_paths = sorted(sealed_root.glob("**/sealed/*.json"))
    sealed_rows = [_read_json(path) for path in sealed_paths]
    actual_ids = [str(row["case"]["id"]) for row in sealed_rows]
    completeness = validate_sealed_case_ids(expected, actual_ids)
    gold_by_id = {str(case["id"]): dict(case) for case in cases}
    join_validation = validate_sealed_joins(
        cases,
        repositories,
        sealed_rows,
        expected_provenance=expected_provenance,
    )

    paired: list[dict[str, Any]] = []
    if join_validation["valid"]:
        for sealed in sealed_rows:
            case_id = str(sealed["case"]["id"])
            gold = gold_by_id.get(case_id, {})
            scored = score_sealed_case(sealed, gold)
            scored["gold_provenance"] = {
                key: gold.get(key) for key in _GOLD_PROVENANCE_KEYS
            }
            scored["execution_provenance"] = sealed.get(
                "execution_provenance"
            )
            scored["explanation"] = _case_explanation(sealed)
            paired.append(scored)
            _write_json(output_root / "paired" / f"{case_id}.json", scored)

    winner = evaluate_winner(paired)
    if not join_validation["valid"]:
        winner = {
            "verdict": "INCONCLUSIVE",
            "reason": "sealed_join_validation_failed",
            "join_validation": join_validation,
        }
    elif not completeness["complete"]:
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
        "join_validation": join_validation,
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

    plan = subparsers.add_parser("plan")
    plan.add_argument("--cases", required=True)
    plan.add_argument(
        "--shard-by",
        choices=_SHARD_BY_MODES,
        default=SHARD_BY_LANGUAGE,
    )
    plan.add_argument(
        "--lane-count",
        type=int,
        default=0,
        help="0 = the mode default (language: 4 per language, case: 40)",
    )
    plan.add_argument("--out", default="")

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--cases", required=True)
    prepare.add_argument("--repos", required=True)
    prepare.add_argument(
        "--language",
        default="",
        help="required by --shard-by language; ignored by --shard-by case",
    )
    prepare.add_argument(
        "--shard-by",
        choices=_SHARD_BY_MODES,
        default=SHARD_BY_LANGUAGE,
    )
    # --shard-index/--shard-count are the pre-lane spellings. Kept as aliases
    # because seal and score are separate jobs, so re-preparing one lane of an
    # already-dispatched run is a real operation.
    prepare.add_argument(
        "--lane-index",
        "--shard-index",
        dest="lane_index",
        required=True,
        type=int,
    )
    prepare.add_argument(
        "--lane-count",
        "--shard-count",
        dest="lane_count",
        required=True,
        type=int,
    )
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
    seal.add_argument("--provenance", required=True)

    score = subparsers.add_parser("score")
    score.add_argument("--cases", required=True)
    score.add_argument("--repos", required=True)
    score.add_argument("--sealed-root", required=True)
    score.add_argument("--out", required=True)
    score.add_argument("--expected-substrate-digest", required=True)
    score.add_argument("--expected-source-sha", required=True)
    score.add_argument("--expected-github-repository", required=True)
    score.add_argument("--expected-github-workflow", required=True)
    score.add_argument("--expected-run-id", required=True)
    score.add_argument("--expected-run-attempt", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "plan":
        plan = plan_lanes(
            _read_json(Path(args.cases)),
            shard_by=args.shard_by,
            lane_count=args.lane_count or None,
        )
        if args.out:
            _write_json(Path(args.out), plan)
        print(json.dumps(plan, sort_keys=True))
        return 0
    if args.command == "prepare":
        prepared = prepare_shard(
            _read_json(Path(args.cases)),
            _read_json(Path(args.repos)),
            language=args.language,
            lane_index=args.lane_index,
            lane_count=args.lane_count,
            shard_by=args.shard_by,
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
        prepared_input = Path(args.input)
        execution_provenance = _validate_seal_provenance(
            _read_json(Path(args.provenance)),
            prepared_input=prepared_input,
        )
        summary = seal_shard(
            _read_json(prepared_input),
            repositories_root=Path(args.repos_dir),
            graphs_root=Path(args.graphs_dir),
            output_root=Path(args.out),
            repeats=args.repeats,
            execution_provenance=execution_provenance,
        )
        print(json.dumps(_json_ready(summary), sort_keys=True))
        return 0 if not summary["failures"] else 1
    if args.command == "score":
        cases_path = Path(args.cases)
        repos_path = Path(args.repos)
        expected_provenance = {
            "substrate_digest": args.expected_substrate_digest,
            "source_sha": args.expected_source_sha,
            "cases_manifest_sha256": _file_sha256(cases_path),
            "repositories_manifest_sha256": _file_sha256(repos_path),
            "github": {
                "repository": args.expected_github_repository,
                "workflow": args.expected_github_workflow,
                "run_id": args.expected_run_id,
                "run_attempt": args.expected_run_attempt,
            },
        }
        report = score_sealed_artifacts(
            _read_json(cases_path),
            _read_json(repos_path),
            sealed_root=Path(args.sealed_root),
            output_root=Path(args.out),
            expected_provenance=expected_provenance,
        )
        print(json.dumps(_json_ready(report["winner"]), sort_keys=True))
        return (
            0
            if report["completeness"]["complete"]
            and report["join_validation"]["valid"]
            else 1
        )
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
