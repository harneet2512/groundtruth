from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path

from groundtruth.runtime.covering_runner import CoveringAttribution
from groundtruth.runtime.lane_attestation import (
    build_covering_candidate_input,
    finalize_covering_attestation,
    finalize_syntax_attestation,
    lane_delivery_candidate_id,
)
from groundtruth.runtime.producer_attestation import PASS, UNMEASURED, validate
from groundtruth.runtime.syntax_observation import build_syntax_observation


def _syntax_complete():
    source = b"def f(\n"
    block = 'File "src/mod.py", line 1\n    def f(\n         ^\nSyntaxError: never closed'
    observation = build_syntax_observation(
        file_path="src/mod.py",
        source_bytes=source,
        check_result={
            "verdict": "syntax_error",
            "diagnostic": block,
            "language": ".py",
            "reason": "parse_error",
            "checker": ["ast.parse"],
        },
        actual_event="edit_result",
        rendered_block=block,
    )
    return observation, source, block


def test_syntax_factory_passes_only_exact_complete_join():
    observation, source, block = _syntax_complete()

    final = finalize_syntax_attestation(
        observation,
        source_bytes=source,
        producer_block=block,
        shipped_suffix=block,
        target="src/mod.py",
        candidate_id=lane_delivery_candidate_id("edit.syntax", "src/mod.py", block),
        delivery_seal=hashlib.sha256(block.encode()).hexdigest()[:16],
    )

    assert validate(final.attestation) == ()
    assert final.attestation.truth_verdict == PASS
    assert final.attestation.freshness_verdict == PASS
    assert final.attestation.decision.open_event == "edit_result"
    artifacts = final.artifact_mapping()
    assert artifacts["producer-candidate.bin"] == block.encode()
    assert artifacts["shipped-suffix.bin"] == block.encode()
    for ref in final.attestation.source_artifacts:
        assert hashlib.sha256(artifacts[ref.artifact_id]).hexdigest() == ref.sha256


def test_syntax_factory_downgrades_mismatch_or_missing_to_unmeasured():
    observation, source, block = _syntax_complete()
    for changed_source, changed_block in ((b"changed", block), (source, block + "!")):
        final = finalize_syntax_attestation(
            observation,
            source_bytes=changed_source,
            producer_block=changed_block,
            shipped_suffix=changed_block,
            target="src/mod.py",
            candidate_id=lane_delivery_candidate_id(
                "edit.syntax", "src/mod.py", changed_block
            ),
            delivery_seal=hashlib.sha256(changed_block.encode()).hexdigest()[:16],
        )
        assert validate(final.attestation) == ()
        assert final.attestation.truth_verdict == UNMEASURED
        assert final.attestation.freshness_verdict == UNMEASURED
        assert all(not predicate.proof_refs for predicate in (
            *final.attestation.truth_predicates,
            *final.attestation.freshness_predicates,
        ))


def _covering_complete():
    current = {
        "executed": True,
        "verdict": "fail",
        "files": ["tests/test_mod.py"],
        "ran": ["tests/test_mod.py"],
        "exit_code": 1,
        "stdout_tail": "src/mod.py:4: in f\nE   TypeError: broken",
        "stderr_tail": "",
    }
    attribution = CoveringAttribution(
        attributed=True,
        method="trace_frame",
        current_verdict="fail",
        base_verdict="not_run",
        implicated_edited_paths=("src/mod.py",),
        covering_files=("tests/test_mod.py",),
    )
    block = "src/mod.py:4: in f\nE   TypeError: broken"
    candidate = build_covering_candidate_input(
        attribution=attribution,
        current_result=current,
        edited_sources={"src/mod.py": (b"def f():\n    raise TypeError\n", "edit:7")},
        covering_files=["tests/test_mod.py"],
        actual_event="edit_result",
        rendered_block=block,
    )
    return candidate, block


def test_covering_factory_binds_result_sources_files_event_and_rendered_candidate():
    candidate, block = _covering_complete()

    final = finalize_covering_attestation(
        candidate,
        producer_block=block,
        shipped_suffix=block,
        target="src/mod.py",
        candidate_id=lane_delivery_candidate_id(
            "verify.horizon.executed", "src/mod.py", block
        ),
        delivery_seal=hashlib.sha256(block.encode()).hexdigest()[:16],
    )

    assert validate(final.attestation) == ()
    assert final.attestation.truth_verdict == PASS
    assert final.attestation.freshness_verdict == PASS
    assert final.attestation.decision.open_event == "edit_result"
    artifacts = final.artifact_mapping()
    assert json.loads(artifacts["current-result.json"])["verdict"] == "fail"
    assert artifacts["producer-candidate.bin"] == block.encode()
    assert artifacts["shipped-suffix.bin"] == block.encode()
    for ref in final.attestation.source_artifacts:
        assert hashlib.sha256(artifacts[ref.artifact_id]).hexdigest() == ref.sha256


def test_covering_factory_incomplete_attribution_or_render_is_unmeasured():
    candidate, block = _covering_complete()
    incomplete = dataclasses.replace(
        candidate,
        attribution=dataclasses.replace(candidate.attribution, attributed=False),
    )
    for value, rendered in ((incomplete, block), (candidate, block + "!")):
        final = finalize_covering_attestation(
            value,
            producer_block=rendered,
            shipped_suffix=rendered,
            target="src/mod.py",
            candidate_id=lane_delivery_candidate_id(
                "verify.horizon.executed", "src/mod.py", rendered
            ),
            delivery_seal=hashlib.sha256(rendered.encode()).hexdigest()[:16],
        )
        assert validate(final.attestation) == ()
        assert final.attestation.truth_verdict == UNMEASURED
        assert final.attestation.freshness_verdict == UNMEASURED


def test_covering_seam_stages_exact_input_only_for_real_candidate(tmp_path, monkeypatch):
    artifact = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
    if artifact not in sys.path:
        sys.path.insert(0, artifact)
    import gt_mini_patch as gmp
    from groundtruth.runtime import covering_runner

    source = tmp_path / "src/mod.py"
    test_file = tmp_path / "tests/test_mod.py"
    source.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source.write_bytes(b"def f():\n    raise TypeError\n")
    test_file.write_text("", encoding="utf-8")
    current = {
        "executed": True,
        "verdict": "fail",
        "files": ["tests/test_mod.py"],
        "ran": ["tests/test_mod.py"],
        "exit_code": 1,
        "stdout_tail": "src/mod.py:4: in f\nE   TypeError: broken",
        "stderr_tail": "",
    }
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(gmp, "_GT_BASELINE", False)
    monkeypatch.setattr(gmp, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(gmp, "_last_test_step", -1)
    monkeypatch.setattr(gmp, "_action_count", 1)
    monkeypatch.setattr(gmp, "_build_verification_executor", lambda: None)
    monkeypatch.setattr(gmp, "_ss_ack_failure_suppresses", lambda *a, **k: False)
    monkeypatch.setattr(covering_runner, "run_covering_tests", lambda *a, **k: current)

    block = gmp._executed_covering_emission(
        [{"file": "tests/test_mod.py"}], {"src/mod.py"}, {"f"}
    )

    assert block
    staged = gmp._last_covering_candidate_input
    assert staged is not None
    assert staged.rendered_sha256_16 == hashlib.sha256(block.encode()).hexdigest()[:16]
    assert staged.actual_event == "edit_result"
    assert staged.current_result_sha256 == hashlib.sha256(staged.current_result_bytes).hexdigest()


def test_finalizer_accepts_only_the_lane_boundary_newline_transformation():
    observation, source, block = _syntax_complete()
    shipped = "\n" + block
    final = finalize_syntax_attestation(
        observation,
        source_bytes=source,
        producer_block=block,
        shipped_suffix=shipped,
        target="src/mod.py",
        candidate_id=lane_delivery_candidate_id("edit.syntax", "src/mod.py", shipped),
        delivery_seal=hashlib.sha256(shipped.encode()).hexdigest()[:16],
    )
    assert final.attestation.truth_verdict == PASS
    assert final.artifact_mapping()["shipped-suffix.bin"] == shipped.encode()

    invalid = finalize_syntax_attestation(
        observation,
        source_bytes=source,
        producer_block=block,
        shipped_suffix="\n\n" + block,
        target="src/mod.py",
        candidate_id=lane_delivery_candidate_id(
            "edit.syntax", "src/mod.py", "\n\n" + block
        ),
        delivery_seal=hashlib.sha256(("\n\n" + block).encode()).hexdigest()[:16],
    )
    assert invalid.attestation.truth_verdict == UNMEASURED


def test_syntax_factory_rejects_wrong_seal_candidate_or_event():
    observation, source, block = _syntax_complete()
    cases = (
        (
            observation,
            lane_delivery_candidate_id("edit.syntax", "src/mod.py", block),
            "0" * 16,
        ),
        (observation, "syntax:wrong", hashlib.sha256(block.encode()).hexdigest()[:16]),
        (
            dataclasses.replace(observation, actual_event="test_result"),
            lane_delivery_candidate_id("edit.syntax", "src/mod.py", block),
            hashlib.sha256(block.encode()).hexdigest()[:16],
        ),
    )
    for value, candidate_id, seal in cases:
        final = finalize_syntax_attestation(
            value,
            source_bytes=source,
            producer_block=block,
            shipped_suffix=block,
            target="src/mod.py",
            candidate_id=candidate_id,
            delivery_seal=seal,
        )
        assert validate(final.attestation) == ()
        assert final.attestation.truth_verdict == UNMEASURED
        assert final.attestation.freshness_verdict == UNMEASURED


def test_covering_factory_rejects_file_or_event_join_mismatch():
    candidate, block = _covering_complete()
    cases = (
        dataclasses.replace(candidate, covering_files=("tests/other.py",)),
        dataclasses.replace(candidate, actual_event="test_result"),
    )
    for value in cases:
        final = finalize_covering_attestation(
            value,
            producer_block=block,
            shipped_suffix=block,
            target="src/mod.py",
            candidate_id=lane_delivery_candidate_id(
                "verify.horizon.executed", "src/mod.py", block
            ),
            delivery_seal=hashlib.sha256(block.encode()).hexdigest()[:16],
        )
        assert validate(final.attestation) == ()
        assert final.attestation.truth_verdict == UNMEASURED
        assert final.attestation.freshness_verdict == UNMEASURED


def test_covering_flag_off_clears_stale_stage_without_running(monkeypatch):
    artifact = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
    if artifact not in sys.path:
        sys.path.insert(0, artifact)
    import gt_mini_patch as gmp

    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    monkeypatch.setattr(gmp, "_GT_BASELINE", False)
    monkeypatch.setattr(gmp, "_last_covering_candidate_input", object(), raising=False)

    assert gmp._executed_covering_emission([], [], []) is None
    assert gmp._last_covering_candidate_input is None
