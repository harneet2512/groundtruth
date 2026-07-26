from __future__ import annotations

import json
from pathlib import Path

from scripts.localization_vnext_oss_compare import (
    _repository_root_for_script,
    prepare_shard,
    score_sealed_artifacts,
    validate_sealed_case_ids,
)


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
        if key.startswith("gold") or key in {"patch_sha256", "fix_commit"}
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
    workflow = Path(
        ".github/workflows/localization_vnext_shadow_compare.yml"
    ).read_text(encoding="utf-8")

    assert "matrix:" in workflow
    assert "language: [python, go, javascript, typescript, rust]" in workflow
    assert "shard: [0, 1, 2, 3]" in workflow
    assert "/cases.input.json:ro" in workflow
    assert "oss_all60_cases.json:/cases" not in workflow
    assert "OMP_NUM_THREADS=1" in workflow
    assert "TF_ENABLE_ONEDNN_OPTS=0" in workflow
    assert "localization_vnext_oss_compare.py score" in workflow
    assert '--cases "benchmarks/data/$CASES_FILE"' in workflow
    assert "actions/upload-artifact@v4" in workflow


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


def test_score_phase_joins_gold_to_already_sealed_artifacts(tmp_path):
    sealed = {
        "case": {"id": "random_case", "language": "python", "split": "random"},
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
        },
        "comparison": {
            "new_admitted_files": ["src/gold.py"],
            "ranked_discovery_files": ["src/gold.py"],
            "deterministic": True,
            "p95_latency_ms": 5.0,
            "peak_memory_bytes": 900,
            "implied_inspection_tokens": 10,
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
                "gold_files": ["src/gold.py"],
            }
        ],
        sealed_root=tmp_path / "download",
        output_root=output,
    )

    assert report["completeness"]["complete"] is True
    assert report["paired_results"][0]["old"]["hit_at_8"] is False
    assert report["paired_results"][0]["new"]["hit_at_1"] is True
    assert (output / "COMPARISON.json").is_file()
