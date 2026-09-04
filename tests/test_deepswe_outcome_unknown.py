"""P2-08 unknown_reason tests."""

from __future__ import annotations

import importlib.util
import os


def _load():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "verify", "deepswe_outcome.py")
    spec = importlib.util.spec_from_file_location("deepswe_outcome_unknown", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unknown_reason_agent_never_ran():
    mod = _load()
    rec = {"failure_class": "UNKNOWN", "reward": 0.0, "n_agent_steps": 0}
    assert mod.unknown_reason(rec) == "agent_never_ran"
