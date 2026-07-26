from __future__ import annotations

import hashlib
import json
from collections import Counter
from itertools import product
from pathlib import Path

import yaml
import pytest

import scripts.localization_vnext_oss_compare as compare_script
from scripts.localization_vnext_oss_compare import (
    _repository_root_for_script,
    _validate_seal_provenance,
    prepare_shard,
    score_sealed_artifacts,
    validate_sealed_joins,
    validate_sealed_case_ids,
)


WORKFLOW = Path(".github/workflows/localization_vnext_shadow_compare.yml")
CASES = Path("benchmarks/data/oss_all60_cases.json")
REPOSITORIES = Path("benchmarks/data/oss_all60_repos.json")


def _provenance() -> dict:
    return {
        "schema": "gt.localization.vnext.github.provenance.v1",
        "substrate_digest": "ghcr.io/example/substrate@sha256:" + "d" * 64,
        "source_sha": "a" * 40,
        "github": {
            "repository": "example/groundtruth",
            "workflow": "example/workflow.yml@refs/pull/1/merge",
            "run_id": "123",
            "run_attempt": "1",
            "job": "seal",
            "matrix_language": "python",
            "matrix_shard": "0",
        },
        "prepared_input_sha256": "b" * 64,
        "cases_manifest_sha256": "c" * 64,
        "repositories_manifest_sha256": "e" * 64,
    }


def test_root_mounted_runner_uses_working_directory_as_repository_root():
    assert _repository_root_for_script(
        Path("/runner.py"),
        cwd=Path("/opt/gt"),
    ) == Path("/opt/gt")
    assert _repository_root_for_script(
        Path("/workspace/scripts/localization_vnext_oss_compare.py"),
        cwd=Path("/ignored"),
    ) == Path("/workspace")


def test_prepare_shard_is_stable_and_strips_all_gold_fields():
    cases = [
        {
            "id": "random_py_b",
            "language": "python",
            "repo": "repo-b",
            "issue_text": "Behavior B should change.",
            "gold_files": ["pkg/b.py"],
            "gold_symbols": ["B.run"],
            "gold_line_ranges": [{"file": "pkg/b.py", "start": 2, "end": 4}],
            "patch_sha256": "secret-after-seal",
            "fix_commit_sha256": "also-secret-after-seal",
        },
        {
            "id": "random_py_a",
            "language": "python",
            "repo": "repo-a",
            "issue_text": "Behavior A should change.",
            "gold_files": ["pkg/a.py"],
        },
        {
            "id": "random_go_c",
            "language": "go",
            "repo": "repo-c",
            "issue_text": "Behavior C should change.",
            "gold_files": ["pkg/c.go"],
        },
    ]
    repos = {
        "repo-a": {"commit": "a" * 40, "url": "https://example.invalid/a"},
        "repo-b": {"commit": "b" * 40, "url": "https://example.invalid/b"},
        "repo-c": {"commit": "c" * 40, "url": "https://example.invalid/c"},
    }

    shard_zero = prepare_shard(
        cases,
        repos,
        language="python",
        shard_index=0,
        shard_count=2,
    )
    shard_one = prepare_shard(
        list(reversed(cases)),
        repos,
        language="python",
        shard_index=1,
        shard_count=2,
    )

    assert [row["id"] for row in shard_zero] == ["random_py_a"]
    assert [row["id"] for row in shard_one] == ["random_py_b"]
    assert {
        key
        for row in [*shard_zero, *shard_one]
        for key in row
        if key.startswith("gold")
        or key in {"patch_sha256", "fix_commit", "fix_commit_sha256"}
    } == set()
    assert shard_zero[0]["revision_identity"] == "a" * 40
    assert shard_one[0]["split"] == "random"


def test_validate_sealed_case_ids_rejects_missing_and_duplicates():
    expected = {"case-a", "case-b"}

    complete = validate_sealed_case_ids(
        expected,
        ["case-a", "case-b"],
    )
    incomplete = validate_sealed_case_ids(
        expected,
        ["case-a", "case-a"],
    )

    assert complete == {"complete": True, "missing": [], "duplicates": [], "extra": []}
    assert incomplete == {
        "complete": False,
        "missing": ["case-b"],
        "duplicates": ["case-a"],
        "extra": [],
    }


def test_workflow_never_mounts_gold_manifest_into_sealing_container():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    document = yaml.safe_load(workflow)
    matrix = document["jobs"]["seal"]["strategy"]["matrix"]
    combinations = list(product(matrix["language"], matrix["shard"]))

    assert matrix["language"] == [
        "python",
        "go",
        "javascript",
        "typescript",
        "rust",
    ]
    assert matrix["shard"] == [0, 1, 2, 3]
    assert len(combinations) == 20
    assert len(set(combinations)) == 20
    assert "/cases.input.json:ro" in workflow
    assert "oss_all60_cases.json:/cases" not in workflow
    assert "OMP_NUM_THREADS=1" in workflow
    assert "TF_ENABLE_ONEDNN_OPTS=0" in workflow
    assert "GT_REQUIRE_LSP=1" not in workflow
    assert "fix_commit_sha256" in workflow
    assert "substrate must be an exact ghcr.io sha256 digest" in workflow
    assert "--provenance /provenance.json" in workflow
    assert "localization_vnext_oss_compare.py score" in workflow
    assert '--cases "benchmarks/data/$CASES_FILE"' in workflow
    assert '--repos "benchmarks/data/$REPOS_FILE"' in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_workflow_matrix_covers_all_60_manifest_cases_exactly_once():
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    matrix = document["jobs"]["seal"]["strategy"]["matrix"]
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    repositories = json.loads(REPOSITORIES.read_text(encoding="utf-8"))
    sealed_case_ids = [
        row["id"]
        for language, shard in product(matrix["language"], matrix["shard"])
        for row in prepare_shard(
            cases,
            repositories,
            language=language,
            shard_index=shard,
            shard_count=len(matrix["shard"]),
        )
    ]

    assert len(cases) == 60
    assert len(sealed_case_ids) == 60
    assert Counter(sealed_case_ids) == Counter(
        {str(case["id"]): 1 for case in cases}
    )


def test_prepared_manifest_round_trip_contains_no_gold(tmp_path):
    cases = [
        {
            "id": "held_py_case",
            "language": "python",
            "repo": "repo",
            "issue_text": "Parser should reject invalid state.",
            "gold_files": ["parser.py"],
        }
    ]
    repos = {
        "repo": {
            "commit": "1" * 40,
            "url": "https://example.invalid/repo",
        }
    }
    output = tmp_path / "input.json"

    output.write_text(
        json.dumps(
            prepare_shard(
                cases,
                repos,
                language="python",
                shard_index=0,
                shard_count=4,
            )
        ),
        encoding="utf-8",
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload[0]["id"] == "held_py_case"
    assert "gold_files" not in output.read_text(encoding="utf-8")


def test_seal_provenance_locks_prepared_input_hash(tmp_path):
    prepared = tmp_path / "cases.input.json"
    prepared.write_text('[{"id":"case"}]', encoding="utf-8")
    provenance = _provenance()
    provenance["prepared_input_sha256"] = hashlib.sha256(
        prepared.read_bytes()
    ).hexdigest()

    validated = _validate_seal_provenance(
        provenance,
        prepared_input=prepared,
    )

    assert validated["source_sha"] == "a" * 40
    unpinned = dict(provenance)
    unpinned["substrate_digest"] = "ghcr.io/example/substrate:latest"
    with pytest.raises(ValueError, match="pinned by sha256"):
        _validate_seal_provenance(unpinned, prepared_input=prepared)
    prepared.write_text('[{"id":"different"}]', encoding="utf-8")
    with pytest.raises(ValueError, match="prepared input hash mismatch"):
        _validate_seal_provenance(provenance, prepared_input=prepared)


def test_join_validation_rejects_issue_revision_and_run_identity_mismatch():
    issue_text = "Expected behavior"
    cases = [
        {
            "id": "case",
            "repo": "repo",
            "issue_text": issue_text,
        }
    ]
    repositories = {"repo": {"commit": "1" * 40}}
    provenance = _provenance()
    sealed = {
        "case": {
            "id": "case",
            "issue_sha256": hashlib.sha256(issue_text.encode()).hexdigest(),
            "revision_identity": "1" * 40,
        },
        "execution_provenance": provenance,
    }
    expected = {
        "substrate_digest": provenance["substrate_digest"],
        "source_sha": provenance["source_sha"],
        "cases_manifest_sha256": provenance["cases_manifest_sha256"],
        "repositories_manifest_sha256": provenance[
            "repositories_manifest_sha256"
        ],
        "github": {
            "repository": provenance["github"]["repository"],
            "workflow": provenance["github"]["workflow"],
            "run_id": provenance["github"]["run_id"],
            "run_attempt": provenance["github"]["run_attempt"],
        },
    }

    assert validate_sealed_joins(
        cases,
        repositories,
        [sealed],
        expected_provenance=expected,
    )["valid"]

    sealed["case"]["issue_sha256"] = "0" * 64
    sealed["case"]["revision_identity"] = "2" * 40
    sealed["execution_provenance"]["github"]["run_id"] = "wrong"
    validation = validate_sealed_joins(
        cases,
        repositories,
        [sealed],
        expected_provenance=expected,
    )

    assert validation["valid"] is False
    assert {failure["code"] for failure in validation["failures"]} == {
        "issue_sha256_mismatch",
        "revision_identity_mismatch",
        "github_run_identity_mismatch",
    }


def test_score_phase_joins_gold_to_already_sealed_artifacts(tmp_path):
    issue_text = "Parser should reject invalid state."
    revision = "1" * 40
    sealed = {
        "case": {
            "id": "random_case",
            "language": "python",
            "split": "random",
            "issue_sha256": hashlib.sha256(issue_text.encode()).hexdigest(),
            "revision_identity": revision,
        },
        "execution_provenance": _provenance(),
        "legacy": {
            "candidate_order": ["src/other.py"],
            "witnesses": [],
            "implied_inspection_tokens": 100,
            "latency_ms": 10.0,
            "peak_memory_bytes": 1000,
            "byte_identity": True,
        },
        "vnext": {
            "deterministic_hash": "a" * 64,
            "discoveries": [{"symbol": "Gold"}],
            "admitted_regions": [],
            "metrics": {"leakage_count": 0},
            "stopping_reason": "coverage_complete",
            "coverage": {
                "required": ["operation"],
                "covered": ["operation"],
                "unresolved": [],
                "unavailable": [],
            },
            "capabilities": {"available": {"fts5": True}, "unavailable": {}},
            "decisions": [
                {
                    "evidence_id": "ev-1",
                    "action": "ADMIT",
                    "reason_codes": ["new_mandatory_certified"],
                }
            ],
        },
        "comparison": {
            "new_admitted_files": ["src/gold.py"],
            "ranked_discovery_files": ["src/gold.py"],
            "deterministic": True,
            "p95_latency_ms": 5.0,
            "peak_memory_bytes": 900,
            "implied_inspection_tokens": 10,
            "first_divergence": {
                "rank": 1,
                "old": "src/other.py",
                "new": "src/gold.py",
            },
            "ablations": {"behavioral_facets": {"changed_output": True}},
        },
    }
    sealed_path = tmp_path / "download" / "shard" / "sealed" / "random_case.json"
    sealed_path.parent.mkdir(parents=True)
    sealed_path.write_text(json.dumps(sealed), encoding="utf-8")
    output = tmp_path / "comparison"

    report = score_sealed_artifacts(
        [
            {
                "id": "random_case",
                "language": "python",
                "repo": "repo",
                "issue_text": issue_text,
                "gold_files": ["src/gold.py"],
            }
        ],
        {"repo": {"commit": revision}},
        sealed_root=tmp_path / "download",
        output_root=output,
    )

    assert report["completeness"]["complete"] is True
    assert report["join_validation"]["valid"] is True
    assert report["paired_results"][0]["old"]["hit_at_8"] is False
    assert report["paired_results"][0]["new"]["hit_at_1"] is True
    explanation = report["paired_results"][0]["explanation"]
    assert explanation["stopping_reason"] == "coverage_complete"
    assert explanation["counts"]["admitted"] == 1
    assert explanation["coverage"]["covered"] == ["operation"]
    assert explanation["capabilities"]["available"]["fts5"] is True
    assert explanation["first_divergence"]["rank"] == 1
    assert explanation["decisions"][0]["action"] == "ADMIT"
    assert explanation["ablations"]["behavioral_facets"]["changed_output"] is True
    assert (output / "COMPARISON.json").is_file()


def test_score_phase_refuses_to_score_a_failed_sealed_join(
    tmp_path,
    monkeypatch,
):
    sealed_path = tmp_path / "download" / "shard" / "sealed" / "case.json"
    sealed_path.parent.mkdir(parents=True)
    sealed_path.write_text(
        json.dumps(
            {
                "case": {
                    "id": "case",
                    "issue_sha256": "0" * 64,
                    "revision_identity": "1" * 40,
                },
                "execution_provenance": _provenance(),
                "vnext": {"deterministic_hash": "a" * 64},
            }
        ),
        encoding="utf-8",
    )

    def scoring_must_not_run(*_args, **_kwargs):
        raise AssertionError("gold scoring ran before sealed join validation")

    monkeypatch.setattr(
        compare_script,
        "score_sealed_case",
        scoring_must_not_run,
    )
    report = score_sealed_artifacts(
        [{"id": "case", "repo": "repo", "issue_text": "expected"}],
        {"repo": {"commit": "1" * 40}},
        sealed_root=tmp_path / "download",
        output_root=tmp_path / "comparison",
    )

    assert report["paired_count"] == 0
    assert report["join_validation"]["valid"] is False
    assert report["winner"]["reason"] == "sealed_join_validation_failed"
