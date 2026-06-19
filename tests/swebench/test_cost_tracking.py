from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[2]
SWE_DIR = REPO_ROOT / "scripts" / "swebench"
if str(SWE_DIR) not in sys.path:
    sys.path.insert(0, str(SWE_DIR))


def _load_cost_tracking(monkeypatch, *, completion=None, acompletion=None):
    litellm_stub = SimpleNamespace(
        model_cost={},
        success_callback=[],
        completion=completion or (lambda *args, **kwargs: SimpleNamespace(choices=[])),
        acompletion=acompletion,
        completion_cost=lambda *args, **kwargs: 0.0,
    )
    monkeypatch.setitem(sys.modules, "litellm", litellm_stub)
    sys.modules.pop("cost_tracking", None)
    return importlib.import_module("cost_tracking")


def test_sync_tool_injection_logs_no_tools_payload(monkeypatch, capsys):
    monkeypatch.setenv("GT_NATIVE_TOOLS", "1")
    monkeypatch.delenv("GT_BASELINE", raising=False)
    cost_tracking = _load_cost_tracking(monkeypatch)

    cost_tracking.litellm.completion(model="x", messages=[])

    captured = capsys.readouterr()
    assert "tool_injection_skip: reason=no_tools_payload" in captured.out


def test_async_tool_injection_logs_no_tools_payload(monkeypatch, capsys):
    async def fake_acompletion(*args, **kwargs):
        return SimpleNamespace(choices=[])

    monkeypatch.setenv("GT_NATIVE_TOOLS", "1")
    monkeypatch.delenv("GT_BASELINE", raising=False)
    cost_tracking = _load_cost_tracking(monkeypatch, acompletion=fake_acompletion)

    import asyncio

    asyncio.run(cost_tracking.litellm.acompletion(model="x", messages=[]))

    captured = capsys.readouterr()
    assert "async_tool_injection_skip: reason=no_tools_payload" in captured.out


def test_async_tool_rewrite_exception_is_logged(monkeypatch, capsys):
    class BadResult:
        @property
        def choices(self):
            raise RuntimeError("bad choices")

    async def fake_acompletion(*args, **kwargs):
        return BadResult()

    monkeypatch.setenv("GT_NATIVE_TOOLS", "0")
    cost_tracking = _load_cost_tracking(monkeypatch, acompletion=fake_acompletion)

    import asyncio

    asyncio.run(cost_tracking.litellm.acompletion(model="x", messages=[]))

    captured = capsys.readouterr()
    assert "async_tool_rewrite_error: bad choices" in captured.out
