"""Live mini-swe executor adapter preserves ambiguous command truth as unknown."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


def _executor_for(monkeypatch, result):
    monkeypatch.setattr(g, "_GT_LIVE_ENV", object())
    monkeypatch.setattr(g, "_GT_LIVE_ORIG_EXECUTE", lambda *args, **kwargs: result)
    executor = g._build_env_executor()
    assert executor is not None
    return executor


@pytest.mark.parametrize("returncode", [None, False, 0.0, "0", object()])
def test_live_adapter_missing_or_invalid_returncode_stays_none(monkeypatch, returncode):
    result = {"output": "SyntaxError: invalid syntax"}
    if returncode is not None:
        result["returncode"] = returncode

    rc, out, err = _executor_for(monkeypatch, result)(["python", "--version"], "/testbed", 5)

    assert rc is None
    assert out == "SyntaxError: invalid syntax"
    assert err == ""


def test_live_adapter_preserves_strict_integer_returncode(monkeypatch):
    rc, out, err = _executor_for(
        monkeypatch, {"output": "", "returncode": 7}
    )(["node", "--check", "x.js"], "/testbed", 5)

    assert (rc, out, err) == (7, "", "")


def test_live_adapter_hostile_output_makes_execution_ambiguous(monkeypatch):
    rc, out, err = _executor_for(
        monkeypatch, {"output": ["not", "text"], "returncode": 0}
    )(["node", "--check", "x.js"], "/testbed", 5)

    assert (rc, out, err) == (None, "", "")


def test_live_adapter_unknown_result_shape_has_no_fabricated_failure(monkeypatch):
    rc, out, err = _executor_for(monkeypatch, ["not", "a", "result"])(
        ["node", "--check", "x.js"], "/testbed", 5
    )

    assert (rc, out, err) == (None, "", "")


def test_live_adapter_exception_has_no_fabricated_failure(monkeypatch):
    monkeypatch.setattr(g, "_GT_LIVE_ENV", object())

    def raises(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(g, "_GT_LIVE_ORIG_EXECUTE", raises)
    executor = g._build_env_executor()
    assert executor is not None

    rc, out, err = executor(["node", "--check", "x.js"], "/testbed", 5)

    assert rc is None
    assert out == ""
    assert "gt_executor_unavailable: RuntimeError" in err


def test_live_adapter_does_not_retry_command_when_execute_raises_typeerror(monkeypatch):
    """A TypeError from inside execute is not proof its signature lacks timeout."""
    calls = []
    monkeypatch.setattr(g, "_GT_LIVE_ENV", object())

    def raises_after_execution(env, action, cwd, *, timeout=None):
        calls.append((action, cwd, timeout))
        raise TypeError("command implementation failed")

    monkeypatch.setattr(g, "_GT_LIVE_ORIG_EXECUTE", raises_after_execution)
    executor = g._build_env_executor()
    assert executor is not None

    rc, out, err = executor(["node", "--check", "x.js"], "/testbed", 5)

    assert len(calls) == 1
    assert rc is None and out == ""
    assert "gt_executor_unavailable: TypeError" in err


def test_live_adapter_supports_execute_without_timeout_parameter(monkeypatch):
    monkeypatch.setattr(g, "_GT_LIVE_ENV", object())

    def execute_without_timeout(env, action, cwd):
        return {"output": "clean", "returncode": 0}

    monkeypatch.setattr(g, "_GT_LIVE_ORIG_EXECUTE", execute_without_timeout)
    executor = g._build_env_executor()
    assert executor is not None

    assert executor(["node", "--check", "x.js"], "/testbed", 5) == (0, "clean", "")


def test_live_adapter_rejects_string_subclass_output(monkeypatch):
    class HostileText(str):
        def strip(self, *args, **kwargs):
            raise RuntimeError("must not execute subclass methods")

    executor = _executor_for(
        monkeypatch, {"output": HostileText("clean"), "returncode": 0}
    )

    assert executor(["node", "--check", "x.js"], "/testbed", 5) == (None, "", "")
