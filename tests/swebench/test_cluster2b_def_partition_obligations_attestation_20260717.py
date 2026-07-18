"""Cluster-2b Defect-3 RED tests: correctness producers for def_partition + obligations.

Before Cluster-2b, ``def_partition`` and ``obligations`` were structurally UNABLE to prove
``correct_info`` — no producer-attestation factory bound their delivered bytes to a
re-verifiable truth basis. These tests exercise the NEW producers end-to-end (build →
persist → join) and the fail-closed mutations the task names.

Documented biting mutations (each verified to fail a test, then restored):

  * MUTATION DP-A — gateway_attestation_factory._complete_definition: drop the
    ``definition_id > 0`` guard. ``test_def_partition_row_not_consumed_by_producer`` then
    WRONGLY passes truth on a fabricated row that names no real graph node. Bite confirmed.

  * MUTATION DP-B — build_gateway_attestation def-partition branch: drop the
    ``inputs.query_identity.strip()`` requirement. ``test_def_partition_requires_query_identity``
    then passes truth with no query the definitions answer. Bite confirmed.

  * MUTATION OB-A — brief_attestation.finalize_obligations_attestation: relax the
    ``_FULL_SHA_RE.match(issue_sha)`` completeness leg. ``test_fabricated_obligations_record_is_unmeasured``
    then mints a PASS on a record with a bogus issue sha. Bite confirmed.

  * MUTATION OB-B — finalize_obligations_attestation: drop the ``full[:16] != seal`` seal
    binding. ``test_obligations_seal_must_match_block_hash`` then returns a bundle whose
    seal does not match the delivered block. Bite confirmed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import attestation_join as aj  # noqa: E402
import gt_feature_metrics as gfm  # noqa: E402
from groundtruth.runtime.attestation_store import persist_attestation  # noqa: E402
from groundtruth.runtime.brief_attestation import (  # noqa: E402
    finalize_obligations_attestation,
)
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope  # noqa: E402
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    build_lineage,
    lineage_ledger_extra,
)
from groundtruth.runtime.gateway_attestation_factory import (  # noqa: E402
    build_gateway_attestation,
)
from groundtruth.runtime.producer_attestation import PASS, UNMEASURED, validate  # noqa: E402
from groundtruth.runtime.producer_inputs import (  # noqa: E402
    PRODUCER_INPUTS_SCHEMA,
    DefinitionRow,
    ProducerInputs,
)

_GRAPH_REV = "graph-77"
_SHIPPED = b"\nsrc/api.py:41:get_user\ndef: src/api.py:41\nfact-tier callers: 3"
_SEAL = hashlib.sha256(_SHIPPED).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# def_partition (post_search) — the four gateway variants share the fact class.
# --------------------------------------------------------------------------- #
def _def_envelope(
    evidence_type: str = "def_ref_partition",
    *,
    definition_rows=None,
    query_identity: str = "get_user",
    graph_revision: str = _GRAPH_REV,
) -> EvidenceEnvelope:
    env = EvidenceEnvelope.build(
        producer=evidence_type,
        fact_id=query_identity,
        target="src/api.py",
        evidence_type=evidence_type,
        payload=("def: src/api.py:41", "fact-tier callers: 3"),
        provenance=(("src/api.py", 41),),
        graph_revision=graph_revision,
    )
    if definition_rows is None:
        definition_rows = (
            DefinitionRow(
                identity=query_identity, file="src/api.py", line=41,
                kind="Function", definition_id=101,
            ),
        )
    inputs = ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA,
        evidence_type=evidence_type,
        candidate_id=env.dedup_key,
        before_state=None,
        after_state=None,
        caller_rows=(),
        graph_revision=graph_revision,
        definition_rows=tuple(definition_rows),
        query_identity=query_identity,
    )
    return dataclasses.replace(env, producer_inputs=inputs)


def test_def_partition_builds_pass_attestation() -> None:
    env = _def_envelope()
    attestation, artifacts = build_gateway_attestation(
        env, delivery_seal=_SEAL, shipped_bytes=_SHIPPED,
        actual_event="search_result", open_event="search_result",
    )
    assert validate(attestation) == ()
    assert attestation.truth_verdict == PASS
    assert attestation.freshness_verdict == PASS
    assert attestation.evidence_type == "def_ref_partition"
    # registered producer for the canonical def_partition class is post_search.
    assert attestation.registered_producer_id == "post_search"
    assert attestation.candidate_id == env.dedup_key
    truth_paths = {
        p.field_path for p in attestation.truth_predicates[0].proof_refs
        if p.proof_type == "producer_input"
    }
    assert truth_paths == {"$.definition_rows", "$.query_identity"}
    # The persisted producer-inputs artifact carries the typed definition rows.
    ref = next(r for r in attestation.source_artifacts if r.kind == "producer_inputs")
    payload = json.loads(artifacts[ref.artifact_id])
    assert payload["definition_rows"][0]["definition_id"] == 101
    assert payload["query_identity"] == "get_user"


def test_all_four_def_partition_variants_supported() -> None:
    for et in ("def_ref_partition", "name_fold", "wrong_surface", "body_concept"):
        env = _def_envelope(et)
        attestation, _ = build_gateway_attestation(
            env, delivery_seal=_SEAL, shipped_bytes=_SHIPPED,
            actual_event="search_result", open_event="search_result",
        )
        assert attestation.truth_verdict == PASS, et
        assert attestation.evidence_type == et


def test_def_partition_row_not_consumed_by_producer() -> None:
    # MUTATION DP-A bites: a fabricated row that names no real graph node
    # (definition_id=0) must never reach truth PASS.
    env = _def_envelope(definition_rows=(
        DefinitionRow(
            identity="get_user", file="src/api.py", line=41,
            kind="Function", definition_id=0,  # not a real consumed node
        ),
    ))
    attestation, _ = build_gateway_attestation(
        env, delivery_seal=_SEAL, shipped_bytes=_SHIPPED,
        actual_event="search_result", open_event="search_result",
    )
    assert attestation.truth_verdict == UNMEASURED
    assert attestation.freshness_verdict == UNMEASURED


def test_def_partition_requires_query_identity() -> None:
    # MUTATION DP-B bites: definitions with no query identity answer no search decision.
    env = _def_envelope(query_identity="")
    attestation, _ = build_gateway_attestation(
        env, delivery_seal=_SEAL, shipped_bytes=_SHIPPED,
        actual_event="search_result", open_event="search_result",
    )
    assert attestation.truth_verdict == UNMEASURED


def test_def_partition_stale_graph_revision_is_unmeasured() -> None:
    # The definitions must be read at the SAME revision the envelope shipped.
    env = EvidenceEnvelope.build(
        producer="def_ref_partition", fact_id="get_user", target="src/api.py",
        evidence_type="def_ref_partition", payload=("def: src/api.py:41",),
        provenance=(("src/api.py", 41),), graph_revision=_GRAPH_REV,
    )
    inputs = ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA, evidence_type="def_ref_partition",
        candidate_id=env.dedup_key, before_state=None, after_state=None,
        caller_rows=(), graph_revision="graph-OTHER",  # != envelope revision
        definition_rows=(DefinitionRow(
            identity="get_user", file="src/api.py", line=41,
            kind="Function", definition_id=101),),
        query_identity="get_user",
    )
    env = dataclasses.replace(env, producer_inputs=inputs)
    attestation, _ = build_gateway_attestation(
        env, delivery_seal=_SEAL, shipped_bytes=_SHIPPED,
        actual_event="search_result", open_event="search_result",
    )
    assert attestation.truth_verdict == UNMEASURED


def test_edit_fact_input_bytes_are_byte_identical_without_def_fields() -> None:
    # An edit-fact producer (caller_break) never sets def fields, so its canonical input
    # bytes must not gain the additive def-partition keys.
    from groundtruth.runtime.gateway_attestation_factory import (
        canonical_producer_inputs_bytes,
    )
    from groundtruth.runtime.producer_inputs import CallerEvidenceRow, SignatureChange, SourceState

    def _src(f, t):
        return SourceState(file=f, sha256=t * 64, revision=f"source:{t * 64}")

    env = EvidenceEnvelope.build(
        producer="caller_contract", fact_id="get_user", target="src/api.py",
        evidence_type="caller_break", payload=("x",), provenance=(("src/caller.py", 2),),
        graph_revision="graph-9",
    )
    inputs = ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA, evidence_type="caller_break",
        candidate_id=env.dedup_key, before_state=_src("src/api.py", "a"),
        after_state=_src("src/api.py", "b"),
        caller_rows=(CallerEvidenceRow(
            identity="use", file="src/caller.py", line=2, confidence=0.95,
            resolution_method="import", source_state=_src("src/caller.py", "c"),
            edge_id=17, definition_id=4),),
        graph_revision="graph-9",
        signature_changes=(SignatureChange(
            symbol="get_user", edited_file="src/api.py",
            before_parameters=("uid",), after_parameters=("uid", "name"),
            old_min_params=None, old_max_params=None, new_min_params=None,
            new_max_params=None, positional_args=None),),
    )
    env = dataclasses.replace(env, producer_inputs=inputs)
    raw = canonical_producer_inputs_bytes(
        env, delivery_seal=_SEAL, actual_event="edit_result", open_event="edit_result")
    payload = json.loads(raw)
    assert "definition_rows" not in payload
    assert "query_identity" not in payload


# --------------------------------------------------------------------------- #
# def_partition — end-to-end join drives correct_info True.
# --------------------------------------------------------------------------- #
def _def_delivered_row(candidate_id: str, seal: str) -> dict:
    return {
        "layer": "gateway.def_ref_partition",
        "event_type": "search_result",
        "file_path": "src/api.py",
        "outcome": "delivered",
        "chars_delivered": len(_SHIPPED),
        "iteration": 3,
        "content_sha256_16": seal,
        "seal_scope": "block",
        "candidate_id": candidate_id,
        **lineage_ledger_extra(build_lineage(
            runtime_producer_id="def_ref_partition",
            evidence_type="def_ref_partition",
            actual_event="search_result")),
    }


def _write_trajectory(task_dir: Path) -> None:
    (task_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({"messages": [{"role": "user", "content": "t"}],
                    "info": {"submission": ""},
                    "trajectory_format": "mini-swe-agent"}),
        encoding="utf-8")


def _write_ledger(task_dir: Path, rows: list[dict]) -> None:
    (task_dir / "gt_runtime_ledger_synthetic.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_collect_task_def_partition_correct_info_true(tmp_path: Path) -> None:
    env = _def_envelope()
    attestation, artifacts = build_gateway_attestation(
        env, delivery_seal=_SEAL, shipped_bytes=_SHIPPED,
        actual_event="search_result", open_event="search_result")
    persist_attestation(
        attestation, artifacts, tmp_path / "art" / "producer_attestations")
    _write_trajectory(tmp_path)
    _write_ledger(tmp_path, [_def_delivered_row(env.dedup_key, _SEAL)])

    record = gfm.collect_task("synthetic__defpart", str(tmp_path), profile="2")

    fc = record["fact_classes"]["def_partition"]
    assert fc["truth_valid"]["value"] is True
    assert fc["authority_valid"]["value"] is True
    assert "def_partition" in aj.ATTESTED_FACT_CLASSES


# --------------------------------------------------------------------------- #
# obligations — the task-start block bound to the extracted record.
# --------------------------------------------------------------------------- #
_BLOCK = "<gt-obligations>\n- [ ] range() must accept negative step\n</gt-obligations>"
_BLOCK_FULL = hashlib.sha256(_BLOCK.encode("utf-8", "surrogatepass")).hexdigest()
_BLOCK_SEAL = _BLOCK_FULL[:16]
_ISSUE_SHA = hashlib.sha256(b"the issue text").hexdigest()
_OBL_DIGEST = hashlib.sha256(b'["range() must accept negative step"]').hexdigest()


def _obl(**over):
    kwargs = dict(
        candidate_id="brief:block:obligations:1",
        delivery_seal=_BLOCK_SEAL,
        block_content_sha256=_BLOCK_FULL,
        issue_sha256=_ISSUE_SHA,
        issue_revision=f"issue:{_ISSUE_SHA}",
        obligation_count=1,
        obligations_digest=_OBL_DIGEST,
    )
    kwargs.update(over)
    return finalize_obligations_attestation(**kwargs)


def test_obligations_real_record_is_pass() -> None:
    final = _obl()
    assert final is not None
    assert validate(final.attestation) == ()
    assert final.attestation.truth_verdict == PASS
    assert final.attestation.freshness_verdict == PASS
    assert final.attestation.evidence_type == "obligations"
    assert final.attestation.registered_producer_id == "spec"
    assert final.attestation.delivery_seal == _BLOCK_SEAL


def test_fabricated_obligations_record_is_unmeasured() -> None:
    # MUTATION OB-A bites: a bogus issue sha (not 64-hex) is not a real source binding.
    final = _obl(issue_sha256="not-a-real-sha")
    assert final is not None  # binding is sound; the record is what is unproven
    assert final.attestation.truth_verdict == UNMEASURED
    assert final.attestation.freshness_verdict == UNMEASURED


def test_zero_obligation_count_is_unmeasured() -> None:
    final = _obl(obligation_count=0)
    assert final is not None
    assert final.attestation.truth_verdict == UNMEASURED


def test_bad_obligations_digest_is_unmeasured() -> None:
    final = _obl(obligations_digest="short")
    assert final is not None
    assert final.attestation.truth_verdict == UNMEASURED


def test_obligations_seal_must_match_block_hash() -> None:
    # MUTATION OB-B bites: a seal that is not the block hash prefix is an unsound binding
    # and yields NO attestation at all (fail-closed, not a fabricated PASS).
    assert _obl(delivery_seal="f" * 16) is None


def test_obligations_empty_candidate_is_none() -> None:
    assert _obl(candidate_id="") is None
