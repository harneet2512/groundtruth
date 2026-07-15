from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"


def doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def step_run(token: str) -> str:
    for job in doc()["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run") if isinstance(step, dict) else None
            if run and token in run:
                return run
    raise AssertionError(f"missing workflow step containing {token!r}")


def test_prepare_refuses_wrong_arm_or_concurrency_before_matrix() -> None:
    workflow = doc()
    steps = workflow["jobs"]["prepare"]["steps"]
    guard_index = next(
        i for i, step in enumerate(steps)
        if "GT_PAID_IDENTITY_PRECHECK_FAILED" in (step.get("run") or "")
    )
    matrix_index = next(i for i, step in enumerate(steps) if step.get("id") == "gen")
    assert guard_index < matrix_index
    guard = steps[guard_index]
    assert guard["env"]["GT_INPUT_BASELINE"] == "${{ inputs.baseline }}"
    assert guard["env"]["GT_INPUT_MAX_PARALLEL"] == "${{ inputs.max_parallel }}"
    assert '"$GT_INPUT_BASELINE" = "true"' in guard["run"]
    assert '"$GT_INPUT_MAX_PARALLEL" != "20"' in guard["run"]
    assert "exit 1" in guard["run"]
    assert workflow["jobs"]["trial"]["strategy"]["max-parallel"] == 20


def test_gt_on_commit_identity_is_exact_at_all_three_gates() -> None:
    workflow = doc()
    assert workflow["env"]["GT_REQUIRE_COMMIT_PARITY"] == (
        "${{ inputs.baseline == true && '0' || '1' }}"
    )
    producer = step_run("gt.run_identity.v1")
    paid = step_run("_GT_PROFILE_EXPORTS")
    completion = step_run("GT_TASK_COMPLETION_INVALID:run_identity")
    for body in (producer, paid, completion):
        for token in (
            "commit_parity_recorded",
            "substrate_build_commit",
            "run_commit",
            "status",
            "match",
        ):
            assert token in body
    assert "workflow_dispatch_matches_prepared" in producer
    assert "workflow_dispatch_matches_prepared" in paid


def test_substrate_digest_is_independently_observed_and_seals_image_id() -> None:
    producer = step_run("gt.run_identity.v1")
    assert 'grep -Fx "$EXPECTED_DIGEST"' in producer
    assert 'ACTUAL_DIGEST="$EXPECTED_DIGEST"' not in producer
    assert "SUBSTRATE_IMAGE_ID" in producer
    assert "substrate_image_id" in producer
    assert "substrate_image_id" in step_run("_GT_PROFILE_EXPORTS")
    assert "substrate_image_id" in step_run("GT_TASK_COMPLETION_INVALID:run_identity")


def test_concurrency_and_task_image_are_sealed_before_immutable_run() -> None:
    producer = step_run("gt.run_identity.v1")
    paid = step_run("_GT_PROFILE_EXPORTS")
    completion = step_run("GT_TASK_COMPLETION_INVALID:run_identity")
    assert "max_parallel_requested" in producer
    assert "max_parallel_effective" in producer
    for token in (
        "TASK_IMAGE_REQUESTED",
        "TASK_IMAGE_REPODIGEST",
        "TASK_IMAGE_ID",
        "TASK_IMAGE_IMMUTABLE",
        "task_image_requested",
        "task_image_repo_digest",
        "task_image_id",
        "GT_TASK_IMAGE_IDENTITY_FAIL",
    ):
        assert token in paid
    docker_tail = paid[paid.index("docker run --rm") :]
    assert '"$TASK_IMAGE_IMMUTABLE"' in docker_tail
    assert '"$_GHCR_IMG"' not in docker_tail[: docker_tail.index("bash -c '")]
    for token in (
        "max_parallel_requested",
        "max_parallel_effective",
        "task_image_repo_digest",
        "task_image_id",
    ):
        assert token in completion
    assert "TASK_IMAGE_IMMUTABLE_ID" in paid
    assert 'docker image inspect --format \'{{.Id}}\' "$TASK_IMAGE_IMMUTABLE"' in paid
    assert '"$TASK_IMAGE_IMMUTABLE_ID" != "$TASK_IMAGE_ID"' in paid
    assert 'TASK_IMAGE_CANONICAL_REPOSITORY="${TASK_IMAGE_REPOSITORY#docker.io/}"' in paid
    assert (
        'case "$candidate" in "${TASK_IMAGE_REPOSITORY}@sha256:"*|'
        '"${TASK_IMAGE_CANONICAL_REPOSITORY}@sha256:"*'
    ) in paid


def test_task_image_fallback_seals_the_reference_that_actually_pulled() -> None:
    paid = step_run("_GT_PROFILE_EXPORTS")
    ghcr_branch = paid[paid.index('if docker pull "$_GHCR_IMG"'):]
    fallback_branch = ghcr_branch[ghcr_branch.index('elif docker pull "$_DH_IMG"'):]
    assert 'TASK_IMAGE_REQUESTED="$_GHCR_IMG"' in ghcr_branch[: ghcr_branch.index("elif docker pull")]
    assert 'TASK_IMAGE_REQUESTED="$_DH_IMG"' in fallback_branch[: fallback_branch.index("else")]
    assert '_GHCR_IMG="$_DH_IMG"' not in fallback_branch[: fallback_branch.index("else")]
    requested = (
        "ghcr.io/acme/sweb.eval.x86_64.repo_1776_task:latest",
        "docker.io/starryzhang/sweb.eval.x86_64.repo_1776_task:latest",
    )
    canonical = tuple(ref.rsplit(":", 1)[0].removeprefix("docker.io/") for ref in requested)
    assert canonical == (
        "ghcr.io/acme/sweb.eval.x86_64.repo_1776_task",
        "starryzhang/sweb.eval.x86_64.repo_1776_task",
    )
    dockerhub_candidates = (
        "docker.io/starryzhang/sweb.eval.x86_64.repo_1776_task@sha256:" + "a" * 64,
        "starryzhang/sweb.eval.x86_64.repo_1776_task@sha256:" + "b" * 64,
    )
    accepted_prefixes = tuple(f"{repo}@sha256:" for repo in (requested[1].rsplit(":", 1)[0], canonical[1]))
    assert all(candidate.startswith(accepted_prefixes) for candidate in dockerhub_candidates)
    assert not ("unrelated/image@sha256:" + "c" * 64).startswith(accepted_prefixes)


def test_task_image_identity_enrichment_fails_closed_and_revalidates_stored_values() -> None:
    paid = step_run("_GT_PROFILE_EXPORTS")
    start = paid.index("GT_TASK_IDENTITY_TASK=")
    end = paid.index("docker run --rm", start)
    enrichment = paid[start:end]
    assert "if ! GT_TASK_IDENTITY_TASK=" in enrichment
    writer_marker = enrichment.index("GT_TASK_IMAGE_IDENTITY_FAIL: identity enrichment failed")
    writer_failure = enrichment[writer_marker : enrichment.index("fi", writer_marker)]
    assert "exit 1" in writer_failure
    stored_marker = enrichment.index("GT_TASK_IMAGE_IDENTITY_FAIL: stored identity validation failed")
    stored_failure = enrichment[stored_marker : enrichment.index("fi", stored_marker)]
    assert "exit 1" in stored_failure
    for token in (
        "stored_task_image_requested",
        "stored_task_image_repo_digest",
        "stored_task_image_id",
        "stored_task",
    ):
        assert token in enrichment
