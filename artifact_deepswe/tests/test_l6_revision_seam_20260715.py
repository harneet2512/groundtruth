"""L6 seam binds an edit to the exact graph revision it attempted to produce."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MINI = ROOT / "artifact_deepswe" / "gt_mini_patch.py"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_mini(name: str):
    spec = importlib.util.spec_from_file_location(name, MINI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _setup(monkeypatch, tmp_path: Path, name: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "src.py"
    source.write_bytes(b"def changed():\n    return 2\n")
    graph = tmp_path / "graph.db"
    graph.write_bytes(b"graph-before")
    binary = tmp_path / "gt-index"
    binary.write_bytes(b"placeholder")
    ledger = tmp_path / "audit" / "runtime.jsonl"
    for key in (
        "GT_PORTABLE_SUBSTRATE",
        "GT_HOST_GRAPH_DB",
        "GT_CERT_DIR",
        "GT_PROOF_MODE",
        "GT_L6_FRESH",
        "GT_SS_PROVENANCE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GT_GRAPH_DB", str(graph))
    monkeypatch.setenv("GT_INDEX_BIN", str(binary))
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    return _load_mini(name), repo, source, graph, binary, ledger


def test_success_persists_exact_immutable_revision_and_monotonic_generation(
    monkeypatch, tmp_path
):
    m, repo, source, graph, binary, ledger = _setup(
        monkeypatch, tmp_path, "gtmp_l6_revision_success"
    )
    m._action_count = 17
    invocations = []

    def controlled_reindex(argv, *, capture_output, timeout):
        invocations.append((tuple(argv), capture_output, timeout))
        graph.write_bytes(b"graph-after")
        return subprocess.CompletedProcess(argv, 0, stdout=b"index-out", stderr=b"index-err")

    monkeypatch.setattr(m.subprocess, "run", controlled_reindex)
    m._invalidate_on_edit("src.py", str(repo))

    assert len(m._l6_revision_attestations) == 1
    attestation = m._l6_revision_attestations[0]
    expected_argv = (
        str(binary),
        f"-root={repo}",
        "-file=src.py",
        f"-output={graph}",
    )
    assert invocations == [(expected_argv, True, m._HOOK_TIMEOUT)]
    assert attestation.action_count == 17
    assert attestation.graph_generation == 1
    assert attestation.repo_root == str(repo)
    assert attestation.edited_path == "src.py"
    assert attestation.edited_source_sha256 == _sha(source.read_bytes())
    assert attestation.source_db.path == str(graph)
    assert attestation.source_db.sha256 == _sha(b"graph-before")
    assert attestation.selected_db_before.sha256 == _sha(b"graph-before")
    assert attestation.selected_db_after.sha256 == _sha(b"graph-after")
    assert attestation.subprocess.argv == expected_argv
    assert attestation.subprocess.returncode == 0
    assert attestation.subprocess.stdout_sha256 == _sha(b"index-out")
    assert attestation.subprocess.stdout_bytes == len(b"index-out")
    assert attestation.subprocess.stderr_sha256 == _sha(b"index-err")
    assert attestation.subprocess.stderr_bytes == len(b"index-err")
    assert attestation.reindex_succeeded is True
    assert attestation.revision_advanced is True

    sidecar = ledger.parent / "gt_l6_revision_attestations.jsonl"
    rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["schema"] == "gt.l6_revision_audit.v1"
    assert rows[0]["canonical_sha256"] == m._l6_revision_commitments[0]
    from groundtruth.runtime.l6_revision_attestation import canonical_sha256, from_dict

    decoded = from_dict(rows[0]["attestation"])
    assert decoded == attestation
    assert canonical_sha256(decoded) == rows[0]["canonical_sha256"]

    graph.write_bytes(b"graph-second-before")
    m._invalidate_on_edit("src.py", str(repo))
    assert [row.graph_generation for row in m._l6_revision_attestations] == [1, 2]


def test_real_nonzero_process_records_full_stream_identity(monkeypatch, tmp_path):
    m, repo, _source, graph, _binary, _ledger = _setup(
        monkeypatch, tmp_path, "gtmp_l6_revision_nonzero"
    )
    monkeypatch.setenv("GT_INDEX_BIN", sys.executable)
    m._invalidate_on_edit("src.py", str(repo))

    assert len(m._l6_revision_attestations) == 1
    attestation = m._l6_revision_attestations[0]
    assert attestation.subprocess.argv[0] == sys.executable
    assert attestation.subprocess.returncode not in (None, 0)
    assert attestation.subprocess.timed_out is False
    assert attestation.subprocess.exception_type == ""
    assert attestation.subprocess.stderr_bytes > 0
    assert attestation.selected_db_before.sha256 == attestation.selected_db_after.sha256
    assert attestation.reindex_succeeded is False


def test_timeout_and_oserror_are_exact_nonpromoting_attestations(monkeypatch, tmp_path):
    m, repo, _source, graph, _binary, _ledger = _setup(
        monkeypatch, tmp_path, "gtmp_l6_revision_failures"
    )

    def timeout(argv, *, capture_output, timeout):
        raise subprocess.TimeoutExpired(
            argv, timeout, output=b"partial-out", stderr=b"partial-err"
        )

    monkeypatch.setattr(m.subprocess, "run", timeout)
    m._invalidate_on_edit("src.py", str(repo))
    first = m._l6_revision_attestations[-1]
    assert first.graph_generation == 1
    assert first.subprocess.returncode is None
    assert first.subprocess.timed_out is True
    assert first.subprocess.exception_type == "TimeoutExpired"
    assert first.subprocess.stdout_sha256 == _sha(b"partial-out")
    assert first.subprocess.stderr_sha256 == _sha(b"partial-err")
    assert first.reindex_succeeded is False
    assert first.selected_db_before.sha256 == first.selected_db_after.sha256

    def os_error(argv, *, capture_output, timeout):
        raise OSError("exec format")

    monkeypatch.setattr(m.subprocess, "run", os_error)
    m._invalidate_on_edit("src.py", str(repo))
    second = m._l6_revision_attestations[-1]
    assert second.graph_generation == 2
    assert second.subprocess.returncode is None
    assert second.subprocess.timed_out is False
    assert second.subprocess.exception_type == "OSError"
    assert second.subprocess.stdout_sha256 == _sha(b"")
    assert second.subprocess.stdout_bytes == 0
    assert second.subprocess.stderr_sha256 == _sha(b"")
    assert second.subprocess.stderr_bytes == 0
    assert second.reindex_succeeded is False


def test_scratch_skip_and_readonly_substrate_create_no_revision_claim(monkeypatch, tmp_path):
    m, repo, _source, _graph, _binary, ledger = _setup(
        monkeypatch, tmp_path, "gtmp_l6_revision_skips"
    )
    scratch = repo / "tmp" / "scratch.py"
    scratch.parent.mkdir()
    scratch.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("GT_SS_PROVENANCE", "1")

    m._invalidate_on_edit("tmp/scratch.py", str(repo))
    assert m._l6_revision_attestations == []
    assert m._l6_graph_generation == 0
    assert not (ledger.parent / "gt_l6_revision_attestations.jsonl").exists()

    monkeypatch.delenv("GT_SS_PROVENANCE")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.delenv("GT_L6_FRESH", raising=False)
    m._invalidate_on_edit("src.py", str(repo))
    assert m._l6_revision_attestations == []
    assert m._l6_graph_generation == 0


def test_unreadable_source_runs_legacy_reindex_but_makes_no_attestation(
    monkeypatch, tmp_path
):
    m, repo, _source, graph, _binary, ledger = _setup(
        monkeypatch, tmp_path, "gtmp_l6_revision_missing_source"
    )
    calls = []
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda argv, **kwargs: calls.append(tuple(argv))
        or subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b""),
    )

    m._invalidate_on_edit("missing.py", str(repo))
    assert len(calls) == 1
    assert m._l6_revision_attestations == []
    assert not (ledger.parent / "gt_l6_revision_attestations.jsonl").exists()
    assert graph.read_bytes() == b"graph-before"
