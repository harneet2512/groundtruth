from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import yaml
import pytest

import scripts.localization_vnext_oss_compare as compare_script
from scripts.localization_vnext_oss_compare import (
    _repository_root_for_script,
    _validate_seal_provenance,
    lane_members,
    plan_lanes,
    prepare_shard,
    score_sealed_artifacts,
    validate_sealed_joins,
    validate_sealed_case_ids,
)


WORKFLOW = Path(".github/workflows/localization_vnext_shadow_compare.yml")
CASES = Path("benchmarks/data/oss_all60_cases.json")
REPOSITORIES = Path("benchmarks/data/oss_all60_repos.json")
NEW_CASES = Path("benchmarks/data/swebench_live_gold_cases.json")
NEW_REPOSITORIES = Path("benchmarks/data/swebench_live_gold_repos.json")

# The case -> job assignment of the sealed oss-60 corpus, digested over all 20
# cells at commit fe09906d7 BEFORE --shard-by existed. A run using this partition
# was in flight when the lane work landed, so a diff here is a corpus-identity
# break, not a test that needs updating.
OSS60_ASSIGNMENT_SHA256 = (
    "226416f8f271c46367c8208ddd327a4177eaacbde9f2be36bf879f4b76cd61ee"
)
OSS60_PREPARED_SHA256 = (
    "408bc2aa1ee9455eae914247b2ee6641d5cbe2ba7511271fd4c7c478c1c22970"
)


def _language_cells():
    return plan_lanes(
        json.loads(CASES.read_text(encoding="utf-8")),
        shard_by="language",
    )["include"]


def _assignment_digests(cases, repositories, plan):
    """Digest case -> job, and the exact prepared-input bytes each job receives."""
    assignment = []
    per_cell = {}
    for cell in plan["include"]:
        rows = prepare_shard(
            cases,
            repositories,
            language=cell["language"],
            lane_index=cell["shard"],
            lane_count=plan["lane_count"],
            shard_by=plan["shard_by"],
        )
        job = f"loc-vnext-{cell['language']}-{cell['shard']}"
        assignment.extend([row["id"], job] for row in rows)
        blob = json.dumps(
            compare_script._json_ready(rows),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        per_cell[job] = {"n": len(rows), "sha256": hashlib.sha256(blob).hexdigest()}
    assignment.sort()
    return (
        hashlib.sha256(
            json.dumps(assignment, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        hashlib.sha256(
            json.dumps(per_cell, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        assignment,
        per_cell,
    )


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
        lane_index=0,
        lane_count=2,
    )
    shard_one = prepare_shard(
        list(reversed(cases)),
        repos,
        language="python",
        lane_index=1,
        lane_count=2,
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
    cells = _language_cells()
    combinations = [(cell["language"], cell["shard"]) for cell in cells]

    # The matrix is now resolved by the planner, so the cells are asserted on the
    # planner's output rather than on a YAML literal that no longer carries them.
    assert matrix == "${{ fromJSON(needs.plan.outputs.matrix) }}"
    assert document["jobs"]["seal"]["needs"] == "plan"
    assert sorted({cell["language"] for cell in cells}) == sorted(
        [
            "python",
            "go",
            "javascript",
            "typescript",
            "rust",
        ]
    )
    assert sorted({cell["shard"] for cell in cells}) == [0, 1, 2, 3]
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
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    repositories = json.loads(REPOSITORIES.read_text(encoding="utf-8"))
    plan = plan_lanes(cases, shard_by="language")
    sealed_case_ids = [
        row["id"]
        for cell in plan["include"]
        for row in prepare_shard(
            cases,
            repositories,
            language=cell["language"],
            lane_index=cell["shard"],
            lane_count=plan["lane_count"],
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
                lane_index=0,
                lane_count=4,
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


# --------------------------------------------------------------------------
# Lane sharding. The oss-60 partition is FROZEN: a run using it was in flight
# when --shard-by landed, so these digests are the backward-compatibility proof.
# --------------------------------------------------------------------------
def test_language_lanes_reproduce_the_frozen_oss60_partition_byte_for_byte():
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    repositories = json.loads(REPOSITORIES.read_text(encoding="utf-8"))
    plan = plan_lanes(cases, shard_by="language")
    assignment_sha, prepared_sha, assignment, per_cell = _assignment_digests(
        cases,
        repositories,
        plan,
    )

    assert plan["shard_by"] == "language"
    assert plan["lane_count"] == 4
    assert len(plan["include"]) == 20
    assert plan["dropped_empty_lanes"] == []
    assert len(assignment) == 60
    assert len({row[0] for row in assignment}) == 60
    assert min(cell["n"] for cell in per_cell.values()) > 0
    assert assignment_sha == OSS60_ASSIGNMENT_SHA256
    assert prepared_sha == OSS60_PREPARED_SHA256


def test_language_mode_is_the_default_so_an_unset_flag_cannot_repartition():
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    repositories = json.loads(REPOSITORIES.read_text(encoding="utf-8"))

    explicit = prepare_shard(
        cases,
        repositories,
        language="go",
        lane_index=1,
        lane_count=4,
        shard_by="language",
    )
    defaulted = prepare_shard(
        cases,
        repositories,
        language="go",
        lane_index=1,
        lane_count=4,
    )

    assert explicit == defaulted
    assert plan_lanes(cases)["shard_by"] == "language"


def test_case_lanes_cover_the_294_case_corpus_once_with_no_empty_lane():
    cases = json.loads(NEW_CASES.read_text(encoding="utf-8"))
    repositories = json.loads(NEW_REPOSITORIES.read_text(encoding="utf-8"))
    plan = plan_lanes(cases, shard_by="case")
    _sha, _prepared, assignment, per_cell = _assignment_digests(
        cases,
        repositories,
        plan,
    )
    sizes = [cell["n"] for cell in per_cell.values()]
    languages = Counter(str(case.get("language") or "").lower() for case in cases)

    assert len(cases) == 294
    assert languages == {"python": 293, "typescript": 1}
    assert plan["lane_count"] == 40
    assert len(plan["include"]) == 40
    assert plan["dropped_empty_lanes"] == []
    assert len(assignment) == 294                      # every case assigned
    assert len({row[0] for row in assignment}) == 294  # exactly once
    assert Counter(row[0] for row in assignment) == Counter(
        {str(case["id"]): 1 for case in cases}
    )
    assert min(sizes) == 7 and max(sizes) == 8         # balanced by construction
    assert sum(sizes) == 294
    assert 0 not in sizes


def test_case_lanes_are_language_blind_so_one_typescript_case_does_not_isolate():
    cases = json.loads(NEW_CASES.read_text(encoding="utf-8"))
    plan = plan_lanes(cases, shard_by="case")
    lanes = {
        cell["shard"]: [
            str(case.get("language") or "")
            for case in lane_members(
                cases,
                lane_index=cell["shard"],
                lane_count=plan["lane_count"],
                shard_by="case",
            )
        ]
        for cell in plan["include"]
    }
    typescript_lanes = [lane for lane, langs in lanes.items() if "typescript" in langs]

    assert {cell["language"] for cell in plan["include"]} == {"all"}
    assert len(typescript_lanes) == 1
    # the typescript case rides a full lane instead of owning a 1-case job
    assert len(lanes[typescript_lanes[0]]) >= 7


def test_language_mode_on_a_single_language_corpus_would_blow_the_job_timeout():
    """The geometry the planner must refuse: 293 python cases in four lanes."""
    cases = json.loads(NEW_CASES.read_text(encoding="utf-8"))
    language_plan = plan_lanes(cases, shard_by="language")
    case_plan = plan_lanes(cases, shard_by="case")
    seconds_per_case = 497  # measured, run 30196352388: 29,833 s / 60 cases
    budget = 180 * 60

    worst_language = max(language_plan["lane_sizes"].values()) * seconds_per_case
    worst_case_mode = max(case_plan["lane_sizes"].values()) * seconds_per_case

    assert worst_language > budget          # 74 cases -> ~10.2 h against a 3 h cap
    assert worst_case_mode < budget         # 8 cases  -> ~66 min
    # every empty language lane is dropped rather than dispatched to fail its upload
    assert len(language_plan["dropped_empty_lanes"]) == 15
    assert language_plan["assigned_count"] == language_plan["case_count"] == 294


def test_plan_lanes_never_emits_a_case_id_or_a_gold_field():
    cases = json.loads(NEW_CASES.read_text(encoding="utf-8"))
    plan = plan_lanes(cases, shard_by="case")
    payload = json.dumps(plan)
    ids = {str(case["id"]) for case in cases}

    assert not any(case_id in payload for case_id in ids)
    assert not any(
        key in payload
        for key in ("gold_files", "gold_symbols", "gold_line_ranges", "patch_sha256")
    )
    assert {key for cell in plan["include"] for key in cell} == {"language", "shard"}


def test_lane_count_override_still_partitions_exactly_once():
    cases = json.loads(NEW_CASES.read_text(encoding="utf-8"))
    for lane_count in (24, 40, 48, 294):
        plan = plan_lanes(cases, shard_by="case", lane_count=lane_count)
        seen = Counter(
            str(case["id"])
            for cell in plan["include"]
            for case in lane_members(
                cases,
                lane_index=cell["shard"],
                lane_count=plan["lane_count"],
                shard_by="case",
            )
        )
        assert plan["lane_count"] == lane_count
        assert len(plan["include"]) == lane_count
        assert seen == Counter({str(case["id"]): 1 for case in cases})
    # a lane count past the corpus size collapses instead of emitting empty jobs
    assert plan_lanes(cases, shard_by="case", lane_count=400)["lane_count"] == 294


def test_shard_by_rejects_an_unknown_mode_and_a_languageless_language_lane():
    cases = json.loads(CASES.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="shard_by must be one of"):
        lane_members(cases, lane_index=0, lane_count=4, shard_by="repository")
    with pytest.raises(ValueError, match="shard_by=language requires a language"):
        lane_members(cases, lane_index=0, lane_count=4, shard_by="language")
    with pytest.raises(ValueError, match="lane_count must be positive"):
        lane_members(cases, lane_index=0, lane_count=0, shard_by="case")
    with pytest.raises(ValueError, match="lane_index must be within lane_count"):
        lane_members(cases, lane_index=4, lane_count=4, shard_by="case")
    with pytest.raises(ValueError, match="shard_by must be one of"):
        plan_lanes(cases, shard_by="repository")
    # a negative lane count must fail, never silently collapse to one lane
    with pytest.raises(ValueError, match="lane_count must be positive"):
        plan_lanes(cases, shard_by="case", lane_count=-5)
    with pytest.raises(ValueError, match="cannot plan lanes for an empty corpus"):
        plan_lanes([], shard_by="case")


def test_workflow_wires_the_planner_to_every_seal_lane():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    document = yaml.safe_load(workflow)
    plan_job = document["jobs"]["plan"]
    seal_job = document["jobs"]["seal"]

    assert plan_job["outputs"]["matrix"] == "${{ steps.lanes.outputs.matrix }}"
    assert plan_job["outputs"]["lane_count"] == "${{ steps.lanes.outputs.lane_count }}"
    assert plan_job["outputs"]["shard_by"] == "${{ steps.lanes.outputs.shard_by }}"
    assert seal_job["strategy"]["matrix"] == (
        "${{ fromJSON(needs.plan.outputs.matrix) }}"
    )
    assert seal_job["timeout-minutes"] == 180
    assert document["jobs"]["score"]["needs"] == ["plan", "seal"]
    assert document["jobs"]["score"]["if"] == (
        "${{ always() && needs.plan.result == 'success' }}"
    )
    # the lane the planner resolved is the lane the runner re-computes
    assert '--shard-by "$SHARD_BY"' in workflow
    assert '--lane-index "$SHARD"' in workflow
    assert '--lane-count "$LANE_COUNT"' in workflow
    assert "--shard-count 4" not in workflow
    assert "loc-vnext-${{ matrix.language }}-${{ matrix.shard }}" in workflow
