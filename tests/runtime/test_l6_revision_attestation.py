from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib

import pytest

from groundtruth.runtime.l6_revision_attestation import (
    L6_REVISION_ATTESTATION_SCHEMA,
    DbIdentity,
    L6RevisionAttestation,
    L6RevisionAttestationError,
    SubprocessResult,
    canonical_bytes,
    canonical_sha256,
    from_dict,
    to_dict,
    validate,
)


def _sha(char: str) -> str:
    return char * 64


def _valid() -> L6RevisionAttestation:
    return L6RevisionAttestation(
        schema=L6_REVISION_ATTESTATION_SCHEMA,
        action_count=7,
        graph_generation=3,
        repo_root="/workspace/repo",
        edited_path="src/example.py",
        edited_source_sha256=_sha("f"),
        source_db=DbIdentity(path="/gt_artifacts/graph.db", sha256=_sha("a")),
        selected_db_before=DbIdentity(path="/tmp/gt_work.db", sha256=_sha("a")),
        selected_db_after=DbIdentity(path="/tmp/gt_work.db", sha256=_sha("b")),
        subprocess=SubprocessResult(
            argv=(
                "/tmp/gt-index",
                "-root=/workspace/repo",
                "-file=src/example.py",
                "-output=/tmp/gt_work.db",
            ),
            returncode=0,
            timed_out=False,
            exception_type="",
            stdout_sha256=_sha("c"),
            stdout_bytes=12,
            stderr_sha256=hashlib.sha256(b"").hexdigest(),
            stderr_bytes=0,
        ),
    )


def test_valid_carrier_is_frozen_canonical_and_round_trips() -> None:
    attestation = _valid()

    assert validate(attestation) == ()
    assert from_dict(to_dict(attestation)) == attestation
    assert canonical_bytes(attestation) == canonical_bytes(from_dict(to_dict(attestation)))
    assert len(canonical_sha256(attestation)) == 64
    assert b"ss_live" not in canonical_bytes(attestation)
    with pytest.raises(FrozenInstanceError):
        attestation.graph_generation = 4  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (
            lambda a: replace(a, source_db=replace(a.source_db, sha256="a" * 63)),
            "source_db:sha256:not_64_lower_hex",
        ),
        (
            lambda a: replace(a, selected_db_after=a.source_db),
            "selected_db_after:path:mismatch",
        ),
        (
            lambda a: replace(a, action_count=-1),
            "action_count:not_nonnegative_int",
        ),
        (
            lambda a: replace(a, graph_generation=True),
            "graph_generation:not_nonnegative_int",
        ),
        (
            lambda a: replace(a, edited_source_sha256="f" * 63),
            "edited_source_sha256:not_64_lower_hex",
        ),
        (
            lambda a: replace(a, repo_root=""),
            "repo_root:invalid",
        ),
        (
            lambda a: replace(
                a, subprocess=replace(a.subprocess, returncode=None)
            ),
            "subprocess:returncode:not_int",
        ),
        (
            lambda a: replace(
                a,
                subprocess=replace(
                    a.subprocess, timed_out=True, returncode=0
                ),
            ),
            "subprocess:timed_out:returncode_forbidden",
        ),
        (
            lambda a: replace(
                a,
                subprocess=replace(
                    a.subprocess, exception_type="OSError", returncode=0
                ),
            ),
            "subprocess:exception:returncode_forbidden",
        ),
    ],
)
def test_validation_fails_closed_on_incomplete_or_contradictory_evidence(
    changed, expected: str
) -> None:
    errors = validate(changed(_valid()))
    assert expected in errors


def test_failed_subprocess_is_recordable_but_not_a_success_claim() -> None:
    failed = replace(
        _valid(),
        selected_db_after=_valid().selected_db_before,
        subprocess=replace(_valid().subprocess, returncode=2),
    )

    assert validate(failed) == ()
    assert failed.reindex_succeeded is False


def test_success_and_revision_advance_are_distinct_observations() -> None:
    unchanged = replace(_valid(), selected_db_after=_valid().selected_db_before)

    assert validate(unchanged) == ()
    assert unchanged.reindex_succeeded is True
    assert unchanged.revision_advanced is False


def test_commitment_binds_action_generation_and_full_db_revision() -> None:
    original = _valid()

    assert canonical_sha256(original) != canonical_sha256(
        replace(original, action_count=8)
    )
    assert canonical_sha256(original) != canonical_sha256(
        replace(original, graph_generation=4)
    )
    assert canonical_sha256(original) != canonical_sha256(
        replace(original, edited_source_sha256=_sha("1"))
    )
    assert canonical_sha256(original) != canonical_sha256(
        replace(
            original,
            repo_root="/workspace/other",
            subprocess=replace(
                original.subprocess,
                argv=tuple(
                    "-root=/workspace/other" if value.startswith("-root=") else value
                    for value in original.subprocess.argv
                ),
            ),
        )
    )
    assert canonical_sha256(original) != canonical_sha256(
        replace(
            original,
            selected_db_after=replace(original.selected_db_after, sha256=_sha("e")),
        )
    )


def test_from_dict_rejects_unknown_missing_and_coerced_fields() -> None:
    raw = to_dict(_valid())
    raw["promote_ss_live"] = True
    with pytest.raises(L6RevisionAttestationError, match="unknown_fields"):
        from_dict(raw)

    raw = to_dict(_valid())
    del raw["source_db"]
    with pytest.raises(L6RevisionAttestationError, match="missing_fields"):
        from_dict(raw)

    raw = to_dict(_valid())
    raw["action_count"] = "7"
    with pytest.raises(L6RevisionAttestationError, match="action_count"):
        from_dict(raw)


def test_paths_and_argv_reject_empty_nul_and_unordered_types() -> None:
    bad_path = replace(_valid(), edited_path="src/\x00bad.py")
    assert "edited_path:invalid" in validate(bad_path)

    bad_argv = replace(
        _valid(), subprocess=replace(_valid().subprocess, argv=("",))
    )
    assert "subprocess:argv[0]:invalid" in validate(bad_argv)

    raw = to_dict(_valid())
    raw["subprocess"]["argv"] = {"gt-index": True}  # type: ignore[index]
    with pytest.raises(L6RevisionAttestationError, match="argv"):
        from_dict(raw)


def test_invocation_must_target_the_attested_file_and_selected_database() -> None:
    wrong_file = replace(
        _valid(),
        subprocess=replace(
            _valid().subprocess,
            argv=tuple(
                "-file=src/other.py" if value.startswith("-file=") else value
                for value in _valid().subprocess.argv
            ),
        ),
    )
    assert "subprocess:argv:edited_path_unbound" in validate(wrong_file)

    wrong_output = replace(
        _valid(),
        subprocess=replace(
            _valid().subprocess,
            argv=tuple(
                "-output=/tmp/other.db" if value.startswith("-output=") else value
                for value in _valid().subprocess.argv
            ),
        ),
    )
    assert "subprocess:argv:selected_db_unbound" in validate(wrong_output)

    wrong_root = replace(
        _valid(),
        subprocess=replace(
            _valid().subprocess,
            argv=tuple(
                "-root=/workspace/other" if value.startswith("-root=") else value
                for value in _valid().subprocess.argv
            ),
        ),
    )
    assert "subprocess:argv:repo_root_unbound" in validate(wrong_root)


def test_empty_stream_digest_must_commit_to_empty_bytes() -> None:
    inconsistent = replace(
        _valid(), subprocess=replace(_valid().subprocess, stderr_sha256=_sha("d"))
    )

    assert "subprocess:stderr_sha256:not_empty_digest" in validate(inconsistent)
