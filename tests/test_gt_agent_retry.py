"""P6 / CP012 option 2 — verifier failure classification + gate note."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("pier", reason="pier is not installed")

_ROOT = Path(__file__).resolve().parents[1]
_AGENT_PATH = _ROOT / "artifact_deepswe" / "gt_agent.py"


def _load_agent():
    spec = importlib.util.spec_from_file_location("gt_agent_p6", _AGENT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gt_agent_p6"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ga():
    return _load_agent()


def test_classify_assertion_error(ga):
    out = "FAILED tests/test_x.py::test_y - AssertionError: expected 1 got 2"
    assert ga._classify_verifier_failure(out) == "assertion_error"


def test_classify_compile_error(ga):
    """D6: ModuleNotFoundError is now compile_error (agent-caused), not env_failure."""
    out = "ModuleNotFoundError: No module named 'foo'"
    assert ga._classify_verifier_failure(out) == "compile_error"


def test_classify_hard_env_is_unverifiable(ga):
    """D6: hard env patterns stay in _ENV_UNVERIFIABLE_RE — never reach classifier."""
    out = "bash: cargo: command not found"
    assert ga._ENV_UNVERIFIABLE_RE.search(out) is not None


def test_feedback_includes_failure_class(ga):
    fb = ga._format_test_feedback(2, "pytest", 1, "AssertionError: boom")
    assert 'failure="assertion_error"' in fb
    assert "Logic bug" in fb
