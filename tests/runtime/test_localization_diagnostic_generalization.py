from __future__ import annotations

from groundtruth.runtime.localization_diagnostic import validate_brief_payload


def _payload(path: str, components: dict, *, sem_count: int = 1) -> dict:
    return {
        "brief_text": f"EDIT-TARGET: {path}",
        "metrics": {
            "rendered_candidate_count": 1,
            "semantic_signal_count": sem_count,
            "sem_components": [float(components.get("sem", 0.0) or 0.0)],
            "localization_proof": [{"path": path, "components": components}],
        },
    }


def test_supported_source_candidate_passes_across_language_shapes():
    cases = [
        ("pkg/module.py", {"sem": 0.3, "lex": 0.4}),
        ("lib/response.js", {"sem": 0.3, "lex": 0.4}),
        ("packages/runtime-core/src/scheduler.ts", {"sem": 0.3, "lex": 0.4}),
        ("metadata/metadata.go", {"sem": 0.3, "lex": 0.4}),
        ("src/buf/reader.rs", {"sem": 0.3, "lex": 0.4}),
    ]
    for path, comps in cases:
        report = validate_brief_payload(_payload(path, comps), require_semantic=True)
        assert report["ok"] is True, (path, report)
        assert report["violations"] == []


def test_zero_evidence_fails_for_any_source_language_shape():
    for path in [
        "pkg/module.py",
        "lib/response.js",
        "packages/runtime-core/src/scheduler.ts",
        "metadata/metadata.go",
        "src/buf/reader.rs",
    ]:
        report = validate_brief_payload(_payload(path, {}, sem_count=0), require_semantic=True)
        assert report["ok"] is False
        assert "ZERO_EVIDENCE_DELIVERED" in report["violations"]
        assert "SEMANTIC_SIGNAL_ZERO" in report["violations"]


def test_non_source_and_declaration_test_paths_fail_without_repo_literals():
    paths = [
        "tests/unit/test_parser.py",
        "examples/demo/index.js",
        "packages-private/dts-test/component.test-d.ts",
        "src/foo.test-d.ts",
        "vendor/pkg/lib.go",
        "third_party/crate/src/lib.rs",
        "README.md",
    ]
    for path in paths:
        report = validate_brief_payload(_payload(path, {"sem": 0.2}), require_semantic=True)
        assert report["ok"] is False, (path, report)
        assert "NON_SOURCE_OR_TEST_DELIVERED" in report["violations"]


def test_gold_absent_is_only_required_when_gold_contract_is_enabled():
    payload = _payload("src/actual.py", {"sem": 0.2})
    loose = validate_brief_payload(payload, gold_files=["src/expected.py"], require_gold=False)
    strict = validate_brief_payload(payload, gold_files=["src/expected.py"], require_gold=True)

    assert "GOLD_ABSENT_FROM_DELIVERED_PROOF" not in loose["violations"]
    assert "GOLD_ABSENT_FROM_DELIVERED_PROOF" in strict["violations"]


def test_missing_proof_and_missing_semantic_log_fail_closed():
    payload = {
        "brief_text": "EDIT-TARGET: src/x.py",
        "metrics": {
            "rendered_candidate_count": 1,
            "semantic_signal_count": 1,
        },
    }
    report = validate_brief_payload(payload, require_semantic=True)
    assert report["ok"] is False
    assert "LOCALIZATION_PROOF_MISSING" in report["violations"]
    assert "SEMANTIC_COMPONENT_LOG_MISSING" in report["violations"]
