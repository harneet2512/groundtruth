"""Pin: SPEC §13.2 — the OH brief-survival channel (.groundtruth/BRIEF.md) at PARITY with DeepSWE.

The turn-0 brief scrolls out of context on a long OH run. DeepSWE (gt_agent.py:1783) persists it
to .groundtruth/BRIEF.md UNCONDITIONALLY (no flag) and appends the re-read pointer ONLY after a
CONFIRMED write (`if _art:` — "never point at a missing file"). OH runs host-side, so the write is
deferred to the first dispatch turn (orig_run_action reaches the container), but the PARITY
contract must hold:
  (a) patch-excluded (.groundtruth/ in _JUNK_DIRS AND added to .git/info/exclude);
  (b) ON by default (GT_BRIEF_SURVIVAL is only a kill-switch — the default matches DeepSWE);
  (c) the pointer is appended ONLY on a confirmed write, never on a failed/absent one.

A default-OFF port would NOT be parity (DeepSWE always fires); a pointer emitted independent of
the write would dangle at a missing file. Both are pinned here.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_WRAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "swebench"))
sys.path.insert(0, _WRAP_DIR)
for _mod in ("litellm", "cost_tracking"):
    sys.modules.setdefault(_mod, SimpleNamespace(
        model_cost={}, success_callback=[], completion=lambda *a, **k: None,
        acompletion=None, completion_cost=lambda *a, **k: 0.0,
        track_cost=lambda *a, **k: None, CostTracker=object))
try:
    import oh_gt_full_wrapper as _w
except Exception:  # heavy sibling deps unavailable
    _w = None

skip = pytest.mark.skipif(_w is None, reason="oh_gt_full_wrapper import unavailable")

_BRIEF = "<gt-task-brief>\nfoo.py :: bar — primary target\n</gt-task-brief>"


def _cfg(text=_BRIEF, written=False):
    return SimpleNamespace(_gt_brief_survival_text=text, _gt_brief_survival_written=written)


def _patch_io(monkeypatch, write_ok=True):
    """Capture the survival write + the container commands (for the git-exclude parity check)."""
    calls, cmds = [], []
    monkeypatch.setattr(_w, "_write_text_to_container",
                        lambda a, content, path: calls.append((content, path)) or write_ok)
    monkeypatch.setattr(_w, "_run_internal", lambda a, c, t=30: cmds.append(c) or "")
    return calls, cmds


@skip
@pytest.mark.parametrize("path", [".groundtruth/BRIEF.md", "a/b/.groundtruth/BRIEF.md"])
def test_survival_file_is_patch_excluded(path):
    assert _w._is_scaffolding_path(path), f"{path} must be patch-excluded (.groundtruth/ in _JUNK_DIRS)"


@skip
def test_flag_defaults_ON_for_parity_and_respects_killswitch(monkeypatch):
    monkeypatch.delenv("GT_BRIEF_SURVIVAL", raising=False)
    assert _w._brief_survival_enabled() is True    # PARITY: DeepSWE writes it unconditionally
    monkeypatch.setenv("GT_BRIEF_SURVIVAL", "0")
    assert _w._brief_survival_enabled() is False    # kill-switch only


@skip
def test_writes_excludes_and_points_on_confirmed_write(monkeypatch):
    monkeypatch.delenv("GT_BRIEF_SURVIVAL", raising=False)  # default ON
    calls, cmds = _patch_io(monkeypatch, write_ok=True)
    cfg = _cfg()
    out = _w._maybe_write_brief_survival(cfg, SimpleNamespace(content="orig"), lambda a: None)
    assert calls == [(_BRIEF, ".groundtruth/BRIEF.md")], calls
    assert cfg._gt_brief_survival_written is True
    # PARITY: .groundtruth/ added to .git/info/exclude
    assert any(".groundtruth/" in c and "exclude" in c for c in cmds), cmds
    # PARITY (DeepSWE `if _art:`): the re-read pointer is delivered on a CONFIRMED write
    body = getattr(out, "content", "")
    assert ".groundtruth/BRIEF.md" in body and "cat" in body
    # idempotent: a second turn does not rewrite
    n = len(calls)
    _w._maybe_write_brief_survival(cfg, SimpleNamespace(content="orig"), lambda a: None)
    assert len(calls) == n


@skip
def test_no_pointer_when_write_fails(monkeypatch):
    # DeepSWE parity: a FAILED write must NOT point the agent at a missing file
    monkeypatch.delenv("GT_BRIEF_SURVIVAL", raising=False)
    _patch_io(monkeypatch, write_ok=False)
    out = _w._maybe_write_brief_survival(_cfg(), SimpleNamespace(content="orig"), lambda a: None)
    assert getattr(out, "content", "") == "orig", "no pointer may be emitted on a failed write"


@skip
@pytest.mark.parametrize("mut", ["killswitch", "no_text", "already_written", "no_action"])
def test_correct_or_quiet(monkeypatch, mut):
    monkeypatch.setenv("GT_BRIEF_SURVIVAL", "0" if mut == "killswitch" else "1")
    calls, _ = _patch_io(monkeypatch, write_ok=True)
    cfg = _cfg(text="" if mut == "no_text" else _BRIEF, written=(mut == "already_written"))
    action = None if mut == "no_action" else (lambda a: None)
    out = _w._maybe_write_brief_survival(cfg, SimpleNamespace(content="orig"), action)
    assert calls == [], f"{mut}: survival write must stay quiet, got {calls}"
    assert getattr(out, "content", "") == "orig", f"{mut}: no pointer expected"
