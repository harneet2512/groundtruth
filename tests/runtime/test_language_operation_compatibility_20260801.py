from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

from groundtruth.runtime.edit_check import _build_check_command, check_edit_syntax
from groundtruth.runtime.deterministic_queries import DeterministicQueryContext, execute_query
from groundtruth.runtime.language_compatibility import (
    COMPATIBILITY,
    OPERATIONS,
    load_language_operation_compatibility,
)
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
)
from groundtruth.runtime.reasoning_runtime import RevisionVector


ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "src" / "groundtruth" / "runtime" / "generated_language_operation_compatibility.json"
GENERATOR = ROOT / "scripts" / "generate_language_operation_compatibility.py"
SYNTAX_EXACT = {"go", "javascript", "python", "ruby", "typescript"}


def test_manifest_is_complete_terminal_and_has_exact_counts() -> None:
    assert len(COMPATIBILITY.rows) == 210
    assert len({row.registry_identity for row in COMPATIBILITY.rows}) == 30
    assert {row.operation for row in COMPATIBILITY.rows} == set(OPERATIONS)
    assert Counter(row.terminal_semantics for row in COMPATIBILITY.rows) == {
        "exact": 35,
        "execution_specific": 30,
        "removed": 145,
    }


def test_only_mechanically_certified_pairs_survive() -> None:
    identities = {row.registry_identity for row in COMPATIBILITY.rows}
    for identity in identities:
        assert COMPATIBILITY.semantics_for(identity, "exact_literal_search") == "exact"
        assert (
            COMPATIBILITY.semantics_for(identity, "verification_status")
            == "execution_specific"
        )
        expected_syntax = "exact" if identity in SYNTAX_EXACT else "removed"
        assert COMPATIBILITY.semantics_for(identity, "syntax") == expected_syntax
        for operation in ("definition", "references", "callers", "patch_impact"):
            assert COMPATIBILITY.semantics_for(identity, operation) == "removed"


def test_checked_artifact_is_reproducible_from_go_registry_specs(tmp_path: Path) -> None:
    output = tmp_path / "compatibility.json"
    subprocess.run(
        [sys.executable, str(GENERATOR), "--output", str(output)],
        cwd=ROOT,
        check=True,
    )
    assert output.read_bytes() == GENERATED.read_bytes()


def test_exact_literal_search_certifies_all_30_registry_languages(tmp_path: Path) -> None:
    generator = runpy.run_path(str(GENERATOR))
    source_manifest = generator["registry_manifest"](
        ROOT / "gt-index" / "internal" / "specs"
    )
    expected_paths = []
    for language in source_manifest["languages"]:
        extension = language["extensions"][0]
        path = tmp_path / f"fixture_{language['name']}{extension}"
        path.write_text("GT_LANGUAGE_SENTINEL\n", encoding="utf-8")
        expected_paths.append(path.name)

    configuration = ConfigurationBinding(
        CONFIGURATION_BINDING_SCHEMA, "cfg", "a" * 64, "b" * 64, "mixed"
    )
    snapshot = RepositorySnapshot(
        REPOSITORY_SNAPSHOT_SCHEMA,
        "repo",
        "c" * 64,
        "HEAD",
        "d" * 64,
        "e" * 64,
        RevisionVector("patch", "graph", "lsp", "runtime"),
        configuration,
    )
    request = ActionRequest.build(
        action_id="all-languages-literal",
        kind=ActionKind.EXACT_LITERAL_SEARCH,
        arguments={"literal": "GT_LANGUAGE_SENTINEL", "paths": ["."]},
        snapshot=snapshot,
        requested_fidelity=RequestedFidelity.EXACT,
    )
    snapshot_files = tuple(
        (path.name, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(tmp_path.iterdir())
        if path.is_file()
    )
    artifact = execute_query(
        request,
        DeterministicQueryContext(
            tmp_path,
            repository_content_revision="patch",
            working_tree_sha256="e" * 64,
            snapshot_files=snapshot_files,
            snapshot_complete=True,
        ),
    )
    assert artifact.semantics is EvidenceSemantics.EXACT
    assert artifact.coverage is Coverage.COMPLETE
    assert [row["path"] for row in artifact.direct_answer["matches"]] == sorted(expected_paths)


@pytest.mark.parametrize(
    ("extension", "command"),
    (
        (".py", "python"),
        (".pyi", "python"),
        (".js", "node"),
        (".jsx", "node"),
        (".mjs", "node"),
        (".cjs", "node"),
        (".ts", "node"),
        (".tsx", "node"),
        (".go", "gofmt"),
        (".rb", "ruby"),
    ),
)
def test_every_extension_of_syntax_certified_languages_has_parser_command(
    extension: str, command: str
) -> None:
    built = _build_check_command(extension, "fixture" + extension)
    assert built is not None and built[0] == command


def test_python_and_stub_adversarial_syntax_is_actually_parsed(tmp_path: Path) -> None:
    for extension in (".py", ".pyi"):
        valid = tmp_path / f"valid{extension}"
        invalid = tmp_path / f"invalid{extension}"
        valid.write_text("def answer(x: int = ...) -> int: ...\n", encoding="utf-8")
        invalid.write_text("def broken(: ...\n", encoding="utf-8")
        assert check_edit_syntax(valid.name, str(tmp_path))["verdict"] == "ok"
        assert check_edit_syntax(invalid.name, str(tmp_path))["verdict"] == "syntax_error"


def test_missing_syntax_toolchain_is_unavailable_not_false_success(tmp_path: Path) -> None:
    target = tmp_path / "main.go"
    target.write_text("package main\n", encoding="utf-8")

    def missing(_command: list[str], _cwd: str, _timeout: int):
        raise FileNotFoundError("tool missing")

    result = check_edit_syntax(target.name, str(tmp_path), executor=missing)
    assert result["verdict"] == "unavailable"
    assert result["reason"] == "spawn_error"


def test_loader_rejects_nonterminal_or_duplicate_rows(tmp_path: Path) -> None:
    raw = json.loads(GENERATED.read_text(encoding="utf-8"))
    raw["rows"][0]["terminal_semantics"] = "experimental"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid compatibility row"):
        load_language_operation_compatibility(bad)

    raw = json.loads(GENERATED.read_text(encoding="utf-8"))
    raw["rows"][-1] = dict(raw["rows"][0])
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="missing or duplicates"):
        load_language_operation_compatibility(bad)
