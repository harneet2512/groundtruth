"""Producer-owned structured inputs for the edit.syntax fact."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from groundtruth.runtime.syntax_observation import build_syntax_observation


def test_builder_binds_exact_source_and_normalized_python_diagnostic():
    source = b"def f(\r\n    return 1\r\n"
    result = {
        "verdict": "syntax_error",
        "diagnostic": (
            'File "pkg/mod.py", line 1\r\n'
            "    def f(\r\n"
            "         ^\r\n"
            "SyntaxError: '(' was never closed\r\n"
        ),
        "language": ".py",
        "reason": "parse_error",
        "checker": ["ast.parse"],
    }

    obs = build_syntax_observation(
        file_path="pkg/mod.py",
        source_bytes=source,
        check_result=result,
        actual_event="post_edit",
        rendered_block=result["diagnostic"],
    )

    normalized = (
        'File "pkg/mod.py", line 1\n'
        "    def f(\n"
        "         ^\n"
        "SyntaxError: '(' was never closed"
    )
    assert obs.source_sha256 == hashlib.sha256(source).hexdigest()
    assert obs.source_bytes_length == len(source)
    assert obs.diagnostic_sha256 == hashlib.sha256(normalized.encode()).hexdigest()
    assert obs.diagnostic_category == "syntax_error"
    assert obs.diagnostic_location is not None
    assert obs.diagnostic_location.to_dict() == {
        "path": "pkg/mod.py",
        "line": 1,
        "column": 6,
    }
    rendered = result["diagnostic"].encode("utf-8")
    assert obs.rendered_sha256_16 == hashlib.sha256(rendered).hexdigest()[:16]
    assert obs.rendered_chars == len(result["diagnostic"])
    assert obs.rendered_bytes_length == len(rendered)
    assert obs.checker == ("ast.parse",)
    assert obs.language == ".py"
    assert obs.verdict == "syntax_error"
    assert obs.reason == "parse_error"
    assert obs.actual_event == "post_edit"
    with pytest.raises(FrozenInstanceError):
        obs.verdict = "ok"


def test_builder_is_deterministic_and_normalizes_diagnostic_line_endings():
    fields = dict(
        file_path="src/a.js",
        source_bytes=b"function f( {",
        actual_event="post_edit",
        rendered_block="SyntaxError: Unexpected end",
    )
    crlf = {
        "verdict": "syntax_error",
        "diagnostic": "src/a.js:1:13: SyntaxError: Unexpected end\r\n",
        "language": ".js",
        "reason": "parse_error",
        "checker": ["node", "--check"],
    }
    lf = dict(crlf, diagnostic=crlf["diagnostic"].replace("\r\n", "\n"))

    first = build_syntax_observation(check_result=crlf, **fields)
    second = build_syntax_observation(check_result=lf, **fields)

    assert first == second
    assert first.diagnostic_location is not None
    assert first.diagnostic_location.to_dict() == {
        "path": "src/a.js",
        "line": 1,
        "column": 13,
    }
    assert first.to_dict() == second.to_dict()


def test_builder_fails_closed_on_malformed_optional_fields():
    obs = build_syntax_observation(
        file_path="pkg/a.ts",
        source_bytes=b"const x: number = 1",
        check_result={
            "verdict": "unavailable",
            "diagnostic": None,
            "language": ".ts",
            "reason": "unsupported_language",
            "checker": ["", None, 3],
        },
        actual_event="post_edit",
        rendered_block="",
    )

    assert obs.diagnostic_category is None
    assert obs.diagnostic_location is None
    assert obs.checker == ()
    assert obs.diagnostic_sha256 == hashlib.sha256(b"").hexdigest()


def test_edit_syntax_candidate_stages_observation_without_tuple_abi_change(
    tmp_path, monkeypatch
):
    artifact = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
    if artifact not in sys.path:
        sys.path.insert(0, artifact)
    import gt_mini_patch as gmp

    rel = "pkg/mod.py"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    raw = b"def f(\n    return 1\n"
    path.write_bytes(raw)
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    monkeypatch.setenv("GT_SS_EDIT_DIAG", "1")
    monkeypatch.setattr(gmp, "_GT_BASELINE", False)
    monkeypatch.setattr(gmp, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(gmp, "_build_edit_check_executor", lambda: None)

    candidate = gmp._edit_syntax_candidate(rel)

    assert candidate is not None and len(candidate) == 4
    obs = gmp._last_edit_syntax_observation
    assert obs is not None
    assert obs.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert obs.file_path == rel
    assert obs.actual_event == "edit_result"
    assert obs.verdict == "syntax_error"


def test_edit_syntax_on_then_off_clears_staged_observation(tmp_path, monkeypatch):
    artifact = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
    if artifact not in sys.path:
        sys.path.insert(0, artifact)
    import gt_mini_patch as gmp

    rel = "pkg/mod.py"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(b"def f(\n")
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    monkeypatch.setattr(gmp, "_GT_BASELINE", False)
    monkeypatch.setattr(gmp, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(gmp, "_build_edit_check_executor", lambda: None)
    assert gmp._edit_syntax_candidate(rel) is not None
    assert gmp._last_edit_syntax_observation is not None

    monkeypatch.delenv("GT_EDIT_CHECK")
    monkeypatch.setattr(
        "builtins.open",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("flag-off read source")),
    )
    assert gmp._edit_syntax_candidate(rel) is None
    assert gmp._last_edit_syntax_observation is None


def test_candidate_does_not_attest_when_source_changes_during_check(tmp_path, monkeypatch):
    artifact = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
    if artifact not in sys.path:
        sys.path.insert(0, artifact)
    import gt_mini_patch as gmp
    from groundtruth.runtime import edit_check

    rel = "pkg/mod.py"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(b"def f(\n")
    monkeypatch.setenv("GT_EDIT_CHECK", "1")
    monkeypatch.setattr(gmp, "_GT_BASELINE", False)
    monkeypatch.setattr(gmp, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(gmp, "_build_edit_check_executor", lambda: None)

    def changing_check(*args, **kwargs):
        path.write_bytes(b"def g(\n")
        return {
            "verdict": "syntax_error",
            "diagnostic": 'File "pkg/mod.py", line 1\nSyntaxError: never closed',
            "language": ".py",
            "reason": "parse_error",
            "checker": ["ast.parse"],
        }

    monkeypatch.setattr(edit_check, "check_edit_syntax", changing_check)

    assert gmp._edit_syntax_candidate(rel) is not None
    assert gmp._last_edit_syntax_observation is None
