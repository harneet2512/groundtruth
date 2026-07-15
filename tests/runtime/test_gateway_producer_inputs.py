from __future__ import annotations

import dataclasses
import hashlib
import sqlite3

from groundtruth.runtime import gateway as gw
from groundtruth.runtime.adapters.miniswe import render_envelope
from groundtruth.runtime.evidence_envelope import render_bytes, to_dict
from groundtruth.runtime.producer_inputs import (
    PRODUCER_INPUTS_SCHEMA,
    CallerEvidenceRow,
    ProducerInputs,
    SourceState,
)
from groundtruth.runtime.patch_delta import SignatureMismatch


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _graph(tmp_path, *, caller_file: str, source_line: int, confidence: float) -> str:
    path = tmp_path / "graph.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " file_path TEXT, start_line INTEGER, is_test INTEGER, language TEXT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER,"
        " target_id INTEGER, type TEXT, source_line INTEGER, source_file TEXT,"
        " resolution_method TEXT, confidence REAL, metadata TEXT);"
    )
    con.execute(
        "INSERT INTO nodes VALUES(1,'Function','get_user','src/api.py',1,0,'python')"
    )
    con.execute(
        "INSERT INTO nodes VALUES(2,'Function','use',?,1,0,'python')",
        (caller_file,),
    )
    con.execute(
        "INSERT INTO edges VALUES(1,2,1,'CALLS',?,?,?, ?,NULL)",
        (source_line, caller_file, "import", confidence),
    )
    con.commit()
    con.close()
    return str(path)


def _edit(before: str, after: str) -> gw.ToolEvent:
    return gw.ToolEvent(
        kind="edit",
        command="str_replace src/api.py",
        changed_files=("src/api.py",),
        action_index=7,
        edit_before_after={"src/api.py": (before, after)},
    )


def test_caller_contract_retains_typed_fact_caller_rows_without_byte_drift(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    caller_text = "def use():\n    return get_user(1)\n"
    (tmp_path / "app").mkdir()
    caller_path = tmp_path / "app" / "main.py"
    caller_path.write_text(caller_text, encoding="utf-8")
    caller_sha = _sha_bytes(caller_path.read_bytes())
    db = _graph(
        tmp_path, caller_file="app/main.py", source_line=2, confidence=0.95
    )
    before = "def get_user(uid):\n    return uid\n"
    after = "def get_user(uid, name):\n    return uid\n"
    env = gw._produce_caller_contract(
        _edit(before, after),
        gw.GatewayState(
            graph_db=db, repo_root=str(tmp_path), graph_revision="graph-rev-7"
        ),
    )[0]

    assert env.producer_inputs == ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA,
        evidence_type="caller_break",
        candidate_id=env.dedup_key,
        before_state=SourceState(
            file="src/api.py", sha256=_sha(before), revision="edit:7:before"
        ),
        after_state=SourceState(
            file="src/api.py", sha256=_sha(after), revision="edit:7:after"
        ),
        caller_rows=(
            CallerEvidenceRow(
                identity="use",
                file="app/main.py",
                line=2,
                confidence=0.95,
                resolution_method="import",
                edge_id=1,
                definition_id=1,
                source_state=SourceState(
                    file="app/main.py",
                    sha256=caller_sha,
                    revision="source:" + caller_sha,
                ),
            ),
        ),
        graph_revision="graph-rev-7",
    )
    assert env.provenance == (("app/main.py", 2),)
    without_inputs = dataclasses.replace(env, producer_inputs=None)
    assert env == without_inputs
    assert env.dedup_key == without_inputs.dedup_key
    assert render_bytes(env) == render_bytes(without_inputs)
    assert render_envelope(env, native=False) == render_envelope(
        without_inputs, native=False
    )
    assert render_envelope(env, native=True) == render_envelope(
        without_inputs, native=True
    )
    assert "producer_inputs" not in to_dict(env)


def test_signature_delta_carries_exact_before_after_caller_and_graph_inputs(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_PATCH_DELTA", "1")
    caller_text = "def use():\n    return get_user(1)\n"
    (tmp_path / "src").mkdir()
    caller_path = tmp_path / "src" / "caller.py"
    caller_path.write_text(caller_text, encoding="utf-8")
    caller_sha = _sha_bytes(caller_path.read_bytes())
    db = _graph(
        tmp_path, caller_file="src/caller.py", source_line=2, confidence=0.9
    )
    before = "def get_user(uid):\n    return uid\n"
    after = "def get_user(uid, name):\n    return uid\n"
    env = [
        item
        for item in gw._produce_patch_delta(
            _edit(before, after),
            gw.GatewayState(
                graph_db=db, repo_root=str(tmp_path), graph_revision="graph-rev-9"
            ),
        )
        if item.evidence_type == "signature_mismatch"
    ][0]

    inputs = env.producer_inputs
    assert isinstance(inputs, ProducerInputs)
    assert inputs.schema == PRODUCER_INPUTS_SCHEMA
    assert inputs.evidence_type == "signature_mismatch"
    assert inputs.candidate_id == env.dedup_key
    assert inputs.before_state.sha256 == _sha(before)
    assert inputs.after_state.sha256 == _sha(after)
    assert inputs.graph_revision == "graph-rev-9"
    assert inputs.caller_rows == (
        CallerEvidenceRow(
            identity="use",
            file="src/caller.py",
            line=2,
            confidence=0.9,
            resolution_method="import",
            edge_id=1,
            definition_id=1,
            source_state=SourceState(
                file="src/caller.py",
                sha256=caller_sha,
                revision="source:" + caller_sha,
            ),
        ),
    )
    without_inputs = dataclasses.replace(env, producer_inputs=None)
    assert env.provenance == (("src/caller.py", 2),)
    assert env.native_args == without_inputs.native_args
    assert env.dedup_key == without_inputs.dedup_key
    assert render_bytes(env) == render_bytes(without_inputs)
    assert render_envelope(env, native=False) == render_envelope(
        without_inputs, native=False
    )
    assert render_envelope(env, native=True) == render_envelope(
        without_inputs, native=True
    )


def test_source_state_and_producer_inputs_are_transitively_immutable() -> None:
    source = SourceState(file="src/x.py", sha256="a" * 64, revision="rev")
    row = CallerEvidenceRow(
        identity="call", file="src/y.py", line=3, confidence=None,
        resolution_method=None, edge_id=None, definition_id=None, source_state=None,
    )
    inputs = ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA,
        evidence_type="caller_break",
        candidate_id="candidate",
        before_state=source,
        after_state=source,
        caller_rows=(row,),
        graph_revision="graph",
    )

    assert dataclasses.is_dataclass(inputs)
    assert inputs.caller_rows == (row,)
    try:
        inputs.graph_revision = "changed"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - load-bearing immutability assertion
        raise AssertionError("ProducerInputs must be frozen")


def test_file_backed_caller_state_hashes_exact_raw_bytes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    raw = b"def use():\n    return get_user(1)\n# \xff\n"
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_bytes(raw)
    db = _graph(tmp_path, caller_file="app/main.py", source_line=2, confidence=0.95)
    env = gw._produce_caller_contract(
        _edit("def get_user(a):\n pass\n", "def get_user(a, b):\n pass\n"),
        gw.GatewayState(graph_db=db, repo_root=str(tmp_path), graph_revision="graph"),
    )[0]

    observed = env.producer_inputs.caller_rows[0].source_state.sha256
    assert observed == hashlib.sha256(raw).hexdigest()
    assert observed != hashlib.sha256(raw.decode("utf-8", "ignore").encode()).hexdigest()


def test_signature_delta_hashes_exact_raw_caller_bytes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_PATCH_DELTA", "1")
    raw = b"def use():\n    return get_user(1)\n# \xff\n"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "caller.py").write_bytes(raw)
    db = _graph(tmp_path, caller_file="src/caller.py", source_line=2, confidence=0.9)
    env = gw._produce_patch_delta(
        _edit("def get_user(a):\n pass\n", "def get_user(a, b):\n pass\n"),
        gw.GatewayState(graph_db=db, repo_root=str(tmp_path), graph_revision="graph"),
    )[0]

    row = env.producer_inputs.caller_rows[0]
    assert row.source_state.sha256 == hashlib.sha256(raw).hexdigest()
    assert row.edge_id == 1
    assert row.definition_id == 1


def test_signature_mismatch_preserves_prior_positional_tier_abi() -> None:
    mismatch = SignatureMismatch(
        "symbol", "edited.py", 1, 1, 2, 2, "caller", "caller.py", 7,
        "symbol(1)", 1, 0.9, "WARNING",
    )

    assert mismatch.tier == "WARNING"
    assert mismatch.resolution_method == ""
    assert mismatch.edge_id is None
    assert mismatch.definition_id is None
