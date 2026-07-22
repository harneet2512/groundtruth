"""Cluster-2a: two verified seam defects that starved producer attestation bundles.

DEFECT 1 (Gateway ProducerInputs candidate binding across provenance sanitization).
  ``_ss_sanitize_gateway_envelope`` re-derives the envelope ``dedup_key`` from the cleaned
  payload/provenance but historically left the typed ``ProducerInputs.candidate_id`` bound to
  the PRE-sanitize key. ``build_gateway_attestation`` requires ``inputs.candidate_id ==
  envelope.dedup_key`` and RAISES otherwise; the persist wrapper swallows the raise -> 0 bundles
  -> ``correct_info`` never True (run #2 zero-bundle state under GT_SS_PROVENANCE / Profile-2).

DEFECT 2 (covering-red source starvation).
  The covering completeness contract requires ``edited_sources`` to COVER
  ``attribution.implicated_edited_paths`` with exact post-edit sha256/length/revision. The seam
  historically passed only edited files that stayed byte-identical across the covering run
  (``_unchanged_sources``), which failed to guarantee the implicated set was present and let one
  UNRELATED changed edited file empty the source set -> UNMEASURED.

Both tests are artifact-first: each reproduces the zero/UNMEASURED state (RED) and then proves the
fixed seam yields a persisted PASS bundle / complete attestation (GREEN), with biting mutations.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from groundtruth.runtime.covering_runner import CoveringAttribution
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope
from groundtruth.runtime.gateway_attestation_factory import build_gateway_attestation
from groundtruth.runtime.lane_attestation import (
    build_covering_candidate_input,
    finalize_covering_attestation,
    lane_delivery_candidate_id,
)
from groundtruth.runtime.producer_attestation import PASS, UNMEASURED, validate
from groundtruth.runtime.producer_inputs import (
    PRODUCER_INPUTS_SCHEMA,
    CallerEvidenceRow,
    ProducerInputs,
    SignatureChange,
    SourceState,
)

import gt_mini_patch as G


# --------------------------------------------------------------------------- #
# DEFECT 1 — Gateway ProducerInputs candidate binding across sanitization
# --------------------------------------------------------------------------- #
def _source(file: str, token: str) -> SourceState:
    return SourceState(file=file, sha256=token * 64, revision=f"source:{token * 64}")


def _caller_break_envelope() -> EvidenceEnvelope:
    """A caller_break envelope carrying a GOOD caller row plus a generated-dir caller row
    that the leak filter passes but provenance sanitization removes."""
    env = EvidenceEnvelope.build(
        producer="caller_contract",
        fact_id="get_user",
        target="src/api.py",
        evidence_type="caller_break",
        payload=("get_user() signature changed - callers must update the call sites",),
        provenance=(("src/caller.py", 12), ("build/gen_caller.py", 5)),
        confidence=0.95,
        tier="WARNING",
        graph_revision="graph-9",
        preferred_event="edit",
    )
    inputs = ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA,
        evidence_type="caller_break",
        candidate_id=env.dedup_key,
        before_state=_source("src/api.py", "a"),
        after_state=_source("src/api.py", "b"),
        caller_rows=(
            CallerEvidenceRow(
                identity="use", file="src/caller.py", line=12, confidence=0.95,
                resolution_method="import", source_state=_source("src/caller.py", "c"),
                edge_id=11, definition_id=4,
            ),
            CallerEvidenceRow(
                identity="gen", file="build/gen_caller.py", line=5, confidence=0.9,
                resolution_method="import",
                source_state=_source("build/gen_caller.py", "d"),
                edge_id=12, definition_id=6,
            ),
        ),
        graph_revision="graph-9",
        signature_changes=(SignatureChange(
            symbol="get_user", edited_file="src/api.py",
            before_parameters=("uid",), after_parameters=("uid", "name"),
            old_min_params=None, old_max_params=None,
            new_min_params=None, new_max_params=None, positional_args=None,
        ),),
    )
    return dataclasses.replace(env, producer_inputs=inputs)


def test_defect1_red_stale_candidate_binding_makes_factory_raise() -> None:
    """RED: an envelope whose producer_inputs.candidate_id no longer matches the (re-derived)
    dedup_key makes build_gateway_attestation raise -> the run-#2 zero-bundle state."""
    env = _caller_break_envelope()
    stale = dataclasses.replace(env, dedup_key="0000000000000000")  # re-derive mismatch
    shipped = b"get_user() signature changed - callers must update the call sites\n"
    with pytest.raises(ValueError, match="candidate mismatch"):
        build_gateway_attestation(
            stale,
            delivery_seal=hashlib.sha256(shipped).hexdigest()[:16],
            shipped_bytes=shipped,
            actual_event="edit_result", open_event="edit_result",
        )


def test_defect1_green_sanitize_rebinds_and_bundle_persists(tmp_path, monkeypatch) -> None:
    """GREEN: with GT_SS_PROVENANCE on, sanitization re-derives the dedup_key AND re-binds the
    typed producer inputs to it (pruning the generated-dir caller) so the factory produces a
    PASS bundle that persists via persist_attestation."""
    from groundtruth.runtime.attestation_store import persist_attestation

    monkeypatch.setenv("GT_SS_PROVENANCE", "1")
    env = _caller_break_envelope()
    sanitized = G._ss_sanitize_gateway_envelope(env, "")

    assert sanitized is not None and sanitized is not env
    assert sanitized.dedup_key != env.dedup_key  # re-derived from cleaned provenance
    assert sanitized.provenance == (("src/caller.py", 12),)  # bad path pruned
    inputs = sanitized.producer_inputs
    assert inputs is not None
    # MUTATION (wrong candidate_id binding): the sidecar is re-bound to the NEW key.
    assert inputs.candidate_id == sanitized.dedup_key
    # MUTATION (attest only delivered evidence): the generated-dir caller is pruned.
    assert tuple((r.file, r.line) for r in inputs.caller_rows) == (("src/caller.py", 12),)

    shipped = (
        b"get_user() signature changed - callers must update the call sites\n"
        b"src/caller.py:12:use\n"
    )
    attestation, artifacts = build_gateway_attestation(
        sanitized,
        delivery_seal=hashlib.sha256(shipped).hexdigest()[:16],
        shipped_bytes=shipped,
        actual_event="edit_result", open_event="edit_result",
    )
    assert attestation.truth_verdict == PASS
    assert attestation.freshness_verdict == PASS
    assert attestation.candidate_id == sanitized.dedup_key
    # The pruned generated-dir path must not leak into any persisted artifact bytes.
    assert not any(b"build/gen_caller.py" in raw for raw in artifacts.values())

    stored = persist_attestation(attestation, artifacts, str(tmp_path))
    assert stored.bundle_dir.exists()  # bundle persisted, not swallowed


def test_defect1_failclosed_drops_sidecar_when_no_caller_survives(monkeypatch) -> None:
    """When EVERY typed caller row is a sanitized-away path, the rebind fails closed: the
    sidecar is dropped rather than attesting a caller the delivery no longer carries."""
    monkeypatch.setenv("GT_SS_PROVENANCE", "1")
    env = EvidenceEnvelope.build(
        producer="caller_contract", fact_id="get_user", target="src/api.py",
        evidence_type="caller_break",
        payload=("get_user() signature changed", "src/caller.py:12 update it"),
        provenance=(("src/caller.py", 12), ("build/gen.py", 5)),
        confidence=0.95, tier="WARNING", graph_revision="graph-9", preferred_event="edit",
    )
    inputs = ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA, evidence_type="caller_break",
        candidate_id=env.dedup_key,
        before_state=_source("src/api.py", "a"), after_state=_source("src/api.py", "b"),
        caller_rows=(CallerEvidenceRow(
            identity="gen", file="build/gen.py", line=5, confidence=0.9,
            resolution_method="import", source_state=_source("build/gen.py", "d"),
            edge_id=12, definition_id=6,
        ),),
        graph_revision="graph-9",
        signature_changes=(SignatureChange(
            symbol="get_user", edited_file="src/api.py",
            before_parameters=("uid",), after_parameters=("uid", "name"),
            old_min_params=None, old_max_params=None,
            new_min_params=None, new_max_params=None, positional_args=None,
        ),),
    )
    env = dataclasses.replace(env, producer_inputs=inputs)
    sanitized = G._ss_sanitize_gateway_envelope(env, "")
    assert sanitized is not None
    # Fail closed: no surviving caller row -> sidecar dropped (never fabricated).
    assert sanitized.producer_inputs is None


# --------------------------------------------------------------------------- #
# DEFECT 2 — covering-red source starvation
# --------------------------------------------------------------------------- #
def _covering_bits(tmp_path):
    root = str(tmp_path)
    (tmp_path / "src").mkdir()
    edit_bytes = b"def get_user(uid, name):\n    return [uid]\n"  # POST-EDIT (after != before)
    (tmp_path / "src" / "api.py").write_bytes(edit_bytes)
    (tmp_path / "src" / "unrelated.py").write_bytes(b"x = 1\n")
    sources_before = {
        "src/api.py": (edit_bytes, "edit:" + hashlib.sha256(edit_bytes).hexdigest()),
        "src/unrelated.py": (b"x = 1\n", "edit:" + hashlib.sha256(b"x = 1\n").hexdigest()),
    }
    return root, edit_bytes, sources_before


def test_defect2_red_empty_edited_sources_is_unmeasured(tmp_path) -> None:
    """RED: the starved seam passed an EMPTY edited_sources -> completeness is UNMEASURED."""
    root, _edit, _sb = _covering_bits(tmp_path)
    attribution = CoveringAttribution(
        attributed=True, method="trace_frame", current_verdict="fail", base_verdict="not_run",
        implicated_edited_paths=("src/api.py",), covering_files=("tests/test_api.py",),
    )
    cres = {
        "executed": True, "verdict": "fail", "files": ["tests/test_api.py"],
        "ran": ["tests/test_api.py"], "exit_code": 1,
        "stdout_tail": "src/api.py:2: in get_user\nE   TypeError", "stderr_tail": "",
    }
    block = "src/api.py:2: in get_user\nE   TypeError"
    starved = build_covering_candidate_input(
        attribution=attribution, current_result=cres,
        edited_sources={},  # the starvation
        covering_files=["tests/test_api.py"], actual_event="edit_result", rendered_block=block,
    )
    final = finalize_covering_attestation(
        starved, producer_block=block, shipped_suffix=block, target="src/api.py",
        candidate_id=lane_delivery_candidate_id("verify.horizon.executed", "src/api.py", block),
        delivery_seal=hashlib.sha256(block.encode()).hexdigest()[:16],
    )
    assert final.attestation.truth_verdict == UNMEASURED
    assert final.attestation.freshness_verdict == UNMEASURED


def test_defect2_green_implicated_sources_yield_complete(tmp_path) -> None:
    """GREEN: the fixed selection scopes to the implicated post-edit source -> complete=True."""
    root, _edit, sources_before = _covering_bits(tmp_path)
    # An UNRELATED edited file changes during the covering run — must NOT invalidate.
    (tmp_path / "src" / "unrelated.py").write_bytes(b"x = 2\n")

    implicated, sources, drift = G._covering_implicated_sources(
        ("src/api.py",), sources_before, root)
    assert drift == ""
    assert set(sources) == {"src/api.py"}  # unrelated churn ignored (regression guard)

    attribution = CoveringAttribution(
        attributed=True, method="trace_frame", current_verdict="fail", base_verdict="not_run",
        implicated_edited_paths=implicated, covering_files=("tests/test_api.py",),
    )
    cres = {
        "executed": True, "verdict": "fail", "files": ["tests/test_api.py"],
        "ran": ["tests/test_api.py"], "exit_code": 1,
        "stdout_tail": "src/api.py:2: in get_user\nE   TypeError", "stderr_tail": "",
    }
    block = "src/api.py:2: in get_user\nE   TypeError"
    cand = build_covering_candidate_input(
        attribution=attribution, current_result=cres, edited_sources=sources,
        covering_files=["tests/test_api.py"], actual_event="edit_result", rendered_block=block,
    )
    final = finalize_covering_attestation(
        cand, producer_block=block, shipped_suffix=block, target="src/api.py",
        candidate_id=lane_delivery_candidate_id("verify.horizon.executed", "src/api.py", block),
        delivery_seal=hashlib.sha256(block.encode()).hexdigest()[:16],
    )
    assert validate(final.attestation) == ()
    assert final.attestation.truth_verdict == PASS
    assert final.attestation.freshness_verdict == PASS


def test_defect2_mutation_missing_implicated_path_fails_closed(tmp_path) -> None:
    root, _edit, sources_before = _covering_bits(tmp_path)
    implicated, sources, drift = G._covering_implicated_sources(
        ("src/never_captured.py",), sources_before, root)
    assert drift == "covering_source_uncaptured"
    assert sources == {}


def test_defect2_mutation_tampered_source_fails_closed(tmp_path) -> None:
    root, _edit, sources_before = _covering_bits(tmp_path)
    # Tamper the implicated source AFTER capture, BEFORE emission re-hash.
    (tmp_path / "src" / "api.py").write_bytes(b"def get_user(uid, name):\n    return []\n")
    implicated, sources, drift = G._covering_implicated_sources(
        ("src/api.py",), sources_before, root)
    assert drift == "covering_source_changed_during_run"
    assert sources == {}


def test_defect2_mutation_unrelated_change_does_not_empty_source_set(tmp_path) -> None:
    """Regression guard: an unrelated changed edited file must never empty the implicated set."""
    root, _edit, sources_before = _covering_bits(tmp_path)
    (tmp_path / "src" / "unrelated.py").write_bytes(b"x = 999\n")  # unrelated churn
    _impl, sources, drift = G._covering_implicated_sources(
        ("src/api.py",), sources_before, root)
    assert drift == ""
    assert set(sources) == {"src/api.py"}
