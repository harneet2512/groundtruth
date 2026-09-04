"""P1-11 — structured adapter error scan from result.json."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCAN = _ROOT / "scripts" / "swebench" / "adapter_error_scan.py"


def test_scan_finds_structured_adapter_error(tmp_path):
    jobs = tmp_path / "jobs" / "20260101" / "task__abc"
    jobs.mkdir(parents=True)
    (jobs / "result.json").write_text(
        json.dumps({"exception_message": "DeepSweAdapterError: brief missing"}),
        encoding="utf-8",
    )
    mod = __import__("importlib").import_module("importlib.util")
    spec = mod.spec_from_file_location("adapter_error_scan_uut", _SCAN)
    assert spec and spec.loader
    uut = mod.module_from_spec(spec)
    spec.loader.exec_module(uut)
    hits = uut.scan_jobs_dir(str(tmp_path / "jobs"))
    assert len(hits) == 1
    assert "DeepSweAdapterError" in hits[0]["error"]


def test_cli_exits_nonzero_on_hit(tmp_path):
    jobs = tmp_path / "jobs" / "run" / "t__1"
    jobs.mkdir(parents=True)
    (jobs / "result.json").write_text(
        json.dumps({"info": {"exception_message": "DeepSweAdapterError: x"}}),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(_SCAN), str(tmp_path / "jobs"), str(tmp_path / "missing.log")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "DEEPSWE_ADAPTER_FAIL" in proc.stdout
