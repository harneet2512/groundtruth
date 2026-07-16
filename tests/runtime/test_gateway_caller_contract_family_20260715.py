from __future__ import annotations

import dataclasses
import hashlib
import sqlite3

from groundtruth.runtime import gateway
from groundtruth.runtime.adapters.miniswe import render_envelope
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope
from groundtruth.runtime.gateway_attestation_factory import (
    build_caller_contract_control_participation,
    build_gateway_attestation,
)
from groundtruth.runtime.producer_attestation import PASS
from groundtruth.runtime.producer_inputs import (
    PRODUCER_INPUTS_SCHEMA,
    CallerEvidenceRow,
    CallerUsageEvidenceRow,
    ProducerInputs,
    SignatureChange,
    SourceState,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source(file: str, token: str) -> SourceState:
    return SourceState(file=file, sha256=token * 64, revision=f"source:{token * 64}")


def _envelope(*, with_usage: bool = True) -> EvidenceEnvelope:
    env = EvidenceEnvelope.build(
        producer="caller_contract",
        fact_id="get_user",
        target="src/api.py",
        evidence_type="caller_break",
        payload=("legacy payload is not authoritative",),
        provenance=(("src/caller.py", 12),),
        confidence=0.95,
        tier="WARNING",
        graph_revision="graph-9",
        preferred_event="edit_result",
    )
    usages = (
        CallerUsageEvidenceRow(
            property_id=31,
            caller_node_id=7,
            caller_identity="use",
            caller_file="src/caller.py",
            usage_kind="iterated",
            callee="get_user",
            call_site="for row in get_user(uid)",
            line=12,
            confidence=0.8,
            source_revision="graph-9",
            extractor="tree_sitter",
            evidence_method="ast_parent",
        ),
    ) if with_usage else ()
    inputs = ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA,
        evidence_type="caller_break",
        candidate_id=env.dedup_key,
        before_state=_source("src/api.py", "a"),
        after_state=_source("src/api.py", "b"),
        caller_rows=(CallerEvidenceRow(
            identity="use", file="src/caller.py", line=12, confidence=0.95,
            resolution_method="import", source_state=_source("src/caller.py", "c"),
            edge_id=11, definition_id=4,
        ),),
        graph_revision="graph-9",
        signature_changes=(SignatureChange(
            symbol="get_user", edited_file="src/api.py",
            before_parameters=("uid",), after_parameters=("uid", "name"),
            old_min_params=None, old_max_params=None,
            new_min_params=None, new_max_params=None, positional_args=None,
        ),),
        caller_usage_rows=usages,
    )
    return dataclasses.replace(
        env,
        producer_inputs=inputs,
        native_args={
            "before_parameters": ("uid",),
            "after_parameters": ("uid", "name"),
            "caller_rows": (("src/caller.py", 12, "use"),),
            "caller_usage_rows": (
                (("src/caller.py", 12, "get_user", "iterated"),)
                if with_usage else ()
            ),
        },
    )


def test_native_gateway_caller_family_is_one_exact_structured_dose() -> None:
    env = _envelope()
    rendered = render_envelope(env, native=True)

    assert rendered == (
        "src/api.py: error: get_user() signature changed (uid -> uid, name); "
        "1 caller(s) in 1 file(s) must update the call sites\n"
        "src/caller.py:12:use\n"
        "src/caller.py:12: note: get_user() result is iterated (expects an iterable)\n"
    )
    assert "<gt-" not in rendered

    shipped = ("\n" + rendered).encode()
    seal = hashlib.sha256(shipped).hexdigest()[:16]
    attestation, artifacts = build_gateway_attestation(
        env, delivery_seal=seal, shipped_bytes=shipped,
        actual_event="edit_result", open_event="edit_result",
    )

    assert attestation.truth_verdict == PASS
    raw = next(raw for key, raw in artifacts.items() if key.startswith("producer-inputs-"))
    assert b'"before_parameters":["uid"]' in raw
    assert b'"after_parameters":["uid","name"]' in raw
    assert b'"caller_usage_rows"' in raw
    assert b'"property_id":31' in raw

    rows = build_caller_contract_control_participation(
        env, attestation=attestation, shipped_bytes=shipped,
        native=True,
        enabled_features={
            "GT_CONTRACT_NATIVE", "GT_CONTRACT_MODE",
            "GT_CONTRACT_BILATERAL", "GT_EVIDENCE_NATIVE",
        },
        iteration=19,
    )
    assert {row.feature_id for row in rows} == {
        "GT_CONTRACT_NATIVE", "GT_CONTRACT_MODE",
        "GT_CONTRACT_BILATERAL", "GT_EVIDENCE_NATIVE",
    }
    assert {row.candidate_sha256_16 for row in rows} == {seal}
    assert {row.candidate_chars for row in rows} == {len(shipped.decode())}
    assert {row.candidate_id for row in rows} == {env.dedup_key}
    assert {row.fact_class for row in rows} == {"caller_contract"}
    assert all(row.decision == "APPLIED" for row in rows)


def test_bilateral_is_no_effect_without_typed_usage_and_never_fabricates() -> None:
    env = _envelope(with_usage=False)
    rendered = render_envelope(env, native=True)
    assert "result is" not in rendered
    shipped = rendered.encode()
    attestation, _ = build_gateway_attestation(
        env,
        delivery_seal=hashlib.sha256(shipped).hexdigest()[:16],
        shipped_bytes=shipped,
        actual_event="edit_result", open_event="edit_result",
    )
    rows = build_caller_contract_control_participation(
        env, attestation=attestation, shipped_bytes=shipped, native=True,
        enabled_features={"GT_CONTRACT_BILATERAL"}, iteration=1,
    )
    assert len(rows) == 1
    assert rows[0].decision == "NO_EFFECT"
    assert rows[0].reason == "no_typed_caller_usage"


def test_family_rejects_tagged_or_nonexact_bytes_and_unrequested_controls() -> None:
    env = _envelope()
    rendered = render_envelope(env, native=True).encode()
    attestation, _ = build_gateway_attestation(
        env,
        delivery_seal=hashlib.sha256(rendered).hexdigest()[:16],
        shipped_bytes=rendered,
        actual_event="edit_result", open_event="edit_result",
    )
    assert build_caller_contract_control_participation(
        env, attestation=attestation, shipped_bytes=rendered, native=False,
        enabled_features={"GT_CONTRACT_NATIVE"}, iteration=0,
    ) == ()

    changed_inputs = dataclasses.replace(
        env.producer_inputs,
        caller_rows=(dataclasses.replace(
            env.producer_inputs.caller_rows[0],
            source_state=_source("src/caller.py", "d"),
        ),),
    )
    changed = dataclasses.replace(env, producer_inputs=changed_inputs)
    assert build_caller_contract_control_participation(
        changed, attestation=attestation, shipped_bytes=rendered, native=True,
        enabled_features={"GT_CONTRACT_NATIVE"}, iteration=0,
    ) == ()
    assert build_caller_contract_control_participation(
        env, attestation=attestation, shipped_bytes=rendered + b"x", native=True,
        enabled_features={"GT_CONTRACT_NATIVE"}, iteration=0,
    ) == ()
    assert build_caller_contract_control_participation(
        env, attestation=attestation, shipped_bytes=rendered, native=True,
        enabled_features=set(), iteration=0,
    ) == ()


def test_incomplete_usage_cannot_ride_a_passing_caller_attestation() -> None:
    env = _envelope()
    malformed = dataclasses.replace(
        env.producer_inputs.caller_usage_rows[0], source_revision="",
    )
    env = dataclasses.replace(
        env,
        producer_inputs=dataclasses.replace(
            env.producer_inputs, caller_usage_rows=(malformed,),
        ),
    )
    rendered = render_envelope(env, native=True).encode()
    attestation, _ = build_gateway_attestation(
        env,
        delivery_seal=hashlib.sha256(rendered).hexdigest()[:16],
        shipped_bytes=rendered,
        actual_event="edit_result", open_event="edit_result",
    )
    assert attestation.truth_verdict != PASS
    assert build_caller_contract_control_participation(
        env, attestation=attestation, shipped_bytes=rendered, native=True,
        enabled_features={"GT_CONTRACT_BILATERAL"}, iteration=0,
    ) == ()


def test_gateway_producer_retains_typed_usage_only_with_full_provenance(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    (tmp_path / "src").mkdir()
    caller = tmp_path / "src" / "caller.py"
    caller.write_text("def use():\n    for x in get_user(1): pass\n", encoding="utf-8")
    db = tmp_path / "graph.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY,label TEXT,name TEXT,file_path TEXT,"
        "start_line INTEGER,is_test INTEGER,language TEXT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY,source_id INTEGER,target_id INTEGER,"
        "type TEXT,source_line INTEGER,source_file TEXT,resolution_method TEXT,"
        "confidence REAL,metadata TEXT);"
        "CREATE TABLE properties(id INTEGER PRIMARY KEY,node_id INTEGER,kind TEXT,value TEXT,"
        "line INTEGER,confidence REAL,property_id TEXT,extractor TEXT,evidence_method TEXT,"
        "source_revision TEXT);"
    )
    con.execute("INSERT INTO nodes VALUES(1,'Function','get_user','src/api.py',1,0,'python')")
    con.execute("INSERT INTO nodes VALUES(7,'Function','use','src/caller.py',1,0,'python')")
    con.execute("INSERT INTO edges VALUES(11,7,1,'CALLS',12,'src/caller.py','import',0.95,NULL)")
    con.execute(
        "INSERT INTO properties VALUES(31,7,'caller_usage',?,12,0.8,'prop-31',"
        "'tree_sitter','ast_parent','graph-9')",
        ("iterated:get_user|for x in get_user(1)",),
    )
    # This malformed/low-confidence neighbor must never be promoted as bilateral truth.
    con.execute(
        "INSERT INTO properties VALUES(32,7,'caller_usage',?,13,0.2,'prop-32',"
        "'tree_sitter','ast_parent','graph-9')",
        ("boolean_check:get_user|if get_user(1)",),
    )
    con.execute(
        "INSERT INTO properties VALUES(33,7,'caller_usage',?,14,0.8,'prop-33',"
        "'tree_sitter','ast_parent','stale-graph')",
        ("exception_guard:get_user|try: get_user(1)",),
    )
    con.commit()
    con.close()

    before = "def get_user(uid):\n    return [uid]\n"
    after = "def get_user(uid, name):\n    return [uid]\n"
    env = gateway._produce_caller_contract(
        gateway.ToolEvent(
            kind="edit", action_index=4,
            edit_before_after={"src/api.py": (before, after)},
        ),
        gateway.GatewayState(
            graph_db=str(db), repo_root=str(tmp_path), graph_revision="graph-9",
        ),
    )[0]

    assert env.producer_inputs.caller_usage_rows == (
        CallerUsageEvidenceRow(
            property_id=31, caller_node_id=7, caller_identity="use",
            caller_file="src/caller.py", usage_kind="iterated", callee="get_user",
            call_site="for x in get_user(1)", line=12, confidence=0.8,
            source_revision="graph-9", extractor="tree_sitter",
            evidence_method="ast_parent",
        ),
    )
    assert env.producer_inputs.caller_rows[0].source_state.sha256 == _sha(
        caller.read_bytes())
