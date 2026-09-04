"""P2-06 baseline rerun guard."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


def _load():
    path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "metrics", "compute_paired_metrics.py"
    )
    spec = importlib.util.spec_from_file_location("compute_paired_metrics", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["compute_paired_metrics"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_rejects_gt_on_baseline_dir():
    mod = _load()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        outcome = base / "outcome.json"
        outcome.write_text(
            json.dumps({"tasks": [{"gt_prebuilt_active": True}]}),
            encoding="utf-8",
        )
        with pytest.raises(SystemExit):
            mod._assert_baseline_arm_is_off(base)
