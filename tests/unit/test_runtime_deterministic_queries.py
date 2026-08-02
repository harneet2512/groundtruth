from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from groundtruth.runtime.deterministic_queries import (
    DeterministicQueryContext,
    execute_query,
)
from groundtruth.runtime.edit_check import check_edit_syntax_bytes
from groundtruth.runtime.observation_compiler import (
    CONFIGURATION_BINDING_SCHEMA,
    REPOSITORY_SNAPSHOT_SCHEMA,
    ActionKind,
    ActionRequest,
    ConfigurationBinding,
    Coverage,
    EvidenceSemantics,
    RepositorySnapshot,
    RequestedFidelity,
    canonical_bytes,
)
from groundtruth.runtime.reasoning_runtime import RevisionVector


def _snapshot(graph: str = "graph-1") -> RepositorySnapshot:
    cfg = ConfigurationBinding(
        CONFIGURATION_BINDING_SCHEMA, "cfg", "a" * 64, "b" * 64, "pytest"
    )
    return RepositorySnapshot(
        REPOSITORY_SNAPSHOT_SCHEMA,
        "repo",
        "c" * 64,
        "HEAD",
        "d" * 64,
        "e" * 64,
        RevisionVector(
            repository_content="patch-1", graph=graph, lsp="lsp-1",
            runtime_evidence="verify-1",
        ),
        cfg,
    )


def _request(kind: ActionKind, arguments: dict, graph: str = "graph-1") -> ActionRequest:
    return ActionRequest.build(
        action_id="a1",
        kind=kind,
        arguments=arguments,
        snapshot=_snapshot(graph),
        requested_fidelity=RequestedFidelity.EXACT,
    )


def _context(root: Path) -> DeterministicQueryContext:
    files = tuple(
        (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(
            item for item in root.rglob("*") if item.is_file() and not item.is_symlink()
        )
    )
    return DeterministicQueryContext(
        root,
        repository_content_revision="patch-1",
        working_tree_sha256="e" * 64,
        snapshot_files=files,
        snapshot_complete=True,
    )


def test_exact_literal_search_is_stable_closed_scope_and_snapshot_bound(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("x needle needle\n", encoding="utf-8")
    if hasattr(os, "symlink"):
        try:
            os.symlink(tmp_path / "a.txt", tmp_path / "link.txt")
        except OSError:
            pass
    req = _request(ActionKind.EXACT_LITERAL_SEARCH, {"literal": "needle", "paths": ["."]})
    artifact = execute_query(req, _context(tmp_path))
    answer = artifact.direct_answer
    assert [(m["path"], m["line"], m["column"]) for m in answer["matches"]] == [
        ("a.txt", 1, 3), ("a.txt", 1, 10), ("b.txt", 1, 1)
    ]
    assert artifact.snapshot_sha256 == req.repository_snapshot_sha256
    assert artifact.semantics is (
        EvidenceSemantics.INCOMPLETE if artifact.omissions else EvidenceSemantics.EXACT
    )
    assert canonical_bytes(artifact) == canonical_bytes(
        execute_query(req, _context(tmp_path))
    )


def test_literal_search_rejects_multiline_and_unknown_semantics(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\nb\n", encoding="utf-8")
    multiline = execute_query(
        _request(ActionKind.EXACT_LITERAL_SEARCH, {"literal": "a\nb", "paths": ["."]}),
        _context(tmp_path),
    )
    assert multiline.semantics is EvidenceSemantics.INCOMPLETE
    assert multiline.omissions == ("invalid_literal",)
    unknown = execute_query(
        _request(
            ActionKind.EXACT_LITERAL_SEARCH,
            {"literal": "a", "paths": ["."], "case_sensitive": False},
        ),
        _context(tmp_path),
    )
    assert unknown.semantics is EvidenceSemantics.INCOMPLETE
    assert unknown.omissions == ("unsupported_argument:case_sensitive",)


def test_literal_search_certifies_zero_result_over_observed_scope(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("absent here\n", encoding="utf-8")
    artifact = execute_query(
        _request(ActionKind.EXACT_LITERAL_SEARCH, {"literal": "needle", "paths": ["."]}),
        _context(tmp_path),
    )
    assert artifact.semantics is EvidenceSemantics.EXACT
    assert artifact.coverage is Coverage.COMPLETE
    assert artifact.direct_answer["matches"] == []
    assert artifact.direct_answer["files_observed"][0]["path"] == "a.txt"


def test_syntax_parses_the_same_captured_bytes_it_hashes(
    tmp_path: Path, monkeypatch,
) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    captured = bad.read_bytes()
    parsed: list[bytes] = []

    def checker(path: str, source_bytes: bytes, repo_root: str):
        parsed.append(source_bytes)
        Path(repo_root, path).write_text("valid = True\n", encoding="utf-8")
        return {
            "verdict": "syntax_error", "diagnostic": "captured syntax error",
            "language": ".py", "reason": "parse_error", "checker": ["ast.parse"],
        }

    monkeypatch.setattr(
        "groundtruth.runtime.deterministic_queries.check_edit_syntax_bytes",
        checker,
    )
    syntax = execute_query(
        _request(ActionKind.SYNTAX_QUERY, {"path": "bad.py"}),
        _context(tmp_path),
    )
    assert syntax.semantics is EvidenceSemantics.EXACT
    assert syntax.direct_answer["verdict"] == "syntax_error"
    assert syntax.anchors[0].path == "bad.py"
    assert parsed == [captured]
    assert syntax.direct_answer["source_sha256"] == hashlib.sha256(captured).hexdigest()


def test_captured_byte_checker_never_reopens_target_or_leaks_temp_identity(
    tmp_path: Path,
) -> None:
    target = tmp_path / "pkg" / "module.py"
    target.parent.mkdir()
    target.write_text("valid = True\n", encoding="utf-8")

    result = check_edit_syntax_bytes(
        "pkg/module.py",
        b"def broken(:\n",
        str(tmp_path),
    )

    assert result["verdict"] == "syntax_error"
    assert "pkg/module.py" in result["diagnostic"]
    assert "gt-syntax-capture-" not in result["diagnostic"]
    assert target.read_text(encoding="utf-8") == "valid = True\n"


def test_exact_query_requires_complete_matching_snapshot_authority(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    request = _request(
        ActionKind.EXACT_LITERAL_SEARCH,
        {"literal": "needle", "paths": ["."]},
    )
    unavailable = execute_query(request, DeterministicQueryContext(tmp_path))
    assert unavailable.semantics is EvidenceSemantics.INCOMPLETE
    assert "snapshot_authority_unavailable" in unavailable.omissions

    stale = execute_query(
        request,
        DeterministicQueryContext(
            tmp_path,
            repository_content_revision="other",
            working_tree_sha256="e" * 64,
            snapshot_files=(("a.txt", hashlib.sha256(b"needle\n").hexdigest()),),
            snapshot_complete=True,
        ),
    )
    assert stale.semantics is EvidenceSemantics.INCOMPLETE
    assert "repository_revision_mismatch" in stale.omissions


def test_snapshot_manifest_rejects_unsafe_paths_and_invalid_hashes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe path"):
        DeterministicQueryContext(
            tmp_path,
            snapshot_files=(("../outside.py", "a" * 64),),
            snapshot_complete=True,
        )
    with pytest.raises(ValueError, match="invalid SHA-256"):
        DeterministicQueryContext(
            tmp_path,
            snapshot_files=(("inside.py", "not-a-digest"),),
            snapshot_complete=True,
        )


def test_verification_status_is_revision_bound(tmp_path: Path) -> None:
    req = _request(
        ActionKind.VERIFICATION_STATUS,
        {
            "plan": {
                "patch_revision": "patch-1", "graph_revision": "graph-1",
                "changed_entities": ["pkg/a.py:target"], "obligations": [], "edited_files": ["pkg/a.py"],
                "checks": [{
                    "kind": "unit", "command": ["pytest"],
                    "selection_basis": "explicit", "covered_entities": ["pkg/a.py:target"],
                    "covered_obligations": [], "expected_cost": "low",
                    "confidence": "high", "attribution_requirement": "none",
                    "targets": [], "reason": ""
                }]
            },
            "result": {
                "kind": "unit", "selection_basis": "explicit", "executed": True,
                "verdict": "pass", "graph_revision": "graph-1", "patch_revision": "patch-1",
                "covered_entities": ["pkg/a.py:target"], "covered_obligations": [],
                "attribution_requirement": "none", "attribution_satisfied": True, "detail": {}
            },
        },
    )
    artifact = execute_query(req, _context(tmp_path))
    assert artifact.semantics is EvidenceSemantics.EXECUTION_SPECIFIC
    assert artifact.direct_answer["green"] is True
    assert artifact.producer_revision == "verify-1"


def test_patch_impact_preserves_exact_file_identities_but_never_overclaims(tmp_path: Path) -> None:
    artifact = execute_query(
        _request(
            ActionKind.PATCH_IMPACT,
            {"edited_files": {"pkg/a.py": {"before": "x = 1\n", "after": "x = 2\n"}}},
        ),
        _context(tmp_path),
    )
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert artifact.coverage is Coverage.PARTIAL
    assert artifact.direct_answer["files"][0]["path"] == "pkg/a.py"
    assert "semantic_impact_not_complete" in artifact.omissions
    assert artifact.producer_revision == "patch-1"
